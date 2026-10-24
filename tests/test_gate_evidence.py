"""门径证据链（docs/39 门径优化）回归测试。

覆盖四层：
1. 证据包字段与签名：requirement/code/visible/hidden/env hash、runner_version、
   per_case_digest、coverage、run_id 全部随证据一起签名，门禁可重算；
2. TOCTOU 与篡改：取证后改代码/改测试/改冻结副本/改签名/改逐用例/换 run_id/
   换需求，``verify_evidence`` 都必须判失败且给出具体 reason；
3. release 密钥策略：缺密钥或密钥过短时 Leader 拒绝启动，绝不退回进程随机；
   稳定密钥可跨进程验签（子进程签、父进程验）；
4. Leader 门禁集成：``_run_gate`` 除 criterion 表达式外，还会硬校验证据链——
   代码在测试通过后被改，门禁整体失败且失败原因可见。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.blackboard import Blackboard
from mas.config import load_config
from mas.contract import freeze_contract, hidden_suite_dir
from mas.errors import ConfigError, SandboxError
from mas.hidden import create_hidden_suite
from mas.leader import Leader
from mas.sandbox import Sandbox
from mas.schemas import AgentContext, NodeTask
from mas.verifier import (
    TestVerifier,
    VerificationError,
    code_tree_hash,
    release_mode,
    require_signing_secret,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 仅测试用的固定签名密钥（不写入生产配置/凭据文件）
TEST_SECRET = "gate-evidence-test-secret-0123456789abcdef"[:40]
_ENV_KEYS = ("MAS_ENV", "MAS_VERIFIER_RELEASE", "MAS_VERIFIER_SECRET")
EVIDENCE_CRITERION = "测试证据链完整（框架重验）"
CONTRACT_CRITERION = "需求契约已冻结且未被篡改"
HIDDEN_CRITERION = "隐藏用例通过（契约冻结后独立生成）"
REVIEW_CRITERION = "审查结论绑定最终代码"

CODE = '''def add(a, b):
    return a + b
'''

TEST_SUITE = '''"""门径证据链测试套件。"""
import unittest

from pkg.core import add


class TestFp01(unittest.TestCase):
    def test_functional_add(self):
        self.assertEqual(add(1, 2), 3)

    def test_exception_wrong_type(self):
        with self.assertRaises(TypeError):
            add(1, "2")

    def test_boundary_zero(self):
        self.assertEqual(add(0, 0), 0)
'''

CASE_META = [
    {"fp_id": "FP-01", "category": "functional", "op": "add"},
    {"fp_id": "FP-01", "category": "exception", "op": "wrong_type"},
    {"fp_id": "FP-01", "category": "boundary", "op": "zero"},
]

REQUIREMENT_TEXT = "# 加法服务\n\n- add(a, b) 必须返回两数之和\n"

SPEC = {
    "title": "加法服务",
    "package": "pkg",
    "delivery_shape": "backend_only",
    "function_points": [{
        "id": "FP-01", "title": "两数相加",
        "samples": [{"input": "add(1, 2)", "expect": "3"}],
        "acceptance_criteria": ["add(1, 2) 返回 3"],
        "source_spans": [{"clause_id": "C-01"}],
    }],
}

HIDDEN_SUITE = '''"""隐藏用例（冻结契约派生）。"""
import unittest

from pkg.core import add


class TestHiddenAdd(unittest.TestCase):
    def test_functional_add_result(self):
        self.assertEqual(add(1, 2), 3)
'''

HIDDEN_SUITE_FAILING = HIDDEN_SUITE.replace("add(1, 2), 3", "add(1, 2), 4")

HIDDEN_CASES = [{
    "id": "TH-FP-01-functional-add-result", "fp_id": "FP-01",
    "op": "add", "name": "result", "category": "functional",
    "call": "add(1, 2)", "expect": "3",
}]


@contextlib.contextmanager
def _env(overrides=None, remove=()):
    """临时改写环境变量并在退出时恢复（隔离宿主机上的 MAS_* 配置）。"""
    keys = set(overrides or {}) | set(remove)
    backup = {key: os.environ.get(key) for key in keys}
    try:
        for key in remove:
            os.environ.pop(key, None)
        os.environ.update(overrides or {})
        yield
    finally:
        for key, value in backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _EventSink:
    """收集事件的最小实现（不落盘，便于断言门禁发出的证据校验事件）。"""

    def __init__(self):
        self.events = []

    def emit(self, kind, **kwargs):
        self.events.append({"kind": kind, **kwargs})


def _parsed(verifier: TestVerifier) -> dict:
    """按冻结清单构造 runner 逐用例记录（全部 passed）。

    这里不跑真实 runner（执行权由 tests/test_runner_authority.py 覆盖），
    只验证 attest/verify_evidence 的证据绑定与重算逻辑。
    """
    manifest = verifier.load_manifest()
    cases = [
        {"test_id": test_id, "status": "passed", "failure": ""}
        for test_id in manifest["test_ids"]
    ]
    return {
        "total": len(cases),
        "passed": len(cases),
        "failed": 0,
        "duration_ms": 5,
        "cases": cases,
        "unexpected_test_ids": [],
        "runner": {
            "name": "mas.verify.runner",
            "version": 1,
            "manifest_digest": manifest["digest"],
        },
    }


class _EvidenceFixture(unittest.TestCase):
    def setUp(self):
        self._env_ctx = _env(remove=_ENV_KEYS)
        self._env_ctx.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.run_dir = root / "run"
        self.sandbox_root = self.run_dir / "code"
        (self.sandbox_root / "pkg").mkdir(parents=True)
        (self.sandbox_root / "tests").mkdir(parents=True)
        (self.sandbox_root / "pkg" / "core.py").write_text(CODE, encoding="utf-8")
        (self.sandbox_root / "tests" / "test_all.py").write_text(TEST_SUITE, encoding="utf-8")
        self.verifier = TestVerifier(self.run_dir, self.sandbox_root, run_id="run-fixed")

    def tearDown(self):
        self.tmp.cleanup()
        self._env_ctx.__exit__(None, None, None)

    def _freeze_and_attest(self, *, requirement_spec=SPEC, run_id="run-fixed"):
        self.verifier.freeze("tests/test_all.py", 1, case_meta=CASE_META)
        attestation = self.verifier.attest(
            parsed=_parsed(self.verifier), exit_code=0, duration_ms=5,
            test_file="tests/test_all.py", iteration=1, spec_fps=["FP-01"],
            requirement_spec=requirement_spec, run_id=run_id,
        )
        self.assertTrue(attestation["valid"], attestation["reasons"])
        return attestation

    def _verify(self, attestation, *, requirement_spec=SPEC, run_id="run-fixed"):
        return self.verifier.verify_evidence(
            attestation, requirement_spec=requirement_spec, run_id=run_id)


class EvidenceBundleTests(_EvidenceFixture):
    def test_bundle_binds_hashes_and_verifies(self):
        attestation = self._freeze_and_attest()
        for field in ("requirement_hash", "code_hash", "visible_suite_hash",
                      "hidden_suite_hash", "env_hash", "per_case_digest"):
            self.assertTrue(attestation[field], f"证据包缺少 {field}")
        self.assertEqual(attestation["runner_version"], 1)
        self.assertEqual(attestation["run_id"], "run-fixed")
        self.assertEqual(attestation["test_hash"], attestation["hidden_suite_hash"])
        self.assertEqual(attestation["coverage"]["functional"]["rate"], 1.0)

        outcome = self._verify(attestation)
        self.assertTrue(outcome["passed"], outcome["reasons"])
        self.assertTrue(outcome["signature_valid"])
        self.assertEqual(outcome["recomputed"]["code_hash"], attestation["code_hash"])
        self.assertEqual(outcome["recomputed"]["env_hash"], attestation["env_hash"])
        on_disk = json.loads(
            (self.run_dir / "verification_attestation.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["signature"], attestation["signature"])

    def test_code_changed_after_attest_fails_toctou(self):
        attestation = self._freeze_and_attest()
        (self.sandbox_root / "pkg" / "core.py").write_text(
            CODE + "\n# 测试通过后又改了代码\n", encoding="utf-8")
        outcome = self._verify(attestation)
        self.assertFalse(outcome["passed"])
        self.assertIn("code_hash_mismatch", outcome["reasons"])
        # 签名本身仍然有效：失败来自门禁重算，而不是签名被改
        self.assertTrue(outcome["signature_valid"])

    def test_changed_test_file_is_rejected(self):
        attestation = self._freeze_and_attest()
        path = self.sandbox_root / "tests" / "test_all.py"
        path.write_text(
            TEST_SUITE + "\n\nclass TestExtra(unittest.TestCase):\n"
                          "    def test_added(self):\n        pass\n",
            encoding="utf-8")
        outcome = self._verify(attestation)
        self.assertFalse(outcome["passed"])
        self.assertIn("visible_suite_hash_mismatch", outcome["reasons"])
        self.assertIn("visible_suite_differs_from_frozen", outcome["reasons"])

    def test_tampered_frozen_copy_is_rejected(self):
        attestation = self._freeze_and_attest()
        frozen = self.run_dir / "frozen_tests" / "tests" / "test_all.py"
        frozen.write_text(TEST_SUITE + "\n# tampered\n", encoding="utf-8")
        outcome = self._verify(attestation)
        self.assertFalse(outcome["passed"])
        self.assertIn("hidden_suite_hash_mismatch", outcome["reasons"])

    def test_tampered_attestation_fails_signature(self):
        attestation = self._freeze_and_attest()
        tampered = dict(attestation)
        tampered["total"] = 1
        outcome = self._verify(tampered)
        self.assertFalse(outcome["passed"])
        self.assertIn("signature_invalid", outcome["reasons"])

    def test_tampered_cases_fail_per_case_digest(self):
        attestation = self._freeze_and_attest()
        tampered = json.loads(json.dumps(attestation))
        tampered["cases"][0]["status"] = "failed"
        outcome = self._verify(tampered)
        self.assertFalse(outcome["passed"])
        self.assertIn("per_case_digest_mismatch", outcome["reasons"])

    def test_wrong_run_id_is_rejected(self):
        attestation = self._freeze_and_attest()
        outcome = self._verify(attestation, run_id="run-other")
        self.assertFalse(outcome["passed"])
        self.assertIn("run_id_mismatch", outcome["reasons"])

    def test_changed_requirement_is_rejected(self):
        attestation = self._freeze_and_attest()
        outcome = self._verify(
            attestation, requirement_spec={**SPEC, "title": "改过的需求"})
        self.assertFalse(outcome["passed"])
        self.assertIn("requirement_hash_mismatch", outcome["reasons"])

    def test_legacy_attestation_without_evidence_fields_is_rejected(self):
        attestation = self._freeze_and_attest()
        legacy = json.loads(json.dumps(attestation))
        for field in ("code_hash", "env_hash", "runner_version", "per_case_digest"):
            legacy.pop(field, None)
        outcome = self._verify(legacy)
        self.assertFalse(outcome["passed"])
        self.assertIn("code_hash_missing", outcome["reasons"])
        self.assertIn("env_hash_missing", outcome["reasons"])
        self.assertIn("runner_version_mismatch", outcome["reasons"])


class ReleaseKeyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.run_dir = root / "run"
        self.sandbox_root = self.run_dir / "code"
        (self.sandbox_root / "pkg").mkdir(parents=True)
        (self.sandbox_root / "tests").mkdir(parents=True)
        (self.sandbox_root / "pkg" / "core.py").write_text(CODE, encoding="utf-8")
        (self.sandbox_root / "tests" / "test_all.py").write_text(TEST_SUITE, encoding="utf-8")
        self.config = load_config(PROJECT_ROOT / "config", workspace=root / "ws")
        self.config.runtime.setdefault("verifier", {})["release_mode"] = False

    def tearDown(self):
        self.tmp.cleanup()

    def test_release_mode_without_secret_refuses_startup(self):
        with _env({"MAS_ENV": "release"},
                  remove=("MAS_VERIFIER_SECRET", "MAS_VERIFIER_RELEASE")):
            self.assertTrue(release_mode(self.config))
            with self.assertRaises(ConfigError) as caught:
                require_signing_secret(self.config)
            self.assertIn("MAS_VERIFIER_SECRET", str(caught.exception))
            with self.assertRaises(VerificationError):
                TestVerifier(self.run_dir, self.sandbox_root, config=self.config)
            with self.assertRaises(ConfigError):
                Leader(self.config)

    def test_release_mode_rejects_short_secret(self):
        with _env({"MAS_ENV": "release", "MAS_VERIFIER_SECRET": "too-short"},
                  remove=("MAS_VERIFIER_RELEASE",)):
            with self.assertRaises(ConfigError):
                require_signing_secret(self.config)
            with self.assertRaises(VerificationError):
                TestVerifier(self.run_dir, self.sandbox_root, config=self.config)

    def test_release_mode_accepts_stable_secret(self):
        with _env({"MAS_ENV": "release", "MAS_VERIFIER_SECRET": TEST_SECRET},
                  remove=("MAS_VERIFIER_RELEASE",)):
            require_signing_secret(self.config)
            verifier = TestVerifier(self.run_dir, self.sandbox_root, config=self.config)
            self.assertTrue(verifier.release_mode)
            Leader(self.config)  # 不抛异常即启动通过

    def test_config_channel_enables_release_policy(self):
        self.config.runtime["verifier"]["release_mode"] = True
        with _env(remove=_ENV_KEYS):
            self.assertTrue(release_mode(self.config))
            with self.assertRaises(ConfigError):
                require_signing_secret(self.config)
            with self.assertRaises(ConfigError):
                Leader(self.config)


class CrossProcessKeyTests(unittest.TestCase):
    def test_release_secret_survives_process_restart(self):
        """子进程签 manifest/attestation，父进程用同一环境密钥验签。

        父子进程的 ``_PROCESS_SECRET`` 必然不同；能验签只可能来自
        ``MAS_VERIFIER_SECRET``，这正是 release 模式要求的稳定密钥。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = f'''
import json, sys
from pathlib import Path
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from mas.verifier import TestVerifier

root = Path({str(root)!r})
sandbox = root / "code"
(sandbox / "pkg").mkdir(parents=True)
(sandbox / "tests").mkdir(parents=True)
(sandbox / "pkg" / "core.py").write_text({CODE!r}, encoding="utf-8")
(sandbox / "tests" / "test_all.py").write_text({TEST_SUITE!r}, encoding="utf-8")
verifier = TestVerifier(root / "run", sandbox, run_id="run-fixed")
manifest = verifier.freeze("tests/test_all.py", 1, case_meta={CASE_META!r})
cases = [{{"test_id": test_id, "status": "passed", "failure": ""}}
         for test_id in manifest["test_ids"]]
parsed = {{"total": len(cases), "passed": len(cases), "failed": 0, "duration_ms": 1,
           "cases": cases, "unexpected_test_ids": [],
           "runner": {{"name": "mas.verify.runner", "version": 1,
                     "manifest_digest": manifest["digest"]}}}}
attestation = verifier.attest(
    parsed=parsed, exit_code=0, duration_ms=1, test_file="tests/test_all.py",
    iteration=1, spec_fps=["FP-01"], requirement_spec={SPEC!r}, run_id="run-fixed")
print(json.dumps({{"valid": attestation["valid"],
                   "cases": len(attestation["cases"])}}))
'''
            child_env = dict(os.environ)
            child_env.pop("MAS_VERIFIER_RELEASE", None)
            child_env.update({"MAS_ENV": "release", "MAS_VERIFIER_SECRET": TEST_SECRET})
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=child_env, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            result = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(result["valid"], proc.stdout)
            self.assertEqual(result["cases"], 3)

            run_dir = root / "run"
            sandbox = root / "code"
            with _env({"MAS_ENV": "release", "MAS_VERIFIER_SECRET": TEST_SECRET},
                      remove=("MAS_VERIFIER_RELEASE",)):
                parent = TestVerifier(run_dir, sandbox, run_id="run-fixed")
                self.assertIsNotNone(parent.load_manifest())
                attestation = json.loads(
                    (run_dir / "verification_attestation.json").read_text(encoding="utf-8"))
                outcome = parent.verify_evidence(
                    attestation, requirement_spec=SPEC, run_id="run-fixed")
                self.assertTrue(outcome["passed"], outcome["reasons"])


class ContainerMountTests(unittest.TestCase):
    def test_hidden_suite_is_mounted_read_only(self):
        """容器后端下隐藏套件必须 :ro 挂载，证据目录可写；非法模式直接拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code"
            root.mkdir()
            hidden = Path(tmp) / "hidden"
            io_dir = Path(tmp) / "io"
            fake = SimpleNamespace(
                root=root.resolve(),
                container={"network": "none", "read_only": True, "tmpfs": "/tmp",
                           "user": "", "memory": "1g", "cpus": "1.0",
                           "pids_limit": 0, "image": "img", "python": "python"},
                _container_runtime_path="docker",
                _container_executable=lambda executable: "python",
                _container_path_part=lambda part: part,
                _container_env=lambda extra: {},
            )
            argv, _, _, _ = Sandbox._container_invocation(
                fake, ["python", "runner.py"], fake.root, {}, {},
                extra_mounts=[(str(io_dir), "/mas-verify", "rw"),
                              (str(hidden), "/mas-hidden", "ro")],
            )
            self.assertIn(f"{io_dir.resolve()}:/mas-verify:rw", argv)
            self.assertIn(f"{hidden.resolve()}:/mas-hidden:ro", argv)
            with self.assertRaises(SandboxError):
                Sandbox._container_invocation(
                    fake, ["python", "runner.py"], fake.root, {}, {},
                    extra_mounts=[(str(hidden), "/mas-hidden", "rwx")],
                )


class GateIntegrationTests(_EvidenceFixture):
    def setUp(self):
        super().setUp()
        self.config = load_config(
            PROJECT_ROOT / "config", workspace=Path(self.tmp.name) / "ws")
        self.config.runtime["verbose"] = False
        self.leader = Leader(self.config)
        self.events = _EventSink()
        self.blackboard = Blackboard(self.run_dir / "artifacts", self.events, "run-fixed")
        # 隐藏用例执行需要真实沙箱（runner/解释器/工作目录）；取证侧同实例保证 env_hash 一致
        sandbox = Sandbox(self.sandbox_root, self.config, events=self.events)
        self.verifier = TestVerifier(
            self.run_dir, self.sandbox_root, config=self.config, sandbox=sandbox,
            run_id="run-fixed",
        )
        self.ctx = AgentContext(
            config=self.config, run_id="run-fixed", run_dir=self.run_dir,
            events=self.events, blackboard=self.blackboard,
            bus=SimpleNamespace(broadcast=lambda *args, **kwargs: None),
            llm=None, sandbox=sandbox, prompts=None, experience=None,
        )

    def _publish_ready(self, attestation, *, hidden_suite=HIDDEN_SUITE,
                       review_report=None):
        def publish(name, data, producer, node):
            self.blackboard.publish(
                name, data, producer=producer, node=node, iteration=1, summary="test")

        review = {
            "blocking": 0, "verdict": "pass",
            "code_hash": code_tree_hash(self.sandbox_root),
        }
        if review_report:
            review.update(review_report)

        publish("requirement_source", {"text": REQUIREMENT_TEXT, "source": "text"},
                "leader", "intake")
        publish("requirement_spec", SPEC, "product", "requirement")
        frozen = freeze_contract(
            self.run_dir, SPEC, REQUIREMENT_TEXT, run_id="run-fixed", config=self.config)
        publish("requirement_contract", {
            "path": "contract/requirement_contract.json",
            "run_id": frozen["run_id"],
            "contract_hash": frozen["contract_hash"],
            "requirement_hash": frozen["requirement_hash"],
            "signature": frozen["signature"],
            "signature_alg": frozen["signature_alg"],
            "clauses": frozen["clauses"],
            "out_of_scope": frozen["out_of_scope"],
            "coverage": {"total": len(frozen["clauses"]), "mapped": len(frozen["clauses"]),
                         "out_of_scope": 0, "uncovered": []},
        }, "product", "requirement")
        hidden = create_hidden_suite(
            config=self.config, run_dir=self.run_dir, sandbox=self.ctx.sandbox,
            run_id="run-fixed", spec=frozen["spec"],
            contract_hash=frozen["contract_hash"],
            data={"files": [{"path": "tests/test_hidden_pkg.py", "content": hidden_suite}],
                  "cases": HIDDEN_CASES},
            iteration=1, package="pkg",
        )
        publish("hidden_suite", hidden, "product", "requirement")
        publish("solution_design", {"evaluation": {"selected": "方案A"}},
                "developer", "solution")
        publish("review_report", review, "reviewer", "review")
        publish("bug_reports", {"open": 0}, "tester", "test")
        publish("ui_evidence", {"failed_asserts": 0, "asserts": [],
                                "delivery_shape": "backend_only",
                                "probe_version": "", "http_status": 0}, "tester", "test")
        publish("test_report", {
            "summary": {"failed": 0},
            "coverage": attestation["coverage"],
            "negotiation": {"open": 0},
            "verification_attestation": attestation,
        }, "tester", "test")

    def _gate(self, iteration=1):
        task = NodeTask.from_node(
            self.config.flow.node("acceptance"), iteration=iteration)
        self.leader._run_gate(self.ctx, task, {})
        return self.blackboard.get("acceptance_gate")

    @staticmethod
    def _evidence_item(gate):
        return next(
            item for item in gate["criteria"] if item["id"] == EVIDENCE_CRITERION)

    @staticmethod
    def _hidden_item(gate):
        return next(
            item for item in gate["criteria"] if item["id"] == HIDDEN_CRITERION)

    @staticmethod
    def _contract_item(gate):
        return next(
            item for item in gate["criteria"] if item["id"] == CONTRACT_CRITERION)

    @staticmethod
    def _review_item(gate):
        return next(
            item for item in gate["criteria"] if item["id"] == REVIEW_CRITERION)

    def test_gate_passes_with_intact_evidence(self):
        self._publish_ready(self._freeze_and_attest())
        gate = self._gate()
        self.assertTrue(gate["passed"], gate["failed_criteria"])
        self.assertTrue(gate["evidence"]["passed"])
        self.assertTrue(self._evidence_item(gate)["passed"])
        self.assertTrue(self._contract_item(gate)["passed"])
        self.assertTrue(self._review_item(gate)["passed"])
        self.assertTrue(self._hidden_item(gate)["passed"])
        self.assertEqual(self._hidden_item(gate)["value"]["summary"]["failed"], 0)
        self.assertTrue((self.run_dir / "hidden_attestation.json").exists())
        self.assertTrue(any(
            event["kind"] == "gate_evidence_checked" and event.get("severity") == "info"
            for event in self.events.events))
        self.assertTrue(any(
            event["kind"] == "gate_hidden_checked" and event.get("severity") == "info"
            for event in self.events.events))

    def test_gate_fails_when_hidden_case_fails(self):
        """可见用例全过但隐藏用例失败时，门禁必须整体失败。"""
        self._publish_ready(
            self._freeze_and_attest(), hidden_suite=HIDDEN_SUITE_FAILING)
        gate = self._gate()
        self.assertFalse(gate["passed"])
        self.assertTrue(self._evidence_item(gate)["passed"])
        item = self._hidden_item(gate)
        self.assertFalse(item["passed"])
        self.assertIn("hidden_cases_failed:1", item["value"]["reasons"])
        self.assertTrue(any(
            event["kind"] == "gate_hidden_checked" and event.get("severity") == "error"
            for event in self.events.events))

    def test_gate_fails_when_hidden_frozen_copy_is_tampered(self):
        self._publish_ready(self._freeze_and_attest())
        root = hidden_suite_dir(self.config.workspace_dir, "run-fixed")
        frozen_test = root / "frozen_tests" / "tests" / "test_hidden_pkg.py"
        frozen_test.write_text(
            frozen_test.read_text(encoding="utf-8") + "\n# 冻结后篡改\n", encoding="utf-8")
        gate = self._gate()
        self.assertFalse(gate["passed"])
        reasons = self._hidden_item(gate)["value"]["reasons"]
        self.assertIn("frozen_manifest_digest_mismatch", reasons)

    def test_gate_fails_when_spec_changes_after_freeze(self):
        """契约冻结后下游改写需求规格：即使测试仍全过，门禁也拒绝。"""
        self._publish_ready(self._freeze_and_attest())
        forged = json.loads(json.dumps(SPEC))
        forged["title"] = "下游偷偷改过的需求"
        self.blackboard.publish(
            "requirement_spec", forged, producer="developer", node="solution",
            iteration=2, summary="篡改规格")
        gate = self._gate(iteration=2)
        self.assertFalse(gate["passed"])
        item = self._contract_item(gate)
        self.assertFalse(item["passed"])
        self.assertIn("requirement_spec_modified_after_freeze", item["value"]["reasons"])

    def test_gate_fails_when_code_changes_after_tests(self):
        self._publish_ready(self._freeze_and_attest())
        self.assertTrue(self._gate()["evidence"]["passed"])

        (self.sandbox_root / "pkg" / "core.py").write_text(
            CODE + "\n# 验收前又被改了一次\n", encoding="utf-8")
        gate = self._gate(iteration=2)
        self.assertFalse(gate["passed"])
        item = self._evidence_item(gate)
        self.assertFalse(item["passed"])
        self.assertIn("code_hash_mismatch", item["value"]["reasons"])
        self.assertIn("code_hash_mismatch", gate["evidence"]["reasons"])
        # 签名仍有效，失败源自门禁重算的 code hash
        self.assertTrue(gate["evidence"]["signature_valid"])
        # 审查结论同样绑定旧代码：review 的 code_hash 与最终代码不一致，不得签字
        review_item = self._review_item(gate)
        self.assertFalse(review_item["passed"])
        self.assertIn("review_code_hash_stale", review_item["value"]["reasons"])
        self.assertTrue(any(
            event["kind"] == "gate_evidence_checked" and event.get("severity") == "error"
            for event in self.events.events))
        self.assertTrue(any(
            event["kind"] == "gate_review_checked" and event.get("severity") == "error"
            for event in self.events.events))

    def test_gate_fails_when_review_hash_is_stale(self):
        """代码未变但审查报告绑定的是别的代码：门禁拒绝。"""
        self._publish_ready(
            self._freeze_and_attest(), review_report={"code_hash": "0" * 64})
        gate = self._gate()
        self.assertFalse(gate["passed"])
        self.assertTrue(self._evidence_item(gate)["passed"])
        item = self._review_item(gate)
        self.assertFalse(item["passed"])
        self.assertIn("review_code_hash_stale", item["value"]["reasons"])

    def test_gate_fails_when_review_hash_missing(self):
        self._publish_ready(
            self._freeze_and_attest(), review_report={"code_hash": ""})
        gate = self._gate()
        self.assertFalse(gate["passed"])
        item = self._review_item(gate)
        self.assertFalse(item["passed"])
        self.assertIn("review_code_hash_missing", item["value"]["reasons"])

    def test_gate_fails_when_review_verdict_is_not_pass(self):
        self._publish_ready(
            self._freeze_and_attest(), review_report={"verdict": "reject"})
        gate = self._gate()
        self.assertFalse(gate["passed"])
        item = self._review_item(gate)
        self.assertFalse(item["passed"])
        self.assertIn("review_not_passed", item["value"]["reasons"])

    def test_lean_profile_skips_review_binding(self):
        """lean 裁剪 review 节点后，门禁不应要求审查结论（否则精简流程必然失败）。"""
        config = load_config(
            PROJECT_ROOT / "config", profile="lean",
            workspace=Path(self.tmp.name) / "ws_lean")
        self.assertNotIn("review", config.flow.nodes)
        self.config = config
        self.leader = Leader(config)
        self._publish_ready(self._freeze_and_attest())
        task = NodeTask.from_node(config.flow.node("acceptance"), iteration=1)
        self.leader._run_gate(self.ctx, task, {})
        gate = self.blackboard.get("acceptance_gate")
        self.assertFalse(any(
            item["id"] == REVIEW_CRITERION for item in gate["criteria"]))

    def test_gate_fails_when_embedded_attestation_diverges(self):
        attestation = self._freeze_and_attest()
        forged = json.loads(json.dumps(attestation))
        forged["signature"] = "0" * 64
        self._publish_ready(forged)
        gate = self._gate()
        self.assertFalse(gate["passed"])
        self.assertIn("test_report_attestation_mismatch", gate["evidence"]["reasons"])

    def test_gate_fails_when_attestation_file_missing(self):
        attestation = self._freeze_and_attest()
        (self.run_dir / "verification_attestation.json").unlink()
        self._publish_ready(attestation)
        gate = self._gate()
        self.assertFalse(gate["passed"])
        self.assertTrue(any(
            "verification_attestation_unreadable" in reason
            for reason in gate["evidence"]["reasons"]))


if __name__ == "__main__":
    unittest.main()
