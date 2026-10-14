"""Leader：统一调度器。

职责：
- 依据 flow.yaml 驱动状态机（任务节点执行 / 门禁节点表达式判定）；
- 处理 pass/fail 分支、回退（含次数上限与升级策略）；
- 中介 Agent 间协商（请求-应答）与关键事件广播（按 collaboration.yaml 矩阵）；
- 每步之后做 L1 即时经验提炼，运行结束做总结提炼 + 晋级 + 指标记录（自学习闭环）；
- 产出 run_summary.json / timeline.md，并返回结构化 RunSummary。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agents import create_agent
from .blackboard import Blackboard
from .bus import MessageBus
from .config import SystemConfig, load_requirement
from .errors import ConfigError
from .events import EventLog
from .experience import Experience, ExperienceLibrary
from .expr import evaluate
from .llm import LLMClient
from .prompts import PromptBuilder
from .sandbox import Sandbox
from .schemas import AgentContext, AgentResult, NodeTask
from .skills import load_run_facts

__all__ = ["Leader", "RunSummary"]


def _permission_allows(agent: Any, action: str) -> bool:
    """读取角色 Agent 的 permissions 声明：只有显式 false/0/'false' 才视为无权限（宽松缺省）。"""
    role = getattr(agent, "role", None)
    permissions = getattr(role, "permissions", None) or {}
    value = permissions.get(action, True)
    return not (value is False or value == 0 or str(value).lower() == "false")


@dataclass
class RunSummary:
    run_id: str
    run_dir: str
    requirement_title: str
    verdict: str                     # pass | fail
    steps: int
    rollbacks: int
    review_rejections: int
    bugs_total: int
    bugs_fixed: int
    report_path: str = ""
    timeline_path: str = ""
    iterations: Dict[str, int] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    forced_fail: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Leader:
    """流水线调度器（全流程无人工干预）。"""

    def __init__(self, config: SystemConfig) -> None:
        self.config = config

    # ================================================================= 运行
    def run(
        self,
        requirement: str,
        *,
        as_text: bool = False,
        run_id: Optional[str] = None,
    ) -> RunSummary:
        started = datetime.now().astimezone()
        stamp_fmt = str(self.config.runtime.get("run_id_format", "%Y%m%d-%H%M%S"))
        run_id = run_id or f"{started.strftime(stamp_fmt)}-{secrets.token_hex(2)}"
        run_dir = self.config.runs_dir / run_id
        for sub in ("input", "code", "reports", "evidence"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)

        verbose = bool(self.config.runtime.get("verbose", True))

        events = EventLog(run_dir / "events.jsonl", run_id, echo=verbose)
        bus = MessageBus(run_dir / "messages.jsonl", events, run_id)
        blackboard = Blackboard(run_dir / "artifacts", events, run_id)
        sandbox = Sandbox(run_dir / "code", self.config, events=events)
        experience = ExperienceLibrary(self.config.workspace_dir, self.config.learning, events, run_id)
        prompts = PromptBuilder(self.config, run_dir, enabled=bool(self.config.runtime.get("save_prompts", True)))
        llm = LLMClient(self.config)

        ctx = AgentContext(
            config=self.config, run_id=run_id, run_dir=run_dir, events=events, blackboard=blackboard,
            bus=bus, llm=llm, sandbox=sandbox, prompts=prompts, experience=experience,
            log=self._logger(events), run_state={
                "run_id": run_id, "injected_experience_ids": [], "injected_tiers": {},
                "injected_total": 0, "rollbacks": 0, "review_rejections": 0, "node_executions": 0,
                "last_extract_seq": 0, "negotiations": 0,
            },
        )
        ctx.consult = self._consult_factory()
        agents = {role_id: create_agent(role_id, ctx) for role_id in self.config.roles}
        self._bind_runtime(ctx, bus, events, agents)

        # function calling：默认不装配（tools=None，行为与原先完全一致）；
        # llm.tools_enabled=true 时从 MCP 服务拉取工具清单，供 Agent 生成时调用。
        tool_runner = self._build_tool_runner()
        ctx.tools = tool_runner

        # ---------------------------------------------------------------- 输入
        try:
            req_title, req_text = load_requirement(
                requirement, as_text=as_text, project_root=self.config.project_root,
                encoding=str(self.config.system.get("requirement_encoding", "utf-8")),
            )
        except ConfigError as exc:
            events.emit("run_failed", severity="error", payload={"error": str(exc)})
            return RunSummary(run_id=run_id, run_dir=str(run_dir), requirement_title="",
                              verdict="fail", steps=0, rollbacks=0, review_rejections=0,
                              bugs_total=0, bugs_fixed=0, error=str(exc))
        (run_dir / "input" / "requirement.md").write_text(req_text, encoding="utf-8")
        blackboard.publish(
            "requirement_source",
            {"title": req_title, "text": req_text, "source": "text" if as_text else "file",
             "chars": len(req_text), "run_id": run_id},
            producer="leader", node="intake", iteration=0, summary=f"需求输入（{len(req_text)} 字符）",
        )
        events.emit(
            "run_started", payload={
                "run_id": run_id, "requirement": req_title, "input_mode": "text" if as_text else "file",
                "profile": self.config.profile, "flow_entry": self.config.flow.entry,
                "config_name": self.config.name, "llm_provider": self.config.llm.get("provider"),
            },
        )
        print(f"\n[RUN] {run_id}｜{self.config.name}｜需求：{req_title}")

        # ---------------------------------------------------------------- 状态机
        try:
            flow = self.config.flow
            collaboration = self.config.collaboration
            iterations: Dict[str, int] = {}
            rollback_counts: Dict[str, int] = {}
            pending_reason = ""
            forced_fail = False
            node_id: Optional[str] = flow.entry
            steps = 0
            max_steps = min(int(flow.max_steps), int(self.config.runtime.get("max_steps", flow.max_steps)))
            max_total_rollbacks = int(collaboration.guards.get("max_total_rollbacks", 8))

            while node_id and node_id not in flow.terminal and steps < max_steps:
                steps += 1
                node = flow.node(node_id)
                iteration = iterations.get(node_id, 0) + 1
                iterations[node_id] = iteration
                ctx.run_state["node_executions"] = steps
                events.emit(
                    "node_start", agent=node.role, node=node_id, iteration=iteration,
                    payload={"title": node.title, "kind": node.kind},
                )
                task = NodeTask.from_node(
                    node, iteration=iteration,
                    directive=self._directive_for(ctx, node_id, iteration),
                    rollback_reason=pending_reason if iteration > 1 else "",
                )
                pending_reason = ""

                if node.kind == "gate":
                    outcome = self._run_gate(ctx, task, agents)
                    status = outcome["status"]
                    result = AgentResult(status=status, summary=outcome.get("summary", ""))
                else:
                    if not node.role or node.role not in agents:
                        raise ConfigError(f"节点 {node_id} 的角色 {node.role} 未注册")
                    result, status = self._act_with_retry(ctx, task, agents, node)
                    self._track_experience(ctx, result)

                # 广播（按协作矩阵）
                for topic in result.events or []:
                    self._broadcast(ctx, node.role or "leader", topic,
                                     result.broadcast_payload or {"summary": result.summary})
                events.emit(
                    "node_end", agent=node.role, node=node_id, iteration=iteration,
                    payload={"status": status, "summary": result.summary[:300]},
                    severity="info" if status == "pass" else "warning",
                )
                if node_id == "review" and status == "fail":
                    ctx.run_state["review_rejections"] += 1

                # L1 即时经验提炼（增量：只处理本步新增事件）
                self._extract_inline(ctx)

                # 分支与回退
                branch = collaboration.branch_of(status)
                target = node.target(branch)
                if target is None:
                    target = "halt"
                if branch == "fail" and self._is_rollback(node_id, target, iterations, rollback_counts):
                    # 权限落地：节点角色显式声明 can_rollback=false 时，拒绝回退并终止流程
                    if not self._rollback_permitted(node_id, node, target, agents, events, iteration):
                        target = "halt"
                        pending_reason = f"角色 {node.role} 无回退权限（can_rollback=false），流程终止"
                    else:
                        policy = collaboration.rollback_policy(node_id, node)
                        rollback_counts[node_id] = rollback_counts.get(node_id, 0) + 1
                        ctx.run_state["rollbacks"] = sum(rollback_counts.values())
                        attempt = rollback_counts[node_id]
                        reason = policy.reason
                        events.emit(
                            "rollback", agent="leader", node=node_id, iteration=iteration,
                            severity="warning",
                            payload={
                                "from": node_id, "to": target, "reason": reason,
                                "attempt": attempt, "max_attempts": policy.max_attempts,
                                "summary": result.summary[:200] or result.error[:200],
                            },
                        )
                        self._broadcast(ctx, "leader", "rollback",
                                        {"from": node_id, "to": target, "reason": reason, "attempt": attempt})
                        pending_reason = f"{reason}（第 {attempt} 次回退）"
                        if attempt > policy.max_attempts or ctx.run_state["rollbacks"] > max_total_rollbacks:
                            forced_fail = True
                            events.emit(
                                "escalation", agent="leader", node=node_id, severity="error",
                                payload={
                                    "reason": f"回退次数耗尽（{node_id} 已回退 {attempt - 1} 次）",
                                    "strategy": collaboration.escalation.get("on_exhausted", "fail_run_and_report"),
                                    "notify": collaboration.escalation.get("notify", []),
                                },
                            )
                            if str(collaboration.escalation.get("on_exhausted", "fail_run_and_report")) == "fail_run_and_report":
                                blackboard.publish(
                                    "acceptance_gate",
                                    {"node": "acceptance", "passed": False, "forced": True,
                                     "criteria": [], "failed_criteria": [{"id": "回退次数耗尽", "reason": pending_reason}],
                                     "signoff": {"verdict": "reject", "opinion": "回退次数耗尽，验收不通过"},
                                     "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                    producer="leader", node="acceptance", iteration=iteration,
                                    summary="回退耗尽，判定不通过",
                                )
                                target = "report"
                            else:
                                target = "halt"
                node_id = target

        finally:
            if tool_runner is not None:
                tool_runner.close()

        if node_id is None or steps >= max_steps and node_id not in flow.terminal:
            forced_fail = True
            events.emit("escalation", agent="leader", severity="error",
                        payload={"reason": f"达到最大步数 {max_steps}，强制终止"})
            node_id = "halt"

        # ---------------------------------------------------------------- 收尾
        duration_s = round((datetime.now().astimezone() - started).total_seconds(), 2)
        gate = blackboard.get("acceptance_gate") or {}
        verdict = "pass" if gate.get("passed") and not forced_fail else "fail"
        bugs = blackboard.get("bug_reports") or {}
        report = blackboard.get("acceptance_report") or {}

        # 运行级经验提炼（成功路径/效率） + 晋级 + 指标
        learned_end = self._extract_run_end(ctx, verdict, steps, duration_s)
        promoted = experience.promote()
        metrics = {
            "run_id": run_id,
            "ts": started.isoformat(timespec="seconds"),
            "requirement_title": req_title,
            "profile": self.config.profile,
            "acceptance": verdict == "pass",
            "node_executions": steps,
            "rollbacks": ctx.run_state["rollbacks"],
            "review_rejections": ctx.run_state["review_rejections"],
            "bugs_total": int(bugs.get("total", 0)),
            "bugs_fixed": int(bugs.get("fixed", 0)),
            "iterations_to_accept": steps,
            "duration_s": duration_s,
            "injected_experiences": ctx.run_state["injected_total"],
            "experience_learned": learned_end,
            "promoted": len(promoted),
        }
        experience.record_metrics(metrics)
        ctx.run_state["metrics"] = metrics

        summary = RunSummary(
            run_id=run_id,
            run_dir=str(run_dir),
            requirement_title=req_title,
            verdict=verdict,
            steps=steps,
            rollbacks=ctx.run_state["rollbacks"],
            review_rejections=ctx.run_state["review_rejections"],
            bugs_total=int(bugs.get("total", 0)),
            bugs_fixed=int(bugs.get("fixed", 0)),
            report_path=str(report.get("path", "")),
            iterations=iterations,
            metrics=metrics,
            forced_fail=forced_fail,
        )
        (run_dir / "run_summary.json").write_text(
            json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        timeline_md = ["# 运行时间线", "", f"- 运行：{run_id}", f"- 需求：{req_title}",
                       f"- 结论：{'通过' if verdict == 'pass' else '未通过'}", ""]
        timeline_md += events.timeline(limit=200)
        timeline_path = run_dir / "timeline.md"
        timeline_path.write_text("\n".join(timeline_md), encoding="utf-8")
        summary.timeline_path = str(timeline_path)

        events.emit(
            "run_finished", payload={
                "verdict": verdict, "steps": steps, "duration_s": duration_s,
                "bugs_total": summary.bugs_total, "bugs_fixed": summary.bugs_fixed,
                "rollbacks": summary.rollbacks, "report": report.get("path", ""),
                "forced_fail": forced_fail,
            },
        )
        print(
            f"[DONE] 运行结束 {run_id}｜验收：{'通过' if verdict == 'pass' else '未通过'}"
            f"｜步数 {steps}｜回退 {summary.rollbacks}｜缺陷 {summary.bugs_total}(闭环 {summary.bugs_fixed})"
            f"｜耗时 {duration_s}s\n  报告：{report.get('path', '-')}"
        )
        return summary

    # ================================================================= 流程沉淀
    def distill_skill(self, run_id: str) -> Dict[str, Any]:
        """把一次「验收通过」的运行沉淀为可复用技能。

        这是 off-flow 动作：`Leader.run()` 不会调用它，只能由用户在后续命令中显式要求。
        提炼规则见 `mas.skills.load_run_facts`——只保留最终通过路径，剔除试错与环境绑定细节。
        """
        run_dir = self.config.runs_dir / run_id
        if not (run_dir / "run_summary.json").exists():
            raise ConfigError(f"运行不存在: {run_id}（可用 `python -m mas report list` 查看）")
        facts = load_run_facts(run_dir, self.config.workspace_dir)
        if facts.get("verdict") != "pass":
            raise ConfigError(
                f"运行 {run_id} 未通过验收（verdict={facts.get('verdict') or '未知'}），只沉淀成功流程"
            )

        events = EventLog(run_dir / "events.jsonl", run_id)
        events.load()
        bus = MessageBus(run_dir / "messages.jsonl", events, run_id)
        blackboard = Blackboard(run_dir / "artifacts", events, run_id)
        sandbox = Sandbox(run_dir / "code", self.config, events=events)
        experience = ExperienceLibrary(self.config.workspace_dir, self.config.learning, events, run_id)
        # 沉淀是 off-flow 动作，不再往历史运行目录追加 Prompt 留痕
        prompts = PromptBuilder(self.config, run_dir, enabled=False)
        llm = LLMClient(self.config)

        ctx = AgentContext(
            config=self.config, run_id=run_id, run_dir=run_dir, events=events, blackboard=blackboard,
            bus=bus, llm=llm, sandbox=sandbox, prompts=prompts, experience=experience,
            log=self._logger(events), run_state={"distill_facts": facts},
        )
        ctx.consult = self._consult_factory()
        agents = {"reporter": create_agent("reporter", ctx)}
        self._bind_runtime(ctx, bus, events, agents)

        task = NodeTask(
            node_id="skill_distill", title="把本次成功流程沉淀为可复用技能", role="reporter",
            kind="task", iteration=1, inputs=[], outputs=["skill"],
            directive="剔除临时试错与环境绑定细节，只保留可复用的输入参数、执行步骤、异常处理与结果校验点",
        )
        result = agents["reporter"].act(task)
        return {
            "run_id": run_id,
            "status": result.status,
            "summary": result.summary,
            "error": result.error,
            "skill": result.artifacts.get("skill") or {},
        }

    # ================================================================= 门禁
    def _run_gate(self, ctx: AgentContext, task: NodeTask, agents: Dict[str, Any]) -> Dict[str, Any]:
        node = self.config.flow.node(task.node_id)
        snapshot = ctx.blackboard.snapshot()
        criteria_results: List[Dict[str, Any]] = []
        for criterion in node.criteria:
            outcome = evaluate(criterion.expr, snapshot)
            criteria_results.append({
                "id": criterion.id,
                "expr": criterion.expr,
                "passed": bool(outcome.ok and outcome.value),
                "value": outcome.value if outcome.ok else None,
                "reason": "" if outcome.ok else outcome.error,
            })
        all_pass = all(item["passed"] for item in criteria_results)

        signoff: Dict[str, Any] = {}
        if node.role and node.role in agents:
            signoff_result = agents[node.role].act(task)
            self._track_experience(ctx, signoff_result)
            signoff = signoff_result.artifacts.get("acceptance_opinion") or {}
            # 权限落地：未声明 can_block_acceptance 的角色，其否决（reject）无效
            if str(signoff.get("verdict", "")) == "reject" and not _permission_allows(
                agents[node.role], "can_block_acceptance"
            ):
                ctx.events.emit(
                    "permission_denied", agent=node.role, node=node.id, iteration=task.iteration,
                    severity="warning",
                    payload={
                        "action": "can_block_acceptance",
                        "reason": f"角色 {node.role} 无验收否决权，其 reject 签署不生效",
                    },
                )
                signoff = {}

        passed = all_pass and str(signoff.get("verdict", "pass")) != "reject"
        failed = [item for item in criteria_results if not item["passed"]]
        gate = {
            "node": node.id,
            "passed": passed,
            "criteria": criteria_results,
            "failed_criteria": failed,
            "signoff": {"verdict": signoff.get("verdict", ""), "opinion": signoff.get("opinion", "")},
            "iteration": task.iteration,
            "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        ctx.blackboard.publish(
            "acceptance_gate", gate, producer="leader", node=node.id, iteration=task.iteration,
            summary=("全部通过" if passed else "未通过：" + "、".join(str(item["id"]) for item in failed)),
        )
        ctx.events.emit(
            "gate_evaluated", agent="leader", node=node.id, iteration=task.iteration,
            payload={"passed": passed, "failed_criteria": failed, "signoff": gate["signoff"]},
        )
        topic = "acceptance_passed" if passed else "acceptance_rejected"
        self._broadcast(ctx, "leader", topic, {
            "passed": passed,
            "summary": ("验收门禁全部通过" if passed else "验收未通过：" + "；".join(str(i["id"]) for i in failed)),
        })
        return {
            "status": "pass" if passed else "fail",
            "summary": gate["summary"] if "summary" in gate else ("验收通过" if passed else "验收未通过"),
        }

    # ================================================================= 协作
    def _consult_factory(self):
        """构造 Agent 间协商函数（请求-应答，Leader 中介并留痕）。"""

        def consult(*, requester: str, target_role: str, question: str, fp_id: str = "",
                    node: Optional[str] = None) -> Dict[str, Any]:
            negotiation = self.config.collaboration.negotiation
            request = self._current_bus.send(
                requester, "negotiation_request", {"question": question, "fp_id": fp_id},
                recipients=[target_role], kind="request", node=node,
            )
            agent = self._current_agents.get(target_role)
            answer: Dict[str, Any] = {}
            if agent is not None and hasattr(agent, "consult"):
                answer = agent.consult(question, fp_id) or {}
            self._current_bus.send(
                target_role, "negotiation_response",
                {"question": question, "answer": answer.get("answer", ""), "fp_id": fp_id},
                recipients=[requester], kind="response", correlation_id=request.id, node=node,
            )
            self._current_events.emit(
                "negotiation_closed", agent=target_role, node=node,
                payload={"topic": "ambiguity", "question": question,
                         "answer": answer.get("answer", ""), "fp_id": fp_id, "requester": requester},
            )
            self._current_ctx.run_state["negotiations"] = \
                self._current_ctx.run_state.get("negotiations", 0) + 1
            return answer

        return consult

    # Leader.run 期间绑定的运行时引用（供 consult 闭包使用）
    _current_bus: Optional[MessageBus] = None
    _current_events: Optional[EventLog] = None
    _current_agents: Dict[str, Any] = {}
    _current_ctx: Optional[AgentContext] = None

    def _bind_runtime(self, ctx: AgentContext, bus: MessageBus, events: EventLog,
                      agents: Dict[str, Any]) -> None:
        self._current_ctx = ctx
        self._current_bus = bus
        self._current_events = events
        self._current_agents = agents

    def _build_tool_runner(self):
        """按 llm.tools_enabled 装配 function calling 工具执行器（默认关闭，返回 None）。

        开启后从 config/mcp.yaml 的 enabled 服务拉取 tools/list 转 OpenAI schema；
        单个 MCP 服务不可用只跳过该服务，不阻塞运行。
        """
        if not bool(self.config.llm.get("tools_enabled", False)):
            return None
        from .toolrunner import ToolRunner

        runner = ToolRunner(self.config)
        try:
            schemas = runner.register_from_mcp()
            static = list(self.config.llm.get("tools") or [])
            if static:
                runner.register_static(static)
            if not schemas and not static:
                self._current_events.emit(
                    "tool_runner_empty", agent="leader", severity="warning",
                    payload={"reason": "llm.tools_enabled=true 但未拉到任何工具（检查 config/mcp.yaml）"},
                )
                runner.close()
                return None      # 无工具可用：降级为无工具运行，避免空 tools 下发干扰端点
            self._current_events.emit(
                "tool_runner_ready", agent="leader",
                payload={"tools": len(runner.schemas)},
            )
        except Exception as exc:  # noqa: BLE001 - 工具装配失败降级为无工具运行
            self._current_events.emit(
                "tool_runner_failed", agent="leader", severity="warning",
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            runner.close()
            return None
        return runner

    def _broadcast(self, ctx: AgentContext, sender: str, topic: str, payload: Dict[str, Any]) -> None:
        targets = self.config.collaboration.broadcast_targets(topic)
        if not targets:
            return
        ctx.bus.broadcast(sender, topic, payload, targets)

    # ---------------------------------------------------------------- 重试与权限
    def _act_with_retry(
        self, ctx: AgentContext, task: NodeTask, agents: Dict[str, Any], node: Any
    ) -> Tuple[AgentResult, str]:
        """节点级原地重试：flow.yaml 的 retry.max_attempts > 1 时，失败后原地重跑该节点。

        与 collaboration.rollback 的回退是两套机制：retry 不改变流程位置（原地重试），
        rollback 退回到上游节点。默认 max_attempts=1（不重试），行为与原来一致。
        """
        result = agents[node.role].act(task)
        max_attempts = max(1, int(getattr(node.retry, "max_attempts", 1) or 1))
        attempts = 1
        while result.status != "pass" and attempts < max_attempts:
            attempts += 1
            ctx.events.emit(
                "node_retry", agent=node.role, node=node.id, iteration=task.iteration,
                severity="warning",
                payload={
                    "attempt": attempts, "max_attempts": max_attempts,
                    "reason": (result.error or result.summary)[:200],
                },
            )
            result = agents[node.role].act(task)
        return result, result.status

    def _rollback_permitted(
        self, node_id: str, node: Any, target: str, agents: Dict[str, Any],
        events: EventLog, iteration: int,
    ) -> bool:
        """回退权限：节点角色显式声明 can_rollback=false 时拒绝回退（缺省允许，不破坏既有流程）。"""
        agent = agents.get(node.role) if node.role else None
        if agent is None or _permission_allows(agent, "can_rollback"):
            return True
        events.emit(
            "permission_denied", agent=node.role, node=node_id, iteration=iteration,
            severity="error",
            payload={
                "action": "can_rollback", "from": node_id, "to": target,
                "reason": f"角色 {node.role} 未声明回退权限，拒绝回退并终止流程",
            },
        )
        return False

    def _track_experience(self, ctx: AgentContext, result: AgentResult) -> None:
        state = ctx.run_state
        known = set(state.get("injected_experience_ids", []))
        for experience_id in result.consumed_experience:
            if experience_id not in known:
                known.add(experience_id)
        state["injected_experience_ids"] = sorted(known)
        tiers = result.metrics.get("experience_tiers") or {}
        for tier, count in tiers.items():
            state["injected_tiers"][tier] = state["injected_tiers"].get(tier, 0) + int(count)
        state["injected_total"] += int(result.metrics.get("experience_injected", 0) or 0)

    # ================================================================= 经验闭环
    def _extract_inline(self, ctx: AgentContext) -> None:
        """每步之后的 L1 增量提炼：只处理上次提炼之后的新事件。"""
        if not self.config.learning.enabled:
            return
        since = int(ctx.run_state.get("last_extract_seq", 0))
        new_events = [event for event in ctx.events.events if event.seq > since]
        if not new_events:
            return
        ctx.run_state["last_extract_seq"] = new_events[-1].seq
        facts = self._facts_from_events(new_events, ctx)
        entries = ctx.experience.extract_from_events(facts)
        if entries:
            ctx.experience.add(entries, source_run=ctx.run_id)

    def _extract_run_end(self, ctx: AgentContext, verdict: str, steps: int, duration_s: float) -> int:
        if not self.config.learning.enabled:
            return 0
        since = int(ctx.run_state.get("last_extract_seq", 0))
        new_events = [event for event in ctx.events.events if event.seq > since]
        ctx.run_state["last_extract_seq"] = ctx.events.events[-1].seq if ctx.events.events else since
        facts = self._facts_from_events(new_events, ctx)
        facts["metrics"] = {
            "acceptance": verdict == "pass",
            "node_executions": steps,
            "duration_s": duration_s,
            "bugs_total": int((ctx.blackboard.get("bug_reports") or {}).get("total", 0)),
        }
        if steps > 12:
            facts.setdefault("efficiency_flags", []).append({
                "lesson": f"本次运行步数偏高（{steps} 步），缺陷与驳回在流程后段才暴露",
                "action": "在实现阶段前置执行静态自检与边界用例，尽早暴露问题",
                "action_key": "reduce_iterations",
            })
        entries = ctx.experience.extract_from_events(facts)
        if entries:
            ctx.experience.add(entries, source_run=ctx.run_id)
        return len(entries)

    def _facts_from_events(self, events: List[Any], ctx: AgentContext) -> Dict[str, Any]:
        spec = ctx.blackboard.get("requirement_spec") or {}
        review_blocking: Dict[str, Dict[str, Any]] = {}
        bug_patterns: Dict[Tuple[str, str], Dict[str, Any]] = {}
        acceptance_failed: List[Dict[str, Any]] = []
        negotiations: List[Dict[str, Any]] = []

        for event in events:
            payload = event.payload or {}
            if event.type == "review_completed" and payload.get("verdict") == "reject":
                for finding in payload.get("findings", []):
                    if str(finding.get("severity")) not in ("critical", "major"):
                        continue
                    rule = str(finding.get("rule", ""))
                    record = review_blocking.setdefault(rule, {
                        "rule": rule, "count": 0, "sample_file": finding.get("file", "-"), "refs": [],
                    })
                    record["count"] += 1
            elif event.type == "bug_reported":
                key = (str(payload.get("fp_id", "")), str(payload.get("category", "functional")))
                record = bug_patterns.setdefault(key, {
                    "fp_id": payload.get("fp_id", ""), "category": payload.get("category", "functional"),
                    "symptom": str(payload.get("symptom", ""))[:120], "fix_strategy": "",
                    "count": 0, "refs": [],
                })
                record["count"] += 1
            elif event.type == "bug_fixed":
                key = (str(payload.get("fp_id", "")), "boundary")
                for candidate_key, record in bug_patterns.items():
                    if candidate_key[0] == payload.get("fp_id"):
                        record["fix_strategy"] = str(payload.get("strategy", ""))
            elif event.type == "gate_evaluated" and not payload.get("passed"):
                for item in payload.get("failed_criteria", []):
                    acceptance_failed.append({
                        "id": item.get("id", ""), "expr": item.get("expr", ""),
                        "reason": item.get("reason", ""), "refs": [],
                    })
            elif event.type == "negotiation_closed":
                negotiations.append({
                    "topic": payload.get("topic", ""),
                    "question": payload.get("question", ""),
                    "answer": payload.get("answer", ""),
                    "fp_id": payload.get("fp_id", ""),
                    "refs": [event.id],
                })

        return {
            "run_id": ctx.run_id,
            "tags": list(spec.get("tags", [])),
            "review_blocking": list(review_blocking.values()),
            "bug_patterns": list(bug_patterns.values()),
            "acceptance_failed": acceptance_failed,
            "negotiations": negotiations,
            "metrics": ctx.run_state.get("metrics") or {},
        }

    # ================================================================= 指令与回退
    def _directive_for(self, ctx: AgentContext, node_id: str, iteration: int) -> str:
        base = {
            "intake": "解析需求输入，拆解功能点与验收标准，显式标注待澄清问题",
            "solution": "完成技术选型、方案评估与实现计划，覆盖全部功能点并闭环歧义",
            "implement": "依据需求规格与技术方案完成编码实现（含异常与边界处理），并通过导入自检",
            "review": "对交付代码执行静态审查与需求一致性检查，阻塞问题必须拦截",
            "solution_review": "对技术方案执行评审：选型依据、功能点覆盖、歧义闭环",
            "test": "依据需求契约独立生成功能/异常/边界用例并真实执行；失败用例提缺陷单；歧义须协商闭环",
            "optimize": "修复全部未闭环缺陷并输出回归自测报告；如来自验收驳回，需同时优化对应门禁项",
            "acceptance": "依据门禁证据签署验收意见（用例、覆盖率、缺陷闭环、审查结论）",
            "report": "汇总全流程证据，生成可追溯的验收报告",
        }.get(node_id, "按角色职责完成本节点任务")
        if node_id == "implement" and iteration > 1:
            review = ctx.blackboard.get("review_report") or {}
            blocking = review.get("blocking_items") or []
            if blocking:
                rules = sorted({str(item.get("rule")) for item in blocking})
                base += f"；上一轮审查阻塞问题：{('、'.join(rules))}"
        if node_id == "optimize":
            bugs = ctx.blackboard.get("bug_reports") or {}
            open_items = [item for item in bugs.get("items", []) if item.get("status") == "open"]
            if open_items:
                base += "；需修复缺陷：" + "、".join(
                    f"{item.get('id')}({item.get('op') or item.get('fp_id')}/{item.get('category')})"
                    for item in open_items
                )
            gate = ctx.blackboard.get("acceptance_gate") or {}
            failed = gate.get("failed_criteria") or []
            if failed:
                base += "；验收未通过项：" + "、".join(str(item.get("id")) for item in failed)
        return base

    def _is_rollback(self, source: str, target: str, iterations: Dict[str, int],
                     rollback_counts: Dict[str, int]) -> bool:
        """是否构成回退（受回退策略与权限约束）。

        只有 collaboration.rollback 显式声明的边才是回退。流程配置里的普通转发边
        （optimize→test 的修复闭环、implement→review 的失败中转）重新进入已执行
        节点属于迭代而非回退：attempt03 实测 optimize 修复后再测试被误判为回退，
        而 developer 角色未声明回退权限 → permission_denied → 整条闭环被终止。
        回退次数仍由三条显式回退边各自的 max_attempts 与全局上限约束，不会失控。
        """
        if target in self.config.flow.terminal:
            return False
        return source in self.config.collaboration.rollback

    @staticmethod
    def _logger(events: EventLog):
        def log(level: str, message: str) -> None:
            events.emit("log", severity=level, payload={"message": message})

        return log
