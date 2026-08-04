import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from app.domain.services.resource_usage_service import ResourceUsageService


def test_docker_usage_reports_per_sandbox_cpu_and_disk(monkeypatch):
    class FakeContainer:
        def __init__(self, name: str, status: str):
            self.name = name
            self.status = status

    class FakeDockerApi:
        def containers(self, all=False, size=False):
            return [
                {
                    "Id": "sandbox-container-id",
                    "Names": ["/sandbox-demo"],
                    "State": "running",
                    "Status": "Up 1 minute",
                    "Image": "sandbox:latest",
                    "SizeRw": 1024,
                    "SizeRootFs": 4096,
                },
                {
                    "Id": "backend-container-id",
                    "Names": ["/backend"],
                    "State": "running",
                    "Status": "Up 1 minute",
                    "Image": "backend:latest",
                    "SizeRw": 2048,
                    "SizeRootFs": 8192,
                },
            ]

        def stats(self, container_id, stream=False):
            assert container_id == "sandbox-container-id"
            return {
                "cpu_stats": {
                    "cpu_usage": {"total_usage": 300, "percpu_usage": [1, 1]},
                    "system_cpu_usage": 1200,
                    "online_cpus": 2,
                },
                "precpu_stats": {
                    "cpu_usage": {"total_usage": 100},
                    "system_cpu_usage": 1000,
                },
                "memory_stats": {"usage": 512, "limit": 2048},
                "networks": {
                    "eth0": {"rx_bytes": 10, "tx_bytes": 20},
                    "eth1": {"rx_bytes": 30, "tx_bytes": 40},
                },
                "blkio_stats": {
                    "io_service_bytes_recursive": [
                        {"op": "Read", "value": 100},
                        {"op": "Write", "value": 200},
                    ]
                },
            }

    class FakeDockerClient:
        def __init__(self):
            self.api = FakeDockerApi()
            self.containers = SimpleNamespace(
                list=lambda all=False: [
                    FakeContainer("sandbox-demo", "running"),
                    FakeContainer("backend", "running"),
                ]
            )

    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(from_env=lambda **kwargs: FakeDockerClient()))

    usage = ResourceUsageService()._get_docker_usage(include_sandboxes=True)

    assert usage["available"] is True
    assert usage["sandbox_containers_total"] == 1
    assert usage["sandbox_containers_running"] == 1
    assert usage["sandbox_cpu_percent"] == 200.0
    assert usage["sandbox_memory_bytes"] == 512
    assert usage["sandbox_disk_bytes"] == 1024
    assert usage["sandboxes"][0]["name"] == "sandbox-demo"
    assert usage["sandboxes"][0]["cpu_percent"] == 200.0
    assert usage["sandboxes"][0]["memory_percent"] == 25.0
    assert usage["sandboxes"][0]["network_rx_bytes"] == 40
    assert usage["sandboxes"][0]["network_tx_bytes"] == 60
    assert usage["sandboxes"][0]["block_read_bytes"] == 100
    assert usage["sandboxes"][0]["block_write_bytes"] == 200


def test_sandbox_created_sort_key_accepts_datetime_and_docker_epoch():
    service = ResourceUsageService()
    rows = [
        {"name": "docker-created", "created": 100},
        {"name": "record-created", "created_at": datetime(2026, 1, 1, tzinfo=UTC)},
    ]

    sorted_rows = sorted(rows, key=service._sandbox_created_sort_key, reverse=True)

    assert [row["name"] for row in sorted_rows] == ["record-created", "docker-created"]
