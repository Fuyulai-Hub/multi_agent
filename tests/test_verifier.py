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
from mas.verifier import TestVerifier, VerificationError, code_tree_hash, verify_attestation

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
    return {"test_id": f"tests.test_all.TestFp01.{name}", "test": name,
            "class": "TestFp01", "fp_id": fp_id, "category": category,
            "status": status, "failure": "", "duration": 0.001}


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
        manifest = self.verifier.freeze("tests/test_all.py", 1, case_meta=[{
            "fp_id": "FP-01", "category": "functional", "op": "add", "name": "",
        }])
        self.assertTrue((self.run_dir / "test_manifest.json").exists())
        self.assertTrue((self.run_dir / "frozen_tests" / "tests" / "test_all.py").exists())
        self.assertEqual(manifest["test_file"], "tests/test_all.py")
        self.assertEqual(manifest["test_ids"], [
            "tests.test_all.TestFp01.test_boundary_zero",
            "tests.test_all.TestFp01.test_exception_div",
            "tests.test_all.TestFp01.test_functional_add",
        ])
        self.assertEqual(
            manifest["case_meta"]["tests.test_all.TestFp01.test_functional_add"]["fp_id"], "FP-01")
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
        payload = dict(parsed)
        payload["runner"] = {
            "name": "mas.verify.runner", "version": 1,
            "manifest_digest": self.verifier.load_manifest()["digest"],
        }
        return self.verifier.attest(
            parsed=payload, exit_code=exit_code, duration_ms=10,
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

    def test_marker_result_without_runner_metadata_is_invalid(self):
        """stdout marker 时代的结果（无 runner 元数据）不再被门禁采信。"""
        attestation = self.verifier.attest(
            parsed=_parsed(), exit_code=0, duration_ms=10,
            test_file="tests/test_all.py", iteration=1, spec_fps=["FP-01"],
        )
        self.assertFalse(attestation["valid"])
        self.assertIn("untrusted_result_source", attestation["reasons"])

    def test_manifest_digest_mismatch_is_invalid(self):
        payload = _parsed()
        payload["runner"] = {
            "name": "mas.verify.runner", "version": 1, "manifest_digest": "other",
        }
        attestation = self.verifier.attest(
            parsed=payload, exit_code=0, duration_ms=10,
            test_file="tests/test_all.py", iteration=1, spec_fps=["FP-01"],
        )
        self.assertFalse(attestation["valid"])
        self.assertIn("manifest_digest_mismatch", attestation["reasons"])

    def test_unexpected_case_is_not_executed_by_gate(self):
        cases = [
            _case("test_functional_add", "functional"),
            _case("test_exception_div", "exception"),
            _case("test_boundary_zero", "boundary"),
            _case("test_functional_extra", "functional"),
        ]
        attestation = self._attest(_parsed(cases))
        self.assertFalse(attestation["valid"])
        self.assertTrue(any(reason.startswith("unexpected_case_ids")
                            for reason in attestation["reasons"]))

    def test_frozen_case_missing_is_invalid(self):
        attestation = self._attest(_parsed([
            _case("test_functional_add", "functional"),
            _case("test_exception_div", "exception"),
        ]))
        self.assertFalse(attestation["valid"])
        self.assertTrue(any(reason.startswith("manifest_cases_not_executed")
                            for reason in attestation["reasons"]))

    def test_runner_error_is_invalid(self):
        payload = _parsed()
        payload["error"] = "no_test_id_matched"
        attestation = self._attest(payload)
        self.assertFalse(attestation["valid"])
        self.assertIn("runner_error:no_test_id_matched", attestation["reasons"])


CORE_SOURCE = '''"""核心实现。"""
def add(a, b):
    """相加。"""
    return a + b


def unused(a):
    """未被测试覆盖的函数。"""
    return a
'''

INIT_SOURCE = '''"""交付包。"""
from .core import add

__all__ = ["add"]
'''

FROZEN_TEST = '''"""冻结测试。"""
import unittest

from pkg import add


class TestPkg(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)
'''


class ReviewerBlockingRecomputeTests(unittest.TestCase):
    """review_report.blocking 必须由框架确定性证据重算，而不是采信模型 JSON。

    新口径：fp_mapping 的值必须是真实的 ``file::symbol``，且符号要被**通过的
    冻结测试** import 并使用；只有 key 存在不能算实现。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.code_root = (root / "code").resolve()
        (self.code_root / "pkg").mkdir(parents=True)
        (self.code_root / "pkg" / "core.py").write_text(CORE_SOURCE, encoding="utf-8")
        (self.code_root / "pkg" / "__init__.py").write_text(INIT_SOURCE, encoding="utf-8")
        self.run_dir = root / "run"
        frozen = self.run_dir / "frozen_tests" / "tests"
        frozen.mkdir(parents=True)
        (frozen / "test_pkg.py").write_text(FROZEN_TEST, encoding="utf-8")

        def read_file(relative: str) -> str:
            path = (self.code_root / relative).resolve()
            if self.code_root not in path.parents or not path.is_file():
                raise FileNotFoundError(relative)
            return path.read_text(encoding="utf-8")

        self.config = load_config(PROJECT_ROOT / "config")
        self.events = SimpleNamespace(emit=lambda *args, **kwargs: None)
        reviewer = ReviewerAgent.__new__(ReviewerAgent)
        reviewer.role = self.config.role("reviewer")
        reviewer.ctx = SimpleNamespace(
            events=self.events, config=self.config, run_dir=self.run_dir,
            sandbox=SimpleNamespace(root=self.code_root, read_file=read_file),
        )
        self.reviewer = reviewer
        self.task = NodeTask(node_id="review", title="代码审查", role="reviewer",
                             kind="task", iteration=1)
        self.perception = SimpleNamespace(
            spec={"function_points": [{"id": "FP-01"}]},
            found={
                "code_manifest": {
                    "files": ["pkg/core.py", "pkg/__init__.py"],
                    "fp_mapping": {"FP-01": "pkg/core.py::add"},
                },
                "test_report": {
                    "summary": {"total": 1, "failed": 0},
                    "verification_attestation": {"valid": True},
                },
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _recompute(self, data=None, context=None):
        return self.reviewer._recompute_blocking(
            self.task, data or {"verdict": "pass", "blocking": 0, "findings": []},
            self.perception, context or {"policies": {}, "static_analysis": {"findings": []}},
        )

    def _mapping(self, value):
        self.perception.found["code_manifest"]["fp_mapping"] = {"FP-01": value}
        return self._recompute()

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
        result = self._recompute()
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(result["blocking_items"][0]["symbol"], "FP-02")
        self.assertEqual(result["verdict"], "reject")

    def test_model_cannot_inflate_framework_blocking_count(self):
        result = self._recompute(data={"verdict": "reject", "blocking": 99, "findings": [
            {"rule": "made_up", "severity": "critical", "file": "x.py", "line": 1}]})
        self.assertEqual(result["blocking"], 0)
        self.assertEqual(result["verdict"], "reject")     # 模型仍可保守驳回，但不能伪造计数

    def test_valid_mapping_sets_code_hash_and_audit(self):
        result = self._recompute()
        self.assertEqual(result["blocking"], 0)
        self.assertEqual(result["code_hash"], code_tree_hash(self.code_root))
        self.assertTrue(result["code_hash"])
        self.assertEqual(result["mapping_audit"]["format"], "file::symbol")
        self.assertEqual(result["mapping_audit"]["valid"], 1)
        self.assertEqual(result["coverage"]["implemented"], 1)

    def test_bare_function_name_is_invalid(self):
        """旧格式（裸函数名）不再被采信：用户实测「映射到不存在的函数也能过」。"""
        result = self._mapping("add")
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(result["blocking_items"][0]["rule"], "invalid_fp_mapping_format")

    def test_nonexistent_symbol_is_blocking(self):
        result = self._mapping("pkg/core.py::missing")
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(result["blocking_items"][0]["rule"], "fp_mapping_symbol_missing")

    def test_symbol_not_covered_by_passing_test_is_blocking(self):
        result = self._mapping("pkg/core.py::unused")
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(
            result["blocking_items"][0]["rule"],
            "fp_mapping_not_covered_by_passing_test",
        )

    def test_nonexistent_file_is_blocking(self):
        manifest = self.perception.found["code_manifest"]
        manifest["files"].append("pkg/other.py")
        manifest["fp_mapping"] = {"FP-01": "pkg/other.py::add"}
        result = self._recompute()
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(result["blocking_items"][0]["rule"], "fp_mapping_file_missing")

    def test_review_without_passing_tests_is_blocking(self):
        self.perception.found["test_report"] = {
            "summary": {"total": 1, "failed": 1},
            "verification_attestation": {"valid": False},
        }
        result = self._recompute()
        rules = {item["rule"] for item in result["blocking_items"]}
        self.assertIn("review_without_passing_tests", rules)
        self.assertEqual(result["mapping_audit"]["test_evidence"]["passed"], False)
        self.assertEqual(result["verdict"], "reject")


if __name__ == "__main__":
    unittest.main()
