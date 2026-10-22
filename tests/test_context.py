"""上下文治理（Codex 式 truncate → trim → compact）回归测试，docs/35。

锁定的契约（任何一条被破坏都会让"最小改动"变成"静默改请求体"）：

1. 估算口径与 Codex 一致：UTF-8 字节数 / 4；
2. **关闭时恒等**：``fit`` 原样返回，请求体逐字节不变（所有既有测试的基线）；
3. **未超限不动**：即使开启，未达 compact_limit 也不许碰请求体；
4. 三阶段各自的行为与顺序（truncate → trim → compact）；
5. ``system`` 与首个 ``user`` 永不被改（改了就切坏交付契约）；
6. trim 按「工具往返」整轮丢弃，保证 tool_call_id 与 assistant 的配对不散；
7. 丢过内容必须挂摘要（禁止静默丢信息），即使 trim 后已回到预算内；
8. 仍超硬上限只标 overflow 告警，不再继续切；
9. 校准默认关（保持确定性），开启后按端点实测 prompt_tokens 收敛；
10. offline provider 不受影响、不触网。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from mas.agents.base import BaseAgent, Perception
from mas.config import load_config
from mas.context import (
    APPROX_BYTES_PER_TOKEN,
    ContextBudget,
    ContextManager,
    FitResult,
    SUMMARY_PREFIX,
    approx_token_count,
    build_summary,
    estimate_messages,
    message_tokens,
)
from mas.experience import RetrievalResult
from mas.llm import LLMClient
from mas.schemas import NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _msg(role: str, content: str, **extra) -> dict:
    message = {"role": role, "content": content}
    message.update(extra)
    return message


def _assistant_call(call_id: str, name: str) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": "{}"}}]}


def _tool(call_id: str, name: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def _history(rounds: int = 3, chars: int = 4000) -> list:
    """造一段多轮工具往返历史（head 为 system + 首个 user）。"""
    messages = [_msg("system", "S" * 200), _msg("user", "U" * 200)]
    for index in range(rounds):
        messages.append(_assistant_call(f"c{index}", f"tool_{index}"))
        messages.append(_tool(f"c{index}", f"tool_{index}", chr(ord("A") + index) * chars))
    return messages


# =========================================================================== 1 估算
class TokenEstimateTests(unittest.TestCase):
    def test_bytes_divided_by_four_matches_codex(self):
        """Codex: (len + BYTES_PER_TOKEN - 1) / BYTES_PER_TOKEN（truncate.rs:87）。"""
        self.assertEqual(APPROX_BYTES_PER_TOKEN, 4)
        self.assertEqual(approx_token_count(""), 0)
        self.assertEqual(approx_token_count("abcd"), 1)        # 4B -> 1
        self.assertEqual(approx_token_count("abcde"), 2)       # 5B -> 2
        self.assertEqual(approx_token_count("中文"), 2)         # 6B -> 2（0.75 token/字，粗下界）

    def test_message_tokens_counts_all_model_visible_fields(self):
        message = _msg("assistant", "正文", reasoning_content="思考链", name="x")
        message["tool_calls"] = [{"id": "1", "function": {"name": "t", "arguments": "{}"}}]
        self.assertGreater(message_tokens(message), approx_token_count("正文"))

    def test_image_part_uses_codex_constant(self):
        message = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}
        self.assertEqual(message_tokens(message), 1843 + 4)     # 7373B/4 + 协议开销

    def test_estimate_messages_sums(self):
        messages = [_msg("system", "a" * 400), _msg("user", "b" * 400)]
        self.assertEqual(estimate_messages(messages), 200 + 8)


# =========================================================================== 2 预算
class BudgetTests(unittest.TestCase):
    def test_codex_thresholds_90_and_95_percent(self):
        budget = ContextBudget(context_window=100000)
        self.assertEqual(budget.usable, 100000)
        self.assertEqual(budget.compact_limit, 90000)
        self.assertEqual(budget.hard_limit, 95000)

    def test_reserve_output_shrinks_usable_window(self):
        """与 Codex 的显式分歧：先扣 max_tokens，否则会把撞墙误判成有余量。"""
        budget = ContextBudget(context_window=262144, reserve_output=64000)
        self.assertEqual(budget.usable, 198144)
        self.assertEqual(budget.compact_limit, 178329)          # int(198144*0.9)
        self.assertGreaterEqual(budget.hard_limit, budget.compact_limit)

    def test_budget_floor_is_one_token(self):
        """窗口被预留额度吃光时不得返回 0/负数（会导致"永远超限"死循环）。"""
        budget = ContextBudget(context_window=100, reserve_output=1000)
        self.assertEqual(budget.usable, 1)
        self.assertEqual(budget.compact_limit, 1)


# =========================================================================== 3~8 fit
class FitTests(unittest.TestCase):
    def _manager(self, window: int = 100000, **kwargs) -> ContextManager:
        return ContextManager(ContextBudget(window), enabled=True, **kwargs)

    # ------------------------------------------------------------ 2 关闭时恒等
    def test_disabled_fit_is_identity(self):
        messages = _history()
        manager = ContextManager(ContextBudget(100), enabled=False)
        result = manager.fit(messages)
        self.assertFalse(result.changed)
        self.assertEqual(result.stages, [])
        self.assertEqual(result.messages, messages)             # 逐内容相等
        self.assertEqual(result.before_tokens, result.after_tokens)

    def test_disabled_even_when_massively_over_window(self):
        """关闭时无论超多少都不动——这是"最小改动"的核心承诺。"""
        messages = _history(rounds=5, chars=20000)
        manager = ContextManager(ContextBudget(10), enabled=False)
        self.assertEqual(manager.fit(messages).messages, messages)

    # ------------------------------------------------------------ 3 未超限不动
    def test_under_limit_no_change(self):
        messages = _history(rounds=1, chars=100)
        result = self._manager(window=100000).fit(messages)
        self.assertFalse(result.changed)
        self.assertEqual(result.messages, messages)

    def test_exactly_at_limit_no_change(self):
        manager = self._manager(window=100000)
        messages = _history(rounds=1, chars=100)
        tokens = manager.estimate(messages)
        exact = ContextManager(ContextBudget(int(tokens / 0.9) + 1000), enabled=True)
        self.assertFalse(exact.fit(messages).changed)

    # ------------------------------------------------------------ 4 阶段一
    def test_truncate_stage_shrinks_tool_output_keeping_head_and_tail(self):
        """头 60% + 尾 40%：错误堆栈与最终结果在末尾，必须留住。"""
        messages = _history(rounds=1, chars=40000)          # 约 10k token
        result = self._manager(window=6000, tool_result_chars=2000).fit(messages)  # 阈值 5.4k
        self.assertIn("truncate", result.stages)
        self.assertEqual(result.truncated, 1)
        content = result.messages[-1]["content"]
        self.assertIn("省略", content)
        self.assertTrue(content.startswith("A" * 1200))
        self.assertTrue(content.endswith("A" * 800))
        self.assertLessEqual(result.after_tokens, result.limit)

    def test_truncate_stage_alone_satisfies_budget(self):
        """阶段一就够了就不许继续丢历史（能不丢就不丢）。"""
        messages = _history(rounds=3, chars=40000)
        result = self._manager(window=6000, tool_result_chars=2000).fit(messages)
        self.assertEqual(result.stages, ["truncate"])
        self.assertEqual(result.dropped, 0)
        self.assertFalse(result.compacted)

    # ------------------------------------------------------------ 6 阶段二
    def test_trim_drops_oldest_round_as_a_unit(self):
        """整轮丢弃：assistant(tool_calls) 与其后 tool 同生共死，配对不散。"""
        messages = _history(rounds=3, chars=8000)           # 约 6.2k token
        manager = self._manager(window=4000, tool_result_chars=0, keep_recent_rounds=1)  # 阈值 3.6k
        result = manager.fit(messages)
        self.assertIn("trim", result.stages)
        roles = [m.get("role") for m in result.messages]
        # 剩下的历史里每个 tool 之前必有 assistant，且不出现孤立 tool
        for index, role in enumerate(roles):
            if role == "tool":
                self.assertIn(result.messages[index - 1].get("role"), ("assistant", "tool"))
        kept_tools = [m for m in result.messages if m.get("role") == "tool"]
        self.assertEqual(len(kept_tools), 1)                    # keep_recent_rounds=1
        self.assertEqual(kept_tools[0]["tool_call_id"], "c2")   # 保留的是最近一轮
        self.assertEqual(kept_tools[0]["content"][:1], "C")

    def test_trim_respects_keep_recent_rounds(self):
        messages = _history(rounds=4, chars=8000)
        manager = self._manager(window=6000, tool_result_chars=0, keep_recent_rounds=2)
        result = manager.fit(messages)
        self.assertEqual(len([m for m in result.messages if m.get("role") == "tool"]), 2)

    # ------------------------------------------------------------ 7 阶段三
    def test_compact_appends_summary_after_trim(self):
        messages = _history(rounds=3, chars=8000)
        manager = self._manager(window=4000, tool_result_chars=0, keep_recent_rounds=1)
        result = manager.fit(messages)
        self.assertIn("compact", result.stages)
        self.assertTrue(result.compacted)
        self.assertGreater(result.summary_chars, 0)
        tail = result.messages[-1]
        self.assertEqual(tail["role"], "user")
        self.assertTrue(tail["content"].startswith(SUMMARY_PREFIX))
        self.assertIn("tool_0", tail["content"])                # 被丢掉的动作进了摘要
        self.assertIn("tool_1", tail["content"])

    def test_summary_is_appended_even_when_trim_already_fits(self):
        """体积回到预算内 ≠ 信息没丢。丢过就必须留摘要，禁止静默丢。"""
        messages = [
            _msg("system", "S" * 200), _msg("user", "U" * 200),
            _assistant_call("c0", "old_tool"), _tool("c0", "old_tool", "A" * 60000),
            _assistant_call("c1", "new_tool"), _tool("c1", "new_tool", "B" * 100),
        ]
        manager = self._manager(window=6000, tool_result_chars=0, keep_recent_rounds=1)
        result = manager.fit(messages)
        self.assertTrue(result.compacted)
        self.assertIn("old_tool", result.messages[-1]["content"])

    def test_summary_fn_can_be_injected_for_llm_summarization(self):
        """留注入点：需要模型摘要时从外部传，不必改本模块（默认仍是规则抽取）。"""
        manager = self._manager(window=4000, tool_result_chars=0, keep_recent_rounds=1)
        manager.summary_fn = lambda dropped: "外部摘要"
        result = manager.fit(_history(rounds=3, chars=8000))
        self.assertEqual(result.messages[-1]["content"], "外部摘要")

    # ------------------------------------------------------------ 5 骨架不可动
    def test_system_and_first_user_never_modified(self):
        messages = _history(rounds=3, chars=8000)
        result = self._manager(window=4000, tool_result_chars=1000).fit(messages)
        self.assertEqual(result.messages[0], messages[0])
        self.assertEqual(result.messages[1], messages[1])

    def test_prefix_detection_covers_developer_role(self):
        messages = [_msg("system", "s"), _msg("developer", "d"), _msg("user", "u"),
                    _tool("c0", "t", "A" * 40000)]
        result = self._manager(window=1000, tool_result_chars=500).fit(messages)
        self.assertEqual(result.messages[:3], messages[:3])

    # ------------------------------------------------------------ 8 overflow
    def test_overflow_flagged_not_contract_broken(self):
        """system+user 本身超窗：只告警，绝不切骨架（切了交付契约就坏了）。"""
        messages = [_msg("system", "S" * 40000),
                    _msg("user", "必须输出 JSON：{\"files\": []}" + "U" * 40000)]
        manager = self._manager(window=1000, tool_result_chars=100)
        result = manager.fit(messages)
        self.assertIn("overflow", result.stages)
        self.assertTrue(result.overflow)
        self.assertEqual(result.messages[:2], messages[:2])     # 骨架原样
        self.assertIn('"files": []', result.messages[1]["content"])

    def test_overflow_is_not_flagged_when_history_was_the_problem(self):
        messages = _history(rounds=3, chars=8000)
        result = self._manager(window=4000, tool_result_chars=0).fit(messages)
        self.assertFalse(result.overflow)

    # ------------------------------------------------------------ 空输入
    def test_empty_and_none_messages_are_safe(self):
        self.assertEqual(self._manager().fit([]).messages, [])
        self.assertEqual(self._manager().fit(None).messages, [])

    def test_fit_result_to_dict_is_json_serializable(self):
        result = self._manager(window=4000, tool_result_chars=0).fit(_history(rounds=3, chars=8000))
        json.dumps(result.to_dict(), ensure_ascii=False)


# =========================================================================== 摘要
class SummaryTests(unittest.TestCase):
    def test_extracts_actions_observations_and_conclusions(self):
        dropped = [_assistant_call("c0", "read_file"), _tool("c0", "read_file", "文件内容"),
                   _msg("assistant", "我先记下这个结论")]
        text = build_summary(dropped)
        self.assertIn("- 动作：调用工具 read_file", text)
        self.assertIn("- 观察（read_file）：文件内容", text)
        self.assertIn("- 结论：我先记下这个结论", text)

    def test_summary_respects_max_chars(self):
        dropped = [_tool("c0", "t", "X" * 5000)]
        text = build_summary(dropped, max_chars=300, per_item_chars=400)
        # max_chars 约束的是正文（不含前缀），故上限 = 前缀 + 正文 + 截断标记
        self.assertLessEqual(len(text), len(SUMMARY_PREFIX) + 300 + 30)
        self.assertIn("摘要截断", text)

    def test_empty_input_yields_empty_summary(self):
        self.assertEqual(build_summary([]), "")


# =========================================================================== 9 校准
class CalibrationTests(unittest.TestCase):
    def test_observe_records_reported_but_does_not_move_estimate_by_default(self):
        manager = ContextManager(ContextBudget(100000), enabled=True)
        messages = _history(rounds=1, chars=400)
        before = manager.estimate(messages)
        manager.fit(messages)
        manager.observe(SimpleNamespace(prompt_tokens=before * 3))
        self.assertEqual(manager.last_reported, before * 3)
        self.assertEqual(manager.estimate(messages), before)     # 默认不校准，保持确定性

    def test_calibration_converges_when_enabled(self):
        manager = ContextManager(ContextBudget(100000), enabled=True, calibrate=True)
        messages = _history(rounds=1, chars=400)
        base = manager.estimate(messages)
        manager.fit(messages)
        for _ in range(20):                                      # 端点实际是估算的 2 倍
            manager.observe(SimpleNamespace(prompt_tokens=base * 2))
        self.assertGreater(manager.estimate(messages), base)     # 被拉高
        self.assertLess(manager.estimate(messages), base * 3)    # 但不会失控

    def test_observe_ignores_missing_usage(self):
        manager = ContextManager(ContextBudget(100000), enabled=True, calibrate=True)
        manager.last_tokens = 100
        manager.observe(SimpleNamespace(prompt_tokens=0))
        self.assertEqual(manager.last_reported, 0)
        self.assertEqual(manager._ratio, 1.0)

    def test_stats_exposes_budget_and_observation(self):
        manager = ContextManager(ContextBudget(262144, 64000), enabled=True)
        stats = manager.stats()
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["context_window"], 262144)
        self.assertEqual(stats["reserve_output"], 64000)
        self.assertEqual(stats["compact_limit"], 178329)


# =========================================================================== 配置
class ConfigTests(unittest.TestCase):
    def test_from_config_defaults_to_disabled(self):
        """未配置 runtime.context 时必须关闭（既有行为逐字节不变）。"""
        manager = ContextManager.from_config(SimpleNamespace(llm={"context_window": 100}, runtime={}))
        self.assertFalse(manager.enabled)

    def test_from_config_reads_runtime_context(self):
        config = SimpleNamespace(
            llm={"context_window": 262144, "max_tokens": 64000},
            runtime={"context": {"enabled": True, "compact_ratio": 0.8,
                                 "tool_result_chars": 1234, "keep_recent_rounds": 2,
                                 "summary_max_chars": 500, "calibrate": True}},
        )
        manager = ContextManager.from_config(config)
        self.assertTrue(manager.enabled)
        self.assertEqual(manager.budget.reserve_output, 64000)
        self.assertEqual(manager.budget.compact_limit, int((262144 - 64000) * 0.8))
        self.assertEqual(manager.tool_result_chars, 1234)
        self.assertEqual(manager.keep_recent_rounds, 2)
        self.assertEqual(manager.summary_max_chars, 500)
        self.assertTrue(manager.calibrate)

    def test_from_config_reserve_output_can_be_disabled(self):
        config = SimpleNamespace(
            llm={"context_window": 1000, "max_tokens": 400},
            runtime={"context": {"enabled": True, "reserve_output": False}},
        )
        self.assertEqual(ContextManager.from_config(config).budget.usable, 1000)

    def test_system_yaml_ships_the_section(self):
        config = load_config(PROJECT_ROOT / "config")
        context = dict(config.runtime.get("context") or {})
        self.assertIn("enabled", context)
        self.assertIsInstance(context["enabled"], bool)          # 别写死 true/false
        self.assertIn("compact_ratio", context)
        # 实测：当前流水线单节点 Prompt 4.6k~39k 字符，远低于 90% 阈值，故开启也不会动手
        manager = ContextManager.from_config(config)
        self.assertGreater(manager.budget.compact_limit, 100000)


# =========================================================================== 端到端
class _StubHandler(BaseHTTPRequestHandler):
    requests: list = []

    def log_message(self, fmt, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        type(self).requests.append(json.loads(self.rfile.read(length).decode("utf-8") or "{}"))
        body = json.dumps({"choices": [{"message": {"content": '{"ok": 1}'},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 11, "completion_tokens": 2,
                                     "total_tokens": 13}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _RecordingEvents:
    def __init__(self):
        self.records = []

    def emit(self, type_, **kwargs):
        self.records.append({"type": type_, **kwargs})


class _CtxAgent(BaseAgent):
    role_id = "developer"
    task_kinds = {"default": "implementation"}

    def perceive(self, task):
        return Perception()

    def retrieve_experience(self, task, perception):
        return RetrievalResult()

    def build_prompt(self, task, perception, experience):
        return SimpleNamespace(path=None, system="你是研发工程师", user="请实现需求" + "。" * 100,
                               experiences=[], skills=[], tier_counts={})

    def build_context(self, task, perception):
        return {"spec": {}}

    def apply(self, task, generation, perception, context, result):
        result.status = "pass"


class EndToEndTests(unittest.TestCase):
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
        self.config.llm["provider"] = "openai_compatible"
        self.config.llm["model"] = "stub-model"
        self.config.llm["timeout"] = 10
        self.config.llm["retry"] = {"max_attempts": 1, "backoff_s": 0}
        self.config.llm_overrides = {"api_key": "stub-key", "base_url": self.base_url}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _bundle(self):
        return SimpleNamespace(path=None, system="sys", user="user", experiences=[],
                               skills=[], tier_counts={})

    # ------------------------------------------------------------ 请求体真的被收敛
    def test_llm_client_fits_payload_before_sending(self):
        """唯一生效点在 _build_payload：发出去的请求体必须是治理后的。"""
        self.config.llm["context_window"] = 4000
        self.config.llm["max_tokens"] = 0
        # tool_result_chars=0：跳过阶段一，让 trim→compact 这条路径确定性生效
        self.config.runtime["context"] = {
            "enabled": True, "tool_result_chars": 0, "keep_recent_rounds": 1,
            "reserve_output": False, "summary_max_chars": 300,
        }
        extra = [
            _assistant_call("c0", "old"), _tool("c0", "old", "A" * 20000),
            _assistant_call("c1", "new"), _tool("c1", "new", "B" * 200),
        ]
        LLMClient(self.config).generate("implementation", self._bundle(), {}, extra_messages=extra)
        sent = _StubHandler.requests[0]["messages"]
        self.assertEqual(sent[0]["content"], "sys")             # 骨架不动
        self.assertEqual(sent[1]["content"], "user")
        self.assertLess(estimate_messages(sent), 4000 * 0.95)
        self.assertEqual(sent[-1]["role"], "user")
        self.assertIn("old", sent[-1]["content"])               # 摘要挂回

    def test_disabled_client_sends_payload_unchanged(self):
        """关闭时发出的请求体与传入的 extra_messages 逐条相等。"""
        self.config.llm["context_window"] = 10                   # 极小窗口也不许动手
        self.config.runtime["context"] = {"enabled": False}
        extra = [_tool("c0", "t", "A" * 20000)]
        LLMClient(self.config).generate("implementation", self._bundle(), {}, extra_messages=extra)
        sent = _StubHandler.requests[0]["messages"]
        self.assertEqual(sent[2], extra[0])

    def test_context_window_from_config_drives_threshold(self):
        self.config.llm["context_window"] = 100000
        self.config.llm["max_tokens"] = 0
        self.config.runtime["context"] = {"enabled": True, "reserve_output": False}
        extra = [_tool("c0", "t", "A" * 200)]
        client = LLMClient(self.config)
        client.generate("implementation", self._bundle(), {}, extra_messages=extra)
        self.assertFalse(client.last_fit.changed)                # 远未到 90%

    # ------------------------------------------------------------ 离线不受影响
    def test_offline_provider_untouched(self):
        self.config.llm["provider"] = "offline"
        self.config.llm["context_window"] = 10
        self.config.runtime["context"] = {"enabled": True}
        gen = LLMClient(self.config).generate("requirement_spec", self._bundle(), {})
        self.assertEqual(gen.provider, "offline")
        self.assertEqual(_StubHandler.requests, [])              # 不触网

    # ------------------------------------------------------------ Agent 留痕
    def _run_agent(self):
        events = _RecordingEvents()
        llm = LLMClient(self.config)
        ctx = SimpleNamespace(config=self.config, events=events, run_dir=self.root,
                              llm=llm, tools=None)
        result = _CtxAgent(ctx).act(
            NodeTask(node_id="implement", title="编码实现", role="developer",
                     kind="task", iteration=1)
        )
        return events, result, llm

    def test_agent_emits_context_compacted_event_only_when_changed(self):
        self.config.llm["context_window"] = 4000
        self.config.llm["max_tokens"] = 0
        self.config.runtime["context"] = {
            "enabled": True, "tool_result_chars": 0, "keep_recent_rounds": 1,
            "reserve_output": False,
        }
        events, result, _ = self._run_agent()
        self.assertEqual(result.status, "pass")
        # 单轮无历史、体积远低于阈值 => 治理不该发生，也不该刷事件
        self.assertEqual([r for r in events.records if r["type"] == "context_compacted"], [])
        self.assertNotIn("context", result.metrics)

    def test_agent_metrics_carry_context_when_governed(self):
        """治理真正发生时要能在 run summary 里看到（否则等于黑箱）。"""
        self.config.llm["context_window"] = 4000
        self.config.llm["max_tokens"] = 0
        self.config.runtime["context"] = {
            "enabled": True, "tool_result_chars": 0, "keep_recent_rounds": 1,
            "reserve_output": False,
        }
        llm = LLMClient(self.config)
        events = _RecordingEvents()
        ctx = SimpleNamespace(config=self.config, events=events, run_dir=self.root,
                              llm=llm, tools=None)
        agent = _CtxAgent(ctx)
        extra = [_assistant_call("c0", "old"), _tool("c0", "old", "A" * 20000),
                 _assistant_call("c1", "new"), _tool("c1", "new", "B" * 100)]
        original = llm.generate

        def generate_with_history(task_kind, bundle, context, **kwargs):
            kwargs["extra_messages"] = extra
            return original(task_kind, bundle, context, **kwargs)

        llm.generate = generate_with_history
        result = agent.act(NodeTask(node_id="implement", title="编码实现", role="developer",
                                    kind="task", iteration=1))
        self.assertIn("context", result.metrics)
        self.assertIn("compact", result.metrics["context"]["stages"])
        compacted = [r for r in events.records if r["type"] == "context_compacted"]
        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0]["payload"]["dropped"], 2)
        self.assertFalse(compacted[0]["payload"]["overflow"])

    def test_agent_trace_records_context_stats_for_every_call(self):
        """trace 里恒带预算状态（治理与否都记），便于事后核对估算与实测差。"""
        self.config.runtime["context"] = {"enabled": True}
        _, _, _ = self._run_agent()
        trace = json.loads((self.root / "llm_traces.jsonl").read_text(encoding="utf-8").strip())
        self.assertIn("context", trace)
        self.assertTrue(trace["context"]["enabled"])
        self.assertIn("compact_limit", trace["context"])

    def test_agent_without_context_capability_is_unaffected(self):
        """桩 LLM 没有 context 属性时不得炸（旧客户端/测试夹具兼容）。"""
        events = _RecordingEvents()
        ctx = SimpleNamespace(config=self.config, events=events, run_dir=self.root,
                              llm=SimpleNamespace(generate=lambda *a, **k: None), tools=None)
        agent = _CtxAgent(ctx)
        self.assertEqual(agent._context_stats(), {})


if __name__ == "__main__":
    unittest.main()
