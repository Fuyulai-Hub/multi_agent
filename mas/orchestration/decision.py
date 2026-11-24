"""节点执行后的分支决策：pass/fail → 下一节点 / 回退 / 强制收尾（docs/50 §5.4）。

本模块的逻辑是从 ``leader.py`` 手写状态机的 ``while`` 体里逐行搬出来的，语义不变：
只做「计算 + 审计事件」，不改流程拓扑（profile 启动期定型，flow 只有 pass/fail 两条边）。

legacy 循环与 LangGraph 图节点共用本函数——两条编排路径的分支行为因此不可能漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class Decision:
    target: str                  # 下一节点 id，或 end/halt
    pending_reason: str = ""     # 下一轮该节点的 rollback_reason
    forced_fail: bool = False    # 是否已判定「不是交付物问题」的强制不通过


def decide(
    leader: Any,
    ctx: Any,
    agents: Dict[str, Any],
    node: Any,
    result: Any,
    status: str,
    *,
    iteration: int,
    iterations: Dict[str, int],
    rollback_counts: Dict[str, int],
    forced_fail: bool,
    max_total_rollbacks: int,
) -> Decision:
    """返回下一步去哪；回退预算、权限、escalation 的判定与改造前逐条一致。"""
    flow = leader.config.flow
    collaboration = leader.config.collaboration
    events = ctx.events
    node_id = node.id

    branch = collaboration.branch_of(status)
    target = node.target(branch) or "halt"
    if branch != "fail":
        return Decision(target, "", forced_fail)

    # 环境/证据通道缺口（docs/15 B 档；task04 的 C++ 工具链缺失同属此类）：
    # 退回研发改代码无法消除，继续回退只会空转并耗尽回退预算（本项目的
    # web_todo_spa 实测：36/36 用例全过仍被判 fail，4 次回退预算全耗在通道缺口上）。
    # 立即如实收尾（forced fail + 门禁 + 报告），不消耗回退次数。
    unfixable = leader._unfixable_failure_reason(ctx, node_id, iteration)
    if unfixable:
        events.emit(
            "escalation", agent="leader", node=node_id, severity="error",
            payload={"reason": unfixable,
                     "strategy": collaboration.escalation.get("on_exhausted", "fail_run_and_report"),
                     "notify": collaboration.escalation.get("notify", [])},
        )
        leader._publish_forced_gate(ctx.blackboard, "环境/证据通道缺口", unfixable,
                                    iteration, run_id=ctx.run_id)
        return Decision("report" if "report" in flow.nodes else "halt", "", True)

    if not leader._is_rollback(node_id, target, iterations, rollback_counts):
        return Decision(target, "", forced_fail)

    # 权限落地：节点角色显式声明 can_rollback=false 时，拒绝回退并终止流程
    if not leader._rollback_permitted(node_id, node, target, agents, events, iteration):
        return Decision("halt", f"角色 {node.role} 无回退权限（can_rollback=false），流程终止",
                        forced_fail)

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
    leader._broadcast(ctx, "leader", "rollback",
                      {"from": node_id, "to": target, "reason": reason, "attempt": attempt})
    # 经验反馈闭环：回退归因降权。只降权「声称能预防本次失败」的经验
    # （trigger 与失败节点本次执行的事件匹配），不误伤无关经验。
    leader._reinforce_injected(
        ctx, outcome="negative", evidence=f"{node_id} 回退：{reason}",
        failure_triggers=leader._failure_triggers_of(ctx, node_id, iteration),
    )
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
            leader._publish_forced_gate(ctx.blackboard, "回退次数耗尽", pending_reason,
                                       iteration, run_id=ctx.run_id)
            target = "report"
        else:
            target = "halt"
    return Decision(target, pending_reason, forced_fail)
