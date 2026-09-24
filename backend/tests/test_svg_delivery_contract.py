"""Vector image receipts follow the same exact-byte delivery contract."""
import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.file import FileInfo
from app.domain.services.analysis_completion import artifact_kind, assess_delivery, verified_deliveries


def svg_artifact():
    path = "/home/ubuntu/output/chart.svg"
    record = {"path": path, "expected_kind": "image", "kind": "image", "valid": True,
              "reason": "validated", "sha256": "a" * 64, "size": 100,
              "metadata": {"format": "SVG", "width": 200, "height": 100, "frames": 1,
                           "validation_level": "safe_static_svg_render"}}
    upload = FileInfo(file_id="svg-artifact", file_path=path, size=100,
                      metadata={"artifact_sha256": "a" * 64})
    return record, upload


def test_verified_svg_satisfies_image_requirement():
    record, upload = svg_artifact()
    assert artifact_kind(upload.file_path) == "image"
    assert verified_deliveries([record], [upload]) == [upload]
    outcome = assess_delivery([DeliverableRequirement(kind="image", formats=["svg"])],
                              [record], [upload], execution_success=True)
    assert outcome.status == "succeeded"
    assert outcome.missing == []


@pytest.mark.parametrize("reason", ["missing_artifact", "invalid_content", "validator_unavailable"])
def test_svg_failure_is_not_promoted_to_success(reason):
    record, upload = svg_artifact()
    record.update(valid=False, reason=reason)
    assert verified_deliveries([record], [upload]) == []
    outcome = assess_delivery([DeliverableRequirement(kind="image")], [record], [upload], execution_success=True)
    assert outcome.status != "succeeded"
    assert outcome.missing


def test_svg_must_match_uploaded_bytes():
    record, upload = svg_artifact()
    upload.metadata["artifact_sha256"] = "b" * 64
    assert verified_deliveries([record], [upload]) == []
