"""Opt-in local chat smoke test using only the bundled synthetic CSV.

Creates a retained test session and briefly installs the sample skill if needed.
Restores that installation afterward. Never changes model credentials or preferences.
"""
import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

PACKAGE_ID = "dataseek/tabular-quality"


def assess(events, runs, report, release, sample):
    """A done frame alone is never evidence of a successful analysis."""
    rejected = any(
        event.get("event") == "message" and
        event.get("data", {}).get("metadata", {}).get("front_controller_error")
        for event in events if isinstance(event.get("data", {}).get("metadata", {}), dict)
    )
    tools = [event["data"] for event in events if event.get("event") == "tool"
             and event.get("data", {}).get("status") == "called"]
    expected_release = {"package_id": PACKAGE_ID, "version": release["version"], "sha256": release["package_sha256"]}
    checks = {
        "stream_done": any(event.get("event") == "done" for event in events),
        "admitted": not rejected,
        "run_succeeded": len(runs) == 1 and runs[0].get("status") == "succeeded" and runs[0].get("history_complete") is True,
        "release_bound": len(runs) == 1 and runs[0].get("skills") == [expected_release],
        "script_read_observed": any(tool.get("function") == "skill_read_script"
                                    and tool.get("args", {}).get("script_filename") == "profile_csv.py" for tool in tools),
        "shell_call_observed": any(tool.get("function") in {"shell_run", "shell_exec"} for tool in tools),
        "report_matches": isinstance(report, dict) and all(report.get(key) == value for key, value in {
            "schema_version": 1, "input_sha256": hashlib.sha256(sample).hexdigest(),
            "row_count": 4, "column_count": 3, "duplicate_rows": 1,
            "columns": [{"name": "id", "missing_count": 0}, {"name": "value", "missing_count": 2},
                        {"name": "label", "missing_count": 1}],
            "method": {"missing": "empty_or_whitespace", "duplicates": "exact_parsed_row_after_first",
                       "dialect": "comma", "encoding": "utf-8-sig"},
        }.items()),
    }
    return {"status": "blocked" if rejected else "passed" if all(checks.values()) else "incomplete", "checks": checks}


def run(output: Path):
    sample = (Path(__file__).resolve().parents[2] / "data-skills/packages/tabular-quality/references/sample.csv").read_bytes()
    receipt = {"scope": "synthetic_csv_chat_sample", "started_at": datetime.now(timezone.utc).isoformat(),
               "status": "incomplete", "installation_restored": False}
    # Refuse to destroy an earlier verification record.
    with output.open("x") as stream:
        json.dump(receipt, stream)
    session = None
    terminal = False
    restore = False
    skill = None
    events = []
    with httpx.Client(base_url="http://localhost:7000/api/v1", trust_env=False, timeout=20) as client:
        def api(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()["data"]

        try:
            skill = next(item for item in api("GET", "/skills/catalog")["skills"]
                         if item.get("package_id") == PACKAGE_ID and item.get("version") == "1.0.0")
            receipt["release"] = {key: skill[key] for key in ["name", "version", "package_sha256"]}
            restore = not skill["installed"]
            if restore:
                api("POST", f"/skills/{skill['id']}/install")
            uploaded = api("POST", "/files", files={"file": ("quality_sample.csv", sample, "text/csv")})
            receipt["input_file_id"] = uploaded["file_id"]
            session = api("PUT", "/sessions", json={})["session_id"]
            receipt["session_id"] = session
            api("PATCH", f"/sessions/{session}/title", json={"title": "技能闭环样例：CSV基础质检"})
            payload = {
                "message": "请使用所选表格基础质检技能分析附件。读取实际技能脚本并在沙箱执行，将报告保存为 /home/ubuntu/output/quality_report.json 并作为文件交付。只做规定检查，不下载数据、不扩展分析，不凭心算代替脚本。",
                "skills": [skill["name"]], "attachments": [uploaded], "client_message_id": "sample-" + session,
            }
            deadline = time.monotonic() + 240
            with client.stream("POST", f"/sessions/{session}/chat", json=payload) as response:
                response.raise_for_status()
                kind = ""
                for line in response.iter_lines():
                    if time.monotonic() > deadline:
                        receipt["limit_reached"] = True
                        break
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:"):
                        events.append({"event": kind, "data": json.loads(line[5:])})
                        if kind == "done":
                            terminal = True
                            break
            runs = api("GET", f"/sessions/{session}/runs")["runs"]
            files = api("GET", f"/sessions/{session}/files")
            reports = [item for item in files if item.get("filename") == "quality_report.json"]
            report = None
            if len(reports) == 1 and 0 < reports[0].get("size", 0) < 65536:
                with client.stream("GET", f"/files/{reports[0]['file_id']}/download") as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes(4096):
                        content.extend(chunk)
                        if len(content) > 65536:
                            raise ValueError("Report exceeds sample limit")
                    report = json.loads(content)
                receipt["report_file_id"] = reports[0]["file_id"]
                receipt["report_sha256"] = hashlib.sha256(content).hexdigest()
            receipt.update(assess(events, runs, report, skill, sample))
            receipt["event_counts"] = dict(Counter(event["event"] for event in events))
            if receipt["status"] == "blocked":
                receipt["blocking_stage"] = "front_controller"
            if any(item.get("status") in {"running", "waiting"} for item in runs):
                api("POST", f"/sessions/{session}/stop")
                receipt["unfinished_run_stop_requested"] = True
        except Exception as error:
            # Do not persist provider response bodies, configuration, or model content.
            receipt["status"] = "blocked"
            receipt["error_type"] = type(error).__name__
        finally:
            if session and not terminal:
                try:
                    api("POST", f"/sessions/{session}/stop")
                except Exception:
                    receipt["stop_failed"] = True
            try:
                if restore and skill:
                    api("DELETE", f"/skills/{skill['id']}/install")
                receipt["installation_restored"] = True
            except Exception:
                receipt["installation_restored"] = False
                receipt["status"] = "blocked"
            output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Create a local test session and call the configured model")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required; this check creates a test session")
    receipt = run(args.output)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
