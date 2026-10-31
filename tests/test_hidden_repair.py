"""隐藏用例生成的有界修复回归测试（task09-miniledger-03 实测问题）。

背景：隐藏套件只生成一次，结构校验任何一条错误（漏功能点用例、方法名非法、
断言为空）都会让 intake 直接 fail，整轮运行作废。这些错误属于「输出未对齐
协议」，把校验错误回灌给模型重生成即可修复，不应等同于需求不可实现。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.product import ProductAgent
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.llm import Generation
from mas.sandbox import Sandbox
from mas.schemas import AgentResult, NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_SUITE_ONE = '''"""隐藏用例（第一版：漏了 FP-02）。"""
import unittest


class TestHidden(unittest.TestCase):
    def test_functional_add_result(self):
        self.assertEqual(1 + 1, 2)
'''

# pytest 风格：模块级 test_ 函数，无 TestCase 类（task09-miniledger-04 attempt1 实测，
# 旧校验放行到 verifier.freeze 才报「没有可枚举的 unittest 用例」，浪费一次 10 分钟级生成）
_SUITE_PYTEST_STYLE = '''"""隐藏用例（错误示范：pytest 风格模块级函数）。"""
def test_functional_add_result():
    assert 1 + 1 == 2
'''

# 方法名与 cases 元数据不一致：方法存在但叫别的名字
_SUITE_METHOD_MISMATCH = '''"""隐藏用例（方法名与 cases 不对应）。"""
import unittest


class TestHidden(unittest.TestCase):
    def test_functional_add_sum(self):
        self.assertEqual(1 + 1, 2)
'''

_SUITE_SYNTAX_ERROR = 'def broken(:\n'


_SUITE_TWO = '''"""隐藏用例（修复版：覆盖 FP-01/FP-02）。"""
import unittest


class TestHidden(unittest.TestCase):
    def test_functional_add_result(self):
        self.assertEqual(1 + 1, 2)

    def test_functional_sub_result(self):
        self.assertEqual(2 - 1, 1)
'''

_CASE_ADD = {
    "id": "TH-FP-01-functional-add-result", "fp_id": "FP-01",
    "op": "add", "name": "result", "category": "functional",
    "call": "add(1, 2)", "expect": "3",
}
_CASE_SUB = {
    "id": "TH-FP-02-functional-sub-result", "fp_id": "FP-02",
    "op": "sub", "name": "result", "category": "functional",
    "call": "sub(2, 1)", "expect": "1",
}


def _spec() -> dict:
    return {
        "title": "加减服务", "package": "pkg", "delivery_shape": "backend_only",
        "function_points": [
            {"id": "FP-01", "title": "加法", "category": "功能",
             "samples": [{"input": "add(1, 2)", "expect": "3"}]},
            {"id": "FP-02", "title": "减法", "category": "功能",
             "samples": [{"input": "sub(2, 1)", "expect": "1"}]},
        ],
    }


class _StubLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def generate(self, kind, bundle, context):
        self.calls.append(bundle.user)
        return Generation(data=self.payloads.pop(0), provider="stub")


class HiddenRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.run_dir = root / "run"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "code").mkdir()
        self.config = load_config(PROJECT_ROOT / "config", workspace=root / "workspace")
        self.config.runtime["verbose"] = False
        self.events = EventLog(self.run_dir / "events.jsonl", "test-hidden-repair")
        self.ctx = SimpleNamespace(
            config=self.config, events=self.events, run_dir=self.run_dir,
            blackboard=Blackboard(self.run_dir / "artifacts", self.events, "test-hidden-repair"),
            bus=MessageBus(self.run_dir / "messages.jsonl", self.events, "test-hidden-repair"),
            sandbox=Sandbox(self.run_dir / "code", self.config, events=self.events),
            prompts=None, llm=None, tools=None, experience=None,
            log=lambda *a, **k: None, run_state={}, consult=None,
            run_id="test-hidden-repair",
        )
        self.spec = _spec()
        self.frozen = {"spec": self.spec, "clauses": [], "contract_hash": "a" * 64}
        self.task = NodeTask(node_id="intake", title="需求评估", role="product",
                             kind="task", iteration=1)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, payloads, max_attempts=2):
        self.config.runtime["hidden_suite_max_attempts"] = max_attempts
        self.ctx.llm = _StubLLM(payloads)
        result = AgentResult()
        agent = ProductAgent(self.ctx)
        artifact = agent._generate_hidden_suite(
            self.task, self.frozen, {"package": "pkg"}, result)
        return agent, result, artifact

    def test_validation_error_triggers_repair_with_feedback(self):
        bad = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_ONE}],
               "cases": [_CASE_ADD]}
        good = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_TWO}],
                "cases": [_CASE_ADD, _CASE_SUB]}
        _agent, result, artifact = self._run([bad, good])

        self.assertIsNotNone(artifact, result.error)
        self.assertNotEqual(result.status, "fail", result.error)
        self.assertEqual(len(self.ctx.llm.calls), 2)
        self.assertIn("hidden_case_missing:FP-02", self.ctx.llm.calls[1])
        self.assertIn("修正后的完整 JSON", self.ctx.llm.calls[1])
        # 逐次留痕：两次尝试的耗时/结果都要保留（此前只保留最后一次，排障只能靠时间戳反推）
        detail = result.metrics.get("hidden_generation_attempts_detail") or []
        self.assertEqual(len(detail), 2, result.metrics)
        self.assertEqual([item["attempt"] for item in detail], [1, 2])
        traces = (self.run_dir / "llm_traces.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(traces), 2)
        kinds = [json.loads(line)["task_kind"] for line in traces]
        self.assertEqual(kinds, ["hidden_test_suite", "hidden_test_suite"])
        self.assertTrue(
            (self.run_dir / "prompts" / "intake_hidden_iter01_repair01.md").exists())
        repair_events = [e for e in self.events.events
                         if e.type == "hidden_suite_repair_requested"]
        self.assertEqual(len(repair_events), 1)
        self.assertEqual(result.metrics["hidden_generation_attempts"], 2)
        self.assertEqual(artifact["case_count"], 2)

    def test_repair_budget_exhausted_fails_with_last_error(self):
        bad = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_ONE}],
               "cases": [_CASE_ADD]}
        _agent, result, artifact = self._run([bad, bad])

        self.assertIsNone(artifact)
        self.assertEqual(result.status, "fail")
        self.assertIn("hidden_case_missing:FP-02", result.error)
        self.assertEqual(len(self.ctx.llm.calls), 2)
        failed = [e for e in self.events.events
                  if e.type == "hidden_suite_generation_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload.get("max_attempts"), 2)

    def test_no_repair_when_disabled(self):
        bad = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_ONE}],
               "cases": [_CASE_ADD]}
        _agent, result, artifact = self._run([bad], max_attempts=1)

        self.assertIsNone(artifact)
        self.assertEqual(result.status, "fail")
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertFalse((self.run_dir / "prompts" / "intake_hidden_iter01_repair01.md").exists())


class HiddenStructureValidationTests(unittest.TestCase):
    """结构校验前移（task09-miniledger-04 attempt1 实测问题）。

    pytest 风格文件 / 方法名与 cases 不一致 / 语法错误，此前都能通过
    validate_generated_suite，直到写盘 + verifier.freeze 才失败——一次
    10 分钟级的生成作废且错误信息不带修复指引。
    """

    def setUp(self):
        self.spec = _spec()

    def test_pytest_style_file_rejected_early(self):
        from mas.hidden import validate_generated_suite
        data = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_PYTEST_STYLE}],
                "cases": [_CASE_ADD, dict(_CASE_SUB)]}
        out = validate_generated_suite(data, self.spec)
        # 无 TestCase 类是根因，报主错误即可（不再叠加每条 case 的 method_missing 噪音）
        self.assertIn("hidden_suite_no_test_methods", out["errors"])

    def test_method_mismatch_rejected_early(self):
        from mas.hidden import validate_generated_suite
        data = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_METHOD_MISMATCH}],
                "cases": [_CASE_ADD, dict(_CASE_SUB)]}
        out = validate_generated_suite(data, self.spec)
        self.assertIn("hidden_case_method_missing:test_functional_add_result", out["errors"])
        self.assertIn("hidden_method_without_case:test_functional_add_sum", out["errors"])

    def test_syntax_error_reported_with_path(self):
        from mas.hidden import validate_generated_suite
        data = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_SYNTAX_ERROR}],
                "cases": [_CASE_ADD]}
        out = validate_generated_suite(data, self.spec)
        self.assertTrue(any(e.startswith("hidden_file_syntax_error:tests/test_hidden.py")
                            for e in out["errors"]))

    def test_valid_suite_still_passes(self):
        from mas.hidden import validate_generated_suite
        data = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_TWO}],
                "cases": [_CASE_ADD, _CASE_SUB]}
        out = validate_generated_suite(data, self.spec)
        self.assertEqual(out["errors"], [])

    def test_suffix_only_name_mismatch_is_reconciled(self):
        """task12 实测：内容完整、仅方法名差一个后缀时不应白扔整份套件。"""
        from mas.hidden import validate_generated_suite
        case = {"id": "TH-FP-01-functional-demo-run-success", "fp_id": "FP-01",
                "op": "demo_run", "name": "success", "category": "functional",
                "call": "run()", "expect": "ok"}
        suite = '''"""隐藏用例（方法名少一个 _success）。"""
import unittest


class TestHidden(unittest.TestCase):
    def test_functional_demo_run(self):
        self.assertTrue(True)
'''
        data = {"files": [{"path": "tests/test_hidden.py", "content": suite}],
                "cases": [case]}
        # 单 FP 规格，隔离「仅方法名差后缀」这一个变量
        spec = {"function_points": [_spec()["function_points"][0]]}
        out = validate_generated_suite(data, spec)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["reconciled"],
                         [{"from": "test_functional_demo_run",
                           "to": "test_functional_demo_run_success"}])
        self.assertIn("def test_functional_demo_run_success(",
                      out["files"][0]["content"])
        # 重命名只动 def 行，测试体不变
        self.assertIn("self.assertTrue(True)", out["files"][0]["content"])

    def test_unrelated_method_name_is_not_reconciled(self):
        """非前后缀关系的错名不能靠重命名蒙混过关（保持原有拒绝口径）。"""
        from mas.hidden import validate_generated_suite
        case = {"id": "TH-FP-01-functional-demo-run-success", "fp_id": "FP-01",
                "op": "demo_run", "name": "success", "category": "functional",
                "call": "run()", "expect": "ok"}
        suite = '''"""隐藏用例（方法名完全不相干）。"""
import unittest


class TestHidden(unittest.TestCase):
    def test_functional_other_thing(self):
        self.assertTrue(True)
'''
        data = {"files": [{"path": "tests/test_hidden.py", "content": suite}],
                "cases": [case]}
        out = validate_generated_suite(data, self.spec)
        self.assertIn("hidden_case_method_missing:test_functional_demo_run_success",
                      out["errors"])
        self.assertEqual(out["reconciled"], [])


class HiddenRepairGuidanceTests(HiddenRepairTests):
    """修复块必须携带针对性指引 + 失败现场全量落盘（task09-miniledger-04 实测）。"""

    def test_repair_prompt_contains_targeted_hints(self):
        bad = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_PYTEST_STYLE}],
               "cases": [_CASE_ADD]}
        good = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_TWO}],
                "cases": [_CASE_ADD, _CASE_SUB]}
        _agent, result, artifact = self._run([bad, good])

        self.assertIsNotNone(artifact, result.error)
        second_prompt = self.ctx.llm.calls[1]
        self.assertIn("hidden_suite_no_test_methods", second_prompt)
        self.assertIn("unittest.TestCase", second_prompt)
        self.assertIn("针对性修复指引", second_prompt)

    def test_failure_dumps_rejected_output(self):
        bad = {"files": [{"path": "tests/test_hidden.py", "content": _SUITE_PYTEST_STYLE}],
               "cases": [_CASE_ADD]}
        _agent, result, artifact = self._run([bad, bad, bad], max_attempts=3)

        self.assertIsNone(artifact)
        self.assertEqual(result.status, "fail")
        rejected = self.run_dir / "rejected" / "hidden_suite_iter01_attempt03.json"
        self.assertTrue(rejected.exists())
        import json as _json
        dumped = _json.loads(rejected.read_text(encoding="utf-8"))
        self.assertEqual(dumped["data"], bad)
        failed = [e for e in self.events.events if e.type == "hidden_suite_generation_failed"]
        self.assertTrue(failed[0].payload.get("rejected"))
        self.assertEqual(failed[0].payload.get("max_attempts"), 3)


if __name__ == "__main__":
    unittest.main()
