"""单路径编排层的重复性对照（docs/50 §8.2、docs/53）。

legacy 循环删除后，原来「legacy vs langgraph」的对照失去意义；这里保留同样的
比对口径，改为**同一路径跑两次**：两次运行必须得到完全相同的节点序列、事件脚本、
回退记录、门禁裁决、产物版本与代码树 hash。它同时是「图驱动是否引入随机性」的回归。

两组必须用**独立 workspace**——经验库跨运行沉淀会让后跑的一组步数变少，
伪造出「改动造成差异」的假信号。

唯一允许不一致的是耗时（`test_report.summary.duration_ms`）以及由此派生的
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


def _run(workspace: Path):
    config = load_config(PROJECT_ROOT / "config", workspace=str(workspace))
    config.llm["provider"] = "offline"
    config.runtime["verbose"] = False
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


class OrchestrationDeterminismTests(unittest.TestCase):
    def test_two_runs_produce_the_same_script_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, first_dir = _run(Path(tmp) / "repeat_a")
            second, second_dir = _run(Path(tmp) / "repeat_b")

            # 运行结论与计数
            self.assertEqual(first.verdict, "pass", first.error)
            self.assertEqual(second.verdict, first.verdict, second.error)
            self.assertEqual(second.steps, first.steps)
            self.assertEqual(second.rollbacks, first.rollbacks)
            self.assertEqual(second.review_rejections, first.review_rejections)
            self.assertEqual(second.bugs_total, first.bugs_total)
            self.assertEqual(second.bugs_fixed, first.bugs_fixed)
            self.assertEqual(second.iterations, first.iterations)

            # 节点访问序列 / 事件脚本 / 回退
            self.assertEqual([e for e in _script(second_dir) if e[0] == "node_start"],
                             [e for e in _script(first_dir) if e[0] == "node_start"])
            self.assertEqual(_script(second_dir), _script(first_dir))
            self.assertEqual(_rollbacks(second_dir), _rollbacks(first_dir))
            self.assertGreaterEqual(len(_rollbacks(first_dir)), 1)   # 对照本身要覆盖回退环

            # 门禁裁决与产物
            first_gate, second_gate = _artifact("acceptance_gate", first_dir), _artifact("acceptance_gate", second_dir)
            self.assertEqual(second_gate["passed"], first_gate["passed"])
            self.assertEqual([item["id"] for item in second_gate.get("failed_criteria") or []],
                             [item["id"] for item in first_gate.get("failed_criteria") or []])
            self.assertEqual(_artifact_versions(second_dir), _artifact_versions(first_dir))
            first_spec = _artifact("requirement_spec", first_dir)
            second_spec = _artifact("requirement_spec", second_dir)
            self.assertEqual(len(second_spec["function_points"]), len(first_spec["function_points"]))
            first_tests = _artifact("test_report", first_dir)["summary"]
            second_tests = _artifact("test_report", second_dir)["summary"]
            # duration_ms 是耗时（docs/50 §8.2 明确允许不一致），其余计数必须逐项相等
            self.assertEqual({k: v for k, v in second_tests.items() if k != "duration_ms"},
                             {k: v for k, v in first_tests.items() if k != "duration_ms"})

            # 代码产物：文件树内容 hash 必须一致
            self.assertEqual(_tree_hash(second_dir / "code"), _tree_hash(first_dir / "code"))


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
