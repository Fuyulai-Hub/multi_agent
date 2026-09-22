"""测试 Agent：依据需求契约生成并真实执行功能/异常/边界用例，产出缺陷报告。"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from .base import BaseAgent, Perception

__all__ = ["TesterAgent"]

_SEVERITY = {"functional": "critical", "exception": "major", "boundary": "major"}


class TesterAgent(BaseAgent):
    role_id = "tester"
    task_kinds = {"default": "test_suite"}
    contracts = {
        "default": {
            "package": "",
            "files": [{"path": "tests/test_xxx.py", "content": ""}],
            "cases": [{"id": "TC-FP-01-functional-xxx", "fp_id": "FP-01", "category": "functional|exception|boundary",
                       "call": "", "expect": "", "assert_kind": "eq"}],
            "case_count": {"total": 0, "functional": 0, "exception": 0, "boundary": 0},
            "negotiation": {"questions": [], "answers": [], "open": 0, "status": "pending"},
            "derived_from": "",
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
            self.ctx.sandbox.write_file(str(file_item["path"]), str(file_item["content"]))
            if str(file_item["path"]).startswith("tests/"):
                test_file = str(file_item["path"])
                self.evidence(result, "file", path=test_file, chars=len(str(file_item["content"])))
        if not test_file:
            result.status, result.error = "fail", "测试套件未生成测试文件"
            result.summary = result.error
            return

        # 2) 需求歧义协商（Agent 间请求-应答通信）
        negotiation = self._negotiate(data, task, result)

        # 3) 沙箱真实执行
        exec_result = self.ctx.sandbox.run(
            [self.ctx.sandbox.python, test_file],
            env_extra={"PYTHONPATH": str(self.ctx.sandbox.root)},
            result_marker="<<<MASS_RESULT>>>",
        )
        run_dir = self.ctx.run_dir / "test_runs" / f"iter{task.iteration:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stdout.txt").write_text(exec_result.stdout, encoding="utf-8")
        (run_dir / "stderr.txt").write_text(exec_result.stderr, encoding="utf-8")
        parsed = exec_result.parsed or {"total": 0, "passed": 0, "failed": 0, "cases": [], "duration_ms": 0}
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
        result.status = "pass" if summary["failed"] == 0 and negotiation["open"] == 0 else "fail"
        result.summary = (
            f"业务测试：功能 {coverage['functional']['total']} / 异常 {coverage['exception']['total']} / "
            f"边界 {coverage['boundary']['total']} 条用例，通过率 {summary['pass_rate']}，"
            f"缺陷 {bugs['total']} 个（新增 {len(bugs.get('new', []))}，闭环 {bugs['fixed']}）；"
            f"歧义协商 {negotiation['closed']}/{negotiation['total']} 闭环"
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

    # ---------------------------------------------------------------- 协商
    def _negotiate(self, data: Dict[str, Any], task: NodeTask, result: AgentResult) -> Dict[str, Any]:
        negotiation_cfg = self.ctx.config.collaboration.negotiation
        questions = list((data.get("negotiation") or {}).get("questions", []))
        answers: List[Dict[str, Any]] = []
        if not negotiation_cfg.get("enabled", True) or not questions:
            return {
                "questions": questions,
                "answers": answers,
                "total": len(questions),
                "closed": 0,
                "open": len(questions),
                "status": "open" if questions else "closed",
            }
        for question in questions[: int(negotiation_cfg.get("max_rounds", 2))]:
            reply = (self.ctx.consult or (lambda **kwargs: None))(
                requester=self.role_id,
                target_role=str(negotiation_cfg.get("responder_role", "developer")),
                question=str(question.get("question", "")),
                fp_id=str(question.get("fp_id", "")),
                node=task.node_id,
            )
            if reply:
                answers.append(reply)
        closed = len(answers)
        return {
            "questions": questions,
            "answers": answers,
            "total": len(questions),
            "closed": closed,
            "open": len(questions) - closed,
            "status": "closed" if closed >= len(questions) else "open",
        }

    # ---------------------------------------------------------------- 覆盖率
    def _coverage(self, cases: List[Dict[str, Any]], fp_total: int, data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        coverage: Dict[str, Dict[str, Any]] = {}
        for category in ("functional", "exception", "boundary"):
            category_cases = [case for case in cases if case.get("category") == category]
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
            method = f"test_{case.get('category')}_{case.get('op')}_{case.get('name')}"
            method_to_case[method] = case

        previous = {
            item.get("test"): item
            for item in ((perception.found.get("bug_reports") or {}).get("items") or [])
        }
        items: List[Dict[str, Any]] = []
        new_ids: List[str] = []
        for case in executed:
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
        for prior in previous.values():
            if prior.get("status") == "open" and not any(item.get("id") == prior.get("id") for item in items):
                prior.update({"status": "fixed", "fixed_iteration": task.iteration, "verified_by": "tester"})
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
