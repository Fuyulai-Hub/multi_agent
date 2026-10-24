"""交付意图路由 + A/B 档支持的单元测试。

对应 docs/15-前后端交付意图路由与AB档/01-技术方案.md：
- L1/L3：``mas/intent.py`` 的确定性预分类与非法值兜底（P0）；
- A 档补丁 1：reviewer 按 delivery_shape 把 network_import 降为 info，且豁免不外溢；
- A 档补丁 3/4/5：Web 服务生命周期三条静态规则 + 前端产物静态证据；
- A 档补丁 2：tester 的 ``_verification_gap`` 按形态判定；
- B 档：``ui_evidence`` 在**任何形态**下都发布（否则 expr 的 BoolOp 不短路求值
  会让引用它的 criterion 整条判失败，老任务被批量误伤）；
- 门禁表达式：两条 criterion 在三值形态下的真实求值结果。

每个用例标注了它守护的文档验收标准编号（见文档 §8）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.product import ProductAgent
from mas.agents.reviewer import ReviewerAgent
from mas.agents.tester import TesterAgent
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.experience import RetrievalResult
from mas.expr import evaluate
from mas.intent import (
    DEFAULT_DELIVERY_SHAPE,
    DELIVERY_SHAPES,
    classify_delivery_shape,
    normalize_delivery_shape,
    resolve_delivery_shape,
    shape_family,
)
from mas.llm import Generation
from mas.prompts import PromptBuilder
from mas.sandbox import Sandbox
from mas.schemas import AgentResult, NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_WEB_APP_SOURCE = (
    '"""web app package."""\n'
    "import http.server\n"
    "import socket\n"
    "\n"
    "\n"
    "def make_handler() -> type:\n"
    '    """Build request handler."""\n'
    "    return http.server.SimpleHTTPRequestHandler\n"
)


class _CtxMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        workspace = root / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=workspace)
        self.config.runtime["verbose"] = False
        self.run_dir = root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "test-web-delivery")
        self.bus = MessageBus(self.run_dir / "messages.jsonl", self.events, "test-web-delivery")
        self.blackboard = Blackboard(self.run_dir / "artifacts", self.events, "test-web-delivery")
        self.sandbox = Sandbox(self.run_dir / "code", self.config, events=self.events)
        self.prompts = PromptBuilder(self.config, self.run_dir, enabled=True)
        self.ctx = SimpleNamespace(
            config=self.config, events=self.events, run_dir=self.run_dir,
            blackboard=self.blackboard, bus=self.bus, sandbox=self.sandbox,
            prompts=self.prompts, llm=None, tools=None, experience=None,
            log=lambda *a, **k: None, run_state={}, consult=None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _publish_spec(self, **extra):
        spec = {"id": "REQ-WEB", "title": "web 需求",
                "function_points": [{"id": "FP-01"}]}
        spec.update(extra)
        self.blackboard.publish(
            "requirement_spec", spec,
            producer="product", node="intake", iteration=1, summary="test",
        )
        return spec

    def _publish_manifest(self, source: str = _WEB_APP_SOURCE, files=None, package="webapp"):
        if source is not None:
            self.sandbox.write_file("webapp/__init__.py", source)
        manifest_files = files if files is not None else ["webapp/__init__.py"]
        self.blackboard.publish(
            "code_manifest",
            {"package": package, "files": manifest_files, "loc": 8, "functions": []},
            producer="developer", node="implement", iteration=1, summary="test",
        )

    def _events_of(self, type_: str):
        return [item for item in self.events.events if item.type == type_]


# --------------------------------------------------------------------------- L1 / L3
class IntentClassificationTests(unittest.TestCase):
    """L1 规则预分类：确定性、提示性质，不依赖 LLM。"""

    def test_explicit_backend_only_sentence(self):
        hint = classify_delivery_shape("实现一个订单定价库，只做后端，不需要前端")
        self.assertEqual(hint["hint"], "backend_only")
        self.assertTrue(hint["explicit"])

    def test_explicit_fullstack_sentence(self):
        hint = classify_delivery_shape("做一个购物车系统，需要前后端，包含页面")
        self.assertEqual(hint["hint"], "full_stack_ssr")
        self.assertTrue(hint["explicit"])

    def test_feature_words_decide_when_no_explicit_sentence(self):
        self.assertEqual(classify_delivery_shape("提供一组 SDK 接口与命令行工具")["hint"],
                         "backend_only")
        self.assertEqual(classify_delivery_shape("用户点击表单后跳转到列表页面")["hint"],
                         "full_stack_ssr")

    def test_conflict_or_no_hit_returns_unknown(self):
        """冲突/全部未命中一律 unknown 并保留命中词，由 L2 定夺（文档 §3.2）。"""
        hint = classify_delivery_shape("既提供接口，也需要在浏览器里点击表单")
        self.assertEqual(hint["hint"], "unknown")
        self.assertTrue(hint["keywords"])
        self.assertEqual(classify_delivery_shape("随便写点什么")["hint"], "unknown")

    def test_normalize_accepts_three_shapes_and_legacy_mode(self):
        for raw, expected in [
            ("backend_only", "backend_only"), ("FULL_STACK_SSR", "full_stack_ssr"),
            ("  full_stack_spa ", "full_stack_spa"),
            ("library", "backend_only"), ("web_fullstack", "full_stack_ssr"),
            ("desktop", None), ("", None), (None, None),
        ]:
            self.assertEqual(normalize_delivery_shape(raw), expected, f"raw={raw!r}")

    def test_resolve_falls_back_to_default(self):
        """缺失或非法 → backend_only + source=default（文档 §3.2 L3）。"""
        for spec in ({}, {"delivery_shape": "garbage"}, {"delivery_shape": None},
                     {"delivery_mode": "unknown_mode"}):
            shape, source, fell_back = resolve_delivery_shape(spec)
            self.assertEqual(shape, DEFAULT_DELIVERY_SHAPE, f"spec={spec}")
            self.assertEqual(source, "default")
            self.assertTrue(fell_back)

    def test_legacy_mode_is_translated_not_dropped(self):
        shape, source, fell_back = resolve_delivery_shape({"delivery_mode": "web_fullstack"})
        self.assertEqual(shape, "full_stack_ssr")
        self.assertEqual(source, "legacy_mode")
        self.assertFalse(fell_back)

    def test_shape_family(self):
        self.assertEqual(shape_family("backend_only"), "backend")
        self.assertEqual(shape_family("full_stack_ssr"), "full_stack")
        self.assertEqual(shape_family("full_stack_spa"), "full_stack")
        self.assertEqual(shape_family("乱写"), "backend")   # 未知值取安全侧


class ProductRoutingTests(_CtxMixin, unittest.TestCase):
    """L3：落黑板前字段恒定存在 + 非法值兜底并告警（验收标准 #2）。"""

    def _apply_product(self, shape=None):
        agent = ProductAgent(self.ctx)
        spec = {
            "id": "REQ-WEB", "title": "web 需求", "package": "web_shop",
            "function_points": [{
                "id": "FP-01", "title": "页面",
                "samples": [{"input": "GET /", "expect": "200"}],
                # 需求契约（门禁加固）：原文只有一行 → 唯一条款 C-01
                "acceptance_criteria": ["GET / 返回 200"],
                "source_spans": [{"clause_id": "C-01"}],
            }],
        }
        if shape is not None:
            spec["delivery_shape"] = shape
        self.blackboard.publish("requirement_source", {"text": "带页面的购物车"},
                                producer="leader", node="intake", iteration=0, summary="t")
        task = NodeTask(node_id="intake", title="需求评估", role="product", kind="task",
                        iteration=1)
        from mas.agents.base import Perception
        result = AgentResult()
        agent.apply(task, Generation(data=spec), Perception(), {}, result)
        return result, spec

    def test_missing_field_is_injected_as_default(self):
        """无 delivery_shape 的需求：注入 backend_only，字段恒定存在。"""
        result, spec = self._apply_product(None)
        self.assertEqual(result.status, "pass")
        self.assertEqual(spec["delivery_shape"], DEFAULT_DELIVERY_SHAPE)
        published = self.blackboard.require(["requirement_spec"])[0]["requirement_spec"]
        self.assertEqual(published["delivery_shape"], DEFAULT_DELIVERY_SHAPE)
        self.assertEqual(len(self._events_of("intent_fallback")), 1)

    def test_illegal_values_all_fall_back(self):
        """多种非法输入全部落到 backend_only 且留有 intent_fallback 事件（验收标准 #2）。"""
        for illegal in ("", "WEBApp", "spa", 42):
            self.setUp()
            try:
                _result, spec = self._apply_product(illegal)
                self.assertEqual(spec["delivery_shape"], DEFAULT_DELIVERY_SHAPE, f"raw={illegal!r}")
                self.assertEqual(len(self._events_of("intent_fallback")), 1, f"raw={illegal!r}")
            finally:
                self.tearDown()

    def test_legal_shapes_are_preserved(self):
        for shape in DELIVERY_SHAPES:
            self.setUp()
            try:
                _result, spec = self._apply_product(shape.upper())
                self.assertEqual(spec["delivery_shape"], shape)
                self.assertEqual(self._events_of("intent_fallback"), [])
            finally:
                self.tearDown()

    def test_l1_hint_recorded_in_context(self):
        agent = ProductAgent(self.ctx)
        self.blackboard.publish("requirement_source", {"text": "做一个带页面的前后端购物车"},
                                producer="leader", node="intake", iteration=0, summary="t")
        task = NodeTask(node_id="intake", title="需求评估", role="product", kind="task", iteration=1)
        context = agent.build_context(task, agent.perceive(task))
        self.assertEqual(context["intent_hint"]["hint"], "full_stack_ssr")
        self.assertEqual(len(self._events_of("intent_classified")), 1)


# --------------------------------------------------------------------------- A 档补丁 1
class NetworkExemptionTests(_CtxMixin, unittest.TestCase):
    """网络模块豁免只在该豁免的形态生效（验收标准 #3）。"""

    def _findings(self, shape):
        self._publish_spec(**({"delivery_shape": shape} if shape else {}))
        self._publish_manifest()
        agent = ReviewerAgent(self.ctx)
        task = NodeTask(node_id="review", title="代码审查", role="reviewer", kind="task", iteration=1)
        return agent.build_context(task, agent.perceive(task))

    def test_backend_only_keeps_network_import_major(self):
        context = self._findings("backend_only")
        network = [f for f in context["static_analysis"]["findings"]
                   if f["rule"] == "network_import"]
        self.assertTrue(network, "import http.server/socket 必须仍被检出（可见性不丢）")
        self.assertTrue(all(f["severity"] == "major" for f in network))
        blocking = [f for f in network
                    if f["severity"] in context["policies"]["blocking_severities"]]
        self.assertEqual(len(blocking), len(network))

    def test_missing_shape_keeps_major_zero_regression(self):
        """历史需求没有 delivery_shape 字段 → 行为与改造前完全一致（验收标准 #1）。"""
        context = self._findings(None)
        network = [f for f in context["static_analysis"]["findings"]
                   if f["rule"] == "network_import"]
        self.assertTrue(all(f["severity"] == "major" for f in network))

    def test_full_stack_shapes_downgrade_to_info(self):
        for shape in ("full_stack_ssr", "full_stack_spa"):
            self.setUp()
            try:
                context = self._findings(shape)
                network = [f for f in context["static_analysis"]["findings"]
                           if f["rule"] == "network_import"]
                self.assertTrue(network, f"{shape}: 可见性必须保留")
                self.assertTrue(all(f["severity"] == "info" for f in network), shape)
                blocking = [f for f in network
                            if f["severity"] in context["policies"]["blocking_severities"]]
                self.assertEqual(blocking, [])
                self.assertEqual(len(self._events_of("network_finding_relaxed")), 1)
            finally:
                self.tearDown()

    def test_exemption_does_not_touch_other_rules(self):
        """只降 network_import 一条：eval_usage 在 full_stack 下仍是 critical（验收标准 #3）。"""
        self._publish_spec(delivery_shape="full_stack_ssr")
        self._publish_manifest(source=(
            '"""web app."""\n'
            "import http.server\n\n\n"
            "def run(code: str) -> object:\n"
            '    """Run."""\n    return eval(code)\n'))
        agent = ReviewerAgent(self.ctx)
        task = NodeTask(node_id="review", title="代码审查", role="reviewer", kind="task", iteration=1)
        context = agent.build_context(task, agent.perceive(task))
        by_rule = {f["rule"]: f["severity"] for f in context["static_analysis"]["findings"]}
        self.assertEqual(by_rule.get("eval_usage"), "critical")
        self.assertEqual(by_rule.get("network_import"), "info")


# --------------------------------------------------------------------------- A 档补丁 3/4/5
class WebStaticRuleTests(_CtxMixin, unittest.TestCase):
    """端口字面量 / 服务线程 / 前端产物三类静态证据（验收标准 #4 #7）。"""

    def _analyze_code(self, shape, source, web=None):
        self._publish_spec(delivery_shape=shape)
        files = ["webapp/__init__.py"] + list(web or {})
        self._publish_manifest(source=source, files=files)
        for name, content in (web or {}).items():
            self.sandbox.write_file(name, content)
        agent = ReviewerAgent(self.ctx)
        task = NodeTask(node_id="review", title="代码审查", role="reviewer", kind="task", iteration=1)
        return agent.build_context(task, agent.perceive(task))["static_analysis"]["findings"]

    def test_web_lifecycle_rules_off_for_backend_only(self):
        """backend_only 下三条规则完全不检出（行为零变更，验收标准 #1）。"""
        source = ('"""app."""\n'
                  "import threading\n"
                  "from http.server import ThreadingHTTPServer\n\n\n"
                  "def build() -> object:\n"
                  '    """Build."""\n'
                  "    srv = ThreadingHTTPServer((\"127.0.0.1\", 8080), object)\n"
                  "    threading.Thread(target=srv.serve_forever).start()\n"
                  "    return srv\n")
        rules = {f["rule"] for f in self._analyze_code("backend_only", source)}
        self.assertFalse(rules & {"web_port_literal", "web_service_not_daemon",
                                  "web_service_lifecycle"})

    def test_port_literal_detected_in_full_stack(self):
        source = ('"""app."""\n'
                  "import threading\n"
                  "from http.server import ThreadingHTTPServer\n\n\n"
                  "def build() -> object:\n"
                  '    """Build."""\n'
                  "    srv = ThreadingHTTPServer((\"127.0.0.1\", 8080), object)\n"
                  "    threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
                  "    return srv\n")
        rules = {f["rule"] for f in self._analyze_code("full_stack_ssr", source)}
        self.assertIn("web_port_literal", rules)

    def test_port_zero_clears_port_literal(self):
        """改成端口 0 后该 finding 消失（验收标准 #4）。"""
        source = ('"""app."""\n'
                  "import threading\n"
                  "from http.server import ThreadingHTTPServer\n\n\n"
                  "def build() -> object:\n"
                  '    """Build."""\n'
                  "    srv = ThreadingHTTPServer((\"127.0.0.1\", 0), object)\n"
                  "    threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
                  "    return srv\n")
        rules = {f["rule"] for f in self._analyze_code("full_stack_ssr", source)}
        self.assertNotIn("web_port_literal", rules)
        self.assertNotIn("web_service_not_daemon", rules)

    def test_daemon_keyword_missing_is_flagged(self):
        source = ('"""app."""\n'
                  "import threading\n\n\n"
                  "def start() -> object:\n"
                  '    """Start."""\n'
                  "    return threading.Thread(target=print)\n")
        rules = {f["rule"] for f in self._analyze_code("full_stack_ssr", source)}
        self.assertIn("web_service_not_daemon", rules)

    def test_module_level_serve_forever_flagged_but_main_guard_ok(self):
        bad = ('"""app."""\n'
               "import threading\n"
               "from http.server import ThreadingHTTPServer\n\n"
               "srv = ThreadingHTTPServer((\"127.0.0.1\", 0), object)\n"
               "threading.Thread(target=srv.serve_forever, daemon=True).start()\n")
        self.assertIn("web_service_lifecycle",
                      {f["rule"] for f in self._analyze_code("full_stack_ssr", bad)})
        good = ('"""app."""\n'
                "import threading\n"
                "from http.server import ThreadingHTTPServer\n\n\n"
                "def main() -> None:\n"
                '    """Main."""\n'
                "    srv = ThreadingHTTPServer((\"127.0.0.1\", 0), object)\n"
                "    threading.Thread(target=srv.serve_forever, daemon=True).start()\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    main()\n")
        self.assertNotIn("web_service_lifecycle",
                         {f["rule"] for f in self._analyze_code("full_stack_ssr", good)})

    def test_html_placeholder_and_missing_asset(self):
        """未替换占位符与缺失资源必须被检出（验收标准 #7）。"""
        web = {"webapp/page.html": "<html><body><h1>{{ title }}</h1>"
                                   "<link rel=\"stylesheet\" href=\"missing.css\">"
                                   "</body></html>"}
        rules = {f["rule"] for f in self._analyze_code("full_stack_ssr", None, web)}
        self.assertIn("html_placeholder_unresolved", rules)
        self.assertIn("html_asset_missing", rules)

    def test_clean_html_has_no_placeholder(self):
        self.sandbox.write_file("webapp/style.css", "body { margin: 0; }\n")
        web = {"webapp/page.html": "<html><body><h1>待办清单</h1>"
                                   "<link rel=\"stylesheet\" href=\"style.css\">"
                                   "</body></html>"}
        rules = {f["rule"] for f in self._analyze_code("full_stack_ssr", None, web)}
        self.assertNotIn("html_placeholder_unresolved", rules)
        self.assertNotIn("html_asset_missing", rules)

    def test_js_bracket_balance_is_weak_but_flagged(self):
        bad = {"webapp/app.js": "function f() { return 1;\n"}
        self.assertIn("js_syntax_error",
                      {f["rule"] for f in self._analyze_code("full_stack_ssr", None, bad)})
        good = {"webapp/app.js": "function f() { return 1; }\n"}
        self.assertNotIn("js_syntax_error",
                         {f["rule"] for f in self._analyze_code("full_stack_ssr", None, good)})


# --------------------------------------------------------------------------- A 档补丁 2
class VerificationGapTests(_CtxMixin, unittest.TestCase):
    """``_verification_gap`` 按交付形态判定（验收标准 #8）。"""

    def _gap(self, language, shape=None, probe=None):
        agent = TesterAgent(self.ctx)
        spec = {"delivery_shape": shape} if shape else {}
        return agent._verification_gap({"tech_choice": {"language": language}}, probe, spec)

    def test_backend_only_keeps_legacy_behaviour(self):
        self.assertEqual(self._gap("Python 3.9+", "backend_only")["gap"], 0)
        self.assertEqual(self._gap("C++17", "backend_only")["gap"], 1)
        self.assertEqual(self._gap("", "backend_only")["gap"], 0)

    def test_ssr_does_not_depend_on_string_coincidence(self):
        """'React + Python' 与 'Python 3.9+' 都可验证，'TypeScript + React' 如实报缺口。"""
        self.assertEqual(self._gap("React + Python", "full_stack_ssr")["gap"], 0)
        self.assertEqual(self._gap("Python 3.9+", "full_stack_ssr")["gap"], 0)
        self.assertEqual(self._gap("TypeScript + React", "full_stack_ssr")["gap"], 1)

    def test_spa_reports_gap_honestly(self):
        """验收标准 #8：SPA 未接浏览器通道必须 gap=1，不得出现假通过。"""
        for language in ("Python 3.9+", "React + Python", ""):
            with self.subTest(language=language):
                self.assertEqual(self._gap(language, "full_stack_spa")["gap"], 1)

    def test_python_plus_js_depends_on_probe(self):
        self.assertEqual(self._gap("Python + JavaScript", "full_stack_ssr",
                                   {"javascript_ready": True})["gap"], 0)
        self.assertEqual(self._gap("Python + JavaScript", "full_stack_ssr",
                                   {"javascript_ready": False})["gap"], 1)

    def test_backward_compatible_signature(self):
        """旧调用形式（只传 solution_design）不受影响。"""
        agent = TesterAgent(self.ctx)
        self.assertEqual(agent._verification_gap({"tech_choice": {"language": "C++17"}})["gap"], 1)
        self.assertEqual(agent._verification_gap({})["gap"], 0)
        self.assertEqual(agent._verification_gap(None)["gap"], 0)


# --------------------------------------------------------------------------- B 档 ui_evidence
class UiEvidenceTests(_CtxMixin, unittest.TestCase):
    """``ui_evidence`` 必须在任何形态下发布（文档 §2.3 前置条件 2）。"""

    def _evidence(self, shape, data=None, summary=None, probe=None):
        agent = TesterAgent(self.ctx)
        return agent._collect_ui_evidence(
            data or {}, summary if summary is not None else {"failed": 0},
            probe or {}, {}, shape)

    def test_backend_only_publishes_disabled_evidence(self):
        ev = self._evidence("backend_only")
        self.assertFalse(ev["enabled"])
        self.assertEqual(ev["failed_asserts"], 0)
        self.assertEqual(ev["asserts"], [])

    def test_ssr_evidence_is_enabled_but_not_fabricated(self):
        ev = self._evidence("full_stack_ssr")
        self.assertTrue(ev["enabled"])
        self.assertEqual(ev["mode"], "ssr")
        self.assertEqual(ev["failed_asserts"], 0)
        # SSR 的 UI 证据来自响应体断言，不是浏览器采集——不得谎报 channel
        self.assertEqual(ev["channel"], "")

    def test_ssr_failed_asserts_track_test_failures(self):
        ev = self._evidence("full_stack_ssr", summary={"failed": 3})
        self.assertEqual(ev["failed_asserts"], 3)

    def test_spa_without_browser_is_insufficient_evidence(self):
        """B 档未完成时如实失败（验收标准 #8）。"""
        ev = self._evidence("full_stack_spa")
        self.assertEqual(ev["failed_asserts"], 1)
        self.assertEqual(ev["asserts"], [])
        self.assertTrue(ev["error"])

    def test_spa_browser_ready_but_no_checks_still_fails(self):
        """浏览器就绪但契约没给 ui_checks → 无可执行清单，按证据不足判失败。"""
        ev = self._evidence("full_stack_spa", data={},
                            probe={"browser_ui_ready": True, "ui_server": {"available": True}})
        with_ui_probe = self.config.runtime.get("ui_probe") or {}
        if not with_ui_probe.get("enabled"):
            # ui_probe 未启用 → 通道不可得，同样判失败（两种原因都通向同一结论）
            self.assertEqual(ev["failed_asserts"], 1)
        self.assertEqual(ev["asserts"], [])

    def test_unknown_shape_treated_as_backend(self):
        ev = self._evidence("乱写的形态")
        self.assertFalse(ev["enabled"])


# --------------------------------------------------------------------------- 门禁表达式
class GateExpressionTests(unittest.TestCase):
    """门禁 criterion 在三值形态下的真实求值结果（表达式已切到文件探测口径）。"""

    _ASSERT_ZERO = ("ui_evidence.delivery_shape != 'full_stack_spa' "
                    "or ui_evidence.failed_asserts == 0")
    _NON_EMPTY = ("ui_evidence.delivery_shape != 'full_stack_spa' "
                  "or len(ui_evidence.asserts) > 0")

    @staticmethod
    def _eval(expr, shape, ui):
        snapshot = {"requirement_spec": {"delivery_shape": shape}}
        if ui is not None:
            snapshot["ui_evidence"] = {**ui, "delivery_shape": shape}
        return evaluate(expr, snapshot)

    def test_backend_only_passes_with_disabled_evidence(self):
        ui = {"enabled": False, "failed_asserts": 0, "asserts": []}
        self.assertTrue(bool(self._eval(self._ASSERT_ZERO, "backend_only", ui)))
        self.assertTrue(bool(self._eval(self._NON_EMPTY, "backend_only", ui)))

    def test_ssr_passes(self):
        ui = {"enabled": True, "failed_asserts": 0, "asserts": []}
        self.assertTrue(bool(self._eval(self._ASSERT_ZERO, "full_stack_ssr", ui)))
        self.assertTrue(bool(self._eval(self._NON_EMPTY, "full_stack_ssr", ui)))

    def test_spa_with_asserts_all_passed(self):
        ui = {"enabled": True, "failed_asserts": 0,
              "asserts": [{"name": "导航", "status": "passed"}]}
        self.assertTrue(bool(self._eval(self._ASSERT_ZERO, "full_stack_spa", ui)))
        self.assertTrue(bool(self._eval(self._NON_EMPTY, "full_stack_spa", ui)))

    def test_spa_empty_asserts_fails_by_insufficient_evidence(self):
        """空证据不得判通过（文档 §5 前置 1）。"""
        ui = {"enabled": True, "failed_asserts": 0, "asserts": []}
        self.assertTrue(bool(self._eval(self._ASSERT_ZERO, "full_stack_spa", ui)))
        self.assertFalse(bool(self._eval(self._NON_EMPTY, "full_stack_spa", ui)))

    def test_spa_failed_asserts_fails(self):
        ui = {"enabled": True, "failed_asserts": 2,
              "asserts": [{"name": "x", "status": "failed"}]}
        self.assertFalse(bool(self._eval(self._ASSERT_ZERO, "full_stack_spa", ui)))

    def test_missing_artifact_proves_injection_is_mandatory(self):
        """反证：没有 ui_evidence 时连 backend_only 都会失败——这正是必须无条件发布的原因。"""
        result = self._eval(self._ASSERT_ZERO, "backend_only", None)
        self.assertFalse(bool(result))
        self.assertIn("字段缺失", result.error)


class RealFlowGateTests(_CtxMixin, unittest.TestCase):
    """用 config/flow.yaml 里**真实**的 criterion 表达式跑黑板快照（不是重写一遍表达式）。

    这是最接近线上验收判定的确定性验证：expr 求值器、BoolOp 的求值语义、配置的
    criterion 集合三者都是真的，只有 artifact 是构造的。
    """

    def _publish_artifacts(self, shape, failed_asserts=0, asserts=None, has_ui=True,
                           probe_version="", http_status=0):
        self._publish_spec(delivery_shape=shape)
        if has_ui:
            self.blackboard.publish(
                "ui_evidence",
                {"enabled": True, "failed_asserts": failed_asserts,
                 "asserts": asserts if asserts is not None else [],
                 # 门禁 SPA 条件引用的字段必须齐全（BoolOp 不短路，缺字段整条判失败）
                 "delivery_shape": shape, "probe_version": probe_version,
                 "http_status": http_status},
                producer="tester", node="test", iteration=1, summary="t")

    def _evaluate_gate(self, profile=None):
        config = load_config(PROJECT_ROOT / "config", workspace=self.run_dir / "ws",
                             profile=profile)
        snapshot = self.blackboard.snapshot()
        results = {}
        for criterion in config.flow.nodes["acceptance"].criteria:
            outcome = evaluate(criterion.expr, snapshot)
            results[criterion.id] = bool(outcome.ok and outcome.value)
        return results

    def _publish_core_artifacts(self, shape, *, with_ui_evidence=True, **ui_kwargs):
        self._publish_spec(delivery_shape=shape)
        self.blackboard.publish(
            "test_report",
            {"summary": {"failed": 0},
             "coverage": {"functional": {"rate": 1.0}, "exception": {"rate": 1.0},
                          "boundary": {"rate": 1.0}},
             # docs/37 P0-2 新增的两条 acceptance criterion 只读这份凭据摘要；
             # 真实签名链路由 tests/test_verifier.py 覆盖，这里构造已通过的门禁快照。
             "verification_attestation": {"valid": True, "test_hash": "fixture-digest"},
             "negotiation": {"open": 0}},
            producer="tester", node="test", iteration=1, summary="t")
        self.blackboard.publish("bug_reports", {"open": 0},
                                producer="tester", node="test", iteration=1, summary="t")
        self.blackboard.publish("review_report", {"blocking": 0},
                                producer="reviewer", node="review", iteration=1, summary="t")
        self.blackboard.publish("solution_design", {"evaluation": {"selected": "方案A"}},
                                producer="developer", node="solution", iteration=1, summary="t")
        if with_ui_evidence:
            self._publish_artifacts(shape, **ui_kwargs)

    def test_backend_only_passes_when_evidence_published(self):
        """真实链路下 tester 无条件发布 ui_evidence → backend_only 老任务全绿（验收标准 #1 #9）。"""
        self._publish_core_artifacts("backend_only")
        results = self._evaluate_gate()
        for cid, passed in results.items():
            self.assertTrue(passed, f"criterion 未通过: {cid}")

    def test_missing_ui_evidence_would_break_even_backend_only(self):
        """反证：无条件发布 ui_evidence 是承重要素，不是防御性代码（文档 §2.3）。

        expr 的 BoolOp 先求全部值再 any/all：**只要 ui_evidence 缺失，连 backend_only
        都会被这条 criterion 判失败**。所以 tester.apply 里的无条件 publish 一旦回退，
        全部历史任务的验收都会掉——这条用例就是它的回归防线。
        """
        self._publish_core_artifacts("backend_only", with_ui_evidence=False)
        results = self._evaluate_gate()
        self.assertFalse(results["Web UI 断言无失败（仅 SPA 形态要求）"],
                         "预期：ui_evidence 缺失时该 criterion 判失败")
        # 其余不受影响（豁免/isolation 未外溢）
        for cid, passed in results.items():
            if not cid.startswith(("Web UI", "SPA")):
                self.assertTrue(passed, f"非 UI criterion 被连带影响: {cid}")

    def test_ssr_passes_ui_criteria(self):
        self._publish_artifacts("full_stack_ssr")
        results = self._evaluate_gate()
        for cid in ("Web UI 断言无失败（仅 SPA 形态要求）", "Web UI 证据非空（SPA 形态下不得证据不足）"):
            self.assertTrue(results[cid], cid)

    def test_spa_empty_evidence_fails_gate(self):
        """B 档命中但无浏览器证据 → 门禁如实判失败（验收标准 #8，诚实性底线）。"""
        self._publish_artifacts("full_stack_spa", asserts=[])
        results = self._evaluate_gate()
        self.assertTrue(results["Web UI 断言无失败（仅 SPA 形态要求）"])
        self.assertFalse(results["Web UI 证据非空（SPA 形态下不得证据不足）"])

    def test_spa_with_real_asserts_passes_ui_criteria(self):
        self._publish_artifacts("full_stack_spa", asserts=[{"name": "导航到首页", "status": "passed"}],
                                probe_version="uiprober/2", http_status=200)
        results = self._evaluate_gate()
        for cid in ("Web UI 断言无失败（仅 SPA 形态要求）",
                    "Web UI 证据非空（SPA 形态下不得证据不足）",
                    "SPA 浏览器证据由框架 probe 采集",
                    "SPA 证据含真实 HTTP 状态"):
            self.assertTrue(results[cid], cid)

    def test_spa_shell_evidence_without_probe_fails_gate(self):
        """agent 自填的空壳证据（只有 failed_asserts: 0 + asserts，没有 probe 版本和
        真实 HTTP 状态）不得通过门禁——证据必须由框架 probe 采集。"""
        self._publish_artifacts("full_stack_spa", asserts=[{"name": "导航", "status": "passed"}])
        results = self._evaluate_gate()
        self.assertTrue(results["Web UI 断言无失败（仅 SPA 形态要求）"])
        self.assertTrue(results["Web UI 证据非空（SPA 形态下不得证据不足）"])
        self.assertFalse(results["SPA 浏览器证据由框架 probe 采集"])
        self.assertFalse(results["SPA 证据含真实 HTTP 状态"])

    def test_spa_probe_with_bad_http_status_fails_gate(self):
        self._publish_artifacts("full_stack_spa", asserts=[{"name": "导航", "status": "passed"}],
                                probe_version="uiprober/2", http_status=500)
        results = self._evaluate_gate()
        self.assertFalse(results["SPA 证据含真实 HTTP 状态"])

    def test_spa_failed_asserts_fails_gate(self):
        self._publish_artifacts("full_stack_spa", failed_asserts=2,
                                asserts=[{"name": "x", "status": "failed"}])
        results = self._evaluate_gate()
        self.assertFalse(results["Web UI 断言无失败（仅 SPA 形态要求）"])


# --------------------------------------------------------------------------- A 档执行协议
class WebProtocolInjectionTests(_CtxMixin, unittest.TestCase):
    """tester 按形态注入 Web 执行协议。"""

    def _bundle(self, shape):
        self._publish_spec(**({"delivery_shape": shape} if shape else {}))
        self.blackboard.publish(
            "code_manifest", {"package": "todoweb", "files": [], "loc": 0, "functions": []},
            producer="developer", node="implement", iteration=1, summary="test",
        )
        agent = TesterAgent(self.ctx)
        task = NodeTask(node_id="test", title="业务测试", role="tester", kind="task", iteration=1)
        return agent.build_prompt(task, agent.perceive(task), RetrievalResult())

    def test_full_stack_injects_protocol(self):
        for shape in ("full_stack_ssr", "full_stack_spa"):
            self.setUp()
            try:
                bundle = self._bundle(shape)
                self.assertIn("Web 交付执行协议", bundle.user)
                self.assertIn("ThreadingHTTPServer", bundle.user)
                self.assertIn("from todoweb import build_handler", bundle.user)
                self.assertIn("沙箱超时", bundle.user)
                self.assertIn("Web 交付执行协议", bundle.path.read_text(encoding="utf-8"))
            finally:
                self.tearDown()

    def test_browser_protocol_only_for_spa(self):
        """ui_service_cmd 的默认值 `python serve.py` 只应在 SPA 形态出现。

        无条件下发会诱导模型在 files 里输出 serve.py，而 tester 的 can_write_code=false，
        写入即抛 SandboxError 让整个测试节点崩溃（web_todo_app 真实运行实测 iter1/iter2 全灭）。
        """
        self.setUp()
        try:
            bundle = self._bundle("full_stack_ssr")
            self.assertNotIn("ui_service_cmd", bundle.user)
            self.assertNotIn("MAS_UI_PORT", bundle.user)
        finally:
            self.tearDown()
        self.setUp()
        try:
            bundle = self._bundle("full_stack_spa")
            self.assertIn("ui_service_cmd", bundle.user)
            self.assertIn("MAS_UI_PORT", bundle.user)
        finally:
            self.tearDown()

    def test_contract_does_not_advertise_service_file(self):
        """静态契约里不得出现 ui_service_cmd/ui_checks 键（它们由协议文本按形态注入）。"""
        contract = TesterAgent.contracts["default"]
        self.assertNotIn("ui_service_cmd", contract)
        self.assertNotIn("ui_checks", contract)

    def test_denied_file_write_does_not_crash_node(self):
        """契约诱导输出 serve.py 时：跳过该文件并记账，不能让整个节点崩掉。"""
        self._publish_spec(delivery_shape="full_stack_ssr")
        self._publish_manifest()
        agent = TesterAgent(self.ctx)
        task = NodeTask(node_id="test", title="业务测试", role="tester", kind="task", iteration=1)
        from mas.agents.base import Perception
        result = AgentResult()
        data = {
            "files": [
                {"path": "serve.py", "content": "print('MAS_UI_PORT=0')\n"},
                {"path": "tests/test_x.py", "content": "x = 1\n"},
            ],
            "cases": [], "case_count": {},
        }
        agent.apply(task, Generation(data=data), agent.perceive(task), {}, result)
        denied = self._events_of("test_file_write_denied")
        self.assertEqual(len(denied), 1, "被拒绝的写入必须留下事件")
        self.assertEqual(denied[0].payload["path"], "serve.py")
        self.assertTrue(self.sandbox.exists("tests/test_x.py"), "合法测试文件仍应写入")

    def test_backend_only_prompt_unchanged(self):
        for shape in ("backend_only", None):
            self.setUp()
            try:
                bundle = self._bundle(shape)
                self.assertNotIn("Web 交付执行协议", bundle.user)
                self.assertNotIn("ThreadingHTTPServer", bundle.user)
            finally:
                self.tearDown()


if __name__ == "__main__":
    unittest.main()
