"""Agent 实现与注册表。"""

from __future__ import annotations

from typing import Any, Dict

from ..schemas import AgentContext
from .base import BaseAgent
from .developer import DeveloperAgent
from .product import ProductAgent
from .reporter import ReporterAgent
from .reviewer import ReviewerAgent
from .tester import TesterAgent

__all__ = ["BaseAgent", "AGENT_REGISTRY", "create_agent"]

AGENT_REGISTRY: Dict[str, Any] = {
    "product": ProductAgent,
    "developer": DeveloperAgent,
    "reviewer": ReviewerAgent,
    "tester": TesterAgent,
    "reporter": ReporterAgent,
}


def create_agent(role_id: str, ctx: AgentContext) -> BaseAgent:
    """按角色 id 实例化 Agent（角色-实现的绑定集中在注册表）。"""
    try:
        return AGENT_REGISTRY[role_id](ctx)
    except KeyError as exc:
        raise KeyError(f"未注册的 Agent 角色: {role_id}") from exc
