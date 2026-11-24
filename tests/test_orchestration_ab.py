"""legacy 与 langgraph 两条编排路径的端到端对照（docs/50 §8.2）。

对照口径：节点访问序列、回退事件、门禁结论、产物版本、代码树 hash、用例执行结果。
两组必须用**独立 workspace**——经验库跨运行沉淀会让后跑的一组步数变少，
伪造出「改动造成差异」的假信号。

唯一允许不一致的是耗时（LangGraph 有额外的检查点序列化开销），以及由此派生的
run_id / 时间戳字段——因此这里比对的是去掉波动字段后的执行脚本与产物摘要。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mas.config import load_config
from mas.leader import Leader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


def _run(orchestrator: str, workspace: Path):
    config = load_config(PROJECT_ROOT / "config", workspace=str(workspace))
    config.llm["provider"] = "offline"
    config.runtime["verbose"] = False
    config.runtime["orchestrator"] = orchestrator
    summary = Leader(config).run(str(REQUIREMENT))
    return summary, Path(summary.run_dir)


def _events(run_dir: Path):
    return [json.loads(line) for line in
            (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def _script(run_dir: Path):
    """事件脚本：类型 + 节点 + 轮次，去掉时间戳/run_id 等波动字段。"""
    return [(event["type"], event.get("node") or "", event.get("iteration")) for event in _events(run_dir)]


def _rollbacks(run_dir: Path):
    return [(event["payload"]["from"], event["payload"]["to"], event["payload"]["attempt"],
             event["payload"]["max_attempts"])
            for event in _events(run_dir) if event["type"] == "rollback"]


def _artifact(name: str, run_dir: Path):
    return json.loads((run_dir / "artifacts" / name / "latest.json").read_text(encoding="utf-8"))


def _artifact_versions(run_dir: Path):
    return {path.parent.name: sorted(item.name for item in path.parent.glob("v*.json"))
            for path in (run_dir / "artifacts").glob("*/latest.json")}


def _tree_hash(root: Path):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("utf-8"))
    return digest.hexdigest()


class OrchestrationABTests(unittest.TestCase):
    def test_legacy_and_langgraph_produce_the_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy, legacy_dir = _run("legacy", Path(tmp) / "ab_legacy")
            graph, graph_dir = _run("langgraph", Path(tmp) / "ab_graph")

            # 运行结论与计数
            self.assertEqual(legacy.verdict, "pass", legacy.error)
            self.assertEqual(graph.verdict, legacy.verdict, graph.error)
            self.assertEqual(graph.steps, legacy.steps)
            self.assertEqual(graph.rollbacks, legacy.rollbacks)
            self.assertEqual(graph.review_rejections, legacy.review_rejections)
            self.assertEqual(graph.bugs_total, legacy.bugs_total)
            self.assertEqual(graph.bugs_fixed, legacy.bugs_fixed)
            self.assertEqual(graph.iterations, legacy.iterations)

            # 节点访问序列 / 事件脚本 / 回退
            self.assertEqual([e for e in _script(graph_dir) if e[0] == "node_start"],
                             [e for e in _script(legacy_dir) if e[0] == "node_start"])
            self.assertEqual(_script(graph_dir), _script(legacy_dir))
            self.assertEqual(_rollbacks(graph_dir), _rollbacks(legacy_dir))
            self.assertGreaterEqual(len(_rollbacks(legacy_dir)), 1)   # 对照本身要覆盖回退环

            # 门禁裁决与产物
            legacy_gate, graph_gate = _artifact("acceptance_gate", legacy_dir), _artifact("acceptance_gate", graph_dir)
            self.assertEqual(graph_gate["passed"], legacy_gate["passed"])
            self.assertEqual([item["id"] for item in graph_gate.get("failed_criteria") or []],
                             [item["id"] for item in legacy_gate.get("failed_criteria") or []])
            self.assertEqual(_artifact_versions(graph_dir), _artifact_versions(legacy_dir))
            legacy_spec = _artifact("requirement_spec", legacy_dir)
            graph_spec = _artifact("requirement_spec", graph_dir)
            self.assertEqual(len(graph_spec["function_points"]), len(legacy_spec["function_points"]))
            legacy_tests = _artifact("test_report", legacy_dir)["summary"]
            graph_tests = _artifact("test_report", graph_dir)["summary"]
            # duration_ms 是耗时（docs/50 §8.2 明确允许不一致），其余计数必须逐项相等
            self.assertEqual({k: v for k, v in graph_tests.items() if k != "duration_ms"},
                             {k: v for k, v in legacy_tests.items() if k != "duration_ms"})

            # 代码产物：文件树内容 hash 必须一致
            self.assertEqual(_tree_hash(graph_dir / "code"), _tree_hash(legacy_dir / "code"))


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
