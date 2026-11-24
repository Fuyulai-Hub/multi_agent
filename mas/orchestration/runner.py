"""编排层门面：与 ``Leader.run()`` 的变量一一对齐（docs/50 §5.5）。

这里只负责四件事：装配依赖 → 建图 → 跑图（含取消/递归上限的收口）→ 把最终
state 交回 leader。收尾（对账、报告、指标、经验晋级）仍由 ``Leader.run()`` 完成，
不在这里重复实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from langgraph.errors import GraphRecursionError

from ..errors import RunCanceledError
from .checkpoint import checkpointer, snapshot_values, verify_consistency
from .graph import build_graph
from .nodes import GraphDeps


def _initial_state(max_steps: int, ctx: Any, resume_state: Optional[Dict[str, Any]],
                   entry: str) -> Dict[str, Any]:
    """新建运行从 entry 起步；``--resume`` 时带上次的进度输入（降级路径用）。"""
    if resume_state is None:
        return {"max_steps": max_steps, "steps": 0, "iterations": {}, "rollback_counts": {},
                "pending_reason": "", "forced_fail": False,
                "run_state": dict(ctx.run_state)}
    ctx.run_state.update(resume_state.get("run_state") or {})
    return {
        "max_steps": max_steps,
        "steps": int(resume_state.get("steps") or 0),
        "next_node": str(resume_state.get("next_node") or entry),
        "iterations": {str(k): int(v) for k, v in (resume_state.get("iterations") or {}).items()},
        "rollback_counts": {str(k): int(v) for k, v in
                            (resume_state.get("rollback_counts") or {}).items()},
        "pending_reason": str(resume_state.get("pending_reason") or ""),
        "forced_fail": bool(resume_state.get("forced_fail")),
        "run_state": dict(ctx.run_state),
    }


def run_graph(
    leader: Any,
    *,
    ctx: Any,
    agents: Dict[str, Any],
    registry: Optional[Any],
    cancel_event: Optional[Any],
    run_id: str,
    run_dir: Path,
    max_steps: int,
    max_total_rollbacks: int,
    checkpoint_path: Path,
    requirement_title: str,
    requirement_sha256: str,
    project_docs_text: str,
    started_at: str,
    resume_state: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    deps = GraphDeps(
        leader=leader, ctx=ctx, agents=agents, registry=registry, cancel_event=cancel_event,
        run_id=run_id, checkpoint_path=checkpoint_path,
        requirement_title=requirement_title, requirement_sha256=requirement_sha256,
        project_docs_text=project_docs_text, started_at=started_at,
        max_total_rollbacks=max_total_rollbacks,
    )
    init_state: Optional[Dict[str, Any]] = _initial_state(
        max_steps, ctx, resume_state, leader.config.flow.entry)
    canceled = False
    recursion_hit = False

    with checkpointer(run_dir) as saver:
        graph = build_graph(deps).compile(checkpointer=saver)
        config = {"configurable": {"thread_id": run_id},
                  # 显式设：LangGraph 默认 25 小于 max_steps=40，不设会提前抛 GraphRecursionError
                  "recursion_limit": max_steps * 2 + 10}
        if resume_state is not None:
            saved = snapshot_values(graph, config)
            if saved:
                verify_consistency(saved, resume_state)
                init_state = None          # 从 graph.sqlite 续跑，不重放输入
        try:
            for _ in graph.stream(init_state, config, stream_mode="values"):
                pass                        # 事件由节点内 emit，这里不消费
        except RunCanceledError:
            # LLM 层在节点内发现取消信号：转成既有取消收尾（docs/41 的契约不变）。
            # 节点函数里没有 try/except，BaseException 才能原样穿透到这里。
            canceled = True
            ctx.events.emit(
                "node_interrupted", agent="leader", node=deps.current_node, severity="warning",
                payload={"reason": "用户手动终止：节点内 LLM 调用被立即中断"},
            )
        except GraphRecursionError:
            recursion_hit = True
            ctx.events.emit("escalation", agent="leader", severity="error",
                            payload={"reason": f"达到最大步数 {max_steps}，强制终止"})
        final = snapshot_values(graph, config) or dict(init_state or {})

    cancel_flag = canceled or bool(final.get("canceled"))
    final_steps = int(final.get("steps") or 0)
    if cancel_flag or recursion_hit:
        # 中断的那一步已计入切换前口径的 steps，下一步固定落在 halt
        final_steps = max(final_steps, deps.in_flight_steps)
        next_node = "halt"
    else:
        next_node = str(final.get("next_node") or "halt")
    return {
        "next_node": next_node,
        "steps": final_steps,
        "canceled": cancel_flag,
        "forced_fail": bool(final.get("forced_fail")) or recursion_hit,
        "iterations": dict(final.get("iterations") or {}),
        "rollback_counts": dict(final.get("rollback_counts") or {}),
        "pending_reason": str(final.get("pending_reason") or ""),
        "run_state": dict(final.get("run_state") or {}),
    }
