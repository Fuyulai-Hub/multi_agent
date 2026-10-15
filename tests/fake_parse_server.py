"""测试用的文档解析 MCP 服务：tools/list 暴露 convert_file；tools/call 返回 markdown content。

用法：python fake_parse_server.py [--mode ok|error|slow]
- ok   ：返回 markdown 文本 content（正常路径）
- error：返回 isError=true
- slow ：tools/call 前 sleep 5s（触发超时路径）
"""

from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "convert_file",
        "description": "把文档转为 markdown 文本",
        "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string"}}},
    },
]


def _mode() -> str:
    argv = sys.argv[1:]
    if "--mode" in argv:
        return argv[argv.index("--mode") + 1]
    return "ok"


def handle(request: dict, mode: str) -> dict:
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": (request.get("params") or {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-parse-server", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        arguments = (request.get("params") or {}).get("arguments") or {}
        if mode == "error":
            return {"content": [{"type": "text", "text": "解析失败"}], "isError": True}
        if mode == "slow":
            time.sleep(5)
        text = f"# 解析结果\n\n### FP-01 计算\n\nsource={arguments.get('file_path', '')}"
        return {"content": [{"type": "text", "text": text}], "isError": False}
    return {}


def main() -> None:
    mode = _mode()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        method = request.get("method")
        if method in ("initialize", "tools/list", "tools/call"):
            payload = {"jsonrpc": "2.0", "id": request.get("id"), "result": handle(request, mode)}
        else:
            payload = {"jsonrpc": "2.0", "id": request.get("id"),
                       "error": {"code": -32601, "message": f"不支持的方法: {method}"}}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
