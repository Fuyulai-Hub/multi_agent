"""研发返工/修复轮的两条硬约束（reconx-core-02 复盘）。

1. ``source=hidden`` 的缺陷不得拿可见冻结清单做定向回归：隐藏用例在 run 工作区
   之外的独立清单里，``--only`` 必然 ``no_test_id_matched``（runner 退出码 2），
   结果把「一行都没跑」记成「回归失败」，节点整轮判 fail（实测 3 轮 optimize
   全部 0/N，验收报告据此误判「修复未生效」）。
2. 返工/修复轮必须看到当前源码：原先 Prompt 只有需求 + 缺陷症状 + 文件清单，
   模型只能整份重写，把已通过的用例改坏（实测一轮重写 6 个文件 618 行，
   新增 3 个缺陷）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.developer import DeveloperAgent
from mas.agents.product import ProductAgent
from mas.config import load_config
from mas.events import EventLog
from mas.sandbox import Sandbox
from mas.schemas import AgentContext, AgentResult, NodeTask
from mas.agents.base import Perception

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _Blackboard:
    def __init__(self) -> None:
        self.published = []

    def publish(self, name, data, **kwargs):  # noqa: ANN001 - 测试桩
        self.published.append((name, data, kwargs))
        return None


class _Execution:
    def __init__(self, *, exit_code: int = 0, total: int = 1, failed: int = 0) -> None:
        self.exec = SimpleNamespace(cmd=["python", "runner.py"], exit_code=exit_code, ok=exit_code == 0,
                                    stdout="", stderr="", duration_ms=1)
        self.result = {"total": total, "failed": failed}


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.llm["provider"] = "offline"
        self.config.runtime["verbose"] = False
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "test-fix")
        self.sandbox = Sandbox(self.run_dir / "code", self.config, events=self.events)
        self.blackboard = _Blackboard()
        self.ctx = AgentContext(
            config=self.config, run_id="test-fix", run_dir=self.run_dir, events=self.events,
            blackboard=self.blackboard, bus=None, llm=None, sandbox=self.sandbox,
            prompts=None, experience=None,
        )
        self.agent = DeveloperAgent(self.ctx)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 脚手架
    def _task(self, node_id: str = "optimize") -> NodeTask:
        return NodeTask(node_id=node_id, title="缺陷修复", role="developer", kind="task", iteration=3)

    def _write(self, relative: str, content: str) -> None:
        self.sandbox.write_file(relative, content)

    def _generation(self, files, *, package="demo"):
        return SimpleNamespace(data={"mode": "fix", "package": package, "files": files,
                                     "fp_mapping": {}, "functions": [], "notes": ""},
                               text="", error="", finish_reason="stop")

    def _perception(self, bugs, *, files=("demo/__init__.py", "demo/core.py")):
        return Perception(found={
            "requirement_spec": {"id": "req", "package": "demo", "function_points": []},
            "code_manifest": {"package": "demo", "files": list(files)},
            "bug_reports": {"items": bugs, "open": len(bugs)},
        })


class HiddenRegressionDeferredTests(_Harness):
    def test_hidden_bug_never_runs_visible_regression(self) -> None:
        calls = []

        def _boom(*args, **kwargs):  # noqa: ANN001 - 断言不会被调用
            calls.append((args, kwargs))
            raise AssertionError("隐藏缺陷不应触发可见清单回归")

        import mas.agents.developer as dev_module
        original = dev_module.execute_frozen_suite
        dev_module.execute_frozen_suite = _boom
        try:
            bug = {"id": "BUG-h1", "fp_id": "FP-03", "category": "functional", "status": "open",
                   "source": "hidden", "test": "tests.test_hidden_fp03.HiddenFp03Tests.test_x",
                   "symptom": "AssertionError: 60 != 3600"}
            perception = self._perception([bug])
            context = self.agent.build_context(self._task(), perception)
            result = AgentResult()
            generation = self._generation([{"path": "demo/__init__.py", "content": "VALUE = 1\n"}])
            self.agent._apply_fix(self._task(), generation, perception, context, result)
        finally:
            dev_module.execute_frozen_suite = original

        self.assertEqual(calls, [])
        self.assertEqual(result.status, "pass", result.error)
        summary = result.artifacts["fix_summary"]
        item = summary["items"][0]
        self.assertIsNone(item["regression_ok"])
        self.assertEqual(item["regression"]["deferred"], "hidden_gate")
        report = result.artifacts["fix_test_report"]
        self.assertEqual(report["deferred"], 1)
        self.assertTrue(report["all_pass"])
        self.assertEqual(report["results"][0]["verified_by"], "hidden_gate")
        self.assertIn("待门禁复跑", result.summary)
        self.assertEqual([e.type for e in self.events.events if e.type == "bug_fix_deferred"],
                         ["bug_fix_deferred"])

    def test_visible_bug_still_runs_regression(self) -> None:
        import mas.agents.developer as dev_module
        original_exec = dev_module.execute_frozen_suite
        original_load = dev_module.TestVerifier.load_manifest
        dev_module.execute_frozen_suite = lambda *a, **k: _Execution()
        dev_module.TestVerifier.load_manifest = lambda self: {"digest": "d"}
        try:
            bug = {"id": "BUG-v1", "fp_id": "FP-01", "category": "exception", "status": "open",
                   "test": "tests.test_all.TestSuite.test_a", "symptom": "boom"}
            perception = self._perception([bug])
            context = self.agent.build_context(self._task(), perception)
            result = AgentResult()
            generation = self._generation([{"path": "demo/__init__.py", "content": "VALUE = 1\n"}])
            self.agent._apply_fix(self._task(), generation, perception, context, result)
        finally:
            dev_module.execute_frozen_suite = original_exec
            dev_module.TestVerifier.load_manifest = original_load

        item = result.artifacts["fix_summary"]["items"][0]
        self.assertTrue(item["regression_ok"], item)
        self.assertEqual(item["regression"]["exit_code"], 0)
        self.assertEqual(result.status, "pass")
        self.assertIn("1/1", result.summary)


class BugViewTests(_Harness):
    def test_hidden_bug_hides_test_method_name(self) -> None:
        view = DeveloperAgent._bug_view({
            "id": "BUG-h", "fp_id": "FP-03", "category": "boundary", "status": "open",
            "source": "hidden", "symptom": "AssertionError: False is not true",
            "test": "tests.test_hidden_fp03.HiddenFp03Tests.test_functional_ReconError_hierarchy",
        })
        self.assertNotIn("test", view)
        self.assertTrue(view["case_ref"].startswith("hidden-"))
        self.assertNotIn("ReconError", str(view))

    def test_visible_bug_keeps_test_id(self) -> None:
        view = DeveloperAgent._bug_view({
            "id": "BUG-v", "fp_id": "FP-01", "status": "open",
            "test": "tests.test_all.TestSuite.test_a",
        })
        self.assertEqual(view["test"], "tests.test_all.TestSuite.test_a")


class FixSourcePromptTests(_Harness):
    def _perception_with_source(self):
        self._write("demo/__init__.py", "VALUE = 1\n")
        self._write("demo/core.py", "def add(a, b):\n    return a + b\n")
        self._write("tests/test_demo.py", "def test_x():\n    assert True\n")
        return self._perception(
            [{"id": "BUG-1", "fp_id": "FP-01", "status": "open", "test": "tests.test_all.T.test_a"}],
            files=("demo/__init__.py", "demo/core.py", "tests/test_demo.py"))

    def test_fix_round_injects_current_source_without_tests(self) -> None:
        perception = self._perception_with_source()
        text = self.agent.prompt_extra_directive(self._task("optimize"), perception)
        self.assertIn("当前交付源码（修改基线）", text)
        self.assertIn("def add(a, b):", text)
        self.assertNotIn("tests/test_demo.py", text)
        self.assertIn("只输出需要改动的文件", text)

    def test_first_pass_has_no_source_block(self) -> None:
        perception = Perception(found={
            "requirement_spec": {"id": "req"},
            "code_manifest": {"files": []},
        })
        self.assertEqual(self.agent.prompt_extra_directive(self._task("implement"), perception), "")

    def test_budget_zero_disables_injection(self) -> None:
        self.config.runtime["fix_source_total_chars"] = 0
        perception = self._perception_with_source()
        self.assertEqual(self.agent.prompt_extra_directive(self._task("optimize"), perception), "")

    def test_other_roles_keep_prompt_unchanged(self) -> None:
        product = ProductAgent(self.ctx)
        perception = self._perception_with_source()
        self.assertEqual(product.prompt_extra_directive(self._task("intake"), perception), "")

    def test_per_file_truncation_is_marked(self) -> None:
        self.config.runtime["fix_source_per_file_chars"] = 20
        perception = self._perception_with_source()
        text = self.agent.prompt_extra_directive(self._task("optimize"), perception)
        self.assertIn("源码在此截断", text)
        self.assertIn("已截断", text)


if __name__ == "__main__":
    unittest.main()
