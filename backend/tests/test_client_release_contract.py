import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.interfaces.api.config_routes import SUPPORTED_CLIENT_PROTOCOLS, router


@pytest.mark.asyncio
async def test_client_contract_is_public_bounded_and_never_contains_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "private-test-key-never-return")
    monkeypatch.setenv("DATASET_STORAGE_ROOT", "/private/host/path")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/config/client-contract")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"schema_version": 1, "supported_client_protocols": [1]}
    assert "private" not in response.text


def test_current_frontend_protocol_is_supported():
    source = Path(__file__).resolve().parents[2] / "frontend/src/utils/releaseCompatibility.ts"
    if not source.exists():
        pytest.skip("frontend source is not included in the backend-only image")
    client_protocol = int(re.search(r"export const CLIENT_PROTOCOL = (\d+);", source.read_text()).group(1))
    assert client_protocol in SUPPORTED_CLIENT_PROTOCOLS
