"""Format evidence is bounded metadata, not a filename or decoder assumption."""
import asyncio
import struct
from types import SimpleNamespace

import pytest

from app.application.services import visualization_probe as probe


HDF5 = b"\x89HDF\r\n\x1a\n"
OME = b'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"><Image ID="Image:0"><Pixels DimensionOrder="XYZCT"/></Image></OME>\0'


class MemoryRanges:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def __call__(self, offset, length):
        self.calls.append((offset, length))
        return self.data[offset:offset + length]


async def inspect(data, **kwargs):
    reader = MemoryRanges(data)
    result = await probe.probe_visualization_content(reader, len(data), **kwargs)
    assert result.bytes_read == sum(length for _, length in reader.calls)
    assert result.bytes_read <= probe.MAX_PROBE_BYTES
    return result, reader


def matlab_header(version=5, endian="<"):
    data = bytearray(128)
    text = b"MATLAB 7.3 MAT-file" if version == 73 else b"MATLAB 5.0 MAT-file"
    data[:len(text)] = text
    struct.pack_into(endian + "H", data, 124, 0x0200 if version == 73 else 0x0100)
    data[126:128] = b"IM" if endian == "<" else b"MI"
    return bytes(data)


def tiff(tags=(), *, big=False, endian="<", next_ifd=0):
    header_size, count_size, entry_size, inline = (16, 8, 20, 8) if big else (8, 2, 12, 4)
    pointer = "Q" if big else "I"
    data = bytearray((b"II" if endian == "<" else b"MM") + struct.pack(endian + "H", 43 if big else 42))
    data += struct.pack(endian + "HHQ", 8, 0, header_size) if big else struct.pack(endian + "I", header_size)
    data += struct.pack(endian + ("Q" if big else "H"), len(tags))
    start = header_size + count_size + entry_size * len(tags) + inline
    payload = bytearray()
    for tag, dtype, number, value in tags:
        data += struct.pack(endian + "HH" + pointer, tag, dtype, number)
        if len(value) <= inline:
            data += value.ljust(inline, b"\0")
        else:
            data += struct.pack(endian + pointer, start + len(payload))
            payload += value
    data += struct.pack(endian + pointer, next_ifd)
    return bytes(data + payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,dialect", [(1, "netcdf-classic"), (2, "netcdf-64bit-offset"), (5, "netcdf-cdf5")])
async def test_netcdf_magic_not_filename(code, dialect):
    result, _ = await inspect(b"CDF" + bytes([code]) + b"\0" * 28)
    assert result.container == "netcdf" and result.dialect == dialect
    assert result.traits == ["numeric-container"]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 512, 1024, 8192, 1024 * 1024])
async def test_hdf5_userblock_powers_without_claiming_netcdf4(offset):
    result, reads = await inspect(b"\0" * offset + HDF5 + b"\0" * 64)
    assert result.container == "hdf5" and result.dialect == "hdf5"
    assert result.bytes_read <= probe.MAX_PROBE_BYTES
    assert all(length <= probe.PROBE_BLOCK_BYTES for _, length in reads.calls)
    if offset <= 1024:
        assert len(reads.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [64, 511, 513, 768, 4097])
async def test_hdf5_embedded_arbitrary_magic_not_userblock(offset):
    result, _ = await inspect(b"\0" * offset + HDF5 + b"\0" * 64)
    assert result.container == "unknown"


@pytest.mark.asyncio
async def test_hdf5_userblock_beyond_budget_is_inconclusive():
    result, reads = await inspect(b"\0" * (2 * 1024 * 1024) + HDF5)
    assert result.container == "unknown" and result.truncated
    assert max(offset for offset, _ in reads.calls) <= probe.MAX_HDF5_USERBLOCK


@pytest.mark.asyncio
@pytest.mark.parametrize("endian", ["<", ">"])
async def test_matlab_level5_is_not_hdf5(endian):
    result, _ = await inspect(matlab_header(endian=endian) + b"\0" * 512 + HDF5)
    assert result.container == "mat" and result.dialect == "matlab-level5"


@pytest.mark.asyncio
@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("offset", [512, 1024, 2048])
async def test_matlab73_requires_header_and_legal_hdf5_signature(endian, offset):
    result, _ = await inspect(matlab_header(73, endian).ljust(offset, b"\0") + HDF5)
    assert result.container == "hdf5" and result.dialect == "matlab-v7.3"
    assert result.evidence == ["matlab-v7.3-header", "hdf5-userblock-signature"]


@pytest.mark.asyncio
async def test_declared_mat73_missing_signature_does_not_route_to_level5():
    result, _ = await inspect(matlab_header(73) + b"\0" * 512)
    assert result.container == "unknown" and result.truncated
    assert result.evidence == ["matlab-v7.3-header"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [b"MATLAB 5.0 MAT-file", matlab_header()[:126] + b"XX", matlab_header()[:124] + b"\x02\x00IM"])
async def test_malformed_mat_header_fails_closed(bad):
    with pytest.raises(probe.VisualizationProbeRejected):
        await inspect(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("big", [False, True])
@pytest.mark.parametrize("endian", ["<", ">"])
async def test_plain_tiff_not_ome_or_geo(big, endian):
    result, _ = await inspect(tiff([(256, 4, 1, struct.pack(endian + "I", 100))], big=big, endian=endian))
    assert result.container == "tiff" and result.dialect == ("bigtiff" if big else "tiff")
    assert result.traits == [] and not result.truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("big", [False, True])
@pytest.mark.parametrize("endian", ["<", ">"])
async def test_ome_content_without_filename(big, endian):
    result, _ = await inspect(tiff([(270, 2, len(OME), OME)], big=big, endian=endian))
    assert result.traits == ["ome"] and "tiff-ome-xml" in result.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [b"This is OME, not XML\0", b"<OME><Pixels/></OME>\0", b'<OME xmlns="https://evil.example/OME"><Pixels/></OME>\0', b'<?xml version="1.0"?><root>OME</root>\0'])
async def test_ome_lookalikes_do_not_get_ome_trait(description):
    result, _ = await inspect(tiff([(270, 2, len(description), description)]))
    assert "ome" not in result.traits


@pytest.mark.asyncio
async def test_ome_description_does_not_leak_metadata_or_paths():
    description = OME.replace(b"<Pixels", b'<Description>/Users/private/patient</Description><Pixels')
    result, _ = await inspect(tiff([(270, 2, len(description), description)]))
    assert "ome" in result.traits and "private" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [b'<!DOCTYPE OME [<!ENTITY e "evil">]><OME/>\0', b'<!ENTITY x SYSTEM "file:///tmp/secret"><OME/>\0'])
async def test_tiff_xml_entity_declarations_rejected(description):
    with pytest.raises(probe.VisualizationProbeRejected):
        await inspect(tiff([(270, 2, len(description), description)]))


@pytest.mark.asyncio
async def test_oversized_description_marks_incomplete_not_plain_tiff():
    description = b" " * probe.MAX_DESCRIPTION_BYTES + OME
    result, reads = await inspect(tiff([(270, 2, len(description), description)]))
    assert result.truncated and "ome" not in result.traits
    assert result.bytes_read <= probe.PROBE_BLOCK_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("big", [False, True])
@pytest.mark.parametrize("endian", ["<", ">"])
async def test_geo_keys_are_routing_evidence_not_crs_validation(big, endian):
    keys = struct.pack(endian + "8H", 1, 1, 0, 1, 2048, 0, 1, 4326)
    result, _ = await inspect(tiff([(34735, 3, 8, keys)], big=big, endian=endian))
    assert result.traits == ["geotiff"] and "tiff-geokeys" in result.evidence


@pytest.mark.asyncio
async def test_tiff_can_have_both_geographic_and_ome_evidence():
    keys = struct.pack("<8H", 1, 1, 0, 1, 2048, 0, 1, 4326)
    result, _ = await inspect(tiff([(270, 2, len(OME), OME), (34735, 3, 8, keys)]))
    assert result.traits == ["geotiff", "ome"]


@pytest.mark.asyncio
@pytest.mark.parametrize("big", [False, True])
async def test_common_tiff_metadata_costs_one_range_without_decoding_pixels(big):
    # The final bytes stand for much larger image strips, never decoded by probe.
    data = tiff([(270, 2, len(OME), OME)], big=big) + b"\0" * 100000
    result, reader = await inspect(data)
    assert result.traits == ["ome"]
    assert reader.calls == [(0, probe.PROBE_BLOCK_BYTES)]


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [struct.pack("<4H", 2, 1, 0, 0), struct.pack("<4H", 1, 1, 0, 8), b"\0\0"])
async def test_malformed_geo_keys_rejected(keys):
    with pytest.raises(probe.VisualizationProbeRejected):
        await inspect(tiff([(34735, 3, len(keys) // 2, keys)]))


@pytest.mark.asyncio
async def test_geo_transform_is_not_sufficient_crs_claim():
    result, _ = await inspect(tiff([(33550, 12, 3, struct.pack("<3d", 1, 1, 0)), (33922, 12, 6, struct.pack("<6d", *([0] * 6)))]))
    assert result.traits == ["geotiff"] and result.evidence[-1] == "tiff-geotransform"


@pytest.mark.asyncio
@pytest.mark.parametrize("big", [False, True])
async def test_tiff_ifd_cycles_rejected(big):
    with pytest.raises(probe.VisualizationProbeRejected, match="循环"):
        await inspect(tiff(big=big, next_ifd=16 if big else 8))


@pytest.mark.asyncio
@pytest.mark.parametrize("big", [False, True])
async def test_tiff_offset_outside_file_rejected(big):
    with pytest.raises(probe.VisualizationProbeRejected, match="越界"):
        await inspect(tiff(big=big, next_ifd=1 << 31))


@pytest.mark.asyncio
async def test_tiff_offset_into_header_rejected():
    with pytest.raises(probe.VisualizationProbeRejected, match="文件头"):
        await inspect(tiff(next_ifd=2))


@pytest.mark.asyncio
async def test_tiff_subifd_cycles_rejected():
    with pytest.raises(probe.VisualizationProbeRejected, match="循环"):
        await inspect(tiff([(330, 4, 1, struct.pack("<I", 8))]))


@pytest.mark.asyncio
async def test_tiff_tag_payload_offset_outside_file_rejected():
    data = bytearray(tiff([(270, 2, len(OME), OME)]))
    struct.pack_into("<I", data, 18, 1 << 31)
    with pytest.raises(probe.VisualizationProbeRejected, match="越界"):
        await inspect(bytes(data))


@pytest.mark.asyncio
async def test_tiff_duplicate_tags_rejected():
    tag = (256, 4, 1, struct.pack("<I", 100))
    with pytest.raises(probe.VisualizationProbeRejected, match="重复标签"):
        await inspect(tiff([tag, tag]))


@pytest.mark.asyncio
async def test_tiff_ifd_entry_budget_is_inconclusive():
    tags = [(1000 + index, 4, 1, struct.pack("<I", 1)) for index in range(probe.MAX_IFD_ENTRIES + 1)]
    result, reads = await inspect(tiff(tags))
    assert result.container == "tiff" and result.truncated
    assert result.bytes_read == min(len(reads.data), probe.PROBE_BLOCK_BYTES)


@pytest.mark.asyncio
async def test_tiff_ifd_chain_budget_is_inconclusive():
    # Nine empty classic IFDs linked sequentially; no image bytes are decoded.
    data = b"II*\0" + struct.pack("<I", 8)
    for index in range(probe.MAX_IFDS + 1):
        following = 8 + 6 * (index + 1) if index < probe.MAX_IFDS else 0
        data += struct.pack("<HI", 0, following)
    result, _ = await inspect(data)
    assert result.container == "tiff" and result.truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"x", b"CDF\x03" + b"\0" * 124, b"MATLAB data is not a header", b"II\x29\0" + b"\0" * 12])
async def test_unknown_magic_never_guesses_from_content_words(payload):
    result, _ = await inspect(payload)
    assert result.container == "unknown"


@pytest.mark.asyncio
async def test_small_probe_budget_does_not_fall_back_to_unbounded_read():
    result, reads = await inspect(b"\0" * 512 + HDF5, max_bytes=4)
    assert result.truncated and result.container == "unknown" and not reads.calls


@pytest.mark.asyncio
async def test_short_range_is_rejected():
    async def broken(*_):
        return b"x"
    with pytest.raises(probe.VisualizationProbeRejected, match="不完整"):
        await probe.probe_visualization_content(broken, 1024)


class FakePlugin:
    id = "tiff"
    adapter = "tiff"
    limits = SimpleNamespace(max_input_bytes=64 * 1024 * 1024)
    capabilities = SimpleNamespace(operations=["bytes", "prepare"])

    def matches_filename(self, filename):
        return filename.lower().endswith((".tif", ".tiff"))

    def model_dump(self, **_):
        return {"id": self.id, "adapter": self.adapter, "version": "1.0.0"}


class FakeCatalog:
    def __init__(self):
        self.plugin, self.enabled, self.revision = FakePlugin(), True, "a" * 64

    async def require_enabled(self, user, plugin_id):
        from app.application.services.visualization_catalog import VisualizationDisabledError
        if not self.enabled:
            raise VisualizationDisabledError()
        return self.plugin

    async def list_for_user(self, user):
        return SimpleNamespace(revision=self.revision, plugins=[SimpleNamespace(id="tiff", enabled=self.enabled, model_dump=self.plugin.model_dump)])


class FakeStorage:
    def __init__(self):
        from app.domain.models.file import FileInfo
        self.data = tiff([(270, 2, len(OME), OME)])
        self.info = FileInfo(file_id="file", filename="sample.tif", size=len(self.data), user_id="owner")
        self.calls = []

    async def get_file_info(self, file, user):
        return self.info if file == "file" and user == "owner" else None

    async def download_file_range(self, file, user, *, offset, length):
        self.calls.append((offset, length))
        return self.data[offset:offset + length], self.info


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(probe, "_PROBE_SLOTS", asyncio.Semaphore(2))
    storage, catalog = FakeStorage(), FakeCatalog()
    return storage, catalog, SimpleNamespace(_file_storage=storage, get_file_info=storage.get_file_info)


async def authorized(environment, *, user="owner", **kwargs):
    _, catalog, service = environment
    return await probe.probe_visualization_file(service, catalog, "file", user, "tiff", **kwargs)


@pytest.mark.asyncio
async def test_authorized_probe_preserves_versions_and_opaque_identity(environment):
    result = await authorized(environment)
    assert result.plugin_id == "tiff" and result.revision == "a" * 64 and len(result.version) == 64
    assert result.traits == ["ome"] and probe._PROBE_SLOTS._value == 2
    assert "sample.tif" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["foreign", "private", "disabled", "version", "filename", "capability"])
async def test_auth_denials_never_read_file_bytes(environment, reason):
    from app.application.services.file_preview import PreviewVersionChanged
    from app.application.services.visualization_catalog import VisualizationDisabledError
    storage, catalog, _ = environment
    if reason == "private": storage.info.metadata = {"source": "tool_output_spill"}
    if reason == "disabled": catalog.enabled = False
    if reason == "filename": storage.info.filename = "secret.txt"
    if reason == "capability": catalog.plugin.capabilities = SimpleNamespace(operations=["bytes"])
    with pytest.raises((FileNotFoundError, VisualizationDisabledError, PreviewVersionChanged, probe.VisualizationProbeRejected)):
        await authorized(environment, user="other" if reason == "foreign" else "owner", **({"version": "b" * 64} if reason == "version" else {}))
    assert not storage.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["disabled", "revision", "version", "private"])
async def test_mutation_during_range_never_returns_probe(environment, mutation):
    from app.application.services.file_preview import PreviewVersionChanged
    from app.application.services.visualization_catalog import VisualizationDisabledError
    storage, catalog, _ = environment
    original = storage.download_file_range
    async def mutate(*args, **kwargs):
        response = await original(*args, **kwargs)
        if mutation == "disabled": catalog.enabled = False
        if mutation == "revision": catalog.revision = "b" * 64
        if mutation == "version": storage.info.metadata = {"sha256": "changed"}
        if mutation == "private": storage.info.metadata = {"source": "tool_output_spill"}
        return response
    storage.download_file_range = mutate
    with pytest.raises((FileNotFoundError, VisualizationDisabledError, PreviewVersionChanged)):
        await authorized(environment)
    assert len(storage.calls) == 1 and probe._PROBE_SLOTS._value == 2


@pytest.mark.asyncio
async def test_cancellation_holds_admission_until_native_range_finishes(environment):
    storage, _, _ = environment
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow(*_, **kwargs):
        started.set()
        await finish.wait()
        return storage.data[:kwargs["length"]], storage.info
    storage.download_file_range = slow
    task = asyncio.create_task(authorized(environment))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert probe._PROBE_SLOTS._value == 1
    finish.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert probe._PROBE_SLOTS._value == 2


@pytest.mark.asyncio
async def test_lower_manifest_budget_bounds_probe_before_read(environment):
    storage, catalog, _ = environment
    catalog.plugin.limits = SimpleNamespace(max_input_bytes=64)
    result = await authorized(environment)
    assert result.truncated and result.bytes_read == 0 and storage.calls == []
