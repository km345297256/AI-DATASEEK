import httpx
import pytest

from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


@pytest.mark.asyncio
async def test_artifact_validation_adapter_uses_batch_read_only_api():
    items = [{"path": "/home/ubuntu/output/chart.png", "kind": "image"}]
    receipt = {"path": items[0]["path"], "expected_kind": "image", "kind": "image", "valid": True,
               "reason": "validated", "sha256": "a" * 64, "size": 100,
               "metadata": {"format": "PNG", "width": 10, "height": 10, "frames": 1}}
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": {"version": 1, "files": [receipt]}})
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sandbox.client = client
        result = await sandbox.validate_artifacts(items)
    assert result.success is True
    assert result.data["files"] == [receipt]
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/file/validate-artifacts"
    assert requests[0].method == "POST"


@pytest.mark.parametrize("failure", ["404", "500", "timeout", "not_json", "bad_version", "missing_files"])
@pytest.mark.asyncio
async def test_missing_validation_never_falls_back_to_existence(failure):
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if failure == "timeout":
            raise httpx.ReadTimeout("private endpoint must not be exposed")
        if failure in {"404", "500"}:
            return httpx.Response(int(failure), text="private server details")
        if failure == "not_json":
            return httpx.Response(200, text="private server details")
        data = {"version": 2, "files": []} if failure == "bad_version" else {"version": 1}
        return httpx.Response(200, json={"success": True, "data": data})
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sandbox.client = client
        result = await sandbox.validate_artifacts([{"path": "/home/ubuntu/output/chart.png", "kind": "image"}])
    assert result.success is False
    assert result.data == {"version": 1, "code": "validation_unavailable", "files": []}
    assert "private" not in result.model_dump_json()
    assert calls == ["/api/v1/file/validate-artifacts"]


@pytest.mark.parametrize("mode", ["valid", "partial", "404", "timeout", "malformed"])
@pytest.mark.asyncio
async def test_analysis_fingerprints_have_no_legacy_fallback(mode):
    source = "/home/ubuntu/datasets/registered/source.csv"
    paths = [source]
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if mode == "404":
            return httpx.Response(404)
        if mode == "timeout":
            raise httpx.ReadTimeout("private endpoint")
        if mode == "malformed":
            return httpx.Response(200, json={"success": True, "data": {"version": 1, "files": []}})
        files = [{"path": source, "size": 4, "sha256": "b" * 64}] if mode == "valid" else []
        errors = [] if mode == "valid" else [{"path": source, "code": "snapshot_size_limit"}]
        return httpx.Response(200, json={"success": True, "data": {"version": 1, "files": files, "errors": errors}})
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sandbox.client = client
        result = await sandbox.analysis_fingerprints(paths)
    assert result.success is (mode in {"valid", "partial"})
    if mode == "partial":
        assert result.data["errors"] == [{"path": source, "code": "snapshot_size_limit"}]
    elif mode not in {"valid"}:
        assert result.data["code"] == "snapshot_unavailable"
    assert calls == ["/api/v1/file/analysis-fingerprints"]
