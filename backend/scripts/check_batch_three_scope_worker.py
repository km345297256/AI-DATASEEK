"""Real NGFF scope -> range broker -> isolated worker acceptance.

Only synthetic Zarr MemoryStore bytes are generated in a networkless sandbox.
The source is staged under an owned TemporaryDirectory in this one-off backend
container, not the production dataset volume. Catalog preferences/references
are in memory; no database, user files, sessions or model APIs are touched.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import docker

from app.application.errors.exceptions import NotFoundError
from app.application.services.dataset_file_preview import DatasetFilePreviewRequest, DatasetFilePreviewService
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.file_service import FileService
from app.application.services.ome_zarr_scope import ome_zarr_visualization
from app.application.services.unified_visualization import VisualizationRequest, unified_visualization
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.core.config import get_settings
from app.domain.models.dataset import DataCenterDataset, DatasetFile, DatasetLocation, DatasetStorageType
from app.domain.models.visualization import VisualizationSnapshot
from app.infrastructure.external.file.datasetfile import DatasetPreviewFileStorage
from app.infrastructure.external.sandbox import window_visualization_worker as gateway
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.external.sandbox.node_health import LOCAL_DEFAULT_NODE_ID

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "viz-ome-zarr"
OPTIONS = {"level": 1, "indices": [1, 1, 2], "roi": [1, 1, 3, 2]}


def generate(image):
    source = (ROOT / "sandbox/tests/ome_zarr_window_fixtures.py").read_text()
    code = source.replace('if __name__ == "__main__":', 'if False:')
    code += '\nimport base64\nstore,_=memory_store(compression="zlib")\nprint(json.dumps({key:base64.b64encode(bytes(store[key])).decode() for key in store.keys()}),flush=True)\n'
    client, container = docker.from_env(timeout=10), None
    name = "ai-dataseek-ngff-fixture-" + uuid.uuid4().hex
    try:
        container = client.containers.create(image, name=name, entrypoint=["/usr/bin/timeout"],
            command=["--signal=KILL", "55s", "/app/.venv/bin/python", "-c", code],
            working_dir="/app", user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="1g", memswap_limit="1g",
            nano_cpus=1000000000, pids_limit=96, tmpfs={"/tmp": "rw,noexec,nosuid,size=16m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        container.reload()
        assert not container.attrs.get("Mounts") and container.attrs["HostConfig"]["NetworkMode"] == "none"
        container.start()
        assert container.wait(timeout=60)["StatusCode"] == 0, "Synthetic generation failed"
        output = container.logs(stdout=True, stderr=False)
        assert len(output) < 256 * 1024
        return {key: base64.b64decode(value, validate=True) for key, value in json.loads(output).items()}
    finally:
        try:
            if container is None:
                try: container = client.containers.get(name)
                except docker.errors.NotFound: pass
            if container is not None: _remove_worker(container)
        finally:
            client.close()


class Preferences:
    def __init__(self): self.states = {}
    async def get_states(self, owner): return dict(self.states)
    async def set_state(self, owner, plugin, enabled): self.states[plugin] = enabled


class References:
    def __init__(self): self.rows = {}
    async def get(self, ident): return self.rows.get(ident)
    async def put(self, ident, row): self.rows[ident] = {"_id": ident, **row}; return self.rows[ident]


class Runtime:
    def __init__(self):
        self.snapshot = VisualizationSnapshot(engine="cordis", revision="a" * 64,
            plugins=[json.loads((ROOT / "plugin-host/visualizations/ome-zarr.json").read_text())])
    async def visualization_snapshot(self): return self.snapshot


class Datasets:
    def __init__(self, dataset): self.dataset, self.archived = dataset, False
    async def get_dataset(self, ident, user_id=None):
        if self.archived or ident != self.dataset.dataset_id or user_id != "synthetic-owner":
            raise NotFoundError("Dataset not found")
        return self.dataset.model_copy(deep=True)


@contextlib.contextmanager
def observe(containers):
    original = gateway.docker.from_env
    class Containers:
        def __init__(self, real): self.real = real
        def get(self, name): return self.real.get(name)
        def create(self, **kwargs):
            result = self.real.create(**kwargs)
            containers.append(result.id)
            result.reload()
            attrs, host = result.attrs, result.attrs["HostConfig"]
            assert host["ReadonlyRootfs"] and host["NetworkMode"] == "none" and not host["Privileged"]
            assert attrs["Config"]["User"] == "65534:65534" and not attrs.get("Mounts")
            assert host["Memory"] == host["MemorySwap"] == 1024**3 and host["PidsLimit"] == 96
            assert not host.get("Binds") and not host.get("Devices") and not host.get("PortBindings")
            assert host["CapDrop"] == ["ALL"] and "no-new-privileges:true" in host["SecurityOpt"]
            assert host["AutoRemove"] and host["LogConfig"]["Type"] == "none"
            assert attrs["Config"]["Entrypoint"] == ["/usr/bin/timeout"]
            return result
    class Client:
        def __init__(self, real): self.real, self.containers = real, Containers(real.containers)
        def close(self): self.real.close()
    gateway.docker.from_env = lambda *args, **kwargs: Client(original(*args, **kwargs))
    try: yield
    finally: gateway.docker.from_env = original


async def check(folder, objects, image, containers):
    dataset_id = "synthetic-ngff-" + uuid.uuid4().hex
    root = Path(folder) / dataset_id / "image.zarr"
    offset, resources = 0, []
    for key, data in sorted(objects.items()):
        resources.append({"key": key, "offset": offset, "size": len(data)})
        offset += len(data)
    gateway.validate_resources(resources, offset)  # Validate keys before any staging.
    for key, data in objects.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    dataset = DataCenterDataset(dataset_id=dataset_id, data_center_id="synthetic", data_center_name="synthetic", name="synthetic",
        files=[DatasetFile(path="image.zarr/" + key, size=len(data)) for key, data in objects.items()],
        locations=[DatasetLocation(node_id=LOCAL_DEFAULT_NODE_ID, storage_type=DatasetStorageType.MANAGED_UPLOAD,
            source_path=dataset_id, read_only=True, verified=True)])
    datasets, references, preferences = Datasets(dataset), References(), Preferences()
    catalog = VisualizationCatalogService(Runtime(), preferences)
    previews = DatasetFilePreviewService(datasets=datasets, repository=references, catalog=catalog,
        settings=SimpleNamespace(jwt_secret_key="synthetic-only-not-a-credential", dataset_storage_root=folder,
            dataset_host_path_allowlist="/no-host-source-granted"))
    files = FileService(DatasetPreviewFileStorage(SimpleNamespace(), previews))
    info = (await previews.prepare(dataset_id, "synthetic-owner", DatasetFilePreviewRequest(path="image.zarr/.zattrs", plugin_id=PLUGIN))).file
    assert info.file_id.startswith("dataset-preview:") and folder not in info.model_dump_json()
    async def request(kind="tree", options=None, version=None, owner="synthetic-owner"):
        return await unified_visualization(files, catalog, image, info.file_id, owner,
            VisualizationRequest(plugin_id=PLUGIN, operation="preview", kind=kind, options=options or {}, version=version))
    checks, negatives = [], []
    with observe(containers):
        tree = await request()
        assert tree.kind == "tree" and tree.metadata["loaded_chunks"] == 0
        checks.append({"case": "scoped-tree", **tree.metadata})
        pixels = await request("image", OPTIONS, tree.version)
        assert pixels.kind == "array" and pixels.payload["array"]["values"] == [713, 715, 717, 731, 733, 735]
        assert pixels.metadata["loaded_chunks"] == 1 and pixels.metadata["decoded_chunk_bytes"] == 48
        assert pixels.metadata["read_bytes"] < pixels.metadata["source_bytes"]
        assert all(s not in pixels.model_dump_json() for s in (folder, "PRIVATE", dataset_id))
        checks.append({"case": "scoped-TCZYX-ROI", **pixels.metadata})
        async def rejected(label, call, exception=(ValueError, FileNotFoundError)):
            try: await call()
            except exception: negatives.append(label)
            else: raise AssertionError(label + " was accepted")
        await rejected("missing-version", lambda: request("image", OPTIONS))
        await rejected("stale-version", lambda: request("image", OPTIONS, "0" * 64), PreviewVersionChanged)
        await rejected("foreign-owner", lambda: request(owner="other-owner"))
        await catalog.set_state("synthetic-owner", PLUGIN, False)
        await rejected("disabled", request, VisualizationDisabledError)
        await catalog.set_state("synthetic-owner", PLUGIN, True)
        # Mutate an untouched pixel object after the real worker returns tree.
        # The final all-object stat fence must reject even though no pixels read.
        path = root / "0/0.0.0.0.0"
        original = path.read_bytes()
        real_worker = gateway.run_window_visualization_worker
        def changed_worker(*args, **kwargs):
            result = real_worker(*args, **kwargs)
            path.write_bytes(original + b"x")
            return result
        await rejected("unread-member-final-fence", lambda: ome_zarr_visualization(files, catalog, image,
            info.file_id, "synthetic-owner", VisualizationRequest(plugin_id=PLUGIN, operation="preview", kind="tree"),
            worker=changed_worker), PreviewVersionChanged)
        path.write_bytes(original)
        datasets.archived = True
        await rejected("archived-scope", request)
    return {"positive": checks, "negative": negatives, "registered_objects": len(objects), "production_writes": 0}


def main():
    image = get_settings().sandbox_image
    assert image, "Configure the existing sandbox image"
    objects = generate(image)
    containers, result = [], None
    try:
        with tempfile.TemporaryDirectory(prefix="dataseek-ngff-scope-check-") as folder:
            result = asyncio.run(check(folder, objects, image, containers))
        assert not Path(folder).exists()
    finally:
        client, remaining = docker.from_env(timeout=5), []
        try:
            for ident in containers:
                try: container = client.containers.get(ident)
                except docker.errors.NotFound: continue
                remaining.append(ident)
                _remove_worker(container)
        finally: client.close()
        assert not remaining, "A range worker needed fallback cleanup"
    print(json.dumps({"passed": True, **result, "workers_verified": len(containers),
        "worker_residue": 0, "temporary_sources_removed": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
