"""Reviewer Agent：代码审查 / 技术方案评审 / 验收签署。"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..intent import resolve_delivery_shape, shape_family
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

    # 只有非纯后端形态才开启的规则：Web 服务生命周期（docs/15 补丁 3/4）与前端产物静态证据
    # （补丁 5）。backend_only 下 severity_map 不含这些键 → sandbox 不检出，行为零变更。
    _WEB_ONLY_SEVERITY = {
        "web_service_lifecycle": "major",
        "web_port_literal": "major",
        "web_service_not_daemon": "major",
        "html_placeholder_unresolved": "major",
        "html_asset_missing": "major",
        "html_tag_unclosed": "minor",
        "js_syntax_error": "info",
    }

    def _analysis_policies(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """按交付形态调整审查策略（docs/15 补丁 1 / 3 / 4 / 5）。

        ``delivery_shape != backend_only`` 时：
        - ``network_import`` 降为 info：HTTP 服务 import http/socket 是需求本身，不是高危外连；
        - 开启 Web 服务生命周期与前端产物规则，补齐这两类此前完全空白的静态证据。

        豁免与新增都只覆盖上表这些规则，``eval_usage`` / ``sandbox_command_bypass`` 等不动。
        backend_only（默认）任务会把上表这些键从 severity_map **剔除**：sandbox 以「规则键是否在
        severity_map 里」作为开关，留着默认值会让这类 finding 在全形态下都产出，破坏验收标准 #1。
        """
        policies = dict(self.role.policies)
        shape, _, _ = resolve_delivery_shape(spec)
        severity_map = dict(policies.get("severity_map") or {})
        if shape_family(shape) == "full_stack":
            severity_map["network_import"] = "info"
            for rule, default in self._WEB_ONLY_SEVERITY.items():
                severity_map.setdefault(rule, default)   # role YAML 已声明时以其为准
        else:
            for rule in self._WEB_ONLY_SEVERITY:
                severity_map.pop(rule, None)
        policies["severity_map"] = severity_map
        return policies

    def _emit_exemption_trace(
        self, task: NodeTask, spec: Dict[str, Any], findings: List[Dict[str, Any]]
    ) -> None:
        """豁免留痕（docs/15 补丁 1）：降级了哪几条规则必须能在事件里查到。

        否则线上只能看到"这次 import socket 没被拦"，却查不出是需求形态触发的豁免、
        还是配置被人改过——这两者的处置方式完全不同。
        """
        shape, source, _ = resolve_delivery_shape(spec)
        if shape_family(shape) != "full_stack":
            return
        relaxed = [item for item in findings if item.get("rule") == "network_import"]
        self.ctx.events.emit(
            "network_finding_relaxed", agent=self.role_id, node=task.node_id,
            iteration=task.iteration,
            payload={"delivery_shape": shape, "delivery_shape_source": source,
                     "rule": "network_import", "from": "major", "to": "info",
                     "count": len(relaxed),
                     "note": "前后端交付形态下 HTTP 依赖属需求本身，已降为提示级，不计入 blocking"},
        )

    def _web_asset_findings(self, manifest: Dict[str, Any], policies: Dict[str, Any]) -> List[Dict[str, Any]]:
        """前端产物静态证据（docs/15 补丁 5）：Python AST 分析器只认 .py，这里单独分析。"""
        files = [str(item) for item in (manifest.get("files") or [])]
        if not files:
            return []
        try:
            findings = self.ctx.sandbox.analyze_web_assets(files, policies)
        except Exception:                       # noqa: BLE001 - 证据采集失败不应拖垮审查
            return []
        return [item.to_dict() for item in findings]

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        manifest = perception.found.get("code_manifest") or {}
        if task.node_id == "review":
            policies = self._analysis_policies(perception.spec)
            analysis = self.ctx.sandbox.analyze(list(manifest.get("files", [])), policies)
            findings = [finding.to_dict() for finding in analysis.findings]
            self._emit_exemption_trace(task, perception.spec, findings)
            web_findings = self._web_asset_findings(manifest, policies)
            if web_findings:
                findings.extend(web_findings)
                self.ctx.events.emit(
                    "web_static_evidence", agent=self.role_id, node=task.node_id,
                    iteration=task.iteration,
                    payload={"delivery_shape": resolve_delivery_shape(perception.spec)[0],
                             "rules": sorted({item["rule"] for item in web_findings}),
                             "count": len(web_findings)},
                )
            return {
                "spec": perception.spec,
                "code_manifest": manifest,
                "static_analysis": {
                    "findings": findings,
                    "loc": analysis.loc,
                    "functions": analysis.functions,
                    "classes": analysis.classes,
                    "syntax_errors": analysis.syntax_errors,
                },
                "policies": policies,
            }
        if task.node_id == "solution_review":
            return {"spec": perception.spec, "solution_design": perception.found.get("solution_design") or {}}
        return {
            "spec": perception.spec,
            "test_report": perception.found.get("test_report") or {},
            "review_report": perception.found.get("review_report") or {},
            "bug_reports": perception.found.get("bug_reports") or {},
        }

    # ---------------------------------------------------------------- Prompt 增强
    def build_prompt(self, task: NodeTask, perception: Perception, experience: Any) -> Any:
        """审查节点把「AST 静态证据 + 交付源码」补进 Prompt。

        原实现只在 build_context 里计算 static_analysis，而 openai_compatible 路径的
        _generate_openai 不消费 context——reviewer 只能看到黑板上的 manifest 清单，
        拿不到任何源码正文，只能以「未提供可读取的源文件内容」驳回
        （attempt03/04/05 实测，三轮同一措辞；attempt04 曾靠二次重做偶然过关）。
        离线 Provider 读 context，不受本改动影响。
        """
        bundle = super().build_prompt(task, perception, experience)
        if task.node_id != "review":
            return bundle
        manifest = perception.found.get("code_manifest") or {}
        files = [str(item) for item in (manifest.get("files") or [])]
        if not files:
            return bundle
        # 第二处 analyze（docs/15 补丁 1 强调两处都要改）：只改 build_context 的话，
        # build_prompt 仍会把 major 级别的 network_import 塞进 Prompt，LLM 照样判阻塞。
        policies = self._analysis_policies(perception.spec)
        analysis = self.ctx.sandbox.analyze(files, policies)
        evidence = {
            "loc": analysis.loc,
            "classes": analysis.classes,
            "functions": analysis.functions,
            "syntax_errors": analysis.syntax_errors,
            "findings": [finding.to_dict() for finding in analysis.findings],
        }
        web_findings = self._web_asset_findings(manifest, policies)
        if web_findings:
            evidence["findings"].extend(web_findings)
        sections = [
            "## 静态分析证据（框架对沙箱真实文件执行的 AST 分析，确定性结果）",
            json.dumps(evidence, ensure_ascii=False, indent=1)[:12000],
        ]
        sources: List[str] = []
        budget = 40000
        used = 0
        for relative in files:
            try:
                content = self.ctx.sandbox.read_file(relative)
            except Exception:  # noqa: BLE001 - 单文件读取失败不阻塞审查
                continue
            if used + len(content) > budget:
                content = content[: max(0, budget - used)] + "\n...(源码超长，截断)"
            sources.append(f"### {relative}\n{content}")
            used += len(content)
            if used >= budget:
                break
        if sources:
            sections.append("## 交付源码（沙箱中的真实文件内容）\n" + "\n\n".join(sources))
        bundle.user += "\n\n" + "\n\n".join(sections)
        if bundle.path:
            try:
                with bundle.path.open("a", encoding="utf-8") as fh:
                    fh.write("\n\n" + "\n\n".join(sections))
            except OSError:
                pass  # 留痕失败不影响审查
        return bundle

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
