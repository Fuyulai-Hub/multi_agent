"""上下文管理：把「进模型的请求体」关进 token 预算里（docs/35）。

对标的 Codex 实现（codex-code/codex/codex-rs）是**分层**的，本模块按同一分层落地：

    tool 结果落历史时      -> truncate   单条截断（头 + 尾，中间挖掉）
    提交前仍超限          -> trim       从最老的工具往返开始整轮丢弃
    丢弃过内容            -> compact    折叠成一条摘要挂回（不静默丢信息）
    仍超限                -> overflow   只告警，不破坏交付契约

与 Codex 的对照与分歧（如实记录）：

* ``APPROX_BYTES_PER_TOKEN = 4`` 直接取自 Codex
  （``utils/string/src/truncate.rs:6``），不引入 tiktoken；
* 双阈值 90% / 95% 取自 Codex 的 ``auto_compact_token_limit`` 与
  ``effective_context_window_percent``（``protocol/src/openai_models.rs:393``）；
* **分歧一**：Codex 的阈值按「整个 context_window」算，本实现先从窗口扣掉
  ``llm.max_tokens`` 再算可用预算。本框架 max_tokens=64000 占 262144 窗口的 24%，
  不扣会把「输入还剩 24% 就撞墙」误判成还有余量；
* **分歧二**：Codex 的摘要由模型生成（一次真实请求），本实现默认用**规则抽取**
  生成摘要。理由是本端点网关 300s 断开是已实测的失败模式，而压缩恰恰只发生在
  上下文已经很大的时候——那时再发一次请求，是把自己送进最容易超时的区间。
  规则抽取确定性、零额外调用、可回放；``ContextManager`` 留出 ``summary_fn``
  注入点，需要模型摘要时从外部传入即可，不必改本模块。

边界原则（与 ``mas.compress`` 一致）：治理只改「进 Prompt 的请求体」，绝不改黑板
上的 artifact 数据，因此不可能刷绿门禁。未启用或未超限时 :meth:`ContextManager.fit`
返回原列表，**请求体逐字节不变**——这是所有回归测试的基线。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "APPROX_BYTES_PER_TOKEN",
    "ContextBudget",
    "ContextManager",
    "FitResult",
    "SUMMARY_PREFIX",
    "approx_token_count",
    "build_summary",
    "estimate_messages",
    "message_tokens",
]

#: Codex 的粗估常量：UTF-8 字节数 / 4（utils/string/src/truncate.rs:6）。
#: 中文一个字 3 字节 -> 0.75 token，属"粗下界"；Codex 源码注释原话是
#: "a coarse lower bound, not a tokenizer-accurate count"。
APPROX_BYTES_PER_TOKEN = 4

#: 单张图片粗估：Codex 的 RESIZED_IMAGE_BYTES_ESTIMATE(7373 字节) / 4。
#: 本框架拿不到缩放后尺寸，按同一常量折算，避免图片把估算拉到离谱的 0。
IMAGE_TOKENS = 7373 // APPROX_BYTES_PER_TOKEN

#: 每条消息的角色与协议字段开销（Codex 同样按 item 粗算，不逐字段精算）。
MESSAGE_OVERHEAD_TOKENS = 4

#: 摘要前缀（对齐 Codex 的 summary_prefix.md：告诉模型"接着别人干，别重复"）。
SUMMARY_PREFIX = (
    "以下是本次任务早前步骤的压缩摘要（原文已从上下文中移除以控制长度）。"
    "请据此继续推进，不要重复已经完成的动作："
)


# --------------------------------------------------------------------------- 估算
def approx_token_count(text: str) -> int:
    """按 Codex 口径粗估 token 数：UTF-8 字节数 / 4（向上取整）。"""
    if not text:
        return 0
    return (len(text.encode("utf-8")) + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def _content_tokens(content: Any) -> int:
    """消息 content 的 token 估算（str / parts 数组 / None 三种形态）。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return approx_token_count(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "image_url":
                    total += IMAGE_TOKENS
                total += approx_token_count(str(part.get("text") or ""))
            else:
                total += approx_token_count(str(part))
        return total
    return approx_token_count(str(content))


def message_tokens(message: Dict[str, Any]) -> int:
    """单条消息的 token 估算（含 tool_calls / reasoning_content / name）。"""
    total = MESSAGE_OVERHEAD_TOKENS
    total += _content_tokens(message.get("content"))
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if reasoning:
        total += _content_tokens(reasoning)
    if message.get("tool_calls"):
        total += approx_token_count(json.dumps(message["tool_calls"], ensure_ascii=False, default=str))
    if message.get("tool_call_id"):
        total += 2
    if message.get("name"):
        total += approx_token_count(str(message["name"]))
    return total


def estimate_messages(messages: Sequence[Dict[str, Any]]) -> int:
    """整份请求体的 token 粗估。"""
    return sum(message_tokens(message) for message in messages if isinstance(message, dict))


# --------------------------------------------------------------------------- 预算
@dataclass
class ContextBudget:
    """上下文预算（对应 Codex 的 ModelInfo 窗口字段 + 配置覆盖）。"""

    context_window: int = 262144        # llm.context_window
    reserve_output: int = 0             # 预留给输出的额度（= llm.max_tokens）
    compact_ratio: float = 0.90         # Codex: auto_compact_token_limit = window * 90%
    hard_ratio: float = 0.95            # Codex: effective_context_window_percent = 95

    @property
    def usable(self) -> int:
        """输入侧可用预算：窗口扣掉输出预留。"""
        return max(1, self.context_window - max(0, self.reserve_output))

    @property
    def compact_limit(self) -> int:
        """达到此值即启动治理（Codex 的自动压缩阈值）。"""
        return max(1, int(self.usable * self.compact_ratio))

    @property
    def hard_limit(self) -> int:
        """治理后仍超此值 = 上下文溢出（只告警，不再破坏请求）。"""
        return max(self.compact_limit, int(self.usable * self.hard_ratio))


@dataclass
class FitResult:
    """一次治理的结果（供事件留痕与回归断言）。"""

    messages: List[Dict[str, Any]] = field(default_factory=list)
    before_tokens: int = 0
    after_tokens: int = 0
    limit: int = 0
    stages: List[str] = field(default_factory=list)
    truncated: int = 0          # 阶段一截断的 tool 消息条数
    dropped: int = 0            # 阶段二丢弃的消息条数
    compacted: bool = False     # 是否挂回了摘要
    overflow: bool = False      # 治理后仍超硬上限
    summary_chars: int = 0

    @property
    def changed(self) -> bool:
        """请求体是否被真正改动过（False 时行为与未启用逐字节一致）。"""
        return bool(self.stages)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "limit": self.limit,
            "stages": list(self.stages),
            "truncated": self.truncated,
            "dropped": self.dropped,
            "compacted": self.compacted,
            "overflow": self.overflow,
            "summary_chars": self.summary_chars,
        }


# --------------------------------------------------------------------------- 结构切分
def _split_prefix(messages: List[Dict[str, Any]]) -> int:
    """返回「不可改动前缀」的长度：开头的 system/developer + 首个 user。

    这两段是本次请求的骨架（角色人设 + 任务指令 + 交付契约），
    任何阶段都不动——截断它们会切坏末尾的 JSON 契约，比超窗更致命。
    """
    index = 0
    while index < len(messages) and messages[index].get("role") in ("system", "developer"):
        index += 1
    if index < len(messages) and messages[index].get("role") == "user":
        index += 1
    return index


def _group_units(messages: List[Dict[str, Any]]) -> List[List[int]]:
    """把消息切成可整体丢弃的单元（保持 OpenAI 协议要求的配对关系）。

    ``assistant(tool_calls)`` 必须与其后的 ``tool`` 消息同生共死：单独删掉
    assistant 会让 tool 消息的 tool_call_id 找不到归属，严格实现的端点直接 400。
    """
    units: List[List[int]] = []
    pending: Optional[List[int]] = None
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            if pending is not None:
                units.append(pending)
            pending = [index]
        elif role == "tool" and pending is not None:
            pending.append(index)
        else:
            if pending is not None:
                units.append(pending)
                pending = None
            units.append([index])
    if pending is not None:
        units.append(pending)
    return units


def _pick(units: List[List[int]], source: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for unit in units:
        out.extend(source[index] for index in unit)
    return out


# --------------------------------------------------------------------------- 阶段一
def truncate_tool_messages(
    messages: List[Dict[str, Any]], max_chars: int
) -> tuple[List[Dict[str, Any]], int]:
    """单条 tool 结果截断：头 60% + 尾 40%，中间标注省略字符数。

    与 ``mas.compress.truncate_tool_results`` 同形状，但**只作用于 role=tool**：
    那份工具会把整份 messages 里的长字符串都切一遍，包括主 user Prompt
    （切坏交付契约），因此本模块自带一份角色限定版。
    保留尾部是有意的：错误堆栈与最终结果通常落在末尾。
    """
    if max_chars <= 0 or not messages:
        return messages, 0
    head_len = int(max_chars * 0.6)
    tail_len = max_chars - head_len
    out: List[Dict[str, Any]] = []
    count = 0
    for message in messages:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str) and len(content) > max_chars:
            omitted = len(content) - max_chars
            message = dict(message)
            message["content"] = (
                content[:head_len]
                + f"\n...(省略 {omitted} 字符)...\n"
                + content[len(content) - tail_len:]
            )
            count += 1
        out.append(message)
    return out, count


# --------------------------------------------------------------------------- 阶段三
def build_summary(
    messages: Sequence[Dict[str, Any]], *, max_chars: int = 2000, per_item_chars: int = 200
) -> str:
    """从被丢弃的消息里规则抽取摘要（默认零额外 LLM 调用，见模块文档分歧二）。

    抽取三类事实：动作（调用了哪些工具）、观察（工具回了什么）、结论（模型说了什么）。
    ``max_chars`` 约束的是摘要正文（不含 :data:`SUMMARY_PREFIX` 前缀）。
    """
    lines: List[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            names = [
                str(call.get("function", {}).get("name") or call.get("name") or "")
                for call in message["tool_calls"]
                if isinstance(call, dict)
            ]
            names = [name for name in names if name]
            if names:
                lines.append(f"- 动作：调用工具 {'、'.join(names)}")
        elif role == "tool":
            name = str(message.get("name") or "")
            text = " ".join(str(message.get("content") or "").split())
            lines.append(f"- 观察（{name}）：{text[:per_item_chars]}")
        elif role == "assistant":
            text = " ".join(str(message.get("content") or "").split())
            if text:
                lines.append(f"- 结论：{text[:per_item_chars]}")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + f"…（摘要截断 {len(body) - max_chars} 字符）"
    return f"{SUMMARY_PREFIX}\n{body}"


# --------------------------------------------------------------------------- 管理器
class ContextManager:
    """上下文预算治理器：估算 → 判阈值 → 分阶段收敛 → 留痕。"""

    def __init__(
        self,
        budget: ContextBudget,
        *,
        enabled: bool = False,
        tool_result_chars: int = 0,
        keep_recent_rounds: int = 1,
        summary_max_chars: int = 2000,
        calibrate: bool = False,
        summary_fn: Optional[Callable[[Sequence[Dict[str, Any]]], str]] = None,
    ) -> None:
        self.budget = budget
        self.enabled = enabled
        self.tool_result_chars = max(0, int(tool_result_chars))
        self.keep_recent_rounds = max(0, int(keep_recent_rounds))
        self.summary_max_chars = max(1, int(summary_max_chars))
        self.calibrate = calibrate
        self.summary_fn = summary_fn
        # 观测态：last_tokens 是本地估算，last_reported 是端点返回的真实 prompt_tokens
        self.last_tokens = 0
        self.last_reported = 0
        self._ratio = 1.0

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_config(cls, config: Any) -> "ContextManager":
        """按 system.yaml 构造（未配置 runtime.context 时 enabled=False，行为不变）。"""
        llm = dict(getattr(config, "llm", None) or {})
        runtime = dict(getattr(config, "runtime", None) or {})
        cfg = dict(runtime.get("context") or {})
        # reserve_output：默认开启。max_tokens=64000 占 262144 窗口 24%，
        # 不预留会把"输入撞墙"误判成"还有余量"（与 Codex 的分歧一）。
        reserve = int(llm.get("max_tokens", 0) or 0) if bool(cfg.get("reserve_output", True)) else 0
        budget = ContextBudget(
            context_window=int(llm.get("context_window", 262144) or 262144),
            reserve_output=reserve,
            compact_ratio=float(cfg.get("compact_ratio", 0.90) or 0.90),
            hard_ratio=float(cfg.get("hard_ratio", 0.95) or 0.95),
        )
        return cls(
            budget,
            enabled=bool(cfg.get("enabled", False)),
            tool_result_chars=int(cfg.get("tool_result_chars", 4000) or 0),
            keep_recent_rounds=int(cfg.get("keep_recent_rounds", 1) or 0),
            summary_max_chars=int(cfg.get("summary_max_chars", 2000) or 2000),
            calibrate=bool(cfg.get("calibrate", False)),
        )

    # ---------------------------------------------------------------- 估算
    def estimate(self, messages: Sequence[Dict[str, Any]]) -> int:
        """估算 token 数（calibrate 开启时按端点实测比例缩放）。"""
        raw = estimate_messages(messages)
        if not self.calibrate:
            return raw
        return max(1, int(raw * self._ratio))

    # ---------------------------------------------------------------- 治理
    def fit(self, messages: Optional[List[Dict[str, Any]]]) -> FitResult:
        """把请求体收敛进预算。**未启用或未超限时原样返回**（逐字节不变）。"""
        if not messages:
            return FitResult(list(messages or []))
        before = self.estimate(messages)
        self.last_tokens = before
        limit = self.budget.compact_limit
        if not self.enabled or before <= limit:
            return FitResult(list(messages), before, before, limit)

        prefix = _split_prefix(messages)
        head = list(messages[:prefix])
        tail = list(messages[prefix:])
        stages: List[str] = []

        # 阶段一 truncate：单条 tool 结果头尾截断
        truncated = 0
        if self.tool_result_chars > 0:
            tail, truncated = truncate_tool_messages(tail, self.tool_result_chars)
            if truncated:
                stages.append("truncate")
        after = self.estimate(head + tail)
        if after <= limit:
            return FitResult(head + tail, before, after, limit, stages, truncated=truncated)

        # 阶段二 trim：从最老的工具往返开始整轮丢弃，至少保留最近 keep_recent_rounds 轮
        units = _group_units(tail)
        dropped: List[Dict[str, Any]] = []
        while len(units) > self.keep_recent_rounds and self.estimate(head + _pick(units, tail)) > limit:
            removed = units.pop(0)
            dropped.extend(tail[index] for index in removed)
        tail = _pick(units, tail)
        if dropped:
            stages.append("trim")

        # 阶段三 compact：丢过内容就必须挂回摘要，禁止静默丢信息。
        # 即使阶段二已经把体积压回预算内，摘要仍然挂——体积不超限不等于信息没丢。
        summary_chars = 0
        compacted = False
        if dropped:
            summary = (
                self.summary_fn(dropped)
                if self.summary_fn is not None
                else build_summary(dropped, max_chars=self.summary_max_chars)
            )
            if summary:
                tail = tail + [{"role": "user", "content": summary}]
                compacted = True
                stages.append("compact")
                summary_chars = len(summary)

        after = self.estimate(head + tail)
        overflow = after > self.budget.hard_limit
        if overflow:
            # 走到这里说明 system + 首个 user 本身就超出窗口——那是配置问题，
            # 不是历史堆积问题；再切就切坏交付契约了，只告警不动手。
            stages.append("overflow")
        return FitResult(
            head + tail, before, after, limit, stages,
            truncated=truncated, dropped=len(dropped), compacted=compacted,
            overflow=overflow, summary_chars=summary_chars,
        )

    # ---------------------------------------------------------------- 观测
    def observe(self, generation: Any) -> None:
        """用端点返回的 prompt_tokens 校准本地估算（Codex：优先信服务端 usage）。"""
        reported = int(getattr(generation, "prompt_tokens", 0) or 0)
        if reported <= 0 or self.last_tokens <= 0:
            return
        self.last_reported = reported
        if not self.calibrate:
            return
        sample = min(4.0, max(0.25, reported / self.last_tokens))
        self._ratio = round(self._ratio * 0.7 + sample * 0.3, 4)

    # ---------------------------------------------------------------- 留痕
    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "context_window": self.budget.context_window,
            "reserve_output": self.budget.reserve_output,
            "compact_limit": self.budget.compact_limit,
            "hard_limit": self.budget.hard_limit,
            "last_tokens": self.last_tokens,
            "last_reported": self.last_reported,
            "ratio": self._ratio,
        }
