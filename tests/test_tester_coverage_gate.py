"""测试套件冻结前覆盖率校验回归测试（20261024-233917-897d 实测问题）。

背景：测试套件一旦冻结，FP×类别覆盖率就被冻结清单的 fp_id 元数据永久固定
（复测忽略新生成、套件只在自身失效时解冻），而验收门禁要求三类覆盖率都 >= 1.0。
首轮生成少覆盖（12 个 FP 只覆盖 10/9/7）时被照单冻结，门禁从此永远失败、
optimize→test 循环无法自救。修复：冻结前校验覆盖，缺口回灌重生成一次，
仍缺口则测试节点判失败（不冻结任何套件），走回退重新生成。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.tester import TesterAgent
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.llm import Generation
from mas.sandbox import Sandbox
from mas.schemas import AgentResult, NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _cases(*pairs):
    """按 (fp_id, category) 序列生成最小用例元数据。"""
    return [
        {"id": f"TC-{fp}-{cat}-{i}", "fp_id": fp, "category": cat,
         "op": "add", "name": f"case{i}", "call": "add(1,2)", "expect": "3"}
        for i, (fp, cat) in enumerate(pairs)
    ]


def _spec():
    return {"function_points": [
        {"id": "FP-01", "title": "加法", "category": "功能",
         "samples": [{"input": "add(1, 2)", "expect": "3"}]},
        {"id": "FP-02", "title": "减法", "category": "功能",
         "samples": [{"input": "sub(2, 1)", "expect": "1"}]},
    ]}


_FULL_CASES = _cases(("FP-01", "functional"), ("FP-01", "exception"), ("FP-01", "boundary"),
                     ("FP-02", "functional"), ("FP-02", "exception"), ("FP-02", "boundary"))
_GAP_CASES = _cases(("FP-01", "functional"), ("FP-01", "exception"), ("FP-01", "boundary"),
                    ("FP-02", "functional"))


class _StubLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def generate(self, kind, bundle, context):
        self.calls.append((kind, bundle.user))
        return Generation(data=self.payloads.pop(0), provider="stub")


class CoverageGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.run_dir = root / "run"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "code").mkdir()
        self.config = load_config(PROJECT_ROOT / "config", workspace=root / "workspace")
        self.config.runtime["verbose"] = False
        self.events = EventLog(self.run_dir / "events.jsonl", "t-cov")
        self.ctx = SimpleNamespace(
            config=self.config, events=self.events, run_dir=self.run_dir,
            blackboard=Blackboard(self.run_dir / "artifacts", self.events, "t-cov"),
            bus=MessageBus(self.run_dir / "messages.jsonl", self.events, "t-cov"),
            sandbox=Sandbox(self.run_dir / "code", self.config, events=self.events),
            prompts=None, llm=None, tools=None, experience=None,
            log=lambda *a, **k: None, run_state={}, consult=None,
            run_id="t-cov",
        )
        self.agent = TesterAgent(self.ctx)
        self.task = NodeTask(node_id="test", title="业务测试", role="tester",
                             kind="task", iteration=1)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, payloads, data_cases, max_attempts=2):
        self.config.runtime["test_coverage_max_attempts"] = max_attempts
        self.ctx.llm = _StubLLM(payloads)
        result = AgentResult()
        data = {"files": [{"path": "tests/test_all.py", "content": "pass"}],
                "cases": data_cases}
        return self.agent._ensure_full_coverage(
            self.task, Generation(data=data, provider="stub"), data, _spec()["function_points"],
            result), result

    def test_full_coverage_skips_repair(self):
        (data, _gen), result = self._run([], _FULL_CASES)
        self.assertNotEqual(result.status, "fail", result.error)
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.agent._coverage_gaps(data["cases"], _spec()["function_points"]), [])

    def test_gap_triggers_repair_with_missing_pairs(self):
        good = {"files": [{"path": "tests/test_all.py", "content": "pass"}],
                "cases": _FULL_CASES}
        (data, _gen), result = self._run([good], _GAP_CASES)

        self.assertNotEqual(result.status, "fail", result.error)
        self.assertEqual(len(self.ctx.llm.calls), 1)
        kind, prompt = self.ctx.llm.calls[0]
        self.assertEqual(kind, "test_suite_repair")
        self.assertIn("FP-02:exception", prompt)
        self.assertIn("FP-02:boundary", prompt)
        self.assertIn("覆盖缺口", prompt)
        repaired = [e for e in self.events.events
                    if e.type == "test_coverage_repair_requested"]
        self.assertEqual(len(repaired), 1)
        self.assertIn("FP-02:exception", repaired[0].payload["gaps"])
        fixed = [e for e in self.events.events if e.type == "test_coverage_repaired"]
        self.assertEqual(len(fixed), 1)
        self.assertEqual(len(data["cases"]), 6)

    def test_exhausted_attempts_fails_without_freeze(self):
        still_gap = {"files": [{"path": "tests/test_all.py", "content": "pass"}],
                     "cases": _GAP_CASES}
        (_data, _gen), result = self._run([still_gap], _GAP_CASES)

        self.assertEqual(result.status, "fail")
        self.assertIn("FP-02:exception", result.error)
        self.assertIn("FP-02:boundary", result.error)
        failed = [e for e in self.events.events if e.type == "test_coverage_insufficient"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload["gap_count"], 2)
        # 关键断言：失败路径不落任何冻结清单
        self.assertFalse((self.run_dir / "test_manifest.json").exists())

    def test_no_llm_skips_gate_with_trace(self):
        """无 LLM 客户端（单元测试直连）时跳过校验并留痕，不判失败、不冻结。"""
        result = AgentResult()
        data = {"files": [], "cases": _GAP_CASES}
        self.agent._ensure_full_coverage(
            self.task, Generation(data=data, provider="stub"), data,
            _spec()["function_points"], result)
        self.assertNotEqual(result.status, "fail")
        skipped = [e for e in self.events.events if e.type == "test_coverage_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("FP-02:exception", skipped[0].payload["gaps"])
        self.assertFalse((self.run_dir / "test_manifest.json").exists())

    def test_gap_report_ignores_malformed_cases(self):
        cases = _FULL_CASES + [None, "junk", {"fp_id": "", "category": "functional"}]
        self.assertEqual(self.agent._coverage_gaps(cases, _spec()["function_points"]), [])


if __name__ == "__main__":
    unittest.main()
