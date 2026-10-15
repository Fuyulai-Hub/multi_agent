"""文档上传承接层测试（docs/16 的 04 文档）+ 对话附图上传（01 文档的前端部分）。

覆盖：
- ``sources.save_upload``：合法 md / 路径穿越名净化 / 超限 / 非法后缀 / 空文件五条用例；
  ``allowed_suffixes`` / ``max_bytes`` 覆盖参数（图片通道）与默认白名单回归；
- 前端 ``POST /api/upload`` 路由与 ``POST /api/run`` 的「二选一」校验（都空/都给 → 400）；
- 前端 ``POST /api/upload_image`` 路由：合法图片 / 文档后缀拒绝 / 超限拒绝 / 空文件名；
- ``POST /api/run`` 的 ``images`` 字段类型校验（非数组/非字符串元素 → 400）；
- ``start_run`` 把 ``images`` 透传给 ``Leader.run``（文字/文件两条通道都不丢附图）。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mas import sources
from mas.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PROJECT_ROOT / "frontend" / "server.py"

# 1x1 透明 PNG（与 tests/test_vision.py 同款最小合法图片）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _load_server_module():
    spec = importlib.util.spec_from_file_location("frontend_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frontend_server = _load_server_module()


class SaveUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_markdown(self):
        content = "# 需求\n### FP-01\n- 规则：x".encode("utf-8")
        meta = sources.save_upload(self.workspace, "req.md", base64.b64encode(content).decode())
        self.assertEqual(meta["name"], "req.md")
        self.assertEqual(meta["kind"], "text")
        self.assertEqual(meta["chars"], len(content.decode("utf-8")))
        self.assertTrue(Path(meta["path"]).exists())
        self.assertIn(self.workspace / "uploads", Path(meta["path"]).parents)

    def test_path_traversal_is_sanitized(self):
        meta = sources.save_upload(self.workspace, "../../evil.md", base64.b64encode(b"hi").decode())
        self.assertEqual(meta["name"], "evil.md")
        resolved = Path(meta["path"]).resolve()
        self.assertIn((self.workspace / "uploads").resolve(), resolved.parents)

    def test_oversize_rejected(self):
        original = sources.MAX_UPLOAD_BYTES
        sources.MAX_UPLOAD_BYTES = 1024
        try:
            payload = base64.b64encode(b"a" * 2048).decode()
            with self.assertRaises(ConfigError) as ctx:
                sources.save_upload(self.workspace, "big.md", payload)
            self.assertIn("大小上限", str(ctx.exception))
        finally:
            sources.MAX_UPLOAD_BYTES = original

    def test_bad_suffix_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            sources.save_upload(self.workspace, "evil.exe", base64.b64encode(b"x").decode())
        self.assertIn("后缀", str(ctx.exception))

    def test_empty_file_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            sources.save_upload(self.workspace, "empty.md", "")
        self.assertIn("为空", str(ctx.exception))


class SaveUploadImageOverrideTests(unittest.TestCase):
    """save_upload 的 allowed_suffixes / max_bytes 覆盖参数（对话附图上传通道）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_image_suffix_allowed_with_override(self):
        payload = base64.b64encode(PNG_BYTES).decode()
        meta = sources.save_upload(self.workspace, "shot.png", payload,
                                   allowed_suffixes=set(sources.IMAGE_SUFFIXES))
        self.assertEqual(meta["name"], "shot.png")
        self.assertTrue(Path(meta["path"]).exists())
        self.assertIn(self.workspace / "uploads", Path(meta["path"]).parents)

    def test_default_whitelist_still_rejects_image(self):
        """回归：不传覆盖参数时，图片仍被需求文档白名单拒绝（/api/upload 行为不变）。"""
        payload = base64.b64encode(PNG_BYTES).decode()
        with self.assertRaises(ConfigError) as ctx:
            sources.save_upload(self.workspace, "shot.png", payload)
        self.assertIn("后缀", str(ctx.exception))

    def test_override_max_bytes_enforced(self):
        payload = base64.b64encode(PNG_BYTES).decode()
        with self.assertRaises(ConfigError) as ctx:
            sources.save_upload(self.workspace, "shot.png", payload,
                                allowed_suffixes=set(sources.IMAGE_SUFFIXES), max_bytes=10)
        self.assertIn("大小上限", str(ctx.exception))

    def test_path_traversal_still_sanitized_with_override(self):
        payload = base64.b64encode(PNG_BYTES).decode()
        meta = sources.save_upload(self.workspace, "../../evil.png", payload,
                                   allowed_suffixes=set(sources.IMAGE_SUFFIXES))
        self.assertEqual(meta["name"], "evil.png")
        resolved = Path(meta["path"]).resolve()
        self.assertIn((self.workspace / "uploads").resolve(), resolved.parents)


class StartRunImagesPassthroughTests(unittest.TestCase):
    """start_run 把 images 透传给 Leader.run（文字/文件两条通道都不丢附图）。

    Leader 被打桩，不触网、不跑流水线；仅验证 HTTP 层到 Leader 的参数契约。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _wait_called(self, run_mock, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not run_mock.called:
            time.sleep(0.02)
        self.assertTrue(run_mock.called, "Leader.run 未在超时内被调用")

    def test_images_passthrough_on_text_channel(self):
        with mock.patch("mas.leader.Leader") as leader_cls:
            frontend_server.start_run(self.workspace, None, "需求文字", images=["a.png"])
            self._wait_called(leader_cls.return_value.run)
            leader_cls.return_value.run.assert_called_once_with(
                "需求文字", as_text=True, run_id=mock.ANY, images=["a.png"])

    def test_images_passthrough_on_file_channel(self):
        with mock.patch("mas.leader.Leader") as leader_cls:
            frontend_server.start_run(self.workspace, None, "ignored",
                                      requirement_path="req.md", images=["a.png"])
            self._wait_called(leader_cls.return_value.run)
            leader_cls.return_value.run.assert_called_once_with(
                "req.md", as_text=False, run_id=mock.ANY, images=["a.png"])

    def test_no_images_stays_none(self):
        """不传 images 时保持 None（Leader.run 默认路径逐字节一致）。"""
        with mock.patch("mas.leader.Leader") as leader_cls:
            frontend_server.start_run(self.workspace, None, "需求文字")
            self._wait_called(leader_cls.return_value.run)
            leader_cls.return_value.run.assert_called_once_with(
                "需求文字", as_text=True, run_id=mock.ANY, images=None)


class UploadRouteTests(unittest.TestCase):
    """前端 HTTP 层：/api/upload 落盘 + /api/run 的二选一校验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        frontend_server.Handler.workspace = self.workspace
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), frontend_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_upload_route_writes_file(self):
        payload = base64.b64encode("# 需求".encode("utf-8")).decode()
        status, body = self._post("/api/upload", {"filename": "req.md", "content_base64": payload})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "text")
        self.assertTrue(Path(body["path"]).exists())

    def test_upload_route_rejects_bad_suffix(self):
        status, body = self._post("/api/upload",
                                  {"filename": "x.psd", "content_base64": "aGk="})
        self.assertEqual(status, 400)
        self.assertIn("后缀", body["error"])

    def test_run_rejects_both_requirement_and_path(self):
        status, body = self._post("/api/run", {"requirement": "x", "requirement_path": "y"})
        self.assertEqual(status, 400)
        self.assertIn("二选一", body["error"])

    def test_run_rejects_empty_payload(self):
        status, body = self._post("/api/run", {})
        self.assertEqual(status, 400)
        self.assertIn("不能为空", body["error"])


class UploadImageRouteTests(unittest.TestCase):
    """前端 HTTP 层：POST /api/upload_image（对话附图上传）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        frontend_server.Handler.workspace = self.workspace
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), frontend_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_upload_image_route_accepts_png(self):
        status, body = self._post("/api/upload_image", {
            "filename": "shot.png", "content_base64": base64.b64encode(PNG_BYTES).decode()})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "image")
        self.assertTrue(Path(body["path"]).exists())

    def test_upload_image_route_rejects_doc_suffix(self):
        """文档后缀走 /api/upload；图片路由只收图片（两条通道白名单隔离）。"""
        status, body = self._post("/api/upload_image", {"filename": "req.md", "content_base64": "aGk="})
        self.assertEqual(status, 400)
        self.assertIn("后缀", body["error"])

    def test_upload_image_route_rejects_oversize(self):
        original = frontend_server.IMAGE_MAX_BYTES
        frontend_server.IMAGE_MAX_BYTES = 16
        try:
            status, body = self._post("/api/upload_image", {
                "filename": "big.png", "content_base64": base64.b64encode(PNG_BYTES).decode()})
            self.assertEqual(status, 400)
            self.assertIn("大小上限", body["error"])
        finally:
            frontend_server.IMAGE_MAX_BYTES = original

    def test_upload_image_route_rejects_empty_filename(self):
        status, body = self._post("/api/upload_image", {"filename": "", "content_base64": "aGk="})
        self.assertEqual(status, 400)
        self.assertIn("filename", body["error"])

    def test_unknown_post_route_still_404(self):
        request = urllib.request.Request(self.base + "/api/upload_photo", data=b"{}", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.fail(f"expected 404, got {response.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


class RunImagesValidationTests(unittest.TestCase):
    """/api/run 的 images 字段类型校验（成功路径会启动真实流水线，不在单测覆盖）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        frontend_server.Handler.workspace = self.workspace
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), frontend_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_images_must_be_list(self):
        status, body = self._post("/api/run", {"requirement": "x", "images": "shot.png"})
        self.assertEqual(status, 400)
        self.assertIn("images", body["error"])

    def test_images_items_must_be_strings(self):
        status, body = self._post("/api/run", {"requirement": "x", "images": [123]})
        self.assertEqual(status, 400)
        self.assertIn("images", body["error"])

    def test_images_do_not_bypass_requirement_guard(self):
        """图片是附图：只有图片、没有文字/文档时仍按「requirement 不能为空」拒绝。"""
        status, body = self._post("/api/run", {"images": ["shot.png"]})
        self.assertEqual(status, 400)
        self.assertIn("不能为空", body["error"])


if __name__ == "__main__":
    unittest.main()
