"""Windows 裸名 spawn 缺陷的检出、归类与冻结前修复通道回归。

背景（run task12-foodlink-spa-03 实测，2026-10-31）：LLM 生成的冻结测试里写
``subprocess.Popen(["npm", "run", "start:dev"])``。本机 npm 确实存在
（``F:\\software\\npm.CMD``），但 Windows ``CreateProcess`` 不查 PATHEXT，裸名
``npm`` 必然 ``FileNotFoundError(WinError 2)`` → 33/39 用例集体阻断。

原实现把这一现象归为 ``delivery_unverifiable``（不可修）：leader 据此直接
forced fail 跳到 report，review/fix 段无任何产物；而套件已冻结，
``restore_if_changed()`` 把后续每轮重新生成的文件整份丢弃
（实测 ``test_generation_ignored`` 事件），修复通道彻底关闭 —— 两轮根因完全
不变、缺陷闭环 0、3.35 小时白烧。

本文件锁定四件事：
1. 裸名 spawn .cmd 包装脚本被静态检出为**可修**规则 ``windows_spawn_extension``，
   且不再重复报 ``sandbox_command_bypass``；已写全名（npm.cmd）不误报。
2. 白名单按 PATHEXT 归一：登记 ``npm`` 即放行 ``npm.cmd``。
3. tester 把该规则归为 ``test_suite_defect``（可修），不再归 ``delivery_unverifiable``。
4. leader 在套件已解冻（修复通道打开）时不再判「回退也改不掉」；
   tester 在无 LLM 时按既有先例跳过冻结前体检并留痕，不冻结坏套件。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.tester import TesterAgent
from mas.config import load_config
from mas.leader import Leader
from mas.llm import Generation
from mas.sandbox import Sandbox
from mas.schemas import AgentResult, NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"
#: 本机 npm 是否解析为 .cmd/.bat 包装脚本（决定 Windows 专属用例是否可跑）
NPM_IS_SCRIPT = bool(IS_WINDOWS and shutil.which("npm")
                     and os.path.splitext(shutil.which("npm"))[1].lower() in (".cmd", ".bat"))

_SPAWN_SRC = (
    '"""t。"""\n'
    "import subprocess\n"
    "\n"
    "\n"
    "def start():\n"
    '    """start。"""\n'
    '    return subprocess.Popen(["npm", "run", "start:dev"])\n'
)
_FIXED_SRC = (
    '"""t。"""\n'
    "import subprocess\n"
    "\n"
    "\n"
    "def start():\n"
    '    """start。"""\n'
    '    return subprocess.Popen(["npm.cmd", "run", "start:dev"])\n'
)


def _sandbox(tmp: str) -> Sandbox:
    config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
    return Sandbox(Path(tmp) / "code", config)


def _task(iteration: int = 1) -> NodeTask:
    return NodeTask(node_id="test", title="业务测试", role="tester",
                    kind="task", iteration=iteration)


# ============================================================ 1 静态检出
class WindowsSpawnFindingTests(unittest.TestCase):
    def _rules(self, source: str):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = _sandbox(tmp)
            sandbox.write_file("t.py", source)
            findings = sandbox.analyze(["t.py"], {}).findings
            return sandbox, [(item.rule, item.message) for item in findings]

    @unittest.skipUnless(NPM_IS_SCRIPT, "需要 Windows 且本机 npm 解析为 .cmd 包装脚本")
    def test_bare_npm_flagged_as_fixable(self):
        """裸名 npm → windows_spawn_extension，且给出真实全名；不再重复报 bypass。"""
        _sandbox, found = self._rules(_SPAWN_SRC)
        rules = {rule for rule, _ in found}
        self.assertIn("windows_spawn_extension", rules)
        self.assertNotIn("sandbox_command_bypass", rules)
        message = next(msg for rule, msg in found if rule == "windows_spawn_extension")
        self.assertIn("npm.cmd", message.lower())
        self.assertIn("FileNotFoundError", message)

    @unittest.skipUnless(NPM_IS_SCRIPT, "需要 Windows 且本机 npm 解析为 .cmd 包装脚本")
    def test_full_name_not_flagged(self):
        """写成 npm.cmd（白名单已登记 npm）→ 一条都不报，修复后能通过准入。"""
        _sandbox, found = self._rules(_FIXED_SRC)
        rules = {rule for rule, _ in found}
        self.assertNotIn("windows_spawn_extension", rules)
        self.assertNotIn("sandbox_command_bypass", rules)

    @unittest.skipUnless(IS_WINDOWS, "Windows 专属")
    def test_exe_target_not_flagged(self):
        """node 解析为 node.EXE（真实 PE 映像）→ 不是本缺陷，不得误报。"""
        if not shutil.which("node"):
            self.skipTest("本机无 node")
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = _sandbox(tmp)
            sandbox.write_file(
                "n.py", '"""n。"""\nimport subprocess\n\n\ndef go():\n'
                        '    """go。"""\n    return subprocess.Popen(["node", "-v"])\n')
            rules = {item.rule for item in sandbox.analyze(["n.py"], {}).findings}
            self.assertNotIn("windows_spawn_extension", rules)

    def test_absent_program_still_bypass(self):
        """本机不存在的 g++ 仍按 bypass 报（不能被新规则吞掉，否则环境问题会被误判为可修）。"""
        if shutil.which("g++"):
            self.skipTest("本机装了 g++，无法验证缺失分支")
        _sandbox, found = self._rules(
            '"""g。"""\nimport subprocess\n\n\ndef go():\n'
            '    """go。"""\n    return subprocess.run(["g++", "-v"])\n')
        rules = {rule for rule, _ in found}
        self.assertIn("sandbox_command_bypass", rules)
        self.assertNotIn("windows_spawn_extension", rules)


# ============================================================ 2 白名单 PATHEXT
class ExecutableAllowedTests(unittest.TestCase):
    def test_script_extension_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = _sandbox(tmp)
            self.assertIn("npm", sandbox.allowed_commands)
            self.assertTrue(sandbox._executable_allowed("npm"))
            self.assertTrue(sandbox._executable_allowed("npm.cmd"))
            self.assertTrue(sandbox._executable_allowed("NPM.CMD"))
            self.assertTrue(sandbox._executable_allowed("npx.cmd"))

    def test_unlisted_program_still_denied(self):
        """归一只针对白名单已登记的程序；未登记的 g++.cmd 不能被顺带放行。"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = _sandbox(tmp)
            self.assertFalse(sandbox._executable_allowed("g++"))
            self.assertFalse(sandbox._executable_allowed("g++.cmd"))
            self.assertFalse(sandbox._executable_allowed("cmd"))

    def test_python_variants_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = _sandbox(tmp)
            for name in ("python", "python3", "python3.13", "py"):
                self.assertTrue(sandbox._executable_allowed(name), name)

    def test_container_maps_script_name_to_binary(self):
        """容器后端把宿主的 npm.cmd 映射成容器内真实二进制名 npm，而不是直接拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = _sandbox(tmp)
            self.assertEqual(sandbox._container_executable("npm.cmd"), "npm")
            self.assertEqual(sandbox._container_executable("node"), "node")
            with self.assertRaises(Exception):
                sandbox._container_executable("g++.cmd")


# ============================================================ 3 根因归类
class RootCauseClassificationTests(unittest.TestCase):
    def _agent(self):
        tmp = tempfile.TemporaryDirectory()
        config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp.name) / "workspace")
        emitted = []
        ctx = SimpleNamespace(
            config=config,
            events=SimpleNamespace(
                emit=lambda event, **kw: emitted.append((event, kw.get("payload")))),
            run_dir=Path(tmp.name), llm=None, tools=None,
            sandbox=Sandbox(Path(tmp.name) / "code", config),
            prompts=None, experience=None, bus=None, blackboard=None,
            log=lambda *a, **k: None, run_state={}, consult=None, run_id="t-win",
        )
        return TesterAgent(ctx), ctx, emitted, tmp

    def test_windows_spawn_is_suite_defect(self):
        """裸名 spawn → test_suite_defect（可修），不是 delivery_unverifiable（不可修）。"""
        agent, _ctx, _emitted, tmp = self._agent()
        try:
            findings = [{"rule": "windows_spawn_extension", "severity": "major",
                         "file": "tests/test_all.py", "line": 58,
                         "message": "subprocess.Popen() 在 Windows 下用裸名 `npm` 启动外部程序"}]
            cases = [{"class": "TestFP01Auth", "test": "test_functional_login",
                      "status": "failed",
                      "message": "AssertionError: spawn failed: FileNotFoundError(2, ...)"}]
            root = agent._root_cause(findings, cases)
            self.assertEqual(root["category"], "test_suite_defect")
            note = agent._root_cause_note(findings, root)
            self.assertIn("测试套件缺陷", note)
        finally:
            tmp.cleanup()

    def test_real_delivery_gap_still_unverifiable(self):
        """白名单外且本机存在的程序（无 .cmd 可解析）仍归 delivery_unverifiable，口径不倒退。"""
        agent, _ctx, _emitted, tmp = self._agent()
        try:
            program = "docker" if shutil.which("docker") else "git"
            if not shutil.which(program):
                self.skipTest("本机无可用对照程序")
            findings = [{"rule": "sandbox_command_bypass", "severity": "major",
                         "file": "tests/test_all.py", "line": 10,
                         "message": f"subprocess.run() 直接调用未在沙箱白名单内的程序 `{program}`"}]
            cases = [{"class": "C", "test": "t", "status": "failed",
                      "message": "FileNotFoundError(2, 'No such file or directory')"}]
            root = agent._root_cause(findings, cases)
            if os.path.splitext(shutil.which(program))[1].lower() in (".cmd", ".bat"):
                self.skipTest(f"{program} 本身是包装脚本，不适合作为不可修对照")
            self.assertEqual(root["category"], "delivery_unverifiable")
        finally:
            tmp.cleanup()


# ============================================================ 4 冻结前修复通道
class FreezeAdmissionTests(unittest.TestCase):
    def _agent(self, llm=None):
        tmp = tempfile.TemporaryDirectory()
        config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp.name) / "workspace")
        emitted = []
        sandbox = Sandbox(Path(tmp.name) / "code", config)
        ctx = SimpleNamespace(
            config=config,
            events=SimpleNamespace(
                emit=lambda event, **kw: emitted.append((event, kw.get("payload")))),
            run_dir=Path(tmp.name), llm=llm, tools=None, sandbox=sandbox,
            prompts=None, experience=None, bus=None, blackboard=None,
            log=lambda *a, **k: None, run_state={}, consult=None, run_id="t-win",
        )
        return TesterAgent(ctx), ctx, emitted, tmp

    @unittest.skipUnless(NPM_IS_SCRIPT, "需要 Windows 且本机 npm 解析为 .cmd 包装脚本")
    def test_clean_suite_passes_without_llm(self):
        """无阻断发现时直接放行，不发任何事件、不触发修复。"""
        agent, _ctx, emitted, tmp = self._agent()
        try:
            agent.sandbox_write("tests/test_all.py", _FIXED_SRC)
            result = AgentResult(status="pass")
            data = {"files": [{"path": "tests/test_all.py", "content": _FIXED_SRC}],
                    "cases": [{"id": "TC-1", "fp_id": "FP-01", "category": "functional"}]}
            generation = Generation(data=dict(data))
            out_file, out_data, _gen = agent._ensure_spawnable_suite(
                _task(), "tests/test_all.py", data, generation, result)
            self.assertEqual(out_file, "tests/test_all.py")
            self.assertEqual(out_data, data)
            self.assertEqual(result.status, "pass")
            self.assertEqual(emitted, [])
        finally:
            tmp.cleanup()

    @unittest.skipUnless(NPM_IS_SCRIPT, "需要 Windows 且本机 npm 解析为 .cmd 包装脚本")
    def test_blocking_suite_skipped_and_logged_without_llm(self):
        """单元测试直连（无 LLM）：按既有先例跳过体检并留痕，不判失败也不静默。"""
        agent, _ctx, emitted, tmp = self._agent()
        try:
            agent.sandbox_write("tests/test_all.py", _SPAWN_SRC)
            result = AgentResult(status="pass")
            data = {"files": [{"path": "tests/test_all.py", "content": _SPAWN_SRC}], "cases": []}
            out_file, _out_data, _gen = agent._ensure_spawnable_suite(
                _task(), "tests/test_all.py", data, Generation(data=dict(data)), result)
            self.assertEqual(out_file, "tests/test_all.py")
            self.assertEqual(result.status, "pass")
            self.assertEqual([event for event, _ in emitted], ["test_spawn_check_skipped"])
        finally:
            tmp.cleanup()

    @unittest.skipUnless(NPM_IS_SCRIPT, "需要 Windows 且本机 npm 解析为 .cmd 包装脚本")
    def test_llm_repair_rewrites_and_freezes_nothing_broken(self):
        """带 LLM：一次修复重生成后套件合格，发 test_spawn_repaired，文件已落盘为全名写法。"""
        class FakeLLM:
            def __init__(self):
                self.calls = []

            def generate(self, kind, bundle, _ctx):
                self.calls.append(kind)
                return Generation(data={
                    "files": [{"path": "tests/test_all.py", "content": _FIXED_SRC}],
                    "cases": [{"id": "TC-1", "fp_id": "FP-01", "category": "functional"}],
                    "derived_from": "spawn-repair",
                })

        llm = FakeLLM()
        agent, _ctx, emitted, tmp = self._agent(llm=llm)
        try:
            agent.sandbox_write("tests/test_all.py", _SPAWN_SRC)
            result = AgentResult(status="pass")
            data = {"files": [{"path": "tests/test_all.py", "content": _SPAWN_SRC}],
                    "cases": [{"id": "TC-1", "fp_id": "FP-01", "category": "functional"}]}
            out_file, out_data, _gen = agent._ensure_spawnable_suite(
                _task(), "tests/test_all.py", data, Generation(data=dict(data)), result)
            self.assertEqual(out_file, "tests/test_all.py")
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(out_data["derived_from"], "spawn-repair")
            self.assertEqual(out_data["cases"], data["cases"])       # 元数据未被清空
            self.assertEqual(llm.calls, ["test_suite_repair"])
            self.assertEqual([event for event, _ in emitted],
                             ["test_spawn_repair_requested", "test_spawn_repaired"])
            written = (agent.ctx.sandbox.root / "tests" / "test_all.py").read_text(encoding="utf-8")
            self.assertIn("npm.cmd", written)
        finally:
            tmp.cleanup()

    @unittest.skipUnless(NPM_IS_SCRIPT, "需要 Windows 且本机 npm 解析为 .cmd 包装脚本")
    def test_unrepairable_suite_fails_before_freeze(self):
        """修复后仍不合格 → 判失败且明确说明「未冻结任何套件」，避免把死档写进冻结副本。"""
        class StubbornLLM:
            def generate(self, kind, bundle, _ctx):
                return Generation(data={
                    "files": [{"path": "tests/test_all.py", "content": _SPAWN_SRC}],
                    "cases": [], "derived_from": "spawn-repair"})

        agent, _ctx, emitted, tmp = self._agent(llm=StubbornLLM())
        try:
            agent.sandbox_write("tests/test_all.py", _SPAWN_SRC)
            result = AgentResult(status="pass")
            data = {"files": [{"path": "tests/test_all.py", "content": _SPAWN_SRC}], "cases": []}
            _out_file, _out_data, _gen = agent._ensure_spawnable_suite(
                _task(), "tests/test_all.py", data, Generation(data=dict(data)), result)
            self.assertEqual(result.status, "fail")
            self.assertIn("冻结前准入体检未通过", result.error)
            events = [event for event, _ in emitted]
            self.assertEqual(events, ["test_spawn_repair_requested", "test_spawn_unrepairable"])
        finally:
            tmp.cleanup()

    @unittest.skipUnless(NPM_IS_SCRIPT, "需要 Windows 且本机 npm 解析为 .cmd 包装脚本")
    def test_repair_cannot_rewrite_case_metadata(self):
        """修复重生成回传了不同的 cases → 一律沿用修复前元数据，覆盖校验结果不被悄悄改写。"""
        class RewritingLLM:
            def generate(self, kind, bundle, _ctx):
                return Generation(data={
                    "files": [{"path": "tests/test_all.py", "content": _FIXED_SRC}],
                    "cases": [{"id": "TC-ONLY", "fp_id": "FP-09", "category": "functional"}],
                    "derived_from": "spawn-repair"})

        agent, _ctx, _emitted, tmp = self._agent(llm=RewritingLLM())
        try:
            original = [{"id": "TC-1", "fp_id": "FP-01", "category": "functional"},
                        {"id": "TC-2", "fp_id": "FP-01", "category": "exception"},
                        {"id": "TC-3", "fp_id": "FP-01", "category": "boundary"}]
            agent.sandbox_write("tests/test_all.py", _SPAWN_SRC)
            result = AgentResult(status="pass")
            data = {"files": [{"path": "tests/test_all.py", "content": _SPAWN_SRC}],
                    "cases": list(original)}
            _out_file, out_data, _gen = agent._ensure_spawnable_suite(
                _task(), "tests/test_all.py", data, Generation(data=dict(data)), result)
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(out_data["cases"], original)
        finally:
            tmp.cleanup()


# ============================================================ 5 leader 升级口径
class LeaderUnfixableTests(unittest.TestCase):
    @staticmethod
    def _ctx(*events):
        return SimpleNamespace(events=SimpleNamespace(events=[
            SimpleNamespace(type=name, node=node, iteration=iteration, payload=payload or {})
            for name, node, iteration, payload in events
        ]))

    def _reason(self, *events):
        leader = Leader.__new__(Leader)          # 只测纯函数，跳过 __init__ 的重资源装配
        return leader._unfixable_failure_reason(self._ctx(*events), "test", 1)

    def test_suite_defect_with_reopen_is_fixable(self):
        """套件已解冻（修复通道打开）→ 不再判「回退也改不掉」，让流程走正常回退。"""
        reason = self._reason(
            ("test_suite_defect", "test", 1, {"note": "【测试套件缺陷】裸名 npm"}),
            ("test_freeze_invalidated", "", 0, {"reason": "test_suite_defect"}),
        )
        self.assertEqual(reason, "")

    def test_suite_defect_without_reopen_still_unfixable(self):
        """没有解冻记录 → 维持原口径（回退给研发也改不了测试文件）。"""
        reason = self._reason(
            ("test_suite_defect", "test", 1, {"note": "【测试套件缺陷】缺 import json"}),
        )
        self.assertIn("测试套件自身违反执行协议", reason)

    def test_other_categories_ignore_reopen(self):
        """环境缺口/交付物不可验证即使有解冻记录也仍判不可修，保护不被放宽。"""
        for event_type, label in (("test_environment_gap", "外部工具链缺失"),
                                  ("test_delivery_unverifiable", "无法被现有验证通道验证"),
                                  ("test_verification_gap", "验证口径/证据通道缺口")):
            reason = self._reason(
                (event_type, "test", 1, {"note": "x"}),
                ("test_freeze_invalidated", "", 0, {"reason": "y"}),
            )
            self.assertIn(label, reason, event_type)

    def test_reopen_must_follow_defect_event(self):
        """解冻记录必须出现在缺陷事件之后（同一节点内 invalidate），之前轮次的不算数。"""
        reason = self._reason(
            ("test_freeze_invalidated", "", 0, {"reason": "上一轮的解冻"}),
            ("test_suite_defect", "test", 1, {"note": "n"}),
        )
        self.assertIn("测试套件自身违反执行协议", reason)


if __name__ == "__main__":
    unittest.main()
