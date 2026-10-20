"""Conservative redirects for recognizable program preparation and execution.

This is not a shell security parser. Unknown/dynamic shell expressions are left
to normal sandbox authorization; it never infers success or executes a token.
Known saved programs must use the direct runner so pipes cannot hide failures.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from app.domain.services.artifact_write_progress import PROGRAM_SUFFIXES


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
        "Use separate read-only inspection for files or logs. If preparation already reported a missing script "
        "or directory, complete that preparation first; changing runners does not create missing source."
    )


# These are authored source/report formats, not a claim that every runtime or
# format is executable. In particular program_run currently supports Python.
_AUTHORED_SUFFIXES = PROGRAM_SUFFIXES | frozenset({
    ".md", ".markdown", ".rst", ".txt", ".html", ".htm", ".svg",
    ".json", ".yaml", ".yml", ".tex", ".csv", ".tsv", ".doc",
})
_SHELL_OPERATORS = ("<<-", "<<<", "<<", ">>", ">&", "<&", ">|", "&&", "||", "|&", ";;", "&>")


@dataclass(frozen=True)
class _ShellToken:
    value: str
    start: int
    end: int
    operator: bool = False
    dynamic: bool = False
    quoted: bool = False


def _literal_shell_segments(command: str) -> list[tuple[list[_ShellToken], str]] | None:
    """Tokenize a deliberately small shell subset, never execute or expand it.

    Quoted words retain their identity as words, even when their value is `>`.
    Heredoc bodies (quoted or not) are skipped as data. Dynamic substitutions,
    groups and malformed input are outside this recognizer, not new authority.
    """
    segments: list[tuple[list[_ShellToken], str]] = []
    segment: list[_ShellToken] = []
    heredocs: list[tuple[str, bool]] = []
    delimiter_pending: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char in " \t\r":
            index += 1
            continue
        if command.startswith("\\\n", index):
            index += 2
            continue
        if char == "#":
            end = command.find("\n", index)
            index = len(command) if end < 0 else end
            continue
        if char == "\n":
            if delimiter_pending:
                return None
            if segment:
                segments.append((segment, "\n"))
                segment = []
            index += 1
            for delimiter, strip_tabs in heredocs:
                while index < len(command):
                    end = command.find("\n", index)
                    end = len(command) if end < 0 else end
                    line = command[index:end]
                    index = min(end + 1, len(command))
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
                else:
                    return None
            heredocs = []
            continue
        if char in "()":
            return None  # Groups/process substitution require a real shell AST.
        if char in ";<>|&":
            value = next((op for op in _SHELL_OPERATORS if command.startswith(op, index)), char)
            token = _ShellToken(value, index, index + len(value), operator=True)
            index = token.end
            if delimiter_pending:
                return None
            if value in {";", "&&", "||", "|", "|&", "&"}:
                if segment:
                    segments.append((segment, value))
                    segment = []
            else:
                segment.append(token)
                if value in {"<<", "<<-"}:
                    delimiter_pending = value
            continue
        start = index
        value: list[str] = []
        dynamic = False
        quoted = False
        while index < len(command) and command[index] not in " \t\r\n;<>|&()":
            char = command[index]
            if char == "\\":
                quoted = True
                if index + 1 >= len(command):
                    return None
                if command[index + 1] != "\n":
                    value.append(command[index + 1])
                index += 2
            elif char in {"'", '"'}:
                quoted = True
                quote = char
                index += 1
                while index < len(command) and command[index] != quote:
                    char = command[index]
                    if quote == '"' and (char == "`" or command.startswith("$(", index)):
                        return None
                    if quote == '"' and char == "\\" and index + 1 < len(command) and command[index + 1] in '$`"\\\n':
                        if command[index + 1] != "\n":
                            value.append(command[index + 1])
                        index += 2
                    else:
                        dynamic |= quote == '"' and char == "$"
                        value.append(char)
                        index += 1
                if index >= len(command):
                    return None
                index += 1
            elif char == "`" or command.startswith("$(", index):
                return None
            else:
                dynamic |= char in "$*?[~{}"
                value.append(char)
                index += 1
        token = _ShellToken("".join(value), start, index, dynamic=dynamic, quoted=quoted)
        segment.append(token)
        if delimiter_pending:
            if token.dynamic or not token.value:
                return None
            heredocs.append((token.value, delimiter_pending == "<<-"))
            delimiter_pending = None
    if heredocs or delimiter_pending:
        return None
    if segment:
        segments.append((segment, ""))
    return segments


def _authored_target(token: _ShellToken) -> bool:
    return bool(not token.operator and not token.dynamic and token.value
                and posixpath.splitext(token.value)[1].lower() in _AUTHORED_SUFFIXES)


def _shell_authored_write(command: str, depth: int = 0) -> bool:
    if depth > 4:
        return False
    segments = _literal_shell_segments(command)
    if segments is None:
        return False
    incoming_pipe = False
    incoming_authored = False
    for segment, separator in segments:
        from_pipe, from_authored = incoming_pipe, incoming_authored
        incoming_pipe = separator in {"|", "|&"}
        incoming_authored = False
        argv: list[_ShellToken] = []
        stdout_targets: list[_ShellToken] = []
        explicit_input = False
        index = 0
        while index < len(segment):
            token = segment[index]
            if not token.operator:
                argv.append(token)
                index += 1
                continue
            if token.value not in {">", ">>", ">|", "&>", "<", "<<", "<<-", "<<<", ">&", "<&"}:
                return False
            if index + 1 >= len(segment) or segment[index + 1].operator:
                return False
            descriptor = "1"
            if argv and not argv[-1].quoted and argv[-1].value.isdigit() and argv[-1].end == token.start:
                descriptor = argv.pop().value
            target = segment[index + 1]
            if token.value in {">", ">>", ">|", "&>"} and descriptor == "1":
                stdout_targets.append(target)
            if token.value in {"<", "<<", "<<-", "<<<"}:
                explicit_input = True
            index += 2
        while argv and not argv[0].quoted and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0].value):
            argv = argv[1:]
        while argv and not argv[0].dynamic and posixpath.basename(argv[0].value) in {"command", "exec", "env"}:
            head = posixpath.basename(argv[0].value)
            argv = argv[1:]
            if head == "env":
                while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0].value):
                    argv = argv[1:]
            if argv and argv[0].value == "--":
                argv = argv[1:]
        if not argv or argv[0].dynamic:
            continue
        head = posixpath.basename(argv[0].value)
        if head in {"sh", "bash", "zsh"} and len(argv) >= 3 and argv[1].value in {"-c", "-lc"}:
            if not argv[2].dynamic and _shell_authored_write(argv[2].value, depth + 1):
                return True
        authored = head in {"cat", "printf", "echo", "tee"}
        if head in {"cat", "tee"} and from_pipe and not explicit_input:
            # A real analysis command may stream its result through cat/tee.
            # That is output persistence, not preparing new authored content.
            authored = from_authored
            if head == "cat" and any(not token.value.startswith("-") for token in argv[1:]):
                authored = True  # Explicit source-file copy, not just stdin.
        incoming_authored = authored
        if authored and any(_authored_target(target) for target in stdout_targets):
            return True
        if head == "tee" and authored:
            # Only known flags: options that take a value would otherwise turn
            # their arguments into an alleged output filename.
            targets = argv[1:]
            while targets and targets[0].value in {"-a", "--append", "-i", "--ignore-interrupts"}:
                targets = targets[1:]
            if targets and targets[0].value == "--":
                targets = targets[1:]
            elif any(token.value.startswith("-") for token in targets):
                continue
            if any(_authored_target(target) for target in targets):
                return True
    return False


def shell_source_write_redirect(tool, call: dict) -> str | None:
    """Redirect literal source/report shell writes only for resolved core tools.

    This complements file-operation admission; it is not a complete shell write
    detector. Unknown dynamic expressions retain normal sandbox authorization.
    """
    from app.domain.services.analysis_protocol_recovery import _core_shell_method

    if call.get("name") not in {"shell_run", "shell_exec"} or not _core_shell_method(tool, call.get("name")):
        return None
    args = call.get("args")
    if not isinstance(args, dict) or not isinstance(args.get("command"), str):
        return None
    if not _shell_authored_write(args["command"]):
        return None
    return (
        "This shell command was NOT executed. It directly creates or rewrites authored source or a report "
        "using shell text-writing utilities. Prepare the actual content with file_write or file_str_replace "
        "so the existing file revision and progress are tracked consistently; do not switch shell commands "
        "to bypass file preparation. Keep successful outputs unless a concrete change is required. "
        "For Python .py analysis, execute the saved source with program_run. For another language, use only "
        "an available supported runtime after preparing its source; do not pass non-Python files to program_run. "
        "A report needs no program execution. This is a preparation redirect, not evidence that any write ran."
    )


_RUNTIME_SOURCE_SUFFIXES = {
    "rscript": frozenset({".r"}),
    "node": frozenset({".js", ".mjs", ".cjs"}),
    "nodejs": frozenset({".js", ".mjs", ".cjs"}),
    "bash": frozenset({".sh", ".bash"}),
    "sh": frozenset({".sh"}),
    "zsh": frozenset({".sh", ".zsh"}),
    "julia": frozenset({".jl"}),
    "ruby": frozenset({".rb"}),
    "perl": frozenset({".pl"}),
}


def shell_runtime_source(call: dict) -> str | None:
    """Identify one static non-Python runtime launch, never certify execution.

    The caller must separately prove the resolved first-party shell identity
    and an identity-bound successful execution receipt before recording any
    progress. Pipelines, redirections, wrappers, runtime flags before a script
    and expansions deliberately yield no target rather than guessing.
    """
    if call.get("name") not in {"shell_run", "shell_exec"}:
        return None
    args = call.get("args")
    if not isinstance(args, dict):
        return None
    command, exec_dir = args.get("command"), args.get("exec_dir")
    if (not isinstance(command, str) or "\0" in command or not isinstance(exec_dir, str)
            or not exec_dir.startswith("/") or "\0" in exec_dir):
        return None
    segments = _literal_shell_segments(command)
    if segments is None or len(segments) != 1:
        return None
    argv, separator = segments[0]
    if separator not in {"", "\n"} or len(argv) < 2 or any(token.operator or token.dynamic for token in argv):
        return None
    suffixes = _RUNTIME_SOURCE_SUFFIXES.get(posixpath.basename(argv[0].value).lower())
    path = argv[1].value
    if not suffixes or not path or path.startswith("-") or posixpath.splitext(path)[1].lower() not in suffixes:
        return None
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(exec_dir, path))
