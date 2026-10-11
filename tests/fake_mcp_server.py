"""测试用的 MCP stdio 服务：只实现 initialize / tools/list / tools/call。

用法：python fake_mcp_server.py（按换行分隔的 JSON-RPC 与客户端通信）
"""

from __future__ import annotations

import json
import sys

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
        if method in ("initialize", "tools/list", "tools/call"):
            payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": handle(request)}
        else:
            payload = {"jsonrpc": "2.0", "id": request.get("id"),
                       "error": {"code": -32601, "message": f"不支持的方法: {method}"}}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
