"""审查对 tests/ 冻结文件的风格豁免回归测试（task09-miniledger-04 实测死锁）。

背景：测试 Agent 冻结整个 tests/ 目录后，开发者对测试文件的改动会被
verifier.restore_if_changed 还原，tester 复测时也忽略新生成的 tests/ 文件。
若审查把 tests/ 文件上的风格规则（docstring/类型注解/函数长度）计入 blocking，
这些阻塞项在后续迭代中对开发者不可修复，与回退上限构成死锁：
54 条 docstring 阻塞项连续两轮驳回、修复被还原，第三次驳回后整轮失败。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.reviewer import ReviewerAgent
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.sandbox import Sandbox
from mas.schemas import NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_agent(root: Path):
    run_dir = root / "run"
    run_dir.mkdir(parents=True)
    code = run_dir / "code"
    cfg = load_config(PROJECT_ROOT / "config", workspace=root / "ws")
    cfg.runtime["verbose"] = False
    events = EventLog(run_dir / "events.jsonl", "t-review-exempt")
    bb = Blackboard(run_dir / "artifacts", events, "t-review-exempt")
    ctx = SimpleNamespace(
        config=cfg, events=events, run_dir=run_dir, blackboard=bb,
        bus=MessageBus(run_dir / "messages.jsonl", events, "t-review-exempt"),
        sandbox=Sandbox(code, cfg, events=events),
        prompts=None, llm=None, tools=None, experience=None,
        log=lambda *a, **k: None, run_state={}, consult=None, run_id="t-review-exempt",
    )
    return ReviewerAgent(ctx), ctx, events, code, bb


def _write_tree(code: Path) -> None:
    (code / "miniledger").mkdir(parents=True, exist_ok=True)
    (code / "miniledger" / "__init__.py").write_text('"""pkg"""\n', encoding="utf-8")
    (code / "miniledger" / "core.py").write_text(
        '"""core"""\n\n\ndef transfer(a, b):\n    return a + b\n', encoding="utf-8")
    tests = code / "tests"
    tests.mkdir(exist_ok=True)
    # 冻结测试文件：无模块 docstring、无函数 docstring、无类型注解（全是风格问题）
    body = ["import unittest", "", "class T(unittest.TestCase):"]
    for i in range(3):
        body += [f"    def test_case_{i}(self):", "        self.assertTrue(True)"]
    (tests / "test_impl.py").write_text("\n".join(body) + "\n", encoding="utf-8")


def _perception(manifest: dict) -> SimpleNamespace:
    spec = {"function_points": [], "delivery_shape": "backend_only"}
    return SimpleNamespace(
        found={"code_manifest": manifest, "requirement_spec": spec, "test_report": {}},
        spec=spec,
    )


class ReviewTestStyleExemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent, self.ctx, self.events, self.code, self.bb = _make_agent(self.root)
        _write_tree(self.code)
        self.manifest = {"files": ["miniledger/core.py", "tests/test_impl.py"]}
        self.bb.publish("code_manifest", self.manifest,
                        producer="developer", node="implement", iteration=1)
        self.bb.publish("requirement_spec",
                        {"function_points": [], "delivery_shape": "backend_only"},
                        producer="product", node="intake", iteration=1)
        self.task = NodeTask(node_id="review", title="审查", role="reviewer",
                             kind="task", iteration=1)

    def tearDown(self):
        self.tmp.cleanup()

    def _context(self):
        return self.agent.build_context(self.task, _perception(self.manifest))

    def test_tests_style_findings_exempt_from_context(self):
        findings = self._context()["static_analysis"]["findings"]
        residue = [f for f in findings
                   if str(f.get("file", "")).replace("\\", "/").startswith("tests/")
                   and f.get("rule") in (
                       "missing_function_docstring", "missing_module_docstring",
                       "missing_type_hints")]
        self.assertEqual(residue, [])
        exempt_events = [e for e in self.events.events
                         if e.type == "review_test_style_exempt"]
        self.assertEqual(len(exempt_events), 1)
        self.assertGreater(exempt_events[0].payload["dropped"], 0)

    def test_blocking_excludes_test_style_but_keeps_source(self):
        # 源码侧放一个真问题：超长函数（开发者可修复，必须继续阻塞）
        long_fn = "def transfer(a, b):\n" + "    a += 1\n" * 62 + "    return a\n"
        (self.code / "miniledger" / "core.py").write_text(
            '"""core"""\n\n' + long_fn, encoding="utf-8")
        data = {"verdict": "pass", "findings": [], "blocking": 0}
        out = self.agent._recompute_blocking(
            self.task, data, _perception(self.manifest), self._context())
        files = {str(b.get("file", "")) for b in out["blocking_items"]}
        self.assertNotIn("tests/test_impl.py", files)
        self.assertIn("miniledger/core.py", files)
        style_rules = {"missing_function_docstring", "missing_module_docstring",
                       "missing_type_hints", "function_too_long"}
        for item in out["blocking_items"]:
            if item.get("rule") in style_rules:
                self.assertFalse(
                    str(item.get("file", "")).replace("\\", "/").startswith("tests/"),
                    f"tests/ 上的风格问题不应阻塞: {item}")

    def test_danger_rules_on_tests_still_block(self):
        # 危险规则不豁免：测试文件里的裸 except 必须继续阻塞
        (self.code / "tests" / "test_impl.py").write_text(
            '"""tests"""\nimport unittest\n\n\nclass T(unittest.TestCase):\n'
            '    def test_case(self):\n        try:\n            pass\n'
            '        except:\n            pass\n', encoding="utf-8")
        data = {"verdict": "pass", "findings": [], "blocking": 0}
        out = self.agent._recompute_blocking(
            self.task, data, _perception(self.manifest), self._context())
        self.assertTrue(any(
            b.get("rule") == "bare_except" and "tests/" in str(b.get("file", ""))
            for b in out["blocking_items"]))

    def test_exempt_set_configurable(self):
        # 置空豁免集 = 回到旧行为（tests/ 风格问题重新计入）
        self.agent.role.policies["test_style_rules_exempt"] = []
        findings = self._context()["static_analysis"]["findings"]
        self.assertTrue(any(
            str(f.get("file", "")).replace("\\", "/").startswith("tests/")
            and f.get("rule") == "missing_function_docstring"
            for f in findings))


if __name__ == "__main__":
    unittest.main()
