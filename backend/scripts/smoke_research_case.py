"""Opt-in acceptance of the user's exact public Iris research CASE, step 1.

Creates one NEW session, submits once, and retains failures. No old-task replay,
extra prompt coaching, other datasets or automatic resubmission. The report's
automated checks are not a substitute for human comparison of the answer and
executed methods with the independent source oracle. No raw model/tool text is
written to the report. The observation bound is test-only, not a task quota.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

try:
    from . import smoke_analysis_task_matrix as matrix
except ImportError:
    import smoke_analysis_task_matrix as matrix


MESSAGE = (
    "读取 iris.csv 与说明文件，核对原始 iris.data 和 CSV 是否为同一批观测，不重复合并。"
    "列出四个形态变量、单位、品种标签、每组样本数、缺失及重复记录。"
    "重复观测先标记，不自动删除。把花瓣与萼片的区分能力作为本次研究问题。"
)
SOURCE_DIR = matrix.IRIS_SOURCE.parent


def source_oracle(directory=SOURCE_DIR):
    """Compare all original records using stdlib Decimal, not generated code."""
    columns, mapped = matrix.read_csv((directory / "iris.csv").read_bytes())
    matrix.require(len(columns) == 5, "oracle_unexpected_schema")
    rows = [tuple(row[column] for column in columns) for row in mapped]
    with (directory / "iris.data").open(newline="", encoding="utf-8") as stream:
        raw = [tuple(row) for row in csv.reader(stream) if row]
    matrix.require(all(len(row) == 5 for row in raw), "oracle_raw_row_width")

    def canonical(row):
        return tuple(Decimal(value) for value in row[:4]) + (row[4].strip(),)

    values, originals = list(map(canonical, rows)), list(map(canonical, raw))
    counts = Counter(values)
    duplicates = [{"one_based_rows": [i + 1 for i, item in enumerate(values) if item == row],
                   "values": [str(value) for value in row]}
                  for row, count in counts.items() if count > 1]
    return {"oracle": "stdlib_csv_decimal", "columns": columns,
        "rows_csv": len(rows), "rows_raw": len(raw),
        "same_order": values == originals, "same_multiset": Counter(values) == Counter(originals),
        "species_counts": dict(Counter(row[-1] for row in values)),
        "blank_cells": sum(not cell.strip() for row in rows for cell in row),
        "duplicate_extra_rows": sum(count - 1 for count in counts.values()),
        "duplicate_member_rows": sum(count for count in counts.values() if count > 1),
        "duplicate_groups": duplicates,
        "source_hashes": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                          for name in ("iris.csv", "iris.data", "iris.names")}}


def inspect(client, session_id, client_message_id):
    session = matrix.api_data(client.get(f"/sessions/{quote(session_id, safe='')}"))
    turn = matrix.requested_turn(session, client_message_id)
    delivery = matrix.extract_delivery(turn)
    answer = matrix.latest_answer(turn)
    errors = []
    if session.get("status") != "completed":
        errors.append("session_not_completed")
    if not matrix.delivery_outcomes_complete(delivery["outcomes"]):
        errors.append("authoritative_outcome_not_succeeded")
    if len(answer.strip()) < 150:
        errors.append("answer_not_substantive")
    if any(item["filename"].lower().endswith((".png", ".jpg", ".svg", ".pdf", ".ipynb"))
           for item in delivery["attachments"].values()):
        errors.append("unrequested_chart_or_report_delivered")
    return {"session_status": session.get("status"), "errors": errors,
        "automated_checks_passed": not errors,
        "outcomes": matrix.safe_outcomes(delivery["outcomes"]),
        "answer": {"characters": len(answer), "sha256": hashlib.sha256(answer.encode()).hexdigest()},
        "attachments": [{"file_id": item["file_id"], "filename": matrix.safe_name(item["filename"])}
                        for item in delivery["attachments"].values()],
        "scientific_scope_and_facts_review": "requires_independent_review"}


def run(base_url, output, max_seconds=600):
    api_base = matrix.validate_base_url(base_url)
    output = Path(output)
    matrix.require(output.is_absolute() and not output.exists() and not output.is_symlink(),
                   "use_new_absolute_report_path")
    matrix.require(type(max_seconds) is int and 30 <= max_seconds <= 900, "invalid_observation_bound")
    report = {"scope": "new_public_iris_case1_only", "automatic_retries": 0,
              "message": MESSAGE, "oracle": source_oracle(), "errors": [],
              "automated_checks_passed": False}
    matrix.save_report(output, report)
    session_id = None
    with httpx.Client(base_url=api_base, timeout=30, trust_env=False) as client:
        try:
            session_id = matrix.api_data(client.put("/sessions", json={}))['session_id']
            matrix.require(isinstance(session_id, str) and bool(session_id), "new_session_identity_missing")
            request_id = uuid4().hex
            report.update(session_id=session_id, client_message_id=request_id)
            matrix.save_report(output, report)
            matrix.api_data(client.patch(f"/sessions/{quote(session_id, safe='')}/title",
                json={"title": "[回归验证] 公开 Iris 科研 CASE 第一步"}))
            matrix.emit({"session_id": session_id, "state": "submitting_exact_case_once"})
            report["stream"] = matrix.send_once(client, session_id, {
                "message": MESSAGE, "dataset_ids": [matrix.IRIS_ID], "client_message_id": request_id,
            }, max_seconds)
            report.update(inspect(client, session_id, request_id))
            if report["stream"]["terminal_event"] != "done":
                report["errors"].append("stream_terminal_not_done")
            matrix.require(source_oracle()["source_hashes"] == report["oracle"]["source_hashes"],
                           "local_source_bytes_changed")
        except (Exception, KeyboardInterrupt) as exc:
            report["errors"].append(str(exc) if isinstance(exc, matrix.CheckFailure)
                                    else "acceptance_transport_or_execution_failed")
            if session_id:
                try:
                    state = matrix.api_data(client.get(f"/sessions/{quote(session_id, safe='')}"))
                    if state.get("status") != "completed":
                        matrix.api_data(client.post(f"/sessions/{quote(session_id, safe='')}/stop", timeout=15))
                        report["stopped_own_test_session"] = True
                except Exception:
                    report["errors"].append("test_session_state_unconfirmed")
        finally:
            report["automated_checks_passed"] = not report["errors"]
            matrix.save_report(output, report)
            matrix.emit({"session_id": session_id, "automated_checks_passed": report["automated_checks_passed"],
                         "errors": report["errors"], "report_path": str(output)})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:7001")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=int, default=600)
    args = parser.parse_args()
    result = run(args.base_url, args.output, args.max_seconds)
    return 0 if result["automated_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
