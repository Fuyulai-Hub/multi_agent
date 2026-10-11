"""报告 Agent：证据统计与验收报告生成。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from typing import Any, Dict, List

from ..errors import ConfigError
from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from ..skills import load_skill, save_skill, validate_skill
from .base import BaseAgent, Perception

__all__ = ["ReporterAgent"]


class ReporterAgent(BaseAgent):
    role_id = "reporter"
    task_kinds = {"default": "acceptance_report", "skill_distill": "skill_distill"}
    contracts = {
        "default": {
            "markdown": "完整验收报告（Markdown）",
            "json": {"run_id": "", "verdict": "pass|fail", "test_summary": {}, "bugs": {}},
        },
        "skill_distill": {
            "markdown": "技能补充说明（Markdown，可为空）",
            "json": {"name": "", "description": "", "when_to_use": [], "trigger": {},
                     "inputs": [], "steps": [], "error_handling": [], "checkpoints": []},
        },
    }

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        if self.task_kind_for(task) == "skill_distill":
            return {"facts": self.ctx.run_state.get("distill_facts") or {}}
        return {"facts": self._build_facts(task, perception)}

    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        if self.task_kind_for(task) == "skill_distill":
            self._apply_skill_distill(generation, result)
            return
        data = generation.data or {}
        markdown = str(data.get("markdown", ""))
        json_summary = dict(data.get("json") or {})
        if not markdown:
            result.status, result.error = "fail", "报告生成失败：无内容"
            result.summary = result.error
            return

        reports_dir = self.ctx.run_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / "acceptance_report.md"
        report_path.write_text(markdown, encoding="utf-8")
        json_path = reports_dir / "acceptance_report.json"
        json_path.write_text(json.dumps(json_summary, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")

        # 证据索引
        evidence_index = self._evidence_index()
        index_path = self.ctx.run_dir / "evidence" / "index.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("w", encoding="utf-8") as handle:
            for item in evidence_index["items"]:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

        # 全局报告归档（跨运行汇总可见）
        global_dir = self.ctx.config.reports_dir
        global_dir.mkdir(parents=True, exist_ok=True)
        archived = global_dir / f"{self.ctx.run_id}-acceptance_report.md"
        shutil.copyfile(report_path, archived)

        # 自学习效果报告
        learning_report_path = None
        if self.role.policy("include_learning_report", True):
            learning_report_path = global_dir / "learning_report.md"
            learning_report_path.write_text(self.ctx.experience.learning_report_markdown(),
                                            encoding="utf-8")

        verdict = str(json_summary.get("verdict", "fail"))
        self.publish(
            "acceptance_report",
            {
                "path": str(report_path.relative_to(self.ctx.run_dir)).replace("\\", "/"),
                "json_path": str(json_path.relative_to(self.ctx.run_dir)).replace("\\", "/"),
                "verdict": verdict,
                "summary": json_summary,
                "archived": str(archived),
            },
            task, summary=f"验收结论：{verdict}",
        )
        self.publish(
            "evidence_index",
            {"count": evidence_index["count"], "index": "evidence/index.jsonl",
             "items": evidence_index["items"][:50]},
            task, summary=f"证据条目 {evidence_index['count']} 条",
        )
        result.status = "pass"
        result.artifacts["acceptance_report"] = json_summary
        result.summary = (
            f"验收报告已生成（结论：{verdict}），证据索引 {evidence_index['count']} 条；"
            f"报告路径 {report_path.relative_to(self.ctx.run_dir)}"
            + (f"；学习报告 {learning_report_path.name}" if learning_report_path else "")
        )
        result.events = ["report_ready"]
        result.broadcast_payload = {"verdict": verdict, "report": str(report_path)}
        self.evidence(result, "report", path=str(report_path), verdict=verdict)

    # ---------------------------------------------------------------- 流程沉淀
    def _apply_skill_distill(self, generation: Generation, result: AgentResult) -> None:
        """把本次运行的成功路径落盘为技能（只在用户显式要求时被调用）。"""
        data = generation.data or {}
        meta = dict(data.get("json") or {})
        body = str(data.get("markdown") or "")
        if not meta.get("steps"):
            result.status, result.error = "fail", "技能沉淀失败：未产出执行步骤 steps"
            result.summary = result.error
            return

        skills_dir = self.ctx.config.skills_dir
        skill_id = str(meta.get("name") or "").strip() or f"distilled-{self.ctx.run_id}"
        meta["name"] = skill_id
        meta.setdefault("source", "distilled")
        meta.setdefault("source_run", self.ctx.run_id)
        meta.setdefault("version", "0.1.0")

        try:
            path = save_skill(skills_dir, skill_id, meta, body, overwrite=True)
            problems = validate_skill(load_skill(skills_dir, skill_id))
        except ConfigError as exc:
            result.status, result.error = "fail", f"技能落盘失败：{exc}"
            result.summary = result.error
            return

        result.status = "pass"
        result.artifacts["skill"] = {"id": skill_id, "path": str(path), "problems": problems}
        result.summary = (
            f"已沉淀技能 {skill_id} → {path}"
            + (f"；元信息仍缺：{'、'.join(problems)}" if problems else "（元信息校验通过）")
        )
        result.metrics["skill_id"] = skill_id
        self.evidence(result, "skill", id=skill_id, path=str(path))

    # ---------------------------------------------------------------- 事实收集
    def _build_facts(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        found = perception.found
        events = self.ctx.events
        stats = events.stats()
        run_state = self.ctx.run_state
        fixes = []
        fix_summary = found.get("fix_summary") or {}
        for item in fix_summary.get("items", []):
            fixes.append({
                "bug_id": item.get("bug_id"),
                "op": item.get("op"),
                "strategy": item.get("strategy"),
                "regression_ok": item.get("regression_ok"),
            })
        learned = [event.payload for event in events.filter(["experience_learned"])]
        injected_ids: List[str] = list(run_state.get("injected_experience_ids") or [])
        learning = {
            "injected": len(injected_ids),
            "injected_total": int(run_state.get("injected_total", 0)),
            "injected_tiers": run_state.get("injected_tiers") or {},
            "learned": len(learned),
            "lessons": [
                {"tier": item.get("tier"), "lesson": item.get("lesson"), "trigger": item.get("trigger")}
                for item in learned
            ],
            "improvement": self.ctx.experience.improvement_summary(),
        }
        return {
            "run_id": self.ctx.run_id,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "spec": found.get("requirement_spec") or {},
            "solution": found.get("solution_design") or {},
            "manifest": found.get("code_manifest") or {},
            "review": found.get("review_report") or {},
            "test": found.get("test_report") or {},
            "bugs": found.get("bug_reports") or {},
            "gate": found.get("acceptance_gate") or {},
            "fixes": fixes,
            "learning": learning,
            "event_stats": stats,
            "timeline": events.timeline(),
            "metrics": run_state.get("metrics") or {},
            "rollbacks": run_state.get("rollbacks", 0),
            "review_rejections": run_state.get("review_rejections", 0),
            "node_executions": run_state.get("node_executions", 0),
            "evidence_paths": {
                "事件日志": "events.jsonl",
                "消息总线": "messages.jsonl",
                "提示词（含经验注入）": "prompts/",
                "产物版本": "artifacts/",
                "交付代码": "code/",
                "测试执行日志": "test_runs/",
                "验收报告": "reports/acceptance_report.md",
            },
        }

    def _evidence_index(self) -> Dict[str, Any]:
        items: List[Dict[str, Any]] = []
        blackboard = self.ctx.blackboard
        for name in blackboard.names():
            for ref in blackboard.versions(name):
                items.append({
                    "kind": "artifact",
                    "name": name,
                    "version": ref.version,
                    "producer": ref.producer,
                    "node": ref.node,
                    "iteration": ref.iteration,
                    "path": str(ref.path.relative_to(self.ctx.run_dir)).replace("\\", "/"),
                    "digest": ref.digest,
                })
        for event in self.ctx.events.events:
            if event.type in ("sandbox_exec", "test_run_completed", "gate_evaluated", "rollback",
                              "bug_reported", "bug_fixed", "negotiation_closed"):
                items.append({
                    "kind": "event",
                    "type": event.type,
                    "seq": event.seq,
                    "agent": event.agent,
                    "node": event.node,
                    "payload_keys": sorted(event.payload.keys()),
                })
        for path in sorted((self.ctx.run_dir / "test_runs").glob("iter*/result.json")) if (self.ctx.run_dir / "test_runs").exists() else []:
            items.append({
                "kind": "test_run",
                "path": str(path.relative_to(self.ctx.run_dir)).replace("\\", "/"),
            })
        return {"count": len(items), "items": items}
