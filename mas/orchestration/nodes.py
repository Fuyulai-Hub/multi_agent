"""把「执行一个节点」包装成 LangGraph 节点函数（docs/50 §5.3）。

节点函数里做的事与 legacy ``while`` 循环每一轮的循环体完全相同：
执行（``Leader._execute_node``）→ 决策（``orchestration.decision.decide``）→ 写检查点。
router 处不产生任何副作用，副作用只发生在这里（理由见 graph.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .decision import decide
from .state import RunState


@dataclass
class GraphDeps:
    """闭包注入的运行时依赖：不可序列化的对象一律走这里，不进 state。"""

    leader: Any
    ctx: Any
    agents: Dict[str, Any]
    registry: Optional[Any]
    cancel_event: Optional[Any]
    run_id: str
    checkpoint_path: Path
    requirement_title: str
    requirement_sha256: str
    project_docs_text: str
    started_at: str
    max_total_rollbacks: int
    # 节点内被 LLM 层中断时，收尾要用「被打断的那个节点」和已计入的步数
    current_node: str = ""
    in_flight_steps: int = 0


def make_node(deps: GraphDeps, node_id: str, node: Any) -> Callable[[RunState], Dict[str, Any]]:
    leader, ctx = deps.leader, deps.ctx

    def _node(state: RunState) -> Dict[str, Any]:
        # 协作式终止的节点边界检查（docs/41 的第二层）：节点不含 LLM 调用的执行段
        # （沙箱跑测试、MCP 工具调用）期间置位时，跑完当前节点后在此收口。
        if leader._cancel_requested(deps.cancel_event, deps.registry, deps.run_id):
            return {"next_node": "halt", "canceled": True}

        ctx.run_state.update(state.get("run_state") or {})
        steps = int(state.get("steps") or 0) + 1
        iterations = dict(state.get("iterations") or {})
        rollback_counts = dict(state.get("rollback_counts") or {})
        iteration = iterations.get(node_id, 0) + 1
        iterations[node_id] = iteration
        deps.current_node = node_id
        deps.in_flight_steps = steps

        result, status = leader._execute_node(
            ctx, deps.agents, node,
            iteration=iteration, steps=steps,
            rollback_reason=str(state.get("pending_reason") or "") if iteration > 1 else "",
            registry=deps.registry,
        )
        decision = decide(
            leader, ctx, deps.agents, node, result, status,
            iteration=iteration, iterations=iterations, rollback_counts=rollback_counts,
            forced_fail=bool(state.get("forced_fail")),
            max_total_rollbacks=deps.max_total_rollbacks,
        )
        # 检查点写在「节点完成 + 分支落定」之后：崩溃发生在节点内部时 next_node
        # 仍是该节点，恢复会重跑它；已完成的节点不会重跑（与 legacy 同口径）。
        leader._write_checkpoint(
            deps.checkpoint_path, req_title=deps.requirement_title,
            requirement_sha256=deps.requirement_sha256, next_node=decision.target,
            iterations=iterations, rollback_counts=rollback_counts,
            pending_reason=decision.pending_reason, forced_fail=decision.forced_fail,
            steps=steps, run_state=ctx.run_state, project_docs_text=deps.project_docs_text,
            started_at=deps.started_at,
        )
        return {
            "node_id": node_id,
            "steps": steps,
            "iteration": iteration,
            "last_status": status,
            "last_summary": result.summary[:300],
            "next_node": decision.target,
            "pending_reason": decision.pending_reason,
            "forced_fail": decision.forced_fail,
            "iterations": iterations,
            "rollback_counts": rollback_counts,
            "run_state": dict(ctx.run_state),
        }

    return _node
