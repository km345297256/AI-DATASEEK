"""Request-local transitions from saved content to execution or delivery.

This is not a write quota or a content reviewer. A saved version, a genuinely
new input observation, and a confirmed execution are different states. Callers
must authenticate the core file operation and any supplied observations before
using this policy. Decisions never execute, validate, or publish a file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import posixpath
from pathlib import PurePosixPath
from typing import Literal

from app.domain.services.execution_identity import private_identity_hmac


PROGRAM_SUFFIXES = frozenset({
    ".py", ".pyw", ".r", ".rmd", ".jl", ".js", ".mjs", ".cjs", ".ts",
    ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".sql", ".ipynb", ".m",
    ".c", ".h", ".cpp", ".hpp", ".java", ".rs", ".go", ".rb", ".pl",
})
WriteAction = Literal["allow", "reuse", "execute_saved", "finalize_saved", "inspect_saved"]


@dataclass(frozen=True)
class WriteDecision:
    action: WriteAction
    path: str | None = None
    kind: Literal["program", "report"] | None = None
    reason: str | None = None
    phase: str | None = None


@dataclass(frozen=True)
class _Mutation:
    path: str
    kind: Literal["program", "report"]
    mode: Literal["overwrite", "append", "replace"]
    identity: str
    content_identity: str | None = None
    reverse_identity: str | None = None
    nonblank: bool = False
    empty_change: bool = False


@dataclass
class _SavedArtifact:
    path: str = field(repr=False)
    kind: Literal["program", "report"]
    phase: str = "building"
    revision: int = 0
    content_identity: str | None = None
    # Authored revision identity is not a promise about current sandbox bytes.
    # Unlike content_identity it survives an intervening process invalidation,
    # so writing the same failed source cannot masquerade as a correction.
    submitted_content_identity: str | None = None
    submitted_operation: str | None = None
    context: tuple[int, int] = (0, 0)
    operations: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    inspections: set[str] = field(default_factory=set)
    inspected: bool = False


def _path(value: object) -> str | None:
    # Only canonicalize spelling. Do not guess a cwd, expand ~, or dereference
    # links without a trusted sandbox observation.
    if (not isinstance(value, str) or not value.startswith("/")
            or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    return "/" + posixpath.normpath(value).lstrip("/")


def _identity(purpose: str, value: object) -> str:
    return private_identity_hmac({"purpose": f"artifact-write-{purpose}/v1", "value": value})


def _content_identity(digest: str) -> str:
    return _identity("content", digest)


def _valid_digest(digest: object) -> bool:
    return (isinstance(digest, str) and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest))


def _mutation(call: dict) -> _Mutation | None:
    if not isinstance(call, dict) or call.get("name") not in {"file_write", "file_str_replace"}:
        return None
    args = call.get("args")
    if not isinstance(args, dict) or (path := _path(args.get("file"))) is None:
        return None
    if PurePosixPath(path).suffix.lower() in PROGRAM_SUFFIXES:
        kind = "program"
    elif path.startswith("/home/ubuntu/output/"):
        kind = "report"
    else:
        return None  # Do not reinterpret ordinary configuration/source edits.
    if call["name"] == "file_write":
        content = args.get("content")
        if not isinstance(content, str) or any(
                args.get(key) is not None and type(args[key]) is not bool
                for key in ("append", "leading_newline", "trailing_newline")):
            return None  # The normal tool schema owns malformed arguments.
        content = ("\n" if args.get("leading_newline") else "") + content + ("\n" if args.get("trailing_newline") else "")
        try:
            digest = _content_identity(hashlib.sha256(content.encode("utf-8")).hexdigest())
        except UnicodeEncodeError:
            return None
        mode = "append" if args.get("append") is True else "overwrite"
        return _Mutation(path, kind, mode, _identity("operation", [mode, digest]),
                         content_identity=digest if mode == "overwrite" else None,
                         nonblank=bool(content.strip()), empty_change=mode == "append" and not content)
    old, new = args.get("old_str"), args.get("new_str")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    return _Mutation(path, kind, "replace", _identity("operation", ["replace", old, new]),
                     reverse_identity=_identity("operation", ["replace", new, old]),
                     nonblank=bool(new.strip()), empty_change=old == new)


class ArtifactWriteProgress:
    """Content-phase policy shared by all execution steps in one user request.

    No source bodies are retained. There is no time, tool-count, revision-count,
    or language-dependent allowance. Distinct append/patch chunks remain valid;
    a full replacement without new input/execution feedback changes phase.
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, _SavedArtifact] = {}
        self._observations: set[str] = set()
        self._global_context = 0
        self._target_contexts: dict[str, int] = {}

    def _context(self, target: str) -> tuple[int, int]:
        return self._global_context, self._target_contexts.get(target, 0)

    def checkpoint(self, path: str) -> str | None:
        """Identify an observed saved revision, not model wording or call IDs."""
        normalized = _path(path)
        target = _identity("target", normalized) if normalized else None
        state = self._artifacts.get(target)
        if state is None:
            return None
        return _identity("checkpoint", [target, state.revision, self._context(target)])

    @staticmethod
    def _decision(state: _SavedArtifact, reason: str, *, incremental: bool = False) -> WriteDecision:
        action: WriteAction = ("finalize_saved" if state.kind == "report" else
                               "inspect_saved" if incremental else "execute_saved")
        return WriteDecision(action, state.path, state.kind, reason, state.phase)

    def before_call(self, call: dict) -> WriteDecision:
        mutation = _mutation(call)
        if mutation is None:
            return WriteDecision("allow")
        target = _identity("target", mutation.path)
        state = self._artifacts.get(target)
        if state is None:
            return WriteDecision("allow", mutation.path, mutation.kind, phase="building")
        if (mutation.empty_change or mutation.mode == "overwrite"
                and mutation.content_identity == state.content_identity):
            return WriteDecision("reuse", state.path, state.kind, "saved_content_unchanged", state.phase)
        fresh_context = state.context != self._context(target)
        if mutation.mode == "overwrite":
            if state.phase != "building" and not fresh_context:
                reason = ("saved_version_cycle" if mutation.content_identity in state.versions else
                          "saved_content_ready")
                return self._decision(state, reason)
        elif not fresh_context and not state.inspected:
            if mutation.identity in state.operations:
                return self._decision(state, "repeated_incremental_edit", incremental=True)
            if mutation.reverse_identity and mutation.reverse_identity in state.operations:
                return self._decision(state, "reversed_incremental_edit", incremental=True)
        return WriteDecision("allow", state.path, state.kind, phase=state.phase)

    def record_success(self, call: dict, *, current_digest: str | None = None) -> bool:
        """Record only a confirmed successful core write, before compaction.

        current_digest, if supplied, must be a host-observed SHA256 of the whole
        current file. A partial read or a model-provided digest is not sufficient.
        Without it, incremental edits deliberately invalidate the known byte hash.
        Return whether this submission establishes a changed authored revision,
        not whether the current file matches an older cached copy. A caller must
        additionally exclude zero-replacement receipts before unlocking a failed
        runtime; a successful no-op is not a source correction.
        """
        mutation = _mutation(call)
        if mutation is None:
            return False
        target = _identity("target", mutation.path)
        state = self._artifacts.setdefault(target, _SavedArtifact(mutation.path, mutation.kind))
        observed_content = _content_identity(current_digest) if _valid_digest(current_digest) else None
        submitted_content = observed_content or mutation.content_identity
        if mutation.empty_change:
            source_changed = False
        elif submitted_content is not None:
            source_changed = submitted_content != state.submitted_content_identity
        else:
            source_changed = mutation.identity != state.submitted_operation
        state.submitted_operation = mutation.identity
        if submitted_content is not None:
            state.submitted_content_identity = submitted_content
        elif not mutation.empty_change:
            # A real append/patch changes the submitted version but its whole
            # bytes cannot be reconstructed from the operation alone.
            state.submitted_content_identity = None
        context = self._context(target)
        if state.context != context:
            state.operations.clear()
        state.context = context
        state.revision += 1
        state.operations.add(mutation.identity)
        state.inspected = False
        if observed_content is not None:
            state.content_identity = observed_content
        elif mutation.mode == "overwrite":
            state.content_identity = mutation.content_identity
        elif not mutation.empty_change:
            state.content_identity = None
        if state.content_identity:
            state.versions.add(state.content_identity)
        if mutation.nonblank or state.phase != "building":
            state.phase = "saved"
        return source_changed

    def record_observation(self, identity: str, *, target: str | None = None) -> bool:
        """Admit genuinely new, trusted input evidence, never saved-output reads.

        Prefer target-bound observations when the affected artifact is known.
        A global observation is for relevant request-scoped input evidence only.
        Repeated observations and model assertions do not grant another revision.
        """
        if not isinstance(identity, str) or not identity:
            return False
        path = _path(target) if target is not None else None
        if target is not None and path is None:
            return False
        key = _identity("target", path) if path is not None else None
        observation = _identity("observation", [key, identity])
        if observation in self._observations:
            return False
        self._observations.add(observation)
        if key is None:
            self._global_context += 1
        else:
            self._target_contexts[key] = self._target_contexts.get(key, 0) + 1
        return True

    def record_execution(self, path: str, *, outcome: str | int, source_digest: str,
                         operation_id: str | None = None) -> bool:
        """A trusted terminal source-bound run opens its next revision phase.

        This does not clear the independent failed-program diagnostic gate.
        Pending/unknown launches cannot grant revision or overwrite authority.
        """
        if (type(outcome) is not int and (not isinstance(outcome, str) or outcome not in {"succeeded", "failed"})) or not _valid_digest(source_digest):
            return False
        normalized = _path(path)
        if normalized is None:
            return False
        changed = self.record_observation(_identity("execution", [source_digest, outcome, operation_id]), target=normalized)
        if changed:
            # A real program can replace/remove its own source or another saved
            # artifact. A launch snapshot proves execution, not current bytes.
            self.invalidate_content()
        if changed and (state := self._artifacts.get(_identity("target", normalized))):
            state.phase = "executed" if outcome in (0, "succeeded") else "needs_revision"
        return changed

    def record_diagnostic(self, path: str, evidence_digest: str) -> bool:
        """Use only after the host has accepted a relevant failure diagnosis."""
        if not isinstance(evidence_digest, str) or not evidence_digest:
            return False
        return self.record_observation(_identity("diagnostic", evidence_digest), target=path)

    def invalidate_content(self, path: str | None = None) -> None:
        """Discard byte-freshness after an untracked mutation, not edit history.

        Call after a shell/plugin operation that may modify saved files. This
        prevents an old successful write receipt becoming a false no-op claim.
        It grants no new rewrite authority and does not mark any output ready.
        """
        normalized = _path(path) if path is not None else None
        states = (self._artifacts.values() if path is None else
                  [self._artifacts.get(_identity("target", normalized))] if normalized else [])
        for state in states:
            if state is not None:
                state.content_identity = None
                state.inspected = False

    def record_saved_inspection(self, path: str, content_digest: str) -> bool:
        """Permit one incremental correction after a new trusted code read.

        This does not grant an entire rewrite, reset input evidence, or declare
        progress. It makes repeated structural append chunks possible without
        allowing an uninspected append/patch loop. Same-byte rereads add nothing.
        """
        normalized = _path(path)
        state = self._artifacts.get(_identity("target", normalized)) if normalized else None
        if state is None or not isinstance(content_digest, str) or not content_digest:
            return False
        observation = _identity("inspection", content_digest)
        if observation in state.inspections:
            return False
        state.inspections.add(observation)
        state.inspected = True
        return True
