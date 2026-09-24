"""Pure whole-target borrowing checks: no storage, model, or application calls."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from app.domain.services.analysis_report_review import ReportTarget, fit_report_targets
from app.domain.services.analysis_review_capacity import restore_whole_targets_with_spare_capacity


def serialize(value):
    # Same options as analysis_answer_review._json, without importing its driver.
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def target(text="x,y\n1,2\n", *, index=1, kind="structured", **changes):
    raw = text.encode("utf-8")
    return replace(ReportTarget(
        report_id=f"report_{index:04d}", size=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
        text=text, read_complete=True, reason="ready", target_kind=kind,
        format="csv" if kind == "structured" else "md", file_id=f"private-id-{index}",
        file_path=f"/home/ubuntu/output/private-{index}.csv",
    ), **changes)


def measured(payload, targets, serializer=serialize):
    return len(serializer({**deepcopy(payload), "report_targets": [t.payload() for t in targets]}).encode("utf-8"))


def restore(payload, targets, *, text_budget=0, envelope=120000, serializer=serialize):
    return restore_whole_targets_with_spare_capacity(
        payload=payload, targets=targets, text_budget=text_budget,
        borrow_envelope_bytes=envelope, serialize=serializer,
    )


def test_complete_33kb_csv_is_restored_without_truncation_or_source_mutation():
    body = "x,y\n" + "1,2\n" * 8405
    assert len(body.encode()) == 33624
    source = target(body)
    payload = {"question": "Compare every row", "draft": "observed results", "requirements": [{"index": 0}],
               "sources": [{"source_id": "measurement", "text": "original observations"},
                           {"source_id": "method", "text": "complete observed method", "method_only": True}]}
    before = deepcopy(payload)
    legacy = fit_report_targets((source,), text_budget=24000)
    assert legacy[0].reason == "report_context_limit" and legacy[0].text is None
    result, meta = restore(payload, [source], text_budget=24000)
    assert result == (source,) and result[0] is source
    assert "".join(b["text"] for b in result[0].blocks()) == body
    assert result[0].blocks()[-1]["byte_end"] == 33624
    assert meta == {"version": 1, "baseline_payload_bytes": measured(payload, legacy),
                    "final_payload_bytes": measured(payload, result), "borrow_envelope_bytes": 120000,
                    "restored_target_count": 1, "status": "within"}
    assert payload == before and source.text == body
    assert "private" not in serialize(meta) and "original observations" not in serialize(meta)


@pytest.mark.parametrize("body", ["汉字🙂" * 30, '\\"quoted\\"\r\n' * 30, "a,b\n" * 1700],
                         ids=["multibyte-utf8", "escaped-characters", "multiple-blocks"])
def test_utf8_escaping_and_block_metadata_count_at_exact_payload_boundary(body):
    source = target(body)
    payload = {"question": "中文", "sources": [{"text": "context"}], "requirements": [{"objective": "all rows"}]}
    required = measured(payload, (source,))
    assert required > len(body.encode())
    rejected, meta = restore(payload, (source,), envelope=required - 1)
    assert rejected[0].reason == "report_context_limit" and meta["restored_target_count"] == 0
    accepted, meta = restore(payload, (source,), envelope=required)
    assert accepted == (source,) and meta["final_payload_bytes"] == required


def test_every_base_payload_field_counts_not_only_evidence_text():
    source = target("row\n" * 50)
    payload = {"question": "q" * 100, "draft": "d" * 100, "sources": [],
               "requirements": [{"objective": "r" * 100}], "inventory": [{"description": "i" * 100}]}
    cutoff = measured({"sources": []}, (source,))
    assert measured(payload, fit_report_targets((source,), text_budget=0)) > cutoff
    result, meta = restore(payload, (source,), envelope=cutoff)
    assert result[0].text is None and meta["status"] == "legacy_over"


def test_legacy_over_envelope_stays_exactly_legacy_without_new_credit():
    kept = target("small", index=1)
    omitted = target("large" * 200, index=2)
    payload = {"sources": [{"text": "e" * 120000}]}
    baseline = fit_report_targets((kept, omitted), text_budget=5)
    calls = []
    result, meta = restore(payload, (kept, omitted), text_budget=5,
                           serializer=lambda value: calls.append(deepcopy(value)) or serialize(value))
    assert result == baseline and result[0].text == "small"
    assert meta["baseline_payload_bytes"] == meta["final_payload_bytes"] > 120000
    assert meta["restored_target_count"] == 0 and meta["status"] == "legacy_over"
    assert len(calls) == 1


def test_equal_baseline_envelope_does_not_attempt_borrowing():
    source = target("omitted")
    baseline = fit_report_targets((source,), text_budget=0)
    calls = []
    result, meta = restore({}, (source,), envelope=measured({}, baseline),
                           serializer=lambda value: calls.append(value) or serialize(value))
    assert result == baseline and meta["status"] == "within"
    assert len(calls) == 1 and meta["restored_target_count"] == 0


@pytest.mark.parametrize("changes", [
    {"sha256": "0" * 64}, {"sha256": None}, {"size": 1}, {"size": True},
    {"read_complete": False}, {"read_complete": 1}, {"text": None},
    {"reason": "protected_report_content"}, {"reason": "report_read_limit"},
])
def test_invalid_or_unread_original_cannot_be_resurrected(changes):
    source = replace(target("unverified body"), **changes)
    result, meta = restore({}, (source,))
    assert result[0].text is None
    assert meta["restored_target_count"] == 0
    assert result == fit_report_targets((source,), text_budget=0)


def test_report_priority_and_original_order_within_each_kind():
    targets = (target("S" * 3000, index=1), target("R" * 3000, index=2, kind="report"),
               target("T" * 3000, index=3, kind="report"), target("U" * 3000, index=4))
    baseline = fit_report_targets(targets, text_budget=0)
    one_report = list(baseline); one_report[1] = targets[1]
    result, meta = restore({}, targets, envelope=measured({}, one_report))
    assert [t.report_id for t in result] == [t.report_id for t in targets]
    assert [t.text is not None for t in result] == [False, True, False, False]
    assert meta["restored_target_count"] == 1


def test_nonfitting_report_does_not_block_later_whole_targets_that_fit():
    targets = (target("large" * 2000, index=1, kind="report"), target("small", index=2))
    baseline = fit_report_targets(targets, text_budget=0)
    result, meta = restore({}, targets, envelope=measured({}, (baseline[0], targets[1])))
    assert result == (baseline[0], targets[1]) and meta["restored_target_count"] == 1


def test_serializer_mutation_cannot_change_input_or_later_candidate_bases():
    payload = {"sources": [{"text": "immutable"}]}
    before = deepcopy(payload); observed = []

    def mutating(value):
        observed.append(deepcopy(value["sources"]))
        encoded = serialize(value)
        value["sources"][0]["text"] = "changed"
        return encoded

    targets = [target("first", index=1), target("second", index=2)]
    before_targets = targets.copy()
    result, meta = restore(payload, targets, serializer=mutating)
    assert result == tuple(targets) and meta["restored_target_count"] == 2
    assert payload == before and targets == before_targets
    assert len(observed) == 3 and all(x == before["sources"] for x in observed)


def test_supplied_serializer_including_its_escaping_is_authoritative():
    source = target("中文🙂" * 30)
    ascii_serializer = lambda value: json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    ordinary_size = measured({}, (source,))
    assert measured({}, (source,), ascii_serializer) > ordinary_size
    result, meta = restore({}, (source,), envelope=ordinary_size, serializer=ascii_serializer)
    assert result[0].text is None and meta["restored_target_count"] == 0


def test_no_targets_and_already_fitted_targets_keep_legacy_identity():
    assert restore({}, ())[0] == ()
    source = target("already fits")
    result, meta = restore({}, (source,), text_budget=1000)
    assert result == (source,) and meta["restored_target_count"] == 0
    assert meta["baseline_payload_bytes"] == meta["final_payload_bytes"]


@pytest.mark.parametrize("kwargs", [{"text_budget": True}, {"envelope": -1}, {"envelope": True}])
def test_malformed_budgets_fail_with_fixed_safe_code(kwargs):
    with pytest.raises(ValueError, match="^invalid_review_capacity_budget$"):
        restore({}, (), **kwargs)


def test_existing_targets_in_base_payload_are_not_silently_overwritten():
    with pytest.raises(ValueError, match="^review_capacity_targets_already_present$"):
        restore({"report_targets": []}, ())
