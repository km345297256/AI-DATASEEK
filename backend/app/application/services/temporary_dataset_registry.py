"""Process-local registry for short-lived dataset submissions.

The registry deliberately has no persistence adapter: entries live only in the
current backend process and are never written to MongoDB or Redis.  Expired
entries are discarded lazily during registry operations, which keeps the
component lifecycle-free while ensuring expired datasets are never returned.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.domain.models.dataset import DataCenterDataset


DEFAULT_DATASET_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_DATASETS = 128
DEFAULT_MAX_DATASETS_PER_OWNER = 16
_ID_GENERATION_ATTEMPTS = 32


@dataclass(frozen=True, slots=True)
class TemporaryDatasetEntry:
    """A registry snapshot that carries the owner needed for authorization."""

    dataset_id: str
    owner_id: str
    dataset: DataCenterDataset
    created_at_monotonic: float
    expires_at_monotonic: float


class TemporaryDatasetRegistry:
    """Async-safe, bounded, process-local storage for temporary datasets.

    ``get`` returns an entry including its owner so an application service can
    apply its own authorization policy.  Callers that only need strict owner
    isolation should use ``get_for_owner`` instead.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_DATASET_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_DATASETS,
        max_entries_per_owner: int = DEFAULT_MAX_DATASETS_PER_OWNER,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._ttl_seconds = self._validate_ttl(ttl_seconds)
        self._max_entries = self._validate_positive_integer(max_entries, "max_entries")
        self._max_entries_per_owner = self._validate_positive_integer(
            max_entries_per_owner,
            "max_entries_per_owner",
        )
        self._clock = clock
        self._id_factory = id_factory or self._new_dataset_id
        self._entries: OrderedDict[str, TemporaryDatasetEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def put(
        self,
        dataset: DataCenterDataset,
        owner_id: str,
        *,
        ttl_seconds: float | None = None,
    ) -> str:
        """Store a defensive copy and return a newly generated opaque ID."""

        if not isinstance(dataset, DataCenterDataset):
            raise TypeError("dataset must be a DataCenterDataset")
        normalized_owner_id = self._normalize_owner_id(owner_id)
        entry_ttl = self._ttl_seconds if ttl_seconds is None else self._validate_ttl(ttl_seconds)

        async with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            dataset_id = self._generate_unique_id_locked()

            while self._owner_entry_count_locked(normalized_owner_id) >= self._max_entries_per_owner:
                self._evict_oldest_owner_entry_locked(normalized_owner_id)

            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)

            stored_dataset = dataset.model_copy(deep=True, update={"dataset_id": dataset_id})
            self._entries[dataset_id] = TemporaryDatasetEntry(
                dataset_id=dataset_id,
                owner_id=normalized_owner_id,
                dataset=stored_dataset,
                created_at_monotonic=now,
                expires_at_monotonic=now + entry_ttl,
            )
            return dataset_id

    async def get(self, dataset_id: str) -> TemporaryDatasetEntry | None:
        """Return a defensive snapshot, including owner metadata, if unexpired."""

        normalized_dataset_id = self._normalize_dataset_id(dataset_id)
        async with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            entry = self._entries.get(normalized_dataset_id)
            return self._copy_entry(entry) if entry is not None else None

    async def get_for_owner(
        self,
        dataset_id: str,
        owner_id: str,
    ) -> DataCenterDataset | None:
        """Return an unexpired dataset only when it belongs to ``owner_id``."""

        normalized_dataset_id = self._normalize_dataset_id(dataset_id)
        normalized_owner_id = self._normalize_owner_id(owner_id)
        async with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            entry = self._entries.get(normalized_dataset_id)
            if entry is None or entry.owner_id != normalized_owner_id:
                return None
            return entry.dataset.model_copy(deep=True)

    async def prune_expired(self) -> int:
        """Remove expired entries and return the number removed."""

        async with self._lock:
            return self._prune_expired_locked(self._clock())

    async def size(self) -> int:
        """Return the number of unexpired entries currently held in memory."""

        async with self._lock:
            self._prune_expired_locked(self._clock())
            return len(self._entries)

    def _generate_unique_id_locked(self) -> str:
        for _ in range(_ID_GENERATION_ATTEMPTS):
            candidate = self._normalize_dataset_id(self._id_factory())
            if candidate not in self._entries:
                return candidate
        raise RuntimeError("failed to generate a unique temporary dataset ID")

    def _prune_expired_locked(self, now: float) -> int:
        expired_ids = [
            dataset_id
            for dataset_id, entry in self._entries.items()
            if entry.expires_at_monotonic <= now
        ]
        for dataset_id in expired_ids:
            del self._entries[dataset_id]
        return len(expired_ids)

    def _owner_entry_count_locked(self, owner_id: str) -> int:
        return sum(entry.owner_id == owner_id for entry in self._entries.values())

    def _evict_oldest_owner_entry_locked(self, owner_id: str) -> None:
        for dataset_id, entry in self._entries.items():
            if entry.owner_id == owner_id:
                del self._entries[dataset_id]
                return
        raise RuntimeError("owner entry count changed while holding the registry lock")

    @staticmethod
    def _copy_entry(entry: TemporaryDatasetEntry) -> TemporaryDatasetEntry:
        return replace(entry, dataset=entry.dataset.model_copy(deep=True))

    @staticmethod
    def _new_dataset_id() -> str:
        return f"tds_{secrets.token_urlsafe(18)}"

    @staticmethod
    def _normalize_dataset_id(dataset_id: str) -> str:
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id must be a non-empty string")
        return dataset_id.strip()

    @staticmethod
    def _normalize_owner_id(owner_id: str) -> str:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must be a non-empty string")
        return owner_id.strip()

    @staticmethod
    def _validate_ttl(ttl_seconds: float) -> float:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError("ttl_seconds must be a positive finite number")
        value = float(ttl_seconds)
        if value <= 0 or not math.isfinite(value):
            raise ValueError("ttl_seconds must be a positive finite number")
        return value

    @staticmethod
    def _validate_positive_integer(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value


_temporary_dataset_registry = TemporaryDatasetRegistry()


def get_temporary_dataset_registry() -> TemporaryDatasetRegistry:
    """Return the backend process's shared temporary dataset registry."""

    return _temporary_dataset_registry
