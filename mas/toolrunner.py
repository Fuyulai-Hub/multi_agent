"""ToolRunner：把 MCP 服务工具接入 LLM function calling 闭环。

链路：MCP 服务 tools/list → 转成 OpenAI tools schema → 随 chat/completions 下发 →
模型返回 tool_calls → 本模块通过 MCP tools/call 真实执行 → 结果以 role=tool 消息回传 →
模型基于结果继续生成。

设计取舍（保持项目零依赖与最小侵入）：
- 不引入 MCP SDK，复用 mas.mcp 的最小客户端（initialize/tools/list/tools/call）；
- 客户端懒创建 + 进程退出前统一 close（MCP stdio 服务是子进程，必须回收）；
- 单个工具调用失败不中断闭环：错误信息作为 tool 消息内容回传，让模型自行纠错；
- 未配置/未启用任何 MCP 服务时 schemas 为空，LLM 不下发 tools，行为与原来完全一致。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .config import SystemConfig
from .errors import ConfigError
from .mcp import MCPClient, load_servers

__all__ = ["ToolRunner"]


class ToolRunner:
    """MCP 工具 ↔ OpenAI function calling 适配器（按需装配，默认不启用）。"""

    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self._schemas: List[Dict[str, Any]] = []
        self._owner: Dict[str, str] = {}          # tool_name -> server_name（""=静态/本地）
        self._local: Dict[str, Any] = {}          # tool_name -> 本地执行器（无需 MCP 服务）
        self._clients: Dict[str, MCPClient] = {}  # server_name -> 已连接客户端
        self._counter = 0

    # ---------------------------------------------------------------- 装配
    def register_from_mcp(self, server_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """从 config/mcp.yaml 拉取工具清单并转 OpenAI tools schema。

        默认只装配 enabled=true 的服务；server_names 可显式指定子集。
        单个服务失败（未启动/握手失败）只跳过该服务并记录，不影响其他服务。
        """
        servers = load_servers(self.config)
        wanted = set(server_names) if server_names else None
        for server in servers:
            name = str(server.get("name", ""))
            if not server.get("enabled", True) or (wanted is not None and name not in wanted):
                continue
            # 只给框架侧直连、不该进 LLM 工具面的服务（如浏览器：由 mas/uiprober.py
            # 在 tester 节点直连取证）。不下发给模型，避免 25 个 browser_* 工具污染
            # 工具面，也避免每次 run 无谓启动一个浏览器子进程。
            if server.get("tools_for_llm") is False:
                continue
            try:
                client = MCPClient(server)
                client.start()
                client.initialize()
                for tool in client.list_tools():
                    tool_name = str(tool.get("name", ""))
                    if not tool_name or tool_name in self._owner:
                        continue
                    self._owner[tool_name] = name
                    self._clients[name] = client
                    self._schemas.append({
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": str(tool.get("description", "")),
                            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                        },
                    })
            except ConfigError:
                continue  # 服务不可用：跳过（CLI 的 mcp check 负责诊断，这里不阻塞主流程）
        return self.schemas

    def register_static(self, tools: List[Dict[str, Any]]) -> None:
        """注册静态声明的 OpenAI tools（config llm.tools），无需 MCP 服务。"""
        for tool in tools or []:
            name = str((tool.get("function") or {}).get("name", ""))
            if name and name not in self._owner:
                self._owner[name] = ""
                self._schemas.append(tool)

    def register_local(
        self,
        name: str,
        fn: Any,
        *,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册本地执行器（docs/16 的 05 文档 路径 B）：``_call_one`` 先查 ``_local`` 直接调用。

        与静态声明的区别：静态声明无可执行后端，本地执行器有真实函数可执行（如 web_search）。
        """
        if not name or name in self._owner:
            return
        self._owner[name] = ""
        self._local[name] = fn
        self._schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters or {"type": "object", "properties": {}},
            },
        })

    @property
    def schemas(self) -> List[Dict[str, Any]]:
        return list(self._schemas)

    # ---------------------------------------------------------------- 执行
    def execute(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """执行一批 tool_calls，返回可直接拼接进 messages 的 role=tool 消息列表。

        兼容两种入参格式：
        - OpenAI 原始格式：{"id":..., "function": {"name":..., "arguments": "json串"}}；
        - LLMClient._parse_tool_calls 归一化格式：{"id":..., "name":..., "arguments": dict}。
        """
        messages: List[Dict[str, Any]] = []
        for call in tool_calls or []:
            self._counter += 1
            call_id, name, raw_args = self._call_fields(call)
            arguments: Dict[str, Any] = {}
            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    arguments = {}
            elif isinstance(raw_args, dict):
                arguments = raw_args
            content = self._call_one(name, arguments)
            messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
        return messages

    @staticmethod
    def _call_fields(call: Dict[str, Any]) -> Tuple[str, str, Any]:
        """从任一种 tool_call 格式中取出 (id, name, arguments)。"""
        function = call.get("function") or {}
        call_id = str(call.get("id") or "")
        name = str(call.get("name") or function.get("name") or "")
        raw_args = call["arguments"] if "arguments" in call else function.get("arguments")
        return call_id, name, raw_args

    def _call_one(self, name: str, arguments: Dict[str, Any]) -> str:
        # 本地执行器优先（docs/16 的 05 文档 路径 B）：直接调用并把返回值 JSON 序列化回传
        local = self._local.get(name)
        if local is not None:
            try:
                result = local(**(arguments if isinstance(arguments, dict) else {}))
            except Exception as exc:  # noqa: BLE001 - 与 MCP 工具失败同约定：错误作为 tool 内容回传
                return f"错误：工具 {name} 执行失败（{type(exc).__name__}: {exc}）"
            return json.dumps(result, ensure_ascii=False, default=str)
        server_name = self._owner.get(name)
        if server_name is None:
            return f"错误：未知工具 {name}（可用: {sorted(self._owner)}）"
        if not server_name:
            return f"错误：工具 {name} 为静态声明，无可执行后端（请配置 MCP 服务）"
        client = self._clients.get(server_name)
        if client is None:
            return f"错误：工具 {name} 所属服务 {server_name} 未连接"
        try:
            result = client.call_tool(name, arguments)
        except ConfigError as exc:
            return f"错误：工具 {name} 执行失败（{exc}）"
        return json.dumps(result, ensure_ascii=False, default=str)

    # ---------------------------------------------------------------- 生命周期
    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应掩盖主流程异常
                pass
        self._clients.clear()

    def __enter__(self) -> "ToolRunner":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
