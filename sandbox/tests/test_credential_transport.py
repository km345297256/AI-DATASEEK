import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.shell import ShellExecRequest
from app.services.shell import ShellService


def test_private_credentials_are_not_serialized_or_repr_and_slots_are_bounded():
    secret = "private-test-secret"
    request = ShellExecRequest(command="trusted tool", credentials={"api_key": secret})
    assert secret not in repr(request)
    assert "credentials" not in request.model_dump()
    assert request.credentials["api_key"].get_secret_value() == secret
    for credentials in ({"PATH": secret}, {"a;echo": secret}, {"key": "short"}, {"key": "x" * 8193}, {"key": "x" * 10 + "\x00"}):
        with pytest.raises(ValidationError):
            ShellExecRequest(command="trusted tool", credentials=credentials)


@pytest.mark.asyncio
async def test_credentials_live_in_one_child_env_not_command_or_server(monkeypatch):
    service = ShellService()
    monkeypatch.setattr(service, "_capture_created_process_group", lambda process: None)
    spawn = AsyncMock(side_effect=lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr("app.services.shell.asyncio.create_subprocess_shell", spawn)
    before = dict(os.environ)
    secret = "private-child-secret"
    await service._create_process("trusted command", "/tmp", credentials={"api_key": secret, "path": secret})
    args, kwargs = spawn.call_args
    assert secret not in args[0]
    assert kwargs["env"]["DATASEEK_CREDENTIAL_API_KEY"] == secret
    assert kwargs["env"]["PATH"] == before["PATH"]
    assert dict(os.environ) == before
    await service._create_process("ordinary command", "/tmp")
    assert "env" not in spawn.call_args.kwargs
