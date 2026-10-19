"""输出预算（max_tokens 与思考链）治理的回归测试。

背景（本端点实测，GLM）：推理模型把 reasoning token 计入 max_tokens 输出预算，
思考链打满 16000 时返回 finish_reason=length、正文 0 字符。本文件用本地 OpenAI 协议桩
（真实 LLMClient + urllib 真实发包）锁定以下行为：

1. usage 明细（completion_tokens_details.reasoning_tokens）被正确读取；
2. 正文为空且 finish_reason=length 时自动降级重发（reasoning_effort=low）；
3. 降级仍为空时给出可诊断的 error，而不是静默失败；
4. empty_output_retry=0 可关闭降级；
5. Agent 层对不可解析正文发 node_error 事件，且 trace 落盘含预算字段；
6. offline provider 不受影响、不触网。

两级降级与超时（后补）：
7. 降级阶梯 low→high：low 仍截断/空/解析失败时兜底到 high；
8. 网关 504 与客户端读超时进入同一条降级链（原实现会直接抛穿），4xx 仍立即失败不降级。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from mas.agents.base import BaseAgent, Perception
from mas.config import load_config
from mas.errors import AgentError
from mas.experience import RetrievalResult
from mas.llm import LLMClient
from mas.schemas import NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# =========================================================================== 桩服务
class _StubHandler(BaseHTTPRequestHandler):
    requests: list = []
    script: list = []

    def log_message(self, fmt, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        type(self).requests.append(payload)
        index = min(len(type(self).requests) - 1, len(type(self).script) - 1)
        item = type(self).script[index]
        result = item(payload) if callable(item) else item
        if isinstance(result, _SSEResponse):
            data = result.body()
            self.send_response(result.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # 脚本项可返回 (状态码, body) 以模拟网关 504 / 4xx
        status, body = result if isinstance(result, tuple) else (200, result)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- 脚本化响应
def _resp(text, finish_reason="stop", usage=None, reasoning=""):
    message = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage or {"total_tokens": 100}}


DEEPSEEK_USAGE = {
    "prompt_tokens": 42, "completion_tokens": 43, "total_tokens": 85,
    "completion_tokens_details": {"reasoning_tokens": 40},
}

# 思考链吃满预算：正文为空、finish_reason=length（本端点 GLM 的实测形态）
TRUNCATED_USAGE = {"prompt_tokens": 1961, "completion_tokens": 16000, "total_tokens": 17961}


def _truncated(payload):
    return _resp(None, finish_reason="length", usage=TRUNCATED_USAGE, reasoning="让我想想…" * 2000)


# ---------------------------------------------------------------- SSE 桩（流式端点）
class _SSEResponse:
    """脚本项：按 text/event-stream 返回（模拟真实流式端点）。"""

    def __init__(self, lines, status: int = 200):
        self.lines = lines
        self.status = status

    def body(self) -> bytes:
        return "".join(self.lines).encode("utf-8")


def _sse_chunk(*, content=None, reasoning=None, finish_reason=None, usage=None):
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    choice: dict = {"delta": delta}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    chunk: dict = {"choices": [choice]}
    if usage:
        chunk["usage"] = usage
    return chunk


def _sse(*parts, reasoning=None, usage=None, finish_reason="stop"):
    """把若干内容块组装成 SSE 响应（末块带 finish_reason 与 usage，末尾 [DONE]）。"""
    chunks = []
    if reasoning:
        chunks.append(_sse_chunk(reasoning=reasoning))
    chunks.extend(_sse_chunk(content=part) for part in parts)
    if not chunks:
        chunks.append(_sse_chunk())
    lines = []
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            chunk["choices"][0]["finish_reason"] = finish_reason
            if usage:
                chunk["usage"] = usage
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")
    return _SSEResponse(lines)


def _sse_truncated():
    """流式形态的空正文 + length：思考链一块、正文空块截断。"""
    return _SSEResponse([
        f"data: {json.dumps(_sse_chunk(reasoning='让我想想…' * 2000), ensure_ascii=False)}\n\n",
        f"data: {json.dumps(_sse_chunk(finish_reason='length', usage=TRUNCATED_USAGE), ensure_ascii=False)}\n\n",
        "data: [DONE]\n\n",
    ])


def _ok_json(payload):
    return _resp(json.dumps({"mode": "first", "package": "timersys"}, ensure_ascii=False),
                 finish_reason="stop", usage={"prompt_tokens": 1961, "completion_tokens": 900,
                                              "total_tokens": 2861})


class _RecordingEvents:
    def __init__(self):
        self.records = []

    def emit(self, type_, **kwargs):
        self.records.append({"type": type_, **kwargs})


class _BudgetAgent(BaseAgent):
    """最小 Agent：只验证生成与兜底，不落文件。"""

    role_id = "developer"
    task_kinds = {"default": "implementation"}

    def __init__(self, ctx):
        super().__init__(ctx)
        self.applied = None

    def perceive(self, task):
        return Perception()

    def retrieve_experience(self, task, perception):
        return RetrievalResult()

    def build_prompt(self, task, perception, experience):
        return SimpleNamespace(path=None, system="你是研发工程师", user="请实现需求",
                               experiences=[], skills=[], tier_counts={})

    def build_context(self, task, perception):
        return {"spec": {}}

    def apply(self, task, generation, perception, context, result):
        result.status = "pass"
        self.applied = generation


# =========================================================================== 测试
class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _StubHandler.requests = []
        self.server = _StubServer(("127.0.0.1", 0), _StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.config.config_dir = self.root / "config"
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.config.llm["provider"] = "openai_compatible"
        self.config.llm["model"] = "stub-model"
        self.config.llm["timeout"] = 10
        self.config.llm["max_tokens"] = 16000
        # 本文件锁定的是「降级链」语义（504/超时/length → 换 reasoning_effort 重发），
        # 与网络重试层（llm.retry，默认 max_attempts=3）是两层：把重试钉回单次，
        # 避免请求计数被重试层放大（重试分类本身由 tests/test_llm_retry.py 锁定）。
        self.config.llm["retry"] = {"max_attempts": 1, "backoff_s": 0}
        self.config.llm_overrides = {"api_key": "stub-key", "base_url": self.base_url}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _bundle(self):
        return SimpleNamespace(path=None, system="sys", user="user", experiences=[], skills=[], tier_counts={})

    # ---------------------------------------------------------------- 1 usage 明细
    def test_usage_details_parsed(self):
        _StubHandler.script = [lambda p: _resp("56", "stop", DEEPSEEK_USAGE, reasoning="思考")]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(gen.finish_reason, "stop")
        self.assertEqual(gen.prompt_tokens, 42)
        self.assertEqual(gen.completion_tokens, 43)
        self.assertEqual(gen.reasoning_tokens, 40)     # completion_tokens_details
        self.assertEqual(gen.tokens, 85)
        self.assertFalse(gen.degraded)

    def test_usage_without_reasoning_details_is_zero_not_guessed(self):
        """端点不返回 reasoning_tokens 明细时记 0，禁止估算出假数据（GLM 实测形态）。"""
        _StubHandler.script = [lambda p: _resp("{}", "stop", TRUNCATED_USAGE, reasoning="思考链")]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(gen.reasoning_tokens, 0)
        self.assertEqual(gen.completion_tokens, 16000)
        self.assertTrue(gen.reasoning)                  # 思考链文本仍在，但明细未知

    # ---------------------------------------------------------------- 2 降级重发
    def test_empty_output_triggers_downgrade_retry(self):
        _StubHandler.script = [_truncated, _ok_json]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertTrue(gen.text, "降级重发后应拿到正文")
        self.assertTrue(gen.degraded)
        self.assertEqual(gen.data, {"mode": "first", "package": "timersys"})
        self.assertEqual(len(_StubHandler.requests), 2)
        self.assertNotIn("reasoning_effort", _StubHandler.requests[0])   # 首次按端点默认
        self.assertEqual(_StubHandler.requests[1]["reasoning_effort"], "low")

    def test_downgrade_uses_configured_effort(self):
        self.config.llm["downgrade_reasoning_effort"] = "medium"
        _StubHandler.script = [_truncated, _ok_json]
        LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(_StubHandler.requests[1]["reasoning_effort"], "medium")

    def test_reasoning_effort_from_config_is_sent_on_first_call(self):
        self.config.llm["reasoning_effort"] = "low"
        _StubHandler.script = [lambda p: _resp("{}", "stop", DEEPSEEK_USAGE)]
        LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(len(_StubHandler.requests), 1)                  # 有正文不重发
        self.assertEqual(_StubHandler.requests[0]["reasoning_effort"], "low")

    # ---------------------------------------------------------------- 3 仍为空 → 可诊断
    def test_downgrade_still_empty_sets_diagnostic_error(self):
        _StubHandler.script = [_truncated]                                # 恒空
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(gen.text, "")
        self.assertIn("输出被 max_tokens 截断", gen.error)
        self.assertIn("思考链占满输出预算", gen.error)
        self.assertIn("completion_tokens=16000", gen.error)
        self.assertEqual(len(_StubHandler.requests), 3)                  # 原请求 + low + high 两级

    def test_empty_output_retry_zero_disables_downgrade(self):
        self.config.llm["empty_output_retry"] = 0
        _StubHandler.script = [_truncated]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertTrue(gen.error)
        self.assertEqual(len(_StubHandler.requests), 1)

    def test_non_length_empty_output_reports_reason(self):
        """finish_reason 不是 length 的空正文同样要有诊断（不误判为截断）。"""
        _StubHandler.script = [lambda p: _resp(None, "stop", DEEPSEEK_USAGE)]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertIn("finish_reason=stop", gen.error)
        self.assertNotIn("截断", gen.error)
        self.assertEqual(len(_StubHandler.requests), 1)                  # 未截断不降级

    # ---------------------------------------------------------------- 5 Agent 兜底
    def test_agent_reports_node_error_on_unparsable_output(self):
        _StubHandler.script = [lambda p: _resp("这不是 JSON", "stop", DEEPSEEK_USAGE)]
        llm = LLMClient(self.config)
        events = _RecordingEvents()
        ctx = SimpleNamespace(config=self.config, events=events, run_dir=self.root, llm=llm, tools=None)
        agent = _BudgetAgent(ctx)
        result = agent.act(NodeTask(node_id="implement", title="编码实现", role="developer",
                                    kind="task", iteration=1))
        self.assertEqual(result.status, "fail")
        self.assertTrue(result.error)
        errors = [r for r in events.records if r["type"] == "node_error"]
        self.assertEqual(len(errors), 1, "不可解析正文此前静默失败，现在必须告警")
        self.assertIn(result.error, errors[0]["payload"]["error"])

        trace = json.loads((self.root / "llm_traces.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(trace["finish_reason"], "stop")
        self.assertEqual(trace["completion_tokens"], 43)
        self.assertEqual(trace["reasoning_tokens"], 40)
        self.assertIn("degraded", trace)

    def test_agent_trace_records_truncation_and_downgrade(self):
        _StubHandler.script = [_truncated, _ok_json]
        llm = LLMClient(self.config)
        events = _RecordingEvents()
        ctx = SimpleNamespace(config=self.config, events=events, run_dir=self.root, llm=llm, tools=None)
        agent = _BudgetAgent(ctx)
        result = agent.act(NodeTask(node_id="implement", title="编码实现", role="developer",
                                    kind="task", iteration=1))
        self.assertEqual(result.status, "pass")
        trace = json.loads((self.root / "llm_traces.jsonl").read_text(encoding="utf-8").strip())
        self.assertTrue(trace["degraded"])
        self.assertEqual(trace["completion_tokens"], 900)   # 降级后那一次的用量

    # ---------------------------------------------------------------- 7 两级降级阶梯
    def _efforts(self):
        return [p.get("reasoning_effort") for p in _StubHandler.requests]

    def test_two_level_ladder_low_then_high_on_truncation(self):
        """low 仍被截断 → 兜底到 high（low 快但可能想不完，high 慢一档但能收敛）。"""
        _StubHandler.script = [_truncated, _truncated, _ok_json]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(self._efforts(), [None, "low", "high"])
        self.assertEqual(len(_StubHandler.requests), 3)
        self.assertTrue(gen.degraded)
        self.assertEqual(gen.data, {"mode": "first", "package": "timersys"})

    def test_low_half_json_still_truncated_escalates_to_high(self):
        """low 吐出半截 JSON（正文非空但 finish_reason=length）→ 质量不足 → high。"""
        half = '{"mode": "first", "package": "timer'
        _StubHandler.script = [
            _truncated,
            lambda p: _resp(half, "length", TRUNCATED_USAGE),
            _ok_json,
        ]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(self._efforts(), [None, "low", "high"])
        self.assertTrue(gen.text.endswith("}"))          # 用的是 high 那份完整 JSON
        self.assertTrue(gen.data)

    def test_low_unparsable_output_escalates_to_high(self):
        """low 返回不可解析正文（stop 但不是 JSON）→ 质量不足 → high。"""
        _StubHandler.script = [
            _truncated,
            lambda p: _resp("这不是 JSON", "stop", DEEPSEEK_USAGE),
            _ok_json,
        ]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(self._efforts(), [None, "low", "high"])
        self.assertTrue(gen.data)

    def test_low_stops_when_output_is_usable(self):
        """low 已经可用 → 不再往下试 high（避免无谓的 115s 调用）。"""
        _StubHandler.script = [_truncated, _ok_json, _ok_json]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(self._efforts(), [None, "low"])
        self.assertEqual(len(_StubHandler.requests), 2)

    def test_empty_output_retry_one_uses_only_first_level(self):
        self.config.llm["empty_output_retry"] = 1
        _StubHandler.script = [_truncated, _truncated, _ok_json]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(self._efforts(), [None, "low"])
        self.assertIn("截断", gen.error)                 # low 仍截断且无第三级 → 诊断失败

    def test_ladder_accepts_list_form(self):
        self.config.llm["downgrade_reasoning_effort"] = ["medium", "high"]
        _StubHandler.script = [_truncated, _truncated, _ok_json]
        LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(self._efforts(), [None, "medium", "high"])

    # ---------------------------------------------------------------- 8 504 / 读超时
    def test_gateway_504_enters_downgrade_chain_instead_of_raising(self):
        """网关 504（openresty 300s 断开）与 length 截断同等对待 → 换 low 重发。"""
        _StubHandler.script = [
            lambda p: (504, {"error": {"message": "Gateway Time-out"}}),
            _ok_json,
        ]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(len(_StubHandler.requests), 2)
        self.assertEqual(self._efforts(), [None, "low"])
        self.assertTrue(gen.degraded)
        self.assertTrue(gen.text)
        self.assertFalse(gen.timed_out)                  # 被 low 救回

    def test_read_timeout_enters_downgrade_chain(self):
        """读阶段抛的是 TimeoutError（非 URLError 子类），原实现既不重试也直接穿透。"""
        self.config.llm["timeout"] = 1

        def _slow(payload):
            time.sleep(3)
            return _resp("{}", "stop", DEEPSEEK_USAGE)

        _StubHandler.script = [_slow, _ok_json]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(len(_StubHandler.requests), 2)
        self.assertEqual(self._efforts(), [None, "low"])
        self.assertTrue(gen.degraded)
        self.assertTrue(gen.text)

    def test_timeout_after_ladder_exhausted_is_diagnosable(self):
        """整条降级链都超时 → 带 timed_out 与可诊断 error，不静默。"""
        _StubHandler.script = [lambda p: (504, {"error": {"message": "Gateway Time-out"}})]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertTrue(gen.timed_out)
        self.assertIn("网关超时", gen.error)
        self.assertEqual(len(_StubHandler.requests), 3)  # 原请求 + low + high

    def test_http_400_fails_fast_without_downgrade(self):
        """4xx 是请求本身的问题，换 reasoning_effort 无用 → 立即失败，不进降级链。"""
        _StubHandler.script = [lambda p: (400, {"error": {"message": "bad request"}})]
        with self.assertRaises(AgentError):
            LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(len(_StubHandler.requests), 1)

    # ---------------------------------------------------------------- 9 流式（SSE）
    def test_stream_request_carries_stream_flag_and_parses_sse(self):
        """默认流式：请求带 stream=true，SSE 分块正确组装（正文/usage/finish_reason）。"""
        _StubHandler.script = [_sse('{"mode": "first", "package": "timersys"}', usage=DEEPSEEK_USAGE)]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertTrue(_StubHandler.requests[0].get("stream"))
        self.assertEqual(gen.text, '{"mode": "first", "package": "timersys"}')
        self.assertEqual(gen.data, {"mode": "first", "package": "timersys"})
        self.assertEqual(gen.finish_reason, "stop")
        self.assertEqual(gen.reasoning_tokens, 40)
        self.assertEqual(gen.completion_tokens, 43)
        self.assertFalse(gen.degraded)

    def test_stream_assembles_multi_chunk_content_and_reasoning(self):
        """多 chunk 的正文与思考链分别累加，不丢块不乱序。"""
        _StubHandler.script = [_sse("hello ", "world", reasoning="思考链", usage=DEEPSEEK_USAGE)]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(gen.text, "hello world")
        self.assertEqual(gen.reasoning, "思考链")

    def test_stream_truncation_enters_downgrade_chain(self):
        """流式下 length 截断同样触发降级链（流式只解网关 300s 墙，不增预算）。"""
        _StubHandler.script = [_sse_truncated(), _sse('{"mode": "first"}', usage=DEEPSEEK_USAGE)]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertEqual(self._efforts(), [None, "low"])
        self.assertTrue(gen.degraded)
        self.assertEqual(gen.data, {"mode": "first"})
        self.assertTrue(_StubHandler.requests[1].get("stream"))   # 降级重发仍是流式

    def test_stream_non_sse_json_response_falls_back(self):
        """服务端忽略 stream=true 返回普通 JSON（Content-Type 判定）→ 退化为一次性解析。

        现有全部非流式桩测试即走此路径：stream 默认开启不得破坏既有行为。
        """
        _StubHandler.script = [lambda p: _resp("{}", "stop", DEEPSEEK_USAGE)]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertTrue(_StubHandler.requests[0].get("stream"))
        self.assertEqual(gen.text, "{}")
        self.assertEqual(gen.finish_reason, "stop")

    def test_stream_disabled_by_config_uses_plain_post(self):
        """llm.stream=false 时回到非流式 POST（不透流式的端点可显式关闭）。"""
        self.config.llm["stream"] = False
        _StubHandler.script = [lambda p: _resp("{}", "stop", DEEPSEEK_USAGE)]
        gen = LLMClient(self.config).generate("implementation", self._bundle(), {})
        self.assertNotIn("stream", _StubHandler.requests[0])
        self.assertEqual(gen.text, "{}")

    # ---------------------------------------------------------------- 6 offline 不受影响
    def test_offline_provider_untouched(self):
        self.config.llm["provider"] = "offline"
        _StubHandler.script = [_truncated]
        gen = LLMClient(self.config).generate("requirement_spec", self._bundle(), {})
        self.assertEqual(gen.provider, "offline")
        self.assertTrue(gen.data)
        self.assertEqual(_StubHandler.requests, [])          # 不触网


if __name__ == "__main__":
    unittest.main()
