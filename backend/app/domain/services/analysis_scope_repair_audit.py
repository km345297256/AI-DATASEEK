"""Durably consume one scope completion opportunity before dispatch.

Only opaque identities, hashes and fixed status are stored. Insert ambiguity is
not replay permission; the unique identity is reused and never upserted/retried.
"""
from datetime import UTC, datetime
import hashlib
import json


class ScopeRepairAuditStore:
    def __init__(self, collection=None):
        self._collection_override = collection

    @property
    def collection(self):
        if self._collection_override is not None:
            return self._collection_override
        from app.infrastructure.models.documents import SessionDocument
        return SessionDocument.get_pymongo_collection().database['analysis_delivery_checks']

    async def claim(self, *, user_id, session_id, snapshot):
        from app.domain.services.analysis_delivery_audit import _identifier, _step_identity
        owner, session = _identifier(user_id), _identifier(session_id)
        if (not isinstance(snapshot, dict) or snapshot.get('consumed') is not True
                or snapshot.get('restored') is not False or type(snapshot.get('input_seq')) is not int
                or not 0 < snapshot['input_seq'] <= 2**63-1):
            return False
        step = _step_identity(snapshot.get('step_id'))
        request_hash = snapshot.get('request_sha256')
        if not isinstance(request_hash, str) or len(request_hash) != 64 or any(c not in '0123456789abcdef' for c in request_hash):
            return False
        identity = dict(owner_id=owner, session_id=session, input_seq=snapshot['input_seq'], **step)
        # The original input gets one opportunity in total, even if a later
        # plan/step name changes. Keep the step only as diagnostic attribution.
        input_identity = {key: identity[key] for key in ('owner_id', 'session_id', 'input_seq')}
        key = hashlib.sha256(json.dumps(input_identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        record = dict(_id='scope-completion:'+key, **identity, document_type='scope_completion',
                      version=1, created_at=datetime.now(UTC), state='consumed', request_sha256=request_hash)
        try:
            result = await self.collection.insert_one(record)
            return result.acknowledged is True
        except Exception:
            # Duplicate, unavailable, or ambiguous insert: do not enqueue. Do
            # not log driver text or retry. CancelledError still propagates.
            return False
