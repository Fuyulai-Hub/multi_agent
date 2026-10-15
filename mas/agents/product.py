"""产品 Agent：需求评估与拆解。"""

from __future__ import annotations

from typing import Any, Dict

from ..intent import (
    DEFAULT_DELIVERY_SHAPE,
    DELIVERY_SHAPES,
    classify_delivery_shape,
    resolve_delivery_shape,
)
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
            "delivery_shape": " | ".join(DELIVERY_SHAPES) + "（交付形态：纯后端（库/CLI/服务）="
                              "backend_only；前后端一起且页面由服务端渲染=full_stack_ssr；"
                              "前后端一起且前端在浏览器里运行=full_stack_spa；无法判定填 backend_only）",
            "delivery_shape_reason": "一句话说明为什么归到该形态（形态无法判定时必须写成待澄清问题）",
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
        text = str(source.get("text", ""))
        # L1 规则预分类（docs/15 §3.2）：确定性提示，随需求原文一起进 Prompt 供 L2 参考。
        # 只产提示不产结论——最终形态仍由模型按契约判定，本层不覆盖它。
        hint = classify_delivery_shape(text)
        self.ctx.events.emit(
            "intent_classified", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={"hint": hint["hint"], "explicit": hint["explicit"], "keywords": hint["keywords"]},
        )
        # 附图（docs/16 的 01 文档）：artifact 只存相对 run_dir 路径，这里还原成绝对路径供 payload 读取
        images = [
            {
                "name": item.get("name", ""),
                "path": str(self.ctx.run_dir / str(item.get("path", ""))),
                "mime": item.get("mime", ""),
                "bytes": item.get("bytes", 0),
            }
            for item in (source.get("images") or [])
            if item.get("path")
        ]
        return {"requirement_text": text, "source": source, "intent_hint": hint, "images": images}

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

        # L3 框架校验（docs/15 §3.2）：delivery_shape 必须在发布前恒定存在且取值受控。
        # 门禁表达式会直接引用该字段，而 expr.py 的 BoolOp 不短路求值——字段缺失会让
        # 引用它的整条 criterion 判失败（历史任务全部被误伤），所以这里是硬要求不是防御性代码。
        # 兜底取 backend_only：现有已验证能力全在该形态下，不改变任何既有行为。
        raw_shape = spec.get("delivery_shape", spec.get("delivery_mode"))
        shape, source, fell_back = resolve_delivery_shape(spec)
        spec["delivery_shape"] = shape
        spec["delivery_shape_source"] = source
        if fell_back:
            self.ctx.events.emit(
                "intent_fallback", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                severity="warning",
                payload={
                    "raw": str(raw_shape if raw_shape is not None else ""),
                    "fallback": DEFAULT_DELIVERY_SHAPE,
                    "note": "需求规格未声明合法的 delivery_shape，按安全默认兜底为纯后端形态",
                },
            )

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
                     f"待澄清 {len(spec.get('open_questions', []))} 项；交付形态 {shape}"
                     f"（{source}）"),
        )
        result.status = "pass"
        result.artifacts["requirement_spec"] = spec
        result.metrics["delivery_shape"] = shape
        result.summary = (
            f"完成需求评估：{spec.get('title', '')} → 功能点 {len(function_points)} 个"
            f"（含异常/边界要求 {sum(1 for fp in function_points if fp.get('category') != '功能')}），"
            f"验收标准 {len(spec.get('acceptance_criteria', []))} 条；交付形态 {shape}"
        )
        result.events = ["requirement_ready"]
        result.broadcast_payload = {
            "summary": result.summary,
            "function_points": len(function_points),
            "open_questions": len(spec.get("open_questions", [])),
            "delivery_shape": shape,
        }
        self.evidence(result, "artifact", name="requirement_spec", note="需求规格已发布至黑板")
        self.evidence(result, "source", note="输入来源 requirement_source", chars=len(context.get("requirement_text", "")))
        self.ctx.events.emit(
            "requirement_ready", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload=result.broadcast_payload,
        )
