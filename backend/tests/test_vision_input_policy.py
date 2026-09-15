import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain.messages import AIMessage, HumanMessage
from PIL import Image

from app.core.config import ModelRequestCapability, Settings
from app.domain.models.file import FileInfo
from app.domain.models.memory import Memory
from app.domain.models.message import Message
from app.domain.services.agents import vision
from app.domain.services.agents import base as base_module
from app.domain.services.agents.vision import VisionAgent
from app.domain.services.context_budget import estimate_context_tokens
from app.domain.services.model_input_policy import (
    ImageInputError, deepseek_image_tokens, deepseek_request_dimensions,
    image_url, project_image, request_image_estimator, resolve_model_capability,
)
from app.domain.services import model_runtime
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def png(width=20, height=10):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def settings(monkeypatch):
    value = Settings(_env_file=None, api_key="fixture")
    monkeypatch.setattr(vision, "get_settings", lambda: value)
    monkeypatch.setattr(model_runtime, "get_settings", lambda: value)
    return value


def agent(storage, owner="owner"):
    result = object.__new__(VisionAgent)
    result._file_storage = storage
    result._user_id = owner
    result._model_provider = "openai"
    result._model_name = "fixture"
    result._image_sources = {}
    result._agent_id = "agent"
    result.memory = None
    return result


class Storage:
    def __init__(self, data=None):
        self.data = data or png()
        self.owners = []
        self.reads = []

    async def get_file_info(self, file_id, user_id):
        self.owners.append(user_id)
        if user_id != "owner":
            raise PermissionError("Denied")
        return FileInfo(file_id=file_id, filename="private.png", content_type="image/png", size=len(self.data))

    async def download_file_range(self, file_id, user_id, *, offset, length):
        info = await self.get_file_info(file_id, user_id)
        self.reads.append((offset, length))
        return self.data[offset:offset + length], info


class TurnStorage(Storage):
    def __init__(self):
        super().__init__()
        self.file_reads = []

    def payload(self, file_id):
        # Different dimensions keep each synthetic source independently
        # recognizable; no actual files or provider calls are used.
        return png(20 + int(file_id.rsplit("-", 1)[-1]), 10)

    async def get_file_info(self, file_id, user_id):
        if user_id != "owner":
            raise PermissionError("Denied")
        return FileInfo(file_id=file_id, filename="fixture.png", content_type="image/png", size=len(self.payload(file_id)))

    async def download_file_range(self, file_id, user_id, *, offset, length):
        info = await self.get_file_info(file_id, user_id)
        self.file_reads.append(file_id)
        return self.payload(file_id)[offset:offset + length], info


def conversation_agent(storage, repository, monkeypatch, requests, settings):
    value = agent(storage)
    value._repository = repository
    value.bind_tools = False
    value.dynamic_system_prompt_provider = None
    value.dynamic_user_context_provider = None
    value._record_token_usage = AsyncMock()
    value._llm_retry_attempts = 1
    model, runnable, chain = MagicMock(), MagicMock(), MagicMock()
    model.identity = SimpleNamespace(provider="openai", model_name="fixture")

    async def invoke(context):
        snapshot = [message.model_copy(deep=True) for message in context]
        request_image_estimator(snapshot, settings=settings, capability=ModelRequestCapability())
        assert all("dataseek_image_refs_v1" not in message.additional_kwargs for message in snapshot)
        assert "private-file-" not in "".join(message.model_dump_json() for message in snapshot)
        requests.append(snapshot)
        return AIMessage(content=f"visual-evidence-{len(requests)}")

    chain.ainvoke = invoke
    runnable.__or__.return_value = chain
    model.bind.return_value = runnable
    value._model = model
    monkeypatch.setattr(base_module.RobustJsonParser, "from_llm", lambda _model: object())
    return value


class MemoryRepository:
    def __init__(self):
        self.saved = Memory()

    async def get_memory(self, *_args):
        return self.saved.model_copy(deep=True)

    async def save_memory(self, *_args):
        self.saved = _args[-1].model_copy(deep=True)


def image_blocks(messages):
    return [block for message in messages if isinstance(message.content, list)
            for block in message.content if isinstance(block, dict) and block.get("type") == "image_url"]


async def image_turn(value, numbers):
    blocks = await value._build_storage_image_blocks(Message(
        message="analyze", attachment_file_ids=[f"private-file-{number}" for number in numbers],
    ))
    return await value.ask_with_messages([HumanMessage(content=[{"type": "text", "text": "analyze"}, *blocks])])


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_each_turn", [False, True])
async def test_twenty_single_image_turns_are_not_a_cumulative_quota(settings, monkeypatch, restart_each_turn):
    storage, repository, requests = TurnStorage(), MemoryRepository(), []
    value = conversation_agent(storage, repository, monkeypatch, requests, settings)
    for number in range(20):
        if restart_each_turn:
            value = conversation_agent(storage, repository, monkeypatch, requests, settings)
        answer = await image_turn(value, [number])
        assert answer.content == f"visual-evidence-{number + 1}"
        assert len(image_blocks(requests[-1])) == min(number + 1, settings.vision_max_images)
    assert len(requests) == 20
    assert "visual-evidence-1" in repository.saved.model_dump_json()
    assert "private-file-0" in repository.saved.model_dump_json()
    assert "not expanded" in "".join(message.model_dump_json() for message in requests[-1])
    assert "not expanded" not in repository.saved.model_dump_json()
    assert "base64" not in repository.saved.model_dump_json()


@pytest.mark.asyncio
async def test_two_full_image_batches_reserve_all_current_images_and_can_restore_old_next_turn(settings, monkeypatch):
    storage, repository, requests = TurnStorage(), MemoryRepository(), []
    value = conversation_agent(storage, repository, monkeypatch, requests, settings)
    await image_turn(value, range(16))
    storage.file_reads.clear()
    await image_turn(value, range(16, 32))
    assert len(image_blocks(requests[-1])) == 16
    assert storage.file_reads == [f"private-file-{index}" for index in range(16, 32)]
    saved = repository.saved.model_dump_json()
    assert "private-file-0" in saved and "private-file-31" in saved
    storage.file_reads.clear()
    await image_turn(value, [32])
    # The previous batch, omitted from request 2's history, is expandable
    # again now that request 3 reserves just one current image.
    assert storage.file_reads == ["private-file-32", *[f"private-file-{index}" for index in range(31, 16, -1)]]
    assert len(image_blocks(requests[-1])) == 16


@pytest.mark.asyncio
async def test_history_bytes_overflow_omits_old_pixels_without_scanning_all_files(settings, monkeypatch):
    storage, repository, requests = TurnStorage(), MemoryRepository(), []
    value = conversation_agent(storage, repository, monkeypatch, requests, settings)
    await image_turn(value, range(10))
    current = await value._build_storage_image_blocks(Message(message="current", attachment_file_ids=["private-file-10"]))
    size = len(image_url(current[0]).encode())
    # Leave less than one projected history image's bytes.
    current = current * 7
    settings.vision_max_request_bytes = max(1024, size * 7 + 1)
    storage.file_reads.clear()
    await value.ask_with_messages([HumanMessage(content=current)])
    assert len(image_blocks(requests[-1])) == 7
    assert storage.file_reads == ["private-file-9"]
    assert "private-file-0" in repository.saved.model_dump_json()


@pytest.mark.asyncio
async def test_current_images_over_limit_still_fail_before_model_or_history_reads(settings, monkeypatch):
    storage, repository, requests = TurnStorage(), MemoryRepository(), []
    value = conversation_agent(storage, repository, monkeypatch, requests, settings)
    block = await value._storage_image_block("private-file-0")
    storage.file_reads.clear()
    with pytest.raises(ImageInputError, match="Too many"):
        await value.ask_with_messages([HumanMessage(content=[block] * 17)])
    assert not requests and not storage.file_reads


@pytest.mark.asyncio
async def test_cancelled_history_projection_leaves_original_references_untouched(settings):
    storage = TurnStorage()
    value = agent(storage)
    block = await value._storage_image_block("private-file-0")
    value.memory = Memory(messages=[HumanMessage(content=[block])])
    repository = MemoryRepository()
    value._repository = repository
    await value._persist_memory()
    value.memory = None
    await value._ensure_memory()
    before = value.memory.model_dump_json()
    sources_before = dict(value._image_sources)
    entered = asyncio.Event()

    async def wait_for_read(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    storage.download_file_range = wait_for_read
    projecting = asyncio.create_task(value._project_history_for_images([]))
    await entered.wait()
    projecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await projecting
    assert value.memory.model_dump_json() == before
    assert repository.saved.model_dump_json() == before
    assert value._image_sources == sources_before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [PermissionError("Denied"), FileNotFoundError("Gone"), ImageInputError("Invalid old image"), None])
async def test_old_revoked_or_deleted_image_does_not_block_current_input(settings, monkeypatch, failure):
    storage, repository, requests = TurnStorage(), MemoryRepository(), []
    value = conversation_agent(storage, repository, monkeypatch, requests, settings)
    await image_turn(value, [0])
    original_info = storage.get_file_info

    async def unavailable_old(file_id, user_id):
        if file_id == "private-file-0":
            if failure is not None:
                raise failure
            return None
        return await original_info(file_id, user_id)

    storage.get_file_info = unavailable_old
    await image_turn(value, [1])
    assert len(image_blocks(requests[-1])) == 1
    assert "unavailable under" in "".join(message.model_dump_json() for message in requests[-1])
    assert "private-file-0" in repository.saved.model_dump_json()
    with pytest.raises(ImageInputError):
        await image_turn(value, [0])
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_persistence_merges_old_refs_and_new_inline_sources(settings):
    value = agent(TurnStorage())
    block = await value._storage_image_block("private-file-1")
    value.memory = Memory(messages=[HumanMessage(
        content=[dict(vision._IMAGE_REFERENCE_PLACEHOLDER), block],
        additional_kwargs={"dataseek_image_refs_v1": [{"index": 0, "file_id": "private-file-0"}]},
    )])
    repository = MemoryRepository()
    value._repository = repository
    await value._persist_memory()
    assert repository.saved.messages[0].additional_kwargs["dataseek_image_refs_v1"] == [
        {"index": 0, "file_id": "private-file-0"}, {"index": 1, "file_id": "private-file-1"},
    ]


@pytest.mark.asyncio
async def test_history_model_projection_records_only_private_hmac_audit(settings, monkeypatch):
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    storage, repository, requests = TurnStorage(), MemoryRepository(), []
    value = conversation_agent(storage, repository, monkeypatch, requests, settings)
    await image_turn(value, range(16))
    records = []

    class Store:
        async def put(self, record):
            records.append(record.model_copy(deep=True))

    with model_runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=Store()):
        await image_turn(value, [16])
    changes = [record for record in records if record.memory_change is not None]
    assert changes
    assert any(record.memory_change.reason == "durable_projection" for record in changes)
    assert any(record.memory_change.before_hmac != record.memory_change.after_hmac for record in changes)
    serialized = "".join(record.model_dump_json() for record in records)
    assert "private-file-" not in serialized and "base64" not in serialized


def test_exact_capability_mapping_never_guesses_model_or_increases_ceiling(settings):
    settings.model_request_capabilities = {"openai": {"verified": ModelRequestCapability(context_tokens=8192)}}
    assert resolve_model_capability(settings, "openai", "verified").context_tokens == 8192
    assert resolve_model_capability(settings, "openai", "verified-huge").context_tokens is None
    assert resolve_model_capability(settings, "deepseek", "verified").context_tokens is None


def test_projection_preserves_original_and_bounds_sent_dimensions(settings):
    original = png(3000, 1500)
    projected = project_image(original, settings=settings, capability=ModelRequestCapability())
    assert (projected.width, projected.height) == (2048, 1024)
    assert Image.open(io.BytesIO(original)).size == (3000, 1500)
    assert Image.open(io.BytesIO(projected.data)).size == (2048, 1024)
    assert projected.mime_type == "image/png"


def test_model_copy_strips_private_exif_but_original_remains_untouched(settings):
    output = io.BytesIO()
    exif = Image.Exif()
    exif[315] = "Private owner metadata"
    Image.new("RGB", (10, 10)).save(output, format="PNG", exif=exif)
    raw = output.getvalue()
    projected = project_image(raw, settings=settings, capability=ModelRequestCapability())
    assert Image.open(io.BytesIO(raw)).getexif()[315] == "Private owner metadata"
    assert not Image.open(io.BytesIO(projected.data)).getexif()


def test_bad_oversize_or_multipage_images_fail_explicitly(settings):
    for raw in [b"not-an-image", b""]:
        with pytest.raises(ImageInputError):
            project_image(raw, settings=settings, capability=ModelRequestCapability())
    settings.vision_max_source_pixels = 1024
    with pytest.raises(ImageInputError, match="pixel"):
        project_image(png(50, 50), settings=settings, capability=ModelRequestCapability())
    frames = io.BytesIO()
    Image.new("RGB", (5, 5), "red").save(frames, format="TIFF", save_all=True, append_images=[Image.new("RGB", (5, 5), "blue")])
    with pytest.raises(ImageInputError, match="Multi-frame"):
        project_image(frames.getvalue(), settings=settings, capability=ModelRequestCapability())


@pytest.mark.parametrize("dimensions", [(1, 1), (544, 544), (1024, 1024), (4000, 1000), (1, 100000), (100000, 1)])
def test_deepseek_grid_projection_is_bounded_and_never_enlarges(dimensions):
    width, height = deepseek_request_dimensions(*dimensions)
    assert 0 < width <= dimensions[0] and 0 < height <= dimensions[1]
    assert 0 < deepseek_image_tokens(width, height) <= 1024


def test_deepseek_known_grid_values():
    assert deepseek_image_tokens(544, 544) == 184
    assert deepseek_image_tokens(1024, 1024) == 652


def test_estimator_uses_sent_geometry_only_when_explicitly_enabled(settings):
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png(1024, 1024)).decode()}}
    messages = [HumanMessage(content=[block])]
    fallback = request_image_estimator(messages, settings=settings, capability=ModelRequestCapability(image_tokens=5000))
    precise = request_image_estimator(messages, settings=settings, capability=ModelRequestCapability(image_token_strategy="deepseek_v41"))
    assert fallback(block) == 5000
    assert precise(block) == 652
    assert estimate_context_tokens(messages, image_estimator=fallback)[0] - estimate_context_tokens(messages, image_estimator=precise)[0] == 5000 - 652
    settings.vision_max_request_bytes = 1024
    with pytest.raises(ImageInputError, match="size"):
        request_image_estimator(messages, settings=settings, capability=ModelRequestCapability())


@pytest.mark.asyncio
async def test_storage_size_rejected_before_download_and_owner_required(settings):
    storage = Storage(b"x" * 2048)
    settings.vision_max_source_bytes = 1024
    value = agent(storage)
    with pytest.raises(ImageInputError, match="source size"):
        await value._build_storage_image_blocks(Message(message="look", attachment_file_ids=["private-file"]))
    assert storage.reads == []
    with pytest.raises(ImageInputError, match="owner"):
        await agent(storage, owner=None)._build_storage_image_blocks(Message(message="look", attachment_file_ids=["private-file"]))


@pytest.mark.asyncio
async def test_vision_projection_uses_resolved_driver_identity(settings):
    settings.model_request_capabilities = {"deepseek": {"verified": ModelRequestCapability(vision=False)}}
    value = agent(Storage())
    value._model = SimpleNamespace(identity=SimpleNamespace(provider="deepseek", model_name="verified"))
    with pytest.raises(ImageInputError, match="does not support"):
        await value._project_image_block(png())


@pytest.mark.asyncio
async def test_failed_storage_access_does_not_fallback_to_sandbox(settings):
    value = agent(Storage(), owner="other-owner")
    sandbox = SimpleNamespace(file_download=AsyncMock())
    with pytest.raises(ImageInputError, match="access"):
        await value._build_image_blocks(Message(message="look", attachment_file_ids=["private-file"], attachments=["/home/ubuntu/private.png"]), sandbox)
    sandbox.file_download.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_does_not_silently_drop_extra_images(settings):
    settings.vision_max_images = 1
    storage = Storage()
    with pytest.raises(ImageInputError, match="Too many"):
        await agent(storage)._build_storage_image_blocks(Message(message="look", attachment_file_ids=["a", "b"]))
    assert len(storage.reads) == 1


@pytest.mark.asyncio
async def test_private_image_reference_persists_without_base64_and_rechecks_owner(settings):
    storage = Storage()
    value = agent(storage)
    blocks = await value._build_storage_image_blocks(Message(message="look", attachment_file_ids=["private-id"]))
    value.memory = Memory(messages=[HumanMessage(content=[{"type": "text", "text": "look"}, *blocks])])
    saved = []
    async def save(*args):
        saved.append(args[-1])
    value._repository = SimpleNamespace(save_memory=save)
    await value._persist_memory()
    assert "base64" not in saved[0].model_dump_json()
    assert "private-id" in saved[0].model_dump_json()
    assert image_url(value.memory.messages[0].content[1]).startswith("data:image/")
    restored = agent(storage)
    restored._repository = SimpleNamespace(get_memory=AsyncMock(return_value=saved[0]))
    await restored._ensure_memory()
    projected = await restored._project_history_for_images([])
    assert projected.messages[0].content == value.memory.messages[0].content
    assert "dataseek_image_refs_v1" not in projected.messages[0].additional_kwargs
    assert storage.owners and set(storage.owners) == {"owner"}
    forbidden = agent(storage, owner="different-owner")
    forbidden._repository = restored._repository
    await forbidden._ensure_memory()
    inaccessible = await forbidden._project_history_for_images([])
    assert inaccessible.messages[0].content[1] == vision._HISTORICAL_IMAGE_UNAVAILABLE
    assert forbidden.memory.messages[0].content[1] == vision._IMAGE_REFERENCE_PLACEHOLDER


@pytest.mark.asyncio
async def test_large_text_and_images_keep_recoverable_slots_and_no_base64_keys(settings):
    storage = Storage()
    value = agent(storage)
    block = await value._storage_image_block("private-file")
    text = "这是很长的文本 \"\\\n" * 20000
    value.memory = Memory(messages=[HumanMessage(content=[{"type": "text", "text": text}, block])])
    value._image_sources["unused-old-digest"] = "old-file"
    repository = SimpleNamespace(save_memory=AsyncMock())
    value._repository = repository
    await value._persist_memory()
    saved = repository.save_memory.call_args.args[-1]
    assert isinstance(saved.messages[0].content, list)
    assert saved.messages[0].additional_kwargs["dataseek_image_refs_v1"][0]["index"] == 1
    assert len(value._image_sources) == 1
    assert all(len(key) == 64 for key in value._image_sources)
    assert "base64" not in saved.model_dump_json()
    assert value.memory.messages[0].content[0]["text"] == text
    restored = agent(storage)
    restored._repository = SimpleNamespace(get_memory=AsyncMock(return_value=saved))
    await restored._ensure_memory()
    projected = await restored._project_history_for_images([])
    assert image_url(projected.messages[0].content[1]) == image_url(block)


def test_native_base64_blocks_count_toward_request_bytes(settings):
    settings.vision_max_request_bytes = 1024
    with pytest.raises(ImageInputError):
        request_image_estimator([HumanMessage(content=[{"type": "image", "base64": "A" * 2048}])],
            settings=settings, capability=ModelRequestCapability())


@pytest.mark.asyncio
async def test_runtime_image_rejection_is_audited_without_public_schema_change(settings, monkeypatch):
    settings.model_request_capabilities = {"openai": {"text-only": ModelRequestCapability(vision=False)}}
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    records = []
    class Store:
        async def put(self, record):
            records.append(record.model_copy(deep=True))
    invoke = AsyncMock()
    with model_runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=Store()):
        with pytest.raises(ImageInputError, match="does not support"):
            await model_runtime.invoke_model_request(messages=[HumanMessage(content=[{"type": "image_url", "image_url": {"url": "https://private.invalid/image"}}])],
                max_output_tokens=100, provider="openai", model_name="text-only", invoke=invoke)
    invoke.assert_not_awaited()
    assert records[-1].error_code == "context_budget_exceeded"
    assert "private.invalid" not in records[-1].public_view().model_dump_json()


@pytest.mark.asyncio
async def test_runtime_resolves_selected_model_capacity_and_output_before_invoking(settings):
    settings.model_request_capabilities = {"openai": {"verified": ModelRequestCapability(context_tokens=8192, max_output_tokens=80)}}
    calls = []
    async def invoke(messages, output):
        calls.append(output)
        return AIMessage(content="done")
    await model_runtime.invoke_model_request(messages=[HumanMessage(content="hello")], max_output_tokens=100,
        provider="openai", model_name="verified", invoke=invoke)
    assert calls == [80]
    with pytest.raises(Exception) as error:
        await model_runtime.invoke_model_request(messages=[HumanMessage(content="x" * 30000)], max_output_tokens=100,
            provider="openai", model_name="verified", invoke=invoke)
    assert getattr(error.value, "code", None) == "context_budget_exceeded"
    assert calls == [80]


class Stream(httpx.AsyncByteStream):
    def __init__(self, *, cancel=False):
        self.closed = False
        self.cancel = cancel
    async def __aiter__(self):
        yield b"1234"
        if self.cancel:
            raise asyncio.CancelledError()
        yield b"5678"
    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,cancel,exception", [(8, False, None), (5, False, ValueError), (8, True, asyncio.CancelledError)])
async def test_sandbox_stream_limit_and_cancellation_close_response(limit, cancel, exception):
    stream = Stream(cancel=cancel)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
    sandbox = object.__new__(DockerSandbox)
    sandbox.client = client
    sandbox.base_url = "http://sandbox"
    try:
        if exception:
            with pytest.raises(exception):
                await sandbox.file_download("/home/ubuntu/image.png", max_bytes=limit)
        else:
            assert (await sandbox.file_download("/home/ubuntu/image.png", max_bytes=limit)).read() == b"12345678"
        assert stream.closed
    finally:
        await client.aclose()
