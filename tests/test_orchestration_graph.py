"""LangGraph 编排层（docs/50）的离线回归：图结构、router 纯函数性、取消、续跑。

legacy 循环的既有行为由其余 53 个测试文件覆盖（system.yaml 默认
``runtime.orchestrator: legacy``）；本文件只测新编排路径里**不需要双跑**的确定性部分，
与 legacy 的逐项对照见 test_orchestration_ab.py。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mas.config import _validate, load_config
from mas.errors import ConfigError, RunCanceledError
from mas.leader import Leader
from mas.llm import LLMClient
from mas.orchestration.checkpoint import graph_db_path
from mas.orchestration.graph import build_graph, route_from_state
from mas.orchestration.nodes import GraphDeps

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


def _config(workspace: Path, orchestrator: str = "langgraph"):
    config = load_config(PROJECT_ROOT / "config", workspace=str(workspace))
    config.llm["provider"] = "offline"          # 确定性：不依赖真实端点
    config.runtime["verbose"] = False
    config.runtime["orchestrator"] = orchestrator
    return config


def _events(run_dir: Path):
    return [json.loads(line) for line in
            (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


class _NthCheckEvent:
    """前 allow 次 ``is_set()`` 返回 False，之后恒 True（与 test_cancel.py 同款）。"""

    def __init__(self, allow: int) -> None:
        self.allow = allow
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls > self.allow


class GraphShapeTests(unittest.TestCase):
    """结构映射：flow.yaml 的节点与 pass/fail 边必须一格不差地进图。"""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config(PROJECT_ROOT / "config")
        deps = GraphDeps(
            leader=SimpleNamespace(config=cls.config), ctx=None, agents={}, registry=None,
            cancel_event=None, run_id="shape", checkpoint_path=Path("unused.json"),
            requirement_title="shape", requirement_sha256="0" * 64, project_docs_text="",
            started_at="", max_total_rollbacks=8,
        )
        cls.graph = build_graph(deps).compile().get_graph()

    def test_nodes_match_flow(self):
        drawn = {name for name in self.graph.nodes if not name.startswith("__")}
        self.assertEqual(drawn, set(self.config.flow.nodes))

    def test_conditional_edges_match_transitions(self):
        edges = {(edge.source, edge.target) for edge in self.graph.edges}
        for node_id, node in self.config.flow.nodes.items():
            for branch in ("pass", "fail"):
                target = node.target(branch)
                drawn = "__end__" if target in self.config.flow.terminal else target
                self.assertIn((node_id, drawn), edges, f"{node_id}.{branch} → {target} 未进图")
        # 回退环（docs/50 §0 的核心诉求：review fail → implement、acceptance fail → optimize）
        self.assertIn(("review", "implement"), edges)
        self.assertIn(("acceptance", "optimize"), edges)
        self.assertIn(("test", "optimize"), edges)
        self.assertIn(("__start__", self.config.flow.entry), edges)

    def test_router_reads_state_only(self):
        state = {"next_node": "test", "steps": 1, "max_steps": 40}
        before = dict(state)
        self.assertEqual(route_from_state(state), "test")
        self.assertEqual(state, before)                 # 不改 state：router 无副作用
        self.assertEqual(route_from_state({"next_node": ""}), "halt")

    def test_router_stops_at_max_steps(self):
        """与 legacy 的 ``while ... and steps < max_steps`` 同一退出点。"""
        self.assertEqual(route_from_state({"next_node": "test", "steps": 39, "max_steps": 40}), "test")
        self.assertEqual(route_from_state({"next_node": "test", "steps": 40, "max_steps": 40}), "halt")

    def test_invalid_orchestrator_rejected(self):
        config = load_config(PROJECT_ROOT / "config")
        config.runtime["orchestrator"] = "langrfaph"
        with self.assertRaises(ConfigError) as caught:
            _validate(config)
        self.assertIn("runtime.orchestrator", str(caught.exception))


class GraphRunTests(unittest.TestCase):
    """跑通 + 取消 + 续跑：三条最容易在换编排后被框架吞掉的契约。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config = _config(self.workspace)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_reaches_pass_and_writes_graph_checkpoint(self):
        summary = Leader(self.config).run(str(REQUIREMENT))
        self.assertEqual(summary.verdict, "pass", summary.error)
        run_dir = Path(summary.run_dir)
        self.assertEqual(summary.steps, len([e for e in _events(run_dir) if e["type"] == "node_start"]))
        self.assertTrue(graph_db_path(run_dir).exists())
        checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["status"], "completed")
        self.assertEqual(checkpoint["iterations"], summary.iterations)

    def test_cancel_at_node_boundary(self):
        """取消信号的节点边界检查在 LangGraph 路径同样生效（步数不虚增）。"""
        summary = Leader(self.config).run(str(REQUIREMENT), cancel_event=_NthCheckEvent(allow=3))
        self.assertTrue(summary.canceled)
        self.assertEqual(summary.verdict, "fail")
        self.assertEqual(summary.steps, 3)
        types = [e["type"] for e in _events(Path(summary.run_dir))]
        self.assertIn("run_canceled", types)
        self.assertNotIn("node_interrupted", types)      # 边界取消不产生「节点内中断」事件
        self.assertFalse((self.workspace / "knowledge" / "metrics.jsonl").exists())

    def test_cancel_inside_node_propagates_through_graph(self):
        """RunCanceledError 继承 BaseException：必须穿透 LangGraph 直到取消收尾。"""
        original = LLMClient.generate
        calls = {"n": 0}

        def generate_then_cancel(self_, task_kind, bundle, context, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RunCanceledError("用户手动终止（测试注入）")
            return original(self_, task_kind, bundle, context, **kwargs)

        with mock.patch.object(LLMClient, "generate", generate_then_cancel):
            summary = Leader(self.config).run(str(REQUIREMENT))
        self.assertTrue(summary.canceled)
        self.assertEqual(summary.verdict, "fail")
        self.assertEqual(calls["n"], 2)                  # 中断后不再有第 3 次 LLM 调用
        types = [e["type"] for e in _events(Path(summary.run_dir))]
        self.assertIn("node_interrupted", types)
        self.assertIn("run_canceled", types)
        self.assertNotIn("node_error", types)

    def test_resume_from_graph_checkpoint(self):
        """崩溃后续跑：从 graph.sqlite 恢复，已完成节点不重跑。"""
        original = Leader._act_with_retry
        calls = {"n": 0}

        def crashing(leader_self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated crash during implement")
            return original(leader_self, *args, **kwargs)

        with mock.patch.object(Leader, "_act_with_retry", crashing):
            with self.assertRaises(RuntimeError):
                Leader(self.config).run(str(REQUIREMENT))
        run_dir = next((self.workspace / "runs").iterdir())
        checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["status"], "running")
        self.assertEqual(checkpoint["next_node"], "implement")
        self.assertEqual(checkpoint["steps"], 2)
        self.assertTrue(graph_db_path(run_dir).exists())

        summary = Leader(self.config).run(str(REQUIREMENT), run_id=run_dir.name, resume=True)
        self.assertEqual(summary.verdict, "pass", summary.error)
        events = _events(run_dir)
        self.assertIn("run_resumed", [e["type"] for e in events])
        starts = [e["node"] for e in events if e["type"] == "node_start"]
        self.assertEqual(starts.count("intake"), 1)      # 已完成节点不重跑
        self.assertEqual(starts.count("solution"), 1)
        self.assertEqual(json.loads((run_dir / "checkpoint.json").read_text(
            encoding="utf-8"))["status"], "completed")


    def test_resume_from_json_only_checkpoint(self):
        """legacy 跑挂的运行可以用 langgraph 续跑：图入口按检查点 next_node 落位。"""
        original = Leader._act_with_retry
        calls = {"n": 0}

        def crashing(leader_self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated crash during implement")
            return original(leader_self, *args, **kwargs)

        with mock.patch.object(Leader, "_act_with_retry", crashing):
            with self.assertRaises(RuntimeError):
                Leader(_config(self.workspace, "legacy")).run(str(REQUIREMENT))
        run_dir = next((self.workspace / "runs").iterdir())
        self.assertFalse(graph_db_path(run_dir).exists())      # legacy 只写 json

        summary = Leader(self.config).run(str(REQUIREMENT), run_id=run_dir.name, resume=True)
        self.assertEqual(summary.verdict, "pass", summary.error)
        events = _events(run_dir)
        resumed = [e for e in events if e["type"] == "run_resumed"][-1]
        self.assertEqual(resumed["payload"]["resumed_from"], "implement")
        starts = [e["node"] for e in events if e["type"] == "node_start"]
        self.assertEqual(starts.count("intake"), 1)


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
