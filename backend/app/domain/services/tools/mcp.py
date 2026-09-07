import asyncio
import hashlib
import os
import logging
import re
from typing import Dict, Any, List, Mapping, Optional, Tuple
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool as MCPToolkit

from langchain.messages import ToolMessage
from langchain.tools import tool

from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.pipeline import opaque_log_identifier, summarize_argument_keys
from app.domain.models.tool_result import ToolResult
from app.domain.models.mcp_config import MCPConfig, MCPServerConfig

logger = logging.getLogger(__name__)


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
    
    def __init__(self, config: Optional[MCPConfig] = None):
        self._clients: Dict[str, ClientSession] = {}
        self._exit_stack = AsyncExitStack()
        self._tools_cache: Dict[str, List[MCPToolkit]] = {}
        self._tool_routes: Dict[str, Tuple[str, str]] = {}
        self._initialized = False
        self._config = config
    
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
                sse_transport = await stack.enter_async_context(sse_client(url))
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
        """缓存服务器工具列表"""
        try:
            tools_response = await session.list_tools()
            tools = tools_response.tools if tools_response else []
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
            self._tools_cache[server_name] = []
    
    async def get_all_tools(self) -> List[Dict[str, Any]]:
        """获取所有 MCP 工具"""
        all_tools = []
        tool_routes: Dict[str, Tuple[str, str]] = {}
        
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
                
                # 转换为标准工具格式
                tool_schema = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": f"[{server_name}] {tool.description or tool.name}",
                        "parameters": tool.inputSchema
                    }
                }
                all_tools.append(tool_schema)

        # Publish the routing table only after the complete catalog validates,
        # so callers can never observe a partially discovered generation.
        self._tool_routes = tool_routes
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
                    return ToolResult(success=False, message=f"MCP 服务器 {server_name} 未连接")
                result = await session.call_tool(original_tool_name, arguments)
            elif transport_type in ('http', 'sse'):
                # SSE 每次调用建立新连接，避免长连接被 idle timeout 断掉
                url = server_config.url
                async with AsyncExitStack() as stack:
                    sse_transport = await stack.enter_async_context(sse_client(url))
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

            if result:
                content = []
                if hasattr(result, 'content') and result.content:
                    for item in result.content:
                        if hasattr(item, 'text'):
                            content.append(item.text)
                        else:
                            content.append(str(item))
                return ToolResult(
                    success=True,
                    data='\n'.join(content) if content else "工具执行成功"
                )
            return ToolResult(success=True, data="工具执行成功")

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
            content = result.model_dump_json() if hasattr(result, "model_dump_json") else str(result)
            return ToolMessage(
                tool_call_id=context.tool_call_id,
                name=self.name,
                content=content,
                artifact=result,
            )

        return await self.toolkit.tool_execution_pipeline.invoke(
            tool=self,
            tool_call=tool_call,
            execute=execute,
        )


class MCPToolkit(BaseToolkit):
    """MCP 工具类"""

    name: str = "mcp"

    def __init__(self):
        super().__init__()
        self._initialized = False
        self._tools = []
        self.manager: Optional[MCPClientManager] = None
        self._config: Optional[MCPConfig] = None

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
            self.manager = MCPClientManager(config)
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
        return self.tools + self._tools

    def get_tool(self, name: str) -> Optional[_MCPToolWrapper]:
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
            self.manager = None
            self._initialized = False
            self._tools = []
            self._config = None
