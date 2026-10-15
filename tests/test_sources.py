"""输入源适配层测试（docs/16 的 03 / 04 / 06 文档）。

覆盖：
- ``classify_source`` / ``file_backend`` 路由表（确定性 L1 规则）；
- ``extract_text`` 三级路由：stdlib（md 直读 / docx 抽取）→ MCP（fake parse server）→ 明确失败；
- ``MCPDocumentParser`` 正常 / isError / 超时三条路径；
- ``fetch_feishu_document``：token + 元信息 + raw_content 三段端点、wiki 分支、错误翻译、
  缺少 ${VAR} 凭据时的明确报错。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from mas import sources
from mas.config import load_config
from mas.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_PARSE_SERVER = Path(__file__).resolve().parent / "fake_parse_server.py"


def _minimal_docx(path: Path, paragraphs, table_cells=()) -> None:
    """用 zipfile 手工构造一个最小 docx（含段落与表格），用于验证 stdlib 抽取。"""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    if table_cells:
        cells = "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in table_cells)
        body += f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


# =========================================================================== 飞书 HTTP 桩
class _FeishuHandler(BaseHTTPRequestHandler):
    token_code = 0
    raw_code = 0
    node_type = "docx"
    meta_code = 0

    def log_message(self, *args):  # 静音
        return

    def _send(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.endswith("/tenant_access_token/internal"):
            if type(self).token_code != 0:
                return self._send({"code": type(self).token_code, "msg": "app 无效"})
            return self._send({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        self._send({"code": 0})

    def do_GET(self):  # noqa: N802
        if "/raw_content" in self.path:
            if type(self).raw_code != 0:
                return self._send({"code": type(self).raw_code, "msg": "permission denied"})
            return self._send({"code": 0, "data": {"content": "# 飞书需求\n\n### FP-01 计算"}})
        if "/wiki/v2/spaces/get_node" in self.path:
            return self._send({"code": 0, "data": {"obj_type": type(self).node_type, "obj_token": "doc-1"}})
        if "/docx/v1/documents/" in self.path:
            if type(self).meta_code != 0:
                return self._send({"code": type(self).meta_code, "msg": "meta 失败"})
            return self._send({"code": 0, "data": {"document": {"title": "飞书需求"}}})
        self._send({"code": 0})


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# =========================================================================== 分类与路由
class SourceClassifyTests(unittest.TestCase):
    def test_classify_source(self):
        self.assertEqual(sources.classify_source("需求文字", as_text=True), "text")
        self.assertEqual(sources.classify_source("examples/requirements/x.md"), "file")
        self.assertEqual(sources.classify_source("https://acme.feishu.cn/docx/AbCdEf"), "feishu")
        self.assertEqual(sources.classify_source("https://x.larksuite.com/wiki/Wik123"), "feishu")
        self.assertEqual(sources.classify_source("https://example.com/a/b"), "url")

    def test_file_backend_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(PROJECT_ROOT / "config", workspace=root / "ws")
            config.config_dir = root / "config"
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "inputs.yaml").write_text(
                yaml.safe_dump({"document_parse": {
                    "server": "docparse", "tool": "convert_file", "path_arg": "file_path",
                    "formats": [".pdf", ".pptx"],
                }}, allow_unicode=True), encoding="utf-8")
            self.assertEqual(sources.file_backend(Path("a.md"), config), "text")
            self.assertEqual(sources.file_backend(Path("a.docx"), config), "docx")
            self.assertEqual(sources.file_backend(Path("a.pdf"), config), "mcp")
            self.assertEqual(sources.file_backend(Path("a.psd"), config), "unsupported")


# =========================================================================== stdlib 抽取
class ExtractStdlibTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_markdown_uses_stdlib(self):
        path = self.root / "req.md"
        path.write_text("# 需求\n### FP-01\n- 规则：x", encoding="utf-8")
        text, backend = sources.extract_text(path)
        self.assertEqual(backend, "stdlib")
        self.assertIn("FP-01", text)

    def test_docx_extracted_with_note_and_table_text(self):
        path = self.root / "req.docx"
        _minimal_docx(path, ["标题段", "规则段"], table_cells=("表A", "表B"))
        text, backend = sources.extract_text(path)
        self.assertEqual(backend, "stdlib")
        self.assertIn("自动抽取", text.splitlines()[0])       # 保真度如实标注
        self.assertIn("标题段", text)
        self.assertIn("规则段", text)
        self.assertIn("表A", text)                            # 表格单元格文本被拼接保留

    def test_unsupported_suffix_fails_explicitly(self):
        path = self.root / "req.psd"
        path.write_bytes(b"\x00\x01")
        with self.assertRaises(ConfigError) as ctx:
            sources.extract_text(path)
        self.assertIn("暂不能解析", str(ctx.exception))

    def test_pdf_without_mcp_server_fails_explicitly(self):
        path = self.root / "req.pdf"
        path.write_bytes(b"%PDF-1.4")
        config = load_config(PROJECT_ROOT / "config", workspace=self.root / "ws")
        config.config_dir = self.root / "config"
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "inputs.yaml").write_text(
            yaml.safe_dump({"document_parse": {"server": "docparse", "tool": "convert_file",
                                               "path_arg": "file_path", "formats": [".pdf"]}},
                           allow_unicode=True), encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            sources.extract_text(path, config)
        self.assertIn("docparse", str(ctx.exception))


# =========================================================================== MCP 解析
class MCPParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "ws")
        self.config.config_dir = self.root / "config"
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.pdf = self.root / "req.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 minimal")

    def tearDown(self):
        self.tmp.cleanup()

    def _configure(self, mode: str, timeout_s: int = 15) -> None:
        (self.root / "config" / "mcp.yaml").write_text(json.dumps({"servers": [{
            "name": "docparse", "transport": "stdio", "enabled": True,
            "command": sys.executable, "args": [str(FAKE_PARSE_SERVER), "--mode", mode],
            "timeout_s": timeout_s,
        }]}, ensure_ascii=False), encoding="utf-8")
        (self.root / "config" / "inputs.yaml").write_text(
            yaml.safe_dump({"document_parse": {"server": "docparse", "tool": "convert_file",
                                              "path_arg": "file_path", "formats": [".pdf"]}},
                           allow_unicode=True), encoding="utf-8")

    def test_mcp_returns_text_content(self):
        self._configure("ok")
        text, backend = sources.extract_text(self.pdf, self.config)
        self.assertEqual(backend, "mcp")
        self.assertIn("解析结果", text)
        self.assertIn("req.pdf", text)

    def test_mcp_iserror_is_config_error(self):
        self._configure("error")
        with self.assertRaises(ConfigError) as ctx:
            sources.extract_text(self.pdf, self.config)
        self.assertIn("返回错误", str(ctx.exception))

    def test_mcp_timeout_is_config_error(self):
        self._configure("slow", timeout_s=1)
        with self.assertRaises(ConfigError) as ctx:
            sources.extract_text(self.pdf, self.config)
        self.assertIn("无响应", str(ctx.exception))


# =========================================================================== 飞书
class FeishuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sources._TOKEN_CACHE.clear()
        _FeishuHandler.token_code = 0
        _FeishuHandler.raw_code = 0
        _FeishuHandler.node_type = "docx"
        self.server = _StubServer(("127.0.0.1", 0), _FeishuHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "ws")
        self.config.config_dir = self.root / "config"
        (self.root / "config").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _write_inputs(self, app_id: str = "cli_test", app_secret: str = "secret_test") -> None:
        (self.root / "config" / "inputs.yaml").write_text(
            yaml.safe_dump({"feishu": {"enabled": True, "api_base": self.api_base,
                                       "app_id": app_id, "app_secret": app_secret,
                                       "timeout_s": 10}}, allow_unicode=True),
            encoding="utf-8")

    def test_fetch_docx_document(self):
        self._write_inputs()
        title, text = sources.fetch_feishu_document(
            "https://acme.feishu.cn/docx/DocToken123", self.config)
        self.assertEqual(title, "飞书需求")
        self.assertIn("FP-01", text)

    def test_fetch_wiki_docx_resolves_node(self):
        self._write_inputs()
        title, text = sources.fetch_feishu_document(
            "https://acme.feishu.cn/wiki/WikToken9", self.config)
        self.assertEqual(title, "飞书需求")
        self.assertIn("FP-01", text)

    def test_wiki_non_docx_reports_type(self):
        self._write_inputs()
        _FeishuHandler.node_type = "sheet"
        with self.assertRaises(ConfigError) as ctx:
            sources.fetch_feishu_document("https://acme.feishu.cn/wiki/WikToken9", self.config)
        self.assertIn("sheet", str(ctx.exception))

    def test_raw_content_error_is_translated(self):
        self._write_inputs()
        _FeishuHandler.raw_code = 4031
        with self.assertRaises(ConfigError) as ctx:
            sources.fetch_feishu_document("https://acme.feishu.cn/docx/DocToken123", self.config)
        self.assertIn("4031", str(ctx.exception))
        self.assertIn("docx:document:readonly", str(ctx.exception))

    def test_token_error_is_translated(self):
        self._write_inputs()
        _FeishuHandler.token_code = 10003
        with self.assertRaises(ConfigError) as ctx:
            sources.fetch_feishu_document("https://acme.feishu.cn/docx/DocToken123", self.config)
        self.assertIn("10003", str(ctx.exception))

    def test_missing_credentials_names_env_var(self):
        os.environ.pop("MAS_MISSING_FEISHU_ID", None)
        self._write_inputs(app_id="${MAS_MISSING_FEISHU_ID}", app_secret="s")
        with self.assertRaises(ConfigError) as ctx:
            sources.fetch_feishu_document("https://acme.feishu.cn/docx/DocToken123", self.config)
        self.assertIn("MAS_MISSING_FEISHU_ID", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
