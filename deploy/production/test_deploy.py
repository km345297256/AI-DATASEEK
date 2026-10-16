"""Offline contract tests: python3 -m unittest discover -s deploy/production -v."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("production_deploy", Path(__file__).with_name("deploy.py"))
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
REPO = Path(__file__).resolve().parents[2]


def manifest():
    return {kind: {"reference": "ai-dataseek-" + kind + ":20261012-test", "id": "sha256:" + str(index) * 64}
            for index, kind in enumerate(guard.KINDS, 1)}


def configuration(images):
    services = {kind if kind in {"frontend", "backend"} else kind + "-image": {"image": images[kind]["reference"]}
                for kind in guard.KINDS}
    services["frontend"]["ports"] = [{"published": "8888", "target": 80, "protocol": "tcp"}]
    services["backend"]["environment"] = {"AUTH_PROVIDER": "none", "SERVER_HOST": guard.ORIGIN,
                                            "SANDBOX_IMAGE": images["sandbox"]["reference"],
                                            "JUPYTER_IMAGE": images["jupyter"]["reference"]}
    services.update({kind: {"image": images.get(kind, {}).get("reference", kind + ":test")} for kind in guard.INFRA_KINDS})
    services["backend"]["environment"].update({"DATASET_MANAGED_VOLUME": guard.volume_name("dataset-data"),
        "JUPYTER_WORKSPACES_VOLUME": guard.volume_name("jupyter-workspaces"),
        "JUPYTER_WORKSPACES_HOST_ROOT": "/data2/state/jupyter", "JUPYTER_WORKSPACES_STORAGE_ROOT": "/data/jupyter-workspaces"})
    for service, mounts in guard.PERSISTENT_MOUNTS.items():
        services[service]["volumes"] = [{"target": target, "type": "volume", "source": key} for target, key in mounts.items()]
    return {"name": "ai-dataseek", "services": services,
            "volumes": {key: {"name": guard.volume_name(key), "external": True} for key in guard.STATE_VOLUMES}}


def persistent_volumes():
    return [{"Name": guard.volume_name(key), "Driver": "local", "Options": {
        "type": "none", "o": "bind", "device": "/var/lib/snapd/hostfs/data2/state/" + directory}}
        for key, directory in guard.STATE_VOLUMES.items()]


def container(service, images, release, *, unmanaged=False):
    labels = {"com.docker.compose.project": "ai-dataseek", "com.docker.compose.service": service}
    if not unmanaged:
        labels.update({"com.docker.compose.config-hash": "abc", "com.docker.compose.container-number": "1",
                       "com.docker.compose.project.working_dir": str(release)})
    bindings = {"80/tcp": [{"HostPort": "8888", "HostIp": "0.0.0.0"}]} if service == "frontend" else {}
    return {"Id": ("a" if service == "frontend" else "b") * 64, "Name": "/ai-dataseek-" + service + "-1",
            "Image": images[service]["id"], "Mounts": [{"Destination": target, "Type": "volume", "Name": guard.volume_name(key)}
                for target, key in guard.PERSISTENT_MOUNTS.get(service, {}).items()], "State": {"Running": True},
            "Config": {"Labels": labels, "Env": [key + "=" + value for key, value in
                                                  configuration(images)["services"]["backend"]["environment"].items()]},
            "HostConfig": {"PortBindings": copy.deepcopy(bindings)}, "NetworkSettings": {"Ports": bindings}}


class ProductionGuardTests(unittest.TestCase):
    def setUp(self):
        self.images = manifest()
        self.config = configuration(self.images)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = self.root / "releases" / "test"
        self.release.mkdir(parents=True)

    def production_fixture(self):
        release = self.release
        current = self.root / "current"
        current.symlink_to(release)
        lock = self.root / "production.lock"
        shell = (REPO / "run.sh").read_text().replace("/data2/releases", str(release.parent))
        shell = shell.replace("/data2/current", str(current))
        (release / "run.sh").write_text(shell)
        (release / "run.sh").chmod(0o755)
        helper = release / "deploy" / "production" / "deploy.py"
        helper.parent.mkdir(parents=True)
        source = Path(guard.__file__).read_text()
        for key, path in (("CURRENT", current), ("RELEASES", release.parent), ("LOCK", lock)):
            source = source.replace(key + " = Path(" + json.dumps(str(getattr(guard, key))) + ")",
                                    key + " = Path(" + repr(str(path)) + ")")
        helper.write_text(source)
        (release / ".env").touch()
        (release / "release-images.json").write_text(json.dumps(self.images))
        metadata = self.root / "metadata.json"
        metadata.write_text(json.dumps({"config": self.config, "volumes": persistent_volumes(),
                                        "images": [{"Id": self.images[k]["id"]} for k in guard.KINDS],
                                        "containers": [container(s, self.images, release) for s in ("frontend", "backend")]}))
        log = self.root / "docker-calls.jsonl"
        docker = self.root / "docker"
        docker.write_text("#!" + sys.executable + "\n" +
                          "import json,sys\nfrom pathlib import Path\n" +
                          "args=sys.argv[1:]\n" +
                          "with open(" + repr(str(log)) + ",'a') as f: f.write(json.dumps(args)+'\\n')\n" +
                          "data=json.loads(Path(" + repr(str(metadata)) + ").read_text())\n" +
                          "if args[:2]==['image','inspect']: print(json.dumps(data['images']))\n" +
                          "elif args[:2]==['container','inspect']: print(json.dumps(data['containers']))\n" +
                          "elif args[:2]==['volume','inspect']: print(json.dumps(data['volumes']))\n" +
                          "elif 'config' in args: print(json.dumps(data['config']))\n" +
                          "elif 'ps' in args: print('a'*64+'\\n'+'b'*64)\n")
        docker.chmod(0o755)
        env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"])
        env.pop(guard.LOCK_FD_ENV, None)
        return current, lock, log, env

    def test_port_and_public_access_contract(self):
        guard.validate_config(self.config, self.images)
        for mutation in (lambda c: c["services"]["frontend"]["ports"][0].update(published="7000"),
                         lambda c: c["services"]["backend"]["environment"].update(AUTH_PROVIDER="sso"),
                         lambda c: c["services"]["backend"]["environment"].update(SERVER_HOST="http://39.106.98.67:8888"),
                         lambda c: c["services"]["backend"].update(ports=[{"published": "8000", "target": 8000}])):
            bad = copy.deepcopy(self.config)
            mutation(bad)
            with self.subTest(config=bad), self.assertRaises(guard.DeploymentError):
                guard.validate_config(bad, self.images)

    def test_v2_names_preserve_project_services_and_reject_other_names(self):
        for service in guard.STACK_SERVICES:
            self.config["services"][service]["container_name"] = guard.v2_container_name(service)
        guard.validate_config(self.config, self.images)
        names = guard.container_names(self.config)
        self.assertEqual(names["frontend"], "DataSeek-v2")
        containers = [container(service, self.images, self.release) for service in ("frontend", "backend")]
        for item in containers:
            service = item["Config"]["Labels"]["com.docker.compose.service"]
            item["Name"] = "/" + names[service]
        guard.validate_runtime(containers, self.images, self.release, names=names)
        containers[0]["Name"] = "/ai-dataseek-frontend-1"
        with self.assertRaisesRegex(guard.DeploymentError, "wrong configured name"):
            guard.validate_runtime(containers, self.images, self.release, names=names)
        self.config["services"]["frontend"]["container_name"] = "another-application"
        with self.assertRaisesRegex(guard.DeploymentError, "naming scheme"):
            guard.validate_config(self.config, self.images)

    def test_stop_verifies_both_old_and_v2_fixed_names(self):
        for name in (guard.legacy_container_name("frontend"), guard.v2_container_name("frontend")):
            item = container("frontend", self.images, self.release)
            item["Name"] = "/" + name
            def run(args):
                return item["Id"] if args[2] == "ls" else json.dumps([item])
            with self.subTest(name=name), self.assertRaisesRegex(guard.DeploymentError, "still running"):
                guard.verify_stopped(run, ("frontend",))
            item["State"]["Running"] = False
            guard.verify_stopped(run, ("frontend",))

    def test_v2_repair_rejects_foreign_container_before_any_mutation(self):
        item = container("frontend", self.images, self.release, unmanaged=True)
        item["Name"] = "/DataSeek-v2"
        guard.validate_repair_target(item, self.images, name="DataSeek-v2")
        with self.assertRaises(guard.DeploymentError):
            guard.validate_repair_target(item, self.images)
        for label, value in (("com.docker.compose.project", "other"),
                             ("com.docker.compose.service", "backend")):
            bad = copy.deepcopy(item)
            bad["Config"]["Labels"][label] = value
            calls = []
            def run(args):
                calls.append(args)
                return bad["Id"] if args[2] == "ls" else json.dumps([bad])
            with self.subTest(label=label), self.assertRaises(guard.DeploymentError):
                guard.repair_frontend(run, self.images, self.release, name="DataSeek-v2")
            self.assertEqual([args[2] for args in calls], ["ls", "inspect"])

    def test_latest_and_implicit_tags_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "release-images.json"
            for reference in ("ai-dataseek-frontend:latest", "registry:5000/frontend", "frontend:LATEST"):
                bad = copy.deepcopy(self.images)
                bad["frontend"]["reference"] = reference
                path.write_text(json.dumps(bad))
                with self.subTest(reference=reference), self.assertRaises(guard.DeploymentError):
                    guard.load_manifest(path)
            path.write_text(json.dumps(self.images))
            self.assertEqual(guard.load_manifest(path), self.images)

    def test_image_identity_is_checked_not_only_the_tag(self):
        images = [{"Id": self.images[k]["id"]} for k in guard.KINDS]
        guard.validate_images(images, self.images)
        images[2]["Id"] = "sha256:" + "f" * 64
        with self.assertRaises(guard.DeploymentError):
            guard.validate_images(images, self.images)

    def test_runtime_checks_real_active_binding_and_compose_identity(self):
        release = self.release
        containers = [container(s, self.images, release) for s in ("frontend", "backend")]
        guard.validate_runtime(containers, self.images, release)
        containers[0]["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"] = "7000"
        with self.assertRaises(guard.DeploymentError):
            guard.validate_runtime(containers, self.images, release)
        containers[0] = container("frontend", self.images, release, unmanaged=True)
        with self.assertRaises(guard.DeploymentError):
            guard.validate_runtime(containers, self.images, release)

    def test_lock_is_nonblocking_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "production.lock"
            code = ("import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); "
                    "print('ready',flush=True); sys.stdin.readline()")
            child = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(guard.DeploymentError, "holds the lock"):
                    with guard.deployment_lock(lock):
                        self.fail("concurrent publisher acquired the lock")
            finally:
                child.communicate("done\n", timeout=5)
            with guard.deployment_lock(lock):
                pass

    def test_frontend_only_keeps_verified_backend_from_previous_release(self):
        release = self.release
        (release.parent / "previous-version").mkdir()
        containers = [container("frontend", self.images, release),
                      container("backend", self.images, release.parent / "previous-version")]
        guard.validate_runtime(containers, self.images, release, ("frontend",))
        containers[1]["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/root/AI-DataSeek"
        with self.assertRaises(guard.DeploymentError):
            guard.validate_runtime(containers, self.images, release, ("frontend",))

    def test_current_symlink_label_resolves_to_approved_release(self):
        current = self.root / "current"
        current.symlink_to(self.release)
        containers = [container(s, self.images, current) for s in ("frontend", "backend")]
        guard.validate_runtime(containers, self.images, self.release)
        outside = self.root / "outside"
        outside.mkdir()
        current.unlink()
        current.symlink_to(outside)
        with self.assertRaises(guard.DeploymentError):
            guard.validate_runtime(containers, self.images, self.release)
        current.unlink()
        current.symlink_to(self.root / "missing")
        with self.assertRaises(guard.DeploymentError):
            guard.validate_runtime(containers, self.images, self.release)

    def test_http_uses_registered_dataset_route_and_real_response_shape(self):
        def response(content_type, payload=None):
            value = MagicMock()
            value.__enter__.return_value = value
            value.status = 200
            value.headers.get_content_type.return_value = content_type
            value.read.return_value = json.dumps(payload).encode()
            return value
        good = {"code": 0, "data": {"datasets": [{"id": "dataset-id"}], "total": 1}}
        opener = MagicMock()
        opener.open.side_effect = [response("text/html"), response("application/json", good),
                                   response("text/html"), response("application/json", good)]
        with patch.object(guard, "build_opener", return_value=opener):
            guard.verify_http()
        self.assertEqual([call.args[0] for call in opener.open.call_args_list],
                         [base + path for base in (guard.DIRECT, guard.ORIGIN)
                          for path in ("/", "/api/v1/datasets/manage?limit=1&offset=0")])
        invalid = [{"code": 0, "data": {"auth_provider": "none"}},
                   {"code": 0, "data": {"datasets": {}, "total": 1}}]
        invalid.extend({"code": 0, "data": {"datasets": [], "total": value}} for value in (True, -1, "1", 1.5))
        for payload in invalid:
            opener.open.side_effect = [response("text/html"), response("application/json", payload)]
            with self.subTest(payload=payload), patch.object(guard, "build_opener", return_value=opener), \
                    self.assertRaises(guard.DeploymentError):
                guard.verify_http()

    def test_apply_uses_one_locked_environment_and_only_requested_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            releases = root / "releases"
            release = releases / "test-version"
            release.mkdir(parents=True)
            (release / "run.sh").touch()
            (release / ".env").touch()
            (release / "release-images.json").write_text(json.dumps(self.images))
            current = root / "current"
            current.symlink_to(release)
            lock = root / "deployment.lock"
            lock_context = guard.deployment_lock
            calls = []
            def execute(args, *, cwd, env, **kwargs):
                self.assertEqual(cwd, release)
                self.assertEqual(env["PWD"], str(release))
                self.assertEqual(kwargs["pass_fds"], (int(env[guard.LOCK_FD_ENV]),))
                self.assertEqual(env["DOCKER_HOST"], "unix:///var/run/docker.sock")
                self.assertEqual(env["COMPOSE_REMOVE_ORPHANS"], "false")
                self.assertNotIn("COMPOSE_ENV_FILES", env)
                self.assertNotIn("DOCKER_CONTEXT", env)
                with self.assertRaises(guard.DeploymentError):
                    with lock_context(lock):
                        self.fail("publication was not locked")
                calls.append(args)
                if args[:3] == ["docker", "image", "inspect"]:
                    return json.dumps([{"Id": self.images[k]["id"]} for k in guard.KINDS])
                if args[:3] == ["docker", "volume", "inspect"]:
                    return json.dumps(persistent_volumes())
                if args[:3] == ["docker", "container", "inspect"]:
                    return json.dumps([container(s, self.images, release) for s in ("frontend", "backend")])
                self.assertEqual(args[:3], ["./run.sh", "--env-file", ".env"])
                if args[3] == "config":
                    return json.dumps(self.config)
                if args[3] == "ps":
                    return "a" * 64 + "\n" + "b" * 64
                return ""
            with patch.multiple(guard, CURRENT=current, RELEASES=releases), \
                    patch.object(guard, "deployment_lock", lambda: lock_context(lock)), \
                    patch.object(guard, "command", execute), patch.object(guard, "verify_http") as http, \
                    patch.dict(os.environ, {"COMPOSE_REMOVE_ORPHANS": "true", "COMPOSE_ENV_FILES": "/wrong/env",
                                            "DOCKER_CONTEXT": "wrong"}), patch("sys.stdout", new_callable=io.StringIO):
                guard.deploy(apply=True, services=("frontend",))
            self.assertEqual([c for c in calls if "up" in c],
                             [["./run.sh", "--env-file", ".env", "up", "-d", "--no-deps", "--no-build", "--pull", "never", "frontend"]])
            self.assertEqual(len([c for c in calls if c[:3] == ["docker", "image", "inspect"]]), 2)
            http.assert_called_once()

    def test_production_ordinary_mutations_and_forged_marker_never_run_docker(self):
        _, lock, log, env = self.production_fixture()
        for args in (["up", "-d", "frontend"], ["down"], ["--env-file", ".env", "up", "-d"],
                     ["config", "--output", "unexpected.yml"], ["config", "--lock-image-digests=true"]):
            with self.subTest(args=args):
                result = subprocess.run(["bash", "run.sh", *args], cwd=self.release,
                                        env=dict(env, FRONTEND_PORT="7000", **{guard.LOCK_FD_ENV: "999"}),
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("./run.sh production", result.stderr)
                self.assertFalse(log.exists())
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            result = subprocess.run(["bash", "run.sh", "up", "-d"], cwd=self.release,
                                    env=dict(env, **{guard.LOCK_FD_ENV: str(fd)}), pass_fds=(fd,),
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, "an open but unlocked descriptor must not authorize publication")
            self.assertFalse(log.exists())
        finally:
            os.close(fd)

    def test_production_queries_remain_available_without_lock(self):
        _, _, log, env = self.production_fixture()
        for action in ("config", "ps", "logs", "images", "version"):
            with self.subTest(action=action):
                result = subprocess.run(["bash", "run.sh", "--env-file", ".env", action], cwd=self.release,
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(log.read_text().splitlines()), 10)

    def test_snap_compose_client_keeps_production_directory_and_does_not_bypass_guard(self):
        _, _, log, env = self.production_fixture()
        client = self.root / "compose-client"
        client.write_text("#!" + sys.executable + "\nimport json,os,sys\nprint(json.dumps([os.getcwd(),sys.argv[1:]]))\n")
        client.chmod(0o755)
        script = self.release / "run.sh"
        script.write_text(script.read_text().replace("/snap/bin/docker", str(self.root / "docker"))
                          .replace("/snap/docker/current/usr/libexec/docker/cli-plugins/docker-compose", str(client)))
        query = subprocess.run(["bash", "run.sh", "config", "--quiet"], cwd=self.release,
                               env=env, capture_output=True, text=True)
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertEqual(json.loads(query.stdout), [str(self.release), ["-f", "docker-compose.yml", "config", "--quiet"]])
        self.assertFalse(log.exists(), "the Snap launcher must not execute")
        mutation = subprocess.run(["bash", "run.sh", "up", "-d"], cwd=self.release,
                                  env=env, capture_output=True, text=True)
        self.assertNotEqual(mutation.returncode, 0)
        self.assertEqual(mutation.stdout, "")
        self.assertIn("./run.sh production", mutation.stderr)
        self.assertFalse(log.exists())

    def test_snap_direct_docker_client_selection_is_fixed_and_conditional(self):
        for available, expected in ((True, "/snap/docker/current/bin/docker"), (False, "docker")):
            with patch.object(guard.shutil, "which", return_value="/snap/bin/docker"), \
                    patch.object(guard.os, "access", return_value=available), \
                    patch.object(guard.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as execute:
                self.assertEqual(guard.command(["docker", "version"], cwd=self.release, env={"PATH": "/snap/bin"}), "ok")
                self.assertEqual(execute.call_args.args[0], [expected, "version"])

    def test_logical_production_alias_cannot_escape_entry_restriction(self):
        current, _, _, env = self.production_fixture()
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.release.parent / "outside-alias"
        alias.symlink_to(outside)
        with patch.multiple(guard, CURRENT=current, RELEASES=self.release.parent), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(guard.DeploymentError):
                guard.check_compose_entry(alias, ["up", "-d"])
            guard.check_compose_entry(alias, ["ps"])
            guard.check_compose_entry(outside, ["up", "-d"])

    def test_data2_storage_requires_external_names_and_backend_mount_contract(self):
        guard.validate_storage_config(self.config)
        for mutate in (lambda c: c["volumes"]["dataset-data"].update(name="ai-dataseek-dataset-data"),
                       lambda c: c["volumes"]["redis-data"].update(external=False),
                       lambda c: c["services"]["backend"]["environment"].update(DATASET_MANAGED_VOLUME="old-data"),
                       lambda c: c["services"]["backend"]["environment"].update(JUPYTER_WORKSPACES_HOST_ROOT="/root/jupyter"),
                       lambda c: c["services"]["mongodb"]["volumes"][0].update(source="legacy-data"),
                       lambda c: c["services"]["backend"]["volumes"][0].update(type="bind")):
            bad = copy.deepcopy(self.config)
            mutate(bad)
            with self.assertRaises(guard.DeploymentError):
                guard.validate_storage_config(bad)

    def test_data2_volume_inspection_rejects_legacy_devices_and_driver_options(self):
        volumes = persistent_volumes()
        guard.validate_volumes(volumes)
        native = copy.deepcopy(volumes)
        for volume in native:
            volume["Options"]["device"] = volume["Options"]["device"].replace("/var/lib/snapd/hostfs", "")
        guard.validate_volumes(native)
        for mutate in (lambda v: v[0]["Options"].update(device="/root/data"),
                       lambda v: v[0]["Options"].update(device="/data2/state/mongodb/../../root"),
                       lambda v: v[0].update(Driver="nfs"), lambda v: v[0]["Options"].update(o="bind,ro"),
                       lambda v: v[0].update(Name=v[1]["Name"]), lambda v: v.pop()):
            bad = copy.deepcopy(volumes)
            mutate(bad)
            with self.assertRaises(guard.DeploymentError):
                guard.validate_volumes(bad)
        candidate = container("backend", self.images, self.release)
        candidate["Mounts"][0]["Name"] = "ai-dataseek-dataset-data"
        with self.assertRaises(guard.DeploymentError):
            guard.validate_persistent_mounts(candidate, "backend")

    def test_stop_requires_a_separate_explicit_operation(self):
        for options in ({"apply": True}, {"services": ("frontend",)}, {"stack": True}, {"repair": True}):
            with self.subTest(options=options), self.assertRaisesRegex(guard.DeploymentError, "Stop cannot"):
                guard.deploy(stop=True, **options)

    def test_stop_runs_under_lock_without_manifest_or_new_volume_requirements(self):
        current, lock, _, env = self.production_fixture()
        (self.release / "release-images.json").unlink()
        stopped = [container(s, self.images, self.release) for s in ("frontend", "backend")]
        for item in stopped:
            item["State"]["Running"] = False
        lock_context = guard.deployment_lock
        for running in (False, True):
            stopped[0]["State"]["Running"] = running
            calls = []
            def execute(args, **kwargs):
                calls.append(args)
                with self.assertRaises(guard.DeploymentError):
                    with lock_context(lock):
                        self.fail("stop was not locked")
                self.assertEqual(kwargs["pass_fds"], (int(kwargs["env"][guard.LOCK_FD_ENV]),))
                if args[:3] == ["docker", "container", "ls"]:
                    return "a" * 64 + "\n" + "b" * 64 + "\n" + "c" * 64
                if args[:3] == ["docker", "container", "inspect"]:
                    dynamic = {"Id": "c" * 64, "Name": "/ai-dataseek-sandbox-session", "State": {"Running": True},
                               "Config": {"Labels": {"com.docker.compose.project": "ai-dataseek", "com.docker.compose.service": "sandbox-image"}}}
                    return json.dumps([*stopped, dynamic])
                self.assertEqual(args[:3], ["./run.sh", "--env-file", ".env"])
                if args[3] == "config":
                    # A retired port and absent new storage are safe to stop.
                    config = copy.deepcopy(self.config)
                    config.pop("volumes")
                    config["services"]["frontend"]["ports"][0]["published"] = "7000"
                    return json.dumps(config)
                self.assertIn(args[3:], [["stop", "--timeout", "60", "frontend", "backend"],
                    ["stop", "--timeout", "60", *[service for service in guard.STACK_SERVICES if service not in {"frontend", "backend"}]]])
                return ""
            with patch.multiple(guard, CURRENT=current, RELEASES=self.release.parent), \
                    patch.object(guard, "deployment_lock", lambda: lock_context(lock)), \
                    patch.object(guard, "command", execute), patch.dict(os.environ, env), \
                    patch.object(guard, "archive_dynamic_sandboxes") as archive, \
                    patch("sys.stdout", new_callable=io.StringIO), self.subTest(running=running):
                if running:
                    with self.assertRaisesRegex(guard.DeploymentError, "still running"):
                        guard.deploy(stop=True)
                    archive.assert_not_called()
                else:
                    guard.deploy(stop=True)
                    archive.assert_called_once()
                    self.assertEqual(archive.call_args.args[1], {})
            self.assertFalse(any("volume" in args or "image" in args or "rm" in args for args in calls))

    def test_stack_requires_explicit_apply_without_service_or_repair_options(self):
        for options in ({}, {"apply": True, "services": ("frontend",)}, {"apply": True, "repair": True}):
            with self.subTest(options=options), self.assertRaisesRegex(guard.DeploymentError, "Full stack"):
                guard.deploy(stack=True, **options)

    def test_stack_manifest_and_configuration_pin_infrastructure(self):
        images = dict(self.images)
        images.update({kind: {"reference": kind + ":20261014-test", "id": "sha256:" + str(index) * 64}
                       for index, kind in enumerate(guard.INFRA_KINDS, 5)})
        path = self.root / "release-images.json"
        path.write_text(json.dumps(self.images))
        with self.assertRaisesRegex(guard.DeploymentError, "incomplete"):
            guard.load_manifest(path, stack=True)
        path.write_text(json.dumps(images))
        self.assertEqual(guard.load_manifest(path, stack=True), images)
        config = configuration(images)
        guard.validate_config(config, images, stack=True)
        for mutate in (lambda c: c["services"]["redis"].update(image="redis:latest"),
                       lambda c: c["services"]["mongodb"].update(ports=[{"published": "27017"}])):
            bad = copy.deepcopy(config)
            mutate(bad)
            with self.assertRaises(guard.DeploymentError):
                guard.validate_config(bad, images, stack=True)
        images["redis"]["reference"] = "redis:latest"
        path.write_text(json.dumps(images))
        with self.assertRaisesRegex(guard.DeploymentError, "non-latest"):
            guard.load_manifest(path, stack=True)

    def test_stack_starts_fixed_services_and_checks_infrastructure_runtime(self):
        current, lock, _, env = self.production_fixture()
        images = dict(self.images)
        images.update({kind: {"reference": kind + ":20261014-test", "id": "sha256:" + str(index) * 64}
                       for index, kind in enumerate(guard.INFRA_KINDS, 5)})
        (self.release / "release-images.json").write_text(json.dumps(images))
        config = configuration(images)
        for service in guard.STACK_SERVICES:
            config["services"][service]["container_name"] = guard.v2_container_name(service)
        names = guard.container_names(config)
        infrastructure = [container(kind, images, self.release) for kind in guard.INFRA_KINDS]
        application = [container(kind, images, self.release) for kind in ("frontend", "backend")]
        for item in [*infrastructure, *application]:
            item["Name"] = "/" + names[item["Config"]["Labels"]["com.docker.compose.service"]]
        for index, item in enumerate(infrastructure, 12):
            item["Id"] = format(index, "x") * 64
        calls = []
        def execute(args, **kwargs):
            calls.append(args)
            if args[:3] == ["docker", "image", "inspect"]:
                self.assertEqual(args[3:], [images[k]["reference"] for k in (*guard.KINDS, *guard.INFRA_KINDS)])
                return json.dumps([{"Id": images[k]["id"]} for k in (*guard.KINDS, *guard.INFRA_KINDS)])
            if args[:3] == ["docker", "volume", "inspect"]:
                return json.dumps(persistent_volumes())
            if args[:3] == ["docker", "container", "inspect"]:
                return json.dumps(infrastructure if args[3] == infrastructure[0]["Id"] else
                                  application)
            if args[3] == "config":
                return json.dumps(config)
            if args[3] == "ps":
                return "\n".join(item["Id"] for item in infrastructure) if args[-3:] == list(guard.INFRA_KINDS) else "a" * 64 + "\n" + "b" * 64
            return ""
        lock_context = guard.deployment_lock
        with patch.multiple(guard, CURRENT=current, RELEASES=self.release.parent), \
                patch.object(guard, "deployment_lock", lambda: lock_context(lock)), \
                patch.object(guard, "command", execute), patch.object(guard, "verify_http"), \
                patch.dict(os.environ, env), patch("sys.stdout", new_callable=io.StringIO):
            guard.deploy(apply=True, stack=True)
        self.assertEqual([c for c in calls if "up" in c],
                         [["./run.sh", "--env-file", ".env", "up", "-d", "--force-recreate", "--no-build", "--pull", "never", *guard.STACK_SERVICES]])
        guard.validate_infrastructure_runtime(infrastructure, images, self.release, names=names)
        for mutate in (lambda c: c[0].update(Image="sha256:" + "f" * 64),
                       lambda c: c[0]["Config"]["Labels"].update({"com.docker.compose.project": "other"}),
                       lambda c: c[0]["State"].update(Running=False),
                       lambda c: c[0]["HostConfig"].update(PortBindings={"27017/tcp": [{"HostPort": "27017"}]})):
            bad = copy.deepcopy(infrastructure)
            mutate(bad)
            with self.assertRaises(guard.DeploymentError):
                guard.validate_infrastructure_runtime(bad, images, self.release, names=names)

    def test_retired_root_tree_blocks_even_an_inherited_lock(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        with patch.object(guard, "LEGACY_ROOTS", (legacy,)), \
                patch.object(guard, "inherited_deployment_lock") as inherited:
            for arguments in (["up", "-d"], ["stop"], ["down"]):
                with self.subTest(arguments=arguments), self.assertRaisesRegex(guard.DeploymentError, "retired"):
                    guard.check_compose_entry(legacy, arguments)
            guard.check_compose_entry(legacy, ["ps"])
            inherited.assert_not_called()

    def test_retired_run_entry_never_dispatches_production(self):
        _, _, log, env = self.production_fixture()
        old_entry = self.root / "legacy"
        old_entry.mkdir()
        (old_entry / "run.sh").write_text((REPO / "run.sh").read_text().replace("/root/AI-DataSeek", str(old_entry)))
        result = subprocess.run(["bash", "run.sh", "production", "--apply"], cwd=old_entry,
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retired", result.stderr)
        self.assertFalse(log.exists())

    def test_apply_reaches_real_entry_check_with_inherited_lock(self):
        current, lock, log, env = self.production_fixture()
        lock_context = guard.deployment_lock
        with patch.multiple(guard, CURRENT=current, RELEASES=self.release.parent), \
                patch.object(guard, "deployment_lock", lambda: lock_context(lock)), \
                patch.object(guard, "verify_http"), patch.dict(os.environ, dict(env, **{guard.LOCK_FD_ENV: "999"})), \
                patch("sys.stdout", new_callable=io.StringIO):
            guard.deploy(apply=True, services=("frontend",))
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([call for call in calls if "up" in call],
                         [["compose", "-f", "docker-compose.yml", "--env-file", ".env", "up", "-d",
                           "--no-deps", "--no-build", "--pull", "never", "frontend"]])

    def test_inherited_lock_requires_exact_file_and_existing_exclusive_owner(self):
        _, lock, _, _ = self.production_fixture()
        lock_context = guard.deployment_lock
        with patch.object(guard, "LOCK", lock):
            with lock_context(lock) as owner:
                with patch.dict(os.environ, {guard.LOCK_FD_ENV: str(owner)}):
                    guard.inherited_deployment_lock()
                other_descriptor = os.open(lock, os.O_RDONLY)
                try:
                    with patch.dict(os.environ, {guard.LOCK_FD_ENV: str(other_descriptor)}), self.assertRaises(guard.DeploymentError):
                        guard.inherited_deployment_lock()
                finally:
                    os.close(other_descriptor)
            with lock_context(self.root / "wrong.lock") as wrong:
                with patch.dict(os.environ, {guard.LOCK_FD_ENV: str(wrong)}), self.assertRaises(guard.DeploymentError):
                    guard.inherited_deployment_lock()
            shared = os.open(lock, os.O_RDONLY)
            try:
                guard.fcntl.flock(shared, guard.fcntl.LOCK_SH | guard.fcntl.LOCK_NB)
                with patch.dict(os.environ, {guard.LOCK_FD_ENV: str(shared)}), self.assertRaises(guard.DeploymentError):
                    guard.inherited_deployment_lock()
            finally:
                os.close(shared)

    def test_child_entry_check_does_not_release_parent_lock(self):
        _, lock, _, env = self.production_fixture()
        with guard.deployment_lock(lock) as owner:
            result = subprocess.run(["bash", "run.sh", "up", "-d", "frontend"], cwd=self.release,
                                    env=dict(env, **{guard.LOCK_FD_ENV: str(owner)}), pass_fds=(owner,),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.assertRaises(guard.DeploymentError):
                with guard.deployment_lock(lock):
                    self.fail("child checker released the parent's exclusive lock")

    def test_repair_requires_explicit_apply_and_frontend_only(self):
        for apply, services in ((False, ("frontend",)), (True, ("backend", "frontend")), (True, ("backend",))):
            with self.subTest(apply=apply, services=services), self.assertRaises(guard.DeploymentError):
                guard.deploy(apply=apply, services=services, repair=True)

    def test_repair_rejects_managed_wrong_image_named_or_mounted_containers(self):
        candidate = container("frontend", self.images, Path("/release"), unmanaged=True)
        guard.validate_repair_target(candidate, self.images)
        for key, value in (("Name", "/other"), ("Image", "sha256:" + "f" * 64),
                           ("Mounts", [{"Type": "volume"}])):
            bad = copy.deepcopy(candidate)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(guard.DeploymentError):
                guard.validate_repair_target(bad, self.images)
        for key in ("com.docker.compose.config-hash", "com.docker.compose.container-number"):
            bad = copy.deepcopy(candidate)
            bad["Config"]["Labels"][key] = "1"
            with self.subTest(key=key), self.assertRaises(guard.DeploymentError):
                guard.validate_repair_target(bad, self.images)

    def test_repair_saves_sanitized_snapshot_before_exact_id_removal(self):
        candidate = container("frontend", self.images, Path("/release"), unmanaged=True)
        candidate["Config"]["Env"].append("SECRET=do-not-save")
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp)
            def run(args):
                calls.append(args)
                if args[2] == "ls":
                    return candidate["Id"]
                if args[2] == "inspect":
                    return json.dumps([candidate])
                snapshots = list((release / ".local" / "production-repairs").glob("*.json"))
                self.assertEqual(len(snapshots), 1)
                self.assertNotIn("do-not-save", snapshots[0].read_text())
                self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)
                return ""
            guard.repair_frontend(run, self.images, release)
        self.assertEqual(calls[-2:], [["docker", "container", "stop", "--time", "30", candidate["Id"]],
                                     ["docker", "container", "rm", candidate["Id"]]])

    def test_run_dispatch_preserves_ordinary_compose_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp)
            (release / "run.sh").write_text((REPO / "run.sh").read_text())
            script = release / "deploy" / "production" / "deploy.py"
            script.parent.mkdir(parents=True)
            script.write_text("import json,sys; print(json.dumps(sys.argv[1:]))")
            docker = release / "docker"
            docker.write_text("#!" + sys.executable + "\nimport json,sys\nif sys.argv[1:] != ['compose','version']: print(json.dumps(sys.argv[1:]))\n")
            docker.chmod(0o755)
            env = dict(os.environ, PATH=str(release) + os.pathsep + os.environ["PATH"])
            prod = subprocess.run(["bash", "run.sh", "production", "--apply", "--services", "frontend"],
                                  cwd=release, env=env, check=True, capture_output=True, text=True)
            normal = subprocess.run(["bash", "run.sh", "config", "--quiet"], cwd=release, env=env,
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(prod.stdout), ["--apply", "--services", "frontend"])
            self.assertEqual(json.loads(normal.stdout), ["compose", "-f", "docker-compose.yml", "config", "--quiet"])


if __name__ == "__main__":
    unittest.main()
