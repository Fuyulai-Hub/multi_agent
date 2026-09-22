"""系统异常类型。"""

from __future__ import annotations


class MasError(Exception):
    """系统基础异常。"""


class ConfigError(MasError):
    """配置缺失或非法。"""


class AgentError(MasError):
    """Agent 执行失败。"""


class SandboxError(MasError):
    """沙箱执行异常。"""


class GateError(MasError):
    """门禁表达式求值异常。"""


class RollbackExhausted(MasError):
    """回退次数耗尽。"""
