"""No Docker required: optional deployment and probe trust boundaries."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sqlserver_probe", ROOT / "deploy/sqlserver-reader/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_version_accepts_only_one_expected_major(self):
        self.assertEqual(probe.version_from_output(b" 16.0.4265.1\r\n"), "16.0.4265.1")
        for raw in (b"17.0.1234.1", b"16.0.1234.1\nprivate", b"/tmp/file", b"x"*129,
                    b"16.0.1234.1\xff", "16.0.1234.1", b"", b"16.0.1234"):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                probe.version_from_output(raw)

    def run_probe(self, ready=True, malformed=False, timeout=False):
        commands, engine_env, output, signals = [], [], [], []
        class Process:
            pid = 321
            def poll(self): return None if ready else 1
            def wait(self, timeout): return 0
        def popen(command, **kw):
            self.assertEqual(command, ["/opt/mssql/bin/sqlservr"])
            self.assertIs(kw["stdout"], kw["stderr"])
            self.assertTrue(hasattr(kw["stdout"], "fileno"))
            self.assertTrue(kw["start_new_session"])
            engine_env.append(kw["env"])
            return Process()
        def run(command, **kw):
            commands.append((command,kw))
            if timeout:
                raise probe.subprocess.TimeoutExpired(command, 5)
            return SimpleNamespace(returncode=0,stdout=b"PRIVATE" if malformed else b"16.0.4265.1\n")
        ticks=iter([0, 0, 151])
        with patch.object(probe.subprocess,"Popen",popen), patch.object(probe.subprocess,"run",run), \
             patch.object(probe.os,"killpg",lambda *args: signals.append(args)), \
             patch.object(probe,"time",SimpleNamespace(monotonic=lambda:next(ticks),sleep=lambda _:None)), \
             patch("builtins.print",lambda value,**_: output.append(value)):
            code=probe.main()
        self.assertEqual(len(output),1)
        report=json.loads(output[0])
        self.assertEqual(report["restore_calls"],0)
        self.assertEqual(report["user_files_read"],0)
        self.assertFalse(report["file_preview_available"])
        self.assertEqual(signals,[(321,probe.signal.SIGTERM)])
        secret=engine_env[0]["MSSQL_SA_PASSWORD"]
        self.assertGreater(len(secret),32)
        self.assertNotIn(secret,output[0])
        self.assertNotIn("PRIVATE",output[0])
        for cmd,kw in commands:
            self.assertEqual(cmd[-2:],["-Q",probe.QUERY])
            self.assertNotIn(secret," ".join(cmd))
            self.assertEqual(kw["env"]["SQLCMDPASSWORD"],secret)
            self.assertEqual(set(kw["env"]),{"PATH","LANG","SQLCMDPASSWORD"})
            self.assertEqual(kw["timeout"],5)
        return code,report

    def test_success_still_does_not_claim_preview_available(self):
        code,report=self.run_probe()
        self.assertEqual(code,0)
        self.assertTrue(report["ready"])

    def test_exit_and_invalid_version_fail_closed(self):
        for kw in ({"ready":False},{"malformed":True},{"timeout":True}):
            with self.subTest(kw=kw):
                code,report=self.run_probe(**kw)
                self.assertEqual(code,1)
                self.assertFalse(report["ready"])

    def test_compose_has_no_data_network_credentials_or_public_port(self):
        text=(ROOT/"docker-compose.yml").read_text()
        block=text.split("\n  sqlserver-reader-probe:\n",1)[1].split("\n  mongodb:\n",1)[0]
        for expected in ("profiles: [database-readers]","platform: linux/amd64", "network_mode: none",
                         "read_only: true", "cap_drop: [ALL]", "no-new-privileges:true",
                         "mem_limit: 4g", "memswap_limit: 4g", "pids_limit: 256", 'restart: "no"'):
            self.assertIn(expected,block)
        for forbidden in ("ports:","volumes:","env_file:","networks:","docker.sock","privileged:","SA_PASSWORD"):
            self.assertNotIn(forbidden,block)
        self.assertNotIn("sqlserver-reader-probe",text.split("  backend:\n",1)[1].split("  sandbox-image:\n",1)[0])

    def test_pinned_upstream_fixed_deadline_and_no_shell(self):
        text=(ROOT/"deploy/sqlserver-reader/Dockerfile").read_text()
        self.assertIn("2022-CU26-ubuntu-22.04@sha256:ba4c8329f48fb8f02e1416be6a930ebfd71268caee78aa985f3af4315e457c89",text)
        self.assertIn('"--signal=KILL", "180s"',text)
        self.assertIn("USER 10001:0",text)
        self.assertIn("os.removexattr(p, 'security.capability')",text)
        self.assertEqual(probe.QUERY,"SET NOCOUNT ON; SELECT CONVERT(varchar(32), SERVERPROPERTY('ProductVersion'));")

    def test_diagnostic_never_echoes_private_log_content(self):
        import io
        self.assertEqual(probe.diagnostic(io.BytesIO(b"PRIVATE /tmp/file Invalid mapping of address")),
                         "unsupported_virtual_address_layout")
        self.assertEqual(probe.diagnostic(io.BytesIO(b"PRIVATE /tmp/file")),
                         "engine_unavailable_or_startup_timeout")
        self.assertEqual(probe.diagnostic(io.BytesIO(b"Invalid mapping of address" + b"x"*65536)),
                         "engine_unavailable_or_startup_timeout")


if __name__ == "__main__":
    unittest.main()
