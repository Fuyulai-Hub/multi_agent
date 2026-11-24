"""SPA 执行协议（docs/52）的离线回归。

覆盖四件事：

1. ``mas_testkit`` 本身：就绪判定、HTTP 取证、日志洪泛不死锁、超时/提前退出、
   进程树回收、命令白名单与 PATHEXT 解析；
2. 框架注入：冻结套件经 runner 执行时，服务命令从 ``MAS_SERVICE_CMD`` 进来、
   日志落到 ``verify_io/``、逐用例记录完整（真 Node 服务端到端）；
3. 协议提示：SPA 拿到服务托管协议、SSR 仍拿进程内协议（两者不能串）；
4. 静态规则：SPA 套件自己 spawn 服务会被冻结点名。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.tester import (
    _BROWSER_PROTOCOL, _SPA_PROTOCOL, _WEB_PROTOCOL, TesterAgent,
)
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.hidden import build_generation_prompt
from mas.prompts import PromptBuilder
from mas.sandbox import Sandbox
from mas.verifier import TestVerifier
from mas.verify import testkit
from mas.verify.exec import (
    TESTKIT_RELATIVE, execute_frozen_suite, execute_hidden_suite,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 最小可服务的 HTTP 服务：绑 0 端口、打印 MAS_UI_PORT、返回两个 JSON 接口。
_ECHO_SERVICE = '''
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/products"):
            self._send(200, {"items": [{"id": 1, "name": "机械键盘", "price": 199}]})
        else:
            self._send(404, {"error": "not found"})


flood = sys.argv[1] if len(sys.argv) > 1 else ""
for _ in range(int(flood or 0)):
    print("startup log line " * 40, flush=True)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print("MAS_UI_PORT=%d" % server.server_address[1], flush=True)
server.serve_forever()
'''

#: 起一个孙进程持续写心跳文件：父进程被杀而孙进程存活时，文件仍在变。
_TREE_SERVICE = '''
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

child_code = (
    "import sys, time\\n"
    "while True:\\n"
    "    with open(sys.argv[1], 'a') as fh:\\n"
    "        fh.write('x')\\n"
    "        fh.flush()\\n"
    "    time.sleep(0.05)\\n"
)
subprocess.Popen([sys.executable, "-c", child_code, sys.argv[1]])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print("MAS_UI_PORT=%d" % server.server_address[1], flush=True)
server.serve_forever()
'''

_SILENT_SERVICE = "import time\ntime.sleep(30)\n"
_EXIT_SERVICE = "import sys\nprint('boom', flush=True)\nsys.exit(3)\n"

#: 冻结套件（SPA 形态）：只用 HTTP 取证，服务由框架托管。
_SPA_SUITE = '''
import unittest

from mas_testkit import start_service


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = start_service()

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()

    def test_functional_products_list(self):
        resp = self.service.get("/api/products")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["items"][0]["name"], "机械键盘")

    def test_exception_unknown_path(self):
        self.assertEqual(self.service.get("/api/missing").status, 404)

    def test_boundary_empty_query(self):
        self.assertEqual(self.service.get("/api/products?q=").status, 200)
'''

#: 隐藏套件（门禁阶段）：与可见套件同一服务、同一契约，但不依赖可见套件的任何代码。
_HIDDEN_SUITE = '''
import unittest

from mas_testkit import start_service


class TestHiddenApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = start_service()

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()

    def test_functional_price_integer(self):
        item = self.service.get("/api/products").json["items"][0]
        self.assertIsInstance(item["price"], int)

    def test_exception_unknown_path(self):
        self.assertEqual(self.service.get("/api/nope").status, 404)
'''

_NODE_SERVER = '''
const http = require('http');
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/api/products')) {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ items: [{ id: 1, name: '机械键盘', price: 199 }] }));
  } else {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not found' }));
  }
});
server.listen(0, '127.0.0.1', () => {
  console.log(`MAS_UI_PORT=${server.address().port}`);
});
'''


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


class TestkitTests(unittest.TestCase):
    """框架托管的外部服务：就绪、取证、回收。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "service.log"
        self._old_env = {key: os.environ.get(key) for key in
                         ("MAS_SERVICE_CMD", "MAS_SERVICE_LOG", "MAS_SERVICE_ALLOWED")}
        os.environ["MAS_SERVICE_LOG"] = str(self.log)
        os.environ.pop("MAS_SERVICE_CMD", None)
        os.environ.pop("MAS_SERVICE_ALLOWED", None)

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _service(self, source: str, *args: str, **kwargs):
        script = _write(self.root, "service.py", source)
        return testkit.start_service([sys.executable, str(script), *args], **kwargs)

    def test_start_serves_http_and_stops(self):
        service = self._service(_ECHO_SERVICE)
        try:
            self.assertGreater(service.port, 0)
            self.assertEqual(service.base_url, f"http://127.0.0.1:{service.port}")
            self.assertEqual(service.get("/api/products").json["items"][0]["price"], 199)
            self.assertEqual(service.get("/api/missing").status, 404)
        finally:
            service.stop()
        self.assertIsNotNone(service.process.poll())          # 已回收
        self.assertIn("MAS_UI_PORT=", self.log.read_text(encoding="utf-8"))

    def test_log_flood_does_not_deadlock(self):
        """启动日志 200KB+ 也不能卡住：这是 docs/46 里 33 条用例全挂的根因。"""
        started = time.time()
        service = self._service(_ECHO_SERVICE, "1000")        # 1000 行 × 680 字符 ≈ 680KB
        try:
            self.assertLess(time.time() - started, 20)
            self.assertEqual(service.get("/api/products").status, 200)
            # 全部启动日志都落盘了（管道方案在这种量级下必然死锁）
            self.assertGreater(self.log.stat().st_size, 500000)
        finally:
            service.stop()

    def test_timeout_reports_log_tail(self):
        with self.assertRaises(testkit.ServiceError) as caught:
            self._service(_SILENT_SERVICE, timeout=0.8)
        self.assertIn("启动超时", str(caught.exception))

    def test_early_exit_reports_code(self):
        with self.assertRaises(testkit.ServiceError) as caught:
            self._service(_EXIT_SERVICE, timeout=15)
        message = str(caught.exception)
        self.assertIn("提前退出", message)
        self.assertIn("boom", message)                        # 日志尾部带进 message

    def test_stop_kills_process_tree(self):
        heartbeat = self.root / "heartbeat.txt"
        script = _write(self.root, "tree_service.py", _TREE_SERVICE)
        service = testkit.start_service([sys.executable, str(script), str(heartbeat)])
        try:
            deadline = time.time() + 10
            while not heartbeat.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(heartbeat.exists(), "孙进程未启动，用例前提不成立")
            time.sleep(0.35)                                  # 让心跳写几轮
            before = heartbeat.stat().st_size
            time.sleep(0.3)
            self.assertGreater(heartbeat.stat().st_size, before, "孙进程没有持续写心跳")
        finally:
            service.stop()
        after = heartbeat.stat().st_size
        time.sleep(0.6)
        self.assertEqual(after, heartbeat.stat().st_size,
                         "父进程回收后孙进程仍在写心跳：进程树没杀干净")

    def test_missing_command_is_explicit(self):
        with self.assertRaises(testkit.ServiceError) as caught:
            testkit.start_service()
        self.assertIn("MAS_SERVICE_CMD", str(caught.exception))

    def test_command_whitelist_rejects_others(self):
        os.environ["MAS_SERVICE_ALLOWED"] = "node"
        try:
            with self.assertRaises(testkit.ServiceError) as caught:
                self._service(_ECHO_SERVICE, timeout=5)
            self.assertIn("白名单", str(caught.exception))
        finally:
            os.environ.pop("MAS_SERVICE_ALLOWED", None)

    def test_npm_resolved_via_pathext(self):
        """Windows 下裸名 npm 不可 CreateProcess：testkit 用 which 解析成 npm.CMD。"""
        resolved = testkit._resolve_command(["npm"])
        self.assertTrue(Path(resolved[0]).name.lower().startswith("npm"))

    def test_missing_executable_is_explicit(self):
        with self.assertRaises(testkit.ServiceError) as caught:
            testkit._resolve_command(["definitely-not-a-real-binary-xyz"])
        self.assertIn("未找到可执行文件", str(caught.exception))


class FrameworkInjectionTests(unittest.TestCase):
    """冻结套件经框架 runner 执行：服务命令、日志、逐用例证据全部由框架托管。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.run_dir = root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.workspace)
        self.config.runtime["verbose"] = False
        self.events = EventLog(self.run_dir / "events.jsonl", "test-service-protocol")
        self.sandbox = Sandbox(self.workspace, self.config, events=self.events)
        _write(self.workspace, "server.js", _NODE_SERVER)
        _write(self.workspace, "tests/test_all.py", _SPA_SUITE)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(shutil.which("node"), "需要本机 node")
    def test_frozen_spa_suite_passes_through_runner(self):
        verifier = TestVerifier(self.run_dir, self.workspace, events=self.events)
        manifest = verifier.freeze("tests/test_all.py", 1)

        execution = execute_frozen_suite(
            self.sandbox, self.run_dir, manifest, iteration=1, tag="spa",
            service_cmd=["node", "server.js"],
        )
        parsed = execution.result or {}
        self.assertEqual(parsed.get("total"), 3, execution.exec.stderr[-2000:])
        self.assertEqual(parsed.get("failed"), 0,
                         json.dumps(parsed.get("cases"), ensure_ascii=False)[:2000])
        self.assertEqual(execution.exec.exit_code, 0)

        # testkit 与 runner 一样由框架安装，模型改写无效
        self.assertTrue((self.workspace / TESTKIT_RELATIVE).exists())
        # 服务日志落在证据目录（模型工作区之外），可用于事后排障
        self.assertIn("MAS_UI_PORT=",
                      (execution.io_dir / "service.log").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要本机 node")
    def test_suite_without_service_command_fails_loudly(self):
        """没给命令时不能静默跳过：全部用例失败，且失败信息指向 MAS_SERVICE_CMD。"""
        verifier = TestVerifier(self.run_dir, self.workspace, events=self.events)
        manifest = verifier.freeze("tests/test_all.py", 1)
        execution = execute_frozen_suite(
            self.sandbox, self.run_dir, manifest, iteration=2, tag="spa-nocmd")
        parsed = execution.result or {}
        self.assertEqual(parsed.get("passed"), 0)
        messages = " ".join(str(case.get("message", "")) for case in parsed.get("cases") or [])
        self.assertIn("MAS_SERVICE_CMD", messages)

    @unittest.skipUnless(shutil.which("node"), "需要本机 node")
    def test_hidden_suite_shares_the_same_service_command(self):
        """门禁阶段的隐藏套件复用同一条命令：同一服务、同一契约，口径不漂移。"""
        hidden_root = self.run_dir.parent / "hidden" / self.run_dir.name
        _write(hidden_root, "tests/test_hidden.py", _HIDDEN_SUITE)
        hidden_verifier = TestVerifier(hidden_root, hidden_root, events=self.events)
        manifest = hidden_verifier.freeze("tests/test_hidden.py", 1)

        execution = execute_hidden_suite(
            self.sandbox, self.run_dir, hidden_root, manifest, iteration=1, tag="hidden",
            service_cmd=["node", "server.js"],
        )
        parsed = execution.result or {}
        self.assertEqual(parsed.get("total"), 2, execution.exec.stderr[-2000:])
        self.assertEqual(parsed.get("failed"), 0,
                         json.dumps(parsed.get("cases"), ensure_ascii=False)[:2000])


class _AgentMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        workspace = root / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=workspace)
        self.config.runtime["verbose"] = False
        self.run_dir = root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "test-service-protocol")
        self.bus = MessageBus(self.run_dir / "messages.jsonl", self.events,
                              "test-service-protocol")
        self.blackboard = Blackboard(self.run_dir / "artifacts", self.events,
                                     "test-service-protocol")
        self.sandbox = Sandbox(workspace, self.config, events=self.events)
        self.ctx = SimpleNamespace(
            config=self.config, events=self.events, run_dir=self.run_dir,
            blackboard=self.blackboard, bus=self.bus, sandbox=self.sandbox,
            prompts=PromptBuilder(self.config, self.run_dir, enabled=False),
            llm=None, tools=None, experience=None, log=lambda *a, **k: None,
            run_state={}, consult=None,
        )

    def tearDown(self):
        self.tmp.cleanup()


class ProtocolPromptTests(_AgentMixin, unittest.TestCase):
    """两套协议不能串：SPA 不许再 `import` 交付包，SSR 也不能改用外部进程。"""

    def test_spa_protocol_uses_testkit_and_forbids_import(self):
        self.assertIn("from mas_testkit import start_service", _SPA_PROTOCOL)
        self.assertNotIn("build_handler", _SPA_PROTOCOL)
        self.assertIn("禁止", _SPA_PROTOCOL)
        # 浏览器证据协议仍叠加（UI 检查不在本次改动范围）
        self.assertIn("ui_service_cmd", _BROWSER_PROTOCOL)

    def test_ssr_protocol_unchanged(self):
        self.assertIn("build_handler", _WEB_PROTOCOL)
        self.assertIn("同进程服务生命周期", _WEB_PROTOCOL)
        self.assertNotIn("mas_testkit", _WEB_PROTOCOL)

    def test_hidden_prompt_follows_delivery_shape(self):
        common = {"spec": {"title": "商城", "function_points": []}, "clauses": [],
                  "package": "shop"}
        spa = build_generation_prompt(delivery_shape="full_stack_spa", **common)[1]
        ssr = build_generation_prompt(delivery_shape="full_stack_ssr", **common)[1]
        self.assertIn("mas_testkit", spa)
        self.assertIn("MAS_UI_PORT", spa)
        self.assertNotIn("mas_testkit", ssr)
        self.assertIn("在测试进程内", ssr)


class SuiteDefectRuleTests(_AgentMixin, unittest.TestCase):
    """SPA 套件自己 spawn 服务 → 冻结前静态点名，而不是等 39 条全挂。"""

    _BAD = "import subprocess\n\n\ndef test_x():\n    subprocess.Popen(['npm', 'run', 'dev'])\n"
    _CLEAN = (
        "from mas_testkit import start_service\n\n\n"
        "def test_x():\n    service = start_service()\n    service.stop()\n"
    )

    def test_self_managed_service_is_flagged_for_spa(self):
        path = "tests/test_all.py"
        self.sandbox.write_file(path, self._BAD)
        findings = self.sandbox.spa_service_bypass_findings(path, lambda rule, default="minor": default)
        self.assertEqual([item.rule for item in findings], ["spa_service_self_managed"])
        self.assertIn("mas_testkit", findings[0].message)

    def test_clean_suite_and_non_test_files_are_ignored(self):
        self.sandbox.write_file("tests/test_ok.py", self._CLEAN)
        self.assertEqual(
            self.sandbox.spa_service_bypass_findings(
                "tests/test_ok.py", lambda rule, default="minor": default), [])
        self.sandbox.write_file("app/main.py", self._BAD)
        self.assertEqual(
            self.sandbox.spa_service_bypass_findings(
                "app/main.py", lambda rule, default="minor": default), [])

    def test_preexec_diagnosis_gated_by_shape(self):
        agent = TesterAgent(self.ctx)
        self.sandbox.write_file("tests/test_all.py", self._BAD)
        rules = {item["rule"] for item in agent._preexec_diagnosis("tests/test_all.py", "full_stack_spa")}
        self.assertIn("spa_service_self_managed", rules)
        rules = {item["rule"] for item in agent._preexec_diagnosis("tests/test_all.py", "full_stack_ssr")}
        self.assertNotIn("spa_service_self_managed", rules)


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
