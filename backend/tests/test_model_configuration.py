from app.application.services.model_configuration_service import legacy_model_settings
from app.domain.models.agent_profile import AgentProfile, AgentSubAgentConfig
from app.domain.models.model_configuration import ModelConfiguration


def test_model_configuration_builds_runtime_settings_without_exposing_core_overrides():
    model = ModelConfiguration(
        id="model-1",
        name="Primary GPT",
        model_provider="openai",
        model_name="gpt-4.1",
        api_base="https://example.test/v1",
        api_key="secret",
        temperature=0.2,
        max_tokens=4096,
        extra_config={"timeout": 120, "model_name": "must-not-override"},
    )

    assert model.runtime_settings() == {
        "model_provider": "openai",
        "model_name": "gpt-4.1",
        "api_base": "https://example.test/v1",
        "api_key": "secret",
        "temperature": 0.2,
        "max_tokens": 4096,
        "timeout": 120,
    }


def test_legacy_model_settings_supports_profile_and_subagent_data():
    assert legacy_model_settings({
        "model_provider": "deepseek",
        "model_name": "deepseek-chat",
        "temperature": 0.4,
    }) == {
        "model_provider": "deepseek",
        "model_name": "deepseek-chat",
        "temperature": 0.4,
    }

    subagent = AgentSubAgentConfig(
        key="execution",
        name="Execution",
        handler_type="execution",
        model_config={"model_name": "gpt-4o-mini", "max_tokens": 1000},
    )
    assert legacy_model_settings(subagent) == {"model_name": "gpt-4o-mini", "max_tokens": 1000}


def test_agent_profile_accepts_shared_model_references():
    profile = AgentProfile(
        id="profile-1",
        name="Default",
        model_config_id="model-1",
        subagents=[
            AgentSubAgentConfig(
                key="execution",
                name="Execution",
                handler_type="execution",
                model_config_id="model-2",
            )
        ],
    )
    assert profile.model_config_id == "model-1"
    assert profile.subagents[0].model_config_id == "model-2"
