"""Fixed empty-engine readiness probe, never a user-file reader or SQL console.

No arguments, stdin, host files or caller credentials. The fresh SA secret is
generated in memory and is never emitted or passed on a command line. The
container independently enforces the total lifetime even if this process hangs.
"""
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import time

SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
QUERY = "SET NOCOUNT ON; SELECT CONVERT(varchar(32), SERVERPROPERTY('ProductVersion'));"


def diagnostic(log):
    # Read only a bounded tail and emit a fixed code, never the original log.
    length = log.seek(0, os.SEEK_END)
    log.seek(max(0, length - 65536))
    raw = log.read(65536).lower()
    for marker, code in ((b"invalid mapping of address", "unsupported_virtual_address_layout"),
                         (b"permission denied", "engine_permission_denied"),
                         (b"insufficient memory", "engine_memory_unavailable"),
                         (b"unable to allocate", "engine_memory_unavailable")):
        if marker in raw:
            return code
    return "engine_unavailable_or_startup_timeout"


def version_from_output(raw):
    if not isinstance(raw, bytes) or len(raw) > 128:
        raise ValueError("Invalid readiness result")
    value = raw.decode("ascii").strip()
    if not re.fullmatch(r"16\.0\.[0-9]{4,5}\.[0-9]{1,4}", value):
        raise ValueError("Unexpected SQL Server version")
    return value


def main():
    process = None
    log = None
    result = {"ready": False, "experimental": True, "engine": "sqlserver",
              "file_preview_available": False, "user_files_read": 0, "restore_calls": 0}
    try:
        secret = "Ds9!" + secrets.token_hex(24)
        common = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        environment = {**common, "ACCEPT_EULA": "Y", "MSSQL_PID": "Developer",
                       "MSSQL_SA_PASSWORD": secret, "MSSQL_MEMORY_LIMIT_MB": "2048",
                       "MSSQL_TCP_PORT": "1433", "MSSQL_AGENT_ENABLED": "false"}
        log = tempfile.TemporaryFile(dir="/tmp")
        process = subprocess.Popen(["/opt/mssql/bin/sqlservr"], env=environment,
                                   stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=log, start_new_session=True)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline and process.poll() is None:
            try:
                command = subprocess.run([SQLCMD, "-S", "127.0.0.1,1433", "-U", "sa", "-C",
                                          "-l", "2", "-t", "3", "-b", "-h", "-1", "-W", "-Q", QUERY],
                                         env={**common, "SQLCMDPASSWORD": secret},
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, timeout=5, check=False)
                if command.returncode == 0:
                    result["version"] = version_from_output(command.stdout)
                    result["ready"] = True
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(.5)
        if not result["ready"]:
            result["reason"] = diagnostic(log)
    except OSError as error:
        # Numeric errno only: never expose exception strings (paths/secrets).
        result["reason"] = "runtime_os_error"
        result["errno"] = error.errno if type(error.errno) is int else None
    except (ValueError, subprocess.SubprocessError):
        result["reason"] = "runtime_probe_failed"
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        if log is not None:
            log.close()
    print(json.dumps(result), flush=True)
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
