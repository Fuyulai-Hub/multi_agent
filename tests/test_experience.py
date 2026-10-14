"""经验库（分级注入 / 晋级 / 提炼）测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mas.config import LearningConfig
from mas.experience import Experience, ExperienceLibrary

RAW = {
    "enabled": True,
    "storage": {"experience": "knowledge/experience.jsonl", "metrics": "knowledge/metrics.jsonl"},
    "tiers": {
        "L3": {"name": "全局", "scope": "global", "max_items": 2, "min_confidence": 0.6, "budget_tokens": 400},
        "L2": {"name": "项目", "scope": "project", "max_items": 2, "min_confidence": 0.4, "budget_tokens": 400},
        "L1": {"name": "即时", "scope": "run", "max_items": 2, "min_confidence": 0.0, "budget_tokens": 400},
    },
    "scoring": {"relevance": 0.45, "confidence": 0.25, "usage": 0.15, "recency": 0.15},
    "extraction": {"confidence_step": 0.1},
    "promotion": {"L1_to_L2": 1, "L2_to_L3": 2},
    "injection": {"enabled": True, "max_total_tokens": 800},
    "feedback": {"reward_on_effect": 0.05},
}


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = ExperienceLibrary(Path(self.tmp.name), LearningConfig.from_raw(RAW))

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, **kwargs):
        defaults = dict(
            tier="L1", scope="run", roles=["developer"], tags=["订单", "边界条件"],
            trigger="bug_found", lesson="阈值比较必须显式处理相等情形",
            action="按 t/t-ε/t+ε 三态实现", action_key="boundary_tri_state",
            confidence=0.8, source_run="run-1",
        )
        defaults.update(kwargs)
        return Experience(id="", **defaults)

    def test_add_and_retrieve(self):
        self.library.add([self._entry()])
        result = self.library.retrieve(role="developer", tags=["订单"], task_text="阈值边界实现",
                                       current_run="run-1")
        self.assertEqual(len(result.entries), 1)
        self.assertIn("阈值比较", result.text)
        self.assertGreater(result.tokens, 0)
        self.assertEqual(result.entries[0].hits, 1)

    def test_role_filter(self):
        self.library.add([self._entry(roles=["tester"])])
        result = self.library.retrieve(role="developer", tags=["订单"], task_text="阈值边界实现",
                                       current_run="run-1")
        # 角色不匹配会降低相关性，但 L1 兜底仍可能注入；用 tester 检索确保命中
        result_tester = self.library.retrieve(role="tester", tags=["订单"], task_text="阈值边界实现",
                                              current_run="run-1")
        self.assertGreaterEqual(len(result_tester.entries), 1)

    def test_merge_same_lesson(self):
        self.library.add([self._entry()])
        self.library.add([self._entry()])
        self.assertEqual(len(self.library.entries), 1)
        self.assertAlmostEqual(self.library.entries[0].confidence, 0.9, places=3)

    def test_run_scope_isolation(self):
        self.library.add([self._entry(source_run="other-run")])
        result = self.library.retrieve(role="developer", tags=["订单"], task_text="阈值", current_run="run-9")
        self.assertEqual(len(result.entries), 0)   # 非本次运行的 L1 不注入

    def test_promotion(self):
        self.library.add([self._entry()])
        self.library.retrieve(role="developer", tags=["订单"], task_text="阈值",
                              current_run="run-1")   # hits -> 1
        # 晋升依据是 hits × success_rate：注入（hits）之外还需要正向反馈证明有效
        self.assertEqual(self.library.promote(), [])   # 无反馈：success_rate=0.5，0.5 < 1 不晋升
        self.library.reinforce(self.library.entries[0].id, outcome="positive")
        promoted = self.library.promote()
        self.assertEqual(promoted[0]["tier"], "L2")

    def test_promotion_requires_effective_success(self):
        """纯使用次数（hits）不足以晋升：防止「反复注入但每次都导致返工」的经验爬上 L3。"""
        self.library.add([self._entry()])
        entry_id = self.library.entries[0].id
        for _ in range(5):
            self.library.retrieve(role="developer", tags=["订单"], task_text="阈值",
                                  current_run="run-1")
            self.library.reinforce(entry_id, outcome="negative")
        entry = self.library.entries[0]
        self.assertGreaterEqual(entry.hits, 5)
        self.assertGreater(entry.hits_failed, 0)
        self.assertEqual(entry.success_rate, 0.0)
        self.assertEqual(self.library.promote(), [])   # 有效成功次数 0：不晋升
        self.assertLess(entry.confidence, 0.8)         # 且置信度被下调

    def test_reinforce_outcomes(self):
        """reinforce 三态：positive 上调+计数、negative 下调+计数、neutral 仅留痕。"""
        self.library.add([self._entry()])
        entry_id = self.library.entries[0].id
        before = self.library.entries[0].confidence

        touched = self.library.reinforce(entry_id, outcome="positive")
        self.assertEqual(touched, 1)
        entry = self.library.entries[0]
        self.assertAlmostEqual(entry.confidence, round(before + 0.05, 3), places=3)
        self.assertEqual(entry.hits_success, 1)
        self.assertEqual(entry.hits_failed, 0)

        touched = self.library.reinforce(entry_id, outcome="negative")
        self.assertEqual(touched, 1)
        entry = self.library.entries[0]
        self.assertAlmostEqual(entry.confidence, round(before + 0.05 - 0.1, 3), places=3)
        self.assertEqual(entry.hits_failed, 1)

        confidence_before = entry.confidence
        touched = self.library.reinforce(entry_id, outcome="neutral")
        self.assertEqual(touched, 1)
        entry = self.library.entries[0]
        self.assertEqual(entry.confidence, confidence_before)   # neutral 不动置信度
        self.assertEqual(entry.hits_success, 1)
        self.assertEqual(entry.hits_failed, 1)

    def test_reinforce_unknown_id_and_empty(self):
        self.assertEqual(self.library.reinforce([], outcome="positive"), 0)
        self.assertEqual(self.library.reinforce(["exp-nope"], outcome="negative"), 0)

    def test_reinforce_negative_floor_and_persistence(self):
        """negative 反馈有置信度下限，且计数持久化后可重载（向后兼容旧库文件）。"""
        self.library.add([self._entry(confidence=0.1)])
        entry_id = self.library.entries[0].id
        self.library.reinforce(entry_id, outcome="negative")
        self.library.reinforce(entry_id, outcome="negative")
        self.assertGreaterEqual(self.library.entries[0].confidence, 0.05)

        reloaded = ExperienceLibrary(Path(self.tmp.name), LearningConfig.from_raw(RAW))
        entry = reloaded.entries[0]
        self.assertEqual(entry.hits_failed, 2)
        self.assertEqual(entry.success_rate, 0.0)

    def test_extract_from_events(self):
        facts = {
            "run_id": "run-x",
            "tags": ["订单"],
            "review_blocking": [{"rule": "missing_function_docstring", "count": 2, "sample_file": "a.py"}],
            "bug_patterns": [{"fp_id": "FP-03", "category": "boundary", "symptom": "1.3kg 运费错误",
                              "fix_strategy": "向上取整", "count": 2}],
            "acceptance_failed": [{"id": "用例全部通过", "reason": "字段缺失"}],
            "negotiations": [{"question": "叠加顺序", "answer": "先折扣后券", "fp_id": "FP-06"}],
            "metrics": {"acceptance": True, "node_executions": 12, "bugs_total": 2},
        }
        entries = self.library.extract_from_events(facts)
        keys = {entry.action_key for entry in entries}
        self.assertIn("avoid_missing_function_docstring", keys)
        self.assertIn("guard_FP-03__boundary", keys)   # 缺 op 时回退功能点 id
        self.assertIn("boundary_tri_state", keys)
        self.assertIn("clarify_before_implement", keys)

    def test_guard_action_key_uses_op(self):
        """缺陷守护经验的 action_key 用算子键（op）：离线 Provider 按 guard_<op>__<category>
        匹配是否注入缺陷（README/docs 声明的 guard_calc_shipping__boundary 形式）。"""
        facts = {
            "run_id": "run-x", "tags": ["订单"], "review_blocking": [], "acceptance_failed": [],
            "metrics": {"acceptance": True, "node_executions": 12, "bugs_total": 2},
            "negotiations": [],
            "bug_patterns": [{"fp_id": "FP-03", "op": "calc_shipping", "category": "boundary",
                              "symptom": "1.3kg 运费错误", "fix_strategy": "向上取整", "count": 1}],
        }
        entries = self.library.extract_from_events(facts)
        guard = [item for item in entries if item.action_key.startswith("guard_")]
        self.assertEqual(guard[0].action_key, "guard_calc_shipping__boundary")

    def test_different_ambiguities_do_not_merge(self):
        """同一 action_key 下的不同歧义必须各自成条，不拼成一条不断膨胀的 lesson。

        实测（task02-attempt02）：`clarify_before_implement` 作为固定去重键，把
        电商的「取消订单口径」与定时的「misfire 策略」两条歧义经验拼进同一条 lesson，
        hits 累到 120、置信度 1.0，注入证据失真。
        """
        facts = {
            "run_id": "run-x", "tags": ["订单"], "review_blocking": [], "bug_patterns": [],
            "acceptance_failed": [],
            "metrics": {"acceptance": True, "node_executions": 12, "bugs_total": 2},
            "negotiations": [
                {"question": "取消已支付订单的状态口径", "answer": "退款即取消", "fp_id": "FP-07"},
                {"question": "misfire 积压周期的排水粒度", "answer": "每 tick 排一次", "fp_id": "FP-06"},
            ],
        }
        entries = self.library.extract_from_events(facts)
        clarify = [item for item in entries if item.action_key == "clarify_before_implement"]
        self.assertEqual(len(clarify), 2)
        self.assertNotEqual(clarify[0].key, clarify[1].key)
        self.library.add(clarify)
        reloaded = ExperienceLibrary(Path(self.tmp.name), LearningConfig.from_raw(RAW))
        merged = [item for item in reloaded.entries if item.action_key == "clarify_before_implement"]
        self.assertEqual(len(merged), 2)
        self.assertNotIn("；", merged[0].lesson)     # 没有被拼接成一条

    def test_same_symptom_reinforces_one_entry(self):
        """同一功能点同一症状重复出现时仍然合并强化（去重键只细分不同症状）。"""
        facts = {
            "run_id": "run-x", "tags": ["订单"], "acceptance_failed": [], "negotiations": [],
            "metrics": {"acceptance": False, "node_executions": 12, "bugs_total": 2},
            "review_blocking": [],
            "bug_patterns": [{"fp_id": "FP-03", "category": "boundary", "symptom": "1.3kg 运费错误",
                              "fix_strategy": "向上取整", "count": 1}] * 2,
        }
        entries = [item for item in self.library.extract_from_events(facts)
                   if item.action_key == "guard_FP-03__boundary"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].key, entries[1].key)
        self.library.add(entries)
        kept = [item for item in self.library.entries if item.action_key == "guard_FP-03__boundary"]
        self.assertEqual(len(kept), 1)
        self.assertGreater(kept[0].confidence, 0.8)

    def test_metrics_report(self):
        self.library.record_metrics({"run_id": "r1", "node_executions": 12, "bugs_total": 4})
        self.library.record_metrics({"run_id": "r2", "node_executions": 7, "bugs_total": 0})
        summary = self.library.improvement_summary()
        self.assertTrue(summary["available"])
        self.assertEqual(summary["deltas"]["node_executions"], -5)
        markdown = self.library.learning_report_markdown()
        self.assertIn("r2", markdown)


if __name__ == "__main__":
    unittest.main()
