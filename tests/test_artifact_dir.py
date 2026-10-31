"""产物目录直达（docs/42）的离线单测。

结构对齐 `tests/test_frontend_react.py` / `tests/test_panel_security.py`：

1. 纯函数 `resolve_artifact_dir`——真实绝对路径、存在性校验、artifacts 优先与 run 目录回退；
2. `open_artifact_dir` / `reveal_in_file_manager`——打开成功、失败降级（路径照常返回）；
3. HTTP 层 `GET /api/run/artifacts` 与 `POST /api/run/open`（含 400/404）；
4. 前端静态一致性——app.js 的接口调用与元素 id 和后端路由同名。

口径：接口返回的 ``path`` 必须是 ``Path.resolve()`` 后的绝对路径，且调用方可用
``Path(path).is_dir()`` 复核——这是「不谎报目录已生成」的落地点。
"""

from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from frontend import server as frontend_server


class ResolveArtifactDirTests(unittest.TestCase):
    """纯函数：路径解析与存在性校验（临时 workspace，不碰真实运行数据）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.run_dir = self.workspace / "runs" / "20261029-000000-aabb"
        (self.run_dir / "input").mkdir(parents=True)
        (self.run_dir / "artifacts" / "requirement_contract").mkdir(parents=True)
        (self.run_dir / "artifacts" / "code_manifest").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prefers_artifacts_subdir_and_path_is_real(self):
        info = frontend_server.resolve_artifact_dir(self.workspace, "20261029-000000-aabb")
        self.assertTrue(info["exists"])
        self.assertTrue(info["is_artifacts_dir"])
        self.assertEqual(info["item_count"], 2)
        # 真实绝对路径 + 目录确实存在（需求要求「校验该目录确实存在」）
        self.assertTrue(Path(info["path"]).is_absolute())
        self.assertTrue(Path(info["path"]).is_dir())
        self.assertEqual(Path(info["path"]), (self.run_dir / "artifacts").resolve())

    def test_falls_back_to_run_dir_when_artifacts_absent(self):
        shutil.rmtree(self.run_dir / "artifacts")   # 整体删除：目录下已有子目录
        info = frontend_server.resolve_artifact_dir(self.workspace, "20261029-000000-aabb")
        self.assertTrue(info["exists"])
        self.assertFalse(info["is_artifacts_dir"])
        self.assertEqual(Path(info["path"]), self.run_dir.resolve())

    def test_missing_run_raises(self):
        with self.assertRaises(ValueError):
            frontend_server.resolve_artifact_dir(self.workspace, "no-such-run")

    def test_load_run_detail_carries_artifact_dir(self):
        (self.run_dir / "events.jsonl").write_text("", encoding="utf-8")
        detail = frontend_server.load_run_detail(self.workspace, "20261029-000000-aabb")
        self.assertIn("artifact_dir", detail)
        self.assertTrue(Path(detail["artifact_dir"]["path"]).is_dir())


class OpenArtifactDirTests(unittest.TestCase):
    """打开动作：成功/失败两条路径，失败也必须带回真实路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.run_dir = self.workspace / "runs" / "20261029-000000-aabb"
        (self.run_dir / "artifacts").mkdir(parents=True)
        self.run_id = "20261029-000000-aabb"

    def tearDown(self):
        self.tmp.cleanup()

    def test_windows_startfile_used_and_reports_opened(self):
        with mock.patch.object(frontend_server.os, "startfile", create=True) as startfile:
            info = frontend_server.open_artifact_dir(self.workspace, self.run_id)
        startfile.assert_called_once_with(str((self.run_dir / "artifacts").resolve()))
        self.assertTrue(info["opened"])
        self.assertEqual(info["reason"], "")
        self.assertTrue(Path(info["path"]).is_dir())

    def test_posix_uses_xdg_open_when_no_startfile(self):
        real = getattr(frontend_server.os, "startfile", None)
        if real is not None:                       # Windows 本机：临时移除该属性
            with mock.patch.object(frontend_server.os, "startfile", None, create=False):
                with mock.patch.object(frontend_server.subprocess, "Popen") as popen:
                    with mock.patch.object(frontend_server.sys, "platform", "linux"):
                        info = frontend_server.open_artifact_dir(self.workspace, self.run_id)
            popen.assert_called_once()
            self.assertEqual(popen.call_args[0][0][0], "xdg-open")
        else:
            with mock.patch.object(frontend_server.subprocess, "Popen") as popen:
                info = frontend_server.open_artifact_dir(self.workspace, self.run_id)
            popen.assert_called_once()
        self.assertTrue(info["opened"])

    def test_open_failure_degrades_with_path_and_reason(self):
        def boom(_path):
            raise OSError("no file manager")

        with mock.patch.object(frontend_server.os, "startfile", boom, create=True):
            info = frontend_server.open_artifact_dir(self.workspace, self.run_id)
        self.assertFalse(info["opened"])
        self.assertIn("不支持", info["reason"])
        self.assertTrue(Path(info["path"]).is_dir())   # 路径照常返回，供前端复制

    def test_not_generated_dir_never_claims_opened(self):
        payload = {"run_id": self.run_id, "path": str(self.run_dir / "artifacts"), "exists": False,
                   "run_dir": str(self.run_dir), "is_artifacts_dir": True, "item_count": 0}
        with mock.patch.object(frontend_server, "resolve_artifact_dir", return_value=payload):
            info = frontend_server.open_artifact_dir(self.workspace, self.run_id)
        self.assertFalse(info["opened"])
        self.assertEqual(info["reason"], "产物目录尚未生成")


class ArtifactDirHTTPTests(unittest.TestCase):
    """HTTP 层：真实起服务，GET/POST 与 400/404 分支。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.run_id = "20261029-000000-aabb"
        (self.workspace / "runs" / self.run_id / "artifacts" / "contract").mkdir(parents=True)
        self._old_token = frontend_server.Handler.token
        self._old_origins = set(frontend_server.Handler.allowed_origins)
        frontend_server.Handler.workspace = self.workspace
        frontend_server.Handler.token = ""
        frontend_server.Handler.allowed_origins = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), frontend_server.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        frontend_server.Handler.token = self._old_token
        frontend_server.Handler.allowed_origins = self._old_origins
        self.tmp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_get_artifacts_returns_existing_dir(self):
        status, raw = self.request("GET", f"/api/run/artifacts?id={self.run_id}")
        self.assertEqual(status, 200)
        info = json.loads(raw.decode("utf-8"))
        self.assertTrue(info["exists"])
        self.assertTrue(Path(info["path"]).is_dir())      # 接口给的路径确实存在

    def test_get_artifacts_rejects_bad_and_missing_run(self):
        status, _ = self.request("GET", "/api/run/artifacts?id=../etc")
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/api/run/artifacts?id=nope")
        self.assertEqual(status, 404)

    def test_post_open_opens_and_reports(self):
        with mock.patch.object(frontend_server.os, "startfile", create=True) as startfile:
            status, raw = self.request("POST", "/api/run/open", {"run_id": self.run_id})
        self.assertEqual(status, 200)
        info = json.loads(raw.decode("utf-8"))
        self.assertTrue(info["opened"])
        startfile.assert_called_once()

    def test_post_open_404_for_missing_run(self):
        status, _ = self.request("POST", "/api/run/open", {"run_id": "nope"})
        self.assertEqual(status, 404)


class FrontendConsistencyTests(unittest.TestCase):
    """前端静态一致性：app.js 的选择器与接口路径必须和后端路由同名。"""

    def setUp(self):
        self.js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
            encoding="utf-8")
        self.css = (Path(__file__).resolve().parent.parent / "frontend" / "style.css").read_text(
            encoding="utf-8")

    def test_js_calls_open_endpoint(self):
        self.assertIn("/api/run/open", self.js)
        self.assertIn("open-artifact-dir", self.js)
        self.assertIn("copy-artifact-dir", self.js)

    def test_js_renders_real_path_and_copy_fallback(self):
        self.assertIn("artifact_dir", self.js)          # 详情里的字段名一致
        self.assertIn("artifact-dir-path", self.js)
        self.assertIn("execCommand", self.js)           # 非安全上下文的复制退路

    def test_css_has_artifact_dir_style(self):
        self.assertIn(".artifact-dir", self.css)


if __name__ == "__main__":
    unittest.main()
