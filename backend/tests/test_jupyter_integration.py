from types import SimpleNamespace

import pytest

from app.application.services.jupyter_service import JupyterService
from app.interfaces.api.session_routes import _jupyter_ticket_user, open_jupyter_notebook
from app.interfaces.schemas.session import OpenJupyterRequest


@pytest.mark.asyncio
async def test_jupyter_rejects_non_python_before_allocating_resources():
    service = JupyterService()
    with pytest.raises(ValueError, match="Only Python"):
        await service.open_notebook(
            session_id="session-a",
            user_id="user-a",
            code="console.log('x')",
            language="javascript",
            sandbox_id=None,
        )


def test_jupyter_names_are_isolated_by_session():
    assert JupyterService._name("session-a") != JupyterService._name("session-b")
    assert JupyterService._volume("session-a") != JupyterService._volume("session-b")


def test_jupyter_dataset_mount_is_visible_without_exposing_internal_ids():
    target = JupyterService._visible_dataset_target(
        "/home/ubuntu/datasets/tds-private/sources/dsl-private/storage-id",
        0,
        1,
    )
    assert target == "/home/jovyan/work/datasets/current"


def test_jupyter_multiple_dataset_mounts_have_distinct_visible_targets():
    first = JupyterService._visible_dataset_target(
        "/home/ubuntu/datasets/tds-a/sources/dsl-a/one.nc",
        0,
        2,
    )
    second = JupyterService._visible_dataset_target(
        "/home/ubuntu/datasets/tds-a/sources/dsl-b/two.nc",
        1,
        2,
    )
    assert first == "/home/jovyan/work/datasets/source-1-one.nc"
    assert second == "/home/jovyan/work/datasets/source-2-two.nc"


@pytest.mark.asyncio
async def test_jupyter_route_keeps_internal_token_out_of_response():
    class Sandbox:
        async def open_browser_url(self, url):
            assert "private-token" in url

    class AgentService:
        async def ensure_interactive_sandbox(self, session_id, user_id):
            return SimpleNamespace(id=session_id, sandbox_id="sandbox-a"), Sandbox()

    class NotebookService:
        async def open_notebook(self, **kwargs):
            assert kwargs["session_id"] == "session-a"
            assert kwargs["user_id"] == "user-a"
            return {
                "notebook_path": "DataSeek.ipynb",
                "browser_url": "http://private:8888/lab?token=private-token",
            }

    response = await open_jupyter_notebook(
        session_id="session-a",
        request=OpenJupyterRequest(code="print(1)", language="python"),
        current_user=SimpleNamespace(id="user-a"),
        agent_service=AgentService(),
        jupyter_service=NotebookService(),
    )
    assert response.data.notebook_path == "DataSeek.ipynb"
    assert "token" not in response.data.model_dump()
    assert response.data.embed_url.startswith("/api/v1/sessions/session-a/jupyter-proxy/")
    assert "private-token" not in response.data.embed_url


def test_jupyter_proxy_ticket_is_scoped_to_session_and_resource_type():
    class TokenService:
        def verify_token(self, token):
            assert token == "ticket"
            return {
                "type": "resource_access",
                "resource_type": "jupyter",
                "resource_id": "session-a",
                "user_id": "user-a",
            }

    service = TokenService()
    assert _jupyter_ticket_user(service, "ticket", "session-a") == "user-a"
    assert _jupyter_ticket_user(service, "ticket", "session-b") is None


def test_jupyter_names_follow_deployment_prefix(monkeypatch):
    from app.application.services import jupyter_service
    from types import SimpleNamespace
    monkeypatch.setattr(jupyter_service, "get_settings", lambda: SimpleNamespace(jupyter_name_prefix="datalab-jupyter"))
    assert jupyter_service.JupyterService._name("session123") == "datalab-jupyter-session123"
    assert jupyter_service.JupyterService._volume("session123") == "datalab-jupyter-work-session123"
