"""图片输入（多模态）测试（docs/16 的 01 文档）。

覆盖：
- ``_build_payload``：images 非空 + vision.enabled → user content 为 parts（text + image_url data URI）；
  images 非空但 disabled / 无图 → user content 仍为纯文本（快照口径，行为与现状一致）；
- 附图文件缺失 / 超限 → AgentError（明确报错）；
- 端点 400 且详情提及 image → 去图重发一次且 ``degraded=True``；无关 400 → 不降级、直接失败。
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from mas.config import load_config
from mas.errors import AgentError
from mas.llm import LLMClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 1x1 透明 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _StubHandler(BaseHTTPRequestHandler):
    requests: list = []
    script: list = []

    def log_message(self, fmt, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        type(self).requests.append(payload)
        index = min(len(type(self).requests) - 1, len(type(self).script) - 1)
        result = type(self).script[index](payload)
        status, body = result if isinstance(result, tuple) else (200, result)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _ok_json(payload):
    return {
        "choices": [{"message": {"role": "assistant",
                                 "content": json.dumps({"ok": True}, ensure_ascii=False)}}],
        "usage": {"total_tokens": 10},
    }


def _reject_image(payload):
    return (400, {"error": {"message": "invalid content type: image_url not supported"}})


def _reject_other(payload):
    return (400, {"error": {"message": "bad request: missing field"}})


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _StubHandler.requests = []
        self.server = _StubServer(("127.0.0.1", 0), _StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

        self.image = self.root / "shot.png"
        self.image.write_bytes(PNG_BYTES)

        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.config.config_dir = self.root / "config"
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.config.llm["provider"] = "openai_compatible"
        self.config.llm["model"] = "stub-model"
        self.config.llm["timeout"] = 10
        self.config.llm_overrides = {"api_key": "stub-key", "base_url": self.base_url}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _bundle(self):
        return SimpleNamespace(path=None, system="sys", user="请解析需求", experiences=[], skills=[], tier_counts={})

    def _images(self):
        return [{"name": "shot.png", "path": str(self.image), "mime": "image/png", "bytes": len(PNG_BYTES)}]

    # ---------------------------------------------------------------- payload 结构
    def test_payload_uses_parts_when_enabled(self):
        self.config.llm["vision"] = {"enabled": True}
        payload = LLMClient(self.config)._build_payload(self._bundle(), context={"images": self._images()})
        content = payload["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(_StubHandler.requests, [])          # 只组装 payload，不断网

    def test_payload_stays_text_when_disabled(self):
        self.config.llm["vision"] = {"enabled": False}
        payload = LLMClient(self.config)._build_payload(self._bundle(), context={"images": self._images()})
        self.assertEqual(payload["messages"][1]["content"], "请解析需求")

    def test_payload_stays_text_without_images(self):
        self.config.llm["vision"] = {"enabled": True}
        payload = LLMClient(self.config)._build_payload(self._bundle(), context={})
        self.assertEqual(payload["messages"][1]["content"], "请解析需求")

    def test_missing_image_raises(self):
        self.config.llm["vision"] = {"enabled": True}
        missing = [{"name": "x.png", "path": str(self.root / "nope.png"), "mime": "image/png"}]
        with self.assertRaises(AgentError):
            LLMClient(self.config)._build_payload(self._bundle(), context={"images": missing})

    def test_oversize_image_raises(self):
        self.config.llm["vision"] = {"enabled": True, "max_image_bytes": 10}
        with self.assertRaises(AgentError):
            LLMClient(self.config)._build_payload(self._bundle(), context={"images": self._images()})

    # ---------------------------------------------------------------- 400 降级
    def test_image_rejection_degrades_once(self):
        self.config.llm["vision"] = {"enabled": True}
        _StubHandler.script = [_reject_image, _ok_json]
        generation = LLMClient(self.config).generate(
            "requirement_spec", self._bundle(), {"images": self._images()})
        self.assertTrue(generation.degraded)
        self.assertEqual(len(_StubHandler.requests), 2)
        self.assertIsInstance(_StubHandler.requests[0]["messages"][1]["content"], list)   # 首次带图
        self.assertEqual(_StubHandler.requests[1]["messages"][1]["content"], "请解析需求")  # 重发无图
        self.assertEqual(generation.data, {"ok": True})

    def test_unrelated_400_fails_without_degrade(self):
        self.config.llm["vision"] = {"enabled": True}
        _StubHandler.script = [_reject_other]
        with self.assertRaises(AgentError):
            LLMClient(self.config).generate("requirement_spec", self._bundle(), {"images": self._images()})
        self.assertEqual(len(_StubHandler.requests), 1)


if __name__ == "__main__":
    unittest.main()
