"""测试 Agent：依据需求契约生成并真实执行功能/异常/边界用例，产出缺陷报告。"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from .base import BaseAgent, Perception

__all__ = ["TesterAgent"]

_SEVERITY = {"functional": "critical", "exception": "major", "boundary": "major"}

# 测试文件结果回传的参考实现（与离线 Provider 模板同源，已在生产验证）。
# 关键点：用自定义 TestResult 的 startTest/addFailure/addError 钩子收集记录，
# 而不是跑完再遍历 suite——unittest 执行后会把 suite 内元素置为 None，
# 二次迭代必崩（'NoneType' object has no attribute 'id'，attempt04 实测）。
_REFERENCE_RUNNER = '''import argparse
import json
import sys
import time
import traceback
import unittest

MARKER = "<<<MASS_RESULT>>>"


class _CollectResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.records = []
        self.started = time.time()

    def startTest(self, test):
        super().startTest(test)
        self.records.append({"id": test.id(), "status": "passed", "message": ""})

    def _mark(self, test, status, err):
        for record in self.records:
            if record["id"] == test.id():
                record["status"] = status
                record["message"] = "".join(
                    traceback.format_exception_only(err[0], err[1])
                )[-300:].strip()

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test, "error", err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "failed", err)


def _filter(suite, keywords):
    filtered = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            filtered.addTests(_filter(item, keywords))
        elif any(keyword in item.id() for keyword in keywords):
            filtered.addTest(item)
    return filtered


def main():
    keywords = []
    if "--only" in sys.argv:
        keywords = [part for part in sys.argv[sys.argv.index("--only") + 1].split(",") if part]
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    if keywords:
        suite = _filter(suite, keywords)
    result = _CollectResult()
    suite.run(result)
    cases = []
    for record in result.records:
        _, class_name, method = record["id"].rsplit(".", 2)[-3:]
        parts = method.split("_")
        cases.append({
            "test": method,
            "class": class_name,
            "fp_id": FP_BY_CLASS.get(class_name, ""),
            "category": parts[1] if len(parts) > 1 else "",
            "status": record["status"],
            "message": record["message"],
        })
    failed = sum(1 for case in cases if case["status"] != "passed")
    summary = {
        "total": result.testsRun,
        "passed": result.testsRun - failed,
        "failed": failed,
        "duration_ms": int((time.time() - result.started) * 1000),
        "cases": cases,
    }
    print(MARKER + json.dumps(summary, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())'''


class TesterAgent(BaseAgent):
    role_id = "tester"
    task_kinds = {"default": "test_suite"}
    contracts = {
        "default": {
            "package": "",
            "files": [{"path": "tests/test_xxx.py", "content": ""}],
            "cases": [{"id": "TC-FP-01-functional-xxx", "fp_id": "FP-01", "op": "被测试函数名(ASCII)",
                       "name": "用例短名(ASCII)", "category": "functional|exception|boundary",
                       "call": "调用表达式", "expect": "期望值", "expect_error": "异常类名或空串",
                       "assert_kind": "eq|truthy|falsy|empty|raises"}],
            "case_count": {"total": 0, "functional": 0, "exception": 0, "boundary": 0},
            "negotiation": {"questions": [], "answers": [], "open": 0, "status": "pending"},
            "derived_from": "",
            "execution_protocol": {
                "note": "测试文件必须是可独立执行的 Python 脚本，并按本协议回传结果；沙箱按 <<<MASS_RESULT>>> 标记解析 stdout，缺失任一要素将导致用例统计为 0、覆盖率门禁必然失败",
                "run_cmd": "python tests/test_xxx.py [--only 名称子串,名称子串]",
                "marker": "<<<MASS_RESULT>>>",
                "stdout_last_line": "MARKER + json.dumps(result_json, ensure_ascii=False)，单行，必须是 stdout 中最后一个非空行",
                "result_json": {
                    "total": "int，执行用例总数",
                    "passed": "int，通过数",
                    "failed": "int，未通过数（含 error）",
                    "duration_ms": "int，耗时毫秒",
                    "cases": [{"test": "方法名", "class": "类名", "fp_id": "FP-01",
                               "category": "functional|exception|boundary",
                               "status": "passed|failed|error（非 passed 均计失败）",
                               "message": "失败摘要（通过时为空串）"}]
                },
                "reference_runner": _REFERENCE_RUNNER,
                "requirements": [
                    "文件末尾的 main() 必须采用 reference_runner 参考实现（自定义 TestResult 在 startTest/addFailure/addError 中收集记录）；文件顶部定义 FP_BY_CLASS = {测试类名: 功能点编号} 供回填 fp_id",
                    "禁止重复遍历已执行的 suite：unittest 执行后会把 suite 内元素置为 None，二次迭代会触发 'NoneType' object has no attribute 'id'（attempt04 实测，marker 因此未输出、用例统计为 0）",
                    "测试方法命名约定 test_{category}_{op}_{name}（category 为 functional/exception/boundary 之一），main 中按方法名第二段回填 category；cases 元数据的 category/op/name 必须与方法名一致",
                    "每条回填的 fp_id 必须如实来自需求规格的功能点编号（覆盖率统计与缺陷跟踪以此准，漏填即覆盖率不达标）",
                    "exit code：全部通过返回 0，否则返回 1",
                    "期望值只能来自需求契约（规则/样例），禁止读取研发实现代码反推",
                    "仅用标准库；import 被测包（package 字段指定的包名）下的公开接口"
                ]
            },
        }
    }

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        return {
            "spec": perception.spec,
            "solution_design": perception.found.get("solution_design") or {},
            "code_manifest": perception.found.get("code_manifest") or {},
            "previous_test_report": perception.found.get("test_report") or {},
            "previous_bugs": perception.found.get("bug_reports") or {},
            "policies": dict(self.role.policies),
        }

    # ---------------------------------------------------------------- 执行
    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        data = generation.data or {}
        spec = perception.spec
        fps = spec.get("function_points", [])
        fp_total = len(fps)

        # 1) 写入测试文件（真实可执行）
        test_file = ""
        for file_item in data.get("files", []):
            if not isinstance(file_item, dict) or not file_item.get("path"):
                continue          # 契约外防御：跳过结构非法的文件项，不让整个节点崩溃
            self.sandbox_write(str(file_item["path"]), str(file_item["content"]))
            if str(file_item["path"]).startswith("tests/"):
                test_file = str(file_item["path"])
                self.evidence(result, "file", path=test_file, chars=len(str(file_item["content"])))
        if not test_file:
            result.status, result.error = "fail", "测试套件未生成测试文件"
            result.summary = result.error
            return

        # 1.5) 执行前静态体检：测试文件里若内联调用沙箱白名单之外的可执行程序（g++/cl 等），
        # 说明它想绕过白名单去验证非 Python 交付物；若用了 json.* 却没 import json，
        # 则结果回传协议必然崩在 NameError。两类都必须在执行前就写明，而不是事后翻 stderr。
        # 实测 task04（C++ 需求）：19 条用例全部败于 FileNotFoundError，报告里看不出根因。
        preexec_findings = self._preexec_diagnosis(test_file)
        if preexec_findings:
            self.ctx.events.emit(
                "test_environment_gap", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"note": "测试文件执行前体检发现外部程序依赖或协议缺陷",
                         "findings": preexec_findings},
            )

        # 2) 需求歧义协商（Agent 间请求-应答通信）
        negotiation = self._negotiate(data, task, result)

        # 2.5) 验证口径与选型语言的一致性：非 Python 选型下，沙箱执行的 Python 测试文件
        # 不构成对交付物的证据（实测 probe-02 因此拿到 8 项门禁全绿）。
        verification = self._verification_gap(perception.found.get("solution_design") or {})
        if verification["gap"]:
            self.ctx.events.emit(
                "test_verification_gap", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"note": verification["note"],
                         "design_language": verification["design_language"],
                         "executable_languages": verification["executable_languages"]},
            )

        # 3) 沙箱真实执行
        exec_result = self.sandbox_run(
            [self.ctx.sandbox.python, test_file],
            env_extra={"PYTHONPATH": str(self.ctx.sandbox.root)},
            result_marker="<<<MASS_RESULT>>>",
        )
        run_dir = self.ctx.run_dir / "test_runs" / f"iter{task.iteration:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stdout.txt").write_text(exec_result.stdout, encoding="utf-8")
        (run_dir / "stderr.txt").write_text(exec_result.stderr, encoding="utf-8")
        parsed = exec_result.parsed
        protocol_note = ""
        if parsed is None:
            # 结果回传协议缺失：测试文件跑完但没输出 <<<MASS_RESULT>>>（如 main() 收集逻辑崩溃）。
            # 原实现静默按 0 用例继续，验收门禁只看到"覆盖率为 0"而看不到为什么。
            protocol_note = ("测试文件未按 execution_protocol 回传结果：stdout 无 <<<MASS_RESULT>>> 标记"
                             "或标记后 JSON 不可解析，用例统计为 0（常见原因：main() 重复遍历已执行的 suite）")
            parsed = {"total": 0, "passed": 0, "failed": 0, "cases": [], "duration_ms": 0}
            self.ctx.events.emit(
                "test_protocol_violation", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"note": protocol_note,
                         "stdout_tail": exec_result.stdout.strip()[-300:],
                         "stderr_tail": exec_result.stderr.strip()[-300:]},
            )
        (run_dir / "result.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.evidence(
            result, "execution", cmd=" ".join(exec_result.cmd), exit_code=exec_result.exit_code,
            log=str((run_dir / "stdout.txt").relative_to(self.ctx.run_dir)).replace("\\", "/"),
        )

        # 4) 覆盖率统计
        coverage = self._coverage(parsed.get("cases", []), fp_total, data)

        # 5) 缺陷跟踪（含上一轮缺陷的闭环判定）
        bugs = self._track_bugs(parsed.get("cases", []), data.get("cases", []), task, perception)

        # 6) 测试报告
        root_cause = self._root_cause(preexec_findings, parsed.get("cases", []))
        summary = {
            "total": int(parsed.get("total", 0)),
            "passed": int(parsed.get("passed", 0)),
            "failed": int(parsed.get("failed", 0)),
            "pass_rate": round(
                (int(parsed.get("passed", 0)) / int(parsed.get("total", 1))) if int(parsed.get("total", 0)) else 0.0, 4
            ),
            "duration_ms": int(parsed.get("duration_ms", exec_result.duration_ms)),
        }
        test_report = {
            "title": f"《{spec.get('title', '')}》业务测试报告",
            "test_file": test_file,
            "summary": summary,
            "coverage": coverage,
            "negotiation": negotiation,
            "executions": [{
                "cmd": " ".join(exec_result.cmd),
                "exit_code": exec_result.exit_code,
                "duration_ms": exec_result.duration_ms,
                "log": f"test_runs/iter{task.iteration:02d}/stdout.txt",
            }],
            "failures": [
                {
                    "test": f"{case.get('class', '')}.{case.get('test', '')}",
                    "fp_id": case.get("fp_id", ""),
                    "category": case.get("category", ""),
                    "message": case.get("message", ""),
                }
                for case in parsed.get("cases", []) if case.get("status") != "passed"
            ],
            "case_count": data.get("case_count", {}),
            "derived_from": data.get("derived_from", ""),
            "protocol_note": protocol_note,
            "preexec_findings": preexec_findings,
            "verification": verification,
            "root_cause": root_cause,
            "root_cause_note": self._root_cause_note(preexec_findings, root_cause),
            "environment": {"python": self.ctx.sandbox.python_version()},
        }
        self.publish(
            "test_report", test_report, task,
            summary=f"用例 {summary['total']}（通过 {summary['passed']} / 失败 {summary['failed']}），"
                    f"缺陷开 {bugs['open']} / 闭环 {bugs['fixed']}",
        )
        self.publish("bug_reports", bugs, task, summary=f"缺陷总数 {bugs['total']}（open {bugs['open']}）")
        result.artifacts["test_report"] = test_report
        result.artifacts["bug_reports"] = bugs
        # 无证据不得判通过：total==0（测试文件崩溃/未按协议回传）时 failed 也为 0，
        # 原实现据此判 pass 并把 32 个缺陷自动闭环（task04-cpp-probe-02 iter02 实测）。
        result.status = ("pass" if summary["total"] > 0 and summary["failed"] == 0
                         and negotiation["open"] == 0 and not verification["gap"] else "fail")
        if verification["gap"]:
            result.error = verification["note"]
        result.summary = (
            f"业务测试：功能 {coverage['functional']['total']} / 异常 {coverage['exception']['total']} / "
            f"边界 {coverage['boundary']['total']} 条用例，通过率 {summary['pass_rate']}，"
            f"缺陷 {bugs['total']} 个（新增 {len(bugs.get('new', []))}，闭环 {bugs['fixed']}）；"
            f"歧义协商 {negotiation['closed']}/{negotiation['total']} 闭环"
            + (f"；根因：{test_report['root_cause_note']}" if preexec_findings else "")
            + (f"；验证口径缺口：{verification['note']}" if verification["gap"] else "")
            + ("" if summary["total"] else "；无执行证据：本轮没有一条用例回传结果，判失败（见 protocol_note 与 test_runs/iter*/stderr.txt）")
        )
        result.events = ["test_passed" if result.status == "pass" else "test_failed"]
        if bugs.get("new"):
            result.events.append("bug_found")
        result.broadcast_payload = {
            "summary": result.summary,
            "failed": summary["failed"],
            "open_bugs": bugs["open"],
            "pass_rate": summary["pass_rate"],
        }
        self.ctx.events.emit(
            "test_run_completed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={
                "passed": summary["passed"],
                "failed": summary["failed"],
                "total": summary["total"],
                "pass_rate": summary["pass_rate"],
                "coverage": {key: value["rate"] for key, value in coverage.items()},
                "failures": test_report["failures"],
            },
            refs=[f"test_runs/iter{task.iteration:02d}/stdout.txt"],
        )
        for bug in bugs["items"]:
            if bug.get("status") == "open" and bug.get("id") in bugs.get("new", []):
                self.ctx.events.emit(
                    "bug_reported", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                    payload={
                        "bug_id": bug["id"],
                        "fp_id": bug["fp_id"],
                        "op": bug.get("op", ""),
                        "category": bug["category"],
                        "severity": bug["severity"],
                        "symptom": bug["symptom"],
                        "test": bug["test"],
                    },
                )

    def _preexec_diagnosis(self, test_file: str) -> List[Dict[str, Any]]:
        """执行前对测试文件做 AST 体检，只挑"绕过/依赖沙箱命令白名单外的外部程序"这一类高危项。

        复用 reviewer 的审查策略（severity_map 可调），分析失败不影响主流程。
        """
        try:
            policies = dict(self.ctx.config.role("reviewer").policies)
            analysis = self.ctx.sandbox.analyze([test_file], policies)
        except Exception:                       # noqa: BLE001 - 体检是诊断，不能拖垮测试节点
            return []
        return [item.to_dict() for item in analysis.findings
                if getattr(item, "rule", "") in ("sandbox_command_bypass",
                                                 "sandbox_command_unresolved")
                or str(getattr(item, "rule", "")).endswith("_not_imported")]

    # 外部程序在本机不存在的报错特征：命中即判定为环境/构建问题，而不是交付物缺陷
    _MISSING_PROG_PATTERNS = ("filenotfounderror", "winerror 2", "no such file or directory",
                              "not found", "not recognized")
    # 测试套件自身的静态缺陷（可修，不该算环境也不该算交付物）：`<模块>_not_imported`
    _SUITE_DEFECT_SUFFIX = "_not_imported"

    def _root_cause(self, findings: List[Dict[str, Any]],
                    cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把"测试全败/无结果"归到四类根因中的一类，避免环境问题被记成研发缺陷、
        也避免测试套件自身的缺陷被推给环境。

        task04（C++ 需求）实测：32 条用例全败于 FileNotFoundError（本机没有编译器），
        原实现只报"32 个缺陷"并回退给研发修复，回退预算耗尽后才以失败终止——
        根因其实是环境/构建问题，研发再怎么改也改不出一个不存在的 g++。
        """
        if not findings:
            return {"category": "", "evidence": []}
        evidence = []
        for case in cases:
            message = str(case.get("message", "")).lower()
            if any(pattern in message for pattern in self._MISSING_PROG_PATTERNS):
                evidence.append(f"{case.get('class', '')}.{case.get('test', '')}: "
                                f"{str(case.get('message', ''))[:120]}")
        rules = {str(item.get("rule", "")) for item in findings}
        if any(rule.endswith(self._SUITE_DEFECT_SUFFIX) for rule in rules):
            category = "test_suite_defect"
        elif evidence:
            category = "environment_or_build"
        else:
            category = "delivery_unverifiable"
        return {"category": category, "evidence": evidence[:5], "evidence_total": len(evidence)}

    def _root_cause_note(self, findings: List[Dict[str, Any]], root_cause: Dict[str, Any]) -> str:
        if not findings:
            return ""
        # 同一条规则的多个命中（如两处 subprocess.run）只保留一句说明，
        # 否则事件与节点 summary 会被重复文本淹没（task04-cpp-probe-03 iter02 实测）。
        detail = "；".join(dict.fromkeys(
            str(item.get("message", "")) for item in findings if item.get("message")))
        if root_cause.get("category") == "test_suite_defect":
            return (f"【测试套件缺陷】{detail}。失败发生在测试文件自身（执行前静态可判定），"
                    "不属于交付物缺陷，也不是环境缺失——需按 execution_protocol 重新生成测试文件")
        if root_cause.get("category") == "environment_or_build":
            return (f"【环境/构建问题】{detail}。本机缺少被测交付物所需的外部工具链，"
                    f"{root_cause.get('evidence_total', 0)} 条用例报"
                    f"`{root_cause['evidence'][0].split(': ',1)[-1] if root_cause.get('evidence') else ''}`；"
                    "这类失败无法靠修改交付物代码消除，需补齐环境或改用可验证的语言")
        return (f"【交付物无法验证】{detail}。沙箱命令白名单只校验框架发起的命令，"
                "测试文件内部再起的程序会绕过它，非 Python 交付物因此无法被真实验证")

    def _verification_gap(self, solution_design: Dict[str, Any]) -> Dict[str, Any]:
        """选型语言不是 Python 时，本轮执行不构成对交付物的验证证据。

        沙箱只能用 `python <测试文件>` 执行证据。task04-cpp-probe-02 实测：C++ 需求在回退压力下
        由研发补一份 Python 复刻 + Python 测试套件，25 条用例全过、三类覆盖率 1.0，8 项门禁全绿并
        签署"验收通过"——而被验收的 `.hpp` 从未被编译过。此处把"验证口径与选型语言不一致"写成
        可判定的事实，交由测试节点如实报失败（与 F17/F19 一样修在证据源，不改门禁表达式）。
        """
        language = str(((solution_design or {}).get("tech_choice") or {}).get("language", "")).strip()
        if not language or "python" in language.lower():
            return {"design_language": language, "executable_languages": ["python"],
                    "gap": 0, "note": ""}
        return {
            "design_language": language, "executable_languages": ["python"], "gap": 1,
            "note": (f"选型语言为 {language}，沙箱只能执行 Python：本轮用例全过验证的是 Python 测试"
                     "文件自身，不是该语言的交付物（补齐工具链或改用可验证的交付形态后才能验收）"),
        }

    # ---------------------------------------------------------------- 协商
    def _negotiate(self, data: Dict[str, Any], task: NodeTask, result: AgentResult) -> Dict[str, Any]:
        negotiation_cfg = self.ctx.config.collaboration.negotiation
        # 契约外防御：LLM 可能把 negotiation 输出成字符串或其他非对象类型，
        # 原实现直接 .get("questions") 会抛 AttributeError 整个节点崩溃（实测）。
        raw = data.get("negotiation")
        if not isinstance(raw, dict):
            raw = {}
        questions = [item for item in (raw.get("questions") or []) if isinstance(item, dict)]
        answers: List[Dict[str, Any]] = []
        # 协商预算：每个入场问题一轮请求-应答。超出预算的问题不计入 open——
        # 原实现只协商前 max_rounds 个、其余永久 open，门禁 negotiation.open==0
        # 永远无法满足（task02 实测：LLM 提 5 问只协 2 问 → 3 问永久阻塞验收）。
        cap = max(1, int(negotiation_cfg.get("max_rounds", 6)))
        admitted = questions[:cap]
        over_cap = questions[cap:]
        if not negotiation_cfg.get("enabled", True) or not admitted:
            return {
                "questions": admitted,
                "answers": answers,
                "total": len(admitted),
                "closed": 0,
                "open": len(admitted),
                "over_cap": len(over_cap),
                "status": "open" if admitted else "closed",
            }
        for question in admitted:
            reply = (self.ctx.consult or (lambda **kwargs: None))(
                requester=self.role_id,
                target_role=str(negotiation_cfg.get("responder_role", "developer")),
                question=str(question.get("question", "")),
                fp_id=str(question.get("fp_id", "")),
                node=task.node_id,
            )
            if reply:
                answers.append(reply)
        # 闭环判定按"请求顺序"配对（每个问题一次请求-应答），并由框架覆写问题上的
        # status/resolution 字段：LLM 常在应答里把多个问题合并复述，原实现只数
        # 应答条数，导致问题项自带 status=open 而汇总写 4/4 闭环，证据自相矛盾。
        # 同时要求应答正文实质非空，空话/空串不算闭环。
        min_len = int(negotiation_cfg.get("min_answer_chars", 8))
        closed = 0
        for index, question in enumerate(admitted):
            reply = answers[index] if index < len(answers) else {}
            text = str(reply.get("answer", "")).strip()
            if len(text) >= min_len:
                question["status"] = "resolved"
                question["resolution"] = text
                closed += 1
            else:
                question["status"] = "open"
                question["resolution"] = ""
        return {
            "questions": admitted,
            "answers": answers,
            "total": len(admitted),
            "closed": closed,
            "open": len(admitted) - closed,
            "over_cap": len(over_cap),
            "status": "closed" if closed >= len(admitted) else "open",
        }

    # ---------------------------------------------------------------- 覆盖率
    def _coverage(self, cases: List[Dict[str, Any]], fp_total: int, data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        coverage: Dict[str, Dict[str, Any]] = {}
        for category in ("functional", "exception", "boundary"):
            category_cases = [
                case for case in cases
                if isinstance(case, dict) and case.get("category") == category
            ]
            fps = {case.get("fp_id") for case in category_cases if case.get("fp_id")}
            coverage[category] = {
                "total": len(category_cases),
                "fps": len(fps),
                "fps_total": fp_total,
                "rate": round(len(fps) / fp_total, 4) if fp_total else 0.0,
            }
        return coverage

    # ---------------------------------------------------------------- 缺陷
    def _track_bugs(
        self,
        executed: List[Dict[str, Any]],
        generated: List[Dict[str, Any]],
        task: NodeTask,
        perception: Perception,
    ) -> Dict[str, Any]:
        method_to_case = {}
        for case in generated:
            if not isinstance(case, dict):
                continue          # 契约外防御：跳过结构非法的用例元数据
            method = f"test_{case.get('category')}_{case.get('op')}_{case.get('name')}"
            method_to_case[method] = case

        previous = {
            item.get("test"): item
            for item in ((perception.found.get("bug_reports") or {}).get("items") or [])
        }
        items: List[Dict[str, Any]] = []
        new_ids: List[str] = []
        for case in executed:
            if not isinstance(case, dict):
                continue          # 契约外防御：跳过结构非法的执行记录
            method = str(case.get("test", ""))
            test_ref = f"{case.get('class', '')}.{method}"
            status = case.get("status", "passed")
            prior = previous.get(test_ref)
            if status == "passed":
                if prior and prior.get("status") == "open":
                    prior.update({"status": "fixed", "fixed_iteration": task.iteration,
                                  "verified_by": "tester"})
                    items.append(prior)
                elif prior:
                    prior.update({"status": "fixed", "fixed_iteration": prior.get("fixed_iteration", task.iteration)})
                    items.append(prior)
                continue
            meta = method_to_case.get(method, {})
            if prior:
                prior.update({
                    "status": "open",
                    "symptom": str(case.get("message", ""))[:200] or prior.get("symptom", ""),
                    "detected_iteration": prior.get("detected_iteration", task.iteration),
                })
                items.append(prior)
                continue
            import hashlib

            bug = {
                "id": "BUG-" + hashlib.sha1(test_ref.encode("utf-8")).hexdigest()[:8],
                "test": test_ref,
                "fp_id": case.get("fp_id") or meta.get("fp_id", ""),
                "op": str(meta.get("op", "")),
                "category": case.get("category") or meta.get("category", "functional"),
                "severity": _SEVERITY.get(str(case.get("category", "")), "major"),
                "symptom": str(case.get("message", ""))[:200],
                "status": "open",
                "detected_iteration": task.iteration,
                "fixed_iteration": None,
                "evidence": {"log": f"test_runs/iter{task.iteration:02d}/stdout.txt"},
            }
            items.append(bug)
            new_ids.append(bug["id"])
        # 缺陷闭环判定只在本轮真实执行到用例时生效：一条都没跑成（协议违规/文件崩溃）
        # 不构成"缺陷已修复"的证据（task04-cpp-probe-02 iter02 实测：32 缺陷被自动闭环，
        # bug_reports.open 归 0，门禁第 6 项被虚假满足）。无证据时旧缺陷原样保留为 open。
        if executed:
            for prior in previous.values():
                if prior.get("status") == "open" and not any(item.get("id") == prior.get("id") for item in items):
                    prior.update({"status": "fixed", "fixed_iteration": task.iteration, "verified_by": "tester"})
                    items.append(prior)
        else:
            for prior in previous.values():
                if prior.get("status") == "open":
                    items.append(prior)
        open_count = sum(1 for item in items if item.get("status") == "open")
        fixed_count = sum(1 for item in items if item.get("status") == "fixed")
        return {
            "items": items,
            "total": len(items),
            "open": open_count,
            "fixed": fixed_count,
            "closed": fixed_count,
            "new": new_ids,
        }
