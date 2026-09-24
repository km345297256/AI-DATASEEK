"""One host-private completion opportunity, never general replay authority.

The caller supplies an exact candidate retained by the current parsed review,
current admission/lease facts, and private execution receipts. Public events,
model-authored metadata, generated files and prose cannot establish these facts.
This pure module performs no I/O and creates no model/tool budget. Keep a tracker
outside executor memory for the original accepted input; all recovery disables
the opportunity, including checkpoints predating this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ScopeReviewBinding:
    input_seq: int
    step_id: str
    request: str = field(repr=False)
    paragraphs: tuple[str, ...] = field(repr=False)
    metadata: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ScopeRepairGuards:
    # None is deliberately blocked. Absence of a blocker is not affirmative
    # knowledge that all original prerequisites and authority remain present.
    analysis_authorized: bool | None = None
    current_input: bool | None = None
    original_request_complete: bool | None = None
    candidate_complete: bool | None = None
    read_only_observations: bool | None = None
    prerequisites_met: bool | None = None
    no_user_input_required: bool | None = None
    no_policy_refusal: bool | None = None
    runtime_live: bool | None = None
    no_artifacts_or_requirements: bool | None = None
    no_review_repair: bool | None = None


@dataclass(frozen=True)
class ScopeRepairDecision:
    allowed: bool
    reason: str
    feedback: dict[str, Any] | None = field(default=None, repr=False)


class ScopeRepairTracker:
    def __init__(self, *, input_seq: int, step_id: str, request: str, restored: bool = False):
        if (type(input_seq) is not int or input_seq < 1 or not isinstance(step_id,str)
                or not 0 < len(step_id) <= 256 or not isinstance(request,str)
                or not request.strip() or type(restored) is not bool):
            raise ValueError('scope_identity_invalid')
        self._input_seq=input_seq
        self._step_id=step_id
        self._request_sha256=_digest(request)
        self._restored=restored
        self._consumed=False

    def snapshot(self) -> dict:
        """Safe private diagnostics only; restoration never grants a retry."""
        return dict(version=1,input_seq=self._input_seq,step_id=self._step_id,
                    request_sha256=self._request_sha256,consumed=self._consumed,restored=self._restored)

    def review(self, *, binding: ScopeReviewBinding, execution: Mapping[str,Any],
               guards: ScopeRepairGuards, now: datetime, deadline_at: datetime | None) -> ScopeRepairDecision:
        def blocked(reason): return ScopeRepairDecision(False,reason)
        if self._restored: return blocked('scope_recovery_not_allowed')
        if self._consumed: return blocked('scope_opportunity_consumed')
        if not isinstance(guards,ScopeRepairGuards) or any(v is not True for v in guards.__dict__.values()):
            return blocked('scope_host_prerequisite_unconfirmed')
        if not isinstance(now,datetime) or now.tzinfo is None:
            return blocked('scope_runtime_time_unconfirmed')
        if deadline_at is not None and (not isinstance(deadline_at,datetime) or deadline_at.tzinfo is None or now >= deadline_at):
            return blocked('scope_runtime_deadline')
        if (not isinstance(binding,ScopeReviewBinding) or type(binding.input_seq) is not int
                or binding.input_seq != self._input_seq or binding.step_id != self._step_id
                or not isinstance(binding.request,str) or _digest(binding.request) != self._request_sha256):
            return blocked('scope_identity_changed')
        paragraphs=binding.paragraphs
        if (not isinstance(paragraphs,tuple) or not 1 <= len(paragraphs) <= 64
                or not all(isinstance(p,str) and p.strip() for p in paragraphs)
                or len('\n\n'.join(paragraphs)) > 24000):
            return blocked('scope_candidate_unconfirmed')
        scope=binding.metadata
        if not isinstance(scope,Mapping): return blocked('scope_review_invalid')
        expected=dict(version=1,review_kind='answer_scope',enabled=True,status='incomplete',
            reason='answer_scope_incomplete',candidate_version_status='original',
            completion_blocker='none',
            request_sha256=self._request_sha256,paragraph_count=len(paragraphs),
            candidate_sha256=_digest(json.dumps(list(paragraphs),ensure_ascii=False,separators=(',',':'))),
            candidate_text_sha256=_digest('\n\n'.join(paragraphs)))
        if any(type(scope.get(k)) is not type(v) or scope.get(k) != v for k,v in expected.items()):
            return blocked('scope_review_invalid')
        count=scope.get('checked_paragraph_count')
        if type(count) is not int or not 0 <= count <= len(paragraphs):
            return blocked('scope_review_invalid')
        if (not isinstance(execution,Mapping) or execution.get('code') != 'completed'
                or execution.get('has_unconfirmed_tool_execution') is not False
                or execution.get('side_effect_state') == 'unknown'):
            return blocked('scope_execution_unconfirmed')
        proof=execution.get('execution_evidence')
        expected_proof=dict(execution_confirmed=True,pending_execution=False,unresolved_call_count=0,
            tracked_operation_count=0,nonreplayable_call_count=0,has_observable_pending=False,has_unresolvable_pending=False)
        if not isinstance(proof,Mapping) or any(type(proof.get(k)) is not type(v) or proof.get(k) != v for k,v in expected_proof.items()):
            return blocked('scope_execution_unconfirmed')
        # Reserve before any caller await/audit/queue mutation. Failure to queue
        # consumes the opportunity too; no changing candidate can restore it.
        self._consumed=True
        return ScopeRepairDecision(True,'scope_completion_allowed',dict(
            schema='answer_scope_completion/v1',request_sha256=self._request_sha256,
            candidate_sha256=scope['candidate_sha256'],status='original_request_incomplete',
            preserve_existing_results=True,repeat_completed_operations=False))
