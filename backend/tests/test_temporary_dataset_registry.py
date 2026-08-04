import asyncio

import pytest

from app.application.services.temporary_dataset_registry import (
    DEFAULT_DATASET_TTL_SECONDS,
    DEFAULT_MAX_DATASETS,
    DEFAULT_MAX_DATASETS_PER_OWNER,
    TemporaryDatasetRegistry,
)
from app.domain.models.dataset import DataCenterDataset


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_dataset(name: str = "Temporary dataset") -> DataCenterDataset:
    return DataCenterDataset(
        dataset_id="caller-controlled-id",
        data_center_id="dataset-chat-demo",
        data_center_name="Test datasets",
        name=name,
        description="Only held in backend process memory.",
        tags=["science"],
    )


@pytest.mark.asyncio
async def test_put_generates_opaque_id_and_stores_defensive_copies():
    registry = TemporaryDatasetRegistry()
    source = make_dataset()

    dataset_id = await registry.put(source, " owner-a ")
    source.name = "changed after put"
    source.tags.append("changed")

    entry = await registry.get(dataset_id)
    assert entry is not None
    assert dataset_id.startswith("tds_")
    assert dataset_id != "caller-controlled-id"
    assert entry.dataset_id == dataset_id
    assert entry.dataset.dataset_id == dataset_id
    assert entry.owner_id == "owner-a"
    assert entry.dataset.name == "Temporary dataset"
    assert entry.dataset.tags == ["science"]

    entry.dataset.name = "changed after get"
    second_entry = await registry.get(dataset_id)
    assert second_entry is not None
    assert second_entry.dataset.name == "Temporary dataset"


@pytest.mark.asyncio
async def test_owner_scoped_lookup_does_not_expose_another_owners_dataset():
    registry = TemporaryDatasetRegistry()
    dataset_id = await registry.put(make_dataset(), "owner-a")

    owned = await registry.get_for_owner(dataset_id, "owner-a")
    not_owned = await registry.get_for_owner(dataset_id, "owner-b")

    assert owned is not None
    assert owned.dataset_id == dataset_id
    assert not_owned is None


@pytest.mark.asyncio
async def test_entries_expire_at_the_ttl_boundary():
    clock = FakeClock()
    registry = TemporaryDatasetRegistry(clock=clock)
    dataset_id = await registry.put(make_dataset(), "owner-a")

    clock.advance(DEFAULT_DATASET_TTL_SECONDS - 1)
    assert await registry.get(dataset_id) is not None

    clock.advance(1)
    assert await registry.get(dataset_id) is None
    assert await registry.size() == 0


@pytest.mark.asyncio
async def test_put_can_override_default_ttl():
    clock = FakeClock()
    registry = TemporaryDatasetRegistry(ttl_seconds=100, clock=clock)
    dataset_id = await registry.put(make_dataset(), "owner-a", ttl_seconds=5)

    clock.advance(5)

    assert await registry.get(dataset_id) is None


@pytest.mark.asyncio
async def test_capacity_evicts_oldest_entry_after_pruning_expired_entries():
    clock = FakeClock()
    ids = iter(["tds_first", "tds_second", "tds_third"])
    registry = TemporaryDatasetRegistry(
        max_entries=2,
        clock=clock,
        id_factory=lambda: next(ids),
    )

    first_id = await registry.put(make_dataset("first"), "owner-a")
    clock.advance(1)
    second_id = await registry.put(make_dataset("second"), "owner-a")
    clock.advance(1)
    third_id = await registry.put(make_dataset("third"), "owner-a")

    assert await registry.get(first_id) is None
    assert await registry.get(second_id) is not None
    assert await registry.get(third_id) is not None
    assert await registry.size() == 2


@pytest.mark.asyncio
async def test_default_owner_quota_keeps_only_that_owners_sixteen_newest_entries():
    registry = TemporaryDatasetRegistry()

    dataset_ids = [
        await registry.put(make_dataset(f"dataset-{index}"), "owner-a")
        for index in range(DEFAULT_MAX_DATASETS_PER_OWNER + 1)
    ]

    assert DEFAULT_MAX_DATASETS == 128
    assert DEFAULT_MAX_DATASETS_PER_OWNER == 16
    assert await registry.get(dataset_ids[0]) is None
    assert all([await registry.get(dataset_id) is not None for dataset_id in dataset_ids[1:]])
    assert await registry.size() == DEFAULT_MAX_DATASETS_PER_OWNER


@pytest.mark.asyncio
async def test_owner_quota_never_evicts_another_owners_older_entry():
    ids = iter(["tds_b_oldest", "tds_a_first", "tds_a_second", "tds_a_third"])
    registry = TemporaryDatasetRegistry(
        max_entries=4,
        max_entries_per_owner=2,
        id_factory=lambda: next(ids),
    )

    owner_b_id = await registry.put(make_dataset("owner-b"), "owner-b")
    owner_a_first = await registry.put(make_dataset("a-first"), "owner-a")
    owner_a_second = await registry.put(make_dataset("a-second"), "owner-a")
    owner_a_third = await registry.put(make_dataset("a-third"), "owner-a")

    assert await registry.get_for_owner(owner_b_id, "owner-b") is not None
    assert await registry.get(owner_a_first) is None
    assert await registry.get_for_owner(owner_a_second, "owner-a") is not None
    assert await registry.get_for_owner(owner_a_third, "owner-a") is not None
    assert await registry.size() == 3


@pytest.mark.asyncio
async def test_concurrent_puts_are_unique_and_bounded():
    registry = TemporaryDatasetRegistry(max_entries=40, max_entries_per_owner=40)

    dataset_ids = await asyncio.gather(*(
        registry.put(make_dataset(f"dataset-{index}"), "owner-a")
        for index in range(40)
    ))
    datasets = await asyncio.gather(*(
        registry.get_for_owner(dataset_id, "owner-a")
        for dataset_id in dataset_ids
    ))

    assert len(set(dataset_ids)) == 40
    assert all(dataset is not None for dataset in datasets)
    assert await registry.size() == 40


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ttl_seconds": 0}, "ttl_seconds"),
        ({"ttl_seconds": float("inf")}, "ttl_seconds"),
        ({"max_entries": 0}, "max_entries"),
        ({"max_entries_per_owner": 0}, "max_entries_per_owner"),
    ],
)
def test_registry_rejects_invalid_limits(kwargs, message):
    with pytest.raises(ValueError, match=message):
        TemporaryDatasetRegistry(**kwargs)


@pytest.mark.asyncio
async def test_registry_rejects_blank_owner_and_non_positive_entry_ttl():
    registry = TemporaryDatasetRegistry()

    with pytest.raises(ValueError, match="owner_id"):
        await registry.put(make_dataset(), "  ")
    with pytest.raises(ValueError, match="ttl_seconds"):
        await registry.put(make_dataset(), "owner-a", ttl_seconds=-1)
