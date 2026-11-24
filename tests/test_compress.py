"""上下文压缩测试（docs/16 的 02 文档）。

覆盖：
- ``found_artifacts``：enabled=false 恒等；**未达 per_item_chars 阈值的产物原样透传**；
  requirement_spec 压缩后 JSON 仍可解析、FP id/title/rules 全保留、samples/parameters 被丢；
  test_report 保 summary/coverage；review_report 保 blocking_items；bug_reports 只保 open items；
- 预算 0 / 负数 / artifact 为 null 或标量时不抛异常；
- total_chars 超限时按优先级裁剪；
- ``truncate_tool_results`` 头尾保留语义与关闭（max_chars<=0）时的恒等返回。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas import compress
from mas.agents.base import BaseAgent, Perception
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.experience import RetrievalResult
from mas.prompts import PromptBuilder
from mas.schemas import NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _ProbeAgent(BaseAgent):
    """最小 Agent：只为驱动真实的 ``perceive`` 接线路径（不触发生成）。"""

    role_id = "product"
    contracts = {"default": {"title": "string"}}

    def build_context(self, task, perception):
        return {}

    def apply(self, *args, **kwargs):  # pragma: no cover - 本文件不触发生成
        return None


def _spec(n: int = 12) -> dict:
    function_points = []
    for index in range(n):
        function_points.append({
            "id": f"FP-{index:02d}",
            "title": f"功能 {index}",
            "description": "描述" * 8,
            "parameters": [{"name": f"p{pos}", "type": "str"} for pos in range(6)],
            "inputs": ["a: str"],
            "outputs": ["b: Decimal"],
            "rules": [f"规则{index}-A", f"规则{index}-B"],
            "samples": [{"input": "x" * 80, "expect": "y" * 80} for _ in range(4)],
            "category": "功能",
            "acceptance": [f"验收{index}"],
            "ambiguous": False,
        })
    return {
        "id": "REQ-1", "title": "需求", "background": ["背景"], "constraints": ["约束"],
        "tags": ["tag"], "function_points": function_points,
        "acceptance_criteria": [{"id": "AC-01", "description": "d"}], "open_questions": [],
    }


class FoundArtifactsTests(unittest.TestCase):
    def test_disabled_is_identity(self):
        found = {"requirement_spec": _spec(3)}
        result = compress.found_artifacts(found, enabled=False)
        self.assertEqual(result.artifacts, found)
        self.assertEqual(result.before_chars, result.after_chars)
        self.assertEqual(result.compressed, [])

    def test_under_threshold_is_passthrough(self):
        """未达 per_item_chars 阈值的产物必须原样透传（这是「按阈值触发」的核心）。"""
        found = {"requirement_spec": _spec(1)}
        size = compress._size(found["requirement_spec"])
        result = compress.found_artifacts(found, enabled=True, per_item_chars=size + 1,
                                          total_chars=10_000_000)
        self.assertEqual(result.artifacts["requirement_spec"], found["requirement_spec"])
        self.assertEqual(result.compressed, [])
        self.assertIn("requirement_spec", result.untouched)

    def test_over_threshold_compresses(self):
        spec = _spec(12)
        result = compress.found_artifacts({"requirement_spec": spec}, enabled=True,
                                          per_item_chars=compress._size(spec) - 1,
                                          total_chars=10_000_000)
        self.assertIn("requirement_spec", result.compressed)
        self.assertLess(result.after_chars, result.before_chars)
        json.dumps(result.artifacts["requirement_spec"], ensure_ascii=False)   # 仍是可解析 JSON

    def test_spec_compression_keeps_structure(self):
        result = compress.found_artifacts({"requirement_spec": _spec()}, enabled=True,
                                          per_item_chars=1000, total_chars=10_000_000)
        spec = result.artifacts["requirement_spec"]
        json.dumps(spec, ensure_ascii=False)                 # 仍是可解析的 JSON 结构
        self.assertIn("requirement_spec", result.compressed)
        for fp in spec["function_points"]:
            self.assertTrue(fp["id"])
            self.assertTrue(fp["title"])
            self.assertTrue(fp["rules"])
            self.assertNotIn("samples", fp)                  # samples 被丢
            self.assertNotIn("parameters", fp)               # parameters 被丢
        self.assertLess(result.after_chars, result.before_chars)

    def test_spec_deep_when_over_per_item_budget(self):
        result = compress.found_artifacts({"requirement_spec": _spec()}, enabled=True,
                                          per_item_chars=200, total_chars=10_000_000)
        for fp in result.artifacts["requirement_spec"]["function_points"]:
            self.assertIn("id", fp)
            self.assertIn("title", fp)
            self.assertIn("rules", fp)                        # 深压也不丢 rules

    def test_zero_and_negative_budget_do_not_raise(self):
        for budget in (0, -5):
            result = compress.found_artifacts({"requirement_spec": _spec(2)}, enabled=True,
                                              per_item_chars=budget, total_chars=10_000_000)
            json.dumps(result.artifacts["requirement_spec"], ensure_ascii=False)

    def test_null_and_scalar_artifacts_untouched(self):
        found = {"requirement_spec": None, "other": 5, "listy": [1, 2, 3]}
        result = compress.found_artifacts(found, enabled=True)
        self.assertIsNone(result.artifacts["requirement_spec"])
        self.assertEqual(result.artifacts["other"], 5)
        self.assertEqual(result.artifacts["listy"], [1, 2, 3])
        self.assertIn("other", result.untouched)

    def test_test_report_keeps_counts(self):
        report = {
            "package": "demo", "summary": {"total": 10, "failed": 0},
            "coverage": {"functional": {"rate": 1.0}},
            "negotiation": {"open": 0, "closed": 2},
            "files": [{"path": "tests/x.py", "content": "x" * 200}],
            "cases": [{"id": f"TC-{i}", "call": "f()" * 40} for i in range(20)],
        }
        result = compress.found_artifacts({"test_report": report}, enabled=True, per_item_chars=1000,
                                          total_chars=10_000_000)
        out = result.artifacts["test_report"]
        self.assertEqual(out["summary"], report["summary"])
        self.assertEqual(out["coverage"], report["coverage"])
        self.assertNotIn("cases", out)                        # 逐条明细被丢
        self.assertNotIn("files", out)

    def test_review_report_keeps_blocking_items(self):
        report = {"verdict": "reject", "blocking": 1, "stats": {"total": 3},
                  "blocking_items": [{"rule": "r", "severity": "major"}],
                  "findings": [{"message": "m" * 100} for _ in range(20)]}
        result = compress.found_artifacts({"review_report": report}, enabled=True, per_item_chars=500,
                                          total_chars=10_000_000)
        out = result.artifacts["review_report"]
        self.assertEqual(out["blocking_items"], report["blocking_items"])
        self.assertNotIn("findings", out)

    def test_bug_reports_keep_only_open_items(self):
        bugs = {"total": 3, "open": 1, "closed": 2, "items": [
            {"id": "B-1", "status": "closed"}, {"id": "B-2", "status": "open"}]}
        result = compress.found_artifacts({"bug_reports": bugs}, enabled=True, per_item_chars=50,
                                          total_chars=10_000_000)
        out = result.artifacts["bug_reports"]
        self.assertEqual([item["id"] for item in out["items"]], ["B-2"])
        self.assertEqual(out["total"], 3)

    def test_total_budget_trims_lower_priority(self):
        found = {
            "requirement_spec": _spec(3),
            "test_report": {"summary": {"total": 5}, "coverage": {}, "package": "p",
                            "cases": [{"id": f"TC-{i}", "call": "c" * 90} for i in range(30)]},
        }
        result = compress.found_artifacts(found, enabled=True, per_item_chars=100000, total_chars=300)
        self.assertLessEqual(result.after_chars, result.before_chars)
        json.dumps(result.artifacts["test_report"], ensure_ascii=False)


class TruncateToolResultsTests(unittest.TestCase):
    def test_head_tail_preserved_with_note(self):
        content = "".join(str(index % 10) for index in range(100))
        messages = [{"role": "tool", "tool_call_id": "c1", "content": content}]
        out = compress.truncate_tool_results(messages, max_chars=20)
        truncated = out[0]["content"]
        self.assertTrue(truncated.startswith(content[:12]))       # 头 60%
        self.assertTrue(truncated.endswith(content[-8:]))         # 尾 40%
        self.assertIn("省略 80 字符", truncated)
        self.assertEqual(out[0]["tool_call_id"], "c1")            # 其它字段不变
        self.assertEqual(messages[0]["content"], content)         # 原消息不被就地修改

    def test_short_content_untouched(self):
        messages = [{"role": "tool", "content": "short"}]
        self.assertEqual(compress.truncate_tool_results(messages, max_chars=4000), messages)

    def test_disabled_returns_identity(self):
        messages = [{"role": "tool", "content": "x" * 10000}]
        out = compress.truncate_tool_results(messages, max_chars=0)
        self.assertIs(out, messages)


class PerceiveWiringTests(unittest.TestCase):
    """``BaseAgent.perceive`` 的接线边界：压缩只改 Prompt 文本，不改 ``Perception.found``。

    这是启用压缩后最容易踩的坑：下游 Agent 会从 ``perception.found`` 里取
    ``test_report.test_file`` / ``bug_reports.items`` / ``env_probe`` 做逻辑判断，
    若把压缩结果塞回 found，压缩就会改变流程行为（而不只是改变 Prompt 长度）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "test-compress-perceive")
        self.blackboard = Blackboard(self.run_dir / "artifacts", self.events, "test-compress-perceive")
        self.bus = MessageBus(self.run_dir / "messages.jsonl", self.events, "test-compress-perceive")
        self.prompts = PromptBuilder(self.config, self.run_dir, enabled=False)
        self.ctx = SimpleNamespace(
            config=self.config, events=self.events, run_dir=self.run_dir,
            blackboard=self.blackboard, bus=self.bus, prompts=self.prompts,
            llm=None, tools=None, experience=None, log=lambda *a, **k: None,
            run_state={}, consult=None, run_id="test-compress-perceive",
        )
        self.task = NodeTask(node_id="intake", title="解析需求文档", role="product",
                             kind="task", iteration=1)

    def _publish_spec(self):
        spec = _spec(12)
        self.blackboard.publish(
            "requirement_spec", spec, producer="product", node="intake",
            iteration=1, summary="test",
        )
        # 感知关系按 collaboration.yaml 走：product 本不带 requirement_spec，
        # 这里显式注入，使 perceive 读到刚发布的大产物（不改配置文件的其余部分）
        self.config.collaboration.perception = {"product": ["requirement_spec"]}
        return spec

    def _agent(self):
        return _ProbeAgent(self.ctx)

    def test_found_keeps_full_artifact_when_compressed(self):
        spec = self._publish_spec()
        self.config.runtime["context_compression"] = {
            "enabled": True, "per_item_chars": compress._size(spec) - 1, "total_chars": 10_000_000,
        }
        perception = self._agent().perceive(self.task)
        # found 仍是完整产物：未被压缩删字段
        self.assertIn("samples", perception.found["requirement_spec"]["function_points"][0])
        self.assertEqual(len(perception.found["requirement_spec"]["function_points"]), 12)
        # 而进 Prompt 的文本已被压缩
        self.assertIn("_compressed", perception.artifacts_brief)
        self.assertNotIn("samples", perception.artifacts_brief)

    def test_disabled_keeps_brief_uncompressed(self):
        self._publish_spec()
        self.config.runtime["context_compression"] = {"enabled": False}
        perception = self._agent().perceive(self.task)
        self.assertNotIn("_compressed", perception.artifacts_brief)

    def test_event_emitted_only_when_enabled(self):
        self._publish_spec()
        self.config.runtime["context_compression"] = {"enabled": False}
        self._agent().perceive(self.task)
        types = [json.loads(line)["type"]
                 for line in (self.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertNotIn("context_compressed", types)

        self.config.runtime["context_compression"] = {"enabled": True, "per_item_chars": 30000}
        self._agent().perceive(self.task)
        types = [json.loads(line)["type"]
                 for line in (self.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn("context_compressed", types)


if __name__ == "__main__":
    unittest.main()
