"""Opt-in upload-to-delivery regression against the running local application.

Creates only synthetic data and one clearly labelled new diagnostic session.
Never resumes, edits, or deletes an existing task. Uses the configured model.
"""
import argparse
import hashlib
import json
import time
from uuid import uuid4

import httpx

from smoke_dataset_delivery import api_data, delivery_outcomes_complete, extract_delivery, report, verify_existing


def send(client, session_id, **payload):
    done, kind, lines = False, "", []
    try:
        with client.stream("POST", f"/sessions/{session_id}/chat", json={
            **payload, "client_message_id": uuid4().hex,
        }) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    lines.append(line[5:].strip())
                elif not line and lines:
                    data = json.loads("\n".join(lines))
                    lines = []
                    if kind == "error":
                        raise AssertionError(f"Analysis stream error: {data.get('code')}")
                    if kind == "step":
                        report({"step_status": data.get("status")})
                    if kind == "message" and data.get("role") == "assistant":
                        outcome = (data.get("metadata") or {}).get("analysis_outcome")
                        if isinstance(outcome, dict):
                            report({"outcome_status": outcome.get("status"),
                                    "outcome_reason": outcome.get("reason_code")})
                    if kind == "done":
                        done = True
                        break
        assert done, "Stream ended without terminal confirmation"
        # SSE terminal delivery can precede the task worker's status persistence.
        for _ in range(20):
            session = api_data(client.get(f"/sessions/{session_id}"))
            if session.get("status") not in {"running", "pending"}:
                break
            time.sleep(0.25)
        assert session.get("status") == "completed", "Session did not finish"
        outcomes = extract_delivery(session)["outcomes"]
        assert not outcomes or delivery_outcomes_complete(outcomes), "Turn ended with incomplete results"
    except BaseException:
        if not done:
            client.post(f"/sessions/{session_id}/stop", timeout=15)
        raise


def assert_inputs(client, session_id, file_id, selected):
    state = api_data(client.get(f"/sessions/{session_id}/analysis-inputs"))
    assert [item["file_id"] for item in state["files"]] == [file_id], "Outputs leaked into inputs"
    assert state["selected_file_ids"] == selected, "Wrong active input scope"
    for item in state["files"]:
        assert not {"analysis_input_namespace", "analysis_input_filename"}.intersection(item.get("metadata") or {})
    report({"available_inputs": len(state["files"]), "selected_inputs": len(selected)})


def assert_no_new_outputs(client, session_id):
    session = api_data(client.get(f"/sessions/{session_id}"))
    assert not extract_delivery(session)["attachments"], "A read-only follow-up unexpectedly delivered files"


def run(base_url):
    with httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=600, trust_env=False) as client:
        uploaded = api_data(client.post("/files", files={
            "file": ("unified_analysis_smoke.csv", b"label,value\nA,2\nB,4\nC,6\n", "text/csv"),
        }))
        session_id = api_data(client.put("/sessions", json={}))['session_id']
        api_data(client.patch(f"/sessions/{session_id}/title", json={"title": "[回归验证] 统一上传分析"}))
        report({"created_diagnostic_session": session_id, "synthetic_input_id": uploaded["file_id"]})
        send(client, session_id, attachments=[uploaded], message=(
            "分析上传的 unified_analysis_smoke.csv：计算 value 列均值，并生成一张 label 对 value 的柱状图，"
            "保存为 unified_upload_bar.png 供下载。只需这一个图表；图内标签使用英文。"))
        delivery = verify_existing(client, session_id)
        assert_inputs(client, session_id, uploaded["file_id"], [uploaded["file_id"]])

        send(client, session_id, message="继续使用本会话的输入文件，列出文件名和列名即可；无需生成文件。")
        assert_no_new_outputs(client, session_id)
        assert_inputs(client, session_id, uploaded["file_id"], [uploaded["file_id"]])
        report({"inherited_without_reupload": True})

        send(client, session_id, input_file_ids=[], message="本次不使用任何上传资料，不读取文件，只回复：已清空本次资料选择。")
        assert_no_new_outputs(client, session_id)
        assert_inputs(client, session_id, uploaded["file_id"], [])
        report({"explicit_clear_preserves_available_file": True})

        send(client, session_id, input_file_ids=[uploaded["file_id"]],
             message="本次重新选择上传资料，列出输入文件名即可；无需生成文件。")
        assert_no_new_outputs(client, session_id)
        assert_inputs(client, session_id, uploaded["file_id"], [uploaded["file_id"]])
        for item in delivery["downloaded_verified_images"]:
            response = client.get(f"/files/{item['file_id']}/download")
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == item["sha256"], "Earlier delivered bytes changed"
        report({"prior_delivery_retained": True, "read_only_followups_did_not_deliver_files": True})
        report({"reselected_existing_input": True, "passed": True, "session_id": session_id})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:7001")
    run(parser.parse_args().base_url)
