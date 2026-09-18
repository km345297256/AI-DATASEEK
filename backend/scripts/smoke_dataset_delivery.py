"""Opt-in real-model delivery check via the same local API as the browser.

Creates a clearly labelled NEW diagnostic session; never resumes/deletes an
existing task. Uses the configured model and read-only dataset mount, therefore
requires explicit operator intent. Prints bounded status, not source data or
model prompts. Leaves the diagnostic session/results available for inspection.

--inspect-session only reads an existing session and downloads its delivered
PNGs for verification. It never creates, resumes, stops, or reruns a task.
"""
import argparse
import hashlib
from io import BytesIO
import json
from uuid import uuid4

import httpx
from PIL import Image


def report(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def api_data(response):
    response.raise_for_status()
    value = response.json()
    assert value["code"] == 0, "API rejected request"
    return value["data"]


def extract_delivery(session):
    """Read the public browser contract, never infer success from prose/steps.

    Step events are flat progress updates. The authoritative outcome belongs to
    assistant message.metadata.analysis_outcome, with metadata.step_id identifying
    revised results for the same step. Only the latest user turn is inspected.
    """
    outcomes, delivered, step_statuses = {}, {}, {}
    for event in session["events"]:
        data = event["data"]
        kind = event["event"]
        if kind == "message" and data.get("role") == "user":
            outcomes, delivered, step_statuses = {}, {}, {}
            continue
        if kind == "step":
            if isinstance(data.get("id"), str):
                step_statuses[data["id"]] = data.get("status")
            continue
        if kind != "message" or data.get("role") != "assistant":
            continue
        metadata = data.get("metadata") or {}
        if "analysis_outcome" in metadata:
            outcome = metadata["analysis_outcome"]
            assert isinstance(outcome, dict), "Malformed analysis outcome"
            assert outcome.get("status") in {"succeeded", "partial", "failed"}, "Missing outcome status"
            assert isinstance(outcome.get("reason_code"), str), "Missing outcome reason"
            assert type(outcome.get("can_resume")) is bool, "Missing outcome continuation state"
            assert isinstance(outcome.get("missing"), list), "Missing outcome requirements"
            issues = outcome.get("issues", [])
            assert isinstance(issues, list), "Malformed artifact issues"
            assert all(isinstance(issue, dict) and type(issue.get("blocking")) is bool
                       for issue in issues), "Malformed artifact issue blocking state"
            step_id = metadata.get("step_id")
            key = step_id if isinstance(step_id, str) and step_id else "summary"
            outcomes[key] = outcome
        for attachment in data.get("attachments") or []:
            if (isinstance(attachment, dict) and isinstance(attachment.get("file_id"), str)
                    and isinstance(attachment.get("filename"), str)):
                delivered[attachment["file_id"]] = attachment
    return {"outcomes": outcomes, "attachments": delivered, "step_statuses": step_statuses}


def delivery_outcomes_complete(outcomes):
    return bool(outcomes) and all(
        value["status"] == "succeeded" and not value["missing"]
        and not any(issue["blocking"] for issue in value.get("issues", []))
        for value in outcomes.values()
    )


def verify_existing(client, session_id):
    """Read-only verification; this path intentionally performs GETs only."""
    from urllib.parse import quote

    session = api_data(client.get(f"/sessions/{quote(session_id, safe='')}"))
    extracted = extract_delivery(session)
    images = []
    for file_id, item in extracted["attachments"].items():
        if not item["filename"].lower().endswith(".png"):
            continue
        response = client.get(f"/files/{quote(file_id, safe='')}/download")
        response.raise_for_status()
        with Image.open(BytesIO(response.content)) as image:
            assert image.format == "PNG", "A delivered PNG has another image format"
            image.verify()
        with Image.open(BytesIO(response.content)) as image:
            image.load()
            dimensions = image.size
            assert min(dimensions) > 1
        images.append({"filename": item["filename"], "file_id": file_id,
                       "bytes": len(response.content), "dimensions": dimensions,
                       "sha256": hashlib.sha256(response.content).hexdigest()})
    passed = bool(session.get("status") == "completed" and images
                  and delivery_outcomes_complete(extracted["outcomes"]))
    result = {"passed": passed, "session_id": session_id, "session_status": session.get("status"),
              "outcomes": extracted["outcomes"], "step_statuses": extracted["step_statuses"],
              "downloaded_verified_images": images}
    report(result)
    if not passed:
        raise SystemExit(1)
    return result


def run(base_url, dataset_id, message):
    with httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=600, trust_env=False) as client:
        session_id = api_data(client.put("/sessions", json={}))['session_id']
        api_data(client.patch(f"/sessions/{session_id}/title", json={"title": "[回归验证] 数据分析成果交付"}))
        report({"created_diagnostic_session": session_id})
        done, event_type, data_lines, states = False, "", [], {}
        try:
            with client.stream("POST", f"/sessions/{session_id}/chat", json={
                "message": message, "dataset_ids": [dataset_id], "client_message_id": uuid4().hex,
            }) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line and data_lines:
                        data = json.loads("\n".join(data_lines))
                        data_lines = []
                        if event_type == "tool" and data.get("status") == "called":
                            key = data.get("tool_call_id")
                            state = data.get("execution_status")
                            if state and states.get(key) != state:
                                states[key] = state
                                report({"tool": data.get("function"), "execution_status": state})
                        if event_type == "step":
                            report({"step_id": data.get("id"), "step_status": data.get("status")})
                        if event_type == "message" and data.get("role") == "assistant":
                            outcome = (data.get("metadata") or {}).get("analysis_outcome")
                            if isinstance(outcome, dict):
                                report({"outcome_status": outcome.get("status"),
                                        "outcome_reason": outcome.get("reason_code")})
                        if event_type == "done":
                            done = True
                            break
            assert done, "Stream ended without task completion"
        except BaseException:
            # Stop only our newly-created diagnostic session if observation
            # failed; never leave an unattended chargeable test running.
            client.post(f"/sessions/{session_id}/stop", timeout=15)
            raise
        verify_existing(client, session_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--inspect-session", metavar="ID", help="Read and verify an existing session without running it")
    parser.add_argument("--base-url", default="http://127.0.0.1:7001")
    parser.add_argument("--dataset-id")
    parser.add_argument("--message")
    options = parser.parse_args()
    if options.inspect_session:
        if options.dataset_id or options.message:
            parser.error("--inspect-session does not accept a new dataset or message")
        with httpx.Client(base_url=options.base_url.rstrip("/") + "/api/v1", timeout=600, trust_env=False) as client:
            verify_existing(client, options.inspect_session)
    else:
        if not options.dataset_id or not options.message:
            parser.error("--live requires --dataset-id and --message")
        run(options.base_url, options.dataset_id, options.message)
