"""Original-request metering and replay prevention, not account or task quotas.

Production has no cumulative tool/model/token limit or elapsed-time deadline.
Each physical reservation occupies its own private, unique record, so long
analyses cannot fill a single Mongo document. Inputs before AgentTaskRunner
(e.g. front-controller classification) are outside this metering lineage.
Finite policies are explicit internal test fixtures, never deployment settings.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from typing import Awaitable, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from app.core.config import get_settings
from app.domain.repositories.analysis_budget_repository import AnalysisBudgetRepository

HEX64 = r"^[0-9a-f]{64}$"
HEX32 = r"^[0-9a-f]{32}$"
MAX_CAS_ATTEMPTS = 16


class BudgetUnavailableError(RuntimeError):
    """Stable failure: a reservation must not execute without its durable audit."""


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    initial_batches: StrictInt | None = Field(default=12, ge=1, le=64)
    increment_batches: StrictInt = Field(default=2, ge=1, le=16)
    max_grants: StrictInt = Field(default=2, ge=0, le=8)
    hard_batches: StrictInt | None = Field(default=16, ge=1, le=64)
    model_token_limit: StrictInt | None = Field(default=1_000_000, ge=1, le=100_000_000)
    model_call_limit: StrictInt | None = Field(default=128, ge=1, le=1024)
    grant_min_model_calls: StrictInt = Field(default=1, ge=1, le=16)
    grant_min_model_tokens: StrictInt = Field(default=4096, ge=1, le=1_000_000)
    deadline_seconds: StrictInt | None = Field(default=900, ge=1, le=900)
    grant_min_remaining_seconds: StrictInt = Field(default=30, ge=1, le=300)

    @model_validator(mode="after")
    def coherent(self):
        limits = (self.initial_batches, self.hard_batches, self.model_token_limit,
                  self.model_call_limit, self.deadline_seconds)
        if any(value is None for value in limits) and not all(value is None for value in limits):
            raise ValueError("Use either an unlimited policy or an explicit finite test policy")
        if self.initial_batches is not None and self.initial_batches > self.hard_batches:
            raise ValueError("Initial analysis allowance exceeds hard limit")
        return self

    @property
    def unlimited(self) -> bool:
        return self.initial_batches is None

    @classmethod
    def from_settings(cls, settings):
        # Do not read retired environment/configuration values: a stale .env
        # must not silently reinstate the removed production task quotas.
        return cls(initial_batches=None, hard_batches=None, model_token_limit=None,
                   model_call_limit=None, deadline_seconds=None, max_grants=0)


class BudgetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_digest: str = Field(pattern=HEX64)
    confirmed_progress_units: StrictInt = Field(ge=0, le=1_000_000)
    progress_digest: str = Field(pattern=HEX64)
    has_unknown_execution: StrictBool = False
    no_progress_loop: StrictBool = False
    next_action_bounded: StrictBool = False
    estimated_next_batches: StrictInt = Field(default=1, ge=1, le=64)


@dataclass(frozen=True)
class BudgetSnapshot:
    lineage_id: str
    tool_batches_used: int
    soft_limit: int | None
    hard_limit: int | None
    grant_count: int
    model_calls: int
    charged_tokens: int
    model_call_limit: int | None
    model_token_limit: int | None
    deadline_at: datetime | None


@dataclass(frozen=True)
class BudgetAdmission(BudgetSnapshot):
    allowed: bool
    reason: str
    reservation_id: str
    granted_batches: int = 0


def _snapshot(doc: dict) -> BudgetSnapshot:
    return BudgetSnapshot(doc["_id"], doc["tool_batches_used"], doc["soft_limit"],
                          doc["policy"]["hard_batches"], doc["grant_count"], doc["model_calls"],
                          doc["charged_tokens"], doc["policy"]["model_call_limit"],
                          doc["policy"]["model_token_limit"],
                          _aware(doc["deadline_at"]) if doc["deadline_at"] is not None else None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _admission(doc: dict, reservation_id: str, *, allowed: bool, reason: str, granted=0):
    return BudgetAdmission(**_snapshot(doc).__dict__, allowed=allowed, reason=reason,
                           reservation_id=reservation_id, granted_batches=granted)


def _identifier(value: str, *, maximum=256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid private budget identity")
    return value


def _reservation_id(value: str | None) -> str:
    value = value or uuid4().hex
    if not isinstance(value, str) or not re.fullmatch(HEX32, value):
        raise ValueError("Invalid budget reservation identity")
    return value


class AnalysisBudgetService:
    def __init__(self, repository: AnalysisBudgetRepository | None = None, *,
                 policy: BudgetPolicy | None = None, store_timeout_seconds: float | None = None):
        if repository is None:
            from app.infrastructure.repositories.mongo_analysis_budget_repository import MongoAnalysisBudgetRepository
            repository = MongoAnalysisBudgetRepository()
        settings = get_settings()
        self.repository = repository
        self.policy = policy or BudgetPolicy.from_settings(settings)
        self.store_timeout_seconds = (settings.model_trace_store_timeout_seconds
                                      if store_timeout_seconds is None else store_timeout_seconds)
        if not 0 < self.store_timeout_seconds <= 30:
            raise ValueError("Invalid budget store timeout")

    async def _store(self, awaitable):
        try:
            async with asyncio.timeout(self.store_timeout_seconds):
                return await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            raise BudgetUnavailableError("analysis_budget_store_unavailable") from None

    def _now(self):
        return datetime.now(UTC)

    async def open(self, *, user_id: str, session_id: str, origin_input_id: str,
                   scope_digest: str, lineage_id: str | None = None,
                   require_live: Callable[[], Awaitable[None]] | None = None) -> AnalysisBudgetHandle:
        _identifier(user_id)
        _identifier(session_id)
        _identifier(origin_input_id)
        if not isinstance(scope_digest, str) or not re.fullmatch(HEX64, scope_digest):
            raise ValueError("Invalid analysis scope identity")
        if require_live is not None:
            await require_live()
        identity = hashlib.sha256(json.dumps(["analysis-budget/v1", user_id, session_id, origin_input_id],
                                              separators=(",", ":")).encode()).hexdigest()[:32]
        if lineage_id is not None:
            if not isinstance(lineage_id, str) or not re.fullmatch(HEX32, lineage_id):
                raise ValueError("Invalid analysis lineage identity")
            doc = await self._store(self.repository.get(lineage_id))
        else:
            now = self._now()
            document = {
                "_id": identity, "schema_version": 1, "version": 0, "owner_id": user_id,
                "session_id": session_id, "origin_input_id": origin_input_id,
                "scope_digest": scope_digest, "policy": self.policy.model_dump(),
                "tool_batches_used": 0, "soft_limit": self.policy.initial_batches, "grant_count": 0,
                "model_calls": 0, "charged_tokens": 0, "last_grant_progress_units": 0,
                "last_grant_progress_digest": None, "tool_reservations": {}, "model_reservations": {},
                "grants": [], "denials": [], "created_at": now, "updated_at": now,
                "deadline_at": (now + timedelta(seconds=self.policy.deadline_seconds)
                                if self.policy.deadline_seconds is not None else None),
            }
            if self.policy.unlimited:
                # Only immutable lineage identity is stored here. Per-call
                # uniqueness and settlement live in independent records.
                document = {key: document[key] for key in (
                    "_id", "owner_id", "session_id", "origin_input_id", "scope_digest",
                    "policy", "created_at", "updated_at")}
                document["schema_version"] = 2
            doc = await self._store(self.repository.create_if_absent(document))
        handle = AnalysisBudgetHandle(self, lineage_id or identity, user_id, session_id, scope_digest, require_live)
        handle._validate(doc)
        return handle


class AnalysisBudgetHandle:
    def __init__(self, service, lineage_id, user_id, session_id, scope_digest, require_live):
        self.service, self.lineage_id = service, lineage_id
        self.user_id, self.session_id, self.scope_digest = user_id, session_id, scope_digest
        self.require_live = require_live
        self.policy = service.policy

    def _validate(self, doc):
        if (not isinstance(doc, dict) or doc.get("schema_version") != (2 if self.policy.unlimited else 1) or doc.get("_id") != self.lineage_id
                or doc.get("owner_id") != self.user_id or doc.get("session_id") != self.session_id
                or doc.get("scope_digest") != self.scope_digest or doc.get("policy") != self.policy.model_dump()):
            raise BudgetUnavailableError("analysis_budget_scope_or_policy_changed")
        if self.policy.unlimited:
            if not isinstance(doc.get("created_at"), datetime):
                raise BudgetUnavailableError("analysis_budget_invalid")
            return
        if (not isinstance(doc.get("created_at"), datetime) or not isinstance(doc.get("deadline_at"), datetime)
                or _aware(doc["deadline_at"]) != _aware(doc["created_at"]) + timedelta(seconds=self.policy.deadline_seconds)):
            raise BudgetUnavailableError("analysis_budget_invalid_deadline")
        for key in ("version", "tool_batches_used", "soft_limit", "grant_count", "model_calls", "charged_tokens",
                    "last_grant_progress_units"):
            if type(doc.get(key)) is not int or doc[key] < 0:
                raise BudgetUnavailableError("analysis_budget_invalid")
        if (doc["soft_limit"] > self.policy.hard_batches or doc["soft_limit"] < self.policy.initial_batches
                or doc["tool_batches_used"] > doc["soft_limit"] or doc["grant_count"] > self.policy.max_grants
                or doc["model_calls"] > self.policy.model_call_limit):
            raise BudgetUnavailableError("analysis_budget_invalid")
        if (not isinstance(doc.get("tool_reservations"), dict) or not isinstance(doc.get("model_reservations"), dict)
                or len(doc["tool_reservations"]) != doc["tool_batches_used"]
                or len(doc["model_reservations"]) != doc["model_calls"]
                or not isinstance(doc.get("grants"), list) or len(doc["grants"]) != doc["grant_count"]
                or not isinstance(doc.get("denials"), list) or len(doc["denials"]) > 32):
            raise BudgetUnavailableError("analysis_budget_invalid")
        charged = 0
        for reservation_id, reservation in doc["model_reservations"].items():
            if (not isinstance(reservation_id, str) or not re.fullmatch(HEX32, reservation_id)
                    or not isinstance(reservation, dict) or type(reservation.get("reserved_tokens")) is not int
                    or reservation["reserved_tokens"] < 1
                    or (reservation.get("actual_tokens") is not None and
                        (type(reservation["actual_tokens"]) is not int or reservation["actual_tokens"] < 0))):
                raise BudgetUnavailableError("analysis_budget_invalid")
            charged += reservation["actual_tokens"] if reservation.get("actual_tokens") is not None else reservation["reserved_tokens"]
        if charged != doc["charged_tokens"]:
            raise BudgetUnavailableError("analysis_budget_invalid")

    async def _read(self, *, require_live=True):
        if require_live and self.require_live is not None:
            await self.require_live()
        doc = await self.service._store(self.service.repository.get(self.lineage_id))
        self._validate(doc)
        return doc

    async def snapshot(self) -> BudgetSnapshot:
        doc = await self._read()
        return await self._usage_snapshot(doc) if self.policy.unlimited else _snapshot(doc)

    async def _usage_snapshot(self, doc) -> BudgetSnapshot:
        usage = await self.service._store(self.service.repository.aggregate_usage(
            self.lineage_id, self.user_id, self.session_id))
        if (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                for key in ("tool_batches_used", "model_calls", "charged_tokens"))):
            raise BudgetUnavailableError("analysis_budget_invalid")
        return BudgetSnapshot(self.lineage_id, usage["tool_batches_used"], None, None, 0,
                              usage["model_calls"], usage["charged_tokens"], None, None, None)

    def _operation_id(self, kind, reservation_id):
        return f"{self.lineage_id}:{kind}:{reservation_id}"

    async def _reserve_unlimited(self, kind, reservation_id, *, evidence_digest=None,
                                 reserved_tokens=None, allowed=True, reason="allowed"):
        doc = await self._read()
        operation = {
            "_id": self._operation_id(kind, reservation_id), "lineage_id": self.lineage_id,
            "owner_id": self.user_id, "session_id": self.session_id, "scope_digest": self.scope_digest,
            "kind": kind, "reservation_id": reservation_id, "evidence_digest": evidence_digest,
            "reserved_tokens": reserved_tokens, "actual_tokens": None,
            "allowed": allowed, "reason": reason, "created_at": self.service._now(),
        }
        if self.require_live is not None:
            await self.require_live()
        stored, created = await self.service._store(self.service.repository.reserve_operation(operation))
        if (not isinstance(stored, dict) or any(stored.get(key) != value for key, value in operation.items()
                if key not in {"created_at", "actual_tokens"})):
            raise BudgetUnavailableError("analysis_budget_reservation_changed")
        if self.require_live is not None:
            await self.require_live()
        snapshot = await self._usage_snapshot(doc)
        if self.require_live is not None:
            await self.require_live()
        # A lost insert response or repeated ID can never authorize another
        # physical call. It remains counted conservatively for the same lineage.
        return BudgetAdmission(**snapshot.__dict__, allowed=allowed and created,
            reason=reason if created or not allowed else (
                "tool_batch_already_reserved" if kind == "tool" else "model_request_already_reserved"),
            reservation_id=reservation_id)

    async def _commit(self, before, after):
        if self.require_live is not None:
            await self.require_live()
        if self.service._now() >= _aware(before["deadline_at"]):
            return False
        after["version"] = before["version"] + 1
        after["updated_at"] = self.service._now()
        committed = await self.service._store(self.service.repository.compare_and_swap(
            self.lineage_id, before["version"], after, require_before_deadline=True))
        if committed and self.require_live is not None:
            await self.require_live()
        if committed and self.service._now() >= _aware(before["deadline_at"]):
            return False
        return committed

    def _grant_reason(self, doc, evidence, increment):
        if doc["grant_count"] >= self.policy.max_grants or increment <= 0:
            return "tool_budget_exhausted"
        if (_aware(doc["deadline_at"]) - self.service._now()).total_seconds() < self.policy.grant_min_remaining_seconds:
            return "budget_finalization_headroom_insufficient"
        if evidence.has_unknown_execution:
            return "budget_execution_unconfirmed"
        if evidence.no_progress_loop:
            return "budget_no_progress_loop"
        if (evidence.confirmed_progress_units <= doc["last_grant_progress_units"]
                or evidence.progress_digest == doc["last_grant_progress_digest"]):
            return "budget_no_confirmed_progress"
        if not evidence.next_action_bounded or evidence.estimated_next_batches > increment:
            return "budget_next_action_unbounded"
        if (doc["model_calls"] + self.policy.grant_min_model_calls > self.policy.model_call_limit
                or doc["charged_tokens"] + self.policy.grant_min_model_tokens > self.policy.model_token_limit):
            return "budget_model_headroom_insufficient"
        return "granted"

    async def reserve_tool_batch(self, evidence: BudgetEvidence, *, reservation_id: str | None = None) -> BudgetAdmission:
        # Type validation is not authority: only the runner builds this evidence.
        evidence = BudgetEvidence.model_validate(evidence)
        reservation_id = _reservation_id(reservation_id)
        evidence_digest = hashlib.sha256(evidence.model_dump_json().encode()).hexdigest()
        if self.policy.unlimited:
            allowed = evidence.scope_digest == self.scope_digest
            return await self._reserve_unlimited("tool", reservation_id, evidence_digest=evidence_digest,
                allowed=allowed, reason="allowed" if allowed else "budget_scope_changed")
        for _ in range(MAX_CAS_ATTEMPTS):
            doc = await self._read()
            if self.service._now() >= _aware(doc["deadline_at"]):
                return _admission(doc, reservation_id, allowed=False, reason="analysis_budget_deadline_exceeded")
            if evidence.scope_digest != self.scope_digest:
                after = deepcopy(doc)
                after["denials"] = (after["denials"] + [{"reservation_id": reservation_id,
                    "reason": "budget_scope_changed", "evidence_digest": evidence_digest,
                    "created_at": datetime.now(UTC)}])[-32:]
                if await self._commit(doc, after):
                    return _admission(after, reservation_id, allowed=False, reason="budget_scope_changed")
                continue
            previous = doc["tool_reservations"].get(reservation_id)
            if previous is not None:
                if previous.get("evidence_digest") != evidence_digest:
                    raise BudgetUnavailableError("analysis_budget_reservation_changed")
                # Idempotent accounting is not permission to replay tools whose
                # execution may have occurred after the original reservation.
                return _admission(doc, reservation_id, allowed=False, reason="tool_batch_already_reserved")
            # Repeating a denied logical admission never manufactures a new grant.
            if any(item.get("reservation_id") == reservation_id for item in doc["denials"]):
                reason = next(item["reason"] for item in doc["denials"] if item["reservation_id"] == reservation_id)
                return _admission(doc, reservation_id, allowed=False, reason=reason)
            after = deepcopy(doc)
            increment = 0
            reason = "allowed"
            if doc["tool_batches_used"] >= doc["soft_limit"]:
                increment = min(self.policy.increment_batches, self.policy.hard_batches - doc["soft_limit"])
                reason = self._grant_reason(doc, evidence, increment)
                if reason == "granted":
                    after["soft_limit"] += increment
                    after["grant_count"] += 1
                    after["last_grant_progress_units"] = evidence.confirmed_progress_units
                    after["last_grant_progress_digest"] = evidence.progress_digest
                    after["grants"].append({"decision_id": uuid4().hex, "reservation_id": reservation_id,
                        "policy_version": self.policy.version, "reason": "confirmed_progress",
                        "from_limit": doc["soft_limit"], "to_limit": after["soft_limit"],
                        "evidence": evidence.model_dump(), "model_calls": doc["model_calls"],
                        "charged_tokens": doc["charged_tokens"], "created_at": datetime.now(UTC)})
            if reason not in {"allowed", "granted"}:
                after["denials"] = (after["denials"] + [{"reservation_id": reservation_id,
                    "reason": reason, "evidence_digest": evidence_digest, "created_at": datetime.now(UTC)}])[-32:]
                if await self._commit(doc, after):
                    return _admission(after, reservation_id, allowed=False, reason=reason)
                continue
            after["tool_batches_used"] += 1
            after["tool_reservations"][reservation_id] = {"batch_index": after["tool_batches_used"],
                "evidence_digest": evidence_digest, "created_at": datetime.now(UTC)}
            if await self._commit(doc, after):
                return _admission(after, reservation_id, allowed=True, reason=reason, granted=increment)
        raise BudgetUnavailableError("analysis_budget_contention")

    async def reserve_model_request(self, tokens: int, *, reservation_id: str | None = None) -> BudgetAdmission:
        if type(tokens) is not int or tokens < 1:
            raise ValueError("Invalid model token reservation")
        reservation_id = _reservation_id(reservation_id)
        if self.policy.unlimited:
            return await self._reserve_unlimited("model", reservation_id, reserved_tokens=tokens)
        for _ in range(MAX_CAS_ATTEMPTS):
            doc = await self._read()
            if self.service._now() >= _aware(doc["deadline_at"]):
                return _admission(doc, reservation_id, allowed=False, reason="analysis_budget_deadline_exceeded")
            previous = doc["model_reservations"].get(reservation_id)
            if previous is not None:
                if previous["reserved_tokens"] != tokens:
                    raise BudgetUnavailableError("analysis_budget_reservation_changed")
                # A physical request with an uncertain outcome must never resend
                # merely because its accounting reservation exists.
                return _admission(doc, reservation_id, allowed=False, reason="model_request_already_reserved")
            if doc["model_calls"] >= self.policy.model_call_limit:
                return _admission(doc, reservation_id, allowed=False, reason="task_call_budget_exceeded")
            if doc["charged_tokens"] + tokens > self.policy.model_token_limit:
                return _admission(doc, reservation_id, allowed=False, reason="task_token_budget_exceeded")
            after = deepcopy(doc)
            after["model_calls"] += 1
            after["charged_tokens"] += tokens
            after["model_reservations"][reservation_id] = {"reserved_tokens": tokens, "actual_tokens": None,
                "call_index": after["model_calls"], "created_at": datetime.now(UTC)}
            if await self._commit(doc, after):
                return _admission(after, reservation_id, allowed=True, reason="allowed")
        raise BudgetUnavailableError("analysis_budget_contention")

    async def settle_model_request(self, reservation_id: str, actual_tokens: int | None) -> BudgetSnapshot:
        _reservation_id(reservation_id)
        if actual_tokens is not None and (type(actual_tokens) is not int or actual_tokens < 0):
            raise ValueError("Invalid actual model usage")
        if self.policy.unlimited:
            doc = await self._read(require_live=False)
            operation_id = self._operation_id("model", reservation_id)
            stored = await self.service._store(self.service.repository.settle_operation(
                operation_id, self.lineage_id, self.user_id, self.session_id, actual_tokens))
            if not isinstance(stored, dict) or stored.get("allowed") is not True or stored.get("kind") != "model":
                raise BudgetUnavailableError("analysis_budget_reservation_missing")
            if actual_tokens is not None and stored.get("actual_tokens") != actual_tokens:
                raise BudgetUnavailableError("analysis_budget_settlement_changed")
            return await self._usage_snapshot(doc)
        for _ in range(MAX_CAS_ATTEMPTS):
            # Accounting may complete after lease expiry; it never authorizes an
            # action. Missing usage remains conservatively reserved.
            doc = await self._read(require_live=False)
            reservation = doc["model_reservations"].get(reservation_id)
            if reservation is None:
                raise BudgetUnavailableError("analysis_budget_reservation_missing")
            if actual_tokens is None or reservation["actual_tokens"] == actual_tokens:
                return _snapshot(doc)
            if reservation["actual_tokens"] is not None:
                raise BudgetUnavailableError("analysis_budget_settlement_changed")
            after = deepcopy(doc)
            after["charged_tokens"] += actual_tokens - reservation["reserved_tokens"]
            after["model_reservations"][reservation_id]["actual_tokens"] = actual_tokens
            after["version"] = doc["version"] + 1
            after["updated_at"] = self.service._now()
            if await self.service._store(self.service.repository.compare_and_swap(self.lineage_id, doc["version"], after)):
                return _snapshot(after)
        raise BudgetUnavailableError("analysis_budget_contention")
