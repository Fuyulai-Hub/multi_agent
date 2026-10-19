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

from .. import compress
from ..config import NodeConfig
from ..errors import SandboxError
from ..llm import Generation, LLMClient
from ..prompts import PromptBuilder, PromptBundle
from ..schemas import AgentContext, AgentResult, NodeTask
from ..experience import RetrievalResult
from ..skills import SkillMatch, match_skills, render_for_prompt

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
            result.metrics["skills_injected"] = len(bundle.skills)
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
    def _tool_schemas(self, task: NodeTask) -> Optional[List[Dict[str, Any]]]:
        """当前可用的 tools schema（无 ToolRunner / 无工具 / 节点不在 tools_nodes 白名单时返回 None）。

        节点隔离（docs/16 的 05 文档 §4.2）：``llm.tools_nodes`` 为空或未配置=全部节点（改造前行为）；
        配置为非空列表时只对列表内节点下发 tools，使下游节点保持 ``response_format`` 的 JSON 强制约束。
        """
        runner = getattr(self.ctx, "tools", None)
        if runner is None:
            return None
        schemas = getattr(runner, "schemas", None)
        if not schemas:
            return None
        llm_cfg = getattr(getattr(self.ctx, "config", None), "llm", None)
        if isinstance(llm_cfg, dict):
            nodes = llm_cfg.get("tools_nodes")
            if nodes is not None:
                allowed = [str(node) for node in nodes]
                if allowed and task.node_id not in allowed:
                    return None
        return list(schemas)

    def _generate_with_tools(
        self, task: NodeTask, bundle: PromptBundle, context: Dict[str, Any], result: AgentResult,
    ) -> Any:
        """生成 + 工具调用闭环：模型请求 tool_calls → ToolRunner 真实执行 → 结果回传 → 续跑。

        轮数上限 llm.tool_max_rounds（默认 2）；无工具配置时退化为单轮 generate，行为不变。
        """
        tools = self._tool_schemas(task)
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
            tool_messages = runner.execute(generation.tool_calls)
            # tool 结果统一截断（docs/16 的 02 文档）：代码默认 0=不截断（与改造前一致）；
            # 当前 system.yaml 实配 4000，截断已生效（runtime.tool_result_max_chars 可调）
            runtime = getattr(self.ctx.config, "runtime", None) or {}
            tool_max_chars = int(runtime.get("tool_result_max_chars", 0) or 0)
            if tool_max_chars > 0:
                tool_messages = compress.truncate_tool_results(tool_messages, max_chars=tool_max_chars)
            extra_messages.extend(tool_messages)
            result.metrics["tool_rounds"] = round_index
        # 轮数用尽时模型仍在请求工具（正文为空）：再发起一次**不带 tools** 的生成，强制收敛出正文。
        # 此前直接返回最后一轮（finish_reason=tool_calls、正文 0 字符），act() 会判
        # 「模型未返回可解析的 JSON」使节点失败——2026-10-17 的真实运行
        # 20261017-020255-e0cf 即此路径（2 轮工具调用用尽 → intake 节点 fail）。
        # 只在「有 tool_calls 且正文为空」这一异常分支追加一次调用，正常路径逐字节不变。
        if generation is not None and generation.tool_calls and not (generation.text or "").strip():
            self.ctx.events.emit(
                "tool_rounds_exhausted", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={
                    "rounds": max_rounds,
                    "tool_calls": [call.get("name", "") for call in generation.tool_calls],
                },
            )
            generation = self.ctx.llm.generate(
                self.task_kind_for(task), bundle, context, extra_messages=extra_messages,
            )
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
        # 感知段单产物字符预算：调高前的历史默认 900 会把 12 个功能点的需求规格截到只剩 3 个 FP 片段
        # （attempt03 实测 implement/test 全程信息残缺）；代码默认与当前 system.yaml 实配均为 10000
        # （runtime.prompt_artifact_max_chars 可再调）
        max_chars = int(self.ctx.config.runtime.get("prompt_artifact_max_chars", 10000))
        # 上下文压缩（docs/16 的 02 文档）：enabled=false 时恒等返回，行为与现状逐字节一致。
        # 压缩只改「进 Prompt 的内容」，黑板 artifact 数据不变（门禁求值走 snapshot，完全隔离）。
        compression = dict(self.ctx.config.runtime.get("context_compression") or {})
        enabled = bool(compression.get("enabled", False))
        result = compress.found_artifacts(
            found,
            enabled=enabled,
            per_item_chars=int(compression.get("per_item_chars", max_chars) or max_chars),
            total_chars=int(compression.get("total_chars", 32000) or 32000),
        )
        if enabled:
            self.ctx.events.emit(
                "context_compressed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                payload={"before_chars": result.before_chars, "after_chars": result.after_chars,
                         "compressed": result.compressed, "untouched": result.untouched},
            )
        brief = self.ctx.blackboard.brief(sorted(result.artifacts.keys()),
                                          max_chars_per_item=max_chars, data=result.artifacts)
        digest = self.ctx.bus.digest(self.role_id)
        return Perception(found=result.artifacts, missing=missing, artifacts_brief=brief, bus_digest=digest)

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

    # ---------------------------------------------------------------- 技能
    def match_skills(self, task: NodeTask, perception: Perception) -> List[SkillMatch]:
        """节点级技能匹配（未配置 `runtime.skills.enabled` 时代码默认关闭，关闭时返回空列表；
        当前 config/system.yaml 已实配 `enabled: true`，匹配真实生效）。

        匹配全确定性（规则见 `mas.skills.score_skill`）：`trigger.requires_artifacts`
        对黑板实际产物、`trigger.nodes`/`roles` 对当前节点与角色、`keywords`/`intents`
        对需求文本与节点标签。同一输入必得同一输出，故可回放、可对照。
        """
        cfg = dict(self.ctx.config.runtime.get("skills") or {})
        if not bool(cfg.get("enabled", False)):
            return []
        spec = perception.spec
        points = [str(item.get("title") or "") for item in (spec.get("function_points") or [])
                  if isinstance(item, dict)]
        context_text = " ".join(
            [task.title, task.directive, str(spec.get("title", "")), str(spec.get("summary", ""))]
            + [str(tag) for tag in (spec.get("tags") or [])] + points
        )
        intent_tags = [
            task.node_id, self.role_id, self.task_kind_for(task),
            str(spec.get("delivery_shape") or ""),
        ] + [str(tag) for tag in (spec.get("tags") or [])]
        return match_skills(
            self.ctx.config.skills_dir,
            node_id=task.node_id,
            role_id=self.role_id,
            context_text=context_text,
            intent_tags=intent_tags,
            artifacts=sorted(perception.found.keys()),
            min_score=int(cfg.get("min_score", 1) or 1),
            limit=int(cfg.get("max_items", 3) or 3),
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
        skills = self.match_skills(task, perception)
        skills_text = ""
        if skills:
            runtime_cfg = dict(self.ctx.config.runtime.get("skills") or {})
            max_chars = int(runtime_cfg.get("max_chars", 88000) or 0)
            skills_text = render_for_prompt(skills, max_chars=max_chars)
            self.ctx.events.emit(
                "skill_matched", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                payload={
                    "matched": [
                        {"id": item.id, "score": item.score, "hits": item.hits,
                         "steps": len(item.steps), "body_chars": len(item.body)}
                        for item in skills
                    ],
                    "injected_chars": len(skills_text),
                    # 0=不截断；未截断时长度必不超过预算，故此判据只在预算为正时有意义
                    "truncated": max_chars > 0 and len(skills_text) > max_chars,
                },
            )
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
            skills_text=skills_text,
            skill_matches=[
                {"id": item.id, "name": item.name, "score": item.score} for item in skills
            ],
            context={
                "perception": sorted(perception.found.keys()),
                "missing": perception.missing,
                # 附图留痕（docs/16 的 01 文档）：artifact 里只存元数据，Prompt 只加一段文字说明
                "images": (perception.found.get("requirement_source") or {}).get("images") or [],
            },
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
