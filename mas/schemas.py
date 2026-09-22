"""Agents 与调度器之间的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Criterion, NodeConfig

__all__ = ["NodeTask", "AgentResult", "AgentContext"]


@dataclass
class NodeTask:
    """交给某个 Agent 执行的一次节点任务。"""

    node_id: str
    title: str
    role: str
    kind: str
    iteration: int
    directive: str = ""
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)
    criteria: List[Criterion] = field(default_factory=list)
    rollback_reason: str = ""

    @classmethod
    def from_node(cls, node: NodeConfig, *, iteration: int, directive: str = "", rollback_reason: str = "") -> "NodeTask":
        return cls(
            node_id=node.id,
            title=node.title,
            role=str(node.role or "leader"),
            kind=node.kind,
            iteration=iteration,
            directive=directive,
            inputs=list(node.inputs),
            outputs=list(node.outputs),
            options=dict(node.options),
            criteria=list(node.criteria),
            rollback_reason=rollback_reason,
        )


@dataclass
class AgentResult:
    """Agent 执行结果（调度器只依赖这些字段）。"""

    status: str = "pass"                      # pass | fail
    summary: str = ""
    artifacts: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    gate: Optional[Dict[str, Any]] = None
    prompt_path: Optional[Path] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    events: List[str] = field(default_factory=list)   # 需要广播的事件主题（配置矩阵决定接收者）
    broadcast_payload: Dict[str, Any] = field(default_factory=dict)
    consumed_experience: List[str] = field(default_factory=list)


@dataclass
class AgentContext:
    """Agent 运行时依赖（由 Leader 注入）。"""

    config: Any
    run_id: str
    run_dir: Path
    events: Any
    blackboard: Any
    bus: Any
    llm: Any
    sandbox: Any
    prompts: Any
    experience: Any
    log: Callable[[str, str], None] = lambda level, message: None
    consult: Optional[Callable[..., Dict[str, Any]]] = None
    run_state: Dict[str, Any] = field(default_factory=dict)

    def role_config(self, role_id: str) -> Any:
        return self.config.role(role_id)
