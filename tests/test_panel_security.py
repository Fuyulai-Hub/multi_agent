"""面板访问控制与路径限制（frontend/server.py）。

覆盖：
- 未配置令牌（回环默认模式）：/api/* 可访问，但跨站 Origin 的 POST 被 403；
- 配置令牌后：无凭据 401（带 WWW-Authenticate），Bearer / X-Panel-Token / 登录 Cookie 均可访问；
- CORS 只回显白名单来源，不再发送 ``Access-Control-Allow-Origin: *``；
- /api/run 的 requirement_path / images 限制在 workspace/uploads 内，profile 拒绝路径穿越；
- /api/llm 不回显明文 API Key（只返回掩码与 api_key_set）。
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PROJECT_ROOT / "frontend" / "server.py"


def _load_server_module():
    spec = importlib.util.spec_from_file_location("frontend_server_panel", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frontend_server = _load_server_module()


class PanelHTTPTestCase(unittest.TestCase):
    """每个用例独立启动一个 HTTP 服务，并在结束时还原 Handler 的类级配置。"""

    token = ""
    origins: set[str] | None = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._old_token = frontend_server.Handler.token
        self._old_origins = set(frontend_server.Handler.allowed_origins)
        frontend_server.Handler.workspace = self.workspace
        frontend_server.Handler.token = self.token
        frontend_server.Handler.allowed_origins = set(self.origins or ())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), frontend_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        frontend_server.Handler.token = self._old_token
        frontend_server.Handler.allowed_origins = self._old_origins
        self.tmp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None,
                headers: dict | None = None):
        """返回 (status, headers(lowercase), body_bytes)；不自动跟随 303。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        merged = dict(headers or {})
        if body is not None:
            merged.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=body, headers=merged)
            response = conn.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            conn.close()

    def json_body(self, data: bytes) -> dict:
        return json.loads(data.decode("utf-8"))


class BindSecurityTests(unittest.TestCase):
    """启动期失败关闭：非回环绑定必须提供令牌。"""

    def test_loopback_without_token_allowed(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            self.assertEqual(frontend_server.bind_security_error(host, ""), "")

    def test_public_bind_without_token_refused(self):
        for host in ("0.0.0.0", "::", "192.168.1.10"):
            message = frontend_server.bind_security_error(host, "")
            self.assertIn("MAS_PANEL_TOKEN", message)

    def test_public_bind_with_token_allowed(self):
        self.assertEqual(frontend_server.bind_security_error("0.0.0.0", "secret"), "")

    def test_loopback_origins_cover_aliases(self):
        origins = frontend_server.allowed_panel_origins("127.0.0.1", 8000)
        self.assertIn("http://127.0.0.1:8000", origins)
        self.assertIn("http://localhost:8000", origins)


class AuthTests(PanelHTTPTestCase):
    token = "panel-secret"

    def test_api_requires_token(self):
        status, headers, _ = self.request("GET", "/api/runs")
        self.assertEqual(status, 401)
        self.assertIn("bearer", headers.get("www-authenticate", "").lower())

    def test_bearer_and_header_token_accepted(self):
        status, _, body = self.request(
            "GET", "/api/runs", headers={"Authorization": "Bearer panel-secret"})
        self.assertEqual(status, 200, body)
        status, _, body = self.request(
            "GET", "/api/runs", headers={"X-Panel-Token": "panel-secret"})
        self.assertEqual(status, 200, body)

    def test_wrong_token_rejected(self):
        status, _, _ = self.request(
            "GET", "/api/runs", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_login_sets_httponly_cookie_and_cookie_authorizes(self):
        status, headers, _ = self.request("GET", "/?token=panel-secret")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("location"), "/")
        cookie = headers.get("set-cookie", "")
        self.assertIn("mas_panel_token=panel-secret", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        status, _, body = self.request(
            "GET", "/api/runs", headers={"Cookie": "mas_panel_token=panel-secret"})
        self.assertEqual(status, 200, body)

    def test_login_with_wrong_token_rejected(self):
        status, _, _ = self.request("GET", "/?token=nope")
        self.assertEqual(status, 401)

    def test_static_page_stays_public(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())

    def test_post_requires_token(self):
        status, _, _ = self.request("POST", "/api/upload", {"filename": "x.md", "content_base64": "aGk="})
        self.assertEqual(status, 401)


class CorsTests(PanelHTTPTestCase):
    token = ""
    origins = {"http://panel.example"}

    def test_allowlisted_origin_is_echoed(self):
        status, headers, _ = self.request(
            "GET", "/api/runs", headers={"Origin": "http://panel.example"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "http://panel.example")
        self.assertEqual(headers.get("vary"), "Origin")

    def test_unknown_origin_gets_no_cors_grant(self):
        status, headers, _ = self.request(
            "GET", "/api/runs", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 200)   # GET 只读；关键是浏览器拿不到 CORS 授权
        self.assertNotIn("access-control-allow-origin", headers)

    def test_cross_site_post_without_token_is_rejected(self):
        status, _, body = self.request(
            "POST", "/api/upload",
            {"filename": "x.md", "content_base64": "aGk="},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("跨站", self.json_body(body)["error"])


class PathRestrictionTests(PanelHTTPTestCase):
    token = ""

    def setUp(self):
        super().setUp()
        self.uploads = self.workspace / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.uploaded = self.uploads / "req.md"
        self.uploaded.write_text("# 需求", encoding="utf-8")
        self.outside = Path(self.tmp.name) / "secret.md"
        self.outside.write_text("# secret", encoding="utf-8")

    def _run(self, payload: dict, headers: dict | None = None):
        return self.request("POST", "/api/run", payload, headers=headers)

    def test_upload_path_accepted(self):
        with mock.patch.object(frontend_server, "start_run", return_value="run-ok") as started:
            status, _, body = self._run(
                {"requirement_path": str(self.uploaded), "profile": "lean"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.json_body(body)["run_id"], "run-ok")
        self.assertEqual(started.call_args.args[0], self.workspace)
        self.assertEqual(started.call_args.args[1], "lean")   # 位置参数：profile
        self.assertEqual(started.call_args.kwargs["requirement_path"], str(self.uploaded.resolve()))

    def test_path_outside_uploads_rejected(self):
        status, _, body = self._run({"requirement_path": str(self.outside)})
        self.assertEqual(status, 400)
        self.assertIn("workspace/uploads", self.json_body(body)["error"])

    def test_traversal_path_rejected(self):
        traversal = str(self.uploads / ".." / self.outside.name)
        status, _, body = self._run({"requirement_path": traversal})
        self.assertEqual(status, 400)
        self.assertIn("workspace/uploads", self.json_body(body)["error"])

    def test_image_outside_uploads_rejected(self):
        status, _, body = self._run({"requirement": "做点东西", "images": [str(self.outside)]})
        self.assertEqual(status, 400)
        self.assertIn("附图", self.json_body(body)["error"])

    def test_missing_file_rejected(self):
        status, _, body = self._run({"requirement_path": str(self.uploads / "missing.md")})
        self.assertEqual(status, 400)
        self.assertIn("不存在", self.json_body(body)["error"])

    def test_profile_traversal_rejected(self):
        status, _, body = self._run(
            {"requirement": "做点东西", "profile": "../../etc/passwd"})
        self.assertEqual(status, 400)
        self.assertIn("profile", self.json_body(body)["error"])

    def test_run_detail_traversal_rejected(self):
        for run_id in ("..", "../config/system"):
            status, _, body = self.request(
                "GET", "/api/run?id=" + quote(run_id, safe=""))
            self.assertEqual(status, 400, body)
            self.assertIn("run_id", self.json_body(body)["error"])

    def test_cancel_traversal_rejected(self):
        status, _, body = self.request("POST", "/api/run/cancel", {"run_id": "../../x"})
        self.assertEqual(status, 400, body)
        self.assertIn("run_id", self.json_body(body)["error"])


class LlmNoEchoTests(PanelHTTPTestCase):
    token = ""

    def test_api_llm_does_not_return_plaintext_key(self):
        with mock.patch("mas.config.resolve_credentials",
                        return_value=("sk-super-secret", "http://127.0.0.1:1/v1")):
            status, _, body = self.request("GET", "/api/llm")
        self.assertEqual(status, 200)
        raw = body.decode("utf-8")
        self.assertNotIn("sk-super-secret", raw)
        payload = self.json_body(body)
        self.assertNotIn("api_key", payload)
        self.assertTrue(payload["api_key_set"])
        self.assertTrue(payload["api_key_masked"])


class SplitDeploymentTests(PanelHTTPTestCase):
    """前后端分离：--no-static 只提供 /api/*，跨源调用由白名单放行。

    覆盖三条边界：
    - static_dir=None 时页面 404，接口不受影响（分离模式下后端不再是页面宿主）；
    - 未声明的跨源来源即使在分离模式下也被拒（失败关闭未被放宽）；
    - 白名单来源的预检与实际响应都带 CORS 头与 credentials 声明。
    """

    token = "split-token"
    origins = {"http://127.0.0.1:9000"}

    def setUp(self):
        super().setUp()
        self._old_static = frontend_server.Handler.static_dir
        frontend_server.Handler.static_dir = None

    def tearDown(self):
        frontend_server.Handler.static_dir = self._old_static
        super().tearDown()

    def test_static_disabled_but_api_available(self):
        for path in ("/", "/index.html", "/app.js"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)
        status, _, body = self.request(
            "GET", "/api/runs", headers={"Authorization": "Bearer split-token"})
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body), [])

    def test_default_still_serves_static(self):
        """未传 --no-static 时页面照旧可达（默认行为不变）。"""
        frontend_server.Handler.static_dir = SERVER_PATH.parent
        status, _, body = self.request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())

    def test_preflight_allowed_origin(self):
        status, headers, _ = self.request(
            "OPTIONS", "/api/run",
            headers={"Origin": "http://127.0.0.1:9000",
                     "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("access-control-allow-origin"), "http://127.0.0.1:9000")
        self.assertEqual(headers.get("access-control-allow-credentials"), "true")
        self.assertEqual(headers.get("vary"), "Origin")

    def test_preflight_unknown_origin_rejected(self):
        status, headers, _ = self.request(
            "OPTIONS", "/api/run", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_api_response_echoes_allowed_origin(self):
        status, headers, _ = self.request(
            "GET", "/api/runs",
            headers={"Origin": "http://127.0.0.1:9000",
                     "Authorization": "Bearer split-token"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "http://127.0.0.1:9000")
        self.assertEqual(headers.get("access-control-allow-credentials"), "true")


if __name__ == "__main__":
    unittest.main()
