"""ReAct 开关前后端接入（GET/POST /api/react）的离线单测。

结构对齐 `tests/test_frontend_skills.py`（同一套开关接入范式）：

1. `_rewrite_react_switch` 纯函数——三层缩进定位、精确替换、不误伤同名键、
   保留行尾注释、缺字段报错、真实配置往返还原；
2. `save_react_switch` / `load_react_switch`——写盘内容、类型校验、读回结构
   （SYSTEM_YAML 打补丁到临时文件）；
3. 前端静态一致性——index.html 的控件 id 与 app.js 的选择器 / 接口调用一致。

字段命名口径：HTTP 的 `enabled` / `max_steps` / `require_thought` 与
system.yaml `runtime.react.*` 末级键同名同义；`nodes` / `tools_nodes` 只回显不写回。
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

# 合成文本：三个「同名 enabled」陷阱键 + 目标三键（含行尾注释）
SYNTHETIC = """\
llm:
  provider: "openai_compatible"
  vision:
    enabled: false
runtime:
  python: "python"
  context_compression:
    enabled: false
  react:
    enabled: false               # 总开关
    nodes: []                    # 空=全部节点
    max_steps: 4                 # 步数上限
    require_thought: false       # 是否强制思考
  ui_probe:
    enabled: false
"""


class RewriteFunctionTests(unittest.TestCase):
    """纯函数：三层缩进定位与精确替换。"""

    def test_toggles_only_target_lines_in_synthetic(self):
        out = server._rewrite_react_switch(SYNTHETIC, True, 6, True)
        self.assertIn("  react:\n    enabled: true", out)
        self.assertIn("    max_steps: 6", out)
        self.assertIn("    require_thought: true", out)
        # 三个同名键原样（llm.vision / runtime.context_compression / runtime.ui_probe）
        self.assertEqual(out.count("    enabled: false"), 3)
        # 行尾注释保留（内容与原文一致）
        self.assertIn("# 总开关", out)
        self.assertIn("# 步数上限", out)

    def test_round_trip_on_real_config(self):
        """真实 system.yaml：切换后逐行差异仅三处，且可往返还原。

        差异由配置**当前值**反推（不写死 false/false/4），否则开关被改过之后
        这条断言会误报。逐行 zip 比较对位置敏感，等价于「仅这三行发生变化」。
        """
        text = REAL_SYSTEM_YAML.read_text(encoding="utf-8")
        current = server.load_react_switch()
        flipped = (not current["enabled"], current["max_steps"] + 1,
                   not current["require_thought"])
        toggled = server._rewrite_react_switch(text, *flipped)
        src_lines, dst_lines = text.splitlines(), toggled.splitlines()
        self.assertEqual(len(src_lines), len(dst_lines))     # 只替换、不增删行
        diff_indexes = [i for i, (a, b) in enumerate(zip(src_lines, dst_lines)) if a != b]
        self.assertEqual(len(diff_indexes), 3)
        for index in diff_indexes:
            self.assertEqual(src_lines[index].split(":")[0], dst_lines[index].split(":")[0])
        self.assertEqual(
            server._rewrite_react_switch(
                toggled, current["enabled"], current["max_steps"], current["require_thought"]),
            text,
        )

    def test_missing_key_raises(self):
        text = SYNTHETIC.replace("    require_thought: false       # 是否强制思考\n", "")
        with self.assertRaises(ValueError) as ctx:
            server._rewrite_react_switch(text, True, 4, False)
        self.assertIn("require_thought", str(ctx.exception))

    def test_comments_with_colon_do_not_break_nesting(self):
        """注释行（含冒号）不参与层级跟踪。"""
        text = SYNTHETIC.replace("    # 总开关", "    # 见 docs/34: 总开关说明")
        out = server._rewrite_react_switch(text, True, 4, False)
        self.assertIn("    # 见 docs/34: 总开关说明", out)
        self.assertIn("    enabled: true", out)


class SaveLoadTests(unittest.TestCase):
    """写盘 / 读回（SYSTEM_YAML 打补丁到临时文件）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.yaml_path = Path(self.tmp.name) / "system.yaml"
        self.yaml_path.write_text(SYNTHETIC, encoding="utf-8", newline="\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_writes_file_and_returns_saved(self):
        saved = {"enabled": True, "max_steps": 6, "require_thought": False,
                 "nodes": [], "tools_nodes": ["intake"]}
        with mock.patch.object(server, "SYSTEM_YAML", self.yaml_path), \
                mock.patch.object(server, "load_react_switch", return_value=saved):
            result = server.save_react_switch(True, 6, False)
        self.assertEqual(result, saved)
        content = self.yaml_path.read_text(encoding="utf-8")
        self.assertIn("  react:\n    enabled: true", content)
        self.assertIn("    max_steps: 6", content)
        self.assertNotIn(b"\r\n", self.yaml_path.read_bytes())   # Windows 行尾不被转换

    def test_save_rejects_bad_types(self):
        with mock.patch.object(server, "SYSTEM_YAML", self.yaml_path):
            with self.assertRaises(ValueError):
                server.save_react_switch(1, 4, False)        # enabled 非 bool
            with self.assertRaises(ValueError):
                server.save_react_switch(True, 0, False)     # max_steps < 1
            with self.assertRaises(ValueError):
                server.save_react_switch(True, "4", False)   # max_steps 非整数
            with self.assertRaises(ValueError):
                server.save_react_switch(True, 4, None)      # require_thought 非 bool
        self.assertEqual(self.yaml_path.read_text(encoding="utf-8"), SYNTHETIC)

    def test_load_returns_yaml_keys_plus_readonly_scope(self):
        result = server.load_react_switch()
        self.assertEqual(sorted(result),
                         ["enabled", "max_steps", "nodes", "require_thought", "tools_nodes"])
        self.assertIsInstance(result["enabled"], bool)
        self.assertIsInstance(result["max_steps"], int)
        self.assertIsInstance(result["require_thought"], bool)
        self.assertIsInstance(result["nodes"], list)
        self.assertIsInstance(result["tools_nodes"], list)


class FrontendWiringTests(unittest.TestCase):
    """前端静态一致性：控件 id 与 app.js 的选择器 / 接口调用对齐。"""

    def test_html_controls_and_js_bindings(self):
        html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for control_id in ("cfg-react-enabled", "cfg-react-require-thought",
                           "cfg-react-max-steps", "cfg-react-note"):
            self.assertIn(f'id="{control_id}"', html, control_id)
            self.assertIn(f'"#{control_id}"', js, control_id)
        self.assertGreaterEqual(len(re.findall(r'fetch\("/api/react"', js)), 2)   # GET + POST
        self.assertIn("enabled: $(CFG.reactEnabled).checked", js)
        self.assertIn("require_thought: $(CFG.reactRequireThought).checked", js)
        self.assertIn("max_steps: Number($(CFG.reactMaxSteps).value)", js)
        # 总开关联动：关闭时置灰从属控件（与技能开关同一交互口径）
        self.assertIn('$(CFG.reactEnabled).addEventListener("change"', js)

    def test_default_enabled_in_system_yaml(self):
        """默认开启：system.yaml 的 runtime.react.enabled 为 true。"""
        self.assertTrue(server.load_react_switch()["enabled"])


if __name__ == "__main__":
    unittest.main()
