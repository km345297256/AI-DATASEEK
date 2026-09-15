"""Check the existing managed data volume against privately frozen input bytes."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from .environment import _docker_python, _run, DEFAULT_BACKEND_CONTAINER, EnvironmentInspectionError
from .protocol import validate_task


def verify_api_origin(base_url):
    """Local Docker fingerprints can only attest to that stack's local API."""
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or parts.path not in {"", "/"}:
        raise EnvironmentInspectionError("This pilot attests only to the existing local frontend; remote services require a separate inspector")
    ports = json.loads(_run(["docker", "inspect", "--type", "container", "--format", "{{json .NetworkSettings.Ports}}", "ai-dataseek-frontend-1"]))
    allowed = {int(item["HostPort"]) for item in ports.get("80/tcp", []) if item.get("HostIp") in {"0.0.0.0", "127.0.0.1", "::"}}
    if (parts.port or 80) not in allowed:
        raise EnvironmentInspectionError("API port does not match the inspected DataSeek frontend")
    return {"local_frontend_verified": True, "port": parts.port or 80}


def verify_service_inputs(tasks, backend_container=DEFAULT_BACKEND_CONTAINER):
    specs = []
    for task in tasks:
        validate_task(task)
        identities = task.get("metadata", {}).get("input_identities")
        if not identities:
            raise ValueError("input identities required before a live pilot")
        specs.append({"task_id": task["id"], "dataset_id": task["dataset_id"], "files": identities})
    # Runs in a private backend inspector, never in an agent sandbox or model prompt.
    source = '''import hashlib, json
from pathlib import Path
from app.core.config import get_settings
from pymongo import MongoClient
specs = json.loads(SPECS)
s = get_settings()
root = Path(s.dataset_storage_root).resolve(strict=True)
results = []
for spec in specs:
    passed = True
    checked = 0
    try:
        base = root / spec['dataset_id']
        if base.is_symlink() or base.resolve(strict=True).parent != root:
            raise ValueError('invalid managed dataset')
        for identity in spec['files']:
            name = identity['name']
            path = base / name
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(base):
                raise ValueError('invalid file')
            content = path.read_bytes()
            passed = passed and len(content) == identity['size'] and hashlib.sha256(content).hexdigest() == identity['sha256']
            checked += 1
    except Exception:
        passed = False
    results.append({'task_id':spec['task_id'],'matched':passed,'checked_files':checked})
client = MongoClient(s.mongodb_uri, serverSelectionTimeoutMS=5000)
try:
    user = client[s.mongodb_database]['users'].find_one({'user_id':'anonymous'}, {'_id':0,'auto_enabled_skills':1})
    if user is None:
        user = client[s.mongodb_database]['users'].find_one({'id':'anonymous'}, {'_id':0,'auto_enabled_skills':1})
    auto_skills = (user or {}).get('auto_enabled_skills', [])
finally:
    client.close()
print(json.dumps({'inputs':results,'all_inputs_match':all(x['matched'] for x in results),'auto_enabled_skills':auto_skills}))
'''.replace("SPECS", repr(json.dumps(specs, ensure_ascii=False)))
    result = _docker_python(source, backend_container, 60)
    if not isinstance(result, dict) or not isinstance(result.get("inputs"), list):
        raise EnvironmentInspectionError("Invalid input verification response")
    if {item.get("task_id") for item in result["inputs"]} != {t["id"] for t in tasks}:
        raise EnvironmentInspectionError("Input verification scope mismatch")
    if result.get("all_inputs_match") is not True:
        raise EnvironmentInspectionError("Managed dataset bytes differ from frozen oracle inputs")
    if result.get("auto_enabled_skills"):
        raise EnvironmentInspectionError("Default user has auto-enabled skills; freeze an explicit controlled variant")
    return result
