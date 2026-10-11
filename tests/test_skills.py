"""技能（Skill）能力测试：目录规范、持久化、参数校验、冒烟与非自动沉淀。

覆盖：
- 落盘 / 列出 / 查看 / 修改 / 删除
- front-matter 必填字段校验
- 冒烟：通过、失败、未声明三种结果
- 流程沉淀：run 结束后不自动保存；显式调用后才落盘，且不含环境绑定细节
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from mas.config import load_config
from mas.errors import ConfigError
from mas.leader import Leader
from mas.skills import (
    check_skill,
    edit_skill_meta,
    list_skills,
    load_skill,
    remove_skill,
    save_skill,
    validate_skill,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"

VALID_META = {
    "name": "unit-skill",
    "description": "单元测试用技能",
    "when_to_use": ["测试场景"],
    "trigger": {"keywords": ["test"], "intents": ["delivery"]},
    "inputs": [{"name": "requirement", "type": "string", "required": True, "description": "需求"}],
    "steps": ["步骤一", "步骤二"],
}


class SkillRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.llm["provider"] = "offline"
        self.config.runtime["verbose"] = False
        self.config.config_dir = self.root / "config"  # 技能与 MCP 配置写入临时目录
        self.skills_dir = self.config.skills_dir

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- CRUD
    def test_save_list_load(self):
        path = save_skill(self.skills_dir, "unit-skill", dict(VALID_META), "正文")
        self.assertTrue(path.exists())
        skills = list_skills(self.skills_dir)
        self.assertEqual([item.id for item in skills], ["unit-skill"])
        self.assertEqual(load_skill(self.skills_dir, "unit-skill").body.strip(), "正文")

    def test_save_duplicate_without_overwrite_fails(self):
        save_skill(self.skills_dir, "unit-skill", dict(VALID_META))
        with self.assertRaises(ConfigError):
            save_skill(self.skills_dir, "unit-skill", dict(VALID_META))

    def test_invalid_skill_id_rejected(self):
        with self.assertRaises(ConfigError):
            save_skill(self.skills_dir, "Bad_ID", dict(VALID_META))

    def test_remove_and_edit(self):
        save_skill(self.skills_dir, "unit-skill", dict(VALID_META))
        edit_skill_meta(self.skills_dir, "unit-skill", {"description": "改过的描述"})
        self.assertEqual(load_skill(self.skills_dir, "unit-skill").description, "改过的描述")
        remove_skill(self.skills_dir, "unit-skill")
        self.assertEqual(list_skills(self.skills_dir), [])

    # ---------------------------------------------------------------- 校验
    def test_validate_reports_all_missing_required_fields(self):
        save_skill(self.skills_dir, "partial", {"name": "partial"})
        problems = validate_skill(load_skill(self.skills_dir, "partial"))
        for field_name in ("description", "when_to_use", "trigger", "inputs", "steps"):
            self.assertIn(f"缺少必填字段: {field_name}", problems)

    def test_validate_steps_must_be_non_empty_strings(self):
        meta = dict(VALID_META, steps=["", 3])
        save_skill(self.skills_dir, "unit-skill", meta)
        self.assertIn("steps 的每一项必须是非空字符串", validate_skill(load_skill(self.skills_dir, "unit-skill")))

    # ---------------------------------------------------------------- 装载冒烟
    def test_check_smoke_pass(self):
        meta = dict(VALID_META, smoke={"command": [sys.executable, "-c", "print('ok')"], "expect_exit": 0})
        save_skill(self.skills_dir, "unit-skill", meta)
        report = check_skill(self.skills_dir, "unit-skill", self.config)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["stage"], "done")
        self.assertEqual(report["smoke"]["exit_code"], 0)

    def test_check_smoke_failure_points_to_stage_and_hint(self):
        meta = dict(VALID_META, smoke={"command": [sys.executable, "-c", "raise SystemExit(3)"],
                                       "expect_exit": 0})
        save_skill(self.skills_dir, "unit-skill", meta)
        report = check_skill(self.skills_dir, "unit-skill", self.config)
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "smoke")
        self.assertIn("3", report["reason"])
        self.assertTrue(report["hint"])

    def test_check_unknown_skill_reports_parse_stage(self):
        report = check_skill(self.skills_dir, "not-here", self.config)
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "parse")
        self.assertTrue(report["hint"])

    def test_check_without_smoke_is_honest(self):
        save_skill(self.skills_dir, "unit-skill", dict(VALID_META))
        report = check_skill(self.skills_dir, "unit-skill", self.config)
        self.assertTrue(report["ok"])
        self.assertIn("未做动态验证", report["reason"])


class SkillDistillTest(unittest.TestCase):
    """沉淀相关：不自动保存 + 显式调用后落盘且剔除环境绑定细节。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.llm["provider"] = "offline"
        self.config.runtime["verbose"] = False
        self.config.config_dir = self.root / "config"

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_does_not_distill_automatically(self):
        summary = Leader(self.config).run(str(REQUIREMENT))
        self.assertEqual(summary.verdict, "pass")
        self.assertEqual(list_skills(self.config.skills_dir), [],
                         "运行结束后不得自动沉淀任何技能")

    def test_distill_after_explicit_call(self):
        summary = Leader(self.config).run(str(REQUIREMENT))
        outcome = Leader(self.config).distill_skill(summary.run_id)
        self.assertEqual(outcome["status"], "pass", outcome)

        skill_id = outcome["skill"]["id"]
        skill = load_skill(self.config.skills_dir, skill_id)
        self.assertEqual(skill.source, "distilled")
        self.assertEqual(skill.meta.get("source_run"), summary.run_id)
        self.assertTrue(skill.meta["steps"], "沉淀结果必须包含执行步骤")

        text = skill.path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.config.runs_dir), text, "沉淀结果不得含绝对路径")
        self.assertNotIn(str(self.config.runs_dir).replace("\\", "/"), text)
        for step in skill.meta["steps"]:
            self.assertNotIn(summary.run_id, step, "执行步骤不得绑定具体 run_id")
        self.assertEqual(skill.meta["source_run"], summary.run_id, "来源运行应作为溯源字段保留")

    def test_distill_rejects_failed_run(self):
        summary = Leader(self.config).run(str(REQUIREMENT))
        run_file = self.config.runs_dir / summary.run_id / "run_summary.json"
        data = json.loads(run_file.read_text(encoding="utf-8"))
        data["verdict"] = "fail"
        run_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ConfigError):
            Leader(self.config).distill_skill(summary.run_id)


if __name__ == "__main__":
    unittest.main()
