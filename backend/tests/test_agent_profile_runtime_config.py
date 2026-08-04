from app.domain.models.agent_profile import AgentSubAgentConfig
from app.domain.models.plan import Plan
from pydantic import ValidationError
from app.domain.models.message import Message
from app.domain.services.flows.plan_act import PlanActFlow


def test_plan_act_flow_renders_enabled_subagents_in_dynamic_prompt():
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.enabled_subagents = {
        "execution": AgentSubAgentConfig(
            key="execution",
            name="Execution Agent",
            handler_type="execution",
            planner_capability="Run code and tools.",
            use_when="Use for files and shell.",
        )
    }

    prompt = flow._subagent_capabilities_prompt()

    assert "Allowed agent values: execution" in prompt
    assert "SubAgent key: execution" in prompt
    assert "Run code and tools." in prompt


def test_plan_act_flow_does_not_force_vision_step_when_vision_disabled():
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.enabled_subagents = {
        "execution": AgentSubAgentConfig(
            key="execution",
            name="Execution Agent",
            handler_type="execution",
            planner_capability="Run code and tools.",
            use_when="Use for files and shell.",
        )
    }
    flow.plan = type("Plan", (), {"steps": []})()
    message = Message(
        message="analyze image",
        attachment_file_ids=["minio:image"],
        attachment_file_infos=[],
    )

    flow._ensure_vision_step_for_image_message(message)

    assert flow.plan.steps == []


def test_plan_act_flow_normalizes_unavailable_agent_to_execution():
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.enabled_subagents = {
        "execution": AgentSubAgentConfig(
            key="execution",
            name="Execution Agent",
            handler_type="execution",
            planner_capability="Run code and tools.",
            use_when="Use for files and shell.",
        )
    }
    step = type("Step", (), {"agent": "vision"})()
    flow.plan = type("Plan", (), {"steps": [step]})()

    flow._normalize_plan_agents()

    assert step.agent == "execution"


def test_agent_profile_allows_custom_subagent_keys_with_existing_handlers():
    subagent = AgentSubAgentConfig(
        key="data_analyzer",
        name="Data Analyzer",
        handler_type="execution",
        planner_capability="Analyze data files.",
        use_when="Use for tabular data analysis.",
    )

    assert subagent.key == "data_analyzer"
    assert subagent.handler_type == "execution"


def test_plan_accepts_custom_subagent_key():
    plan = Plan.model_validate(
        {
            "goal": "Query institutional database",
            "steps": [
                {
                    "id": "1",
                    "description": "Use the configured database SubAgent",
                    "agent": "instdb",
                }
            ],
        }
    )

    assert plan.steps[0].agent == "instdb"


def test_agent_profile_rejects_unknown_handler_type():
    try:
        AgentSubAgentConfig(key="custom", name="Custom", handler_type="unknown")
    except ValidationError as exc:
        assert "handler_type must be execution or vision" in str(exc)
    else:
        raise AssertionError("expected ValidationError")
