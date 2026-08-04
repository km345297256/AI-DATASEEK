"""
Pytest configuration and fixtures
"""
import sys
import pytest
import socket
import threading
import time
from pathlib import Path

# Add the parent directory to Python path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.file import router as file_router
from app.api.v1.shell import router as shell_router
from app.core.exceptions import (
    AppException,
    app_exception_handler,
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)

app = FastAPI()
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)
app.include_router(file_router, prefix="/api/v1/file")
app.include_router(shell_router, prefix="/api/v1/shell")

with socket.socket() as port_socket:
    port_socket.bind(("127.0.0.1", 0))
    TEST_PORT = port_socket.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


@pytest.fixture(scope="session", autouse=True)
def sandbox_api_server():
    """Run the Sandbox API in-process so tests need no external service."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=TEST_PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", TEST_PORT)) == 0:
                break
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=1)
        raise RuntimeError("Sandbox test server did not start")

    yield

    server.should_exit = True
    thread.join(timeout=5)

@pytest.fixture
def client():
    """Create a session targeting the managed in-process test server."""
    with requests.Session() as session:
        yield session


@pytest.fixture
def temp_test_file(client, tmp_path):
    """Create a temporary file through the in-process API."""
    temp_file = str(tmp_path / "test_file.txt")
    content = "Line 1: Hello World\nLine 2: This is a test\nLine 3: Python testing"
    response = client.post(f"{BASE_URL}/api/v1/file/write", json={
        "file": temp_file,
        "content": content
    })
    assert response.status_code == 200
    yield temp_file
