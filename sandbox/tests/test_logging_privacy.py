import logging
import sys
from types import SimpleNamespace

import pytest

from app.core import middleware as middleware_module


@pytest.mark.asyncio
async def test_timeout_middleware_does_not_log_request_path_or_exception(
    monkeypatch,
    caplog,
):
    private_path = "/api/v1/shell/session-private/Users/alice/data.nc"
    private_error = "Bearer middleware-secret /Users/alice/private"

    class Supervisor:
        timeout_active = True
        auto_expand_enabled = True

        async def extend_timeout(self):
            raise RuntimeError(private_error)

    monkeypatch.setattr(
        middleware_module.settings,
        "SERVICE_TIMEOUT_MINUTES",
        1,
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.supervisor",
        SimpleNamespace(supervisor_service=Supervisor()),
    )
    request = SimpleNamespace(url=SimpleNamespace(path=private_path))

    async def call_next(_request):
        return "ok"

    with caplog.at_level(logging.DEBUG, logger=middleware_module.__name__):
        assert await middleware_module.auto_extend_timeout_middleware(
            request,
            call_next,
        ) == "ok"

    records = [
        record
        for record in caplog.records
        if record.name == middleware_module.__name__
    ]
    rendered = "\n".join(record.getMessage() for record in records)
    assert "error_type=RuntimeError" in rendered
    assert private_path not in rendered
    assert "middleware-secret" not in rendered
    assert "/Users/alice" not in rendered
    assert all(record.exc_info is None for record in records)
