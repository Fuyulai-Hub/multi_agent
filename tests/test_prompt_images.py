"""附图 prompt 措辞与 llm.vision.enabled 对齐的回归测试。

背景（2026-10-16 两次带图需求运行）：prompt 的「附带图片」段恒称
「图片随请求一并提供」，但 ``llm.vision.enabled=false`` 时图片内容从不进入
请求体（``LLMClient._user_content`` 直接返回纯文本，仅 prompt 多一行文件名）——
模型被误导以为能看到图，对「识别图片中内容……」这类需求可能假装看过并臆造
功能点。修复后按 vision 开关分支措辞：开启才承诺随请求提供；关闭必须如实告知
「看不到图片」，并要求走 open_questions 澄清。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.config import load_config
from mas.prompts import PromptBuilder

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PromptImageSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.builder = PromptBuilder(self.config, self.run_dir, enabled=False)
        self.role = SimpleNamespace(id="product", name="产品 Agent")
        self.node = SimpleNamespace(id="intake", title="需求评估与拆解")
        self.images = [
            {"name": "shot.png", "path": "input/images/shot.png", "mime": "image/png", "bytes": 100},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def _user(self, images):
        return self.builder._user_prompt(
            role=self.role, node=self.node, task_kind="requirement_spec", iteration=1,
            directive="d", artifacts_brief="ab", bus_digest="bd",
            perception=["requirement_source"], missing=[], experience_text="",
            contract={"id": "x"}, images=images,
        )

    def test_vision_enabled_claims_image_sent(self):
        self.config.llm["vision"] = {"enabled": True}
        text = self._user(self.images)
        self.assertIn("图片随请求一并提供", text)
        self.assertIn("shot.png", text)

    def test_vision_disabled_states_image_not_visible(self):
        self.config.llm["vision"] = {"enabled": False}
        text = self._user(self.images)
        self.assertNotIn("图片随请求一并提供", text)
        self.assertIn("无法看到图片", text)
        self.assertIn("open_questions", text)

    def test_vision_missing_key_treated_as_disabled(self):
        # vision 段缺失（或为 None）时按关闭处理，不得崩也不得谎称提供
        self.config.llm["vision"] = None
        text = self._user(self.images)
        self.assertIn("无法看到图片", text)

    def test_no_images_no_section(self):
        self.config.llm["vision"] = {"enabled": True}
        text = self._user(None)
        self.assertNotIn("附带图片", text)


if __name__ == "__main__":
    unittest.main()
