"""门禁表达式求值器测试。"""

from __future__ import annotations

import unittest

from mas.expr import evaluate

CONTEXT = {
    "test_report": {
        "summary": {"failed": 0, "passed": 10, "total": 10},
        "coverage": {"functional": {"rate": 1.0}, "exception": {"rate": 1.0}, "boundary": {"rate": 0.5}},
        "negotiation": {"open": 0},
    },
    "bug_reports": {"open": 0},
}


class ExprTests(unittest.TestCase):
    def test_eq_pass(self):
        result = evaluate("test_report.summary.failed == 0", CONTEXT)
        self.assertTrue(result.ok)
        self.assertTrue(bool(result.value))

    def test_missing_field(self):
        result = evaluate("test_report.summary.missing == 0", CONTEXT)
        self.assertFalse(result.ok)
        self.assertIn("字段缺失", result.error)

    def test_boolean_combo(self):
        result = evaluate(
            "test_report.summary.failed == 0 and test_report.coverage.boundary.rate >= 1.0", CONTEXT
        )
        self.assertTrue(result.ok)
        self.assertFalse(bool(result.value))

    def test_functions(self):
        self.assertTrue(bool(evaluate("len(test_report.summary) > 2", CONTEXT)))
        self.assertFalse(bool(evaluate("min(1, 2) > 3", CONTEXT)))

    def test_syntax_error(self):
        result = evaluate("test_report.summary.failed ==", CONTEXT)
        self.assertFalse(result.ok)
        self.assertIn("语法错误", result.error)

    def test_forbidden_syntax(self):
        result = evaluate("__import__('os')", {})
        self.assertFalse(result.ok)
        result = evaluate("(lambda: 1)()", {})
        self.assertFalse(result.ok)

    def test_arithmetic(self):
        self.assertTrue(bool(evaluate("1 + 2 * 3 == 7", CONTEXT)))


if __name__ == "__main__":
    unittest.main()
