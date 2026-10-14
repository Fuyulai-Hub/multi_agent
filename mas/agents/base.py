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
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import NodeConfig
from ..errors import SandboxError
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
            generation = self._generate_with_tools(task, bundle, context, result)
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
                result.status, result.error = "fail", (
                    generation.error
                    or f"模型未返回可解析的 JSON（finish_reason={generation.finish_reason or 'unknown'}，"
                       f"正文 {len(generation.text)} 字符）"
                )
                # 空/不可解析正文此前只置 fail 不告警，面板上表现为"步骤失败但无原因"
                self.ctx.events.emit(
                    "node_error", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                    severity="error", payload={"error": result.error},
                )
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

    # ---------------------------------------------------------------- function calling
    def _tool_schemas(self) -> Optional[List[Dict[str, Any]]]:
        """当前可用的 tools schema（无 ToolRunner 或无工具时返回 None，走原有单轮生成）。"""
        runner = getattr(self.ctx, "tools", None)
        if runner is None:
            return None
        schemas = getattr(runner, "schemas", None)
        return list(schemas) if schemas else None

    def _generate_with_tools(
        self, task: NodeTask, bundle: PromptBundle, context: Dict[str, Any], result: AgentResult,
    ) -> Any:
        """生成 + 工具调用闭环：模型请求 tool_calls → ToolRunner 真实执行 → 结果回传 → 续跑。

        轮数上限 llm.tool_max_rounds（默认 2）；无工具配置时退化为单轮 generate，行为不变。
        """
        tools = self._tool_schemas()
        if tools is None:
            try:
                return self.ctx.llm.generate(self.task_kind_for(task), bundle, context)
            except Exception as exc:
                self._trace_llm(task, bundle, None, error=f"{type(exc).__name__}: {exc}")
                raise
        runner = self.ctx.tools
        max_rounds = int(getattr(self.ctx.llm, "tool_max_rounds", 2) or 2)
        extra_messages: List[Dict[str, Any]] = []
        generation = None
        for round_index in range(1, max_rounds + 1):
            generation = self.ctx.llm.generate(
                self.task_kind_for(task), bundle, context, tools=tools, extra_messages=extra_messages,
            )
            if generation.error or not generation.tool_calls:
                return generation          # 最终轮：由 act() 统一留痕，避免重复
            self._trace_llm(task, bundle, generation, error=generation.error)  # 中间轮同样留痕
            self.ctx.events.emit(
                "tool_calls_requested", agent=self.role_id, node=task.node_id,
                iteration=task.iteration,
                payload={"round": round_index, "calls": [
                    {"name": call.get("name", ""), "arguments": call.get("arguments", {})}
                    for call in generation.tool_calls
                ]},
            )
            # OpenAI 协议要求：tool 消息之前必须有一条携带 tool_calls 的 assistant 消息，
            # 否则严格实现的端点会因 tool_call_id 找不到归属而报 400。
            extra_messages.append(self._assistant_tool_message(generation))
            extra_messages.extend(runner.execute(generation.tool_calls))
            result.metrics["tool_rounds"] = round_index
        return generation

    @staticmethod
    def _assistant_tool_message(generation: Any) -> Dict[str, Any]:
        """还原携带 tool_calls 的 assistant 消息（arguments 归一化为 JSON 字符串）。"""
        calls = []
        for call in generation.tool_calls or []:
            arguments = call.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            calls.append({
                "id": str(call.get("id", "")),
                "type": "function",
                "function": {"name": str(call.get("name", "")), "arguments": arguments or "{}"},
            })
        message: Dict[str, Any] = {"role": "assistant", "tool_calls": calls}
        if getattr(generation, "text", ""):
            message["content"] = generation.text
        return message

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
            # 输出预算诊断：截断标记与 token 拆分（reasoning_tokens 端点不返回时为 0）
            "finish_reason": generation.finish_reason if generation else "",
            "prompt_tokens": generation.prompt_tokens if generation else 0,
            "completion_tokens": generation.completion_tokens if generation else 0,
            "reasoning_tokens": generation.reasoning_tokens if generation else 0,
            "degraded": bool(generation and generation.degraded),
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
        # 感知段单产物字符预算：默认 900 会把 12 个功能点的需求规格截到只剩 3 个 FP 片段
        # （attempt03 实测 implement/test 全程信息残缺）；由 runtime.prompt_artifact_max_chars 配置
        max_chars = int(self.ctx.config.runtime.get("prompt_artifact_max_chars", 10000))
        brief = self.ctx.blackboard.brief(sorted(found.keys()), max_chars_per_item=max_chars)
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

    # ---------------------------------------------------------------- 权限守卫
    # 角色 YAML 的 permissions 段（can_write_code / can_write_tests / can_execute /
    # can_rollback / can_block_acceptance）经此处真正生效：只有显式声明 false 才拦截，
    # 未声明的键默认放行——保证既有角色配置与流程不被误伤。
    def permit(self, action: str, *, target: str = "") -> bool:
        permissions = self.role.permissions or {}
        # 兼容旧键名：未声明 can_execute 时，can_run_tests 作为其别名生效
        if action == "can_execute" and "can_execute" not in permissions and "can_run_tests" in permissions:
            action = "can_run_tests"
        value = permissions.get(action, True)
        if value is False or value == 0 or str(value).lower() == "false":
            self.ctx.events.emit(
                "permission_denied", agent=self.role_id, severity="warning",
                payload={"action": action, "target": target},
            )
            return False
        return True

    def sandbox_write(self, relative: str, content: str) -> Path:
        """带权限守卫的沙箱写入：tests/ 目录走 can_write_tests，其余走 can_write_code。"""
        normalized = str(relative).replace("\\", "/")
        action = "can_write_tests" if normalized.startswith("tests/") else "can_write_code"
        if not self.permit(action, target=normalized):
            raise SandboxError(f"角色 {self.role_id} 无 {action} 权限（目标: {normalized}）")
        return self.ctx.sandbox.write_file(relative, content)

    def sandbox_run(self, cmd: List[str], **kwargs: Any) -> Any:
        """带权限守卫的沙箱执行（can_execute）。"""
        if not self.permit("can_execute", target=" ".join(str(part) for part in cmd)):
            raise SandboxError(f"角色 {self.role_id} 无 can_execute 权限（命令: {cmd}）")
        return self.ctx.sandbox.run(cmd, **kwargs)
