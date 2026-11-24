"""LangGraph 检查点与既有 ``checkpoint.json`` 的关系（docs/50 §6.3）。

运行期续跑的权威源是 ``run_dir/graph.sqlite``；``checkpoint.json`` 继续按节点写，
降级为「人可读的审计快照」（前端、worker 与人工排查仍读它，不删）。

两个源的 ``next_node`` 不一致时拒绝续跑并报错，不做猜测——沿用
``Leader._resume_input`` 的既有原则。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver

from ..errors import ConfigError


def graph_db_path(run_dir: Path) -> Path:
    return Path(run_dir) / "graph.sqlite"


@contextmanager
def checkpointer(run_dir: Path) -> Iterator[SqliteSaver]:
    """一次运行一个 sqlite 连接；关闭连接即释放文件句柄（Windows 上必须显式关）。"""
    conn = sqlite3.connect(str(graph_db_path(run_dir)), check_same_thread=False)
    try:
        saver = SqliteSaver(conn)
        saver.setup()
        yield saver
    finally:
        conn.close()


def snapshot_values(graph: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """读该 thread 最新检查点的 state（没有检查点时为 {}）。"""
    snapshot = graph.get_state(config)
    return dict(snapshot.values or {})


def verify_consistency(values: Dict[str, Any], resume_state: Dict[str, Any]) -> None:
    """两个检查点源的下一步必须一致，不一致即拒绝续跑。"""
    sqlite_next = str(values.get("next_node") or "")
    json_next = str(resume_state.get("next_node") or "")
    if sqlite_next != json_next:
        raise ConfigError(
            f"无法恢复：graph.sqlite 的 next_node={sqlite_next!r} 与 "
            f"checkpoint.json 的 next_node={json_next!r} 不一致，拒绝猜测续跑位置"
        )
