"""Reviewer Agent：代码审查 / 技术方案评审 / 验收签署。"""

from __future__ import annotations

from typing import Any, Dict

from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from .base import BaseAgent, Perception

__all__ = ["ReviewerAgent"]


class ReviewerAgent(BaseAgent):
    role_id = "reviewer"
    task_kinds = {
        "default": "code_review",
        "review": "code_review",
        "solution_review": "design_review",
        "acceptance": "acceptance_signoff",
    }
    contracts = {
        "review": {
            "reviewed_files": [],
            "findings": [{"rule": "", "severity": "critical|major|minor|info", "file": "", "line": 0,
                          "symbol": "", "message": ""}],
            "blocking": 0,
            "stats": {"by_severity": {}, "by_rule": {}},
            "coverage": {"function_points": 0, "implemented": 0, "missing": []},
            "verdict": "pass|reject",
            "summary": "",
            "recommendations": [],
        },
        "solution_review": {"findings": [], "blocking": 0, "verdict": "pass|reject", "summary": ""},
        "acceptance": {"verdict": "pass|reject", "opinion": "", "blocking_reasons": []},
        "default": {},
    }

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        manifest = perception.found.get("code_manifest") or {}
        if task.node_id == "review":
            analysis = self.ctx.sandbox.analyze(list(manifest.get("files", [])), dict(self.role.policies))
            return {
                "spec": perception.spec,
                "code_manifest": manifest,
                "static_analysis": {
                    "findings": [finding.to_dict() for finding in analysis.findings],
                    "loc": analysis.loc,
                    "functions": analysis.functions,
                    "classes": analysis.classes,
                    "syntax_errors": analysis.syntax_errors,
                },
                "policies": dict(self.role.policies),
            }
        if task.node_id == "solution_review":
            return {"spec": perception.spec, "solution_design": perception.found.get("solution_design") or {}}
        return {
            "spec": perception.spec,
            "test_report": perception.found.get("test_report") or {},
            "review_report": perception.found.get("review_report") or {},
            "bug_reports": perception.found.get("bug_reports") or {},
        }

    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        data = generation.data or {}
        if task.node_id == "acceptance":
            self._apply_signoff(task, data, result)
            return
        verdict = str(data.get("verdict", "reject"))
        artifact_name = "review_report" if task.node_id == "review" else "design_review"
        self.publish(
            artifact_name, data, task,
            summary=f"verdict={verdict}，阻塞 {data.get('blocking', 0)} 项，"
                    f"发现 {len(data.get('findings', []))} 项",
        )
        result.artifacts[artifact_name] = data
        result.status = "pass" if verdict == "pass" else "fail"
        result.summary = str(data.get("summary", "")) or f"{task.title}结论：{verdict}"
        result.events = ["review_passed" if verdict == "pass" else "review_rejected"]
        result.broadcast_payload = {
            "verdict": verdict,
            "blocking": data.get("blocking", 0),
            "summary": result.summary,
        }
        self.ctx.events.emit(
            "review_completed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={
                "verdict": verdict,
                "blocking": data.get("blocking", 0),
                "total": len(data.get("findings", [])),
                "findings": data.get("findings", []),
                "artifact": artifact_name,
            },
            refs=[f"artifacts/{artifact_name}"],
        )

    def _apply_signoff(self, task: NodeTask, data: Dict[str, Any], result: AgentResult) -> None:
        verdict = str(data.get("verdict", "reject"))
        self.publish("acceptance_opinion", data, task, summary=f"签署意见：{verdict}")
        result.artifacts["acceptance_opinion"] = data
        result.status = "pass"   # 门禁结论由 Leader 依据 criteria 表达式与签署意见综合判定
        result.summary = f"验收签署：{data.get('opinion', '')}"
        result.events = []
        result.metrics["signoff_verdict"] = verdict
        self.evidence(result, "signoff", verdict=verdict, reasons=data.get("blocking_reasons", []))
