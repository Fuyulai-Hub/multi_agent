"""端到端流水线测试（离线 Provider，真实沙箱执行）。

覆盖：
- 完整链路：需求 → 方案 → 实现 → 审查 → 测试 → 修复 → 验收 → 报告
- 缺陷闭环：注入缺陷被测试发现 → 研发修复 → 回归通过
- 自学习闭环：第二轮运行注入历史经验，步数与缺陷显著下降
- 文字需求输入通道
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mas.config import load_config
from mas.leader import Leader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


class PipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        # 每个用例独立工作区，避免运行顺序带来的经验库耦合
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.workspace)
        self.config.llm["provider"] = "offline"  # 端到端用例固定离线 Provider，与 system.yaml 默认解耦
        self.config.runtime["verbose"] = False   # 测试时静默事件回显

    def tearDown(self):
        self.tmp.cleanup()

    def _read_artifact(self, run_dir: Path, name: str):
        path = run_dir / "artifacts" / name / "latest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_first_run_full_loop(self):
        summary = Leader(self.config).run(str(REQUIREMENT))
        run_dir = Path(summary.run_dir)

        self.assertEqual(summary.verdict, "pass", summary.error)
        # 需求评估
        spec = self._read_artifact(run_dir, "requirement_spec")
        self.assertEqual(len(spec["function_points"]), 7)
        # 技术方案
        design = self._read_artifact(run_dir, "solution_design")
        self.assertTrue(design["evaluation"]["selected"])
        # 审查驳回发生过（首轮缺文档字符串）
        self.assertGreaterEqual(summary.review_rejections, 1)
        self.assertGreaterEqual(summary.rollbacks, 2)
        # 缺陷闭环：注入缺陷被测试发现并修复
        self.assertGreaterEqual(summary.bugs_total, 1)
        self.assertEqual(summary.bugs_fixed, summary.bugs_total)
        bugs = self._read_artifact(run_dir, "bug_reports")
        self.assertEqual(bugs["open"], 0)
        # 测试报告
        test_report = self._read_artifact(run_dir, "test_report")
        self.assertEqual(test_report["summary"]["failed"], 0)
        for category in ("functional", "exception", "boundary"):
            self.assertEqual(test_report["coverage"][category]["rate"], 1.0)
        # 交付代码真实存在且可执行
        self.assertTrue((run_dir / "code" / "order_pricing" / "core.py").exists())
        self.assertTrue((run_dir / "code" / "tests" / "test_order_pricing.py").exists())
        # 研发输出了缺陷修复的回归自测报告
        fix_report = self._read_artifact(run_dir, "fix_test_report")
        self.assertTrue(fix_report["all_pass"])
        # 门禁与报告
        gate = self._read_artifact(run_dir, "acceptance_gate")
        self.assertTrue(gate["passed"])
        report = self._read_artifact(run_dir, "acceptance_report")
        self.assertEqual(report["verdict"], "pass")
        self.assertTrue((run_dir / "reports" / "acceptance_report.md").exists())
        # 证据与经验
        self.assertTrue((run_dir / "evidence" / "index.jsonl").exists())
        self.assertTrue((run_dir / "events.jsonl").exists())
        self.assertTrue((run_dir / "prompts").is_dir())

    def test_second_run_self_learning(self):
        first = Leader(self.config).run(str(REQUIREMENT))
        self.assertEqual(first.verdict, "pass")
        second = Leader(self.config).run(str(REQUIREMENT))

        # 经验被注入（Prompt 分层注入生效）
        self.assertGreater(second.metrics["injected_experiences"], 0)
        # 守护经验生效：不再出现缺陷与审查驳回
        self.assertEqual(second.bugs_total, 0)
        self.assertEqual(second.review_rejections, 0)
        # 步数显著下降（首轮 12 步 -> 次轮 7 步）
        self.assertLess(second.steps, first.steps)
        self.assertLessEqual(second.rollbacks, first.rollbacks)
        # 经验库有沉淀且发生晋级
        library_lines = (self.workspace / "knowledge" / "experience.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        self.assertGreater(len(library_lines), 0)
        entries = [json.loads(line) for line in library_lines]
        self.assertTrue(any(entry["tier"] in ("L2", "L3") for entry in entries))

    def test_text_requirement_input(self):
        text = REQUIREMENT.read_text(encoding="utf-8")
        summary = Leader(self.config).run(text, as_text=True)
        self.assertEqual(summary.verdict, "pass", summary.error)
        run_dir = Path(summary.run_dir)
        source = self._read_artifact(run_dir, "requirement_source")
        self.assertEqual(source["source"], "text")

    def test_learning_report_generated(self):
        # 跑两轮：第二轮报告时指标库已有首轮数据，产生真实对比
        first = Leader(self.config).run(str(REQUIREMENT))
        second = Leader(self.config).run(str(REQUIREMENT))
        self.assertEqual(second.verdict, "pass")
        report = self.workspace / "reports" / "learning_report.md"
        self.assertTrue(report.exists())
        content = report.read_text(encoding="utf-8")
        self.assertIn("自学习", content)
        self.assertIn(first.run_id, content)


if __name__ == "__main__":
    unittest.main()
