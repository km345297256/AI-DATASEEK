"""Direct Python launches: immutable source input, literal argv and real exit status.

These helpers are deliberately not a shell command generator. Source and
diagnostics use inherited anonymous files; data printed by the program never
decides whether the process succeeded.
"""
import ast
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import warnings

from app.core.exceptions import BadRequestException


MAX_PROGRAM_SOURCE_BYTES = 8 * 1024 * 1024
MAX_REVIEW_SOURCE_BYTES = 24_000
MAX_PROGRAM_OUTPUT_CHARS = 32_000
PROGRAM_OUTPUT_OMISSION = "\n\n[... middle of program output omitted; bounded head and tail only ...]\n\n"
_OUTPUT_HEAD_CHARS = (MAX_PROGRAM_OUTPUT_CHARS - len(PROGRAM_OUTPUT_OMISSION)) // 2
_OUTPUT_TAIL_CHARS = MAX_PROGRAM_OUTPUT_CHARS - len(PROGRAM_OUTPUT_OMISSION) - _OUTPUT_HEAD_CHARS
BOOTSTRAP = r'''
import hashlib, io, json, linecache, os, re, sys, tokenize, traceback, types
source_fd, diagnostic_fd = int(sys.argv[1]), int(sys.argv[2])
script_path = sys.argv[3]
sys.argv = sys.argv[3:]
sys.path[0] = os.path.dirname(script_path)
with os.fdopen(source_fd, "rb") as source:
    code = source.read()
try:
    encoding = tokenize.detect_encoding(io.BytesIO(code).readline)[0]
    linecache.cache[script_path] = (len(code), None, code.decode(encoding).splitlines(True), script_path)
    main = types.ModuleType("__main__")
    main.__dict__.update(__file__=script_path, __package__=None, __cached__=None, __spec__=None)
    sys.modules["__main__"] = main
    exec(compile(code, script_path, "exec"), main.__dict__)
except BaseException as error:
    frames = traceback.extract_tb(error.__traceback__)
    frame = next((frame for frame in reversed(frames) if frame.filename == script_path), None)
    diagnostic = {"exception_type": type(error).__name__,
                  "function": frame.name if frame else None,
                  "line": frame.lineno if frame else getattr(error, "lineno", None),
                  "message_fingerprint": hashlib.sha256(re.sub(
                      r"\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b", "#",
                      str(error)[:2048],
                  ).encode("utf-8", errors="replace")).hexdigest()}
    os.write(diagnostic_fd, json.dumps(diagnostic).encode("utf-8"))
    raise
finally:
    os.close(diagnostic_fd)
'''


def program_command(script_path: str, args: list[str]) -> str:
    """Canonical receipt identity, shared with the host (not executed by bash)."""
    return json.dumps({"kind": "python_program", "script_path": script_path,
                       "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _PreflightUnavailable(Exception):
    """The current prerequisite state could not be safely established."""


_PREPARATION_MESSAGES = {
    "script_missing": "Program source does not exist; save the script before running it",
    "cwd_missing": "Program working directory does not exist",
    "permission_denied": "Program source or working directory is not accessible",
    "syntax_error": "Program source has a syntax error; correct the saved script before running it",
    "source_not_regular": "Program source must be a regular file",
    "source_too_large": "Program source exceeds the supported file size",
    "unsafe_path": "Program prerequisites cannot use symbolic links or unsafe paths",
    "invalid_request": "Use an absolute Python .py script path and literal string arguments",
    "cwd_not_directory": "Program working directory must be a directory",
    "preflight_unavailable": "Program preparation could not safely establish readable prerequisites",
}


class ProgramPreparationError(BadRequestException):
    """Safe startup diagnosis, never authority to retry a previous operation."""

    def __init__(self, reason: str, *, diagnostic: dict | None = None):
        super().__init__(_PREPARATION_MESSAGES[reason])
        self.data = {"program_preparation": {
            "version": 1, "phase": "not_started", "reason": reason,
            "diagnostic": diagnostic,
        }}


def _source_syntax(source: bytes) -> dict:
    """Parse and compile without running code, imports, or module top levels."""
    result = {"state": "ready", "exception_type": None, "line": None, "offset": None}
    try:
        with warnings.catch_warnings():
            # Compiler warnings may echo source text. This check returns only
            # bounded classifications; normal runtime output remains separate.
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            tree = ast.parse(source, filename="<analysis-program>")
            # ast.parse alone accepts invalid executable constructs such as a
            # top-level return. Compilation still does not execute any code.
            compile(tree, "<analysis-program>", "exec")
    except SyntaxError as error:
        result.update(state="invalid", exception_type=type(error).__name__,
                      line=error.lineno if type(error.lineno) is int and error.lineno > 0 else None,
                      offset=error.offset if type(error.offset) is int and error.offset > 0 else None)
    return result


def _preflight_identity(value: os.stat_result) -> tuple[int, ...]:
    # Race detection only. These identities never enter the returned digest:
    # touching or replacing a file with identical usable bytes is not progress.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    # Unrelated files in a shared ancestor may be created during a probe. That
    # changes directory size/mtime, not the identity/accessibility of this path.
    # Keep replacement and permission changes visible without false races.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


def _preflight_entry(path: str, *, directory: bool, cancelled: threading.Event) -> tuple[dict, tuple]:
    """Observe without following links or opening devices/FIFOs for reading."""
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open("/", flags)
    proof = []
    try:
        parts = Path(path).parts[1:]
        if not parts:
            root = os.fstat(parent)
            access = os.access(".", os.X_OK, dir_fd=parent, effective_ids=True, follow_symlinks=False)
            return {"state": "ready" if access else "not_accessible"}, (_directory_identity(root),)
        for index, part in enumerate(parts):
            if cancelled.is_set():
                raise _PreflightUnavailable
            proof.append(_directory_identity(os.fstat(parent)))
            try:
                observed = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                # The existing, verified ancestor proves absence; a second
                # traversal must agree before this can become a blocked state.
                return {"state": "missing"}, (*proof, "missing", index)
            proof.append((_directory_identity if stat.S_ISDIR(observed.st_mode)
                          else _preflight_identity)(observed))
            if stat.S_ISLNK(observed.st_mode):
                raise _PreflightUnavailable
            final = index == len(parts) - 1
            if not final or directory:
                if not stat.S_ISDIR(observed.st_mode):
                    return {"state": "not_directory" if directory else "not_readable"}, tuple(proof)
                if not os.access(part, os.X_OK, dir_fd=parent, effective_ids=True, follow_symlinks=False):
                    return {"state": "not_accessible" if directory else "not_readable"}, tuple(proof)
                child = os.open(part, flags, dir_fd=parent)
                if _directory_identity(os.fstat(child)) != _directory_identity(observed):
                    os.close(child)
                    raise _PreflightUnavailable
                os.close(parent)
                parent = child
                if final:
                    return {"state": "ready"}, tuple(proof)
                continue
            if not stat.S_ISREG(observed.st_mode):
                return {"state": "not_regular", "source_digest": None}, tuple(proof)
            if not os.access(part, os.R_OK, dir_fd=parent, effective_ids=True, follow_symlinks=False):
                return {"state": "not_readable", "source_digest": None}, tuple(proof)
            if observed.st_size > MAX_PROGRAM_SOURCE_BYTES:
                return {"state": "too_large", "source_digest": None}, tuple(proof)
            descriptor = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                if _preflight_identity(os.fstat(stream.fileno())) != _preflight_identity(observed):
                    raise _PreflightUnavailable
                digest = hashlib.sha256()
                chunks = []
                remaining = observed.st_size + 1
                size = 0
                while remaining:
                    if cancelled.is_set():
                        raise _PreflightUnavailable
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    digest.update(chunk)
                    chunks.append(chunk)
                    size += len(chunk)
                    remaining -= len(chunk)
                if (size != observed.st_size
                        or _preflight_identity(os.fstat(stream.fileno())) != _preflight_identity(observed)):
                    raise _PreflightUnavailable
            syntax = _source_syntax(b"".join(chunks))
            if cancelled.is_set():
                raise _PreflightUnavailable
            return {"state": "ready", "source_digest": digest.hexdigest(),
                    "syntax": syntax}, tuple(proof)
        raise _PreflightUnavailable
    finally:
        os.close(parent)


def probe_program_prerequisites(exec_dir: str, script_path: str, *,
                                root: Path | None = None,
                                cancelled: threading.Event | None = None) -> dict:
    """Read-only launch prerequisites, not permission to execute or replay.

    Stable absence and inaccessible prerequisites are distinguishable from an
    unavailable probe. No process is launched and no source/path/stat identity
    is returned. The host still requires proof that the prior launch did not
    start before changed, now-ready prerequisites can authorize a retry.
    """
    unavailable = {"version": 1, "status": "unavailable", "ready": False,
                   "prerequisite_digest": None, "cwd": {"state": "unavailable"},
                   "source": {"state": "unavailable", "source_digest": None},
                   "syntax": {"state": "not_checked", "exception_type": None,
                              "line": None, "offset": None}}
    cancelled = cancelled or threading.Event()
    try:
        for path in (exec_dir, script_path):
            if (not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path
                    or "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path)):
                return unavailable
        if (not script_path.endswith(".py")
                or (root is not None and (not root.is_absolute() or root == Path("/")
                    or ".." in root.parts or not Path(script_path).is_relative_to(root)))):
            return unavailable
        # Probe the same absolute .py locations accepted by prepare_program,
        # including analysis working directories outside the deliverable root.
        # This remains read-only, no-follow, bounded and race checked; an
        # optional narrower root is used only by isolated callers/tests.
        observations = []
        for _ in range(2):
            cwd, cwd_proof = _preflight_entry(exec_dir, directory=True, cancelled=cancelled)
            source, source_proof = _preflight_entry(script_path, directory=False, cancelled=cancelled)
            source.setdefault("source_digest", None)
            observations.append((cwd, source, cwd_proof, source_proof))
        if observations[0] != observations[1]:
            return unavailable
        cwd, source, _, _ = observations[-1]
        syntax = source.pop("syntax", dict(unavailable["syntax"]))
        ready = cwd["state"] == source["state"] == syntax["state"] == "ready"
        identity = {"version": 1, "cwd": cwd, "source": source, "syntax": syntax}
        return {**identity, "status": "ready" if ready else "blocked", "ready": ready,
                "prerequisite_digest": hashlib.sha256(json.dumps(
                    identity, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()}
    except (OSError, ValueError, RecursionError, _PreflightUnavailable):
        return unavailable


def append_program_output(previous: str, addition: str, truncated: bool) -> tuple[str, bool]:
    """Keep a fixed-size head+tail display, not a complete stdout archive.

    Input has already passed the stream's incremental UTF-8 decoder, so slicing
    characters cannot leave split byte sequences or introduce replacement text.
    The omission marker is included in the total character bound.
    """
    if not truncated:
        combined = previous + addition
        if len(combined) <= MAX_PROGRAM_OUTPUT_CHARS:
            return combined, False
        return (combined[:_OUTPUT_HEAD_CHARS] + PROGRAM_OUTPUT_OMISSION
                + combined[-_OUTPUT_TAIL_CHARS:]), True
    return (previous[:_OUTPUT_HEAD_CHARS] + PROGRAM_OUTPUT_OMISSION
            + (previous[-_OUTPUT_TAIL_CHARS:] + addition)[-_OUTPUT_TAIL_CHARS:]), True


def validate_program_working_directory(exec_dir: str) -> None:
    """Observe caller cwd; never create arbitrary missing directories."""
    try:
        state, _ = _preflight_entry(exec_dir, directory=True, cancelled=threading.Event())
    except PermissionError:
        raise ProgramPreparationError("permission_denied") from None
    except (OSError, ValueError, _PreflightUnavailable):
        raise ProgramPreparationError("preflight_unavailable") from None
    reason = {"missing": "cwd_missing", "not_directory": "cwd_not_directory",
              "not_accessible": "permission_denied"}.get(state["state"])
    if reason:
        raise ProgramPreparationError(reason)


def prepare_program(script_path: str, args: list[str]):
    if (not isinstance(script_path, str) or not os.path.isabs(script_path)
            or os.path.normpath(script_path) != script_path or "\\" in script_path
            or any(ord(char) < 32 or ord(char) == 127 for char in script_path)
            or not script_path.endswith(".py")
            or not isinstance(args, list) or len(args) > 256
            or any(not isinstance(value, str) or "\x00" in value for value in args)):
        raise ProgramPreparationError("invalid_request")
    parent = None
    try:
        flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parent = os.open("/", flags)
        for part in Path(script_path).parts[1:-1]:
            if stat.S_ISLNK(os.stat(part, dir_fd=parent, follow_symlinks=False).st_mode):
                raise ProgramPreparationError("unsafe_path")
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        name = Path(script_path).name
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(observed.st_mode):
            raise ProgramPreparationError("unsafe_path")
        if not stat.S_ISREG(observed.st_mode):
            raise ProgramPreparationError("source_not_regular")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as original:
            if _preflight_identity(os.fstat(original.fileno())) != _preflight_identity(observed):
                raise ProgramPreparationError("preflight_unavailable")
            source = original.read(MAX_PROGRAM_SOURCE_BYTES + 1)
            if _preflight_identity(os.fstat(original.fileno())) != _preflight_identity(observed):
                raise ProgramPreparationError("preflight_unavailable")
        if len(source) > MAX_PROGRAM_SOURCE_BYTES:
            raise ProgramPreparationError("source_too_large")
    except OSError as error:
        reason = ("script_missing" if error.errno == errno.ENOENT else
                  "permission_denied" if error.errno in {errno.EACCES, errno.EPERM} else
                  "unsafe_path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else
                  "preflight_unavailable")
        raise ProgramPreparationError(reason) from None
    finally:
        if parent is not None:
            os.close(parent)
    try:
        syntax = _source_syntax(source)
    except (ValueError, RecursionError):
        raise ProgramPreparationError("preflight_unavailable") from None
    if syntax["state"] != "ready":
        raise ProgramPreparationError("syntax_error", diagnostic=syntax)
    snapshot, diagnostics = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    snapshot.write(source)
    snapshot.seek(0)
    metadata = {"version": 1, "script_path": script_path,
                "source_digest": hashlib.sha256(source).hexdigest()}
    # Bind review context to exactly the bytes in the anonymous execution
    # snapshot, not a later path read or a reconstruction of file edits.
    # The trusted backend adapter consumes and strips this private field.
    if len(source) <= MAX_REVIEW_SOURCE_BYTES:
        try:
            content = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass
        else:
            metadata["source_snapshot"] = {
                "version": 1, "encoding": "utf-8", "size_bytes": len(source),
                "sha256": metadata["source_digest"], "content": content,
            }
    return snapshot, diagnostics, metadata


def program_feedback(shell: dict) -> dict | None:
    metadata = shell.get("program_execution")
    if metadata is None:
        return None
    result = dict(metadata)
    returncode = shell["process"].returncode
    result.update(returncode=returncode, output_truncated=bool(shell.get("output_truncated")),
                  failure_fingerprint=None, diagnostic=None)
    diagnostic_file = shell.get("program_diagnostics")
    if returncode is not None and diagnostic_file is not None:
        try:
            diagnostic_file.seek(0)
            diagnostic = json.loads(diagnostic_file.read(4096))
            if isinstance(diagnostic, dict):
                result["diagnostic"] = diagnostic
        except (OSError, ValueError):
            pass
        finally:
            diagnostic_file.close()
            shell["program_diagnostics"] = None
        shell["program_diagnostic_result"] = result["diagnostic"]
    elif returncode is not None:
        result["diagnostic"] = shell.get("program_diagnostic_result")
    if returncode not in (None, 0):
        diagnostic = result["diagnostic"] or {}
        # A line can shift with a harmless edit. Fingerprint the observed error
        # class/function plus process status, not source version or line number.
        identity = {"returncode": returncode,
                    "exception_type": diagnostic.get("exception_type"),
                    "function": diagnostic.get("function"),
                    "message_fingerprint": diagnostic.get("message_fingerprint")}
        result["failure_fingerprint"] = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
    return result
