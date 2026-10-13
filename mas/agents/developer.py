"""研发 Agent：技术方案设计 / 编码实现 / 缺陷修复（含回归自测报告）。"""

from __future__ import annotations

from typing import Any, Dict, List

from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from .base import BaseAgent, Perception

__all__ = ["DeveloperAgent"]


class DeveloperAgent(BaseAgent):
    role_id = "developer"
    task_kinds = {
        "default": "implementation",
        "solution": "solution_design",
        "implement": "implementation",
        "optimize": "implementation",
    }
    contracts = {
        "solution": {
            "tech_choice": {"language": "", "dependencies": [], "rationale": ""},
            "candidates": [{"name": "", "scores": {}, "pros": [], "cons": []}],
            "evaluation": {"method": "", "selected": "", "reason": ""},
            "design": {"architecture": "", "modules": [], "error_strategy": "", "testing_strategy": "",
                       "ambiguity_resolutions": []},
            "risks": [{"risk": "", "mitigation": ""}],
            "implementation_plan": [],
            "requirement_coverage": [{"id": "FP-01", "target": "模块.函数"}],
        },
        "default": {
            "mode": "first|rework|fix",
            "package": "",
            "files": [{"path": "", "content": ""}],
            "fp_mapping": {"FP-01": "function_name"},
            "functions": [],
            "defects_injected": [],
            "fix_targets": [],
            "notes": "",
        },
    }

    # ---------------------------------------------------------------- 模式判定
    def _mode(self, perception: Perception, node_id: str = "implement") -> str:
        if node_id == "optimize":
            # 修复/优化节点永远走 fix 通道：绝不重新引入已修复的缺陷
            return "fix"
        bugs = perception.found.get("bug_reports") or {}
        if int(bugs.get("open", 0) or 0) > 0:
            return "fix"
        review = perception.found.get("review_report") or {}
        if review.get("verdict") == "reject":
            return "rework"
        return "rework" if perception.found.get("code_manifest") else "first"

    # ---------------------------------------------------------------- 上下文
    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        spec = perception.spec
        bugs = perception.found.get("bug_reports") or {}
        review = perception.found.get("review_report") or {}
        if task.node_id == "solution":
            return {"spec": spec}
        return {
            "spec": spec,
            "mode": self._mode(perception, task.node_id),
            "open_bugs": [item for item in (bugs.get("items") or []) if item.get("status") == "open"],
            "review_findings": review.get("blocking_items") or review.get("findings") or [],
            "test_report": perception.found.get("test_report") or {},
            "node": task.node_id,
            "iteration": task.iteration,
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
        if task.node_id == "solution":
            self._apply_solution(task, generation, perception, result)
        elif task.node_id == "optimize":
            self._apply_fix(task, generation, perception, context, result)
        else:
            self._apply_implementation(task, generation, perception, context, result)

    # ---------------------------------------------------------------- 方案设计
    def _apply_solution(self, task: NodeTask, generation: Generation, perception: Perception,
                        result: AgentResult) -> None:
        design = generation.data or {}
        evaluation = design.get("evaluation") or {}
        if not evaluation.get("selected"):
            result.status, result.error = "fail", "技术方案缺少选型结论（evaluation.selected）"
            result.summary = result.error
            return
        coverage = design.get("requirement_coverage") or []
        spec_fps = [fp.get("id") for fp in perception.spec.get("function_points", [])]
        covered = {item.get("id") for item in coverage}
        if spec_fps and set(spec_fps) - covered:
            result.status = "fail"
            result.error = f"方案未覆盖功能点: {', '.join(sorted(set(spec_fps) - covered))}"
            result.summary = result.error
            return
        self.publish(
            "solution_design", design, task,
            summary=f"选型『{evaluation.get('selected')}』，候选 {len(design.get('candidates', []))} 个，"
                    f"风险 {len(design.get('risks', []))} 项",
        )
        result.status = "pass"
        result.artifacts["solution_design"] = design
        result.summary = (
            f"技术方案完成：选型『{evaluation.get('selected')}』（候选 {len(design.get('candidates', []))} 个、"
            f"模块 {len((design.get('design') or {}).get('modules', []))} 个、"
            f"风险 {len(design.get('risks', []))} 项），功能点覆盖 {len(covered)}/{len(spec_fps)}"
        )
        result.events = ["design_ready"]
        result.broadcast_payload = {"summary": result.summary, "selected": evaluation.get("selected")}
        self.ctx.events.emit(
            "design_ready", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload=result.broadcast_payload,
        )

    # ---------------------------------------------------------------- 编码实现
    def _write_files(self, files: List[Dict[str, Any]], task: NodeTask, result: AgentResult) -> List[str]:
        written = []
        for file_item in files or []:
            path = self.sandbox_write(str(file_item["path"]), str(file_item["content"]))
            written.append(str(file_item["path"]))
            self.evidence(result, "file", path=str(file_item["path"]), chars=len(str(file_item["content"])))
        return written

    def _self_check(self, package: str, task: NodeTask) -> Dict[str, Any]:
        check = self.sandbox_run(
            [self.ctx.sandbox.python, "-c", f"import {package}; print('import-ok')"],
            env_extra={"PYTHONPATH": str(self.ctx.sandbox.root)},
        )
        return {
            "cmd": " ".join(check.cmd),
            "exit_code": check.exit_code,
            "ok": check.ok,
            "stdout_tail": check.stdout.strip()[-200:],
            "stderr_tail": check.stderr.strip()[-200:],
            "duration_ms": check.duration_ms,
        }

    def _apply_implementation(
        self, task: NodeTask, generation: Generation, perception: Perception,
        context: Dict[str, Any], result: AgentResult,
    ) -> None:
        data = generation.data or {}
        files = data.get("files") or []
        if not files:
            result.status, result.error = "fail", "实现未产出任何文件"
            result.summary = result.error
            return
        written = self._write_files(files, task, result)
        package = data.get("package") or self.ctx.config.llm.get("offline", {}).get("package_name", "delivery")
        self_check = self._self_check(package, task)
        stats = self.ctx.sandbox.code_stats(written)
        manifest = {
            "package": package,
            "files": written,
            "loc": stats.get("total_loc", 0),
            "functions": data.get("functions", []),
            "fp_mapping": data.get("fp_mapping", {}),
            "self_check": self_check,
            "mode": data.get("mode", context.get("mode")),
            "defects_injected": data.get("defects_injected", []),
            "notes": data.get("notes", ""),
            "file_stats": stats.get("files", []),
        }
        summary_data = {
            "mode": manifest["mode"],
            "files": written,
            "loc": manifest["loc"],
            "fp_mapping": manifest["fp_mapping"],
            "self_check": self_check,
            "notes": data.get("notes", ""),
        }
        self.publish("code_manifest", manifest, task, summary=f"{len(written)} 个文件，{manifest['loc']} 行")
        self.publish("implementation_summary", summary_data, task, summary=f"模式 {manifest['mode']}")
        result.artifacts["code_manifest"] = manifest
        result.artifacts["implementation_summary"] = summary_data
        result.status = "pass" if self_check.get("ok") else "fail"
        result.summary = (
            f"编码实现（{manifest['mode']}）：{len(written)} 个文件、{manifest['loc']} 行代码、"
            f"功能点映射 {len(manifest['fp_mapping'])} 个；导入自检{'通过' if self_check.get('ok') else '失败'}"
        )
        result.events = ["implementation_ready"]
        result.broadcast_payload = {
            "summary": result.summary,
            "files": written,
            "self_check_ok": self_check.get("ok", False),
        }
        self.ctx.events.emit(
            "implementation_ready", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={"files": written, "loc": manifest["loc"], "mode": manifest["mode"]},
        )
        if not self_check.get("ok"):
            result.error = f"导入自检失败: {self_check.get('stderr_tail', '')[:200]}"

    # ---------------------------------------------------------------- 缺陷修复
    def _apply_fix(
        self, task: NodeTask, generation: Generation, perception: Perception,
        context: Dict[str, Any], result: AgentResult,
    ) -> None:
        data = generation.data or {}
        files = data.get("files") or []
        if not files:
            result.status, result.error = "fail", "修复未产出任何文件"
            result.summary = result.error
            return
        written = self._write_files(files, task, result)
        package = data.get("package") or (perception.found.get("code_manifest") or {}).get("package", "delivery")
        self_check = self._self_check(package, task)

        open_bugs = context.get("open_bugs") or []
        test_report = perception.found.get("test_report") or {}
        test_file = test_report.get("test_file", "")
        fix_items: List[Dict[str, Any]] = []
        all_ok = bool(self_check.get("ok"))
        for bug in open_bugs:
            item = {
                "bug_id": bug.get("id"),
                "fp_id": bug.get("fp_id"),
                "op": bug.get("op", ""),
                "category": bug.get("category"),
                "strategy": (data.get("fix_strategies") or {}).get(bug.get("op", ""), "按需求契约语义修正实现"),
                "regression": None,
                "regression_ok": None,
            }
            if test_file and bug.get("test"):
                regression = self.sandbox_run(
                    [self.ctx.sandbox.python, test_file, "--only", str(bug["test"])],
                    env_extra={"PYTHONPATH": str(self.ctx.sandbox.root)},
                    result_marker="<<<MASS_RESULT>>>",
                )
                parsed = regression.parsed or {}
                item["regression"] = {
                    "cmd": " ".join(regression.cmd),
                    "exit_code": regression.exit_code,
                    "failed": parsed.get("failed", regression.exit_code),
                    "stdout_tail": regression.stdout.strip()[-400:],
                }
                item["regression_ok"] = bool(regression.ok)
                all_ok = all_ok and bool(regression.ok)
            fix_items.append(item)
            self.ctx.events.emit(
                "bug_fixed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                payload={
                    "bug_id": bug.get("id"),
                    "fp_id": bug.get("fp_id"),
                    "op": bug.get("op"),
                    "strategy": item["strategy"],
                    "regression_ok": item["regression_ok"],
                },
            )

        stats = self.ctx.sandbox.code_stats(written)
        manifest = {
            "package": package,
            "files": written,
            "loc": stats.get("total_loc", 0),
            "functions": data.get("functions", []),
            "fp_mapping": data.get("fp_mapping", {}),
            "self_check": self_check,
            "mode": "fix",
            "defects_injected": [],
            "notes": data.get("notes", ""),
            "file_stats": stats.get("files", []),
        }
        fix_summary = {
            "fixed": len(fix_items),
            "items": fix_items,
            "self_check": self_check,
            "fix_targets": data.get("fix_targets", []),
        }
        fix_test_report = {
            "title": "缺陷修复回归自测报告（研发 Agent）",
            "scope": [bug.get("id") for bug in open_bugs],
            "results": [
                {
                    "bug_id": item["bug_id"],
                    "test": next((bug.get("test") for bug in open_bugs if bug.get("id") == item["bug_id"]), ""),
                    "regression_ok": item["regression_ok"],
                    "evidence": item["regression"],
                }
                for item in fix_items
            ],
            "all_pass": all_ok,
            "conclusion": ("全部缺陷的定向回归用例通过，等待测试 Agent 复测确认。"
                           if all_ok else "存在回归未通过的缺陷，需继续修复。"),
        }
        self.publish("code_manifest", manifest, task, summary=f"修复后 {len(written)} 个文件，{manifest['loc']} 行")
        self.publish("fix_summary", fix_summary, task, summary=f"修复 {len(fix_items)} 个缺陷")
        self.publish("fix_test_report", fix_test_report, task, summary="回归自测报告")
        result.artifacts.update({
            "code_manifest": manifest,
            "fix_summary": fix_summary,
            "fix_test_report": fix_test_report,
        })
        result.status = "pass" if all_ok else "fail"
        result.summary = (
            f"缺陷修复：处理 {len(fix_items)} 个缺陷（定向回归 "
            f"{sum(1 for item in fix_items if item['regression_ok'])}/{len(fix_items)} 通过），"
            f"导入自检{'通过' if self_check.get('ok') else '失败'}；已产出回归自测报告"
        )
        result.events = ["bug_fixed"]
        result.broadcast_payload = {"summary": result.summary, "fixed": [item["bug_id"] for item in fix_items]}
        self.ctx.events.emit(
            "bug_fixed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={"fixed": len(fix_items), "all_regression_ok": all_ok},
        )

    # ---------------------------------------------------------------- 协商应答
    def consult(self, question: str, fp_id: str = "") -> Dict[str, Any]:
        """响应其他 Agent 的澄清请求（Agent 间双向通信）。"""
        prompt = self.ctx.prompts.build(
            role=self.role,
            node=type("N", (), {"id": "consultation", "title": "需求歧义澄清", "role": "developer", "kind": "task"})(),
            task_kind="consultation",
            iteration=0,
            directive=f"请回答测试 Agent 的澄清请求（涉及 {fp_id or '需求整体'}）",
            artifacts_brief="",
            bus_digest=self.ctx.bus.digest(self.role_id),
            perception=["requirement_spec"],
            missing=[],
            experience=self.ctx.experience.retrieve(
                role=self.role_id, tags=list(self.role.learning_tags) + ["需求歧义"],
                task_text=question, tier_keys=["L2", "L3"], current_run=self.ctx.run_id,
            ),
            contract={"question": "原始问题", "answer": "结论", "impact": "对实现与测试的影响"},
            context={"question": question, "fp_id": fp_id},
        )
        generation = self.ctx.llm.generate("consultation", prompt, {"question": question, "fp_id": fp_id})
        answer = generation.data or {"answer": "（无法澄清）"}
        answer.setdefault("question", question)
        answer.setdefault("fp_id", fp_id)
        return answer
