import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.application.errors.exceptions import AppException
from app.interfaces.errors import exception_handlers as handlers_module
from app.interfaces.errors.exception_handlers import register_exception_handlers


def _handler_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name == handlers_module.__name__
    ]


def test_exception_handlers_log_only_classification_metadata(caplog):
    secret = "Bearer backend-secret /Users/alice/private/data.nc"
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/application")
    async def application_error():
        raise AppException(code=409, msg=secret, status_code=409)

    @app.get("/http")
    async def http_error():
        raise HTTPException(status_code=403, detail=secret)

    @app.get("/unexpected")
    async def unexpected_error():
        raise RuntimeError(secret)

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger=handlers_module.__name__):
        assert client.get("/application").status_code == 409
        assert client.get("/http").status_code == 403
        assert client.get("/unexpected").status_code == 500

    records = _handler_records(caplog)
    rendered = "\n".join(record.getMessage() for record in records)
    assert "backend-secret" not in rendered
    assert "/Users/alice" not in rendered
    assert "error_type=RuntimeError" in rendered
    assert all(record.exc_info is None for record in records)


def test_backend_runtime_entrypoints_do_not_log_exception_tracebacks_or_values():
    for module in (handlers_module,):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "logger.exception(" not in source
        assert "str(exc)" not in source

    main_source = Path(
        handlers_module.__file__.replace(
            "interfaces/errors/exception_handlers.py",
            "main.py",
        )
    ).read_text(encoding="utf-8")
    assert "logger.exception(" not in main_source
    assert "str(exc)" not in main_source
    assert "str(e)" not in main_source
