"""配置加载与 Profile 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from mas.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigTests(unittest.TestCase):
    def test_base_config(self):
        config = load_config(CONFIG_DIR)
        self.assertEqual(config.flow.entry, "intake")
        self.assertIn("implement", config.flow.nodes)
        self.assertEqual(config.flow.nodes["implement"].role, "developer")
        self.assertEqual(config.flow.nodes["review"].target("fail"), "implement")
        self.assertEqual(len(config.roles), 5)
        self.assertIn("reviewer", config.roles)
        self.assertTrue(config.flow.nodes["acceptance"].criteria)

    def test_strict_profile_adds_node(self):
        config = load_config(CONFIG_DIR, profile="strict")
        self.assertIn("solution_review", config.flow.nodes)
        self.assertEqual(config.flow.nodes["solution"].target("pass"), "solution_review")
        self.assertEqual(config.flow.nodes["solution_review"].target("fail"), "solution")
        self.assertEqual(config.collaboration.rollback["review"]["max_attempts"], 4)
        review_node = config.flow.nodes["review"]
        self.assertEqual(config.collaboration.rollback_policy("review", review_node).max_attempts, 4)
        self.assertIn("acceptance", config.collaboration.rollback)

    def test_lean_profile_skips_review(self):
        config = load_config(CONFIG_DIR, profile="lean")
        self.assertNotIn("review", config.flow.nodes)
        # implement 的 pass 分支被自动改接到 review 的后继 test
        self.assertEqual(config.flow.nodes["implement"].target("pass"), "test")
        # 引用被裁剪节点的回退策略被清理
        self.assertNotIn("review", config.collaboration.rollback)
        # 验收门禁收敛为 3 项
        self.assertEqual(len(config.flow.nodes["acceptance"].criteria), 3)

    def test_describe(self):
        config = load_config(CONFIG_DIR)
        text = config.describe()
        self.assertIn("intake", text)
        self.assertIn("回退策略", text)


if __name__ == "__main__":
    unittest.main()
