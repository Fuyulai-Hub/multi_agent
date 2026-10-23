"""配置加载与 Profile 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from mas.config import load_config, load_credentials_raw, save_credentials

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
        # implement 的 pass 分支本身就是 test（test→review 的边随 review 一起被裁剪）
        self.assertEqual(config.flow.nodes["implement"].target("pass"), "test")
        # 引用被裁剪节点的回退策略被清理
        self.assertNotIn("review", config.collaboration.rollback)
        # 验收门禁收敛：lean 只保留核心三条，其余（含 docs/15 的 UI 证据两条）不在此 profile 生效。
        # 不写死总数，避免每次新增 criterion 都要改这条与本用例无关的断言。
        criteria_ids = [item.id for item in config.flow.nodes["acceptance"].criteria]
        for core in ("用例全部通过", "功能点覆盖完整", "无未关闭缺陷"):
            self.assertIn(core, criteria_ids)
        for dropped in ("阻塞性问题清零", "方案选型结论完整", "待澄清问题均已闭环"):
            self.assertNotIn(dropped, criteria_ids)

    def test_describe(self):
        config = load_config(CONFIG_DIR)
        text = config.describe()
        self.assertIn("intake", text)
        self.assertIn("回退策略", text)


class SaveCredentialsTests(unittest.TestCase):
    """save_credentials 必须按全文合并写入，不得冲掉 store 等非 LLM 段。

    回归背景（2026-10-19）：该函数曾用 load_credentials（只过滤
    api_key/base_url/token_name 三键）整文件重写 credentials.yaml，
    导致前端每保存一次模型配置就静默丢掉 store 段（MySQL 密码），
    存储后端随后按设计静默降级——数据只进 jsonl、不进 MySQL。
    """

    def test_preserves_store_section_and_untouched_llm_keys(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            (cfg / "credentials.yaml").write_text(
                "api_key: sk-old\n"
                "base_url: https://old.example/v1\n"
                "token_name: old\n"
                "store:\n"
                "  mysql:\n"
                "    password: secret-pw\n"
                "    host: 10.0.0.9\n",
                encoding="utf-8",
            )
            saved = save_credentials(cfg, api_key="sk-new")

            # 传入的键被覆盖，未传的 LLM 键保持原值
            self.assertEqual(saved["api_key"], "sk-new")
            self.assertEqual(saved["base_url"], "https://old.example/v1")
            self.assertEqual(saved["token_name"], "old")
            # store 段逐字保留（MySQL 密码不被冲掉）
            raw = load_credentials_raw(cfg)
            self.assertEqual(
                raw.get("store"),
                {"mysql": {"password": "secret-pw", "host": "10.0.0.9"}},
            )

    def test_missing_file_writes_only_passed_keys(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            saved = save_credentials(cfg, base_url="https://api.chat.csu.edu.cn/v1/")

            self.assertEqual(saved, {"base_url": "https://api.chat.csu.edu.cn/v1"})
            self.assertNotIn("store", load_credentials_raw(cfg))


if __name__ == "__main__":
    unittest.main()
