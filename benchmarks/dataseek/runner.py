"""Opt-in API pilot. Wall time is cancelled; token limits are observational only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote

from .http_client import DataSeekClient


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def public_task(task):
    """Explicit allowlist: never serialize the oracle or source paths to an Agent."""
    result = {key: task[key] for key in ("id", "dataset_id", "domain", "prompt", "required_artifacts")}
    if "input_files" in task:
        result["input_files"] = task["input_files"]
    return result


def build_plan(tasks, repeats, backbone_version, experiment_id, method="dataseek_default"):
    import random
    if repeats < 1 or repeats > 10:
        raise ValueError("repeats must be between 1 and 10")
    plan = []
    for task in tasks:
        for index in range(repeats):
            identity = "%s:%s:%s:%s:%s" % (experiment_id, task["id"], method, backbone_version, index)
            plan.append({"run_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                         "task_id": task["id"], "method": method,
                         "backbone_version": backbone_version, "run_index": index, "status": "missing"})
    random.Random(experiment_id).shuffle(plan)
    return plan


def read_traces(client, session_id):
    traces, cursor, cursors = {}, None, set()
    for _ in range(100):
        path = "/api/v1/sessions/%s/model-traces?limit=200" % quote(session_id, safe="")
        if cursor:
            path += "&before=" + quote(cursor, safe="")
        page = client.api("GET", path)
        for trace in page.get("traces", []):
            traces[trace["trace_id"]] = trace
        cursor = page.get("next_cursor")
        if not cursor:
            return list(traces.values())
        if cursor in cursors:
            raise RuntimeError("repeated_trace_cursor")
        cursors.add(cursor)
    raise RuntimeError("too_many_trace_pages")


def trace_usage(traces):
    requests = [t for t in traces if t.get("kind") == "model_request"]
    observed = [t for t in requests if t.get("actual_input_tokens") is not None and t.get("actual_output_tokens") is not None]
    return {
        "traced_physical_calls": len(requests),
        "traced_usage_coverage": len(observed) / len(requests) if requests else None,
        "traced_actual_input_tokens": sum(t["actual_input_tokens"] for t in observed) if observed else None,
        "traced_actual_output_tokens": sum(t["actual_output_tokens"] for t in observed) if observed else None,
        "trace_scope_includes_bootstrap": False,
        "model_names": sorted(set(t.get("model_name", "unknown") for t in requests)),
    }


def _request_stop(client, session_id):
    client.api("POST", "/api/v1/sessions/%s/stop" % quote(session_id, safe=""), {})
    for _ in range(10):
        state = client.api("GET", "/api/v1/sessions/%s" % quote(session_id, safe=""))
        if state.get("status") not in ("running", "pending"):
            return True
        time.sleep(1)
    return False


def run_dataseek(client, task, planned, run_root, wall_seconds=300):
    """Submit exactly once, preserve partial evidence and stop owned work on error."""
    if wall_seconds <= 0:
        raise ValueError("wall_seconds must be positive")
    root = Path(run_root) / planned["run_id"]
    root.mkdir(parents=True, exist_ok=False)
    artifact_root = root / "artifacts"
    artifact_root.mkdir()
    record = dict(planned, status="failed", cost_usd=None, claimed_complete=None,
                  actual_input_tokens=None, actual_output_tokens=None, model_call_count=None,
                  estimated_tokens=None, elapsed_seconds=None, stop_reason="not_submitted",
                  metadata={"gold_available_to_agent": False, "budget_mode": "wall_clock_cancel_only",
                            "strict_token_admission": False, "seed_supported": False})
    write_json(root / "run.json", record)
    started = time.monotonic()
    events, stream_errors = [], []
    seen = set()
    done = threading.Event()
    session_id = None
    terminal_seen = False
    try:
        session = client.api("PUT", "/api/v1/sessions", {})
        session_id = session["session_id"]
        record["metadata"]["session_id"] = session_id
        record["stop_reason"] = "created"
        write_json(root / "run.json", record)
        client.api("PATCH", "/api/v1/sessions/%s/title" % quote(session_id, safe=""),
                   {"title": "[预实验] %s / %s" % (task["id"], planned["run_index"])})
        body = {"message": public_task(task)["prompt"], "dataset_ids": [task["dataset_id"]],
                "client_message_id": "eval-" + planned["run_id"], "skills": [], "mcp_servers": []}
        write_json(root / "request.json", {"task": public_task(task), "request": body})

        def consume():
            try:
                for event in client.stream("/api/v1/sessions/%s/chat" % quote(session_id, safe=""), body):
                    identity = event.get("id") or event.get("data", {}).get("event_id")
                    if identity and identity in seen:
                        continue
                    if identity:
                        seen.add(identity)
                    if len(events) >= 10000:
                        raise RuntimeError("too_many_events")
                    events.append(event)
                    with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                    if event["event"] == "done":
                        break
            except Exception as exc:
                stream_errors.append(type(exc).__name__)
            finally:
                done.set()

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        remaining = max(0, wall_seconds - (time.monotonic() - started))
        finished = done.wait(remaining)
        terminal_seen = any(e["event"] == "done" for e in events)
        if not finished:
            record.update(status="timeout", stop_reason="wall_clock_limit")
        elif stream_errors or not terminal_seen:
            record.update(status="failed", stop_reason="stream_unconfirmed")
            record["metadata"]["transport_errors"] = stream_errors
        else:
            record.update(status="completed", stop_reason="stream_done")
        if not terminal_seen:
            record["metadata"]["stop_confirmed"] = _request_stop(client, session_id)
            done.wait(5)

        messages = [e["data"] for e in events if e["event"] == "message" and e["data"].get("role") == "assistant"]
        final = messages[-1] if messages else {}
        write_json(root / "final.json", final)
        outcome = (final.get("metadata") or {}).get("analysis_outcome")
        record["metadata"]["product_outcome"] = outcome
        # Natural-language completion claim is deliberately not inferred from product status.
        files = client.api("GET", "/api/v1/sessions/%s/files" % quote(session_id, safe="")) or []
        write_json(root / "files.json", files)
        final_files = final.get("attachments") or []
        candidates = [f for f in final_files if f.get("filename") == "answer.json"]
        if not candidates:
            candidates = [f for f in files if f.get("filename") == "answer.json"]
        candidates = {f["file_id"]: f for f in candidates}
        record["metadata"]["answer_candidate_count"] = len(candidates)
        if len(candidates) == 1:
            item = next(iter(candidates.values()))
            raw = client.download(item["file_id"])
            (artifact_root / "answer.json").write_bytes(raw)
            record["metadata"]["answer_sha256"] = hashlib.sha256(raw).hexdigest()
        elif len(candidates) > 1:
            record["metadata"]["artifact_issue"] = "ambiguous_answer_files"
        code_dir = artifact_root / "code"
        for item in files:
            name = item.get("filename", "")
            if name.endswith(".py") and "/" not in name and "\\" not in name:
                code_dir.mkdir(exist_ok=True)
                raw = client.download(item["file_id"])
                (code_dir / (hashlib.sha256(item["file_id"].encode()).hexdigest()[:8] + "-" + name)).write_bytes(raw)
        try:
            traces = read_traces(client, session_id)
            write_json(root / "traces.json", traces)
            record["metadata"].update(trace_usage(traces))
            names = record["metadata"]["model_names"]
            record["metadata"]["traced_backbone_matches_label"] = (
                all(name == planned["backbone_version"] for name in names) if names else None)
        except Exception as exc:
            record["metadata"]["trace_collection_error"] = type(exc).__name__
    except BaseException as exc:
        record.update(status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                      stop_reason="runner_" + type(exc).__name__)
        if session_id and not terminal_seen:
            try:
                record["metadata"]["stop_confirmed"] = _request_stop(client, session_id)
            except Exception:
                record["metadata"]["stop_confirmed"] = False
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        record["elapsed_seconds"] = time.monotonic() - started
        write_json(root / "run.json", record)
    return record
