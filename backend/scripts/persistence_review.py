"""Extract actual schemas from trusted local Git revisions without checkout.

The selected revisions' Python schema code runs with this interpreter's installed
dependencies. This is not a sandbox for untrusted commits. No store, model,
repository .env, acknowledged snapshot, or current worktree source is consulted.
"""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), "-c", "protocol.allow=never", *args],
        env={"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8",
             "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}, capture_output=True)
    if result.returncode:
        raise ValueError("Local Git revision or source archive is unavailable")
    return result.stdout


def catalog_at_ref(repo: Path, ref: str) -> tuple[str, dict]:
    if not ref or len(ref) > 1024 or "\x00" in ref:
        raise ValueError("Invalid Git revision")
    commit = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("Git revision did not resolve to a commit")
    archive = _git(repo, "archive", "--format=tar", commit, "backend/app")
    with tempfile.TemporaryDirectory(prefix="dataseek-persistence-review-") as scratch:
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            members = source.getmembers()
            for member in members:
                path = Path(member.name)
                if (path.is_absolute() or ".." in path.parts
                        or path.parts[:2] != ("backend", "app")
                        or not (member.isfile() or member.isdir())):
                    # git archive may include the parent backend directory.
                    if not (member.isdir() and member.name.rstrip("/") == "backend"):
                        raise ValueError("Unsupported entry in schema source archive")
            source.extractall(scratch, members=members, filter="data")
        backend = Path(scratch) / "backend"
        code = (
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from app.domain.utils.persistence_contracts import current_catalog;"
            "print(json.dumps(current_catalog(),ensure_ascii=False))"
        )
        # Isolated cwd and environment avoid loading deployment configuration
        # or passing credentials into the selected source revision.
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(backend)],
            cwd=scratch, env={"PATH": os.defpath, "LANG": "C.UTF-8",
                "API_KEY": "offline-schema-review", "API_BASE": "http://127.0.0.1:9"},
            capture_output=True)
        if result.returncode:
            raise ValueError(f"Schema extraction failed for commit {commit}")
        try:
            catalog = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise ValueError(f"Invalid schema extraction output for commit {commit}") from None
    return commit, catalog
