"""联网搜索开关的前后端接入（GET/POST /api/websearch）离线单测。

结构对齐 `tests/test_frontend_react.py`（同一套开关接入范式）：

1. `_rewrite_websearch_switch` 纯函数——顶层键定位、只改 ``websearch.enabled``、
   不误伤同名的 ``feishu.enabled``、保留行尾注释、缺字段报错、真实配置往返还原；
2. `save_websearch_switch` / `load_websearch_switch`——写盘内容、类型校验、读回结构
   与「开了但不生效」的原因判定（INPUTS_YAML + load_config 打补丁）；
3. 前端静态一致性——index.html 的控件 id 与 app.js 的选择器 / 接口调用一致；
4. 默认开启——``config/inputs.yaml`` 的 ``websearch.enabled`` 为 true（对话窗口开关默认勾上）。

字段命名口径：HTTP 的 ``enabled`` 与 inputs.yaml ``websearch.enabled`` 同名同义；
``provider`` / ``keyless`` / ``tools_nodes`` / ``available`` / ``reason`` 只回显不写回。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from frontend import server

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_INPUTS_YAML = PROJECT_ROOT / "config" / "inputs.yaml"

# 合成文本：feishu 段的同名 enabled 是主要陷阱键，websearch 段带行尾注释
SYNTHETIC = """\
feishu:
  enabled: true
  api_base: "https://open.feishu.cn"
document_parse:
  server: "docparse"
websearch:
  enabled: false                          # 面板「联网」开关
  pre_enrich: false                       # 路径 C：Leader 入口预检索
  provider: "bing_web"                    # 免 Key 通道
  api_key: ""
  max_results: 5
  timeout_s: 15
"""


def _synthetic(**overrides) -> str:
    """只改 ``websearch`` 段内的字段：``feishu`` 段有同名 ``enabled``，全局替换会误伤。"""
    lines = SYNTHETIC.splitlines()
    section = next(i for i, line in enumerate(lines) if line.startswith("websearch:"))
    for key, value in overrides.items():
        for i in range(section + 1, len(lines)):
            if re.match(rf"^  {re.escape(key)}:\s*\S", lines[i]):
                lines[i] = f"  {key}: {value}"
                break
    return "\n".join(lines) + "\n"


def _llm(tools_enabled: bool = True, tools_nodes=("intake",)):
    return SimpleNamespace(llm={"tools_enabled": tools_enabled, "tools_nodes": list(tools_nodes)})


class RewriteFunctionTests(unittest.TestCase):
    """纯函数：顶层键定位与精确替换。"""

    def test_toggles_only_websearch_enabled(self):
        out = server._rewrite_websearch_switch(SYNTHETIC, True)
        self.assertIn("websearch:\n  enabled: true", out)
        self.assertIn("feishu:\n  enabled: true", out)        # 同名键不被误改
        self.assertIn("# 面板「联网」开关", out)                 # 行尾注释保留
        self.assertIn('  provider: "bing_web"', out)           # 其余字段原样

    def test_round_trip_on_real_config(self):
        """真实 inputs.yaml：切换后逐行差异仅一处，且可往返还原（不写死当前值）。"""
        text = REAL_INPUTS_YAML.read_text(encoding="utf-8")
        current = server.load_websearch_switch()["enabled"]
        toggled = server._rewrite_websearch_switch(text, not current)
        src_lines, dst_lines = text.splitlines(), toggled.splitlines()
        self.assertEqual(len(src_lines), len(dst_lines))        # 只替换、不增删行
        diff_indexes = [i for i, (a, b) in enumerate(zip(src_lines, dst_lines)) if a != b]
        self.assertEqual(len(diff_indexes), 1)
        self.assertEqual(src_lines[diff_indexes[0]].split(":")[0],
                         dst_lines[diff_indexes[0]].split(":")[0])
        self.assertEqual(server._rewrite_websearch_switch(toggled, current), text)

    def test_missing_key_raises(self):
        text = SYNTHETIC.replace('  enabled: false                          # 面板「联网」开关\n', "")
        with self.assertRaises(ValueError) as ctx:
            server._rewrite_websearch_switch(text, True)
        self.assertIn("enabled", str(ctx.exception))

    def test_comments_with_colon_do_not_break_nesting(self):
        """注释行（含冒号）不参与层级跟踪。"""
        text = SYNTHETIC.replace("# 面板「联网」开关", "# 见 docs/16: 面板开关说明")
        out = server._rewrite_websearch_switch(text, True)
        self.assertIn("# 见 docs/16: 面板开关说明", out)
        self.assertIn("websearch:\n  enabled: true", out)


class SaveLoadTests(unittest.TestCase):
    """写盘 / 读回（INPUTS_YAML 打补丁到临时文件，load_config 打补丁固定总闸状态）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.yaml_path = Path(self.tmp.name) / "inputs.yaml"
        self.yaml_path.write_text(SYNTHETIC, encoding="utf-8", newline="\n")
        self._patches = [
            mock.patch.object(server, "INPUTS_YAML", self.yaml_path),
            mock.patch("mas.config.load_config", return_value=_llm()),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self.tmp.cleanup()

    def test_save_writes_file_and_returns_saved(self):
        result = server.save_websearch_switch(True)
        self.assertTrue(result["enabled"])
        content = self.yaml_path.read_text(encoding="utf-8")
        self.assertIn("websearch:\n  enabled: true", content)
        self.assertIn("feishu:\n  enabled: true", content)
        self.assertNotIn(b"\r\n", self.yaml_path.read_bytes())   # Windows 行尾不被转换

    def test_save_rejects_non_bool(self):
        for bad in (1, "true", None):
            with self.assertRaises(ValueError):
                server.save_websearch_switch(bad)
        self.assertEqual(self.yaml_path.read_text(encoding="utf-8"), SYNTHETIC)

    def test_load_returns_keys_and_keyless_availability(self):
        result = server.load_websearch_switch()
        self.assertEqual(
            sorted(result),
            ["available", "enabled", "key_present", "keyless", "max_results", "pre_enrich",
             "provider", "reason", "tools_enabled", "tools_nodes"],
        )
        self.assertFalse(result["enabled"])                     # 合成文本里是 false
        self.assertTrue(result["keyless"])                      # bing_web 免 Key
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "开关关闭")
        self.assertEqual(result["tools_nodes"], ["intake"])

    def test_available_when_enabled_and_keyless(self):
        self.yaml_path.write_text(_synthetic(enabled="true"), encoding="utf-8", newline="\n")
        result = server.load_websearch_switch()
        self.assertTrue(result["available"])
        self.assertIn("免 Key", result["reason"])

    def test_key_provider_without_key_is_unavailable(self):
        """官方 API 通道缺 Key：开关开着也不可用，原因里点明怎么修（不误报"已生效"）。"""
        self.yaml_path.write_text(
            _synthetic(enabled="true", provider='"bing"', api_key='"${WEBSEARCH_API_KEY}"'),
            encoding="utf-8", newline="\n",
        )
        result = server.load_websearch_switch()
        self.assertFalse(result["keyless"])
        self.assertFalse(result["key_present"])
        self.assertFalse(result["available"])
        self.assertIn("需要订阅 Key", result["reason"])

    def test_function_calling_master_switch_off_is_unavailable(self):
        self.yaml_path.write_text(_synthetic(enabled="true"), encoding="utf-8", newline="\n")
        with mock.patch("mas.config.load_config", return_value=_llm(tools_enabled=False)):
            result = server.load_websearch_switch()
        self.assertFalse(result["available"])
        self.assertIn("llm.tools_enabled=false", result["reason"])


class FrontendWiringTests(unittest.TestCase):
    """前端静态一致性：控件 id 与 app.js 的选择器 / 接口调用对齐。"""

    def test_html_controls_and_js_bindings(self):
        html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for control_id in ("websearch-toggle", "websearch-control"):
            self.assertIn(f'id="{control_id}"', html, control_id)
            self.assertIn(f'"#{control_id}"', js, control_id)
        # GET + POST 两个调用点；接口路径前可能带部署基址（前后端分离的 ?api=），故只匹配路径本身
        self.assertGreaterEqual(len(re.findall(r'fetch\([^;\n]*"/api/websearch"', js)), 2)
        self.assertIn("JSON.stringify({ enabled })", js)
        self.assertIn('$("#websearch-toggle").addEventListener("change", toggleWebsearch)', js)
        self.assertIn("refreshWebsearchToggle();", js)           # 启动即回显当前配置

    def test_default_enabled_in_inputs_yaml(self):
        """默认开启：config/inputs.yaml 的 websearch.enabled 为 true（对话窗口开关默认勾上）。"""
        self.assertTrue(server.load_websearch_switch()["enabled"])


if __name__ == "__main__":
    unittest.main()
