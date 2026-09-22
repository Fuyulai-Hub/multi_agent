"""产品 Agent：需求评估与拆解。"""

from __future__ import annotations

from typing import Any, Dict

from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from .base import BaseAgent, Perception

__all__ = ["ProductAgent"]


class ProductAgent(BaseAgent):
    role_id = "product"
    task_kinds = {"default": "requirement_spec"}
    contracts = {
        "default": {
            "id": "需求编号",
            "title": "需求标题",
            "background": ["业务背景"],
            "constraints": ["技术约束"],
            "tags": ["领域标签"],
            "function_points": [
                {
                    "id": "FP-01",
                    "title": "功能点标题",
                    "description": "描述",
                    "parameters": [{"name": "参数名", "type": "类型"}],
                    "inputs": ["输入契约"],
                    "outputs": ["输出契约"],
                    "rules": ["业务规则"],
                    "samples": [{"input": "样例输入", "expect": "期望输出"}],
                    "category": "功能/异常/边界",
                    "acceptance": ["功能点级验收要点"],
                    "ambiguous": False,
                }
            ],
            "acceptance_criteria": [{"id": "AC-01", "description": "验收标准"}],
            "open_questions": [{"id": "Q-1", "question": "待澄清问题"}],
            "metrics": {"function_points": 0},
        }
    }

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        source = perception.found.get("requirement_source") or {}
        return {"requirement_text": str(source.get("text", "")), "source": source}

    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        spec = generation.data or {}
        function_points = spec.get("function_points") or []
        if not function_points:
            result.status = "fail"
            result.error = "需求规格未解析出任何功能点"
            result.summary = result.error
            return

        # 结构完备性检查（产品策略：require_samples_per_fp）
        if self.role.policy("require_samples_per_fp", True):
            no_samples = [fp.get("id") for fp in function_points if not fp.get("samples")]
            if no_samples:
                result.status = "fail"
                result.error = f"以下功能点缺少样例: {', '.join(map(str, no_samples))}"
                result.summary = result.error
                return

        self.publish(
            "requirement_spec", spec, task,
            summary=(f"功能点 {len(function_points)} 个，验收标准 {len(spec.get('acceptance_criteria', []))} 条，"
                     f"待澄清 {len(spec.get('open_questions', []))} 项"),
        )
        result.status = "pass"
        result.artifacts["requirement_spec"] = spec
        result.summary = (
            f"完成需求评估：{spec.get('title', '')} → 功能点 {len(function_points)} 个"
            f"（含异常/边界要求 {sum(1 for fp in function_points if fp.get('category') != '功能')}），"
            f"验收标准 {len(spec.get('acceptance_criteria', []))} 条"
        )
        result.events = ["requirement_ready"]
        result.broadcast_payload = {
            "summary": result.summary,
            "function_points": len(function_points),
            "open_questions": len(spec.get("open_questions", [])),
        }
        self.evidence(result, "artifact", name="requirement_spec", note="需求规格已发布至黑板")
        self.evidence(result, "source", note="输入来源 requirement_source", chars=len(context.get("requirement_text", "")))
        self.ctx.events.emit(
            "requirement_ready", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload=result.broadcast_payload,
        )
