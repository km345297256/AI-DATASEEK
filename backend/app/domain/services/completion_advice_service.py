import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Optional

from app.core.config import get_settings
from app.domain.models.event import MessageEvent, PlanEvent, StepEvent, ToolEvent
from langchain.messages import HumanMessage
from app.infrastructure.external.llm import create_chat_model
from app.domain.utils.robust_json_parser import parse_json_lenient

logger = logging.getLogger(__name__)


@dataclass
class CompletionAdvice:
    recommendations: list[str]
    is_skill_candidate: bool
    skill_reason: str


class CompletionAdviceService:
    def __init__(self) -> None:
        self._settings = get_settings()

    def _default_advice(self) -> CompletionAdvice:
        return CompletionAdvice(
            recommendations=[
                "把刚才的结果解释得更详细一点",
                "给我一个类似问题的完整示例",
                "把这个结论整理成可复制的格式",
            ],
            is_skill_candidate=False,
            skill_reason="",
        )

    def default_advice(self) -> CompletionAdvice:
        return self._default_advice()

    def to_payload(self, advice: CompletionAdvice) -> dict[str, Any]:
        return asdict(advice)

    def _serialize_events(self, events: list[Any]) -> str:
        messages: list[dict[str, Any]] = []
        for event in events:
            if isinstance(event, MessageEvent):
                messages.append(
                    {
                        "type": "message",
                        "role": event.role,
                        "content": event.message,
                        "metadata": event.metadata or {},
                        "attachments": [attachment.model_dump(mode="json") for attachment in event.attachments or []],
                    }
                )
            elif isinstance(event, PlanEvent):
                messages.append(
                    {
                        "type": "plan",
                        "title": event.plan.title,
                        "goal": event.plan.goal,
                        "language": event.plan.language,
                        "steps": [step.model_dump(mode="json") for step in event.plan.steps],
                    }
                )
            elif isinstance(event, StepEvent):
                messages.append(
                    {
                        "type": "step",
                        "status": event.status.value,
                        "id": event.step.id,
                        "description": event.step.description,
                        "result": event.step.result,
                        "attachments": event.step.attachments,
                    }
                )
            elif isinstance(event, ToolEvent):
                messages.append(
                    {
                        "type": "tool",
                        "tool_name": event.tool_name,
                        "function_name": event.function_name,
                        "function_args": event.function_args,
                        "status": event.status.value,
                    }
                )
        return json.dumps(messages, ensure_ascii=False, default=str)[:24000]

    async def analyze(self, events: list[Any]) -> CompletionAdvice:
        if not events:
            return self._default_advice()

        user_messages = [event for event in events if isinstance(event, MessageEvent) and event.role == "user"]
        assistant_messages = [event for event in events if isinstance(event, MessageEvent) and event.role == "assistant"]
        steps = [event for event in events if isinstance(event, StepEvent)]
        tools = [event for event in events if isinstance(event, ToolEvent)]
        plans = [event for event in events if isinstance(event, PlanEvent)]

        if len(steps) + len(tools) < 2 or not user_messages:
            return self._default_advice()

        heuristic_skill = self._heuristic_skill_candidate(user_messages, assistant_messages, plans, steps, tools)
        prompt = self._build_prompt(events, heuristic_skill)

        try:
            model = create_chat_model(self._settings, overrides={"temperature": 0})
            message = await model.ainvoke([HumanMessage(content=prompt)])
            parsed = parse_json_lenient(getattr(message, "content", "") or "")
            advice = self._coerce_advice(parsed)
            if advice.is_skill_candidate:
                advice.recommendations = [
                    "使用/skill-create将这个流程替保存为可复用的技能",
                    *[item for item in advice.recommendations if item != "使用/skill-create将这个流程替保存为可复用的技能"],
                ][:3]
            return advice
        except Exception as exc:
            logger.warning("Completion advice generation failed: %s", exc)
            return heuristic_skill or self._default_advice()

    def _build_prompt(self, events: list[Any], heuristic_skill: CompletionAdvice) -> str:
        session_summary = self._serialize_events(events)
        return f"""
You are generating post-completion suggestions for a completed agent task.

Return strict JSON only with this schema:
{{
  "recommendations": ["string", "string", "string"],
  "is_skill_candidate": true/false,
  "skill_reason": "string"
}}

Rules:
- recommendations must be exactly 3 short Chinese follow-up user utterances.
- Each recommendation must be directly sendable as the user's next chat message.
- Write in the user's voice, not the assistant's voice.
- Do not write guiding options that make the user rephrase again.
- Avoid leading patterns like "是否", "要不要", "需要...吗", "是否需要我", "要不要我".
- Avoid product guidance, capability advertising, or asking whether the assistant should do something.
- Prefer natural imperative user phrasing, for example:
  "帮我再算一下 81 的平方根"
  "把刚才的计算过程写详细一点"
  "给我一个可以批量计算平方根的 Python 脚本"
- If is_skill_candidate is true, recommendation[0] must be exactly:
  使用/skill-create将这个流程替保存为可复用的技能
- Prefer concrete next actions, not generic compliments.
- Keep each recommendation under 24 Chinese characters when possible.

Heuristic hint:
{json.dumps(heuristic_skill.__dict__, ensure_ascii=False)}

Conversation trace:
{session_summary}
""".strip()

    def _heuristic_skill_candidate(
        self,
        user_messages: list[MessageEvent],
        assistant_messages: list[MessageEvent],
        plans: list[PlanEvent],
        steps: list[StepEvent],
        tools: list[ToolEvent],
    ) -> CompletionAdvice:
        tool_names = {tool.tool_name for tool in tools}
        artifact_steps = [step for step in steps if step.step.attachments or step.step.result]
        workflow_score = 0
        if len(user_messages) >= 1:
            workflow_score += 1
        if len(plans) > 0:
            workflow_score += 1
        if len(steps) >= 2:
            workflow_score += 1
        if len(tools) >= 2:
            workflow_score += 1
        if artifact_steps:
            workflow_score += 1
        if {"shell", "file", "browser"}.intersection(tool_names):
            workflow_score += 1

        is_skill_candidate = workflow_score >= 4
        if is_skill_candidate:
            reason = "该任务具备明确输入、分步执行和可复用产出，适合沉淀为技能。"
            recommendations = [
                "使用/skill-create将这个流程替保存为可复用的技能",
                "把这个流程整理成参数模板",
                "给这个流程补一组异常输入示例",
            ]
        else:
            reason = "该任务更偏一次性问答或低复用流程，暂不建议立即沉淀为技能。"
            recommendations = [
                "把刚才的结果解释得更详细一点",
                "给我一个类似问题的完整示例",
                "把这个结论整理成可复制的格式",
            ]
        return CompletionAdvice(
            recommendations=recommendations[:3],
            is_skill_candidate=is_skill_candidate,
            skill_reason=reason,
        )

    def _coerce_advice(self, payload: Any) -> CompletionAdvice:
        if not isinstance(payload, dict):
            return self._default_advice()
        recommendations = payload.get("recommendations")
        if not isinstance(recommendations, list):
            recommendations = []
        normalized = []
        for item in recommendations:
            if isinstance(item, str) and item.strip():
                normalized.append(self._normalize_recommendation(item.strip()))
        while len(normalized) < 3:
            normalized.append(self._default_advice().recommendations[len(normalized)])
        recommendations = normalized[:3]
        is_skill_candidate = bool(payload.get("is_skill_candidate"))
        skill_reason = str(payload.get("skill_reason") or "")
        if is_skill_candidate:
            recommendations[0] = "使用/skill-create将这个流程替保存为可复用的技能"
        return CompletionAdvice(
            recommendations=recommendations,
            is_skill_candidate=is_skill_candidate,
            skill_reason=skill_reason,
        )

    def _normalize_recommendation(self, recommendation: str) -> str:
        replacements = (
            ("是否需要我把", "把"),
            ("是否需要我", ""),
            ("是否需要", ""),
            ("是否要我把", "把"),
            ("是否要我", ""),
            ("是否要", ""),
            ("要不要我把", "把"),
            ("要不要我", ""),
            ("要不要", ""),
            ("需要我把", "把"),
            ("需要我", ""),
        )
        normalized = recommendation.strip()
        for prefix, replacement in replacements:
            if normalized.startswith(prefix):
                normalized = replacement + normalized[len(prefix) :]
                break
        if normalized.endswith("吗？") or normalized.endswith("吗?"):
            normalized = normalized[:-2]
        return normalized.strip(" ？?")


def get_completion_advice_service() -> CompletionAdviceService:
    return CompletionAdviceService()
