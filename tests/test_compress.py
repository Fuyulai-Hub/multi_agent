"""上下文压缩测试（docs/16 的 02 文档）。

覆盖：
- ``found_artifacts``：enabled=false 恒等；requirement_spec 压缩后 JSON 仍可解析、
  FP id/title/rules 全保留、samples/parameters 被丢；test_report 保 summary/coverage；
  review_report 保 blocking_items；bug_reports 只保 open items；
- 预算 0 / 负数 / artifact 为 null 或标量时不抛异常；
- total_chars 超限时按优先级裁剪；
- ``truncate_tool_results`` 头尾保留语义与关闭（max_chars<=0）时的恒等返回。
"""

from __future__ import annotations

import json
import unittest

from mas import compress


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

    def test_spec_compression_keeps_structure(self):
        result = compress.found_artifacts({"requirement_spec": _spec()}, enabled=True,
                                          per_item_chars=1000000)
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
                                          per_item_chars=200)
        for fp in result.artifacts["requirement_spec"]["function_points"]:
            self.assertIn("id", fp)
            self.assertIn("title", fp)
            self.assertIn("rules", fp)                        # 深压也不丢 rules

    def test_zero_and_negative_budget_do_not_raise(self):
        for budget in (0, -5):
            result = compress.found_artifacts({"requirement_spec": _spec(2)}, enabled=True,
                                              per_item_chars=budget)
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
        result = compress.found_artifacts({"test_report": report}, enabled=True, per_item_chars=1000)
        out = result.artifacts["test_report"]
        self.assertEqual(out["summary"], report["summary"])
        self.assertEqual(out["coverage"], report["coverage"])
        self.assertNotIn("cases", out)                        # 逐条明细被丢
        self.assertNotIn("files", out)

    def test_review_report_keeps_blocking_items(self):
        report = {"verdict": "reject", "blocking": 1, "stats": {"total": 3},
                  "blocking_items": [{"rule": "r", "severity": "major"}],
                  "findings": [{"message": "m" * 100} for _ in range(20)]}
        result = compress.found_artifacts({"review_report": report}, enabled=True, per_item_chars=500)
        out = result.artifacts["review_report"]
        self.assertEqual(out["blocking_items"], report["blocking_items"])
        self.assertNotIn("findings", out)

    def test_bug_reports_keep_only_open_items(self):
        bugs = {"total": 3, "open": 1, "closed": 2, "items": [
            {"id": "B-1", "status": "closed"}, {"id": "B-2", "status": "open"}]}
        result = compress.found_artifacts({"bug_reports": bugs}, enabled=True)
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


if __name__ == "__main__":
    unittest.main()
