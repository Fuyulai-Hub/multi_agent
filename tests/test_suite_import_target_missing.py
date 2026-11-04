"""「测试套件 import 的被测包在工作区里不存在」的静态检出与归类。

背景（run task12-foodlink-spa-03 真实全链路续跑，2026-11-03 起跑 → 2026-11-04 收尾）：
环境补齐后（PostgreSQL 起着、依赖装完、prisma db push + seed、后端冷启动 3.0s 返 200），
iter03 用新加的 `spawn_unread_pipe` 规则把坏套件解冻了，这是修好了的部分；但重生成的
套件换了一种方式全败——39 条集体挂在

    setUpClass (tests.test_all.TestFP01Auth): RuntimeError:
    foodlink.build_handler 不可用: ModuleNotFoundError("No module named 'foodlink'")

原因是框架自己下发的协议：`mas/agents/tester.py` 的 `_WEB_PROTOCOL` 只有
「同进程起一个 Python 服务」一种形态（`from {package_hint} import build_handler`，
`package_hint` 取 `code_manifest.package`），而 task12 的交付是 NestJS + Prisma +
PostgreSQL + Vue3，工作区里没有任何 Python 包。提示词留痕实测可查：
`prompts/test#4-tester-031238.md` 里注入的模板首行就是 `from foodlink import build_handler`。

真正要修的是归因（本文件锁住的行为）。iter05/iter06 的 `root_cause_category` 是**空串**：
`environment_or_build`（依赖数据库都在）、`test_suite_defect`（旧规则表里没有这一条）、
`delivery_unverifiable`（没命中）都不是，空分类的默认落点是「交付缺陷」，于是连续三轮
把「让 `import foodlink` 成立」这件研发不可能完成的事派给 developer。attempt 3 的真实
后果（`code/` 里 08:32:26 的产物）：developer 不再修 `stats.service.ts` 那两处 TS2345，
而是往交付里新写了一个 Python 包 `code/foodlink/`（`code_manifest` 从 57 文件的
TypeScript 变成 18 文件的 Python），**证据通道的形状把交付物的语言改了**；
新实现自己又报 `NameError: _get_docs`，39 条仍然全挂。

同类缺陷此前已经出现过两次（裸名 `npm`、无人读的 PIPE），共同点都是
「失败信息指向交付物，缺陷其实写在测试文件里」，所以处置也一致：
静态检出 → 归 `test_suite_defect` → 冻结前拦下交回生成方修，不得删用例。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.tester import TesterAgent
from mas.config import load_config
from mas.sandbox import Sandbox

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: task12 iter04/05/06 冻结套件的真实形状：只 import 契约声明的包名，工作区里没有它
_MISSING_SRC = '''"""t。"""
import http.client
from foodlink import build_handler


class TestFP01Auth:
    """t。"""

    @classmethod
    def setUpClass(cls):
        """setUpClass。"""
        cls.app = build_handler()
'''

#: 修法一：研发真的交付了这个 Python 包
_PACKAGE_DELIVERED_SRC = '''"""t。"""
from foodlink import build_handler


def run():
    """run。"""
    return build_handler()
'''

#: 不该被这条规则碰到的三种形状：只 import 标准库、import 另一个存在的第三方名、
#: 以及被测包名恰好是标准库名（判定保守，一律不报）
_STDLIB_ONLY_SRC = '''"""t。"""
import http.client


def ping():
    """ping。"""
    return http.client.HTTPConnection("127.0.0.1", 8080)
'''

#: 两条缺陷同时存在时（iter03 那份的真实形状）：裸名 npm + 无人读 PIPE + 不存在的 import
_BOTH_SRC = '''"""t。"""
import subprocess
from foodlink import build_handler


def start():
    """start。"""
    return subprocess.Popen(["npm", "run", "start:dev"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
'''


def _sandbox(tmp: Path) -> Sandbox:
    config = load_config(PROJECT_ROOT / "config", workspace=tmp / "ws")
    return Sandbox(tmp / "code", config)


class SuiteImportTargetFindingTests(unittest.TestCase):
    """静态检出本身：该报的报，不该报的绝不报。"""

    def test_detects_import_of_package_missing_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sandbox = _sandbox(tmp)
            sandbox.write_file("tests/test_all.py", _MISSING_SRC)
            findings = sandbox.suite_import_target_findings(
                "tests/test_all.py", "foodlink", lambda rule, default="minor": default)
        self.assertEqual([f.rule for f in findings], ["suite_import_target_missing"])
        self.assertEqual(findings[0].severity, "major")
        self.assertEqual(findings[0].line, 3)              # 指到 import 那一行
        self.assertIn("工作区里不存在", findings[0].message)
        self.assertIn("不得删用例", findings[0].message)

    def test_silent_when_package_actually_delivered(self) -> None:
        """研发真交付了这个包（`foodlink/__init__.py` 存在）就不能再报——
        本规则拦的是「无法兑现的 import」，不是「非 Python 交付」。"""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sandbox = _sandbox(tmp)
            sandbox.write_file("foodlink/__init__.py", '"""foodlink。"""\n')
            sandbox.write_file("tests/test_all.py", _PACKAGE_DELIVERED_SRC)
            findings = sandbox.suite_import_target_findings(
                "tests/test_all.py", "foodlink", lambda rule, default="minor": default)
        self.assertEqual(findings, [])

    def test_silent_for_non_tests_files_and_stdlib_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sandbox = _sandbox(tmp)
            # 实现代码里 import 同名包不该被这条规则管（口径只约束测试套件）
            sandbox.write_file("app/main.py", _MISSING_SRC)
            sandbox.write_file("tests/test_all.py", _STDLIB_ONLY_SRC)
            outside = sandbox.suite_import_target_findings(
                "app/main.py", "foodlink", lambda rule, default="minor": default)
            stdlib = sandbox.suite_import_target_findings(
                "tests/test_all.py", "foodlink", lambda rule, default="minor": default)
            # 被测包名恰好是标准库名时不报（保守）
            sandbox.write_file("tests/test_http.py",
                               '"""t。"""\nimport http\n')
            as_stdlib = sandbox.suite_import_target_findings(
                "tests/test_http.py", "http", lambda rule, default="minor": default)
        self.assertEqual(outside, [])
        self.assertEqual(stdlib, [])
        self.assertEqual(as_stdlib, [])

    def test_no_finding_without_declared_package(self) -> None:
        """backend_only 之类没有 package 契约的场景必须完全静默。"""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sandbox = _sandbox(tmp)
            sandbox.write_file("tests/test_all.py", _MISSING_SRC)
            findings = sandbox.suite_import_target_findings(
                "tests/test_all.py", "", lambda rule, default="minor": default)
        self.assertEqual(findings, [])

    def test_ignores_relative_and_dotted_imports_of_other_packages(self) -> None:
        """`from .foodlink import x`（相对导入）和 `import foodlink.utils` 之外的
        第三方名都不该被牵连——只按契约声明的那个根包名判定。"""
        src = '''"""t。"""
from .foodlink import build_handler
from otherpkg import thing


def run():
    """run。"""
    return build_handler(), thing
'''
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sandbox = _sandbox(tmp)
            sandbox.write_file("tests/test_all.py", src)
            findings = sandbox.suite_import_target_findings(
                "tests/test_all.py", "foodlink", lambda rule, default="minor": default)
        self.assertEqual(findings, [])

    def test_reports_together_with_spawn_defects(self) -> None:
        """三条套件缺陷互不遮挡（iter03 那份同时有裸名与 PIPE；这里再加上 import）。"""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sandbox = _sandbox(tmp)
            config = load_config(PROJECT_ROOT / "config", workspace=tmp / "ws")
            sandbox.write_file("tests/test_all.py", _BOTH_SRC)
            policies = dict(config.role("reviewer").policies)
            analysis = sandbox.analyze(["tests/test_all.py"], policies)
            spawn_rules = {f.rule for f in analysis.findings
                           if f.rule in ("windows_spawn_extension", "spawn_unread_pipe")}
            import_findings = sandbox.suite_import_target_findings(
                "tests/test_all.py", "foodlink", lambda rule, default="minor": default)
        self.assertEqual(spawn_rules, {"windows_spawn_extension", "spawn_unread_pipe"})
        self.assertEqual([f.rule for f in import_findings], ["suite_import_target_missing"])


class SuiteImportTargetClassificationTests(unittest.TestCase):
    """归类：检出之后必须落到 test_suite_defect，而不是空串→默认派给研发。"""

    def test_rule_is_declared_repairable_suite_defect(self) -> None:
        self.assertIn("suite_import_target_missing", TesterAgent._SUITE_DEFECT_RULES)

    def test_root_cause_is_test_suite_defect(self) -> None:
        findings = [{"rule": "suite_import_target_missing",
                     "message": "测试套件 import 的被测包 `foodlink` 在工作区里不存在",
                     "file": "tests/test_all.py", "line": 4}]
        cases = [{"class": "TestFP01Auth", "test": "test_functional_login",
                  "message": 'RuntimeError: foodlink.build_handler 不可用: '
                             'ModuleNotFoundError("No module named \'foodlink\'")'}]
        agent = TesterAgent.__new__(TesterAgent)
        verdict = agent._root_cause(findings, cases)
        self.assertEqual(verdict["category"], "test_suite_defect")
        self.assertIn("测试套件缺陷", agent._root_cause_note(findings, verdict))

    def test_empty_category_was_the_blind_spot(self) -> None:
        """锁住修复前的错误行为：这条发现不在规则表里时，归因落到空串/默认档。"""
        findings = [{"rule": "some_unrelated_rule", "message": "x"}]
        agent = TesterAgent.__new__(TesterAgent)
        self.assertEqual(agent._root_cause(findings, [])["category"],
                         "delivery_unverifiable")
        self.assertEqual(agent._root_cause([], [])["category"], "")

    def test_preexec_diagnosis_keeps_the_new_rule(self) -> None:
        """`_preexec_diagnosis` 的过滤白名单必须带上这条，否则检出了也进不了归因。"""
        self.assertIn("suite_import_target_missing", TesterAgent._SUITE_DEFECT_RULES)
        source = (PROJECT_ROOT / "mas" / "agents" / "tester.py").read_text(encoding="utf-8")
        block = source.split("def _preexec_diagnosis", 1)[1].split("@classmethod", 1)[0]
        self.assertIn('"suite_import_target_missing"', block)

    def test_diagnosis_uses_declared_package_and_reports_missing(self) -> None:
        """整条通道串起来：ctx.sandbox + 契约包名 → 发现进 preexec 结果。"""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = load_config(PROJECT_ROOT / "config", workspace=tmp / "ws")
            sandbox = Sandbox(tmp / "code", config)
            sandbox.write_file("tests/test_all.py", _MISSING_SRC)
            agent = TesterAgent.__new__(TesterAgent)
            agent.ctx = SimpleNamespace(
                sandbox=sandbox, config=config,
                blackboard=SimpleNamespace(get=lambda key: {"package": "foodlink"}
                                           if key == "code_manifest" else None))
            findings = agent._preexec_diagnosis("tests/test_all.py")
        self.assertIn("suite_import_target_missing",
                      [str(item.get("rule")) for item in findings])

    def test_diagnosis_silent_when_package_present(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = load_config(PROJECT_ROOT / "config", workspace=tmp / "ws")
            sandbox = Sandbox(tmp / "code", config)
            sandbox.write_file("foodlink/__init__.py", '"""foodlink。"""\n')
            sandbox.write_file("tests/test_all.py", _MISSING_SRC)
            agent = TesterAgent.__new__(TesterAgent)
            agent.ctx = SimpleNamespace(
                sandbox=sandbox, config=config,
                blackboard=SimpleNamespace(get=lambda key: {"package": "foodlink"}
                                           if key == "code_manifest" else None))
            findings = agent._preexec_diagnosis("tests/test_all.py")
        self.assertEqual([item for item in findings
                          if item.get("rule") == "suite_import_target_missing"], [])


if __name__ == "__main__":
    unittest.main()
