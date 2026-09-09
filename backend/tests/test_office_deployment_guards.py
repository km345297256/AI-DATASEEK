"""Office remains an optional, isolated local read-only deployment."""

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
def test_local_office_secrets_are_excluded(ignore_file):
    patterns = (ROOT / ignore_file).read_text().splitlines()
    assert ".local/" in patterns


@pytest.mark.parametrize("service", ["onlyoffice", "office-gateway"])
def test_office_services_require_an_explicit_profile(service):
    compose = (ROOT / "docker-compose.yml").read_text()
    block = compose.split(f"\n  {service}:\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line and not line.startswith("    ") and not line.lstrip().startswith("#"):
            break
        lines.append(line)
    assert "    profiles: [office]" in lines
    assert not any(line.strip() == "ports:" for line in lines)


def test_onlyoffice_plugin_is_readonly_prepare_without_changing_existing_default_view():
    plugin = json.loads((ROOT / "plugin-host/visualizations/onlyoffice.json").read_text())
    assert plugin["default_enabled"] is False
    assert plugin["priority"] < json.loads((ROOT / "plugin-host/visualizations/word.json").read_text())["priority"]
    assert plugin["permissions"] == ["file:read"]
    assert plugin["capabilities"]["operations"] == ["prepare"]


def test_document_server_has_no_app_network_mounts_or_generic_gateway_proxy():
    compose = (ROOT / "docker-compose.yml").read_text()
    onlyoffice = compose.split("\n  onlyoffice:\n", 1)[1].split("\n  office-gateway:\n", 1)[0]
    assert "networks: [onlyoffice-internal]" in onlyoffice
    assert "volumes:" not in onlyoffice and "docker.sock" not in onlyoffice
    assert "dns: [127.0.0.1]" in onlyoffice
    assert "/var/lib/onlyoffice/documentserver/App_Data/cache/files:rw,nosuid,nodev,noexec,size=512m" in onlyoffice
    assert "internal: true" in compose
    assert "9.3.1.2@sha256:290a0a1406a485acc8a6bc20418ac7158c5eec78b4844c218f8f975a9800ddbc" in onlyoffice
    gateway = (ROOT / "deploy/office-gateway/nginx.conf.template").read_text()
    assert gateway.count("location / { return 404; }") == 2
    assert "proxy_pass $backend/api/v1/office-viewer/private/files/$lease" in gateway
    assert "proxy_set_header X-Office-Gateway" in gateway
    assert "proxy_set_header Range '';" in gateway


def test_frontend_virtual_host_never_exposes_application_api_or_same_origin_office():
    nginx = (ROOT / "frontend/nginx.conf").read_text()
    office_host = nginx.split("server_name office.localhost;", 1)[1].split("listen       80 default_server;", 1)[0]
    assert "location / { return 404; }" in office_host
    assert "if ($http_host !~* ^office" in office_host
    assert "location /office-viewer/ { return 404; }" in nginx
    assert "location ^~ /api/v1/office-viewer/private/" in nginx
    assert "if ($office_origin_blocked) { return 403; }" in nginx


def test_frontend_access_log_does_not_record_office_capability_paths():
    nginx = (ROOT / "frontend/nginx.conf").read_text()
    assert "map $uri $office_access_loggable" in nginx
    assert "~*^/(api/v1/)?office-viewer(/|$) 0;" in nginx
    assert "access_log  /var/log/nginx/access.log  main if=$office_access_loggable;" in nginx
