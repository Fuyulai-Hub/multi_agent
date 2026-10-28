"""系统异常类型。"""

from __future__ import annotations


class MasError(Exception):
    """系统基础异常。"""


class ConfigError(MasError):
    """配置缺失或非法。"""


class AgentError(MasError):
    """Agent 执行失败。"""


class RunCanceledError(BaseException):
    """用户手动终止：LLM 层在流式读取/退避等待中发现取消信号后立即抛出。

    特意继承 BaseException 而非 Exception/MasError（同 asyncio.CancelledError 的
    设计理由）：Agent 各层遍布 `except Exception` 降级兜底（如 product 的
    _generate_hidden_suite、llm 的 AgentError 分支），Exception 子类会被就近吞成
    "节点失败/静默降级"，取消信号永远传不到 Leader。BaseException 让所有通用
    except 自然拦不住它，只有 Leader 状态机的 `except RunCanceledError` 能接住并
    走既有取消收尾。全仓 `except BaseException` 仅存在于 verify/runner.py 的
    测试执行器（try 块内无 LLM 调用），不会反向吞掉本异常。
    """


class SandboxError(MasError):
    """沙箱执行异常。"""


class GateError(MasError):
    """门禁表达式求值异常。"""


class RollbackExhausted(MasError):
    """回退次数耗尽。"""
