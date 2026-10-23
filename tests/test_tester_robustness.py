"""测试 Agent 对契约外 LLM 输出的防御回归。

背景（run task01-cron-attempt02 实测）：LLM 把 negotiation 输出成字符串，
`(data.get("negotiation") or {}).get("questions")` 抛
AttributeError: 'str' object has no attribute 'get'，整个 test 节点崩溃、
连沙箱都没跑到。本文件锁定：契约外类型不再崩溃，按「无协商问题」降级处理。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.tester import TesterAgent
from mas.config import load_config
from mas.schemas import NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _agent(agent_cls=None):
    tmp = tempfile.TemporaryDirectory()
    config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp.name) / "workspace")
    ctx = SimpleNamespace(
        config=config,
        events=SimpleNamespace(emit=lambda *a, **k: None),
        run_dir=Path(tmp.name),
        llm=None,
        tools=None,
        sandbox=None,
        prompts=None,
        experience=None,
        bus=None,
        blackboard=None,
        log=lambda *a, **k: None,
        run_state={},
        consult=None,
    )
    agent = (agent_cls or TesterAgent)(ctx)
    return agent, ctx, tmp


class NegotiationRobustnessTests(unittest.TestCase):
    def _negotiate(self, data):
        agent, _ctx, tmp = _agent()
        try:
            return agent._negotiate(data, NodeTask(node_id="test", title="业务测试", role="tester",
                                                   kind="task", iteration=1), None)
        finally:
            tmp.cleanup()

    def test_negotiation_as_string_does_not_crash(self):
        """LLM 把 negotiation 写成字符串（实测形态）→ 降级为无问题，不抛异常。"""
        result = self._negotiate({"negotiation": "无待澄清问题"})
        self.assertEqual(result["questions"], [])
        self.assertEqual(result["open"], 0)
        self.assertEqual(result["status"], "closed")

    def test_negotiation_questions_with_non_dict_items_filtered(self):
        """questions 列表混入非对象项 → 过滤掉，只保留结构化问题。"""
        result = self._negotiate({"negotiation": {"questions": ["口头问题", {"question": "Q1", "fp_id": "FP-01"}]}})
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["questions"][0]["question"], "Q1")

    def test_negotiation_missing_key_treated_as_no_questions(self):
        result = self._negotiate({})
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["status"], "closed")

    def test_negotiation_none_value_does_not_crash(self):
        result = self._negotiate({"negotiation": None})
        self.assertEqual(result["open"], 0)

    def test_over_cap_questions_do_not_block_gate(self):
        """超出协商预算的问题不计入 open——否则 LLM 一次提多问就永久阻塞门禁。

        task02 实测：LLM 提 5 问、旧代码只协 2 问，3 问永远 open，
        门禁 negotiation.open==0 无法满足，整轮验收失败。
        """
        agent, ctx, tmp = _agent()
        try:
            ctx.consult = lambda **kwargs: {"answer": "结论：按需求契约执行"}
            questions = [{"question": f"Q{i}", "fp_id": "FP-01"} for i in range(8)]
            task = NodeTask(node_id="test", title="业务测试", role="tester",
                            kind="task", iteration=1)
            result = agent._negotiate({"negotiation": {"questions": questions}}, task, None)
            self.assertEqual(result["total"], 6)      # 预算内全部入场并协商
            self.assertEqual(result["closed"], 6)
            self.assertEqual(result["open"], 0)       # 门禁可满足
            self.assertEqual(result["over_cap"], 2)   # 超量可见、不阻塞
            self.assertEqual(result["status"], "closed")
        finally:
            tmp.cleanup()

    def test_contract_declares_execution_protocol(self):
        """执行权在框架：契约只要求文件定义用例，不再教模型写 main/marker。"""
        contract = TesterAgent.contracts["default"]
        protocol = contract.get("execution_protocol") or {}
        self.assertEqual(protocol.get("runner"), "mas/verify/runner.py")
        self.assertIn("test_ids", protocol.get("case_discovery", ""))
        self.assertNotIn("marker", protocol)
        self.assertNotIn("reference_runner", protocol)
        self.assertTrue(any("只定义" in item for item in protocol.get("requirements") or []))
        # cases 元数据必须带 op/name（runner 按 test_{category}_{op}_{name} 绑定 fp_id）
        case_schema = (contract.get("cases") or [{}])[0]
        self.assertIn("op", case_schema)
        self.assertIn("name", case_schema)

    def test_offline_provider_emits_case_only_suite(self):
        """离线 Provider 的测试文件也只定义用例，不再内嵌 runner/marker。"""
        agent, ctx, tmp = _agent()
        try:
            from mas.providers.offline import OfflineProvider

            provider = OfflineProvider(ctx.config)
            bundle = SimpleNamespace(experiences=[], system="s", user="u", path=None)
            data = provider.provide("test_suite", bundle=bundle, context={"spec": {
                "title": "t", "function_points": [
                    {"id": "FP-01", "title": "x", "samples": [{"input": "a", "expect": "b"}]}
                ]}})
            self.assertTrue(data.get("files"))
            content = data["files"][0]["content"]
            self.assertIn("import unittest", content)
            self.assertNotIn("<<<MASS_RESULT>>>", content)
            self.assertNotIn('if __name__ == "__main__"', content)
            self.assertNotIn("--only", content)
        finally:
            tmp.cleanup()


class NegotiationClosureIntegrityTests(unittest.TestCase):
    """闭环判定必须落到"每个问题都有实质应答"，而不是数应答条数。

    task02-attempt02 实测：研发 LLM 把多个问题合并复述在 answer 的 question 字段里，
    原实现 `closed = len(answers)` 让 4/4 闭环通过门禁，而 tester 自己标注的
    Q-3/Q-4 仍是 open——门禁虚报，证据自相矛盾。
    """

    def _run(self, consult, questions):
        agent, ctx, tmp = _agent()
        try:
            ctx.consult = consult
            task = NodeTask(node_id="test", title="业务测试", role="tester",
                            kind="task", iteration=1)
            return agent._negotiate({"negotiation": {"questions": questions}}, task, None)
        finally:
            tmp.cleanup()

    def test_substantive_answer_marks_question_resolved(self):
        answer = "结论：按需求契约，取消已支付订单时先退款再置为 cancelled。"
        result = self._run(lambda **kw: {"answer": answer},
                           [{"question": "Q1 取消口径", "fp_id": "FP-05"},
                            {"question": "Q2 订单 id 生成", "fp_id": "FP-05"}])
        self.assertEqual((result["closed"], result["open"]), (2, 0))
        self.assertEqual({q["status"] for q in result["questions"]}, {"resolved"})
        self.assertIn("先退款", result["questions"][0]["resolution"])

    def test_empty_or_stub_answer_stays_open(self):
        """空答复/一句话敷衍不算闭环：否则 negotiation.open==0 形同虚设。"""
        result = self._run(lambda **kw: {"answer": "无"},
                           [{"question": "Q1 取消口径", "fp_id": "FP-05"}])
        self.assertEqual((result["closed"], result["open"]), (0, 1))
        self.assertEqual(result["questions"][0]["status"], "open")
        self.assertEqual(result["status"], "open")

    def test_missing_reply_keeps_index_paired(self):
        """第 2 问无应答时不能把第 1 问的应答算给它（按请求顺序配对）。"""
        calls = [{"answer": "结论：采用退款即取消口径即可。"}, {}]
        result = self._run(lambda **kw: calls.pop(0) if calls else {},
                           [{"question": "Q1", "fp_id": ""}, {"question": "Q2", "fp_id": ""}])
        self.assertEqual(result["closed"], 1)
        self.assertEqual(result["questions"][0]["status"], "resolved")
        self.assertEqual(result["questions"][1]["status"], "open")


class NonPythonDeliveryHonestyTests(unittest.TestCase):
    """非 Python 交付物：度量与自检口径必须诚实，失败点要落在真正受限的环节。

    task04（C++ 智能指针）实测：manifest 报「2 个文件，0 行」（code_stats 只认 .py），
    implement 因 `python -c import smartptr` ModuleNotFoundError 被判"导入自检失败"——
    而真实边界在测试节点（沙箱只能执行 Python，无法编译验证 C++）。
    """

    def test_self_check_not_applicable_for_non_python(self):
        from mas.agents.developer import DeveloperAgent

        agent, _ctx, tmp = _agent(DeveloperAgent)
        try:
            task = NodeTask(node_id="implement", title="编码实现", role="developer",
                            kind="task", iteration=1)
            check = agent._self_check("smartptr", task, ["include/smartptr.hpp", "tests/t.cpp"])
            self.assertTrue(check["ok"])
            self.assertTrue(check["not_applicable"])
        finally:
            tmp.cleanup()

    def test_self_check_still_runs_for_python(self):
        from mas.agents.developer import DeveloperAgent
        from mas.sandbox import Sandbox

        agent, ctx, tmp = _agent(DeveloperAgent)
        try:
            root = Path(tmp.name) / "sbx"
            ctx.sandbox = Sandbox(root, ctx.config)
            (root).mkdir(parents=True, exist_ok=True)
            agent.sandbox_write = lambda path, content: (root / path).parent.mkdir(
                parents=True, exist_ok=True) or (root / path).write_text(content, encoding="utf-8")
            agent.sandbox_write("pkg/__init__.py", "")
            task = NodeTask(node_id="implement", title="编码实现", role="developer",
                            kind="task", iteration=1)
            check = agent._self_check("pkg", task, ["pkg/__init__.py"])
            self.assertNotIn("not_applicable", check)
        finally:
            tmp.cleanup()

    def test_self_check_skips_test_only_round_for_cpp(self):
        """C++ 任务的 optimize 轮只额外重写 tests/test_*.py 时，不得再跑 `import smartptr`。

        task04-cpp-probe-03 optimize#1 实测：本轮 written = [include/smartptr.hpp,
        tests/test_smartptr.cpp, tests/test_smartptr.py]，原判定只看"有没有 .py"，
        于是对 C++ 包名发起导入、ModuleNotFoundError、节点被判"导入自检失败"。
        """
        from mas.agents.developer import DeveloperAgent

        agent, _ctx, tmp = _agent(DeveloperAgent)
        try:
            task = NodeTask(node_id="optimize", title="缺陷修复", role="developer",
                            kind="task", iteration=1)
            check = agent._self_check("smartptr", task, [
                "include/smartptr.hpp", "tests/test_smartptr.cpp", "tests/test_smartptr.py"])
            self.assertTrue(check["ok"])
            self.assertTrue(check["not_applicable"])
        finally:
            tmp.cleanup()

    def test_self_check_still_runs_when_package_already_delivered(self):
        """Python 任务的"只改测试文件"轮次仍要跑导入自检（沙箱里已有该包）。"""
        from mas.agents.developer import DeveloperAgent
        from mas.sandbox import Sandbox

        agent, ctx, tmp = _agent(DeveloperAgent)
        try:
            root = Path(tmp.name) / "sbx"
            ctx.sandbox = Sandbox(root, ctx.config)
            (root / "pkg").mkdir(parents=True, exist_ok=True)
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            task = NodeTask(node_id="optimize", title="缺陷修复", role="developer",
                            kind="task", iteration=1)
            check = agent._self_check("pkg", task, ["tests/test_pkg.py"])
            self.assertNotIn("not_applicable", check)
        finally:
            tmp.cleanup()

    def test_self_check_label_is_honest(self):
        from mas.agents.developer import DeveloperAgent

        self.assertEqual(DeveloperAgent._self_check_label({"ok": True, "not_applicable": True}),
                         "导入自检不适用（非 Python 交付物）")
        self.assertEqual(DeveloperAgent._self_check_label({"ok": True}), "导入自检通过")
        self.assertEqual(DeveloperAgent._self_check_label({"ok": False}), "导入自检失败")

    def test_preexec_diagnosis_flags_compiler_bypass(self):
        """tester 执行前静态体检：检出测试文件绕过沙箱命令白名单调用编译器。"""
        from mas.sandbox import Sandbox

        agent, ctx, tmp = _agent()
        try:
            root = Path(tmp.name) / "sbx"
            root.mkdir(parents=True, exist_ok=True)
            ctx.sandbox = Sandbox(root, ctx.config)
            (root / "tests").mkdir(parents=True, exist_ok=True)
            (root / "tests" / "t.py").write_text(
                '"""t。"""\nimport subprocess\n\n\ndef go():\n'
                '    """go。"""\n    return subprocess.run(["g++", "-std=c++17", "a.cpp"])\n',
                encoding="utf-8")
            findings = agent._preexec_diagnosis("tests/t.py")
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["rule"], "sandbox_command_bypass")
        finally:
            tmp.cleanup()

    def test_preexec_diagnosis_flags_variable_compiler_target(self):
        """`CXX = os.environ.get("CXX", "g++")` 后 `subprocess.run([CXX] + ...)` 也必须被检出。

        task04-cpp-probe-02 实测漏检：preexec_findings 为空 → root_cause_note 为空 →
        32 条失败用例被当成研发缺陷回退，真实根因（本机无编译器）无人说明。
        """
        from mas.sandbox import Sandbox

        agent, ctx, tmp = _agent()
        try:
            root = Path(tmp.name) / "sbx"
            root.mkdir(parents=True, exist_ok=True)
            ctx.sandbox = Sandbox(root, ctx.config)
            (root / "tests").mkdir(parents=True, exist_ok=True)
            (root / "tests" / "v.py").write_text(
                '"""v。"""\nimport os\nimport subprocess\n\nCXX = os.environ.get("CXX", "g++")\n\n\n'
                'def go(flags, src, exe):\n    """go。"""\n'
                '    return subprocess.run([CXX] + flags + [src, "-o", exe])\n',
                encoding="utf-8")
            findings = agent._preexec_diagnosis("tests/v.py")
            self.assertEqual([item["rule"] for item in findings], ["sandbox_command_unresolved"])
        finally:
            tmp.cleanup()

    def test_root_cause_separates_environment_from_defects(self):
        """缺编译器的失败归为环境/构建问题，而不是代码缺陷。"""
        agent, _ctx, tmp = _agent()
        try:
            findings = [{"rule": "sandbox_command_unresolved", "message": "外部程序名不是字面量"}]
            cases = [{"class": "T", "test": "t1", "status": "failed",
                      "message": "FileNotFoundError: [WinError 2] 系统找不到指定的文件。"}]
            cause = agent._root_cause(findings, cases)
            self.assertEqual(cause["category"], "environment_or_build")
            self.assertEqual(cause["evidence_total"], 1)
            note = agent._root_cause_note(findings, cause)
            self.assertIn("【环境/构建问题】", note)
            self.assertIn("WinError 2", note)
            # 无失败证据时只说"交付物无法验证"，不冤枉环境
            other = agent._root_cause(findings, [{"status": "failed", "message": "assert 3 == 4"}])
            self.assertEqual(other["category"], "delivery_unverifiable")
            self.assertIn("【交付物无法验证】", agent._root_cause_note(findings, other))
            # 无 findings → 不臆造根因
            self.assertEqual(agent._root_cause([], cases), {"category": "", "evidence": []})
            self.assertEqual(agent._root_cause_note([], {"category": ""}), "")
            # 测试套件自身缺陷（漏 import json）优先归为套件缺陷，既不推给环境也不推给交付物
            suite = [{"rule": "json_not_imported", "message": "使用 json.* 但没有 import json"}]
            cause3 = agent._root_cause(suite, cases)
            self.assertEqual(cause3["category"], "test_suite_defect")
            self.assertIn("【测试套件缺陷】", agent._root_cause_note(suite, cause3))
            # 同一规则命中多处（probe-03 iter02 的 line 223/234）时说明文本只出现一次
            twice = [{"rule": "sandbox_command_unresolved", "message": "外部程序名不是字面量"},
                     {"rule": "sandbox_command_unresolved", "message": "外部程序名不是字面量"}]
            note2 = agent._root_cause_note(twice, agent._root_cause(twice, cases))
            self.assertEqual(note2.count("外部程序名不是字面量"), 1)
        finally:
            tmp.cleanup()


class VerificationGapTests(unittest.TestCase):
    """选型语言与沙箱可执行语言不一致时，"用例全过"不构成验收证据。

    task04-cpp-probe-02 实测：C++ 需求下研发补一份 Python 复刻 + Python 测试套件，
    25 条用例全过、三类覆盖率 1.0、bug_reports.open=0，8 项门禁全绿并签署"验收通过"，
    而被验收的 .hpp 从未被编译过（沙箱只执行了 `python tests/test_smartptr.py`）。
    """

    def test_non_python_design_language_marks_gap(self):
        agent, _ctx, tmp = _agent()
        try:
            gap = agent._verification_gap({"tech_choice": {"language": "C++17"}})
            self.assertEqual(gap["gap"], 1)
            self.assertEqual(gap["design_language"], "C++17")
            self.assertIn("沙箱只能执行 Python", gap["note"])
        finally:
            tmp.cleanup()

    def test_python_design_language_has_no_gap(self):
        agent, _ctx, tmp = _agent()
        try:
            for language in ("Python 3.9+", "python 3.13（标准库）", ""):
                self.assertEqual(agent._verification_gap(
                    {"tech_choice": {"language": language}})["gap"], 0, language)
            # 上游产物缺失同样不报缺口（避免凭空判失败）
            self.assertEqual(agent._verification_gap({})["gap"], 0)
            self.assertEqual(agent._verification_gap(None)["gap"], 0)
        finally:
            tmp.cleanup()


class ZeroEvidenceIntegrityTests(unittest.TestCase):
    """无执行证据不得判通过、不得自动闭环缺陷。

    task04-cpp-probe-02 iter02 实测：测试文件 `main()` 因 `NameError: name 'json'
    is not defined` 崩溃，marker 未输出 → total=0/failed=0 → 原实现判 status=pass，
    并把上一轮 32 个缺陷全部自动记为"已修复"（缺陷开 0 / 闭环 32）。
    """

    def _perception(self, open_bugs):
        from mas.agents.base import Perception

        return Perception(found={"bug_reports": {"items": open_bugs}})

    def test_no_executed_cases_does_not_close_prior_bugs(self):
        import copy

        agent, _ctx, tmp = _agent()
        try:
            prior = [{"id": "BUG-aaaa1111", "test": "T.test_functional_x", "status": "open",
                      "fp_id": "FP-01", "category": "functional"}]
            task = NodeTask(node_id="test", title="业务测试", role="tester", kind="task",
                            iteration=2)
            bugs = agent._track_bugs([], [], task, self._perception(copy.deepcopy(prior)))
            self.assertEqual(bugs["open"], 1, bugs)
            self.assertEqual(bugs["fixed"], 0)
            # 有执行记录时，未复现的旧缺陷仍按原语义闭环（不影响既有行为）
            executed = [{"class": "T", "test": "test_functional_other", "status": "passed",
                         "category": "functional", "fp_id": "FP-01"}]
            bugs2 = agent._track_bugs(executed, [], task, self._perception(copy.deepcopy(prior)))
            self.assertEqual(bugs2["fixed"], 1, bugs2)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
