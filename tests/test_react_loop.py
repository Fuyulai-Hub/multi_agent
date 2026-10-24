"""ReAct（Thought → Action → Observation）循环验证。

与 test_function_calling_e2e.py 同一套真实组件套路：本地 OpenAI 协议桩服务 + 真实
LLMClient + 真实 ToolRunner（拉起 tests/fake_mcp_server.py 真实 MCP 子进程），
验证 ReAct 循环的三件事：

1. 每一步的 Thought / Action / Observation 都被显式抽取并落 ``react_step`` 事件；
2. 步数由 ``runtime.react.max_steps`` 独立控制，用尽时追加不带 tools 的收尾轮强制收敛；
3. 默认关闭（``react.enabled=false``）时走改造前的工具闭环，事件与 Prompt 逐字节不变。
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

from mas.agents.base import REACT_DIRECTIVE, BaseAgent, Perception
from mas.config import load_config
from mas.experience import RetrievalResult
from mas.llm import LLMClient
from mas.schemas import NodeTask
from mas.toolrunner import ToolRunner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_MCP_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"

THOUGHT_1 = "思考：需求未给出运费计算规则，先用 echo 工具确认该算子的既有口径，再决定实现。"
FINAL_JSON = {"ok": True, "answer": "react-converged"}


# =========================================================================== 桩服务
class _StubLLMHandler(BaseHTTPRequestHandler):
    requests: list = []
    script: list = []

    def log_message(self, fmt, *args):  # 静音
        return

    def do_POST(self):  # noqa: N802
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


def _thought_then_tool(payload):
    """一步 ReAct：正文写思考（Thought），同时发起工具调用（Action）。"""
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": THOUGHT_1,
            "tool_calls": [{
                "id": "call-react-0001",
                "type": "function",
                "function": {"name": "echo",
                             "arguments": json.dumps({"text": "ping-from-llm"}, ensure_ascii=False)},
            }],
        }}],
        "usage": {"total_tokens": 42},
    }


def _tool_only(payload):
    """只要工具、不写思考的一步（正文为空）。"""
    body = _thought_then_tool(payload)
    body["choices"][0]["message"]["content"] = None
    return body


def _final_answer(payload):
    """不再请求工具的一步：给出最终 JSON。"""
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": json.dumps(FINAL_JSON, ensure_ascii=False),
        }}],
        "usage": {"total_tokens": 18},
    }


class _RecordingEvents:
    def __init__(self):
        self.records = []

    def emit(self, type_, **kwargs):
        self.records.append({"type": type_, **kwargs})


class _RecordingPrompts:
    """记录 build() 入参（用于断言 directive 是否注入 ReAct 协议），返回最小 bundle。"""

    def __init__(self):
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        directive = str(kwargs.get("directive") or "")
        return SimpleNamespace(
            path=None, system="你是研发工程师",
            user=f"请实现需求\n\n[DIRECTIVE]{directive}[/DIRECTIVE]",
            experiences=[], skills=[], tier_counts={},
        )


class _ReactAgent(BaseAgent):
    """走真实 build_prompt（不覆盖），只接管感知/经验/落地。"""

    role_id = "developer"
    task_kinds = {"default": "requirement_spec"}

    def __init__(self, ctx):
        super().__init__(ctx)
        self.applied = None

    def perceive(self, task):
        return Perception()

    def retrieve_experience(self, task, perception):
        return RetrievalResult()

    def build_context(self, task, perception):
        return {"requirement_text": "# 订单计价\n\n### FP-01 计算订单金额"}

    def apply(self, task, generation, perception, context, result):
        result.status = "pass"
        result.summary = "applied"
        self.applied = generation


# =========================================================================== 测试
class ReactLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _StubLLMHandler.requests = []
        _StubLLMHandler.script = [_thought_then_tool, _final_answer]
        self.server = _StubServer(("127.0.0.1", 0), _StubLLMHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.config.runtime["skills"] = {"enabled": False}      # 不扫技能目录，测试保持轻量
        self.config.llm["tools_nodes"] = []                     # 本文件验证 ReAct 自身，放开节点隔离
        self.config.config_dir = self.root / "config"
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "mcp.yaml").write_text(json.dumps({"servers": [{
            "name": "fake-stdio", "transport": "stdio", "enabled": True,
            "command": sys.executable, "args": [str(FAKE_MCP_SERVER)], "timeout_s": 10,
        }]}, ensure_ascii=False), encoding="utf-8")

        self.config.llm["provider"] = "openai_compatible"
        self.config.llm["model"] = "stub-model"
        self.config.llm["timeout"] = 10
        self.config.llm_overrides = {"api_key": "stub-key", "base_url": self.base_url}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _runner(self):
        runner = ToolRunner(self.config)
        runner.register_from_mcp()
        return runner

    def _ctx(self, runner, prompts):
        return SimpleNamespace(config=self.config, events=_RecordingEvents(), run_dir=self.root,
                               llm=LLMClient(self.config), tools=runner, prompts=prompts)

    def _task(self):
        return NodeTask(node_id="implement", title="编码实现", role="developer",
                        kind="task", iteration=1)

    def _set_react(self, **kwargs):
        cfg = {"enabled": True, "nodes": ["implement"], "max_steps": 4,
               "thought_max_chars": 2000, "observation_max_chars": 1500}
        cfg.update(kwargs)
        self.config.runtime["react"] = cfg

    # ------------------------------------------------------- 开启：完整 ReAct 轨迹
    def test_react_emits_thought_action_observation(self):
        """开启后每步落 react_step（思考 + 动作 + 观察），收敛那步落 react_final。"""
        self._set_react(max_steps=4)
        runner = self._runner()
        prompts = _RecordingPrompts()
        try:
            ctx = self._ctx(runner, prompts)
            agent = _ReactAgent(ctx)
            result = agent.act(self._task())

            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(agent.applied.data, FINAL_JSON)
            self.assertEqual(result.metrics.get("react_steps"), 2)

            steps = [r for r in ctx.events.records if r["type"] == "react_step"]
            finals = [r for r in ctx.events.records if r["type"] == "react_final"]
            self.assertEqual(len(steps), 1)
            self.assertEqual(len(finals), 1)

            step = steps[0]["payload"]
            self.assertEqual(step["step"], 1)
            self.assertEqual(step["thought"], THOUGHT_1)          # 思考被完整抽出
            self.assertEqual(step["thought_chars"], len(THOUGHT_1))
            self.assertEqual(step["actions"][0]["name"], "echo")  # Action
            # Observation：MCP 真实执行结果进入事件摘要
            self.assertEqual(len(step["observations"]), 1)
            self.assertEqual(step["observations"][0]["name"], "echo")
            self.assertIn("ping-from-llm", step["observations"][0]["preview"])
            self.assertEqual(finals[0]["payload"]["step"], 2)

            # 协议：两次请求都下发 tools；第二轮携带 assistant(tool_calls) + tool 消息
            self.assertEqual(len(_StubLLMHandler.requests), 2)
            self.assertIn("tools", _StubLLMHandler.requests[0])
            second = _StubLLMHandler.requests[1]
            self.assertEqual([m["role"] for m in second["messages"]],
                             ["system", "user", "assistant", "tool"])
            self.assertIn("ping-from-llm", second["messages"][3]["content"])
            # 协议指令进 user prompt，思考作为 assistant 消息的 content 一并下发
            self.assertIn("ReAct 执行协议", second["messages"][1]["content"])
            self.assertEqual(second["messages"][2]["content"], THOUGHT_1)

            # Prompt 侧：ReAct 协议指令注入 directive 并随 prompts/*.md 落盘
            self.assertIn("ReAct 执行协议", prompts.calls[0]["directive"])
            self.assertIn("ReAct 执行协议", REACT_DIRECTIVE)
        finally:
            runner.close()

    # ------------------------------------------------------------- 步数用尽收敛
    def test_react_step_limit_forces_text_only_final_round(self):
        """max_steps 用尽仍在要工具：落 react_step_limit + 追加不带 tools 的收尾轮。"""
        self._set_react(max_steps=2)
        _StubLLMHandler.requests = []
        _StubLLMHandler.script = [_tool_only, _tool_only, _final_answer]
        runner = self._runner()
        try:
            ctx = self._ctx(runner, _RecordingPrompts())
            result = _ReactAgent(ctx).act(self._task())

            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(_ReactAgent(ctx).applied, None)   # 新实例未被 apply，仅确认无副作用
            steps = [r for r in ctx.events.records if r["type"] == "react_step"]
            limits = [r for r in ctx.events.records if r["type"] == "react_step_limit"]
            self.assertEqual(len(steps), 2)
            self.assertEqual(len(limits), 1)
            self.assertEqual(limits[0]["payload"]["steps"], 2)
            self.assertEqual(limits[0]["payload"]["tool_calls"], ["echo"])

            self.assertEqual(len(_StubLLMHandler.requests), 3)
            final = _StubLLMHandler.requests[2]
            self.assertNotIn("tools", final)                     # 收尾轮不再下发工具
            self.assertEqual([m["role"] for m in final["messages"]].count("tool"), 2)
        finally:
            runner.close()

    def test_react_step_limit_forces_final_round_when_text_is_thought(self):
        """步数用尽时正文是 Thought（非空但非 JSON）：同样必须触发收尾轮。

        回归 2026-10-20 真实长任务：3 步 ReAct 全用来读文件，步数用尽时模型仍在请求
        工具、正文是 213 字符的思考；按「正文为空」判定收尾轮永不触发 →
        节点以「模型未返回可解析的 JSON」失败。判定已改为「data is None」。
        """
        self._set_react(max_steps=2)
        _StubLLMHandler.requests = []
        # 两步都要工具且正文是思考文本（THOUGHT_1 不是 JSON），第三步才给结论
        _StubLLMHandler.script = [_thought_then_tool, _thought_then_tool, _final_answer]
        runner = self._runner()
        try:
            ctx = self._ctx(runner, _RecordingPrompts())
            agent = _ReactAgent(ctx)
            result = agent.act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(agent.applied.data, FINAL_JSON)
            limits = [r for r in ctx.events.records if r["type"] == "react_step_limit"]
            self.assertEqual(len(limits), 1)
            self.assertEqual(len(_StubLLMHandler.requests), 3)
            self.assertNotIn("tools", _StubLLMHandler.requests[2])   # 收尾轮不带工具
        finally:
            runner.close()

    def test_react_no_final_round_when_conclusion_already_parseable(self):
        """步数用尽但末步正文已是可解析 JSON：不再追加收尾轮（避免多余调用）。"""
        self._set_react(max_steps=1)
        _StubLLMHandler.requests = []

        def _tool_with_json(payload):
            body = _thought_then_tool(payload)
            body["choices"][0]["message"]["content"] = json.dumps(FINAL_JSON, ensure_ascii=False)
            return body

        _StubLLMHandler.script = [_tool_with_json]
        runner = self._runner()
        try:
            ctx = self._ctx(runner, _RecordingPrompts())
            result = _ReactAgent(ctx).act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(len(_StubLLMHandler.requests), 1)
            self.assertFalse([r for r in ctx.events.records
                              if r["type"] == "react_step_limit"])
        finally:
            runner.close()

    def test_react_requires_thought_warning(self):
        """require_thought=true：只在发起 Action 的步骤校验思考，收敛步不要求。"""
        self._set_react(max_steps=2, require_thought=True)
        _StubLLMHandler.script = [_tool_only, _final_answer]
        runner = self._runner()
        try:
            ctx = self._ctx(runner, _RecordingPrompts())
            result = _ReactAgent(ctx).act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            missing = [r for r in ctx.events.records if r["type"] == "react_thought_missing"]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["payload"]["step"], 1)
            self.assertEqual(missing[0].get("severity"), "warning")
        finally:
            runner.close()

    # ------------------------------------------------------------- 思考抽取来源
    def test_extract_thought_prefers_json_field_then_text(self):
        """抽取优先级：正文 JSON 的 thought 字段 > 非 JSON 正文 > reasoning_content。"""
        from mas.llm import Generation

        self.assertEqual(BaseAgent._extract_thought(
            Generation(text='{"thought": "先查口径", "other": 1}', data={"thought": "先查口径"})),
            "先查口径")
        self.assertEqual(BaseAgent._extract_thought(Generation(text="我打算先查口径")), "我打算先查口径")
        self.assertEqual(BaseAgent._extract_thought(
            Generation(text="", reasoning="内部思考链")), "内部思考链")
        # 正文是纯 JSON 且无 thought 字段时不把它当思考，回落 reasoning
        self.assertEqual(BaseAgent._extract_thought(
            Generation(text='{"a": 1}', data={"a": 1}, reasoning="内部思考链")), "内部思考链")

    # ------------------------------------------------ 收敛步：混排正文恢复 JSON
    def test_react_final_recovers_json_from_thought_text(self):
        """收敛步正文是「思考 + ```json 块」：恢复出 JSON，节点不判不可解析。

        回归真机验证（2026-10-20）：模型在收敛步写出 872 字符「观察结果…```json{…}```」，
        parse_json_block 解不出 → act() 判「模型未返回可解析的 JSON」→ 节点 fail。
        """
        # 复现真机形态：正文中夹有 `{}` 这类干扰花括号，使 parse_json_block 的
        # 「首个 { 到最后一个 }」子串含中文而解析失败。
        mixed = (
            "观察结果：ping 返回 `{}`（`isError: false`），服务可达。"
            "两次工具调用均已完成，现输出结论：\n\n"
            "```json\n" + json.dumps(FINAL_JSON, ensure_ascii=False) + "\n```"
        )
        self.assertIsNone(__import__("mas.llm", fromlist=["parse_json_block"])
                          .parse_json_block(mixed))   # 前置条件：原解析器确实解不出
        def _mixed_final(payload):
            return {"choices": [{"message": {"role": "assistant", "content": mixed}}],
                    "usage": {"total_tokens": 30}}

        self._set_react(max_steps=4)
        _StubLLMHandler.requests = []
        _StubLLMHandler.script = [_thought_then_tool, _mixed_final]
        runner = self._runner()
        try:
            ctx = self._ctx(runner, _RecordingPrompts())
            agent = _ReactAgent(ctx)
            result = agent.act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(agent.applied.data, FINAL_JSON)
            finals = [r for r in ctx.events.records if r["type"] == "react_final"]
            self.assertEqual(len(finals), 1)
            self.assertIn("观察结果", finals[0]["payload"]["thought"])
        finally:
            runner.close()

    def test_recover_final_json_variants(self):
        """recover_final_json：代码块 / 括号平衡 / 无 JSON 三种输入。"""
        from mas.agents.base import recover_final_json

        self.assertEqual(recover_final_json("思考\n```json\n{\"a\": 1}\n```"), {"a": 1})
        self.assertEqual(recover_final_json("结论如下：{\"a\": 1}，以上。"), {"a": 1})
        # 字符串内的花括号不参与配对
        self.assertEqual(recover_final_json('前缀 {"t": "} not end", "n": 2} 后缀'),
                         {"t": "} not end", "n": 2})
        self.assertIsNone(recover_final_json("没有任何 JSON 的纯文本结论"))
        self.assertIsNone(recover_final_json(""))
        # 首个 { 解不出时继续往后扫
        self.assertEqual(recover_final_json("坏片段 {oops 之后 {\"ok\": true}"), {"ok": True})

    def test_legacy_path_unaffected_by_recover(self):
        """recover_final_json 只在 ReAct 分支生效：关闭时混排正文仍按改造前口径判失败。"""
        mixed = ("观察结果：ping 返回 `{}`，服务可达。结论如下：\n```json\n"
                 + json.dumps(FINAL_JSON, ensure_ascii=False) + "\n```")
        self.config.runtime.pop("react", None)
        self.config.llm["tools_enabled"] = False
        runner = self._runner()
        try:
            ctx = self._ctx(runner, _RecordingPrompts())

            def _mixed(payload):
                return {"choices": [{"message": {"role": "assistant", "content": mixed}}],
                        "usage": {"total_tokens": 30}}
            _StubLLMHandler.script = [_mixed]
            agent = _ReactAgent(ctx)
            result = agent.act(self._task())
            self.assertEqual(result.status, "fail")     # 改造前：不可解析即失败（口径不变）
            self.assertIn("模型未返回可解析的 JSON", result.error)
        finally:
            runner.close()

    # ------------------------------------------------------- 关闭：行为逐字节不变
    def test_disabled_by_default_keeps_legacy_loop(self):
        """react 未开启：走改造前的 _generate_with_tools，无 ReAct 事件、directive 不含协议。"""
        self.config.runtime.pop("react", None)
        runner = self._runner()
        prompts = _RecordingPrompts()
        try:
            ctx = self._ctx(runner, prompts)
            result = _ReactAgent(ctx).act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(result.metrics.get("tool_rounds"), 1)     # 改造前口径
            self.assertNotIn("react_steps", result.metrics)
            self.assertFalse([r for r in ctx.events.records
                              if r["type"].startswith("react_")])
            self.assertNotIn("ReAct 执行协议", prompts.calls[0]["directive"])
        finally:
            runner.close()

    def test_node_not_in_whitelist_falls_back(self):
        """react.nodes 白名单未命中该节点：仍走改造前闭环。"""
        self._set_react(nodes=["intake"])          # 本用例节点是 implement
        runner = self._runner()
        prompts = _RecordingPrompts()
        try:
            ctx = self._ctx(runner, prompts)
            result = _ReactAgent(ctx).act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            self.assertNotIn("react_steps", result.metrics)
            self.assertNotIn("ReAct 执行协议", prompts.calls[0]["directive"])
        finally:
            runner.close()

    def test_react_without_tools_degrades_to_single_round(self):
        """react 开启但 tools_nodes 未放行该节点：无工具可用，退化为单轮生成，不报错。

        并且 directive 不得再注入 ReAct 协议——真机反例（task09-miniledger-02 implement）：
        提示词按协议要求模型"先探查工作区再输出"，但请求里没有 tools，模型 5 秒返回
        空文件清单，节点以「实现未产出任何文件」失败。
        """
        self._set_react()
        self.config.llm["tools_nodes"] = ["intake"]
        _StubLLMHandler.requests = []
        _StubLLMHandler.script = [_final_answer]
        runner = self._runner()
        prompts = _RecordingPrompts()
        try:
            ctx = self._ctx(runner, prompts)
            result = _ReactAgent(ctx).act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(len(_StubLLMHandler.requests), 1)
            self.assertNotIn("tools", _StubLLMHandler.requests[0])
            self.assertFalse([r for r in ctx.events.records if r["type"] == "react_step"])
            self.assertNotIn("ReAct 执行协议", prompts.calls[0]["directive"])
        finally:
            runner.close()

    def test_offline_provider_with_react_enabled(self):
        """offline Provider + react.enabled：不触网、不执行工具、结果正常。"""
        self._set_react()
        self.config.llm["provider"] = "offline"
        runner = self._runner()
        try:
            ctx = self._ctx(runner, _RecordingPrompts())
            agent = _ReactAgent(ctx)
            result = agent.act(self._task())
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(len(_StubLLMHandler.requests), 0)
            self.assertIsNotNone(agent.applied.data)
        finally:
            runner.close()


if __name__ == "__main__":
    unittest.main()
