import json
from types import SimpleNamespace

import pytest

import app.application.services.dataset_suggested_question_service as service_module
from app.application.services.dataset_suggested_question_service import (
    DEFAULT_SUGGESTED_QUESTION_CACHE_MAX_ENTRIES,
    DEFAULT_SUGGESTED_QUESTION_CACHE_TTL_SECONDS,
    FALLBACK_SUGGESTED_QUESTIONS,
    MAX_DATASET_PROMPT_CHARS,
    MAX_FILES_IN_PROMPT,
    MAX_SUGGESTED_QUESTION_CHARS,
    DatasetSuggestedQuestionCache,
    DatasetSuggestedQuestionService,
)
from app.domain.models.dataset import (
    DataCenterDataset,
    DatasetFile,
    DatasetLocation,
    DatasetStorageType,
)


VALID_QUESTIONS = [
    "这个数据集包含哪些文件？",
    "数据质量怎么样？",
    "数据有哪些趋势或关系？",
    "如何进行数据可视化？",
]


def make_dataset(*, files: list[DatasetFile] | None = None) -> DataCenterDataset:
    return DataCenterDataset(
        dataset_id="tds-private-id",
        external_id="external-private-id",
        data_center_id="private-center-id",
        data_center_name="国家科学数据中心",
        name="祁连山生态观测数据集",
        description="包含多年生态观测结果。",
        temporal_coverage="2011-2020",
        spatial_coverage="祁连山国家公园",
        data_type="GeoTIFF 栅格数据",
        tags=["生态", "遥感"],
        preview_url="https://private.example/preview/token",
        files=files or [DatasetFile(path="sources/location-a/annual.tif")],
        metadata={"private_note": "/metadata/secret/source.csv"},
        locations=[
            DatasetLocation(
                node_id="local-docker",
                storage_type=DatasetStorageType.HOST_PATH,
                source_path="/srv/private/tenant-a/annual.tif",
                mount_name="annual.tif",
                verified=True,
            )
        ],
        created_by="private-owner-id",
    )


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = None
        self.message_batches = []

    async def ainvoke(self, messages):
        self.messages = messages
        self.message_batches.append(messages)
        response = self.responses[min(len(self.message_batches) - 1, len(self.responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(content=response)


def install_fake_model(monkeypatch, *responses):
    model = FakeModel(*responses)
    settings = object()
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    calls = []

    def create_model(received_settings):
        calls.append(received_settings)
        return model

    monkeypatch.setattr(service_module, "create_chat_model", create_model)
    return model, settings, calls


def make_service(*, cache=None):
    return DatasetSuggestedQuestionService(
        cache=cache or DatasetSuggestedQuestionCache(),
    )


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.asyncio
async def test_generate_calls_configured_model_and_returns_four_questions(monkeypatch):
    model, settings, calls = install_fake_model(
        monkeypatch,
        json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False),
    )

    questions = await make_service().generate(make_dataset())

    assert questions == VALID_QUESTIONS
    assert calls == [settings]
    assert model.messages is not None
    assert len(model.messages) == 2
    system_prompt = model.messages[0].content
    assert "文件与数据概览" in system_prompt
    assert "数据质量或统计特征" in system_prompt
    assert "趋势或变量关系" in system_prompt
    assert "数据可视化" in system_prompt
    assert "尽量不超过 20 个中文字符" in system_prompt
    assert "最多 32 个字符" in system_prompt
    assert "这个数据集包含哪些文件？" in system_prompt
    assert "如何进行数据可视化？" in system_prompt


@pytest.mark.asyncio
async def test_prompt_uses_safe_metadata_and_filename_basenames_only(monkeypatch):
    model, _, _ = install_fake_model(
        monkeypatch,
        json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False),
    )
    dataset = make_dataset(files=[
        DatasetFile(path="/srv/private/tenant-a/annual.tif"),
        DatasetFile(path=r"C:\private\tenant-a\metadata.json"),
    ])

    await make_service().generate(dataset)

    combined_prompt = "\n".join(str(message.content) for message in model.messages)
    dataset_payload = json.loads(model.messages[1].content)
    assert dataset_payload["file_names"] == ["annual.tif", "metadata.json"]
    assert dataset_payload["dataset_name"] == dataset.name
    assert dataset_payload["summary"] == dataset.description
    assert "/srv/private" not in combined_prompt
    assert "C:\\private" not in combined_prompt
    assert "/metadata/secret" not in combined_prompt
    assert "private-owner-id" not in combined_prompt
    assert "private.example" not in combined_prompt
    assert "source_path" not in combined_prompt
    assert "locations" not in combined_prompt


@pytest.mark.asyncio
async def test_generate_reads_json_split_across_content_blocks(monkeypatch):
    raw = json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False)
    midpoint = len(raw) // 2
    install_fake_model(
        monkeypatch,
        [
            {"type": "text", "text": raw[:midpoint]},
            {"type": "reasoning", "reasoning": "not part of the answer"},
            {"type": "text", "text": {"value": raw[midpoint:]}},
        ],
    )

    questions = await make_service().generate(make_dataset())

    assert questions == VALID_QUESTIONS


@pytest.mark.asyncio
async def test_prompt_caps_metadata_size_and_file_count(monkeypatch):
    model, _, _ = install_fake_model(
        monkeypatch,
        json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False),
    )
    dataset = make_dataset(files=[
        DatasetFile(path=f"/private/root/file-{index}-{'x' * 300}.csv")
        for index in range(100)
    ])
    dataset.description = "超长摘要" * 5_000
    dataset.tags = [f"关键词-{index}-{'y' * 200}" for index in range(100)]

    await make_service().generate(dataset)

    human_prompt = model.messages[1].content
    payload = json.loads(human_prompt)
    assert len(human_prompt) <= MAX_DATASET_PROMPT_CHARS
    assert len(payload["file_names"]) <= MAX_FILES_IN_PROMPT
    assert all("/" not in name and "\\" not in name for name in payload["file_names"])


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "prefix ```json\n{\"questions\": []}\n```",
        "```json\n```json\n{\"questions\": []}\n```\n```",
        json.dumps({"questions": VALID_QUESTIONS[:3]}, ensure_ascii=False),
        json.dumps({"questions": [*VALID_QUESTIONS[:3], VALID_QUESTIONS[0]]}, ensure_ascii=False),
        json.dumps({"questions": [*VALID_QUESTIONS[:3], ""]}, ensure_ascii=False),
        json.dumps({"questions": [*VALID_QUESTIONS[:3], "What is in this dataset?"]}, ensure_ascii=False),
        json.dumps({"questions": [*VALID_QUESTIONS[:3], "这不是疑问句。"]}, ensure_ascii=False),
        json.dumps(
            {
                "questions": [
                    *VALID_QUESTIONS[:3],
                    "这个问题包含过多分析要求所以明显超过允许的三十二个字符并且还在继续堆砌更多不必要的分析要求？",
                ]
            },
            ensure_ascii=False,
        ),
        json.dumps({"questions": VALID_QUESTIONS, "extra": True}, ensure_ascii=False),
    ],
)
@pytest.mark.asyncio
async def test_invalid_model_output_uses_four_analysis_fallback_questions(monkeypatch, content):
    model, _, _ = install_fake_model(monkeypatch, content)

    questions = await make_service().generate(make_dataset())

    assert questions == list(FALLBACK_SUGGESTED_QUESTIONS)
    assert len(questions) == 4
    assert len(model.message_batches) == 2


def test_fallback_questions_are_exactly_four_distinct_schema_valid_questions():
    questions = list(FALLBACK_SUGGESTED_QUESTIONS)

    parsed = DatasetSuggestedQuestionService._parse_questions(
        json.dumps({"questions": questions}, ensure_ascii=False)
    )

    assert parsed == questions
    assert len(questions) == 4
    assert len(set(questions)) == 4
    assert all(len(question) <= MAX_SUGGESTED_QUESTION_CHARS for question in questions)
    assert questions == [
        "这个数据集包含哪些文件？",
        "数据质量怎么样？",
        "数据有哪些趋势或关系？",
        "如何进行数据可视化？",
    ]


@pytest.mark.asyncio
async def test_single_json_fence_is_accepted_without_retry(monkeypatch):
    raw = json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False)
    model, _, _ = install_fake_model(monkeypatch, f"```json\n{raw}\n```")

    questions = await make_service().generate(make_dataset())

    assert questions == VALID_QUESTIONS
    assert len(model.message_batches) == 1


@pytest.mark.asyncio
async def test_invalid_output_is_corrected_once_by_the_same_model_and_cached(monkeypatch):
    valid = json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False)
    model, settings, factory_calls = install_fake_model(monkeypatch, "not json", valid)
    cache = DatasetSuggestedQuestionCache()
    service = make_service(cache=cache)

    first = await service.generate(make_dataset())
    second = await DatasetSuggestedQuestionService(cache=cache).generate(make_dataset())

    assert first == VALID_QUESTIONS
    assert second == VALID_QUESTIONS
    assert factory_calls == [settings]
    assert len(model.message_batches) == 2
    assert len(model.message_batches[1]) == 4
    assert model.message_batches[1][-1].content.startswith("上一条输出未通过格式校验")
    assert await cache.size() == 1


@pytest.mark.asyncio
async def test_cache_hit_across_service_instances_does_not_call_model_again(monkeypatch):
    valid = json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False)
    model, settings, factory_calls = install_fake_model(monkeypatch, valid)
    cache = DatasetSuggestedQuestionCache()
    monkeypatch.setattr(service_module, "_suggested_question_cache", cache)

    first = await DatasetSuggestedQuestionService().generate(make_dataset())
    first.pop()
    second = await DatasetSuggestedQuestionService().generate(make_dataset())

    assert second == VALID_QUESTIONS
    assert factory_calls == [settings]
    assert len(model.message_batches) == 1


@pytest.mark.asyncio
async def test_cache_key_includes_dataset_id_and_safe_prompt_hash(monkeypatch):
    valid = json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False)
    model, _, _ = install_fake_model(monkeypatch, valid)
    cache = DatasetSuggestedQuestionCache()
    service = DatasetSuggestedQuestionService(cache=cache)
    dataset = make_dataset()

    await service.generate(dataset)
    dataset.description = "安全摘要发生变化。"
    await service.generate(dataset)
    second_dataset = dataset.model_copy(deep=True, update={"dataset_id": "tds-other-id"})
    await service.generate(second_dataset)

    assert len(model.message_batches) == 3
    assert await cache.size() == 3


@pytest.mark.asyncio
async def test_cache_ttl_expiry_calls_model_again(monkeypatch):
    valid = json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False)
    model, _, factory_calls = install_fake_model(monkeypatch, valid)
    clock = FakeClock()
    cache = DatasetSuggestedQuestionCache(ttl_seconds=3_600, clock=clock)
    service = DatasetSuggestedQuestionService(cache=cache)

    await service.generate(make_dataset())
    clock.advance(3_599)
    await service.generate(make_dataset())
    clock.advance(1)
    await service.generate(make_dataset())

    assert DEFAULT_SUGGESTED_QUESTION_CACHE_TTL_SECONDS == 3_600
    assert len(factory_calls) == 2
    assert len(model.message_batches) == 2


@pytest.mark.asyncio
async def test_cache_is_bounded_and_evicts_least_recently_used_entry(monkeypatch):
    valid = json.dumps({"questions": VALID_QUESTIONS}, ensure_ascii=False)
    model, _, _ = install_fake_model(monkeypatch, valid)
    cache = DatasetSuggestedQuestionCache(max_entries=2)
    service = DatasetSuggestedQuestionService(cache=cache)
    datasets = [
        make_dataset().model_copy(deep=True, update={"dataset_id": f"tds-{index}"})
        for index in range(3)
    ]

    await service.generate(datasets[0])
    await service.generate(datasets[1])
    await service.generate(datasets[0])
    await service.generate(datasets[2])
    await service.generate(datasets[1])

    assert DEFAULT_SUGGESTED_QUESTION_CACHE_MAX_ENTRIES == 256
    assert await cache.size() == 2
    assert len(model.message_batches) == 4


@pytest.mark.asyncio
async def test_model_failure_returns_fresh_fallback_without_caching(monkeypatch):
    class FailingModel:
        calls = 0

        async def ainvoke(self, _messages):
            self.calls += 1
            raise TimeoutError("provider timed out")

    model = FailingModel()
    monkeypatch.setattr(service_module, "get_settings", lambda: object())
    monkeypatch.setattr(service_module, "create_chat_model", lambda _settings: model)
    cache = DatasetSuggestedQuestionCache()
    service = make_service(cache=cache)

    first = await service.generate(make_dataset())
    first.pop()
    second = await service.generate(make_dataset())

    assert second == list(FALLBACK_SUGGESTED_QUESTIONS)
    assert len(second) == 4
    assert model.calls == 2
    assert await cache.size() == 0
