"""联网搜索测试（docs/16 的 05 文档）。

覆盖：
- ``websearch.search``：本地 HTTP 桩返回固定结果（bing 形状）；超时 / 500 / 空结果三条路径不抛异常；
- ``ToolRunner.register_local`` + ``_call_one`` 本地执行器的回传格式与失败约定；
- ``BaseAgent._tool_schemas`` 的 ``tools_nodes`` 节点隔离（intake 有工具 / implement 无工具）。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from mas import websearch
from mas.agents.base import BaseAgent
from mas.config import load_config
from mas.schemas import NodeTask
from mas.toolrunner import ToolRunner

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _SearchHandler(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):  # noqa: N802
        mode = type(self).mode
        if mode == "slow":
            time.sleep(3)
        if mode == "error":
            body = b'{"error": "boom"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        pages = [] if mode == "empty" else [
            {"name": "结果一", "url": "https://a.example/1", "snippet": "摘要一"},
            {"name": "结果二", "url": "https://a.example/2", "snippet": "摘要二"},
        ]
        body = json.dumps({"webPages": {"value": pages}}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        _SearchHandler.mode = "ok"
        self.server = _StubServer(("127.0.0.1", 0), _SearchHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}/search"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _sink(self, kind, payload):
        self.events.append({"kind": kind, **payload})

    def test_search_returns_results(self):
        results = websearch.search("订单计价", provider="bing", api_key="k",
                                   endpoint=self.endpoint, event_sink=self._sink)
        self.assertEqual([item["title"] for item in results], ["结果一", "结果二"])
        self.assertTrue(any(item["kind"] == "websearch_requested" for item in self.events))

    def test_empty_results_no_exception(self):
        _SearchHandler.mode = "empty"
        results = websearch.search("x", provider="bing", api_key="k", endpoint=self.endpoint,
                                   event_sink=self._sink)
        self.assertEqual(results, [])
        self.assertTrue(any(item["kind"] == "websearch_empty" for item in self.events))

    def test_http_500_returns_empty_with_event(self):
        _SearchHandler.mode = "error"
        results = websearch.search("x", provider="bing", api_key="k", endpoint=self.endpoint,
                                   event_sink=self._sink)
        self.assertEqual(results, [])
        self.assertTrue(any(item["kind"] == "websearch_failed" for item in self.events))

    def test_timeout_returns_empty(self):
        _SearchHandler.mode = "slow"
        results = websearch.search("x", provider="bing", api_key="k", endpoint=self.endpoint,
                                   timeout_s=1, event_sink=self._sink)
        self.assertEqual(results, [])
        self.assertTrue(any(item["kind"] == "websearch_failed" for item in self.events))

    def test_missing_key_and_bad_provider_return_empty(self):
        self.assertEqual(websearch.search("x", provider="bing", api_key=""), [])
        self.assertEqual(websearch.search("x", provider="nope", api_key="k",
                                          endpoint=self.endpoint, event_sink=self._sink), [])
        self.assertTrue(any(item["kind"] == "websearch_failed" for item in self.events))

    def test_empty_query_returns_empty(self):
        self.assertEqual(websearch.search("   ", provider="bing", api_key="k"), [])


class LocalToolRunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(PROJECT_ROOT / "config", workspace=PROJECT_ROOT / "workspace")

    def test_register_local_and_execute(self):
        runner = ToolRunner(self.config)
        runner.register_local(
            "web_search", lambda query="": [{"title": "t", "url": "u", "snippet": query}],
            description="检索", parameters={"type": "object",
                                             "properties": {"query": {"type": "string"}}},
        )
        self.assertEqual([t["function"]["name"] for t in runner.schemas], ["web_search"])
        messages = runner.execute([{"id": "c1", "name": "web_search", "arguments": {"query": "hi"}}])
        self.assertEqual(messages[0]["role"], "tool")
        self.assertIn("hi", messages[0]["content"])
        self.assertEqual(json.loads(messages[0]["content"])[0]["title"], "t")

    def test_local_executor_failure_returns_error_text(self):
        def _boom(**_kwargs):
            raise RuntimeError("search down")

        runner = ToolRunner(self.config)
        runner.register_local("web_search", _boom)
        messages = runner.execute([{"id": "c1", "name": "web_search", "arguments": {"query": "x"}}])
        self.assertIn("执行失败", messages[0]["content"])

    def test_static_tool_without_local_still_reports_no_backend(self):
        runner = ToolRunner(self.config)
        runner.register_static([{"type": "function", "function": {"name": "ping"}}])
        messages = runner.execute([{"id": "c1", "name": "ping", "arguments": {}}])
        self.assertIn("无可执行后端", messages[0]["content"])


class _MinAgent(BaseAgent):
    """只测 _tool_schemas 过滤：跳过基类 __init__（不需要 role）。"""

    role_id = "developer"

    def __init__(self, ctx):
        self.ctx = ctx


class ToolSchemaFilterTests(unittest.TestCase):
    def _agent(self, llm_cfg):
        schemas = [{"type": "function", "function": {"name": "web_search"}}]
        ctx = SimpleNamespace(config=SimpleNamespace(llm=llm_cfg),
                              tools=SimpleNamespace(schemas=schemas))
        return _MinAgent(ctx)

    def _task(self, node_id):
        return NodeTask(node_id=node_id, title="t", role="product", kind="task", iteration=1)

    def test_tools_nodes_isolates_intake(self):
        agent = self._agent({"tools_nodes": ["intake"]})
        self.assertIsNotNone(agent._tool_schemas(self._task("intake")))
        self.assertIsNone(agent._tool_schemas(self._task("implement")))

    def test_empty_tools_nodes_means_all_nodes(self):
        agent = self._agent({"tools_nodes": []})
        self.assertIsNotNone(agent._tool_schemas(self._task("implement")))

    def test_absent_tools_nodes_means_all_nodes(self):
        agent = self._agent({})
        self.assertIsNotNone(agent._tool_schemas(self._task("implement")))

    def test_no_runner_returns_none(self):
        agent = _MinAgent(SimpleNamespace(config=SimpleNamespace(llm={}), tools=None))
        self.assertIsNone(agent._tool_schemas(self._task("intake")))


if __name__ == "__main__":
    unittest.main()
