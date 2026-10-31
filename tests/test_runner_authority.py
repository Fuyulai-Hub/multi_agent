"""框架侧 runner 的执行权回归（测试文件不能决定跑哪些 case / 结果）。

锁定四件事：
1. runner 只执行冻结清单 test_ids；文件里多写的用例不执行、缺的记 error；
2. 测试文件打印 ``<<<MASS_RESULT>>>`` 伪结果不影响真实逐用例记录；
3. 文件末尾 ``if __name__ == "__main__"`` 不被执行；
4. 结果文件写在沙箱工作区之外，并由 verifier 只认 runner 记录。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mas.config import load_config
from mas.sandbox import Sandbox
from mas.verifier import TestVerifier
from mas.verify.exec import execute_frozen_suite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT_ROOT / "mas" / "verify" / "runner.py"

GOOD_SUITE = '''"""runner 测试套件。"""
import json
import unittest


class TestFp01(unittest.TestCase):
    def test_functional_ok(self):
        print("<<<MASS_RESULT>>>" + json.dumps(
            {"total": 99, "passed": 99, "failed": 0, "cases": []}))
        self.assertEqual(1 + 1, 2)

    def test_exception_bad(self):
        self.assertEqual(1, 2)

    def test_boundary_extra(self):
        """不在冻结清单里的用例：runner 不得执行。"""
        raise AssertionError("extra case must not run")


if __name__ == "__main__":
    with open("main_should_not_run.txt", "w", encoding="utf-8") as fh:
        fh.write("main executed")
'''


class RunnerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "code"
        (self.root / "tests").mkdir(parents=True)
        (self.root / "tests" / "test_all.py").write_text(GOOD_SUITE, encoding="utf-8")
        self.ids = [
            "tests.test_all.TestFp01.test_functional_ok",
            "tests.test_all.TestFp01.test_exception_bad",
        ]
        self.manifest_path = Path(self.tmp.name) / "test_manifest.json"
        self.out_path = Path(self.tmp.name) / "evidence" / "runner_result.json"
        self._write_manifest(self.ids)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_manifest(self, test_ids, digest="digest-1"):
        self.manifest_path.write_text(json.dumps({
            "version": 2,
            "test_file": "tests/test_all.py",
            "files": {"tests/test_all.py": "sha"},
            "digest": digest,
            "test_ids": test_ids,
            "case_meta": {
                test_id: {"fp_id": "FP-01", "category": test_id.rsplit(".", 1)[-1].split("_")[1]}
                for test_id in test_ids
            },
        }), encoding="utf-8")

    def _run(self, *extra):
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--manifest", str(self.manifest_path),
             "--root", str(self.root), "--out", str(self.out_path), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        return proc, data

    def test_only_frozen_test_ids_are_executed(self):
        proc, data = self._run()
        self.assertEqual(data["total"], 2)
        self.assertEqual({case["test_id"] for case in data["cases"]}, set(self.ids))
        self.assertEqual(data["unexpected_test_ids"], [
            "tests.test_all.TestFp01.test_boundary_extra"])
        self.assertFalse((self.root / "main_should_not_run.txt").exists())
        statuses = {case["test_id"]: case["status"] for case in data["cases"]}
        self.assertEqual(statuses[self.ids[0]], "passed")
        self.assertEqual(statuses[self.ids[1]], "failed")
        # 有 failed + 额外用例 → 非 0；stdout 里的伪 marker 不改变记录
        self.assertEqual(proc.returncode, 1)
        self.assertIn("<<<MASS_RESULT>>>", proc.stdout)
        self.assertNotEqual(data["total"], 99)

    def test_only_filter_cannot_expand_execution_set(self):
        proc, data = self._run("--only", "test_functional_ok")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["cases"][0]["test_id"], self.ids[0])
        self.assertEqual(proc.returncode, 0)

    def test_missing_frozen_case_is_an_error_record(self):
        missing = "tests.test_all.TestFp01.test_functional_missing"
        self._write_manifest([self.ids[0], missing])
        proc, data = self._run()
        self.assertEqual(data["total"], 2)
        record = next(case for case in data["cases"] if case["test_id"] == missing)
        self.assertEqual(record["status"], "error")
        self.assertIn("test_id_not_found", record["failure"])
        self.assertEqual(proc.returncode, 1)

    def test_manifest_without_test_ids_is_rejected(self):
        self._write_manifest([])
        proc, data = self._run()
        self.assertEqual(data["error"], "manifest_has_no_test_ids")
        self.assertEqual(data["total"], 0)
        self.assertEqual(proc.returncode, 2)


CLASS_FIXTURE_SUITE = '''"""类级夹具：只有经过 TestSuite.run 才会执行 setUpClass。"""
import unittest


class TestFp01(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resource = {"ready": True}

    @classmethod
    def tearDownClass(cls):
        cls.resource.clear()

    def test_functional_uses_fixture(self):
        self.assertTrue(self.resource["ready"])

    def test_smoke_uses_fixture(self):
        self.assertEqual(sorted(self.resource), ["ready"])
'''


class RunnerClassFixtureTests(unittest.TestCase):
    """回归：逐条 test.run() 会绕过 setUpClass（task11 实测 14/49 假失败）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "code"
        (self.root / "tests").mkdir(parents=True)
        self.suite_path = self.root / "tests" / "test_all.py"
        self.manifest_path = Path(self.tmp.name) / "test_manifest.json"
        self.out_path = Path(self.tmp.name) / "evidence" / "runner_result.json"
        self.ids = [
            "tests.test_all.TestFp01.test_functional_uses_fixture",
            "tests.test_all.TestFp01.test_smoke_uses_fixture",
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, source):
        self.suite_path.write_text(source, encoding="utf-8")
        self.manifest_path.write_text(json.dumps({
            "version": 2,
            "test_file": "tests/test_all.py",
            "files": {"tests/test_all.py": "sha"},
            "digest": "digest-1",
            "test_ids": self.ids,
            "case_meta": {
                test_id: {"fp_id": "FP-01", "category": test_id.rsplit(".", 1)[-1].split("_")[1]}
                for test_id in self.ids
            },
        }), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--manifest", str(self.manifest_path),
             "--root", str(self.root), "--out", str(self.out_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return proc, json.loads(self.out_path.read_text(encoding="utf-8"))

    def test_class_fixtures_run_once_around_manifest_cases(self):
        proc, data = self._run(CLASS_FIXTURE_SUITE)
        statuses = {case["test_id"]: case["status"] for case in data["cases"]}
        self.assertEqual(statuses, {test_id: "passed" for test_id in self.ids})
        self.assertEqual(data["failed"], 0)
        self.assertEqual(proc.returncode, 0)

    def test_failing_setup_class_is_attributed_to_its_cases(self):
        proc, data = self._run(CLASS_FIXTURE_SUITE.replace(
            'cls.resource = {"ready": True}',
            'raise RuntimeError("fixture boom")'))
        self.assertEqual(data["total"], 2)
        for case in data["cases"]:
            self.assertEqual(case["status"], "error")
            self.assertIn("setUpClass", case["failure"])
            self.assertIn("fixture boom", case["failure"])
        self.assertEqual(proc.returncode, 1)

    def test_failing_teardown_class_is_not_silently_passed(self):
        proc, data = self._run(CLASS_FIXTURE_SUITE.replace(
            'cls.resource.clear()',
            'raise RuntimeError("teardown boom")'))
        statuses = [case["status"] for case in data["cases"]]
        self.assertIn("error", statuses)
        tail = data["cases"][-1]
        self.assertEqual(tail["status"], "error")
        self.assertIn("tearDownClass", tail["failure"])
        self.assertEqual(proc.returncode, 1)


INTEGRATION_SUITE = '''"""集成用例。"""
import unittest


class TestFp01(unittest.TestCase):
    def test_functional_ok(self):
        self.assertEqual(1 + 1, 2)

    def test_exception_bad(self):
        self.assertEqual(1, 2)
'''


class RunnerVerifierIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.sandbox_root = self.run_dir / "code"
        (self.sandbox_root / "tests").mkdir(parents=True)
        (self.sandbox_root / "tests" / "test_all.py").write_text(
            INTEGRATION_SUITE, encoding="utf-8")
        self.config = load_config(PROJECT_ROOT / "config",
                                  workspace=Path(self.tmp.name) / "workspace")
        self.config.runtime["sandbox_backend"] = "local"
        self.sandbox = Sandbox(self.sandbox_root, self.config)
        self.verifier = TestVerifier(self.run_dir, self.sandbox_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_runner_result_is_outside_workspace_and_attestable(self):
        manifest = self.verifier.freeze(
            "tests/test_all.py", 1,
            case_meta=[
                {"fp_id": "FP-01", "category": "functional", "op": "ok", "name": ""},
                {"fp_id": "FP-01", "category": "exception", "op": "bad", "name": ""},
            ],
        )
        execution = execute_frozen_suite(
            self.sandbox, self.run_dir, manifest, iteration=1, tag="test")
        self.assertIsNotNone(execution.result, execution.exec.stderr)
        self.assertEqual(execution.result["total"], 2)
        self.assertEqual(execution.result["failed"], 1)
        self.assertNotIn(self.sandbox_root.resolve(), execution.result_path.resolve().parents)
        self.assertTrue(execution.result_path.exists())

        attestation = self.verifier.attest(
            parsed=execution.result, exit_code=execution.exec.exit_code,
            duration_ms=execution.result["duration_ms"],
            test_file="tests/test_all.py", iteration=1, spec_fps=["FP-01"],
        )
        # 证据可信（runner 记录完整一致），只是有用例失败——失败由调用方按 failed 判定
        self.assertTrue(attestation["valid"], attestation["reasons"])
        self.assertEqual(attestation["failed"], 1)
        self.assertEqual(attestation["test_hash"], manifest["digest"])
        self.assertEqual(attestation["total"], 2)
        self.assertTrue(all(case["fp_id"] == "FP-01" for case in attestation["cases"]))


if __name__ == "__main__":
    unittest.main()
