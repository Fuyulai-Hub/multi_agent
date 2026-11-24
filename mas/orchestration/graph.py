"""``flow.yaml`` → ``StateGraph`` 的结构映射（docs/50 §3、§5.2）。

映射关系是双射，没有重新设计模型：
``flow.entry`` → START 条件边；``node.transitions`` → 条件边；``end``/``halt`` → END。

两条硬约束：
1. **router 必须是纯函数**。LangGraph 在 superstep 边界会重算路由，router 里发事件
   会在恢复时重复写进 ``events.jsonl``（前端时间线出现两次回退）。所有副作用
   （rollback 事件、经验降权、escalation）都在节点函数里，router 只读 state。
2. **``recursion_limit`` 必须显式设**（见 runner.py）。默认 25 比 ``max_steps: 40`` 小，
   不设会在长流程上提前抛 ``GraphRecursionError``。
"""

from __future__ import annotations

from typing import Dict

from langgraph.graph import END, START, StateGraph

from .nodes import GraphDeps, make_node
from .state import RunState

TERMINAL = {"end", "halt", ""}


def build_graph(deps: GraphDeps) -> StateGraph:
    flow = deps.leader.config.flow
    builder = StateGraph(RunState)

    for node_id, node in flow.nodes.items():
        builder.add_node(node_id, make_node(deps, node_id, node))

    # 入口：新运行走 flow.entry；从 checkpoint.json 降级续跑时直接落到 next_node。
    all_ids: Dict[str, str] = {node_id: node_id for node_id in flow.nodes}
    all_ids.update({"end": END, "halt": END})

    def entry_route(state: RunState) -> str:
        return state.get("next_node") or flow.entry

    builder.add_conditional_edges(START, entry_route, all_ids)

    for node_id, node in flow.nodes.items():
        # 终止语义固定映射：即使某节点 transitions 里没写 halt，decide() 也可能兜底到 halt；
        # 只遍历 pass/fail —— NodeConfig.target() 内部已兜底 "default"。
        path_map = {"end": END, "halt": END}
        for branch in ("pass", "fail"):
            target = node.target(branch)
            if target and target not in TERMINAL:
                path_map[target] = target
        builder.add_conditional_edges(node_id, route_from_state, path_map)

    return builder


def route_from_state(state: RunState) -> str:
    """纯函数：只读 state，不做任何副作用。

    ``steps >= max_steps`` 时回 halt，与切换前手写循环的
    ``while ... and steps < max_steps`` 同一个退出点（否则图会多跑一个节点）。
    """
    if int(state.get("steps") or 0) >= int(state.get("max_steps") or 0):
        return "halt"
    return state.get("next_node") or "halt"
