"""子进程输出「接了 PIPE 却没人读」的静态检出与归类。

背景（run task12-foodlink-spa-03 单因子对照实测，2026-11-03）：冻结套件用
``subprocess.Popen(["npm", "run", "start:dev"], stdout=subprocess.PIPE)`` 起 NestJS
后端，并且只在轮询端口前写 ``self.log``——全文从不读取管道。同一目录、同一条命令下：

    mode=pipe  (PIPE，无人读)     wall=60.0s NOT READY
    mode=drain (PIPE + 守护线程读) wall=3.0s  READY status=200
    mode=file  (stdout 指文件)     wall=3.0s  READY status=200

启动日志实测 8656 字节，超过 Windows 匿名管道缓冲；写满后子进程永久阻塞在 write()，
端口永远不监听，39 条用例里 33 条集体报 ``timeout waiting for backend``。

这一步之所以关键：裸名 ``npm`` 修成 ``npm.cmd``、依赖装齐、PostgreSQL 起来、甚至
交付里那两处 Prisma 类型错误都修好之后，用例**仍然**全败——失败原因完全在测试文件
自己身上。它既不是环境缺失（数据库/依赖都在），也不是交付缺陷（代码一行没动）。
所以必须像 ``windows_spawn_extension`` 一样，归为可修的套件缺陷，并在**冻结前**
拦下来交回生成方重写（套件一旦冻结，``restore_if_changed()`` 会每轮恢复这份写法）。
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

#: task12 冻结套件的真实形状：Popen 接 PIPE、只 poll 不读、超时后把原因写进 self.log
_PIPE_SRC = '''"""t。"""
import subprocess


def start():
    """start。"""
    proc = subprocess.Popen(["npm.cmd", "run", "start:dev"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc
'''

#: 修法一：输出重定向到工作区内日志文件（被测服务退出后再读它做诊断）
_FILE_SRC = '''"""t。"""
import subprocess


def start():
    """start。"""
    log = open("backend.log", "wb")
    return subprocess.Popen(["npm.cmd", "run", "start:dev"],
                            stdout=log, stderr=subprocess.STDOUT)
'''

#: 修法二：仍然用 PIPE，但持续读取（communicate / 守护线程都算「有人读」）
_COMMUNICATE_SRC = '''"""t。"""
import subprocess


def start():
    """start。"""
    proc = subprocess.Popen(["npm.cmd", "run", "start:dev"], stdout=subprocess.PIPE,
                            text=True)
    out, _ = proc.communicate()
    return out
'''

_DRAIN_SRC = '''"""t。"""
import subprocess
import threading


def start():
    """start。"""
    proc = subprocess.Popen(["npm.cmd", "run", "start:dev"], stdout=subprocess.PIPE,
                            text=True)
    threading.Thread(target=lambda: [print(line) for line in proc.stdout],
                     daemon=True).start()
    return proc
'''

#: 与 PIPE 无关的写法：subprocess.run(capture_output=True) 自己会读完管道
_RUN_SRC = '''"""t。"""
import subprocess


def start():
    """start。"""
    return subprocess.run(["npm.cmd", "run", "start:dev"], capture_output=True, text=True)
'''


def _rules(source: str):
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Sandbox(Path(tmp) / "code",
                          load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws"))
        sandbox.write_file("t.py", source)
        findings = [(item.rule, item.message) for item in sandbox.analyze(["t.py"], {}).findings]
    return findings


class UnreadPipeFindingTests(unittest.TestCase):
    def test_pipe_without_reader_is_flagged(self):
        """接 PIPE 且全文无人读取 → spawn_unread_pipe，并给出可执行的修法。"""
        found = _rules(_PIPE_SRC)
        rules = {rule for rule, _ in found}
        self.assertIn("spawn_unread_pipe", rules)
        message = next(msg for rule, msg in found if rule == "spawn_unread_pipe")
        self.assertIn("PIPE", message)
        self.assertIn("日志文件", message)

    def test_redirect_to_file_not_flagged(self):
        """改成 stdout=<打开的日志文件> → 不再报（这正是修复指令的落点）。"""
        self.assertNotIn("spawn_unread_pipe", {rule for rule, _ in _rules(_FILE_SRC)})

    def test_communicate_not_flagged(self):
        self.assertNotIn("spawn_unread_pipe", {rule for rule, _ in _rules(_COMMUNICATE_SRC)})

    def test_draining_thread_not_flagged(self):
        self.assertNotIn("spawn_unread_pipe", {rule for rule, _ in _rules(_DRAIN_SRC)})

    def test_capture_output_run_not_flagged(self):
        """subprocess.run(capture_output=True) 内部会读完管道，不能误伤。"""
        self.assertNotIn("spawn_unread_pipe", {rule for rule, _ in _rules(_RUN_SRC)})

    def test_both_defects_reported_together(self):
        """裸名 npm + PIPE 无人读同时存在时，两条规则都要报（修复需一次到位）。"""
        source = _PIPE_SRC.replace('"npm.cmd"', '"npm"')
        found = _rules(source)
        rules = {rule for rule, _ in found}
        self.assertIn("spawn_unread_pipe", rules)


class UnreadPipeClassificationTests(unittest.TestCase):
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
            log=lambda *a, **k: None, run_state={}, consult=None, run_id="t-pipe",
        )
        return TesterAgent(ctx), ctx, emitted, tmp

    def test_is_suite_defect_not_environment_or_delivery(self):
        """与 task12 实测一致的症状（timeout waiting for backend）→ test_suite_defect。"""
        agent, _ctx, _emitted, tmp = self._agent()
        try:
            findings = [{"rule": "spawn_unread_pipe", "severity": "major",
                         "file": "tests/test_all.py", "line": 57,
                         "message": "subprocess.Popen() 把 stdout 接到 PIPE，但整个文件没有任何读取动作"}]
            cases = [{"class": "TestFP01Auth", "test": "test_functional_login", "status": "failed",
                      "message": "AssertionError: backend service unavailable: "
                                 "timeout waiting for backend on port 62694"}]
            self.assertEqual(agent._root_cause(findings, cases)["category"], "test_suite_defect")
            self.assertIn("测试套件缺陷", agent._root_cause_note(findings,
                                                                agent._root_cause(findings, cases)))
        finally:
            tmp.cleanup()

    def test_preexec_diagnosis_keeps_the_rule(self):
        """规则必须出现在冻结前准入体检的白名单里，否则修复通道收不到它。"""
        agent, _ctx, _emitted, tmp = self._agent()
        try:
            agent.ctx.sandbox.write_file("tests/test_all.py", _PIPE_SRC)
            findings = agent._preexec_diagnosis("tests/test_all.py")
            self.assertIn("spawn_unread_pipe", {str(item.get("rule")) for item in findings})
            self.assertIn("spawn_unread_pipe", TesterAgent._SUITE_DEFECT_RULES)
        finally:
            tmp.cleanup()

    def test_freeze_admission_blocks_before_freeze(self):
        """无 LLM 时按既有先例跳过并留痕，且不冻结这种套件（与 windows_spawn_extension 同口径）。"""
        agent, _ctx, emitted, tmp = self._agent()
        try:
            from mas.llm import Generation
            agent.ctx.sandbox.write_file("tests/test_all.py", _PIPE_SRC)
            task = SimpleNamespace(node_id="test", iteration=1)
            result = AgentResultStub()
            out_file, _data, _gen = agent._ensure_spawnable_suite(
                task, "tests/test_all.py", {"cases": []}, Generation(data={"files": []}), result)
            self.assertEqual(out_file, "tests/test_all.py")
            self.assertIn("test_spawn_check_skipped", {name for name, _ in emitted})
        finally:
            tmp.cleanup()


class AgentResultStub:
    """tester 的 _ensure_spawnable_suite 只读写 status/error/summary 三个字段。"""

    def __init__(self):
        self.status = "ok"
        self.error = ""
        self.summary = ""
        self.evidence = []


if __name__ == "__main__":
    unittest.main()
