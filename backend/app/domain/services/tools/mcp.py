import asyncio
import copy
import hashlib
import os
import logging
import re
from typing import Dict, Any, List, Mapping, Optional, Tuple
from contextlib import AsyncExitStack

from jsonschema.validators import validator_for
from referencing import Registry

from mcp import ClientSession as _SDKClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientRequest,
    Tool as MCPToolkit,
)

from langchain.messages import ToolMessage
from langchain.tools import tool

from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.pipeline import opaque_log_identifier, summarize_argument_keys
from app.domain.services.tools.spill_projection import (
    sanitize_spill_public_data,
    sanitize_spill_public_text,
)
from app.domain.models.tool_result import ToolResult
from app.domain.models.mcp_config import MCPConfig, MCPServerConfig
from app.domain.services.tools.mcp_images import (
    MCPImageContext, MCPImageToolResult, MCP_IMAGE_READ_TOOL, image_tool_message,
)

logger = logging.getLogger(__name__)


class ClientSession(_SDKClientSession):
    """One wire invocation; output policy belongs to our pinned tool catalog.

    SDK 1.26's convenience call_tool refreshes discovery on a new connection
    and validates with a default JSON Schema resolver. That can change a live
    task's contract and retrieve untrusted external schema URLs. Use the SDK's
    public typed request API instead; MCPClientManager validates exactly once
    with its discovery snapshot and a non-fetching registry.
    """

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> CallToolResult:
        return await self.send_request(
            ClientRequest(CallToolRequest(params=CallToolRequestParams(
                name=name, arguments=arguments,
            ))),
            CallToolResult,
        )


# Stdio MCP servers run as children of the backend process.  Only inherit the
# small set of values needed to locate executables and provide a predictable
# locale/home/temp environment.  Credentials and service configuration must be
# supplied explicitly through MCPServerConfig.env instead of leaking from the
# backend process environment.
_STDIO_INHERITED_ENV_KEYS = frozenset({
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LANGUAGE",
    "LC_ADDRESS",
    "LC_ALL",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_IDENTIFICATION",
    "LC_MEASUREMENT",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NAME",
    "LC_NUMERIC",
    "LC_PAPER",
    "LC_TELEPHONE",
    "LC_TIME",
    "LOCALAPPDATA",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "USERPROFILE",
    "WINDIR",
})

_MODEL_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_TOOL_DISCOVERY_PAGES = 100
_MAX_SERVER_TOOLS = 4096
_MAX_RESULT_DEPTH = 64
_MAX_RESULT_NODES = 1_000_000


def _sanitize_mcp_data(value: Any, server_config: MCPServerConfig) -> Any:
    """Quarantine secrets/host paths without truncating normal spill payloads.

    Reuse the public projection's string/key policy, but not its small preview
    node budget: complete structured results must still reach the spill layer.
    The MCP wire payload remains data, never trusted execution metadata.
    """
    def secret_key(key: str) -> bool:
        # Match the shared projection policy, including common environment
        # names such as SERVICE_KEY. Runtime flags/paths are not credentials:
        # globally replacing an env value like TIMEOUT=1 corrupts numeric data.
        return (
            next(iter(sanitize_spill_public_data({key: None}).values())) == "[redacted credential]"
            or re.sub(r"[^a-z0-9]", "", key.lower()).endswith("key")
        )

    secrets = {value for key, value in (server_config.env or {}).items() if secret_key(key)}
    public_headers = {"accept", "content-type", "user-agent", "mcp-protocol-version"}
    secrets.update(value for key, value in (server_config.headers or {}).items()
                   if key.lower() not in public_headers)
    # An Authorization header may be echoed with or without its auth scheme.
    secrets.update(value.split(" ", 1)[1] for value in tuple(secrets)
                   if value.lower().startswith(("bearer ", "basic ")))
    secrets = sorted((value for value in secrets if value), key=len, reverse=True)
    remaining = _MAX_RESULT_NODES

    def text(value: str) -> str:
        for secret in secrets:
            value = value.replace(secret, "[redacted credential]")
        # Inline image bytes belong only to the typed, owner-bound media path,
        # including when an MCP server echoes them in a textual envelope.
        value = re.sub(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "[inline image omitted]", value)
        return sanitize_spill_public_text(value)

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if depth > _MAX_RESULT_DEPTH or remaining < 0:
            raise ValueError("MCP result exceeded structural limits")
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("MCP object keys must be strings")
                # A one-key projection reuses the credential-key rules without
                # treating the entire output as a bounded public preview.
                key_projection = sanitize_spill_public_data({key: None})
                is_secret = next(iter(key_projection.values())) == "[redacted credential]"
                safe_key = text(key)
                if safe_key in result:
                    raise ValueError("MCP result keys collide after redaction")
                result[safe_key] = "[redacted credential]" if is_secret else visit(child, depth + 1)
            return result
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, str):
            return text(item)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        raise ValueError("MCP result must contain JSON values")

    return visit(value, 0)


def _project_mcp_content(content: Any) -> list[dict[str, Any]]:
    """Retain text, explicitly mark unsupported binary blocks, never fetch URIs.

    Arbitrary SDK object reprs may include raw base64, annotations or local
    resource locations. Only the documented data fields below may cross into
    the existing ToolResult/spill channel.
    """
    projected = []
    for item in content or []:
        kind = getattr(item, "type", "text" if hasattr(item, "text") else "unknown")
        if kind == "text" and isinstance(getattr(item, "text", None), str):
            projected.append({"type": "text", "text": item.text})
        elif kind == "resource" and isinstance(getattr(getattr(item, "resource", None), "text", None), str):
            projected.append({"type": "resource", "text": item.resource.text})
        else:
            projected.append({
                "type": kind if kind in {"image", "audio", "resource", "resource_link"} else "unsupported",
                "omitted": True,
                "reason": "MCP non-text content is not exposed through this text adapter",
            })
    return projected


def _model_tool_name(server_name: str, tool_name: str) -> str:
    """Build a stable provider-safe name while preserving existing valid names."""
    safe_server = server_name.replace("-", "_")
    prefix = safe_server if safe_server.startswith("mcp_") else f"mcp_{safe_server}"
    candidate = f"{prefix}_{tool_name}"
    if _MODEL_TOOL_NAME_RE.fullmatch(candidate):
        return candidate

    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", candidate).strip("_-")
    if not normalized.startswith("mcp_"):
        normalized = f"mcp_{normalized}"
    digest = hashlib.sha256(
        f"{server_name}\0{tool_name}".encode("utf-8", errors="strict")
    ).hexdigest()[:12]
    suffix = f"_{digest}"
    stem = normalized[: 64 - len(suffix)].rstrip("_-") or "mcp_tool"
    return f"{stem}{suffix}"


def _build_stdio_environment(
    configured_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build a minimal child environment and apply explicit server overrides."""
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key in _STDIO_INHERITED_ENV_KEYS
    }
    inherited.update(configured_env or {})
    return inherited


def _server_log_ref(server_name: object) -> str:
    """Return a stable, value-free identifier for an untrusted server name."""
    return hashlib.sha256(
        str(server_name).encode("utf-8", errors="replace")
    ).hexdigest()[:12]


class MCPClientManager:
    """MCP 客户端管理器"""
    
    def __init__(self, config: Optional[MCPConfig] = None, *, image_context: MCPImageContext | None = None):
        self._clients: Dict[str, ClientSession] = {}
        self._exit_stack = AsyncExitStack()
        self._tools_cache: Dict[str, List[MCPToolkit]] = {}
        self._tool_routes: Dict[str, Tuple[str, str]] = {}
        self._output_validators: Dict[str, Any] = {}
        self._initialized = False
        self._config = config
        self._image_context = image_context
    
    async def initialize(self):
        """初始化 MCP 客户端管理器"""
        if self._initialized:
            return
        
        try:
            logger.info(f"从配置加载了 {len(self._config.mcpServers)} 个 MCP 服务器配置")
            
            # 连接到所有启用的服务器
            await self._connect_servers()
            
            self._initialized = True
            logger.info("MCP 客户端管理器初始化成功")
            
        except Exception as error:
            logger.error(
                "MCP client initialization failed error_type=%s",
                type(error).__name__,
            )
            raise

    
    async def _connect_servers(self):
        """连接到所有启用的 MCP 服务器"""
        for server_name, server_config in self._config.mcpServers.items():
            if not server_config.enabled:
                continue
                
            try:
                await self._connect_server(server_name, server_config)
            except Exception as error:
                logger.error(
                    "MCP server connection failed server_ref=%s error_type=%s",
                    _server_log_ref(server_name),
                    type(error).__name__,
                )
                # 继续连接其他服务器
                continue
    
    async def _connect_server(self, server_name: str, server_config: MCPServerConfig):
        """连接到单个 MCP 服务器"""
        try:
            transport_type = server_config.transport
            
            if transport_type == 'stdio':
                await self._connect_stdio_server(server_name, server_config)
            elif transport_type == 'http' or transport_type == 'sse':
                await self._connect_http_server(server_name, server_config)
            elif transport_type == 'streamable-http':
                await self._connect_streamable_http_server(server_name, server_config)
            else:
                logger.error(f"不支持的传输类型: {transport_type}")
                
        except Exception as error:
            logger.error(
                "MCP transport setup failed server_ref=%s error_type=%s",
                _server_log_ref(server_name),
                type(error).__name__,
            )
            raise
    
    async def _connect_stdio_server(self, server_name: str, server_config: MCPServerConfig):
        """连接到 stdio MCP 服务器"""
        command = server_config.command
        args = server_config.args or []
        env = server_config.env or {}
        
        if not command:
            raise ValueError(f"服务器 {server_name} 缺少 command 配置")
        

        # 创建服务器参数（路径处理已在配置提供者中完成）
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=_build_stdio_environment(env),
        )
        
        try:
            # 建立连接
            stdio_transport = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            read_stream, write_stream = stdio_transport
            
            # 创建会话
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            
            # 初始化会话
            await session.initialize()
            
            # 缓存客户端
            self._clients[server_name] = session
            
            # 获取并缓存工具列表
            await self._cache_server_tools(server_name, session)
            
            logger.info(
                "Connected to stdio MCP server_ref=%s",
                _server_log_ref(server_name),
            )
            
        except Exception as error:
            logger.error(
                "Stdio MCP connection failed server_ref=%s error_type=%s",
                _server_log_ref(server_name),
                type(error).__name__,
            )
            raise
    
    async def _connect_http_server(self, server_name: str, server_config: MCPServerConfig):
        """获取 HTTP/SSE MCP 服务器工具列表（仅初始化用，不保持持久连接）"""
        url = server_config.url
        if not url:
            raise ValueError(f"服务器 {server_name} 缺少 url 配置")

        try:
            # 临时连接仅用于获取工具列表，call_tool 时会按需重连
            async with AsyncExitStack() as stack:
                sse_transport = await stack.enter_async_context(
                    sse_client(url, headers=server_config.headers or {})
                )
                read_stream, write_stream = sse_transport
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()
                await self._cache_server_tools(server_name, session)
            logger.info(
                "Loaded HTTP MCP tools server_ref=%s",
                _server_log_ref(server_name),
            )
        except Exception as error:
            logger.error(
                "HTTP MCP connection failed server_ref=%s error_type=%s",
                _server_log_ref(server_name),
                type(error).__name__,
            )
            raise

    async def _connect_streamable_http_server(self, server_name: str, server_config: MCPServerConfig):
        """获取 streamable-http MCP 服务器工具列表（仅初始化用，不保持持久连接）"""
        url = server_config.url
        if not url:
            raise ValueError(f"服务器 {server_name} 缺少 url 配置")

        headers = server_config.headers or {}

        try:
            client_params = {"url": url}
            if headers:
                client_params["headers"] = headers

            async with AsyncExitStack() as stack:
                streamable_transport = await stack.enter_async_context(
                    streamablehttp_client(**client_params)
                )
                if len(streamable_transport) == 3:
                    read_stream, write_stream, _ = streamable_transport
                else:
                    read_stream, write_stream = streamable_transport
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()
                await self._cache_server_tools(server_name, session)
            logger.info(
                "Loaded streamable HTTP MCP tools server_ref=%s",
                _server_log_ref(server_name),
            )
        except Exception as error:
            logger.error(
                "Streamable HTTP MCP connection failed server_ref=%s error_type=%s",
                _server_log_ref(server_name),
                type(error).__name__,
            )
            raise
    
    async def _cache_server_tools(self, server_name: str, session: ClientSession):
        """Atomically cache a complete, bounded catalog; never publish a prefix."""
        try:
            tools = []
            names = set()
            seen_cursors = set()
            cursor = None
            for _ in range(_MAX_TOOL_DISCOVERY_PAGES):
                tools_response = (await session.list_tools() if cursor is None
                                  else await session.list_tools(cursor=cursor))
                if tools_response is None:
                    raise ValueError("MCP tool discovery returned no response")
                page = tools_response.tools
                if len(tools) + len(page) > _MAX_SERVER_TOOLS:
                    raise ValueError("MCP tool discovery exceeded the tool limit")
                for item in page:
                    if item.name in names:
                        raise ValueError("MCP tool discovery returned a duplicate name")
                    names.add(item.name)
                    tools.append(copy.deepcopy(item))
                cursor = getattr(tools_response, "nextCursor", None)
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                    raise ValueError("MCP tool discovery returned an invalid or repeated cursor")
                seen_cursors.add(cursor)
            else:
                raise ValueError("MCP tool discovery exceeded the page limit")
            self._tools_cache[server_name] = tools
            logger.info(
                "MCP server_ref=%s exposes tool_count=%d",
                _server_log_ref(server_name),
                len(tools),
            )
            
        except Exception as error:
            logger.error(
                "MCP tool discovery failed server_ref=%s error_type=%s",
                _server_log_ref(server_name),
                type(error).__name__,
            )
            # Keep a previously complete generation intact if a refresh fails.
            self._tools_cache.setdefault(server_name, [])
    
    async def get_all_tools(self) -> List[Dict[str, Any]]:
        """获取所有 MCP 工具"""
        all_tools = []
        tool_routes: Dict[str, Tuple[str, str]] = {}
        output_validators: Dict[str, Any] = {}
        
        for server_name, tools in self._tools_cache.items():
            for tool in tools:
                tool_name = _model_tool_name(server_name, tool.name)

                route = (server_name, tool.name)
                existing_route = tool_routes.get(tool_name)
                if existing_route is not None:
                    raise ValueError(
                        "MCP 工具名冲突: "
                        f"{tool_name} 同时映射到 "
                        f"{existing_route[0]}/{existing_route[1]} 和 "
                        f"{server_name}/{tool.name}"
                    )
                tool_routes[tool_name] = route

                output_schema = getattr(tool, "outputSchema", None)
                if output_schema is not None:
                    # Pin the discovery-time contract; reconnecting for one
                    # invocation must not silently change a running task's schema.
                    output_schema = copy.deepcopy(output_schema)
                    validator_class = validator_for(output_schema)
                    validator_class.check_schema(output_schema)
                    output_validators[tool_name] = validator_class(
                        output_schema, registry=Registry(),
                    )
                
                # 转换为标准工具格式
                tool_schema = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": f"[{server_name}] {tool.description or tool.name}",
                        "parameters": copy.deepcopy(tool.inputSchema)
                    }
                }
                all_tools.append(tool_schema)

        # Publish the routing table only after the complete catalog validates,
        # so callers can never observe a partially discovered generation.
        self._tool_routes = tool_routes
        self._output_validators = output_validators
        return all_tools

    def get_tool_route(self, tool_name: str) -> Optional[Tuple[str, str]]:
        """Return the exact discovery-time route for a model-facing tool name."""
        return self._tool_routes.get(tool_name)
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> ToolResult:
        """调用 MCP 工具"""
        try:
            route = self._tool_routes.get(tool_name)
            if route is None:
                raise ValueError(f"无法解析 MCP 工具名称: {tool_name}")
            server_name, original_tool_name = route

            server_config = self._config.mcpServers[server_name]
            transport_type = server_config.transport

            logger.info(
                "调用 MCP 工具 %s，参数键: %s",
                opaque_log_identifier(original_tool_name, namespace="tool"),
                summarize_argument_keys(arguments),
            )

            if transport_type == 'stdio':
                # stdio 保持持久连接，进程重建代价高
                session = self._clients.get(server_name)
                if not session:
                    return ToolResult(success=False, message="MCP 服务器未连接")
                result = await session.call_tool(original_tool_name, arguments)
            elif transport_type in ('http', 'sse'):
                # SSE 每次调用建立新连接，避免长连接被 idle timeout 断掉
                url = server_config.url
                async with AsyncExitStack() as stack:
                    sse_transport = await stack.enter_async_context(
                        sse_client(url, headers=server_config.headers or {})
                    )
                    read_stream, write_stream = sse_transport
                    session = await stack.enter_async_context(
                        ClientSession(read_stream, write_stream)
                    )
                    await session.initialize()
                    result = await session.call_tool(original_tool_name, arguments)
            elif transport_type == 'streamable-http':
                url = server_config.url
                headers = server_config.headers or {}
                client_params = {"url": url}
                if headers:
                    client_params["headers"] = headers
                async with AsyncExitStack() as stack:
                    streamable_transport = await stack.enter_async_context(
                        streamablehttp_client(**client_params)
                    )
                    if len(streamable_transport) == 3:
                        read_stream, write_stream, _ = streamable_transport
                    else:
                        read_stream, write_stream = streamable_transport
                    session = await stack.enter_async_context(
                        ClientSession(read_stream, write_stream)
                    )
                    await session.initialize()
                    result = await session.call_tool(original_tool_name, arguments)
            else:
                return ToolResult(success=False, message=f"不支持的传输类型: {transport_type}")

            if result is None:
                raise ValueError("MCP tool returned no result")
            is_error = getattr(result, "isError", False)
            if not isinstance(is_error, bool):
                raise ValueError("MCP tool returned an invalid error status")
            structured = getattr(result, "structuredContent", None)
            if structured is not None and not isinstance(structured, dict):
                raise ValueError("MCP structured content must be an object")
            validator = self._output_validators.get(tool_name)
            if not is_error and validator is not None:
                try:
                    if structured is None or not validator.is_valid(structured):
                        raise ValueError("MCP output contract mismatch")
                except Exception:
                    # Invalid/unresolvable output stays quarantined. Never echo
                    # validator messages or retry a potentially completed write.
                    return ToolResult(
                        success=False,
                        message="MCP 工具输出不符合声明的契约",
                        data={"error": "mcp_tool_output_invalid"},
                    )
            content = _project_mcp_content(getattr(result, "content", None))
            if self._image_context is not None and any(item.get("type") == "image" for item in content):
                public, blocks, refs = await self._image_context.prepare(
                    getattr(result, "content", None) or [], _sanitize_mcp_data(content, server_config), tool_name=tool_name,
                )
                safe_data = {"content": public}
                if structured is not None:
                    safe_data["structuredContent"] = _sanitize_mcp_data(structured, server_config)
                    import json
                    blocks.append({"type": "text", "text": json.dumps(safe_data["structuredContent"], ensure_ascii=False)})
                rich = MCPImageToolResult(success=not is_error,
                    message="MCP 工具执行失败" if is_error else None, data=safe_data)
                rich._model_blocks, rich._image_refs = blocks, refs
                return rich
            if structured is None and all(item["type"] == "text" for item in content):
                data = "\n".join(item["text"] for item in content) or (
                    "MCP 工具执行失败" if is_error else "工具执行成功"
                )
            else:
                data = {"content": content}
                if structured is not None:
                    data["structuredContent"] = structured
            return ToolResult(
                success=not is_error,
                message="MCP 工具执行失败" if is_error else None,
                data=_sanitize_mcp_data(data, server_config),
            )

        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "MCP tool call failed tool=%s error_type=%s",
                tool_name,
                type(error).__name__,
            )
            return ToolResult(
                success=False,
                message="MCP 工具调用失败",
                data={"error": "mcp_tool_call_failed"},
            )

    async def cleanup(self):
        """清理资源"""
        try:
            await self._exit_stack.aclose()
            self._clients.clear()
            self._tools_cache.clear()
            self._tool_routes.clear()
            self._output_validators.clear()
            self._initialized = False
            logger.info("MCP 客户端管理器已清理")
            
        except Exception as error:
            logger.error(
                "MCP client cleanup failed error_type=%s",
                type(error).__name__,
            )


class _MCPToolWrapper:
    """Duck-typed wrapper so MCPToolkit.get_tool() returns something BaseAgent can invoke."""

    def __init__(
        self,
        name: str,
        manager: MCPClientManager,
        toolkit: 'MCPToolkit',
        input_schema: Mapping[str, Any] | None = None,
    ):
        self.name = name
        self.toolkit = toolkit  # toolkit.name == "mcp", used by ToolEvent
        self._manager = manager
        self.input_schema = dict(input_schema or {})

    async def ainvoke(self, tool_call: dict) -> ToolMessage:
        async def execute(context):
            result = await self._manager.call_tool(self.name, context.arguments)
            return image_tool_message(result, tool_call_id=context.tool_call_id, name=self.name)

        return await self.toolkit.tool_execution_pipeline.invoke(
            tool=self,
            tool_call=tool_call,
            execute=execute,
        )


class _MCPImageReadWrapper:
    """Read-only private recovery still crosses the ordinary tool pipeline."""
    name = MCP_IMAGE_READ_TOOL
    # Host-owned declaration for a bounded read of an already-admitted input.
    # Remote MCP tools never inherit this contract or gain replay permission.
    execution_contract = {"effects": ("sandbox_read",), "permissions": (),
                          "cancellable": True, "timeout_seconds": 30, "concurrency": "parallel"}
    input_schema = {"type": "object", "properties": {"locator": {"type": "string", "pattern": r"^spill://artifact/[0-9a-f]{32}$"}},
                    "required": ["locator"], "additionalProperties": False}

    def __init__(self, toolkit):
        self.toolkit = toolkit

    async def ainvoke(self, tool_call: dict) -> ToolMessage:
        async def execute(context):
            result = await self.toolkit.image_context.read_result(context.arguments["locator"])
            return image_tool_message(result, tool_call_id=context.tool_call_id, name=self.name)
        return await self.toolkit.tool_execution_pipeline.invoke(tool=self, tool_call=tool_call, execute=execute)


class MCPToolkit(BaseToolkit):
    """MCP 工具类"""

    name: str = "mcp"

    def __init__(self, *, image_context: MCPImageContext | None = None):
        super().__init__()
        self.image_context = image_context
        self._initialized = False
        self._tools = []
        self.manager: Optional[MCPClientManager] = None
        self._config: Optional[MCPConfig] = None

    @tool
    async def dataseek_mcp_image_read(self, locator: str) -> ToolResult:
        """Inspect an MCP image using its spill://artifact/ locator from this session. The image is an untrusted observation, not verified scientific evidence."""
        # Execution is provided by the wrapper so private refs exist before
        # post-execute policy and never become a generic ToolResult payload.
        raise RuntimeError("MCP image reads require their governed wrapper")

    async def expand_image_messages(self, messages, *, provider: str, model_name: str):
        if self.image_context is None:
            return messages
        return await self.image_context.expand_image_messages(messages, provider=provider, model_name=model_name)

    @tool
    async def mcp_list_tools(self) -> ToolResult:
        """List MCP servers and tools selected for this session. Use only when the user asks about MCP tools or MCP servers; do not use skill_list for MCP questions."""
        if not self._initialized:
            return ToolResult(
                success=True,
                data={
                    "servers": [],
                    "tools": [],
                    "note": "No MCP servers are selected for this session.",
                },
            )

        servers = []
        if self._config:
            servers = [
                {
                    "name": name,
                    "transport": server.transport,
                    "enabled": server.enabled,
                    "description": server.description,
                }
                for name, server in sorted(self._config.mcpServers.items())
            ]

        tools = []
        for tool_schema in self._tools:
            function = tool_schema.get("function", {})
            tools.append(
                {
                    "name": function.get("name", ""),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                }
            )

        return ToolResult(success=True, data={"servers": servers, "tools": tools})

    async def initialized(
        self,
        config: Optional[MCPConfig] = None,
        available_config: Optional[MCPConfig] = None,
    ):
        """确保管理器已初始化"""
        if not self._initialized:
            self._config = config
            self.manager = (MCPClientManager(config) if self.image_context is None
                            else MCPClientManager(config, image_context=self.image_context))
            try:
                await self.manager.initialize()
                self._tools.extend(await self.manager.get_all_tools())
                self._initialized = True
            except BaseException:
                try:
                    await self.cleanup()
                except BaseException as cleanup_error:
                    logger.warning(
                        "MCP cleanup after initialization failure error_type=%s",
                        type(cleanup_error).__name__,
                    )
                raise

    def get_tools(self) -> List[Any]:
        return [item for item in self.tools if item.name != MCP_IMAGE_READ_TOOL or self.image_context is not None] + self._tools

    def get_tool(self, name: str) -> Optional[_MCPToolWrapper]:
        if name == MCP_IMAGE_READ_TOOL:
            return _MCPImageReadWrapper(self) if self.image_context is not None else None
        builtin_tool = super().get_tool(name)
        if builtin_tool:
            return builtin_tool
        for tool in self._tools:
            if tool['function']['name'] == name:
                return _MCPToolWrapper(
                    name=name,
                    manager=self.manager,
                    toolkit=self,
                    input_schema=tool["function"].get("parameters"),
                )
        return None

    async def cleanup(self):
        """清理资源"""
        manager = self.manager
        try:
            if manager:
                await manager.cleanup()
        finally:
            if self.image_context is not None:
                await self.image_context.drain()
            self.manager = None
            self._initialized = False
            self._tools = []
            self._config = None
