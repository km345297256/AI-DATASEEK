"""Active analysis containers must not inherit supervisor shutdown timers."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.config import Settings
from app.infrastructure.external.sandbox import docker_sandbox


def test_retired_sandbox_ttl_environment_cannot_reactivate_runtime_expiry(monkeypatch):
    monkeypatch.setenv("SANDBOX_TTL_MINUTES", "1")
    settings = Settings(_env_file=None)
    assert "sandbox_ttl_minutes" not in Settings.model_fields
    assert "sandbox_ttl_minutes" not in settings.model_dump()


def test_container_launch_does_not_arm_service_shutdown_even_with_stale_settings(monkeypatch):
    container = SimpleNamespace(reload=MagicMock())
    client = SimpleNamespace(containers=SimpleNamespace(run=MagicMock(return_value=container)), close=MagicMock())
    monkeypatch.setattr(docker_sandbox.docker, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(docker_sandbox, "get_settings", lambda: SimpleNamespace(
        sandbox_image="synthetic", sandbox_name_prefix="synthetic", sandbox_ttl_minutes=1,
        sandbox_docker_create_timeout_seconds=60, sandbox_network=None, sandbox_chrome_args="",
        sandbox_https_proxy=None, sandbox_http_proxy=None, sandbox_no_proxy=None,
    ))
    monkeypatch.setattr(docker_sandbox.DockerSandbox, "_get_container_ip", lambda container: "127.0.0.1")
    monkeypatch.setattr(docker_sandbox.DockerSandbox, "_get_container_image_identity",
                        lambda container, **kwargs: ("synthetic", "sha256:synthetic"))
    sandbox = docker_sandbox.DockerSandbox._create_task()
    environment = client.containers.run.call_args.kwargs["environment"]
    assert "SERVICE_TIMEOUT_MINUTES" not in environment
    assert sandbox.id.startswith("synthetic-")
    client.close.assert_called_once()
