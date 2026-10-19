"""「取消上传」与「终止任务」的离线回归测试（docs/23）。

覆盖三层：
1. ``mas/leader.py`` 协作式取消 ``cancel_event``——节点边界退出、收尾态
   （canceled=true / verdict=fail / run_canceled 事件 / 不污染知识库）；
2. ``frontend/server.py`` 的 ``remove_upload``——uploads 目录边界校验、
   路径穿越拒绝、幂等；
3. ``frontend/server.py`` 的 ``cancel_run`` + ``RUNNING_TASKS`` 注册表语义，
   以及运行列表/详情对「已终止」状态的呈现。

真实 HTTP 端到端（起服务 → POST /api/run/cancel 与 /api/upload/remove）取证见 docs/23。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from frontend import server
from mas.config import load_config
from mas.leader import Leader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


class _NthCheckEvent:
    """确定性模拟「中途取消」：前 allow 次 ``is_set()`` 返回 False，之后恒 True。

    只实现 ``is_set()``——与 Leader 的取消检查契约（``cancel_event`` 鸭子类型）一致，
    不依赖线程时序，结果可复现。
    """

    def __init__(self, allow: int) -> None:
        self.allow = allow
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls > self.allow


class LeaderCancelTests(unittest.TestCase):
    """Leader 协作式取消：节点边界退出与收尾态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.workspace)
        self.config.llm["provider"] = "offline"  # 与 system.yaml 默认解耦，确定性跑通
        self.config.runtime["verbose"] = False

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _events(run_dir: Path) -> list[dict]:
        path = run_dir / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_cancel_after_three_nodes(self):
        """跑完 intake/solution/implement 后在第 4 个节点边界退出。"""
        summary = Leader(self.config).run(str(REQUIREMENT), cancel_event=_NthCheckEvent(allow=3))
        self.assertTrue(summary.canceled)
        self.assertEqual(summary.verdict, "fail")
        self.assertEqual(summary.steps, 3)

        run_dir = Path(summary.run_dir)
        data = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertTrue(data["canceled"])                    # 列表/详情状态呈现的数据来源
        self.assertTrue(data["metrics"]["canceled"])         # 指标体同样带标记

        events = self._events(run_dir)
        types = [e["type"] for e in events]
        self.assertIn("run_canceled", types)
        finished = [e for e in events if e["type"] == "run_finished"][-1]
        self.assertTrue(finished["payload"]["canceled"])

        # 半截运行不沉淀指标/经验：knowledge/metrics.jsonl 不产生
        self.assertFalse((self.workspace / "knowledge" / "metrics.jsonl").exists())

    def test_cancel_before_first_node(self):
        """信号预置：0 步退出，run_summary 仍落盘（面板可立即看到「已终止」）。"""
        event = threading.Event()
        event.set()
        summary = Leader(self.config).run(str(REQUIREMENT), cancel_event=event)
        self.assertTrue(summary.canceled)
        self.assertEqual(summary.steps, 0)
        self.assertTrue((Path(summary.run_dir) / "run_summary.json").exists())

    def test_without_cancel_event_unchanged(self):
        """回归：不传 cancel_event 时行为与改造前一致（正常跑完 + 指标落盘）。"""
        summary = Leader(self.config).run(str(REQUIREMENT))
        self.assertFalse(summary.canceled)
        self.assertEqual(summary.verdict, "pass", summary.error)
        self.assertTrue((self.workspace / "knowledge" / "metrics.jsonl").exists())


class RemoveUploadTests(unittest.TestCase):
    """取消上传：uploads 目录边界校验 + 幂等删除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        (self.workspace / "uploads").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_deletes_file(self):
        target = self.workspace / "uploads" / "abc123__req.md"
        target.write_text("content", encoding="utf-8")
        result = server.remove_upload(self.workspace, str(target))
        self.assertTrue(result["removed"])
        self.assertFalse(target.exists())

    def test_remove_is_idempotent(self):
        # 文件不存在（重复点击/并发取消）→ removed=False 而非报错
        result = server.remove_upload(self.workspace, str(self.workspace / "uploads" / "gone.md"))
        self.assertFalse(result["removed"])

    def test_remove_rejects_paths_outside_uploads(self):
        outside = self.workspace / "runs" / "keep.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            server.remove_upload(self.workspace, str(outside))
        self.assertTrue(outside.exists())        # 越界文件必须原样保留

    def test_remove_rejects_traversal(self):
        outside = self.workspace / "runs" / "keep.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("keep", encoding="utf-8")
        # uploads/../runs/keep.md 解析后越界 → 拒绝
        with self.assertRaises(ValueError):
            server.remove_upload(self.workspace, str(self.workspace / "uploads" / ".." / "runs" / "keep.md"))
        self.assertTrue(outside.exists())


class CancelRunTests(unittest.TestCase):
    """终止任务：RUNNING_TASKS 注册表语义。"""

    def test_cancel_run_sets_signal(self):
        event = threading.Event()
        server.RUNNING_TASKS["20261017-test-0001"] = event
        try:
            result = server.cancel_run("20261017-test-0001")
            self.assertTrue(event.is_set())
            self.assertEqual(result, {"run_id": "20261017-test-0001", "status": "canceling"})
        finally:
            server.RUNNING_TASKS.pop("20261017-test-0001", None)

    def test_cancel_run_unknown_raises(self):
        # 已完成 / CLI 启动的运行不在注册表：如实报错，不伪造送达
        with self.assertRaises(ValueError):
            server.cancel_run("20261017-not-running")


class RunStatusPresentationTests(unittest.TestCase):
    """「已终止」在运行列表/详情中的状态呈现（与已通过/未通过区分）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        run_dir = self.workspace / "runs" / "20261017-test-0002"
        run_dir.mkdir(parents=True)
        (run_dir / "run_summary.json").write_text(
            json.dumps({
                "run_id": "20261017-test-0002", "requirement_title": "t", "verdict": "fail",
                "steps": 3, "rollbacks": 0, "review_rejections": 0, "bugs_total": 0,
                "bugs_fixed": 0, "canceled": True,
                "metrics": {"ts": "2026-10-17T00:00:00+08:00", "duration_s": 1.0, "canceled": True},
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_marks_terminated(self):
        runs = server.load_runs(self.workspace)
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["canceled"])
        self.assertEqual(runs[0]["status"], "已终止")
        self.assertTrue(runs[0]["finished"])     # 已终止属终态：前端据此停止轮询

    def test_detail_carries_run_id_and_canceled(self):
        detail = server.load_run_detail(self.workspace, "20261017-test-0002")
        self.assertEqual(detail["run_id"], "20261017-test-0002")
        self.assertTrue(detail["canceled"])
        self.assertEqual(detail["status"], "已终止")


class FrontendStaticConsistencyTests(unittest.TestCase):
    """前端静态一致性：index.html 的控件 id 与 app.js 的选择器/接口调用对应（防拼写漂移）。"""

    def setUp(self):
        self.html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.js = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    def test_cancel_upload_wiring(self):
        self.assertIn('id="cancel-upload"', self.html)          # 按钮存在
        self.assertIn('$("#cancel-upload")', self.js)           # 显隐/绑定引用该 id
        self.assertIn('"/api/upload/remove"', self.js)          # 指向取消上传接口
        self.assertIn("syncUploadUi", self.js)                  # 显隐同步函数被接入

    def test_terminate_run_wiring(self):
        self.assertIn('id="cancel-run"', self.js)               # 终止按钮（详情区动态渲染）
        self.assertIn('closest("#cancel-run")', self.js)        # 点击委托
        self.assertIn('"/api/run/cancel"', self.js)             # 指向终止接口
        self.assertIn("cancelingRuns", self.js)                 # 「终止中…」置灰状态集合


if __name__ == "__main__":
    unittest.main()
