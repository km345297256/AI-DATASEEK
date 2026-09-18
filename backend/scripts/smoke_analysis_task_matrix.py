"""Opt-in, serial real-model acceptance of complete analysis deliveries.

Each --live invocation creates new [回归验证] sessions. It never resumes or reruns
old work. --inspect-report only GETs sessions/downloads from an earlier report.
Reports retain synthetic/public requests, opaque IDs and bounded evidence, never
raw model/tool output, credentials, or host dataset paths. No automatic retries.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError

try:
    from .smoke_dataset_delivery import api_data, delivery_outcomes_complete, extract_delivery
except ImportError:
    from smoke_dataset_delivery import api_data, delivery_outcomes_complete, extract_delivery


IRIS_ID = "open-uci-iris"
IRIS_SOURCE = Path(__file__).resolve().parents[1] / "app/resources/datasets/open-uci-iris/iris.csv"
SYNTHETIC_SOURCE = b"label,value\nA,2\nB,4\nC,6\nD,8\nE,10\nF,12\n"
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class CheckFailure(Exception):
    """A deliberately safe diagnostic code, without API bodies or tool text."""


@dataclass(frozen=True)
class Case:
    name: str
    message: str
    filenames: tuple[str, ...]
    csv_columns: tuple[str, ...] = ()
    expected_status: str = "succeeded"
    unavailable_filenames: tuple[str, ...] = ()


CASES = {
    "synthetic": Case(
        "synthetic",
        "分析本次上传的 task_matrix_input.csv。计算 value 列非空数值的 count、mean、min、max。"
        "只交付两个文件：task_matrix_summary.csv 和 task_matrix_bar.png。"
        "CSV 必须是 UTF-8，列名依次为 count,mean,min,max，且只有一行统计结果。"
        "PNG 为 label 对 value 的柱状图，完整显示六个标签及数值，图内标签用英文。"
        "最终回答简要说明统计结论并提供这两个文件；不要交付 Python 源码、报告或其他额外文件。",
        ("task_matrix_summary.csv", "task_matrix_bar.png"),
        ("count", "mean", "min", "max"),
    ),
    "iris": Case(
        "iris",
        "使用当前 Iris 数据集中的 iris.csv，按 species 分组统计样本数及 sepal_length_cm 均值。"
        "只交付 iris_species_summary.csv 和 iris_species_scatter.png 两个文件。"
        "CSV 必须是 UTF-8，列名依次为 species,count,sepal_length_mean；"
        "species 保留源数据完整名称，均值至少保留 9 位小数。"
        "PNG 横轴 sepal_length_cm、纵轴 sepal_width_cm，全部150个观测按 species 着色，"
        "显示三类图例及英文坐标轴标签。最终回答简要说明统计结论并提供两个文件。"
        "不要交付源代码、报告或其他额外文件。",
        ("iris_species_summary.csv", "iris_species_scatter.png"),
        ("species", "count", "sepal_length_mean"),
    ),
    "followup": Case(
        "followup",
        "继续使用本会话的 Iris 数据及刚才统计。只在回答中给出一个 JSON 对象："
        "total_count 为总观测数，species_counts 为完整 species 名称到观测数的对象，"
        "max_mean_species 为 sepal_length_cm 均值最大的完整 species 名称。不要把 JSON 写入文件。"
        "无需生成或交付任何文件；"
        "不要重新附上上一轮文件，也不要生成新图、新表、报告或代码。",
        (),
    ),
    "missing-column": Case(
        "missing-column",
        "检查上传的 task_matrix_input.csv 是否有 missing_measurement 列；"
        "如果不存在，明确说明缺少该列，列出真实列名并请求正确列名。"
        "不要猜测、用 value 替代、编造统计结果或生成任何文件。",
        (),
    ),
    "partial": Case(
        "partial",
        "分析上传的 task_matrix_input.csv，分别计算 value 和 missing_measurement 两列的"
        "非空数值 count、mean、min、max，并分别交付 task_matrix_value_summary.csv 和 "
        "task_matrix_missing_summary.csv。每份CSV必须为UTF-8，列名依次是 count,mean,min,max，"
        "且只有一行统计结果。如果某列不存在，不要猜测、替换列名或编造数据，也不要生成该列的统计文件；"
        "继续完成存在列的真实统计并保留其CSV交付。最终明确说明完成了哪部分、缺少哪列，"
        "以及因此未生成哪个文件。不要交付代码、报告、图或其他额外文件。",
        ("task_matrix_value_summary.csv",), ("count", "mean", "min", "max"),
        expected_status="partial", unavailable_filenames=("task_matrix_missing_summary.csv",),
    ),
}


def require(condition, code):
    if not condition:
        raise CheckFailure(code)


def safe_code(value):
    return value if isinstance(value, str) and CODE.fullmatch(value) else "unavailable"


def safe_name(value):
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(char for char in name if char.isprintable())[:160]


def safe_outcomes(outcomes):
    return {safe_code(key): {
        "status": safe_code(value.get("status")),
        "reason_code": safe_code(value.get("reason_code")),
        "can_resume": value.get("can_resume") is True,
        "missing_count": len(value.get("missing") or []),
        "issues": [{"artifact_name": safe_name(issue.get("artifact_name")),
                    "reason_code": safe_code(issue.get("reason_code")),
                    "blocking": issue.get("blocking") is True}
                   for issue in value.get("issues", [])[:30]],
    } for key, value in outcomes.items()}


def read_csv(data):
    try:
        text = data.decode("utf-8-sig", errors="strict")
        require("\x00" not in text, "csv_contains_null")
        rows = list(csv.reader(StringIO(text, newline=""), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise CheckFailure("csv_not_complete_utf8") from exc
    require(bool(rows) and bool(rows[0]), "csv_empty")
    require(len(set(rows[0])) == len(rows[0]), "csv_duplicate_columns")
    require(all(len(row) == len(rows[0]) for row in rows[1:]), "csv_ragged_rows")
    return rows[0], [dict(zip(rows[0], row)) for row in rows[1:]]


def oracle_rows(case_name):
    """Independent stdlib Decimal oracle; no generated code or model assertions."""
    if case_name in {"synthetic", "partial"}:
        _, rows = read_csv(SYNTHETIC_SOURCE)
        values = [Decimal(row["value"]) for row in rows]
        return [{"count": Decimal(len(values)), "mean": sum(values) / len(values),
                 "min": min(values), "max": max(values)}]
    if case_name == "iris":
        _, rows = read_csv(IRIS_SOURCE.read_bytes())
        groups = defaultdict(list)
        for row in rows:
            groups[row["species"]].append(Decimal(row["sepal_length_cm"]))
        return [{"species": species, "count": Decimal(len(values)),
                 "sepal_length_mean": sum(values) / len(values)}
                for species, values in sorted(groups.items())]
    return []


def verify_statistics(case, data):
    columns, actual = read_csv(data)
    require(tuple(columns) == case.csv_columns, "csv_columns_do_not_match_request")
    expected = oracle_rows(case.name)
    require(len(actual) == len(expected), "csv_wrong_row_count")
    if case.name == "iris":
        require(len({row["species"] for row in actual}) == len(actual), "csv_duplicate_species")
        actual.sort(key=lambda row: row["species"])
    for row, reference in zip(actual, expected):
        for field, expected_value in reference.items():
            if isinstance(expected_value, str):
                require(row[field] == expected_value, "csv_group_differs_from_source")
                continue
            try:
                value = Decimal(row[field])
            except InvalidOperation as exc:
                raise CheckFailure("csv_invalid_number") from exc
            require(value.is_finite(), "csv_nonfinite_number")
            tolerance = Decimal(0) if field == "count" else Decimal("1e-9")
            require(abs(value - expected_value) <= tolerance, f"csv_wrong_{field}")
    return {"rows": len(actual), "columns": columns, "oracle": "stdlib_decimal",
            "absolute_tolerance": "1e-9", "values": actual}


def verify_png(data):
    try:
        with Image.open(BytesIO(data)) as image:
            require(image.format == "PNG", "png_wrong_format")
            require(image.width * image.height <= 16_000_000, "png_pixel_budget_exceeded")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()  # verify() alone does not decode the complete pixel payload.
            require(min(image.size) >= 200, "png_too_small")
            extrema = image.convert("RGB").getextrema()
            require(any(high - low > 16 for low, high in extrema), "png_visually_blank")
            return {"format": "PNG", "width": image.width, "height": image.height,
                    "full_decode": True, "nonblank": True}
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise CheckFailure("png_cannot_fully_decode") from exc


def download_bytes(client, file_id):
    chunks, size = [], 0
    with client.stream("GET", f"/files/{quote(file_id, safe='')}/download") as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            size += len(chunk)
            require(size <= MAX_DOWNLOAD_BYTES, "download_exceeds_budget")
            chunks.append(chunk)
        content_length = response.headers.get("content-length")
        if content_length is not None and not response.headers.get("content-encoding"):
            require(content_length.isdecimal() and int(content_length) == size, "download_incomplete")
    require(size > 0, "download_empty")
    return b"".join(chunks)


def latest_answer(session):
    messages = []
    for event in session.get("events", []):
        data = event.get("data") or {}
        if event.get("event") == "message" and data.get("role") == "user":
            messages = []
        elif event.get("event") == "message" and data.get("role") == "assistant":
            messages.append(data.get("content") or "")
    return "\n".join(messages)


def verify_followup_facts(answer):
    expected = oracle_rows("iris")
    counts = {row["species"]: int(row["count"]) for row in expected}
    wanted = {"total_count": sum(counts.values()), "species_counts": counts,
              "max_mean_species": max(expected, key=lambda row: row["sepal_length_mean"])["species"]}
    # Allow a Markdown code fence around the requested inline JSON, but verify
    # every group count and the winning category, not isolated number mentions.
    for match in re.finditer(r"\{", answer):
        try:
            value, _ = json.JSONDecoder().raw_decode(answer[match.start():])
        except json.JSONDecodeError:
            continue
        if value == wanted:
            return True
    return False


def partial_outcomes_match(outcomes, case):
    """A requested impossible subset must be explicit, never fabricated success."""
    values = list(outcomes.values())
    if not values or not any(value.get("status") == "partial" for value in values):
        return False
    if any(value.get("status") not in {"succeeded", "partial"} for value in values):
        return False
    missing_names = set(case.unavailable_filenames)
    if any(issue.get("blocking") and issue.get("artifact_name") not in missing_names
           for value in values for issue in value.get("issues", [])):
        return False
    for value in values:
        for issue in value.get("issues", []):
            if issue.get("blocking") and issue.get("artifact_name") in missing_names:
                return True
        for missing in value.get("missing", []):
            label = str(missing.get("label") or "")
            if "missing_measurement" in label or any(name in label for name in missing_names):
                return True
    return False


def requested_turn(session, client_message_id):
    """Select a recorded request, including after another turn has completed."""
    events = session.get("events", [])
    starts = [index for index, event in enumerate(events)
              if event.get("event") == "message" and event.get("data", {}).get("role") == "user"
              and (event["data"].get("metadata") or {}).get("client_message_id") == client_message_id]
    require(len(starts) == 1, "requested_turn_not_uniquely_recorded")
    start = starts[0]
    end = next((index for index in range(start + 1, len(events))
                if events[index].get("event") == "message"
                and events[index].get("data", {}).get("role") == "user"), len(events))
    return {**session, "events": events[start:end]}


def verify_case(client, case, session_id, client_message_id=None):
    """GET-only validation. Preserve failures instead of accepting SSE/prose."""
    session = api_data(client.get(f"/sessions/{quote(session_id, safe='')}"))
    if client_message_id is not None:
        session = requested_turn(session, client_message_id)
    result = {"case": case.name, "session_id": session_id,
              "session_status": safe_code(session.get("status")), "errors": [], "artifacts": []}
    if session.get("status") != "completed":
        result["errors"].append("session_not_completed")
    try:
        extracted = extract_delivery(session)
    except (AssertionError, KeyError, TypeError) as exc:
        result["errors"].append("malformed_authoritative_outcome")
        result["passed"] = False
        return result
    result["outcomes"] = safe_outcomes(extracted["outcomes"])
    result["step_statuses"] = {safe_code(key): safe_code(value)
                                for key, value in extracted["step_statuses"].items()}
    if case.expected_status == "partial":
        if not partial_outcomes_match(extracted["outcomes"], case):
            result["errors"].append("authoritative_partial_or_explicit_missing_item_absent")
    elif not delivery_outcomes_complete(extracted["outcomes"]):
        result["errors"].append("authoritative_outcome_not_succeeded")
    delivered = list(extracted["attachments"].values())
    result["delivered_filenames"] = [safe_name(item["filename"]) for item in delivered]
    if Counter(item["filename"] for item in delivered) != Counter(case.filenames):
        result["errors"].append("delivered_filenames_do_not_exactly_match_request")
    for item in delivered:
        filename = item["filename"]
        if filename not in case.filenames:
            continue
        entry = {"filename": filename, "file_id": item["file_id"]}
        try:
            data = download_bytes(client, item["file_id"])
            entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            if isinstance(item.get("size"), int):
                require(item["size"] == len(data), "download_differs_from_declared_size")
            if filename.endswith(".csv"):
                entry["statistics"] = verify_statistics(case, data)
            elif filename.endswith(".png"):
                entry["image"] = verify_png(data)
                # Pixels alone cannot establish axis/species semantics; the final
                # authoritative answer review is required independently above.
            entry["passed"] = True
        except CheckFailure as exc:
            entry.update(passed=False, error=str(exc))
            result["errors"].append(str(exc))
        except httpx.HTTPError:
            entry.update(passed=False, error="artifact_download_failed")
            result["errors"].append("artifact_download_failed")
        result["artifacts"].append(entry)
    answer = latest_answer(session)
    if case.name == "followup":
        result["inline_facts_verified"] = verify_followup_facts(answer)
        if not result["inline_facts_verified"]:
            result["errors"].append("followup_facts_do_not_match_oracle")
    if case.name in {"missing-column", "partial"}:
        expected_columns = ("label", "value") if case.name == "missing-column" else ("value",)
        if not ("missing_measurement" in answer and all(name in answer for name in expected_columns)
                and any(word in answer for word in ("不存在", "缺少", "没有", "not found", "missing"))):
            result["errors"].append("missing_column_not_explained")
        if case.name == "partial" and not all(name in answer for name in case.unavailable_filenames):
            result["errors"].append("partial_unavailable_filename_not_explained")
    result["passed"] = not result["errors"]
    return result


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def send_once(client, session_id, request, max_seconds):
    started = time.monotonic()
    started_wall = datetime.now(timezone.utc)
    event_type, data_lines, counts = "", [], Counter()
    terminal = None
    with client.stream("POST", f"/sessions/{quote(session_id, safe='')}/chat", json=request,
                       timeout=httpx.Timeout(max_seconds, connect=15)) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            require(time.monotonic() - started <= max_seconds, "analysis_deadline_exceeded")
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line and data_lines:
                try:
                    data = json.loads("\n".join(data_lines))
                except json.JSONDecodeError as exc:
                    raise CheckFailure("invalid_sse_json") from exc
                data_lines = []
                counts[safe_code(event_type)] += 1
                if event_type == "step":
                    emit({"session_id": session_id, "step_status": safe_code(data.get("status"))})
                if event_type in {"done", "error", "wait"}:
                    terminal = event_type
                    break
    require(terminal is not None, "stream_ended_without_terminal")
    # Terminal SSE can precede persistence. Poll status, never resubmit a task.
    for _ in range(40):
        state = api_data(client.get(f"/sessions/{quote(session_id, safe='')}"))
        if state.get("status") not in {"pending", "running"}:
            break
        time.sleep(.25)
    elapsed = time.monotonic() - started
    finished_wall = datetime.now(timezone.utc)
    wall_elapsed = (finished_wall - started_wall).total_seconds()
    return {"terminal_event": terminal, "event_counts": dict(counts),
            "elapsed_seconds": round(elapsed, 2),
            "started_at": started_wall.isoformat(), "finished_at": finished_wall.isoformat(),
            "wall_elapsed_seconds": round(wall_elapsed, 2),
            "clock_discontinuity_seconds": round(wall_elapsed - elapsed, 2)}


def validate_base_url(base_url):
    parsed = urlsplit(base_url)
    require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port == 7001 and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "only_existing_loopback_7001_is_allowed")
    return base_url.rstrip("/") + "/api/v1"


def verify_public_iris_source(client):
    prepared = api_data(client.post(f"/datasets/{IRIS_ID}/files/preview",
                                   json={"path": "iris.csv", "plugin_id": "csv"}))
    source = download_bytes(client, prepared["file"]["file_id"])
    require(source == IRIS_SOURCE.read_bytes(), "registered_iris_differs_from_local_oracle")
    return {"dataset_id": IRIS_ID, "filename": "iris.csv", "bytes": len(source),
            "sha256": hashlib.sha256(source).hexdigest()}


def run_matrix(base_url, output, case_names=("synthetic", "iris", "followup"), max_seconds=600):
    api_base = validate_base_url(base_url)
    require(all(name in CASES for name in case_names), "unknown_case")
    require("followup" not in case_names or "iris" in case_names, "followup_requires_new_iris_case")
    require(len(set(case_names)) == len(case_names), "duplicate_case")
    require(not Path(output).exists(), "report_already_exists_use_new_path")
    report = {"schema_version": 1, "run_id": uuid4().hex,
              "started_at": datetime.now(timezone.utc).isoformat(), "base_url": base_url,
              "cases": [], "passed": False, "automatic_retries": 0}
    save_report(output, report)
    iris_session = None
    with httpx.Client(base_url=api_base, timeout=30, trust_env=False) as client:
        for name in case_names:
            case = CASES[name]
            entry = {"case": name, "request": {"message": case.message, "client_message_id": uuid4().hex},
                     "expected_status": case.expected_status,
                     "expected_filenames": list(case.filenames),
                     "unavailable_filenames": list(case.unavailable_filenames),
                     "passed": False, "errors": []}
            report["cases"].append(entry)
            save_report(output, report)
            try:
                if name == "followup":
                    require(iris_session is not None, "iris_session_unavailable")
                    session_id = iris_session
                    current = api_data(client.get(f"/sessions/{quote(session_id, safe='')}"))
                    require(current.get("status") == "completed", "prior_iris_turn_not_completed")
                else:
                    if name == "iris":
                        entry["source"] = verify_public_iris_source(client)
                        entry["request"]["dataset_ids"] = [IRIS_ID]
                    else:
                        uploaded = api_data(client.post("/files", files={
                            "file": ("task_matrix_input.csv", SYNTHETIC_SOURCE, "text/csv")}))
                        entry["source"] = {"filename": "task_matrix_input.csv", "file_id": uploaded["file_id"],
                                           "sha256": hashlib.sha256(SYNTHETIC_SOURCE).hexdigest()}
                        entry["request"]["attachments"] = [{"file_id": uploaded["file_id"], "filename": "task_matrix_input.csv"}]
                    created = api_data(client.put("/sessions", json={}))
                    session_id = created["session_id"]
                    entry["session_id"] = session_id
                    save_report(output, report)  # Persist identity before any chargeable request.
                    api_data(client.patch(f"/sessions/{quote(session_id, safe='')}/title",
                                          json={"title": f"[回归验证] 严格分析验收 {name}"}))
                    if name == "iris":
                        iris_session = session_id
                entry["session_id"] = session_id
                entry["submitted_once"] = True
                save_report(output, report)
                emit({"case": name, "session_id": session_id, "state": "submitting_once"})
                entry["stream"] = send_once(client, session_id, entry["request"], max_seconds)
                entry.update(verify_case(client, case, session_id, entry["request"]["client_message_id"]))
                if entry["stream"]["terminal_event"] != "done":
                    entry["passed"] = False
                    entry["errors"].append("stream_terminal_not_done")
            except Exception as exc:
                code = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
                entry["errors"].append(safe_code(code))
                if entry.get("session_id"):
                    try:
                        entry.update(verify_case(client, case, entry["session_id"], entry["request"]["client_message_id"]))
                        entry["errors"].append(safe_code(code))
                        entry["passed"] = False
                    except Exception:
                        entry["errors"].append("diagnostic_state_unavailable")
                    if entry.get("submitted_once") and entry.get("session_status") not in {"completed", "waiting"}:
                        try:
                            client.post(f"/sessions/{quote(entry['session_id'], safe='')}/stop", timeout=15).raise_for_status()
                            entry["stopped_own_diagnostic_after_observation_failure"] = True
                        except Exception:
                            entry["errors"].append("diagnostic_stop_unconfirmed")
            finally:
                save_report(output, report)
                emit({"case": name, "session_id": entry.get("session_id"),
                      "passed": entry["passed"], "errors": entry["errors"]})
            if entry.get("session_id") and entry.get("session_status") not in {"completed", "waiting"}:
                # A timeout/failed stop must never overlap the next real task.
                try:
                    current = api_data(client.get(f"/sessions/{quote(entry['session_id'], safe='')}"))
                    if current.get("status") in {"pending", "running"}:
                        raise CheckFailure("matrix_halted_unconfirmed_task_state")
                except (CheckFailure, httpx.HTTPError, AssertionError, KeyError, ValueError):
                    entry["errors"].append("matrix_halted_unconfirmed_task_state")
                    entry["passed"] = False
                    save_report(output, report)
                    break
        report["passed"] = bool(report["cases"]) and all(entry["passed"] for entry in report["cases"])
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_report(output, report)
    return report


def inspect_report(path):
    """Only inspect previously recorded turns; never create or rerun sessions."""
    original = json.loads(Path(path).read_text(encoding="utf-8"))
    with httpx.Client(base_url=validate_base_url(original["base_url"]), timeout=30, trust_env=False) as client:
        checked = []
        for entry in original["cases"]:
            session_id = entry.get("session_id")
            if not session_id:
                continue
            checked.append(verify_case(client, CASES[entry["case"]], session_id,
                                       entry["request"]["client_message_id"]))
    return {"run_id": original["run_id"], "read_only_inspection": True,
            "cases": checked, "passed": bool(checked) and all(entry["passed"] for entry in checked)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--inspect-report", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:7001")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", default="synthetic,iris,followup")
    parser.add_argument("--max-seconds", type=int, default=600)
    options = parser.parse_args()
    if options.inspect_report:
        result = inspect_report(options.inspect_report)
    else:
        if not options.output:
            parser.error("--live requires a new --output report path")
        if not 30 <= options.max_seconds <= 900:
            parser.error("--max-seconds must be between 30 and 900")
        result = run_matrix(options.base_url, options.output, tuple(options.cases.split(",")), options.max_seconds)
    emit(result)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
