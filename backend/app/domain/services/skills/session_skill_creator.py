import re
import shlex
from typing import List

from app.domain.models.event import MessageEvent, PlanEvent, StepEvent, ToolEvent
from app.domain.models.skill import Skill
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.skills.registry import SkillRegistry
from app.domain.models.workspace import personal_workspace_id


def _normalize_skill_name(text: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower()).strip("-")
    if not normalized:
        return "saved-task-skill"
    return normalized[:48].strip("-") or "saved-task-skill"


def _is_skill_create_request(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "/skill-create",
            "skill_create_from_session",
            "create a skill",
            "create skill",
            "save skill",
            "save this workflow",
            "save this task",
            "turn this task into a skill",
            "current session",
            "save-task-skill",
            "saved task skill",
            "保存为可复用的技能",
            "保存为skill",
            "保存成skill",
            "创建skill",
            "创建技能",
        )
    )


def _derive_skill_metadata(events) -> tuple[str, str, List[str]]:
    user_messages = [event for event in events if isinstance(event, MessageEvent) and event.role == "user"]
    plans = [event for event in events if isinstance(event, PlanEvent)]

    task_user_messages = [
        event
        for event in user_messages
        if not _is_skill_create_request(event.message)
    ]
    task_plans = [
        event
        for event in plans
        if not _is_skill_create_request(event.plan.title)
        and not _is_skill_create_request(event.plan.goal)
        and not _is_skill_create_request(event.plan.message or "")
    ]

    latest_plan = task_plans[-1].plan if task_plans else None
    title = latest_plan.title if latest_plan and latest_plan.title else ""
    goal = latest_plan.goal if latest_plan and latest_plan.goal else ""
    user_goal = task_user_messages[-1].message if task_user_messages else ""

    source = title or goal or user_goal or "saved task skill"
    name = _normalize_skill_name(source)
    description = goal or user_goal[:160] or "Reusable workflow saved from a completed task."

    triggers: list[str] = []
    for value in [title, goal, user_goal]:
        for token in re.split(r"[\s,，。.!?！？;；:/\\|]+", value or ""):
            token = token.strip()
            if 2 <= len(token) <= 32 and token not in triggers:
                triggers.append(token)
            if len(triggers) >= 8:
                break
        if len(triggers) >= 8:
            break

    return name, description, triggers


def _tool_label(tool_event: ToolEvent) -> str:
    return f"{tool_event.tool_name}.{tool_event.function_name}"


def _extract_shell_commands(tools: List[ToolEvent]) -> List[str]:
    commands: list[str] = []
    for tool_event in tools:
        if tool_event.tool_name != "shell":
            continue
        command = tool_event.function_args.get("command")
        if isinstance(command, str) and command.strip():
            commands.append(command.strip())
    return commands


def build_reference_files(events) -> dict[str, str]:
    user_messages = [event for event in events if isinstance(event, MessageEvent) and event.role == "user"]
    assistant_messages = [event for event in events if isinstance(event, MessageEvent) and event.role == "assistant"]
    plans = [event for event in events if isinstance(event, PlanEvent)]
    steps = [event for event in events if isinstance(event, StepEvent)]
    tools = [event for event in events if isinstance(event, ToolEvent)]

    goal = user_messages[-1].message if user_messages else "Repeat this successful task pattern."
    final = assistant_messages[-1].message if assistant_messages else ""
    latest_plan = plans[-1].plan if plans else None

    lines = ["# Saved Session Summary", "", "## User Goal", goal, "", "## Plan"]
    if latest_plan and latest_plan.steps:
        for index, step in enumerate(latest_plan.steps, start=1):
            lines.append(f"{index}. {step.description}")
    elif steps:
        for index, step_event in enumerate(steps, start=1):
            lines.append(f"{index}. {step_event.step.description}")
    else:
        lines.append("No explicit plan was captured.")

    if tools:
        lines.extend(["", "## Tools Used"])
        for tool_event in tools:
            lines.append(f"- `{_tool_label(tool_event)}` args: `{tool_event.function_args}`")

    references = {"references/session-summary.md": "\n".join(lines)}
    if final:
        references["references/final-output.md"] = "# Final Output Pattern\n\n" + final[:4000]
    return references


def build_script_files(events) -> dict[str, str]:
    tools = [event for event in events if isinstance(event, ToolEvent)]
    shell_commands = _extract_shell_commands(tools)
    if not shell_commands:
        return {}

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Commands captured from the saved task. Review before running on a new task.",
    ]
    for command in shell_commands:
        lines.append("")
        lines.append(f"echo {shlex.quote('$ ' + command)}")
        lines.append(command)
    lines.append("")
    return {"scripts/replay-commands.sh": "\n".join(lines)}


def build_skill_content_from_events(events, references: dict[str, str], scripts: dict[str, str]) -> str:
    user_messages = [event for event in events if isinstance(event, MessageEvent) and event.role == "user"]
    plans = [event for event in events if isinstance(event, PlanEvent)]
    steps = [event for event in events if isinstance(event, StepEvent)]
    tools = [event for event in events if isinstance(event, ToolEvent)]

    goal = user_messages[-1].message if user_messages else "Repeat this successful task pattern."
    latest_plan = plans[-1].plan if plans else None

    lines = [
        "# Saved Task Workflow",
        "",
        "Use this skill when the user asks for work similar to the saved task. It captures the reusable workflow and decision points from that successful run.",
        "",
        "Load `references/session-summary.md` before applying this workflow. It contains the captured goal, plan, and tool trace from the source task.",
    ]
    if "references/final-output.md" in references:
        lines.append("Load `references/final-output.md` when the expected output format matters.")
    if scripts:
        lines.append("Use scripts in `scripts/` only after reviewing and adapting their inputs for the current task.")

    lines.extend(["", "## Goal Pattern", goal, "", "## Workflow"])
    if latest_plan and latest_plan.steps:
        for index, step in enumerate(latest_plan.steps, start=1):
            lines.append(f"{index}. {step.description}")
    elif steps:
        for index, step_event in enumerate(steps, start=1):
            lines.append(f"{index}. {step_event.step.description}")
    else:
        lines.append("1. Understand the user's target and reproduce the successful approach from this session.")

    if tools:
        lines.extend(["", "## Tool Usage Notes"])
        seen = []
        for tool_event in tools:
            label = _tool_label(tool_event)
            if label not in seen:
                seen.append(label)
        for label in seen[:12]:
            lines.append(f"- Consider using `{label}` when it matches the user's request.")

    if scripts:
        lines.extend(["", "## Bundled Scripts"])
        for path in scripts:
            lines.append(f"- `{path}`: reusable shell commands captured from the source task; review before running.")

    if references:
        lines.extend(["", "## Bundled References"])
        for path in references:
            lines.append(f"- `{path}`")

    lines.extend([
        "",
        "## Guardrails",
        "- Adapt the workflow to the new user request; do not copy session-specific facts unless the user asks for them.",
        "- Use available tools only when they are relevant to the current task.",
        "- Treat bundled scripts and references as reusable starting points, not as commands to execute blindly.",
    ])
    return "\n".join(lines)


async def create_skill_from_session(
    session_id: str,
    user_id: str,
    session_repository: SessionRepository,
    registry: SkillRegistry,
) -> Skill:
    session = await session_repository.find_by_id_and_user_id(session_id, user_id)
    if not session:
        raise ValueError("Session not found")

    events = await session_repository.get_events(session_id)
    name, description, triggers = _derive_skill_metadata(events)
    references = build_reference_files(events)
    scripts = build_script_files(events)
    content = build_skill_content_from_events(events, references, scripts)
    return await registry.save_generated_skill(
        name=name,
        description=description,
        triggers=triggers,
        content=content,
        user_id=user_id,
        created_from_session_id=session_id,
        workspace_id=personal_workspace_id(user_id),
        references=references,
        scripts=scripts,
    )
