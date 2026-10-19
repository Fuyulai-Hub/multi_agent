"""技能开关前后端接入（GET/POST /api/skills）的离线单测。

覆盖三层：
1. `_rewrite_skills_switches` 纯函数——三层缩进定位、精确替换、不误伤同名键、
   保留注释、缺字段报错、往返还原；
2. `save_skills_switch` / `load_skills_switch`——写盘内容、布尔校验、读回结构
   （SYSTEM_YAML 打补丁到临时文件，不触碰真实配置）；
3. 前端静态一致性——index.html 的控件 id 与 app.js 的选择器 / 接口调用一致。

字段命名口径：HTTP 层的 `enabled` / `tools_enabled` 与 system.yaml 的
`runtime.skills.enabled` / `runtime.skills.tools_enabled` 末级键同名同义，
`/api/skills` 路由前缀补全 `runtime.skills` 命名空间。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from frontend import server

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_SYSTEM_YAML = PROJECT_ROOT / "config" / "system.yaml"

# 合成文本：三个「同名 enabled」陷阱键 + 目标两键 + 注释
SYNTHETIC = """\
llm:
  provider: "openai_compatible"
  vision:
    enabled: false
runtime:
  python: "python"
  context_compression:
    enabled: false
  skills:
    enabled: false
    # 单节点最多注入几个技能（按匹配度降序）
    tools_enabled: false
    tools_timeout_s: 60
  ui_probe:
    enabled: false
"""


class RewriteFunctionTests(unittest.TestCase):
    """纯函数：三层缩进定位与精确替换。"""

    def test_toggles_only_target_lines_in_synthetic(self):
        out = server._rewrite_skills_switches(SYNTHETIC, True, True)
        # 目标两键被替换
        self.assertIn("  skills:\n    enabled: true\n", out)
        self.assertIn("    tools_enabled: true\n", out)
        # 三个同名键原样（顶层 llm.vision / runtime.context_compression / runtime.ui_probe）
        self.assertEqual(out.count("    enabled: false"), 3)      # vision + context_compression + ui_probe
        self.assertIn("  ui_probe:\n    enabled: false\n", out)

    def test_round_trip_on_real_config(self):
        """真实 system.yaml：切换后与原文**逐行**差异仅两处，且可往返还原。

        不能用「行集合差异」判定：真实配置里存在与目标行同形的行
        （如 sandbox_command_policy 的 `    enabled: true`），集合语义会漏判；
        逐行 zip 比较对位置敏感，才等价于「仅这两行发生变化」。

        期望差异由配置**当前值**反推（不是写死 false）：前端开关会把这两键写成 true，
        写死「false → true」会在开关打开后变成「无差异」而误报失败。
        """
        text = REAL_SYSTEM_YAML.read_text(encoding="utf-8")
        current = server.load_skills_switch()
        toggled = server._rewrite_skills_switches(
            text, not current["enabled"], not current["tools_enabled"])
        src_lines, dst_lines = text.splitlines(), toggled.splitlines()
        self.assertEqual(len(src_lines), len(dst_lines))     # 只替换、不增删行
        diff_pairs = [(a, b) for a, b in zip(src_lines, dst_lines) if a != b]
        self.assertEqual(diff_pairs, [
            (f"    enabled: {str(current['enabled']).lower()}",
             f"    enabled: {str(not current['enabled']).lower()}"),
            (f"    tools_enabled: {str(current['tools_enabled']).lower()}",
             f"    tools_enabled: {str(not current['tools_enabled']).lower()}"),
        ])
        self.assertEqual(
            server._rewrite_skills_switches(toggled, current["enabled"], current["tools_enabled"]), text)

    def test_comments_with_colon_do_not_break_nesting(self):
        """注释行（含 ascii 冒号）不参与层级跟踪，目标行仍被准确定位。"""
        text = SYNTHETIC.replace(
            "    # 单节点最多注入几个技能（按匹配度降序）",
            "    # 见 docs/16 的 05 文档: 匹配度阈值说明",
        )
        out = server._rewrite_skills_switches(text, True, False)
        self.assertIn("    # 见 docs/16 的 05 文档: 匹配度阈值说明", out)
        self.assertIn("    enabled: true\n", out)
        self.assertIn("    tools_enabled: false\n", out)

    def test_missing_key_raises(self):
        text = SYNTHETIC.replace("    tools_enabled: false\n", "")
        with self.assertRaises(ValueError) as ctx:
            server._rewrite_skills_switches(text, True, True)
        self.assertIn("tools_enabled", str(ctx.exception))


class SaveLoadTests(unittest.TestCase):
    """写盘 / 读回的端到端（SYSTEM_YAML 打补丁到临时文件）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.yaml_path = Path(self.tmp.name) / "system.yaml"
        # newline="\n"：以 LF 落盘（与仓库中 system.yaml 的真实行尾一致），
        # save 写回后必须仍是 LF——Windows 下 write_text 默认会把 \n 转成 \r\n。
        self.yaml_path.write_text(SYNTHETIC, encoding="utf-8", newline="\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_writes_file_and_returns_saved(self):
        saved = {"enabled": True, "tools_enabled": False}
        with mock.patch.object(server, "SYSTEM_YAML", self.yaml_path), \
                mock.patch.object(server, "load_skills_switch", return_value=saved):
            result = server.save_skills_switch(True, False)
        self.assertEqual(result, saved)
        content = self.yaml_path.read_text(encoding="utf-8")
        self.assertIn("  skills:\n    enabled: true\n", content)
        self.assertIn("    tools_enabled: false\n", content)
        # 行尾不被转换（Windows write_text 默认会把整份文件转成 CRLF）
        self.assertNotIn(b"\r\n", self.yaml_path.read_bytes())

    def test_save_rejects_non_bool(self):
        with mock.patch.object(server, "SYSTEM_YAML", self.yaml_path):
            with self.assertRaises(ValueError):
                server.save_skills_switch(1, False)      # 1 不是 bool
            with self.assertRaises(ValueError):
                server.save_skills_switch(True, None)
        # 拒绝后文件未被改动
        self.assertEqual(self.yaml_path.read_text(encoding="utf-8"), SYNTHETIC)

    def test_load_returns_same_keys_as_yaml(self):
        """读回结构：键名 = system.yaml 的 runtime.skills 子键名，值为布尔。"""
        result = server.load_skills_switch()
        self.assertEqual(sorted(result), ["enabled", "tools_enabled"])
        for key, value in result.items():
            self.assertIsInstance(value, bool, key)


class FrontendWiringTests(unittest.TestCase):
    """前端静态一致性：控件 id 与 app.js 的选择器 / 接口调用对齐。"""

    def test_html_controls_and_js_bindings(self):
        html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for control_id in ("cfg-skills-enabled", "cfg-skills-tools-enabled"):
            self.assertIn(f'id="{control_id}"', html, control_id)
            self.assertIn(f'"#{control_id}"', js, control_id)
        # 读写接口与字段名（与后端 /api/skills、runtime.skills.* 一致）
        self.assertGreaterEqual(len(re.findall(r'fetch\("/api/skills"', js)), 2)  # GET + POST
        self.assertIn("enabled: $(CFG.skillsEnabled).checked", js)
        self.assertIn("tools_enabled: $(CFG.skillsToolsEnabled).checked", js)
        # 主界面入口：弹窗默认 hidden，主界面必须另有可见开关（否则界面上「看不到开关」）
        self.assertIn('id="skills-toggle"', html)
        self.assertIn('"#skills-toggle"', js)
        self.assertIn('$("#skills-toggle").addEventListener("change", toggleSkills)', js)


if __name__ == "__main__":
    unittest.main()
