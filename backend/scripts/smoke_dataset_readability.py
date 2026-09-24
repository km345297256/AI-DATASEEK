"""Opt-in isolated real-Docker dataset-view permission regression.

Only creates a labelled synthetic volume and no-network short-lived helpers.
Never mounts, resumes or mutates any production dataset/task. Cleans up its own
volume after all read/write-denial checks; no host source paths enter the report.
"""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import docker

from app.infrastructure.external.sandbox.dataset_readability import (
    DatasetReadabilityError, prepare_managed_dataset_source, verify_dataset_readability,
)


def run(docker_host, image, output):
    output = Path(output)
    if not output.is_absolute() or output.exists():
        raise ValueError("Use a new absolute report path")
    client = docker.DockerClient(base_url=docker_host, timeout=60)
    identifier = uuid4().hex
    volume = client.volumes.create(name="dataseek-readability-test-" + identifier,
                                    labels={"dataseek.synthetic-test": identifier})
    report = {"passed": False, "scope": "new_synthetic_docker_volume_only", "checks": {}}

    def helper(source, script, *, user="0:0", kind="volume", read_only=True):
        result = client.containers.run(image, entrypoint="python3", command=["-c", script],
            mounts=[docker.types.Mount(source=source, target="/fixture", type=kind, read_only=read_only)],
            user=user, network_disabled=True, read_only=True, remove=True,
            environment={"PYTHONDONTWRITEBYTECODE": "1"}, security_opt=["no-new-privileges:true"])
        return json.loads(result)

    stats = '''import hashlib,json,os,stat
from pathlib import Path
r=Path('/fixture/restricted')
print(json.dumps({str(p.relative_to(r)):{'mode':stat.S_IMODE(p.stat().st_mode),'hash':hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None} for p in [r,*r.rglob('*')]}))
'''
    try:
        helper(volume.name, '''import json,os
from pathlib import Path
os.umask(0o077)
p=Path('/fixture/restricted/nested');p.mkdir(parents=True)
(p/'sample.data').write_bytes(b'value,label\\n2,A\\n4,B\\n')
os.chmod(p/'sample.data',0o700)
print(json.dumps({'created':True}))
''', read_only=False)
        before = helper(volume.name, stats)
        source = volume.attrs["Mountpoint"] + "/restricted"
        try:
            verify_dataset_readability(client, image=image, source=source)
        except DatasetReadabilityError as exc:
            assert exc.code == "dataset_unreadable", exc.code
            report["checks"]["unreadable_host_source_rejected_before_analysis"] = True
        else:
            raise AssertionError("Restricted source was unexpectedly readable")

        first = prepare_managed_dataset_source(client, image=image, volume=volume.name, dataset_id="restricted")
        second = prepare_managed_dataset_source(client, image=image, volume=volume.name, dataset_id="restricted")
        assert first == second
        report["checks"]["unchanged_source_reuses_same_snapshot"] = True
        report["checks"]["source_bytes_and_permissions_unchanged"] = helper(volume.name, stats) == before
        assert report["checks"]["source_bytes_and_permissions_unchanged"]

        view = helper(first, '''import hashlib,json,os,pwd,stat
from pathlib import Path
r=Path('/fixture');p=r/'nested/sample.data'
assert os.geteuid()==pwd.getpwnam('ubuntu').pw_uid
b=p.read_bytes()
try:
    p.write_bytes(b'forbidden')
except OSError:
    denied=True
else:
    denied=False
assert denied
assert stat.S_IMODE(r.stat().st_mode)==0o555
assert stat.S_IMODE(p.parent.stat().st_mode)==0o555
assert stat.S_IMODE(p.stat().st_mode)==0o444
print(json.dumps({'uid':os.geteuid(),'sha256':hashlib.sha256(b).hexdigest(),'write_denied':denied}))
''', user="ubuntu", kind="bind")
        assert view["sha256"] == hashlib.sha256(b"value,label\n2,A\n4,B\n").hexdigest()
        report["checks"]["actual_ubuntu_can_read_but_not_write"] = view

        helper(volume.name, '''import json
from pathlib import Path
Path('/fixture/restricted/nested/sample.data').write_bytes(b'value,label\\n8,A\\n')
print(json.dumps({'changed_only_synthetic_input':True}))
''', read_only=False)
        third = prepare_managed_dataset_source(client, image=image, volume=volume.name, dataset_id="restricted")
        assert third != first
        old = helper(first, "import json;from pathlib import Path;print(json.dumps(Path('/fixture/nested/sample.data').read_text()))",
                     kind="bind", user="ubuntu")
        assert old == "value,label\n2,A\n4,B\n"
        report["checks"]["source_change_creates_new_view_old_view_unchanged"] = True

        for name, code in (("symlink", "p.symlink_to('/etc/passwd')"),
                           ("hardlink", "os.link('/fixture/restricted/nested/sample.data',p)")):
            helper(volume.name, "import json,os;from pathlib import Path;r=Path('/fixture/" + name + "');r.mkdir();p=r/'unsafe';" + code + ";print(json.dumps(True))", read_only=False)
            try:
                prepare_managed_dataset_source(client, image=image, volume=volume.name, dataset_id=name)
            except DatasetReadabilityError as exc:
                assert exc.code == "dataset_unsafe"
                report["checks"][name + "_rejected"] = True
            else:
                raise AssertionError("Unsafe source accepted")
        report["passed"] = True
    finally:
        volume.reload()
        if volume.attrs.get("Labels", {}).get("dataseek.synthetic-test") == identifier:
            volume.remove()
            report["synthetic_volume_removed"] = True
        client.close()
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--image", default="ai-dataseek-sandbox:latest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.docker_host, args.image, args.output)
