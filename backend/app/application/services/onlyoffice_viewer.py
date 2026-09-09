"""Read-only ONLYOFFICE provider behind opaque, expiring file capabilities.

The document processor has no application-network access. It can only reach a
scoped gateway which can fetch the exact owner/file/version authorized here.
No callback, conversion output save, generic URL proxy or original write exists.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict

from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import _is_private_spill
from app.application.services.scientific_visualization import ScientificPreviewRejected, VisualizationWorkerError
from app.core.config import get_settings
from app.infrastructure.storage.redis import get_redis
from app.interfaces.schemas.file import public_filename

PLUGIN_ID = "viz-onlyoffice"
LEASE_SECONDS = 900
MAX_ACTIVE_LEASES = 16
MAX_SOURCE_FETCHES = 8
MAX_INPUT = 64 * 1024 * 1024
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SECRET = re.compile(r"^[a-f0-9]{64}$")
_INDEX = "visualization:office:leases"
_FORMATS = {
    "docx": "word", "doc": "word", "odt": "word", "rtf": "word", "txt": "word",
    "xlsx": "cell", "xls": "cell", "ods": "cell", "csv": "cell",
    "pptx": "slide", "ppt": "slide", "odp": "slide", "pdf": "pdf",
}


class OfficeLease(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str
    file_id: str
    version: str
    revision: str
    expires_at: int


def _key(token: str) -> str:
    if not _TOKEN.fullmatch(token):
        raise FileNotFoundError("Office lease not found")
    return "visualization:office:lease:" + hashlib.sha256(token.encode()).hexdigest()


class OfficeLeaseStore:
    """Atomic, TTL-bounded coordination across backend workers; no file bytes."""
    async def _client(self):
        redis = get_redis()
        await redis.initialize()
        return redis.client

    async def create(self, token: str, lease: OfficeLease):
        client = await self._client()
        code = await client.eval("""
            redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
            if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
            if not redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4], 'NX') then return 0 end
            redis.call('ZADD', KEYS[1], ARGV[5], KEYS[2])
            redis.call('EXPIRE', KEYS[1], ARGV[4])
            return 1
        """, 2, _INDEX, _key(token), int(time.time()), MAX_ACTIVE_LEASES,
            lease.model_dump_json(), LEASE_SECONDS, lease.expires_at)
        if code != 1:
            raise ScientificPreviewRejected("已打开的办公预览过多，请先关闭一些预览。")

    async def get(self, token: str) -> OfficeLease:
        data = await (await self._client()).get(_key(token))
        if not data:
            raise FileNotFoundError("Office lease not found")
        return OfficeLease.model_validate_json(data)

    async def claim_source(self, token: str):
        client = await self._client()
        key = _key(token)
        accepted = await client.eval("""
            local ttl = redis.call('TTL', KEYS[1])
            if ttl <= 0 then return 0 end
            local n = redis.call('INCR', KEYS[2])
            redis.call('EXPIRE', KEYS[2], ttl)
            if n > tonumber(ARGV[1]) then return 0 end
            return 1
        """, 2, key, key + ":reads", MAX_SOURCE_FETCHES)
        if accepted != 1:
            raise FileNotFoundError("Office lease unavailable")

    async def delete(self, token: str):
        client = await self._client()
        key = _key(token)
        await client.eval("redis.call('DEL', KEYS[2], KEYS[3]); return redis.call('ZREM', KEYS[1], KEYS[2])",
            3, _INDEX, key, key + ":reads")


@lru_cache()
def get_office_lease_store():
    return OfficeLeaseStore()


def configured(settings) -> str:
    if not settings.onlyoffice_enabled or not _SECRET.fullmatch(settings.onlyoffice_jwt_secret) or not _SECRET.fullmatch(settings.onlyoffice_gateway_secret):
        raise VisualizationWorkerError("ONLYOFFICE 本机只读服务未配置，请先完成本机部署。")
    if hmac.compare_digest(settings.onlyoffice_jwt_secret, settings.onlyoffice_gateway_secret):
        raise VisualizationWorkerError("ONLYOFFICE 必须使用两把独立密钥。")
    try:
        origin = urlsplit(settings.onlyoffice_public_origin)
        origin.port  # Reject malformed/out-of-range port before issuing a lease.
    except ValueError:
        raise VisualizationWorkerError("ONLYOFFICE 本机来源配置无效。") from None
    if (origin.scheme not in {"http", "https"} or origin.hostname != "office.localhost"
        or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise VisualizationWorkerError("ONLYOFFICE 仅允许独立的本机 Office 来源。")
    return settings.onlyoffice_public_origin.rstrip("/")


def require_gateway(value: str | None, settings=None):
    settings = settings or get_settings()
    configured(settings)
    if not value or not hmac.compare_digest(value, settings.onlyoffice_gateway_secret):
        raise FileNotFoundError("Office resource not found")


async def check_health():
    try:
        async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
            response = await client.get("http://office-gateway:8080/healthcheck")
        if response.status_code != 200 or response.text.strip() != "true":
            raise ValueError()
    except Exception:
        raise VisualizationWorkerError("ONLYOFFICE 本机服务尚未就绪，请稍后重试。") from None


async def prepare_office_viewer(file_service, catalog, file_id, user_id, plugin, revision, version,
                                *, settings=None, store=None, health=check_health):
    from app.application.services.unified_visualization import _fence, check_output_budget, VisualizationResult
    settings, store = settings or get_settings(), store or get_office_lease_store()
    origin = configured(settings)
    info = await file_service.get_file_info(file_id, user_id)
    if info is None or _is_private_spill(info):
        raise FileNotFoundError("File not found")
    extension = public_filename(info.filename).rsplit(".", 1)[-1].lower()
    if (plugin.id != PLUGIN_ID or plugin.reader != "office-viewer" or "prepare" not in plugin.capabilities.operations
        or not plugin.matches_filename(info.filename or "") or extension not in _FORMATS):
        raise ScientificPreviewRejected("ONLYOFFICE 不支持此文件格式；不接受宏专用格式。")
    if type(info.size) is not int or not 0 < info.size <= min(MAX_INPUT, plugin.limits.max_input_bytes):
        raise ScientificPreviewRejected("办公文档超过 64 MiB 本机预览上限，或文件为空。")
    await health()
    await _fence(file_service, catalog, file_id, user_id, plugin, revision, version)
    token = secrets.token_urlsafe(32)
    lease = OfficeLease(user_id=user_id, file_id=file_id, version=version, revision=revision,
        expires_at=int(time.time()) + LEASE_SECONDS)
    result = VisualizationResult(plugin_id=plugin.id, version=version, revision=revision, kind="resources",
        payload={"provider": "onlyoffice", "frame_url": f"{origin}/office-viewer/frame/{token}",
            "lease": token, "expires_at": lease.expires_at},
        metadata={"mode": "view", "source_writeback": False, "external_network": False},
        warnings=["仅供本机只读查看；不提供编辑、原文件回写、打印或从查看器下载。"])
    check_output_budget(result, plugin)
    await store.create(token, lease)
    try:
        await _fence(file_service, catalog, file_id, user_id, plugin, revision, version)
    except BaseException:
        await store.delete(token)
        raise
    return result


async def authorize_lease(token, file_service, catalog, *, store=None, settings=None):
    from app.application.services.unified_visualization import _fence
    configured(settings or get_settings())
    lease = await (store or get_office_lease_store()).get(token)
    if lease.expires_at <= int(time.time()):
        raise FileNotFoundError("Office lease expired")
    plugin = await catalog.require_enabled(lease.user_id, PLUGIN_ID)
    info = await file_service.get_file_info(lease.file_id, lease.user_id)
    if (info is None or _is_private_spill(info) or plugin.reader != "office-viewer"
        or not plugin.matches_filename(info.filename or "")):
        raise FileNotFoundError("Office document not found")
    if preview_version(info) != lease.version:
        raise PreviewVersionChanged()
    if type(info.size) is not int or not 0 < info.size <= min(MAX_INPUT, plugin.limits.max_input_bytes):
        raise ScientificPreviewRejected("办公文档超过预览上限。")
    await _fence(file_service, catalog, lease.file_id, lease.user_id, plugin, lease.revision, lease.version)
    if lease.expires_at <= int(time.time()):
        raise FileNotFoundError("Office lease expired")
    return lease, plugin, info


def view_configuration(token: str, lease: OfficeLease, info, settings=None):
    settings = settings or get_settings()
    configured(settings)
    extension = public_filename(info.filename).rsplit(".", 1)[-1].lower()
    name = public_filename(info.filename)[:240]
    if extension not in _FORMATS:
        raise ScientificPreviewRejected("不支持此办公格式。")
    permissions = {key: False for key in ("edit", "download", "print", "copy", "comment", "review", "fillForms",
        "modifyContentControl", "modifyFilter", "protect", "chat")}
    config = {
        "documentType": _FORMATS[extension], "type": "desktop", "width": "100%", "height": "100%",
        "document": {"fileType": extension, "key": hashlib.sha256((token + lease.version).encode()).hexdigest(),
            "title": name, "url": f"http://office-gateway:8081/files/{token}", "permissions": permissions},
        "editorConfig": {"mode": "view", "lang": "zh", "region": "zh-CN",
            "user": {"id": hashlib.sha256(lease.user_id.encode()).hexdigest(), "name": "本机只读用户"},
            "coEditing": {"mode": "strict", "change": False},
            "customization": {"autosave": False, "forcesave": False, "macros": False, "macrosMode": "disable",
                "plugins": False, "chat": False, "comments": False, "help": False,
                "feedback": {"visible": False}, "anonymous": {"request": False},
                "hideRightMenu": True, "integrationMode": "embed", "logo": {"url": ""},
                "review": {"trackChanges": False}, "suggestFeature": False,
                "features": {"spellcheck": {"mode": False}}}},
    }
    # No callbackUrl/createUrl/saveAsUrl or plugins/event-provided write hooks.
    config["token"] = jwt.encode({**config, "exp": lease.expires_at, "iat": int(time.time())},
        settings.onlyoffice_jwt_secret, algorithm="HS256")
    return config


def frame_html(config: dict[str, Any], expires_at: int, token: str) -> str:
    _key(token)
    encoded = json.dumps(config, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>本机只读办公预览</title>
<style>html,body,#office{height:100%;width:100%;margin:0;overflow:hidden}body{font:14px sans-serif;background:white}#error{padding:20px;color:#9a3412}</style>
</head><body><div id="office"></div><div id="error" hidden></div>
<script src="/office-viewer/web-apps/apps/api/documents/api.js"></script><script>
const config=""" + encoded + ";\nconst expires=" + str(expires_at) + """;
let editor, stopped=false;
function status(type){parent.postMessage({type}, '*')}
function stop(){if(stopped)return;stopped=true;if(editor)editor.destroyEditor();document.getElementById('error').hidden=false;document.getElementById('error').textContent='预览已结束或授权到期，请重新打开。';status('dataseek-office-expired')}
config.events={onDocumentReady(){status('dataseek-office-ready')},onError(){status('dataseek-office-error')}};
try{editor=new DocsAPI.DocEditor('office',config)}catch(e){status('dataseek-office-error')}
setTimeout(stop,Math.max(0,expires*1000-Date.now()));
const poll=setInterval(async()=>{if(stopped)return;try{const r=await fetch('/office-viewer/status/""" + token + """',{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(5000)});if(!r.ok)stop()}catch(e){stop()}},10000);
window.addEventListener('pagehide',()=>{clearInterval(poll);if(editor)editor.destroyEditor()},{once:true});
</script></body></html>"""
