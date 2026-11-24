"""LangGraph 图的运行状态（docs/50 §5.1）。

硬约束：state 里只允许标量、字典、列表。``Blackboard`` / ``Path`` / ``Sandbox``
这类运行时对象一律通过 ``GraphDeps`` 闭包注入，不进 state——否则 checkpointer
序列化失败、续跑直接崩。
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, TypedDict


def merge_dict(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """字典通道的 reducer：后写的覆盖同键，未涉及的键保留。"""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class RunState(TypedDict, total=False):
    max_steps: int                       # 本次运行的步数上限（recursion_limit 的语义来源）
    steps: int                           # 已执行节点数，与 legacy 循环同口径
    iteration: int                       # 最近一次执行的节点是它的第几轮
    last_status: str                     # "pass" | "fail"
    last_summary: str
    next_node: str                       # router 只读这个字段
    pending_reason: str                  # 下一轮回退的归因（等价于 leader 的 pending_reason）
    forced_fail: bool
    canceled: bool
    iterations: Annotated[Dict[str, int], merge_dict]
    rollback_counts: Annotated[Dict[str, int], merge_dict]
    run_state: Dict[str, Any]            # ctx.run_state 的可序列化投影
