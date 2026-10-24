"""门禁最小加固的回归测试。

覆盖（对应 docs/39 续篇）：
1. 产物发布策略：角色 ACL、leader-only 产物、schema 类型校验；
2. 报告只渲染 gate：模型自报 verdict 无效、gate 缺失/run_id 不符直接失败；
3. report ↔ gate 对账：run_id/verdict 任一不一致即判失败；
4. 运行态声明产物校验：节点自称通过但没发布声明产物 → 节点失败；
5. release 只允许 full/strict；lean 在 release 模式启动即拒绝；
6. 裁决记录携带 run_id/profile/gate_config_hash；
7. 端到端（离线 Provider）：gate/report/run_summary 三处口径一致。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.reporter import ReporterAgent
from mas.artifacts import ArtifactPolicy, gate_config_hash
from mas.blackboard import Blackboard
from mas.config import load_config
from mas.errors import ConfigError
from mas.events import EventLog
from mas.leader import Leader
from mas.llm import Generation
from mas.schemas import AgentResult, NodeTask
from mas.verifier import require_release_profile

from tests.test_gate_evidence import TEST_SECRET, _env

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


def _config(root: Path, profile: str | None = None):
    config = load_config(PROJECT_ROOT / "config", profile=profile,
                         workspace=root / f"ws-{profile or 'base'}")
    config.llm["provider"] = "offline"
    config.runtime["verbose"] = False
    return config


def _gate(*, run_id: str = "run-fixed", passed: bool = True) -> dict:
    return {
        "node": "acceptance",
        "run_id": run_id,
        "profile": "base",
        "gate_config_hash": "a" * 64,
        "passed": passed,
        "criteria": [],
        "failed_criteria": [],
        "signoff": {"verdict": "pass" if passed else "reject", "opinion": ""},
        "evaluated_at": "2026-10-24T00:00:00+08:00",
    }


class ArtifactPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = _config(Path(self.tmp.name))
        self.policy = ArtifactPolicy.from_config(self.config)

    def test_role_acl_denies_foreign_publish(self):
        with self.assertRaises(ConfigError) as caught:
            self.policy.check("test_report", {"summary": {}, "coverage": {}}, "developer")
        self.assertIn("无权发布", str(caught.exception))
        self.policy.check("test_report", {"summary": {}, "coverage": {}}, "tester")

    def test_undeclared_artifact_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self.policy.check("made_up_artifact", {"x": 1}, "developer")
        self.assertIn("未在任何角色/节点 outputs 中声明", str(caught.exception))

    def test_gate_is_leader_only_even_if_declared_by_flow(self):
        # flow.yaml 的 acceptance 节点 outputs 含 acceptance_gate，但裁决记录只允许 leader
        with self.assertRaises(ConfigError) as caught:
            self.policy.check("acceptance_gate", _gate(), "reviewer")
        self.assertIn("只允许 leader", str(caught.exception))
        self.policy.check("acceptance_gate", _gate(), "leader")

    def test_schema_checks_types_and_missing_fields(self):
        with self.assertRaises(ConfigError) as caught:
            self.policy.check("acceptance_report",
                              {"run_id": "r", "verdict": "pass"}, "reporter")
        self.assertIn("gate_passed", str(caught.exception))
        # bool 不能被当作 int 通过
        with self.assertRaises(ConfigError):
            self.policy.check("bug_reports", {"open": True}, "tester")
        self.policy.check("bug_reports", {"open": 0}, "tester")

    def test_blackboard_without_policy_keeps_fixture_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Blackboard(Path(tmp) / "artifacts", run_id="fixture")
            board.publish("anything", {"from": "fixture"}, producer="fixture",
                          node="n", iteration=1)
            self.assertEqual(board.get("anything"), {"from": "fixture"})


class ReportGateBindingTests(unittest.TestCase):
    """报告节点只渲染 acceptance_gate；模型自报的 verdict 不生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        self.config.roles["reporter"].policies["include_learning_report"] = False
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "run-fixed")
        self.board = Blackboard(self.run_dir / "artifacts", self.events, "run-fixed",
                                policy=ArtifactPolicy.from_config(self.config))
        self.ctx = SimpleNamespace(
            config=self.config, run_id="run-fixed", run_dir=self.run_dir,
            events=self.events, blackboard=self.board, experience=None,
        )
        self.agent = ReporterAgent(self.ctx)
        self.task = NodeTask(node_id="report", title="报告", role="reporter",
                             kind="task", iteration=1, outputs=["acceptance_report",
                                                                "evidence_index"])

    def _apply(self, gate: dict, llm_verdict: str):
        generation = Generation(data={
            "markdown": "# 验收报告\n\n模型正文。\n",
            "json": {"verdict": llm_verdict, "test_summary": {}, "bugs": {}},
        })
        result = AgentResult()
        self.agent.apply(self.task, generation, SimpleNamespace(found={}),
                         {"facts": {"gate": gate}}, result)
        return result

    def test_gate_pass_overrides_llm_fail(self):
        result = self._apply(_gate(passed=True), "fail")
        self.assertEqual(result.status, "pass", result.error)
        report = self.board.get("acceptance_report")
        self.assertEqual(report["run_id"], "run-fixed")
        self.assertEqual(report["verdict"], "pass")
        self.assertTrue(report["gate_passed"])
        self.assertEqual(report["verdict_source"], "acceptance_gate")
        self.assertEqual(report["summary"]["verdict"], "pass")
        self.assertEqual(report["summary"]["llm_verdict"], "fail")
        markdown = (self.run_dir / "reports" / "acceptance_report.md").read_text(encoding="utf-8")
        self.assertIn("验收结论：**pass**", markdown)

    def test_gate_fail_overrides_llm_pass(self):
        result = self._apply(_gate(passed=False), "pass")
        self.assertEqual(result.status, "pass", result.error)   # 报告节点本身仍可完成
        report = self.board.get("acceptance_report")
        self.assertEqual(report["verdict"], "fail")
        self.assertFalse(report["gate_passed"])
        self.assertEqual(report["summary"]["llm_verdict"], "pass")

    def test_missing_gate_fails_report_node(self):
        result = self._apply({}, "pass")
        self.assertEqual(result.status, "fail")
        self.assertIn("缺少 acceptance_gate", result.error)
        self.assertIsNone(self.board.get("acceptance_report"))

    def test_foreign_run_gate_fails_report_node(self):
        result = self._apply(_gate(run_id="another-run"), "pass")
        self.assertEqual(result.status, "fail")
        self.assertIn("run_id 不匹配", result.error)


class ReconcileTests(unittest.TestCase):
    def test_consistent(self):
        self.assertEqual(
            Leader._reconcile_report_gate(
                _gate(), {"run_id": "run-fixed", "verdict": "pass"}, "run-fixed"),
            [],
        )

    def test_gate_missing(self):
        self.assertEqual(
            Leader._reconcile_report_gate(
                {}, {"run_id": "run-fixed", "verdict": "pass"}, "run-fixed"),
            ["gate_missing"],
        )

    def test_report_missing(self):
        self.assertIn(
            "report_missing",
            Leader._reconcile_report_gate(_gate(), {}, "run-fixed"),
        )

    def test_run_id_mismatch(self):
        reasons = Leader._reconcile_report_gate(
            _gate(run_id="run-a"),
            {"run_id": "run-b", "verdict": "pass"},
            "run-fixed",
        )
        self.assertTrue(any(item.startswith("gate_run_id_mismatch") for item in reasons))
        self.assertTrue(any(item.startswith("report_run_id_mismatch") for item in reasons))
        self.assertTrue(any(item.startswith("report_gate_run_id_mismatch") for item in reasons))

    def test_verdict_mismatch(self):
        reasons = Leader._reconcile_report_gate(
            _gate(passed=False),
            {"run_id": "run-fixed", "verdict": "pass"},
            "run-fixed",
        )
        self.assertIn("report_verdict_mismatch:pass!=fail", reasons)


class DeclaredOutputTests(unittest.TestCase):
    """节点自称 pass 但没把声明产物发布到黑板 → 运行态判失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        self.leader = Leader(self.config)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "run-fixed")
        self.board = Blackboard(self.run_dir / "artifacts", self.events, "run-fixed",
                                policy=self.leader.artifact_policy)
        self.ctx = SimpleNamespace(events=self.events, blackboard=self.board, run_state={})
        self.task = NodeTask.from_node(self.config.flow.node("implement"), iteration=1)

    def test_pass_without_publishing_is_rejected(self):
        class _Liar:
            def act(self, task):
                return AgentResult(status="pass", summary="我完成了")

        result, status = self.leader._act_with_retry(
            self.ctx, self.task, {"developer": _Liar()}, self.config.flow.node("implement"))
        self.assertEqual(status, "fail")
        self.assertIn("缺少声明产物", result.error)
        missing = [event for event in self.events.events if event.type == "artifact_missing"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(set(missing[0].payload["missing"]),
                         {"code_manifest", "implementation_summary"})

    def test_pass_with_declared_artifacts_succeeds(self):
        board = self.board

        class _Honest:
            def act(self, task):
                board.publish("code_manifest", {"files": []}, producer="developer",
                              node="implement", iteration=task.iteration)
                board.publish("implementation_summary", {"files": []}, producer="developer",
                              node="implement", iteration=task.iteration)
                return AgentResult(status="pass", summary="已发布")

        result, status = self.leader._act_with_retry(
            self.ctx, self.task, {"developer": _Honest()}, self.config.flow.node("implement"))
        self.assertEqual(status, "pass", result.error)


class ReleaseProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lean = _config(self.root, "lean")
        self.strict = _config(self.root, "strict")

    def test_lean_rejected_in_release(self):
        with _env({"MAS_ENV": "release", "MAS_VERIFIER_SECRET": TEST_SECRET},
                  remove=("MAS_VERIFIER_RELEASE",)):
            with self.assertRaises(ConfigError) as caught:
                require_release_profile(self.lean)
            self.assertIn("release 模式不允许", str(caught.exception))
            with self.assertRaises(ConfigError):
                Leader(self.lean)

    def test_full_and_strict_allowed_in_release(self):
        with _env({"MAS_ENV": "release", "MAS_VERIFIER_SECRET": TEST_SECRET},
                  remove=("MAS_VERIFIER_RELEASE",)):
            require_release_profile(self.strict)
            Leader(self.strict)
            base = _config(self.root)
            require_release_profile(base)
            Leader(base)

    def test_lean_allowed_in_dev(self):
        with _env(remove=("MAS_ENV", "MAS_VERIFIER_RELEASE", "MAS_VERIFIER_SECRET")):
            require_release_profile(self.lean)
            Leader(self.lean)


class VerdictRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        self.leader = Leader(self.config)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "run-fixed")
        self.board = Blackboard(self.run_dir / "artifacts", self.events, "run-fixed",
                                policy=self.leader.artifact_policy)

    def test_forced_gate_records_profile_and_hash(self):
        self.leader._publish_forced_gate(self.board, "回退次数耗尽", "次数用尽",
                                         3, run_id="run-fixed")
        gate = self.board.get("acceptance_gate")
        self.assertEqual(gate["run_id"], "run-fixed")
        self.assertEqual(gate["profile"], "base")
        self.assertEqual(len(gate["gate_config_hash"]), 64)
        self.assertFalse(gate["passed"])

    def test_gate_config_hash_is_deterministic_and_profile_sensitive(self):
        node = self.config.flow.node("acceptance")
        first = gate_config_hash(node, self.config)
        second = gate_config_hash(node, self.config)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        lean = _config(self.root, "lean")
        self.assertNotEqual(first, gate_config_hash(lean.flow.node("acceptance"), lean))


class EndToEndVerdictConsistencyTests(unittest.TestCase):
    """离线 Provider 跑通一次：gate / report / run_summary 口径必须一致。"""

    def test_gate_report_summary_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            summary = Leader(config).run(str(REQUIREMENT))
            self.assertEqual(summary.verdict, "pass", summary.error)
            run_dir = Path(summary.run_dir)
            gate = json.loads(
                (run_dir / "artifacts" / "acceptance_gate" / "latest.json").read_text(encoding="utf-8"))
            report = json.loads(
                (run_dir / "artifacts" / "acceptance_report" / "latest.json").read_text(encoding="utf-8"))
            metrics = summary.metrics

            self.assertEqual(gate["run_id"], summary.run_id)
            self.assertEqual(gate["profile"], "base")
            self.assertEqual(len(gate["gate_config_hash"]), 64)
            self.assertEqual(report["run_id"], gate["run_id"])
            self.assertEqual(report["verdict"], "pass" if gate["passed"] else "fail")
            self.assertTrue(report["gate_passed"])
            self.assertEqual(report["verdict_source"], "acceptance_gate")
            self.assertEqual(metrics["gate_config_hash"], gate["gate_config_hash"])

            markdown = (run_dir / "reports" / "acceptance_report.md").read_text(encoding="utf-8")
            self.assertTrue(markdown.startswith("> 验收结论：**pass**"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
