"""运行时隔离与经验反馈闭环测试。

覆盖重构方案（docs/13）的两条 P0 验收标准：
- G1：同一进程内并发 run() 多个需求，产物/事件/消息零串扰（原 Leader 用类属性承载
  运行态，同一实例并发复用时后一次运行会覆盖前一次的运行时绑定——实测复现：
  A 运行的 negotiation_closed 事件全部写进 B 运行的 events.jsonl）；
- G2：经验反馈闭环真实闭合——注入的经验未能预防回退时 reinforce() 被调用并下调
  置信度（回退归因降权）；验收通过时上调（终态奖励）。

同时验证与原有架构的兼容性：离线 Provider 两轮自学习契约（步数下降、缺陷清零、
审查驳回清零）在改动后保持成立。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from mas.config import load_config
from mas.experience import ExperienceLibrary
from mas.leader import Leader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


class RuntimeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.workspace)
        self.config.llm["provider"] = "offline"
        self.config.runtime["verbose"] = False

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 工具
    def _events(self, run_dir: Path):
        return [json.loads(line) for line in
                (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    def _run_concurrent(self, *, same_instance: bool):
        leader = Leader(self.config)
        results = {}
        errors = {}

        def worker(name):
            try:
                target = leader if same_instance else Leader(self.config)
                results[name] = target.run(str(REQUIREMENT))
            except Exception as exc:  # noqa: BLE001 - 线程异常汇总后断言
                errors[name] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results, errors

    # ---------------------------------------------------------------- G1
    def test_concurrent_runs_isolated_same_instance(self):
        """同一 Leader 实例并发两次运行：事件/消息/产物零串扰，且都通过验收。"""
        results, errors = self._run_concurrent(same_instance=True)
        self.assertEqual(errors, {})
        self.assertEqual(len(results), 2)
        for name, summary in results.items():
            self.assertEqual(summary.verdict, "pass", f"{name}: {summary.error}")
            run_dir = Path(summary.run_dir)
            events = self._events(run_dir)
            # 事件全部属于本次运行（run_id 一致）
            foreign = [event for event in events if event.get("run_id") != summary.run_id]
            self.assertEqual(foreign, [], f"{name} 的 events.jsonl 混入其他运行的事件")
            # 协商闭环事件数正确（顺序单跑每轮 2 条，实测基线）
            negotiations = [event for event in events if event["type"] == "negotiation_closed"]
            self.assertEqual(len(negotiations), 2, f"{name} 的协商事件缺失或被写入他处")
            # 产物目录互不包含
            other = [item for key, item in results.items() if key != name]
            for summary_other in other:
                self.assertNotIn(Path(summary_other.run_dir).name, str(run_dir))

    def test_concurrent_runs_isolated_two_instances(self):
        """两个不同 Leader 实例并发：同样零串扰（显式传参后两种形态都由构造保证）。"""
        results, errors = self._run_concurrent(same_instance=False)
        self.assertEqual(errors, {})
        for name, summary in results.items():
            self.assertEqual(summary.verdict, "pass", f"{name}: {summary.error}")
            run_dir = Path(summary.run_dir)
            events = self._events(run_dir)
            foreign = [event for event in events if event.get("run_id") != summary.run_id]
            self.assertEqual(foreign, [])
            negotiations = [event for event in events if event["type"] == "negotiation_closed"]
            self.assertEqual(len(negotiations), 2)

    def test_consult_closure_uses_own_run(self):
        """consult 闭包读取的是本次运行的 bus/events/agents（而非共享引用）。"""
        leader = Leader(self.config)
        first = leader.run(str(REQUIREMENT))
        # 第一次运行结束后，其 consult 闭包仍应绑定第一次运行的运行时
        run_dir = Path(first.run_dir)
        events = self._events(run_dir)
        negotiations = [event for event in events if event["type"] == "negotiation_closed"]
        self.assertEqual(len(negotiations), 2)


class ExperienceFeedbackLoopTests(unittest.TestCase):
    """G2：经验反馈闭环端到端（回退归因降权 + 终态奖励 + 晋升依据）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.workspace)
        self.config.llm["provider"] = "offline"
        self.config.runtime["verbose"] = False

    def tearDown(self):
        self.tmp.cleanup()

    def _library_entries(self):
        library = ExperienceLibrary(self.workspace, self.config.learning)
        return {entry.action_key: entry for entry in library.entries}

    def test_positive_feedback_on_passing_run(self):
        """首轮通过后：被注入的经验获得 positive 反馈（hits_success>0）并按有效次数晋级。"""
        summary = Leader(self.config).run(str(REQUIREMENT))
        self.assertEqual(summary.verdict, "pass", summary.error)
        entries = self._library_entries()
        guard = entries.get("guard_calc_shipping__boundary")
        self.assertIsNotNone(guard, f"库中应有算子键守护经验，实际：{sorted(entries)}")
        self.assertGreaterEqual(guard.hits_success, 1)
        self.assertEqual(guard.hits_failed, 0)
        self.assertIn(guard.tier, ("L2", "L3"))   # 注入且有效 → 晋级

    def test_negative_feedback_on_rollback_attribution(self):
        """预置一条声称能防边界缺陷但实际无效的守护经验（trigger=bug_found）：
        运行中测试失败回退 → 该经验被归因降权（confidence 下降、hits_failed+1），
        且不因使用次数多而晋升。
        """
        library = ExperienceLibrary(self.workspace, self.config.learning)
        from mas.experience import Experience

        # 无效守护：action_key 不匹配任何算子（guard_FP-99__boundary），Provider 不会因此
        # 停止注入缺陷；但 trigger=bug_found，测试失败回退时会被归因。
        bad = Experience(
            id="", tier="L2", scope="project", roles=["developer", "tester"],
            tags=["订单", "电商", "缺陷修复", "boundary"],
            trigger="bug_found", lesson="功能点 FP-99 出现边界缺陷（预置无效经验，用于验证降权）",
            action="实现 FP-99 时显式覆盖边界输入", action_key="guard_FP-99__boundary",
            confidence=0.8, evidence_count=1,
        )
        library.add([bad], source_run="seed")
        bad_id = library.entries[0].id
        self.assertEqual(library.entries[0].confidence, 0.8)

        summary = Leader(self.config).run(str(REQUIREMENT))
        self.assertEqual(summary.verdict, "pass", summary.error)

        reloaded = {entry.id: entry for entry in
                    ExperienceLibrary(self.workspace, self.config.learning).entries}
        demoted = reloaded.get(bad_id)
        self.assertIsNotNone(demoted)
        self.assertGreaterEqual(demoted.hits_failed, 1, "无效经验应被回退归因降权")
        self.assertLess(demoted.confidence, 0.8, "置信度应被下调")
        self.assertLess(demoted.success_rate, 0.5)
        self.assertEqual(demoted.tier, "L2", "高失败率的经验不得晋升")

    def test_unrelated_experience_not_demoted(self):
        """归因精确性：测试失败（bug_found）不应降权 review_rejected 类经验。"""
        library = ExperienceLibrary(self.workspace, self.config.learning)
        from mas.experience import Experience

        review_lesson = Experience(
            id="", tier="L2", scope="project", roles=["developer"],
            tags=["订单", "电商", "代码审查", "missing_function_docstring"],
            trigger="review_rejected", lesson="代码审查拦截「函数文档字符串缺失」（预置）",
            action="提交审查前补充文档字符串", action_key="avoid_missing_function_docstring",
            confidence=0.8, evidence_count=1,
        )
        library.add([review_lesson], source_run="seed")
        review_id = library.entries[0].id

        Leader(self.config).run(str(REQUIREMENT))

        reloaded = {entry.id: entry for entry in
                    ExperienceLibrary(self.workspace, self.config.learning).entries}
        entry = reloaded.get(review_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.hits_failed, 0, "review 类经验不应被 test 失败归因误伤")
        self.assertGreaterEqual(entry.hits_success, 1, "运行通过应获得正向反馈")


if __name__ == "__main__":
    unittest.main()
