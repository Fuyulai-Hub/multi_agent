"""function calling 端到端验证（真实组件，非 mock）。

与 test_hardening.FunctionCallingTests 的区别：那个用假 LLM/假 Runner 验证骨架逻辑；
本文件用**真实组件**跑完整链路——

    本地 OpenAI 协议桩 HTTP 服务（脚本化返回 tool_calls → 最终 JSON）
      → 真实 LLMClient（urllib 真实发包）
      → 真实 BaseAgent._generate_with_tools 闭环
      → 真实 ToolRunner.register_from_mcp（拉起 tests/fake_mcp_server.py 真实子进程）
      → 真实 MCP stdio JSON-RPC tools/call 执行
      → tool 消息回传，桩服务记录第二轮请求供断言

同时覆盖：
- Leader._build_tool_runner 装配/关闭（tools_enabled 默认 false 不装配）；
- tools 与 response_format(json_object) 的互斥；
- 协议正确性：tool 消息前必须有携带 tool_calls 的 assistant 消息；
- offline Provider + tools_enabled=true 不触网、行为不变。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from mas.agents.base import BaseAgent, Perception
from mas.config import load_config
from mas.experience import RetrievalResult
from mas.leader import Leader
from mas.llm import LLMClient
from mas.schemas import NodeTask
from mas.toolrunner import ToolRunner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_MCP_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"


# =========================================================================== 桩服务
class _StubLLMHandler(BaseHTTPRequestHandler):
    """脚本化 OpenAI /chat/completions 桩：记录全部请求，按脚本序列返回。"""

    requests: list = []
    script: list = []

    def log_message(self, fmt, *args):  # 静音
        return

    def do_POST(self):  # noqa: N802（标准库命名）
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        type(self).requests.append(payload)
        index = min(len(type(self).requests) - 1, len(type(self).script) - 1)
        body = type(self).script[index](payload)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _tool_call_payload(payload):
    """第一轮：模型请求调用 echo 工具。"""
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-e2e-0001",
                "type": "function",
                "function": {"name": "echo",
                             "arguments": json.dumps({"text": "ping-from-llm"}, ensure_ascii=False)},
            }],
        }}],
        "usage": {"total_tokens": 42},
    }


def _final_payload(payload):
    """第二轮：模型基于工具结果给出最终 JSON。"""
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": json.dumps({"ok": True, "answer": "tool-result-consumed"}, ensure_ascii=False),
        }}],
        "usage": {"total_tokens": 18},
    }


class _RecordingEvents:
    def __init__(self):
        self.records = []

    def emit(self, type_, **kwargs):
        self.records.append({"type": type_, **kwargs})


class _E2EAgent(BaseAgent):
    """最小 Agent：只验证生成闭环，落地逻辑记录 generation。"""

    role_id = "developer"
    task_kinds = {"default": "requirement_spec"}

    def __init__(self, ctx):
        super().__init__(ctx)
        self.applied = None

    def perceive(self, task):
        return Perception()

    def retrieve_experience(self, task, perception):
        return RetrievalResult()

    def build_prompt(self, task, perception, experience):
        # 真实 LLMClient 会读 bundle.system / bundle.user / bundle.path / bundle.experiences
        return SimpleNamespace(path=None, system="你是研发工程师", user="请实现需求",
                               experiences=[], skills=[], tier_counts={})

    def build_context(self, task, perception):
        return {"requirement_text": "# 订单计价\n\n### FP-01 计算订单金额"}

    def apply(self, task, generation, perception, context, result):
        result.status = "pass"
        result.summary = "applied"
        self.applied = generation


# =========================================================================== 测试
class FunctionCallingE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 桩 LLM 服务（脚本：第一轮 tool_calls，第二轮最终 JSON）
        _StubLLMHandler.requests = []
        _StubLLMHandler.script = [_tool_call_payload, _final_payload]
        self.server = _StubServer(("127.0.0.1", 0), _StubLLMHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"

        # 真实配置（项目 config），config_dir 指向临时目录并写入 mcp.yaml
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        # 本文件验证的是 function calling 线上协议（节点=implement）；节点隔离（tools_nodes）
        # 默认只对 intake 生效，此处显式放开为「全部节点」，使协议验证独立于该默认值。
        # 节点隔离本身的断言见 tests/test_websearch.py。
        self.config.llm["tools_nodes"] = []
        self.config.config_dir = self.root / "config"
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "mcp.yaml").write_text(json.dumps({"servers": [{
            "name": "fake-stdio",
            "transport": "stdio",
            "enabled": True,
            "command": sys.executable,
            "args": [str(FAKE_MCP_SERVER)],
            "timeout_s": 10,
        }]}, ensure_ascii=False), encoding="utf-8")

        # LLM 指向桩服务（凭据外注，不落盘）
        self.config.llm["provider"] = "openai_compatible"
        self.config.llm["model"] = "stub-model"
        self.config.llm["timeout"] = 10
        self.config.llm_overrides = {"api_key": "stub-key", "base_url": self.base_url}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.tmp.cleanup()

    # ------------------------------------------------- 轮数用尽：强制收尾出正文
    def test_tool_rounds_exhausted_forces_text_only_final_round(self):
        """轮数用尽时模型仍请求工具（正文为空）：追加一次**不带 tools** 的生成强制收敛。

        回归 2026-10-17 的真实故障：tool_max_rounds=2 时模型两轮都在请求工具，
        旧逻辑直接返回「finish_reason=tool_calls、正文 0 字符」→ act() 判
        「模型未返回可解析的 JSON」→ 节点 fail（真实运行 20261017-020255-e0cf）。
        """
        self.config.llm["tool_max_rounds"] = 2
        _StubLLMHandler.requests = []
        # 前两轮都请求工具（把轮数用尽），第三轮（收尾）才给出最终 JSON
        _StubLLMHandler.script = [_tool_call_payload, _tool_call_payload, _final_payload]

        runner = ToolRunner(self.config)
        runner.register_from_mcp()
        try:
            events = _RecordingEvents()
            ctx = SimpleNamespace(
                config=self.config, events=events, run_dir=self.root,
                llm=LLMClient(self.config), tools=runner,
            )
            agent = _E2EAgent(ctx)
            task = NodeTask(node_id="implement", title="编码实现", role="developer",
                            kind="task", iteration=1)

            result = agent.act(task)

            # 1) 节点不再因「轮数用尽 + 正文为空」失败
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(agent.applied.data, {"ok": True, "answer": "tool-result-consumed"})
            self.assertEqual(result.metrics.get("tool_rounds"), 2)
            # 2) 真实发包三次：两轮工具 + 一次收尾；收尾轮不带 tools（否则又会要工具）
            self.assertEqual(len(_StubLLMHandler.requests), 3)
            self.assertIn("tools", _StubLLMHandler.requests[0])
            self.assertIn("tools", _StubLLMHandler.requests[1])
            final = _StubLLMHandler.requests[2]
            self.assertNotIn("tools", final)
            self.assertNotIn("tool_choice", final)
            # 收尾轮的历史里必须带着两轮工具结果，模型才能据此给结论
            self.assertEqual([m["role"] for m in final["messages"]].count("tool"), 2)
            # 3) 留痕事件：轮数用尽被显式记录
            exhausted = [r for r in events.records if r["type"] == "tool_rounds_exhausted"]
            self.assertEqual(len(exhausted), 1)
            self.assertEqual(exhausted[0]["payload"]["rounds"], 2)
            self.assertEqual(exhausted[0]["payload"]["tool_calls"], ["echo"])
        finally:
            runner.close()

    # ---------------------------------------------------------------- 全链路
    def test_full_loop_real_mcp_and_wire_protocol(self):
        """端到端：tools 下发 → tool_calls → 真实 MCP 执行 → 消息回传 → 续跑出最终 JSON。"""
        runner = ToolRunner(self.config)
        schemas = runner.register_from_mcp()
        try:
            # 1) MCP 工具清单被转成 OpenAI tools schema
            self.assertEqual([tool["function"]["name"] for tool in schemas], ["echo", "ping"])
            self.assertEqual(schemas[0]["type"], "function")

            llm = LLMClient(self.config)
            events = _RecordingEvents()
            ctx = SimpleNamespace(
                config=self.config, events=events, run_dir=self.root, llm=llm, tools=runner,
            )
            agent = _E2EAgent(ctx)
            task = NodeTask(node_id="implement", title="编码实现", role="developer",
                            kind="task", iteration=1)

            result = agent.act(task)

            # 2) Agent 结果：最终 JSON 被消费
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(agent.applied.data, {"ok": True, "answer": "tool-result-consumed"})
            self.assertEqual(result.metrics.get("tool_rounds"), 1)
            tool_events = [r for r in events.records if r["type"] == "tool_calls_requested"]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0]["payload"]["calls"][0]["name"], "echo")

            # 3) 桩服务收到两轮请求，逐轮断言线上协议
            self.assertEqual(len(_StubLLMHandler.requests), 2)
            first, second = _StubLLMHandler.requests

            self.assertIn("tools", first)                       # tools 已下发
            self.assertEqual(first["tool_choice"], "auto")
            self.assertNotIn("response_format", first)          # tools 与 json_mode 互斥
            self.assertEqual(first["messages"][0]["role"], "system")

            roles = [m["role"] for m in second["messages"]]
            self.assertEqual(roles, ["system", "user", "assistant", "tool"])
            assistant_msg = second["messages"][2]
            self.assertEqual(assistant_msg["tool_calls"][0]["id"], "call-e2e-0001")
            self.assertEqual(assistant_msg["tool_calls"][0]["function"]["name"], "echo")
            tool_msg = second["messages"][3]
            self.assertEqual(tool_msg["tool_call_id"], "call-e2e-0001")
            # 4) MCP tools/call 真实执行的结果（fake server 回显入参）进入回传消息
            self.assertIn("ping-from-llm", tool_msg["content"])
            self.assertIn("isError", tool_msg["content"])   # MCP 标准返回结构

            # 5) llm_traces.jsonl 留痕可查
            trace_lines = (self.root / "llm_traces.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(trace_lines), 2)
        finally:
            runner.close()

    # ---------------------------------------------------------------- Leader 装配
    def test_leader_build_tool_runner_gate(self):
        """tools_enabled 默认 false 不装配；显式 true 时装配并发 tool_runner_ready 事件。"""
        leader = Leader(self.config)
        events = _RecordingEvents()

        # 默认（system.yaml 为 false）→ None，不拉任何 MCP 子进程
        self.assertIsNone(leader._build_tool_runner(events))

        self.config.llm["tools_enabled"] = True
        runner = leader._build_tool_runner(events)
        try:
            self.assertIsNotNone(runner)
            self.assertEqual([t["function"]["name"] for t in runner.schemas], ["echo", "ping"])
            ready = [r for r in events.records if r["type"] == "tool_runner_ready"]
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0]["payload"]["tools"], 2)
        finally:
            if runner is not None:
                runner.close()

    def test_leader_tool_runner_empty_when_no_server(self):
        """tools_enabled=true 但无 MCP 服务：降级为无工具运行并发告警事件。"""
        (self.root / "config" / "mcp.yaml").write_text("servers: []\n", encoding="utf-8")
        self.config.llm["tools_enabled"] = True
        leader = Leader(self.config)
        events = _RecordingEvents()
        runner = leader._build_tool_runner(events)
        self.assertIsNone(runner)
        self.assertTrue(any(r["type"] == "tool_runner_empty"
                            for r in events.records))

    # ---------------------------------------------------------------- 关闭时的行为保持
    def test_disabled_by_default_sends_no_tools(self):
        """tools_enabled=false：ctx.tools=None，请求体不含 tools（行为与修复前一致）。"""
        _StubLLMHandler.script = [_final_payload]   # 未开启工具：首轮即返回最终 JSON
        llm = LLMClient(self.config)
        ctx = SimpleNamespace(config=self.config, events=_RecordingEvents(),
                              run_dir=self.root, llm=llm, tools=None)
        agent = _E2EAgent(ctx)
        result = agent.act(NodeTask(node_id="implement", title="编码", role="developer",
                                    kind="task", iteration=1))
        self.assertEqual(result.status, "pass")
        self.assertEqual(len(_StubLLMHandler.requests), 1)
        self.assertNotIn("tools", _StubLLMHandler.requests[0])   # 未开启即不下发
        self.assertIn("response_format", _StubLLMHandler.requests[0])

    def test_offline_provider_ignores_tools(self):
        """offline Provider + tools_enabled=true：不触网、不执行工具、结果正常。"""
        self.config.llm["provider"] = "offline"
        runner = ToolRunner(self.config)
        runner.register_from_mcp()
        try:
            llm = LLMClient(self.config)
            ctx = SimpleNamespace(config=self.config, events=_RecordingEvents(),
                                  run_dir=self.root, llm=llm, tools=runner)
            agent = _E2EAgent(ctx)
            result = agent.act(NodeTask(node_id="implement", title="编码", role="developer",
                                        kind="task", iteration=1))
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(len(_StubLLMHandler.requests), 0)   # 离线不发 HTTP
            self.assertIsNotNone(agent.applied.data)             # 离线 Provider 正常产出
        finally:
            runner.close()


if __name__ == "__main__":
    unittest.main()
