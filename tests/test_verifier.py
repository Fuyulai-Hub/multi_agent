"""测试冻结与独立验证（docs/37 P0-2）的单元测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.reviewer import ReviewerAgent
from mas.config import load_config
from mas.schemas import NodeTask
from mas.verifier import TestVerifier, VerificationError, verify_attestation

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GOOD_TEST = '''"""测试。"""
import unittest


class TestFp01(unittest.TestCase):
    def test_functional_add(self):
        self.assertEqual(1 + 1, 2)

    def test_exception_div(self):
        with self.assertRaises(ZeroDivisionError):
            1 / 0

    def test_boundary_zero(self):
        self.assertTrue(0 == 0)
'''


def _case(name: str, category: str, status: str = "passed", fp_id: str = "FP-01"):
    return {"test": name, "class": "TestFp01", "fp_id": fp_id,
            "category": category, "status": status, "message": ""}


def _parsed(cases=None):
    cases = cases or [
        _case("test_functional_add", "functional"),
        _case("test_exception_div", "exception"),
        _case("test_boundary_zero", "boundary"),
    ]
    failed = sum(1 for case in cases if case["status"] != "passed")
    return {"total": len(cases), "passed": len(cases) - failed, "failed": failed,
            "duration_ms": 12, "cases": cases}


class TestFreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.run_dir = root / "run"
        self.sandbox_root = root / "run" / "code"
        (self.sandbox_root / "tests").mkdir(parents=True)
        self.test_path = self.sandbox_root / "tests" / "test_all.py"
        self.test_path.write_text(GOOD_TEST, encoding="utf-8")
        self.events = SimpleNamespace(emit=lambda *args, **kwargs: None)
        self.verifier = TestVerifier(self.run_dir, self.sandbox_root, events=self.events)

    def tearDown(self):
        self.tmp.cleanup()

    def test_freeze_creates_manifest_and_copies(self):
        manifest = self.verifier.freeze("tests/test_all.py", 1)
        self.assertTrue((self.run_dir / "test_manifest.json").exists())
        self.assertTrue((self.run_dir / "frozen_tests" / "tests" / "test_all.py").exists())
        self.assertEqual(manifest["test_file"], "tests/test_all.py")
        self.assertTrue(verify_attestation(manifest))
        self.assertIsNotNone(self.verifier.load_manifest())

    def test_modified_test_is_restored(self):
        self.verifier.freeze("tests/test_all.py", 1)
        self.test_path.write_text('"""弱化。"""\nimport unittest\n\n\nclass X(unittest.TestCase):\n'
                                  '    def test_x(self):\n        pass\n', encoding="utf-8")
        changes = self.verifier.restore_if_changed()
        self.assertEqual(changes, [{"path": "tests/test_all.py", "reason": "modified"}])
        self.assertEqual(self.test_path.read_text(encoding="utf-8"), GOOD_TEST)

    def test_added_test_file_is_removed(self):
        self.verifier.freeze("tests/test_all.py", 1)
        extra = self.sandbox_root / "tests" / "test_weakened.py"
        extra.write_text("pass\n", encoding="utf-8")
        changes = self.verifier.restore_if_changed()
        self.assertEqual(changes, [{"path": "tests/test_weakened.py", "reason": "added"}])
        self.assertFalse(extra.exists())

    def test_tampered_manifest_is_rejected(self):
        self.verifier.freeze("tests/test_all.py", 1)
        path = self.run_dir / "test_manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["test_file"] = "tests/other.py"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(VerificationError):
            self.verifier.load_manifest()

    def test_invalidated_manifest_allows_regeneration(self):
        self.verifier.freeze("tests/test_all.py", 1)
        self.verifier.invalidate("protocol")
        self.assertIsNone(self.verifier.load_manifest())
        self.assertEqual(self.verifier.restore_if_changed(), [])
        self.verifier.freeze("tests/test_all.py", 2)
        self.assertIsNotNone(self.verifier.load_manifest())


class TestAttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.run_dir = root / "run"
        self.sandbox_root = root / "run" / "code"
        (self.sandbox_root / "tests").mkdir(parents=True)
        self.test_path = self.sandbox_root / "tests" / "test_all.py"
        self.test_path.write_text(GOOD_TEST, encoding="utf-8")
        self.verifier = TestVerifier(self.run_dir, self.sandbox_root)
        self.verifier.freeze("tests/test_all.py", 1)

    def tearDown(self):
        self.tmp.cleanup()

    def _attest(self, parsed, exit_code=0):
        return self.verifier.attest(
            parsed=parsed, exit_code=exit_code, duration_ms=10,
            test_file="tests/test_all.py", iteration=1, spec_fps=["FP-01"],
        )

    def test_valid_attestation_recomputes_coverage(self):
        attestation = self._attest(_parsed())
        self.assertTrue(attestation["valid"], attestation["reasons"])
        self.assertEqual(attestation["total"], 3)
        self.assertEqual(attestation["failed"], 0)
        self.assertEqual(attestation["coverage"]["functional"]["rate"], 1.0)
        self.assertGreater(attestation["assertions"], 0)
        on_disk = json.loads((self.run_dir / "verification_attestation.json").read_text(encoding="utf-8"))
        self.assertTrue(verify_attestation(on_disk))

    def test_self_reported_total_mismatch_is_invalid(self):
        parsed = _parsed()
        parsed["total"] = 99
        attestation = self._attest(parsed)
        self.assertFalse(attestation["valid"])
        self.assertIn("summary_total_mismatch", attestation["reasons"])

    def test_test_file_change_after_freeze_is_invalid(self):
        self.test_path.write_text(GOOD_TEST + "\n# changed\n", encoding="utf-8")
        attestation = self._attest(_parsed())
        self.assertFalse(attestation["valid"])
        self.assertIn("test_files_changed_during_execution", attestation["reasons"])

    def test_unknown_fp_is_invalid(self):
        parsed = _parsed([_case("test_functional_add", "functional", fp_id="FP-99")])
        attestation = self._attest(parsed)
        self.assertFalse(attestation["valid"])
        self.assertTrue(any(reason.startswith("unknown_fp_id") for reason in attestation["reasons"]))

    def test_duplicate_cases_are_invalid(self):
        duplicate = _case("test_functional_add", "functional")
        parsed = _parsed([duplicate, dict(duplicate), duplicate])
        attestation = self._attest(parsed)
        self.assertFalse(attestation["valid"])
        self.assertIn("duplicate_case_id", attestation["reasons"])

    def test_no_assertions_is_invalid(self):
        self.test_path.write_text('"""空。"""\nimport unittest\n\n\nclass T(unittest.TestCase):\n'
                                  '    def test_functional_x(self):\n        pass\n', encoding="utf-8")
        self.verifier.freeze("tests/test_all.py", 2)
        attestation = self._attest(_parsed([_case("test_functional_x", "functional")]))
        self.assertFalse(attestation["valid"])
        self.assertIn("no_assertions_in_frozen_tests", attestation["reasons"])

    def test_exit_code_inconsistent_is_invalid(self):
        parsed = _parsed([_case("test_functional_add", "functional", status="failed")])
        attestation = self._attest(parsed, exit_code=0)
        self.assertFalse(attestation["valid"])
        self.assertIn("exit_code_zero_with_failures", attestation["reasons"])


class ReviewerBlockingRecomputeTests(unittest.TestCase):
    """review_report.blocking 必须由框架确定性证据重算，而不是采信模型 JSON。"""

    def setUp(self):
        self.config = load_config(PROJECT_ROOT / "config")
        self.events = SimpleNamespace(emit=lambda *args, **kwargs: None)
        reviewer = ReviewerAgent.__new__(ReviewerAgent)
        reviewer.role = self.config.role("reviewer")
        reviewer.ctx = SimpleNamespace(events=self.events, config=self.config)
        self.reviewer = reviewer
        self.task = NodeTask(node_id="review", title="代码审查", role="reviewer",
                             kind="task", iteration=1)
        self.perception = SimpleNamespace(
            spec={"function_points": [{"id": "FP-01"}]},
            found={"code_manifest": {"files": ["pkg/core.py"], "fp_mapping": {"FP-01": "add"}}},
        )

    def test_model_cannot_hide_deterministic_blockers(self):
        context = {
            "policies": {"blocking_severities": ["critical", "major"]},
            "static_analysis": {"findings": [{
                "rule": "eval_usage", "severity": "critical", "file": "pkg/core.py",
                "line": 3, "symbol": "run", "message": "使用了 eval",
            }]},
        }
        data = {"verdict": "pass", "blocking": 0, "findings": [], "summary": "模型说没问题"}
        result = self.reviewer._recompute_blocking(self.task, data, self.perception, context)
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(result["verdict"], "reject")
        self.assertEqual(result["llm_verdict"], "pass")
        self.assertEqual(result["blocking_items"][0]["rule"], "eval_usage")

    def test_missing_function_point_is_deterministic_blocker(self):
        self.perception.spec = {"function_points": [{"id": "FP-01"}, {"id": "FP-02"}]}
        result = self.reviewer._recompute_blocking(
            self.task, {"verdict": "pass", "blocking": 0, "findings": []},
            self.perception, {"policies": {}, "static_analysis": {"findings": []}},
        )
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(result["blocking_items"][0]["symbol"], "FP-02")
        self.assertEqual(result["verdict"], "reject")

    def test_model_cannot_inflate_framework_blocking_count(self):
        result = self.reviewer._recompute_blocking(
            self.task, {"verdict": "reject", "blocking": 99, "findings": [
                {"rule": "made_up", "severity": "critical", "file": "x.py", "line": 1}]},
            self.perception, {"policies": {}, "static_analysis": {"findings": []}},
        )
        self.assertEqual(result["blocking"], 0)
        self.assertEqual(result["verdict"], "reject")     # 模型仍可保守驳回，但不能伪造计数


if __name__ == "__main__":
    unittest.main()
