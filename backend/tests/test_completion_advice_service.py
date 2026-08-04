import pytest
from datetime import datetime, UTC

from app.domain.models.event import DoneEvent, MessageEvent, StepEvent, StepStatus
from app.domain.models.file import FileInfo
from app.domain.models.plan import Plan, Step
from app.domain.services import completion_advice_service as advice_module
from app.domain.services.completion_advice_service import CompletionAdviceService


@pytest.mark.asyncio
async def test_completion_advice_marks_skill_candidate_for_workflow_like_tasks(monkeypatch):
    class _FakeMessage:
        content = """{
          "recommendations": [
            "是否要补充参数模板或默认值？",
            "要不要增加异常处理和批量场景？",
            "是否要导出为可复用脚本？"
          ],
          "is_skill_candidate": true,
          "skill_reason": "该任务具备明确输入、分步执行和可复用产出，适合沉淀为技能。"
        }"""

    class _FakeModel:
        async def ainvoke(self, _messages):
            return _FakeMessage()

    monkeypatch.setattr(advice_module, "create_chat_model", lambda *args, **kwargs: _FakeModel())

    service = CompletionAdviceService()
    events = [
        MessageEvent(role="user", message="请帮我批量生成报告"),
        StepEvent(status=StepStatus.COMPLETED, step=Step(description="分析输入数据", success=True)),
        StepEvent(status=StepStatus.COMPLETED, step=Step(description="输出报告文件", success=True, attachments=["/home/ubuntu/report.md"])),
        DoneEvent(),
    ]

    advice = await service.analyze(events)

    assert advice.is_skill_candidate is True
    assert advice.recommendations[0] == "使用/skill-create将这个流程替保存为可复用的技能"


def test_completion_advice_prompt_requires_direct_user_utterances():
    service = CompletionAdviceService()
    prompt = service._build_prompt([], service.default_advice())

    assert "directly sendable as the user's next chat message" in prompt
    assert "Write in the user's voice" in prompt
    assert "Avoid leading patterns like" in prompt
    assert "帮我再算一下 81 的平方根" in prompt


def test_completion_advice_default_recommendations_are_not_guiding_options():
    service = CompletionAdviceService()
    advice = service.default_advice()

    assert len(advice.recommendations) == 3
    assert all(not item.startswith(("是否", "要不要")) for item in advice.recommendations)
    assert all(not item.endswith(("吗？", "吗?")) for item in advice.recommendations)


def test_completion_advice_coerce_normalizes_guiding_options():
    service = CompletionAdviceService()

    advice = service._coerce_advice(
        {
            "recommendations": [
                "是否需要我把计算过程写详细一点？",
                "要不要我生成批量计算脚本？",
                "是否要导出为 CSV 或 JSON 格式？",
            ],
            "is_skill_candidate": False,
            "skill_reason": "",
        }
    )

    assert advice.recommendations == [
        "把计算过程写详细一点",
        "生成批量计算脚本",
        "导出为 CSV 或 JSON 格式",
    ]


@pytest.mark.asyncio
async def test_completion_advice_defaults_for_trivial_tasks():
    service = CompletionAdviceService()
    events = [MessageEvent(role="user", message="今天天气怎么样？"), DoneEvent()]

    advice = await service.analyze(events)

    assert len(advice.recommendations) == 3
    assert advice.is_skill_candidate is False


def test_completion_advice_serializes_datetime_in_attachments():
    service = CompletionAdviceService()
    events = [
        MessageEvent(
            role="assistant",
            message="已生成图像。",
            attachments=[
                FileInfo(
                    file_id="file-1",
                    filename="chart.png",
                    file_path="/home/ubuntu/chart.png",
                    size=123,
                    content_type="image/png",
                    upload_date=datetime(2026, 6, 24, tzinfo=UTC),
                )
            ],
        )
    ]

    serialized = service._serialize_events(events)

    assert "chart.png" in serialized
    assert "upload_date" in serialized
    assert "2026-06-24T00:00:00Z" in serialized


@pytest.mark.asyncio
async def test_agent_domain_service_resolves_uploaded_file_metadata():
    from app.domain.services.agent_domain_service import AgentDomainService

    class _FakeFileStorage:
        async def get_file_info(self, file_id, user_id=None):
            return FileInfo(
                file_id=file_id,
                filename="SVS工具表.xlsx",
                size=2048,
                upload_date=datetime(2026, 6, 24, tzinfo=UTC),
                user_id=user_id,
            )

    service = AgentDomainService.__new__(AgentDomainService)
    service._file_storage = _FakeFileStorage()

    attachments = await service._resolve_message_attachments(
        [{"file_id": "6a3c842f0c4073a13afdf6e2", "filename": "SVS工具表.xlsx"}],
        "user-1",
    )

    assert attachments is not None
    assert attachments[0].size == 2048
    assert attachments[0].upload_date == datetime(2026, 6, 24, tzinfo=UTC)
