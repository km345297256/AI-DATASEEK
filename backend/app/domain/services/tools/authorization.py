from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace
from typing import Any

from app.domain.external.plugin_runtime import ToolExecutionDescriptor
from app.domain.services.execution_identity import private_identity_hmac
from app.domain.services.tools.interceptors import ToolPolicyGuardInterceptor, ToolPolicySnapshot
from app.domain.services.tools.pipeline import ToolExecutionContext, ToolExecutionInterceptor
from app.domain.services.tools.spill_projection import sanitize_spill_public_data


logger = logging.getLogger(__name__)
_STATE = "call_authorization_state"
_RISK_EFFECTS = frozenset({"network", "credential_use", "external_side_effect"})


class ToolAuthorizationStopped(asyncio.CancelledError):
    """A denied/expired grant ends the dependent turn, never a model retry."""

    code = "tool_authorization_stopped"


class ToolCallAuthorizationInterceptor(ToolExecutionInterceptor):
    """Call-specific consent, separate from the registry's immutable ceiling.

    Guard waits outside the execution deadline/lock. Admission consumes exactly
    once *after* the lock, binding the original arguments and credential versions.
    Neither browser input nor tool arguments can provide an effective grant.
    """

    def __init__(self, snapshot: ToolPolicySnapshot, *, approvals, credentials,
                 user_id: str, session_id: str, identity_provider, event_sink=None,
                 poll_seconds: float = 0.5, wait_seconds: float = 300):
        self.snapshot = snapshot
        self.approvals = approvals
        self.credentials = credentials
        self.user_id = user_id
        self.session_id = session_id
        self.identity_provider = identity_provider
        self.event_sink = event_sink
        self.poll_seconds = poll_seconds
        self.wait_seconds = wait_seconds

    @staticmethod
    def _contract(context: ToolExecutionContext) -> dict[str, Any]:
        contract = context.metadata.get("execution_contract") or getattr(context.tool, "execution_contract", {})
        if context.metadata.get("plugin"):
            # Validate again at the invocation boundary, including fail-closed
            # handling for adapters not backed by the Node catalog.
            return ToolExecutionDescriptor.model_validate(contract).model_dump(mode="json")
        if contract:
            return ToolExecutionDescriptor.model_validate(contract).model_dump(mode="json")
        # Host-owned classification for calls to external services. Shell/code
        # remains governed by its existing sandbox boundary, not code guessing.
        toolkit = getattr(getattr(context.tool, "toolkit", None), "name", "")
        if toolkit in {"mcp", "browser", "search"} or context.tool_name.startswith("mcp_"):
            return {"effects": ["network", "external_side_effect"] if toolkit == "mcp" or context.tool_name.startswith("mcp_") else ["network"], "permissions": [], "credentials": []}
        return {}

    def _digest(self, context: ToolExecutionContext, bindings) -> str:
        return private_identity_hmac({
            "purpose": "tool-call-consent/v1", "user_id": self.user_id,
            "session_id": self.session_id, "identity": self.identity_provider(),
            "call_id": context.tool_call_id, "tool": context.tool_name,
            "arguments": context.arguments, "contract": self._contract(context),
            "credential_bindings": [view.model_dump(mode="json") for view in bindings],
        })

    async def _publish(self, record, context):
        if self.event_sink is None:
            return
        try:
            async with asyncio.timeout(1):
                await self.event_sink(record.public_view(), context)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Tool approval event failed error_type=%s", type(error).__name__)

    async def guard(self, context: ToolExecutionContext) -> None:
        try:
            contract = self._contract(context)
            if contract:
                context.metadata["execution_contract"] = contract
            permissions = frozenset(contract.get("permissions", []))
            # This temporary snapshot only validates the immutable ceiling.
            # Declared permissions are not granted until consume() below.
            structural = replace(self.snapshot, granted_permissions=self.snapshot.granted_permissions | permissions)
            await ToolPolicyGuardInterceptor(structural).guard(context)
            effects = frozenset(contract.get("effects", []))
            requirements = contract.get("credentials", [])
            if not (effects & _RISK_EFFECTS or permissions or requirements):
                return
            # Freeze the reviewed call without changing legacy low-risk calls.
            # External mutation of the caller-owned dict while the user thinks
            # must never alter what an approved invocation executes.
            context.tool_call = copy.deepcopy(context.tool_call)
            identity = self.identity_provider()
            if not identity.get("task_id"):
                raise ToolAuthorizationStopped()
            if requirements and not context.metadata.get("plugin"):
                raise ToolAuthorizationStopped()
            bindings = await self.credentials.describe_bindings(self.user_id, context.tool_name, requirements) if requirements else []
            preview = sanitize_spill_public_data(copy.deepcopy(context.arguments))
            if len(json.dumps(preview, ensure_ascii=True).encode()) > 12_000:
                # A huge request cannot be adequately reviewed in this card.
                raise ToolAuthorizationStopped()
            digest = self._digest(context, bindings)
            record = await self.approvals.create(
                user_id=self.user_id, session_id=self.session_id, task_id=identity["task_id"],
                tool_name=context.tool_name, call_digest=digest, effects=sorted(effects),
                permissions=sorted(permissions), arguments_preview=preview,
                credential_refs=[view.reference for view in bindings],
                execution_snapshot_id=identity.get("execution_snapshot_id"),
                catalog_revision=identity.get("catalog_revision"),
            )
            context.metadata[_STATE] = {"record": record, "digest": digest, "bindings": bindings}
            context.metadata["policy_decision"] = "pending"
            await self._publish(record, context)
            async with asyncio.timeout(self.wait_seconds):
                while True:
                    latest = await self.approvals.get_for_owner(self.user_id, self.session_id, record.approval_id)
                    if latest is None:
                        raise ToolAuthorizationStopped()
                    if latest.revision != record.revision:
                        record = latest
                        context.metadata[_STATE]["record"] = latest
                        await self._publish(latest, context)
                    if latest.status == "approved":
                        return
                    if latest.status != "pending":
                        raise ToolAuthorizationStopped()
                    await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not turn a permission/configuration failure into an automatic
            # tool-repair loop, or disclose a private provider/DB exception.
            logger.warning("Tool call authorization stopped error_type=%s", type(error).__name__)
            raise ToolAuthorizationStopped() from None

    async def admit(self, context: ToolExecutionContext) -> None:
        state = context.metadata.get(_STATE)
        if state is None:
            return
        try:
            if self._digest(context, state["bindings"]) != state["digest"]:
                raise ToolAuthorizationStopped()
            consumed = await self.approvals.consume(state["record"].approval_id, state["digest"])
            if consumed is None or consumed.status != "consumed":
                raise ToolAuthorizationStopped()
            state["record"] = consumed
            await self._publish(consumed, context)
            if self._digest(context, state["bindings"]) != state["digest"]:
                raise ToolAuthorizationStopped()
            # Resolve only after the one-shot CAS and its notification: a
            # revoke committed during either await must not leave a previously
            # decrypted value available for this invocation. A consumed grant
            # may still fail credential admission; it must never be retried.
            values = await self.credentials.resolve_bindings(self.user_id, context.tool_name, state["bindings"]) if state["bindings"] else {}
            if self._digest(context, state["bindings"]) != state["digest"]:
                raise ToolAuthorizationStopped()
            context.metadata["credential_values"] = values
            context.metadata["policy_decision"] = "allow"
            context.metadata["policy_reason"] = "call_grant_consumed"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Tool grant admission stopped error_type=%s", type(error).__name__)
            raise ToolAuthorizationStopped() from None

    async def on_error(self, context: ToolExecutionContext, error: BaseException) -> None:
        context.metadata.pop("credential_values", None)
        state = context.metadata.get(_STATE)
        if state is not None and state["record"].status in {"pending", "approved"}:
            latest = await self.approvals.cancel(state["record"].approval_id)
            if latest is not None:
                await self._publish(latest, context)


class ToolCallAdmissionInterceptor(ToolExecutionInterceptor):
    def __init__(self, authorization: ToolCallAuthorizationInterceptor):
        self.authorization = authorization

    async def execute(self, context, call_next):
        try:
            await self.authorization.admit(context)
            return await call_next()
        finally:
            context.metadata.pop("credential_values", None)
