"""六项缺陷修复的回归测试。

覆盖：
1. 并发：parallel_map 保序/异常传播；analyze 并行结果与逐文件串行一致；
2. 分布式共享：文件锁互斥；黑板跨实例版本不冲突；新实例 get() 回落磁盘；
3. 沙箱隔离：命令白名单拒绝 shell；敏感环境变量剥离；network_import 检测；
4. 语义检索：bigram 软匹配 + 同义词扩展召回同义改写；
5. function calling：tool_calls 解析归一化；Agent 工具调用闭环；
6. 权限与重试：显式 false 才拦截；sandbox_write/run 守卫；节点级 retry；回退权限。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.blackboard import Blackboard
from mas.concurrency import parallel_map
from mas.config import NodeConfig, RoleConfig, load_config
from mas.errors import SandboxError
from mas.events import EventLog
from mas.experience import Experience, ExperienceLibrary
from mas.filelock import FileLockTimeout, file_lock
from mas.leader import Leader, _permission_allows
from mas.llm import Generation, LLMClient
from mas.sandbox import Sandbox
from mas.schemas import AgentResult, NodeTask
from mas.toolrunner import ToolRunner

from tests.test_experience import RAW as LEARNING_RAW

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _RecordingEvents:
    """极简事件记录器（只采集调用，不落盘）。"""

    def __init__(self):
        self.records = []

    def emit(self, type_, **kwargs):
        self.records.append({"type": type_, **kwargs})


# =========================================================================== 1 并发
class ConcurrencyTests(unittest.TestCase):
    def test_parallel_map_preserves_order(self):
        result = parallel_map(lambda item: item * 2, list(range(64)), max_workers=4)
        self.assertEqual(result, [item * 2 for item in range(64)])

    def test_parallel_map_propagates_task_exception_in_place(self):
        def boom(item):
            if item == 3:
                raise ValueError("boom")
            return item

        result = parallel_map(boom, list(range(6)), max_workers=3)
        self.assertEqual(result[:3], [0, 1, 2])
        self.assertIsInstance(result[3], ValueError)
        self.assertEqual(result[4:], [4, 5])

    def test_analyze_parallel_matches_per_file_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
            sandbox = Sandbox(Path(tmp) / "code", config)
            files = []
            for index in range(6):
                relative = f"pkg/mod{index}.py"
                sandbox.write_file(relative, f'"""模块{index}。"""\n\n\ndef f{index}(a):\n    return a\n')
                files.append(relative)

            policies = {"require_type_hints": False, "require_function_docstring": False}
            parallel_result = sandbox.analyze(files, policies)
            # 串行基准：逐文件单独分析后合并
            merged = {"findings": [], "loc": 0, "classes": 0, "functions": [], "syntax_errors": []}
            for relative in files:
                item = sandbox._analyze_file(
                    relative, severity_map={}, max_lines=60, max_line_length=120,
                    require_module_doc=True, require_function_doc=False, require_hints=False,
                )
                for key in merged:
                    merged[key].extend(item[key]) if isinstance(item[key], list) else None
                merged["loc"] += item["loc"]
                merged["classes"] += item["classes"]

            self.assertEqual(parallel_result.loc, merged["loc"])
            self.assertEqual(len(parallel_result.functions), len(merged["functions"]))
            self.assertEqual(len(parallel_result.findings), len(merged["findings"]))
            self.assertEqual(
                sorted(f.rule for f in parallel_result.findings),
                sorted(f["rule"] for f in merged["findings"]),
            )


# =========================================================================== 2 分布式共享
class FileLockAndBlackboardTests(unittest.TestCase):
    def test_file_lock_excludes_concurrent_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            target.write_text("{}", encoding="utf-8")
            held = threading.Event()

            def holder():
                with file_lock(target, timeout_s=2):
                    held.set()
                    time.sleep(0.4)

            thread = threading.Thread(target=holder, daemon=True)
            thread.start()
            self.assertTrue(held.wait(timeout=2))
            with self.assertRaises(FileLockTimeout):
                with file_lock(target, timeout_s=0.1):
                    pass
            thread.join(timeout=3)
            # 锁释放后可再次获取
            with file_lock(target, timeout_s=1):
                pass

    def test_blackboard_versions_do_not_collide_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            first = Blackboard(root, run_id="instance-a")
            second = Blackboard(root, run_id="instance-b")   # 模拟另一个进程/实例

            ref_a = first.publish("gate", {"from": "a"}, producer="a", node="n", iteration=1)
            ref_b = second.publish("gate", {"from": "b"}, producer="b", node="n", iteration=1)

            self.assertEqual(ref_a.version, 1)
            self.assertEqual(ref_b.version, 2)               # 原实现两个实例都会写 v1 互相覆盖
            self.assertTrue((root / "gate" / "v1.json").exists())
            self.assertTrue((root / "gate" / "v2.json").exists())
            self.assertEqual(json.loads((root / "gate" / "latest.json").read_text(encoding="utf-8")), {"from": "b"})

    def test_blackboard_get_falls_back_to_disk_for_foreign_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            writer = Blackboard(root, run_id="writer")
            writer.publish("spec", {"title": "T"}, producer="writer", node="intake", iteration=1)

            reader = Blackboard(root, run_id="reader")        # 全新实例，内存为空
            self.assertTrue(reader.has("spec"))
            self.assertEqual(reader.get("spec"), {"title": "T"})
            self.assertIsNone(reader.get("missing"))


# =========================================================================== 3 沙箱隔离
class SandboxIsolationTests(unittest.TestCase):
    def _sandbox(self, tmp: str) -> Sandbox:
        config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
        return Sandbox(Path(tmp) / "code", config)

    def test_shell_interpreter_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            for shell in (["bash", "-c", "echo hi"], ["cmd", "/c", "echo hi"],
                          ["powershell", "-Command", "echo hi"]):
                with self.assertRaises(SandboxError):
                    sandbox.run(shell)

    def test_python_interpreter_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            result = sandbox.run([sandbox.python, "-c", "print('ok')"])
            self.assertTrue(result.ok, result.stderr)
            self.assertIn("ok", result.stdout)

    def test_sensitive_env_stripped_from_child(self):
        canary = "MAS_TEST_SECRET_CANARY"     # 含 SECRET，应被剥离
        os.environ[canary] = "sk-should-not-leak"
        os.environ["MAS_LLM_API_KEY"] = "sk-should-not-leak"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sandbox = self._sandbox(tmp)
                script = (
                    "import os\n"
                    f"print('canary=' + str({canary!r} in os.environ))\n"
                    "print('api_key=' + str('MAS_LLM_API_KEY' in os.environ))\n"
                    "print('path_kept=' + str('PATH' in os.environ))\n"
                )
                sandbox.write_file("probe.py", script)
                result = sandbox.run([sandbox.python, "probe.py"])
                self.assertIn("canary=False", result.stdout)
                self.assertIn("api_key=False", result.stdout)
                self.assertIn("path_kept=True", result.stdout)   # 非敏感环境变量保留
        finally:
            os.environ.pop(canary, None)
            os.environ.pop("MAS_LLM_API_KEY", None)

    def test_network_import_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            sandbox.write_file("net.py", '"""net。"""\n\n\nimport socket\n\n\ndef go():\n    return socket\n')
            analysis = sandbox.analyze(["net.py"], {})
            rules = {finding.rule for finding in analysis.findings}
            self.assertIn("network_import", rules)

    def test_isolation_label_records_active_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            label = sandbox._isolation_label()
            self.assertIn("path+cmd", label)
            self.assertIn("env-strip", label)


# =========================================================================== 4 语义检索
class SemanticRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raw = dict(LEARNING_RAW)
        raw["synonyms"] = {"运费": ["物流费", "配送费"], "计价": ["计算", "核算"]}
        from mas.config import LearningConfig

        self.library = ExperienceLibrary(Path(self.tmp.name), LearningConfig.from_raw(raw))

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, **kwargs):
        defaults = dict(
            tier="L1", scope="run", roles=["developer"], tags=["订单"],
            trigger="bug_found", lesson="运费计算必须向上取整，禁止截断",
            action="按重量区间查表", confidence=0.8, source_run="run-1",
        )
        defaults.update(kwargs)
        return Experience(id="", **defaults)

    def test_bigram_soft_match_beats_exact_substring(self):
        # 同义改写无公共子串：「阈值比较」 vs 「边界阈值处理」
        overlap = self.library._semantic_overlap("边界阈值处理", "阈值比较必须显式处理相等情形")
        self.assertGreater(overlap, 0.0)

    def test_synonym_expansion_recalls_paraphrase(self):
        self.library.add([self._entry()])
        result = self.library.retrieve(role="developer", tags=[], task_text="物流费核算逻辑",
                                       current_run="run-1")
        self.assertEqual(len(result.entries), 1)
        self.assertIn("运费", result.text)

    def test_empty_text_scores_zero(self):
        self.assertEqual(self.library._semantic_overlap("", "任意文本"), 0.0)


# =========================================================================== 5 function calling
class FunctionCallingTests(unittest.TestCase):
    def test_parse_tool_calls_normalizes_arguments(self):
        message = {"tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "echo", "arguments": "{\"x\": 1}"},
        }]}
        calls = LLMClient._parse_tool_calls(message)
        self.assertEqual(calls[0]["name"], "echo")
        self.assertEqual(calls[0]["arguments"], {"x": 1})

    def test_parse_tool_calls_tolerates_bad_json(self):
        message = {"tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "not-json"}}]}
        calls = LLMClient._parse_tool_calls(message)
        self.assertEqual(calls[0]["arguments"], "not-json")

    def test_agent_tool_call_loop_executes_and_feeds_back(self):
        from mas.agents.base import BaseAgent, Perception
        from mas.experience import RetrievalResult

        class _SequenceLLM:
            tool_max_rounds = 2

            def __init__(self):
                self.calls = []

            def generate(self, task_kind, bundle, context, *, tools=None, extra_messages=None):
                self.calls.append({"tools": tools, "extra": list(extra_messages or [])})
                if len(self.calls) == 1:
                    return Generation(text="", tool_calls=[
                        {"id": "call-1", "name": "echo", "arguments": {"x": 1}}])
                return Generation(text="{}", data={"ok": True})

        class _FakeRunner:
            schemas = [{"type": "function", "function": {"name": "echo"}}]

            def __init__(self):
                self.executed = []

            def execute(self, tool_calls):
                self.executed.append(tool_calls)
                return [{"role": "tool", "tool_call_id": "call-1", "content": "{\"echo\": 1}"}]

        class _ToolLoopAgent(BaseAgent):
            role_id = "developer"

            def __init__(self, ctx):
                super().__init__(ctx)
                self.applied = None

            def perceive(self, task):
                return Perception()

            def retrieve_experience(self, task, perception):
                return RetrievalResult()

            def build_prompt(self, task, perception, experience):
                return SimpleNamespace(path=None, experiences=[], tier_counts={})

            def build_context(self, task, perception):
                return {}

            def apply(self, task, generation, perception, context, result):
                result.status = "pass"
                self.applied = generation

        with tempfile.TemporaryDirectory() as tmp:
            role = RoleConfig.from_raw("developer", {"id": "developer", "name": "研发", "permissions": {}})
            llm = _SequenceLLM()
            runner = _FakeRunner()
            ctx = SimpleNamespace(
                config=SimpleNamespace(role=lambda role_id: role),
                events=_RecordingEvents(), run_dir=Path(tmp), llm=llm, tools=runner,
            )
            agent = _ToolLoopAgent(ctx)
            task = NodeTask(node_id="implement", title="编码", role="developer", kind="task", iteration=1)
            result = agent.act(task)

            self.assertEqual(result.status, "pass")
            self.assertEqual(len(runner.executed), 1)              # 工具被真实执行过一次
            self.assertEqual(runner.executed[0][0]["name"], "echo")
            self.assertEqual(len(llm.calls), 2)                    # 工具结果回传后再次生成
            self.assertTrue(llm.calls[0]["tools"])                 # 首轮下发了 tools
            # 协议顺序：assistant(带 tool_calls) → tool(执行结果)
            self.assertEqual(llm.calls[1]["extra"][0]["role"], "assistant")
            self.assertEqual(llm.calls[1]["extra"][0]["tool_calls"][0]["id"], "call-1")
            self.assertEqual(llm.calls[1]["extra"][1]["role"], "tool")
            self.assertEqual(result.metrics.get("tool_rounds"), 1)
            self.assertTrue(agent.applied.data["ok"])

    def test_tool_runner_static_schema_and_error_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
            runner = ToolRunner(config)
            runner.register_static([{"type": "function", "function": {"name": "ping"}}])
            self.assertEqual([t["function"]["name"] for t in runner.schemas], ["ping"])
            # 静态工具无后端：执行返回错误文本而不是抛异常
            messages = runner.execute([{"id": "c1", "function": {"name": "ping", "arguments": "{}"}}])
            self.assertEqual(messages[0]["role"], "tool")
            self.assertIn("错误", messages[0]["content"])
            # 未知工具同样返回错误文本
            unknown = runner.execute([{"id": "c2", "function": {"name": "nope", "arguments": "{}"}}])
            self.assertIn("未知工具", unknown[0]["content"])


# =========================================================================== 6 权限与重试
class PermissionAndRetryTests(unittest.TestCase):
    def _agent(self, permissions):
        from mas.agents.base import BaseAgent

        class _Agent(BaseAgent):
            role_id = "tester"

        role = RoleConfig.from_raw("tester", {"id": "tester", "name": "测试", "permissions": permissions})
        ctx = SimpleNamespace(config=SimpleNamespace(role=lambda role_id: role),
                              events=_RecordingEvents())
        return _Agent(ctx)

    def test_explicit_false_denies_and_undeclared_allows(self):
        agent = self._agent({"can_write_code": False, "can_execute": True})
        self.assertFalse(agent.permit("can_write_code"))
        self.assertTrue(agent.permit("can_execute"))
        self.assertTrue(agent.permit("can_rollback"))            # 未声明 -> 放行
        denied = [r for r in agent.ctx.events.records if r["type"] == "permission_denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["payload"]["action"], "can_write_code")

    def test_sandbox_write_guard_blocks_code_but_allows_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
            sandbox = Sandbox(Path(tmp) / "code", config)
            agent = self._agent({"can_write_code": False})
            agent.ctx.sandbox = sandbox
            with self.assertRaises(SandboxError):
                agent.sandbox_write("pkg/core.py", "x = 1")
            agent.sandbox_write("tests/test_core.py", "x = 1")   # tests/ 走 can_write_tests，未声明放行
            self.assertTrue(sandbox.exists("tests/test_core.py"))

    def test_sandbox_run_guard_blocks_without_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
            sandbox = Sandbox(Path(tmp) / "code", config)
            agent = self._agent({"can_execute": False})
            agent.ctx.sandbox = sandbox
            with self.assertRaises(SandboxError):
                agent.sandbox_run([sandbox.python, "-c", "print(1)"])

    def test_legacy_can_run_tests_key_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
            sandbox = Sandbox(Path(tmp) / "code", config)
            agent = self._agent({"can_run_tests": False})        # 旧键名作为 can_execute 别名
            agent.ctx.sandbox = sandbox
            with self.assertRaises(SandboxError):
                agent.sandbox_run([sandbox.python, "-c", "print(1)"])

    def test_node_retry_reruns_until_pass_or_budget(self):
        class _FlakyAgent:
            def __init__(self, statuses):
                self.statuses = list(statuses)
                self.calls = 0

            def act(self, task):
                self.calls += 1
                status = self.statuses.pop(0) if self.statuses else "fail"
                return AgentResult(status=status, error="boom" if status == "fail" else "")

        leader = Leader(SimpleNamespace())
        ctx = SimpleNamespace(events=_RecordingEvents(), run_state={})
        task = NodeTask(node_id="implement", title="编码", role="developer", kind="task", iteration=1)
        node = NodeConfig.from_raw("implement", {"title": "编码", "role": "developer", "kind": "task",
                                                 "retry": {"max_attempts": 3}})

        flaky = _FlakyAgent(["fail", "fail", "pass"])
        result, status = leader._act_with_retry(ctx, task, {"developer": flaky}, node)
        self.assertEqual(status, "pass")
        self.assertEqual(flaky.calls, 3)
        retries = [r for r in ctx.events.records if r["type"] == "node_retry"]
        self.assertEqual(len(retries), 2)

        # 预算耗尽：仍然失败，且不会无限重试
        always_fail = _FlakyAgent(["fail"] * 5)
        result, status = leader._act_with_retry(ctx, task, {"developer": always_fail}, node)
        self.assertEqual(status, "fail")
        self.assertEqual(always_fail.calls, 3)

    def test_rollback_permission_denied_terminates(self):
        leader = Leader(SimpleNamespace())
        events = _RecordingEvents()
        node = SimpleNamespace(role="developer")
        agents = {"developer": self._agent({"can_rollback": False})}
        self.assertFalse(leader._rollback_permitted("implement", node, "solution", agents, events, 1))
        self.assertTrue(any(r["type"] == "permission_denied" for r in events.records))

        agents_ok = {"developer": self._agent({"can_rollback": True})}
        self.assertTrue(leader._rollback_permitted("implement", node, "solution", agents_ok, events, 1))

    def test_permission_allows_helper(self):
        self.assertTrue(_permission_allows(self._agent({}), "can_block_acceptance"))
        self.assertFalse(_permission_allows(self._agent({"can_block_acceptance": False}),
                                            "can_block_acceptance"))

    def test_llm_retry_defaults_to_single_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
            client = LLMClient(config)
            self.assertEqual(client.retry_max_attempts, 1)      # 默认不重试，行为不变
            config.llm["retry"] = {"max_attempts": 3, "backoff_s": 0.1}
            self.assertEqual(LLMClient(config).retry_max_attempts, 3)


if __name__ == "__main__":
    unittest.main()
