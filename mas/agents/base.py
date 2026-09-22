"""Agent 基类：感知 → 经验检索 → Prompt 组装 → 生成 → 落地（写文件/执行）→ 发布产物 → 广播。

所有角色 Agent 共享这一执行骨架，保证：
- 动手前必须感知上游成果（黑板产物 + 通讯摘要）；
- Prompt 中的经验注入全部留痕（prompts/*.md），形成可审计的自学习证据；
- 产物发布统一写黑板（版本化），并声明需要广播的事件主题（由 Leader 按配置矩阵分发）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import NodeConfig
from ..llm import Generation, LLMClient
from ..prompts import PromptBuilder, PromptBundle
from ..schemas import AgentContext, AgentResult, NodeTask
from ..experience import RetrievalResult

__all__ = ["BaseAgent", "Perception"]


@dataclass
class Perception:
    """感知结果：上游产物 + 缺失项 + 通讯摘要。"""

    found: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    artifacts_brief: str = ""
    bus_digest: str = ""

    @property
    def spec(self) -> Dict[str, Any]:
        return self.found.get("requirement_spec") or {}


class BaseAgent:
    """角色 Agent 基类。"""

    role_id: str = ""
    task_kinds: Dict[str, str] = {}       # node_id -> task_kind（default 兜底）

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.role = ctx.config.role(self.role_id)

    # ---------------------------------------------------------------- 骨架
    def act(self, task: NodeTask) -> AgentResult:
        result = AgentResult()
        try:
            perception = self.perceive(task)
            experience = self.retrieve_experience(task, perception)
            bundle = self.build_prompt(task, perception, experience)
            result.prompt_path = bundle.path
            result.metrics["experience_injected"] = len(bundle.experiences)
            result.metrics["experience_tiers"] = bundle.tier_counts
            result.consumed_experience = [item["id"] for item in bundle.experiences]
            context = self.build_context(task, perception)
            try:
                generation = self.ctx.llm.generate(self.task_kind_for(task), bundle, context)
            except Exception as exc:
                self._trace_llm(task, bundle, None, error=f"{type(exc).__name__}: {exc}")
                raise
            self._trace_llm(task, bundle, generation, error=generation.error)
            result.metrics["generation"] = generation.to_dict()
            if generation.error:
                result.status, result.error = "fail", generation.error
                self.ctx.events.emit(
                    "node_error", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                    severity="error", payload={"error": generation.error},
                )
                return result
            if generation.data is None:
                result.status, result.error = "fail", "模型未返回可解析的 JSON"
                return result
            self.apply(task, generation, perception, context, result)
        except Exception as exc:  # noqa: BLE001 - 任何 Agent 异常都转为节点失败
            result.status = "fail"
            result.error = f"{type(exc).__name__}: {exc}"
            self.ctx.events.emit(
                "node_error", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                severity="error", payload={"error": result.error},
            )
        return result

    # ---------------------------------------------------------------- LLM 调用留痕
    TRACE_LIMIT = 12000  # 单条思考/输出最长落盘字符数

    def _trace_llm(
        self,
        task: NodeTask,
        bundle: PromptBundle,
        generation: Optional[Generation],
        *,
        error: str = "",
    ) -> None:
        """把一次 LLM 调用（含思考链与原始输出）追加到 run_dir/llm_traces.jsonl。"""
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "agent": self.role_id,
            "node": task.node_id,
            "iteration": task.iteration,
            "task_kind": self.task_kind_for(task),
            "prompt_path": str(bundle.path) if bundle.path else None,
            "provider": (generation.provider if generation else "") or getattr(self.ctx.llm, "provider", ""),
            "model": (generation.model if generation else "") or getattr(self.ctx.llm, "model", ""),
            "latency_ms": generation.latency_ms if generation else 0,
            "tokens": generation.tokens if generation else 0,
            "reasoning": (generation.reasoning if generation else "")[: self.TRACE_LIMIT],
            "output": (generation.text if generation else "")[: self.TRACE_LIMIT],
            "error": error,
        }
        try:
            with (self.ctx.run_dir / "llm_traces.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 留痕失败不影响主流程

    # ---------------------------------------------------------------- 钩子
    def task_kind_for(self, task: NodeTask) -> str:
        if task.node_id in self.task_kinds:
            return self.task_kinds[task.node_id]
        return self.task_kinds.get("default", self.role_id)

    def contract_for(self, task: NodeTask) -> Dict[str, Any]:
        return self.contracts.get(task.node_id, self.contracts.get("default", {}))

    contracts: Dict[str, Any] = {}

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:  # pragma: no cover - 抽象
        return {"spec": perception.spec}

    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ---------------------------------------------------------------- 感知
    def perceive(self, task: NodeTask) -> Perception:
        names = self.ctx.config.collaboration.perception_of(self.role_id) or list(task.inputs)
        found, missing = self.ctx.blackboard.require(names)
        brief = self.ctx.blackboard.brief(sorted(found.keys()))
        digest = self.ctx.bus.digest(self.role_id)
        return Perception(found=found, missing=missing, artifacts_brief=brief, bus_digest=digest)

    # ---------------------------------------------------------------- 经验
    def retrieve_experience(self, task: NodeTask, perception: Perception) -> RetrievalResult:
        spec = perception.spec
        tags = (
            list(self.role.learning_tags)
            + list(spec.get("tags", []))
            + [task.node_id, str(spec.get("id", ""))]
        )
        task_text = " ".join(
            [task.title, task.directive, str(spec.get("title", ""))]
            + [str(item) for item in task.inputs]
        )
        return self.ctx.experience.retrieve(
            role=self.role_id,
            tags=tags,
            task_text=task_text,
            tier_keys=self.role.learning_tiers,
            max_items=self.role.learning_max_items,
            current_run=self.ctx.run_id,
        )

    # ---------------------------------------------------------------- Prompt
    def build_prompt(
        self, task: NodeTask, perception: Perception, experience: RetrievalResult
    ) -> PromptBundle:
        role_cfg = self.role
        node = NodeConfig(id=task.node_id, title=task.title, role=task.role, kind=task.kind)
        directive = task.directive
        if task.rollback_reason:
            directive = (directive + "；" if directive else "") + f"回退原因：{task.rollback_reason}"
        return self.ctx.prompts.build(
            role=role_cfg,
            node=node,
            task_kind=self.task_kind_for(task),
            iteration=task.iteration,
            directive=directive,
            artifacts_brief=perception.artifacts_brief,
            bus_digest=perception.bus_digest,
            perception=list(perception.found.keys()) or list(task.inputs),
            missing=perception.missing,
            experience=experience,
            contract=self.contract_for(task),
            context={"perception": sorted(perception.found.keys()), "missing": perception.missing},
        )

    # ---------------------------------------------------------------- 便捷
    def publish(self, name: str, data: Any, task: NodeTask, *, summary: str = ""):
        return self.ctx.blackboard.publish(
            name, data,
            producer=self.role_id, node=task.node_id, iteration=task.iteration, summary=summary,
        )

    def broadcast(self, topic: str, payload: Dict[str, Any], task: Optional[NodeTask] = None) -> None:
        targets = self.ctx.config.collaboration.broadcast_targets(topic)
        if not targets:
            return
        self.ctx.bus.broadcast(
            self.role_id, topic, payload, targets,
            node=task.node_id if task else None,
            iteration=task.iteration if task else None,
        )

    def log(self, message: str) -> None:
        self.ctx.log("info", f"[{self.role.name}] {message}")

    def evidence(self, result: AgentResult, kind: str, **kwargs: Any) -> None:
        result.evidence.append({"kind": kind, **kwargs})
