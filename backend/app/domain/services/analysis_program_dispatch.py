"""Conservative redirects for recognizable saved-Python shell invocations.

This is not a shell security parser. Unknown/dynamic shell expressions are left
to normal sandbox authorization; it never infers success or executes a token.
Known saved programs must use the direct runner so pipes cannot hide failures.
"""
from __future__ import annotations

import posixpath
import re
import shlex


_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)?)?$")
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n"})


def _saved_program(argv: list[str], exec_dir: str, depth: int) -> str | None:
    if not argv or depth > 4:
        return None
    # Leading shell variable assignments are a normal non-interactive launch
    # form (e.g. MPLBACKEND=Agg python3 plot.py), not an executable name.
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        argv = argv[1:]
    if not argv:
        return None
    head = posixpath.basename(argv[0])
    if head in {"then", "do", "else", "!", "exec", "command"}:
        return _saved_program(argv[1:], exec_dir, depth + 1)
    if head == "env":
        remaining = argv[1:]
        while remaining and ("=" in remaining[0] and not remaining[0].startswith("-")):
            remaining = remaining[1:]
        if remaining and remaining[0] == "--":
            remaining = remaining[1:]
        return _saved_program(remaining, exec_dir, depth + 1)
    if head == "timeout" and len(argv) > 2 and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", argv[1]):
        return _saved_program(argv[2:], exec_dir, depth + 1)
    if head in {"bash", "sh", "zsh"} and len(argv) >= 3 and argv[1] in {"-c", "-lc"}:
        return _shell_program(argv[2], exec_dir, depth + 1)
    if _PYTHON.fullmatch(head):
        remaining = argv[1:]
        while remaining:
            value = remaining.pop(0)
            if value == "--":
                break
            if value in {"-c", "-m", "-"} or value.startswith(("-c", "-m")):
                return None  # Inline diagnostics/modules are not saved-program launches.
            if value in {"-W", "-X"}:
                if not remaining:
                    return None
                remaining.pop(0)
                continue
            if value.startswith("-"):
                if value not in {"-u", "-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-v", "-q"}:
                    return None
                continue
            remaining.insert(0, value)
            break
        path = remaining[0] if remaining else ""
    else:
        path = argv[0]
    if not path.endswith(".py") or any(marker in path for marker in ("$", "`", "*", "?", "[")):
        return None
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(exec_dir, path))


def _shell_program(command: str, exec_dir: str, depth: int = 0) -> str | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    # Here-document bodies are data, not shell commands. Do not guess their
    # boundaries after shlex has removed delimiter quoting.
    if any(token.startswith("<<") for token in tokens):
        return None
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SEPARATORS or (token and set(token) == {"\n"}):
            found = _saved_program(segment, exec_dir, depth)
            if found:
                return found
            segment = []
        else:
            segment.append(token)
    return None


def shell_saved_program(call: dict) -> str | None:
    if call.get("name") not in {"shell_run", "shell_exec"}:
        return None
    args = call.get("args") or {}
    command, exec_dir = args.get("command"), args.get("exec_dir")
    if not isinstance(command, str) or not isinstance(exec_dir, str) or not exec_dir.startswith("/"):
        return None
    return _shell_program(command, exec_dir)


def saved_program_redirect(call: dict) -> str | None:
    if shell_saved_program(call) is None:
        return None
    return (
        "This saved Python program was NOT executed through the shell. Use program_run with its "
        "script_path, exec_dir, and argv as a list of literal program arguments. Do not add pipes, tail, or ls to the "
        "execution: the host captures bounded stdout/stderr and the program's actual exit status. "
        "Use separate read-only inspection for files or logs."
    )
