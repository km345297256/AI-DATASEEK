"""Opt-in live, tool-free reviewer smoke check using entirely synthetic evidence.

Run with --live in the configured backend environment. Never reads user files,
creates sessions, runs an analysis tool, or prints credentials/provider errors.
"""
import argparse
import asyncio
import json

from app.core.config import get_settings
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.analysis_answer_review import AnswerEvidence
from app.infrastructure.external.llm import create_chat_model


async def main(debug=False):
    evidence = AnswerEvidence()
    evidence.observe(ToolEvent(tool_call_id="smoke-compute", tool_name="shell", function_name="shell_run",
        function_args={"command": "python -c synthetic_fixture"}, status=ToolStatus.CALLED,
        function_result={"success": True, "data": {"exit_code": 0, "stdout":
            "Synthetic fixture: 12 records; mean(value)=4.5. A bar chart was produced at "
            "/home/ubuntu/output/measured.png. No dimensionality reduction was performed."}}))
    agent = object.__new__(ExecutionAgent)
    model = create_chat_model(get_settings())
    captured = []
    class CaptureModel:
        def bind(self, **kwargs):
            class Bound:
                async def ainvoke(self, messages):
                    try:
                        response = await model.bind(**kwargs).ainvoke(messages)
                    except BaseException as error:
                        captured.append({"error_type": type(error).__name__})
                        raise
                    captured.append(response.content)
                    return response
            return Bound()
    agent._model = CaptureModel()
    result = await agent.review_delivery_answer(question="把这组模拟测量可视化并解释结果。",
        draft="已生成 measured.png 与 invented-projection.png。降维结果显示三个明显聚类。均值为4.5。",
        files=[FileInfo(file_id="synthetic-verified", filename="measured.png",
                        file_path="/home/ubuntu/output/measured.png", size=120,
                        metadata={"artifact_sha256": "a" * 64})],
        evidence=evidence, requirements=[], language="zh")
    # The provider may paraphrase, but cannot publish a nonexistent attachment.
    success = result.status == "corrected" and "invented-projection.png" not in result.text
    print(json.dumps({"passed": success, "status": result.status, "metadata": result.metadata,
                      "text": result.text}, ensure_ascii=False))
    if debug:
        print(json.dumps({"synthetic_review_response": captured}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.debug)))
