"""测试用的 MCP stdio 服务：只实现 initialize / tools/list / tools/call。

用法：python fake_mcp_server.py（按换行分隔的 JSON-RPC 与客户端通信）

环境变量 FAKE_MCP_NOTIFY=1 时，在每次 tools/call 响应**之前**先输出一条
``notifications/tools/list_changed`` 通知帧（真实 Playwright MCP 就会这样发）。
用于回归 mcp.py 的响应 id 匹配：只读一行的实现会把这帧当响应，导致返回 {} 并让
后续调用整体错位一格。
"""

from __future__ import annotations

import json
import os
import sys

NOTIFY_BEFORE_CALL = os.environ.get("FAKE_MCP_NOTIFY") == "1"

TOOLS = [
    {"name": "echo", "description": "回显入参", "inputSchema": {"type": "object"}},
    {"name": "ping", "description": "健康检查", "inputSchema": {"type": "object"}},
]


def handle(request: dict) -> dict:
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": (request.get("params") or {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp-server", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        arguments = (request.get("params") or {}).get("arguments") or {}
        return {"content": [{"type": "text", "text": json.dumps(arguments, ensure_ascii=False)}],
                "isError": False}
    return {}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        method = request.get("method")
        if NOTIFY_BEFORE_CALL and method == "tools/call":
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
                ensure_ascii=False) + "\n")
            sys.stdout.flush()
        if method in ("initialize", "tools/list", "tools/call"):
            payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": handle(request)}
        else:
            payload = {"jsonrpc": "2.0", "id": request.get("id"),
                       "error": {"code": -32601, "message": f"不支持的方法: {method}"}}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
