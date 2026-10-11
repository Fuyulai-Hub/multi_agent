"""MCP 服务测试：参数校验、stdio 真实握手、失败阶段定位、sse 传输。

覆盖：
- 配置校验：缺字段 / 非法 transport / 非法名称
- 持久化：新增 / 列出 / 删除
- ${VAR} 环境变量展开（含缺失时明确报错）
- stdio：与 tests/fake_mcp_server.py 真实握手并拿到工具列表
- 失败定位：服务未配置 → parse；参数非法 → validate；命令不存在 → connect
- sse：与内置的事件流服务完成握手与 tools/list
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mas.config import load_config
from mas.errors import ConfigError
from mas.mcp import (
    add_server,
    check_server,
    expand_env,
    load_servers,
    remove_server,
    save_servers,
    validate_server,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"

_SSE_QUEUE: "queue.Queue[dict]" = queue.Queue()


class _SSEHandler(BaseHTTPRequestHandler):
    """测试用 MCP SSE 服务：GET 建立事件流，POST 收请求，响应回写到事件流。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802（标准库命名）
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(b"event: endpoint\ndata: /message\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = _SSE_QUEUE.get(timeout=5)
                except queue.Empty:
                    return
                chunk = "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):  # noqa: N802（标准库命名）
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        request = json.loads(raw)
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {},
                      "serverInfo": {"name": "fake-sse", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "remote_echo"}]}
        else:
            result = {}
        _SSE_QUEUE.put({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _SSEServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MCPConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.config.config_dir = self.root / "config"

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 校验
    def test_validate_accepts_stdio_and_sse(self):
        self.assertEqual(validate_server({"name": "a", "transport": "stdio", "command": "x"}), [])
        self.assertEqual(validate_server({"name": "b", "transport": "sse", "url": "https://h/sse"}), [])

    def test_validate_rejects_bad_definitions(self):
        self.assertIn("缺少必填字段: transport", validate_server({"name": "a"}))
        self.assertTrue(validate_server({"name": "a", "transport": "grpc"}))
        self.assertTrue(validate_server({"name": "Bad_Name", "transport": "stdio", "command": "x"}))
        self.assertTrue(validate_server({"name": "a", "transport": "stdio"}))
        self.assertTrue(validate_server({"name": "a", "transport": "sse", "url": "ftp://h"}))
        self.assertTrue(validate_server({"name": "a", "transport": "stdio", "command": "x", "args": "y"}))

    # ---------------------------------------------------------------- 持久化
    def test_add_list_remove(self):
        add_server(self.config, {"name": "demo", "transport": "stdio", "command": "echo"})
        self.assertEqual([item["name"] for item in load_servers(self.config)], ["demo"])
        with self.assertRaises(ConfigError):
            add_server(self.config, {"name": "demo", "transport": "stdio", "command": "echo"})
        remove_server(self.config, "demo")
        self.assertEqual(load_servers(self.config), [])
        with self.assertRaises(ConfigError):
            remove_server(self.config, "demo")

    def test_expand_env(self):
        os.environ["MAS_TEST_TOKEN"] = "tok-123"
        try:
            self.assertEqual(expand_env("Bearer ${MAS_TEST_TOKEN}"), "Bearer tok-123")
        finally:
            del os.environ["MAS_TEST_TOKEN"]
        with self.assertRaises(ConfigError):
            expand_env("${MAS_MISSING_TOKEN_XYZ}")


class MCPStdioTest(unittest.TestCase):
    """stdio 传输：与假服务真实握手。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.config.config_dir = self.root / "config"

    def tearDown(self):
        self.tmp.cleanup()

    def _add_fake(self) -> None:
        add_server(self.config, {
            "name": "fake",
            "transport": "stdio",
            "enabled": True,
            "command": sys.executable,
            "args": [str(FAKE_SERVER)],
            "timeout_s": 15,
        })

    def test_check_handshake_and_tools(self):
        self._add_fake()
        report = check_server(self.config, "fake")
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["stage"], "done")
        self.assertEqual(report["tools"], ["echo", "ping"])

    def test_check_with_tool_call(self):
        self._add_fake()
        report = check_server(self.config, "fake", call="echo", params={"text": "hi"})
        self.assertTrue(report["ok"], report)
        self.assertIn("call_result", report)

    def test_check_command_not_found_reports_connect_stage(self):
        add_server(self.config, {"name": "ghost", "transport": "stdio",
                                 "command": "definitely-not-a-command-xyz", "timeout_s": 5})
        report = check_server(self.config, "ghost")
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "connect")
        self.assertIn("启动命令不存在", report["reason"])
        self.assertTrue(report["hint"])

    def test_check_unknown_server_reports_parse_stage(self):
        report = check_server(self.config, "not-configured")
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "parse")

    def test_check_invalid_config_reports_validate_stage(self):
        save_servers(self.config, [{"name": "broken", "transport": "stdio"}])
        report = check_server(self.config, "broken")
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "validate")
        self.assertTrue(report["hint"])


class MCPSseTest(unittest.TestCase):
    """sse 传输：与内置事件流服务完成握手。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.config.config_dir = self.root / "config"
        self.server = _SSEServer(("127.0.0.1", 0), _SSEHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_check_sse(self):
        url = f"http://127.0.0.1:{self.server.server_address[1]}/sse"
        add_server(self.config, {"name": "remote", "transport": "sse", "url": url, "timeout_s": 10})
        report = check_server(self.config, "remote")
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["tools"], ["remote_echo"])


if __name__ == "__main__":
    unittest.main()
