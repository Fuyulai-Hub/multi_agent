"""上下文压缩（Prompt 上下文治理，docs/16 的 02 文档）。

现状只有一处「压缩」：``blackboard.brief()`` 的无结构硬截断（超长直接切字符串，JSON 被切坏）。
本模块补两件事，全部在 ``enabled=false`` 时恒等返回（行为与现状逐字节一致）：

1. 感知段结构化压缩 ``found_artifacts``：按 artifact 名分派压缩策略（白名单制），
   未知名保持现状硬截断不动；先按 ``per_item_chars`` 压，再按 ``total_chars`` 优先级裁剪；
2. tool 结果统一截断 ``truncate_tool_results``：头 60% + 尾 40%，中间标注省略字符数。

边界原则：压缩只改「进 Prompt 的内容」，**绝不改黑板上的 artifact 数据**——
门禁求值走 ``blackboard.snapshot()``，与压缩完全隔离（压缩出错不可能刷绿门禁）。
本模块不 import LLM（LLM 摘要列为可选第二步，默认关闭）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

__all__ = ["CompressionResult", "found_artifacts", "truncate_tool_results"]


@dataclass
class CompressionResult:
    """压缩结果：artifact 映射 + 前后字符数 + 被压缩/未压缩项名（供事件审计）。"""

    artifacts: Dict[str, Any] = field(default_factory=dict)
    before_chars: int = 0
    after_chars: int = 0
    compressed: List[str] = field(default_factory=list)
    untouched: List[str] = field(default_factory=list)


def _size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


# --------------------------------------------------------------------------- 策略表
def _compress_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """需求规格：保 FP 的 id/title/description/inputs/outputs/rules/category/acceptance；
    丢 parameters 与 samples（体积最大、对下游实现/测试的可推导价值最低）。"""
    out = {key: value for key, value in spec.items() if key != "function_points"}
    function_points: List[Any] = []
    for fp in spec.get("function_points") or []:
        if not isinstance(fp, dict):
            function_points.append(fp)
            continue
        reduced = {
            key: fp.get(key)
            for key in ("id", "title", "description", "inputs", "outputs",
                        "rules", "category", "acceptance", "ambiguous")
        }
        function_points.append({k: v for k, v in reduced.items() if v not in (None, [], "")})
    out["function_points"] = function_points
    out["_compressed"] = True
    return out


def _compress_spec_deep(spec: Dict[str, Any]) -> Dict[str, Any]:
    """进一步裁剪：只保 FP 的 id/title/rules/category（永不丢，供下游规则对齐）。"""
    out = {key: value for key, value in spec.items() if key != "function_points"}
    out["function_points"] = [
        {k: fp.get(k) for k in ("id", "title", "rules", "category")
         if isinstance(fp, dict) and fp.get(k) not in (None, [], "")}
        for fp in spec.get("function_points") or []
    ]
    out["_compressed"] = True
    return out


def _compress_test_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """测试报告：保 summary/coverage/negotiation 计数；丢逐条用例明细与落盘文件。"""
    out = {key: report[key] for key in ("package", "summary", "coverage", "negotiation")
           if key in report}
    out["_compressed"] = True
    return out


def _compress_test_report_deep(report: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: report[key] for key in ("summary", "coverage") if key in report}
    out["_compressed"] = True
    return out


def _compress_review_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """审查报告：保 verdict/blocking 计数与 blocking_items；丢 findings 全量明细。"""
    out = {key: report[key] for key in ("verdict", "blocking", "blocking_items", "stats", "coverage")
           if key in report}
    out["_compressed"] = True
    return out


def _compress_review_report_deep(report: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: report[key] for key in ("verdict", "blocking", "stats") if key in report}
    out["_compressed"] = True
    return out


def _compress_bug_reports(bugs: Dict[str, Any]) -> Dict[str, Any]:
    """缺陷单：保 total/open/closed 计数与 open items；丢 closed 明细。"""
    out = {key: bugs[key] for key in ("total", "open", "closed") if key in bugs}
    out["items"] = [item for item in (bugs.get("items") or [])
                    if isinstance(item, dict) and item.get("status") == "open"]
    out["_compressed"] = True
    return out


_STRATEGIES = {
    "requirement_spec": _compress_spec,
    "test_report": _compress_test_report,
    "review_report": _compress_review_report,
    "bug_reports": _compress_bug_reports,
}

_DEEP = {
    "requirement_spec": _compress_spec_deep,
    "test_report": _compress_test_report_deep,
    "review_report": _compress_review_report_deep,
    "bug_reports": _compress_bug_reports,
}

#: total 预算超限时的裁剪顺序（优先级 spec > solution_design > review/test 明细）
_TOTAL_TRIM_ORDER = ["test_report", "review_report", "bug_reports"]


# --------------------------------------------------------------------------- 入口
def found_artifacts(
    found: Dict[str, Any],
    *,
    enabled: bool = False,
    per_item_chars: int = 10000,
    total_chars: int = 32000,
) -> CompressionResult:
    """对感知到的上游产物做结构化压缩；``enabled=false`` 时恒等返回。

    预算为 0 / 负数时只做「深压」不抛异常；artifact 为 null / 标量时保持原样（不压缩）。
    """
    if not isinstance(found, dict):
        return CompressionResult({}, 0, 0, [], [])
    before = sum(_size(value) for value in found.values())
    if not enabled or not found:
        return CompressionResult(dict(found), before, before, [], list(found.keys()))

    per_item = max(1, int(per_item_chars))
    artifacts: Dict[str, Any] = {}
    compressed: List[str] = []
    untouched: List[str] = []
    for name, value in found.items():
        strategy = _STRATEGIES.get(name)
        if strategy is None or not isinstance(value, dict):
            # 白名单制：未知 artifact / 非 dict 产物保持现状（交由 brief 的硬截断兜底）
            artifacts[name] = value
            untouched.append(name)
            continue
        try:
            reduced = strategy(value)
        except Exception:  # noqa: BLE001 - 压缩策略异常不得影响主流程
            artifacts[name] = value
            untouched.append(name)
            continue
        if _size(reduced) > per_item:
            reduced = _DEEP[name](reduced)
        if _size(reduced) < _size(value):
            compressed.append(name)
        else:
            reduced = value
            untouched.append(name)
        artifacts[name] = reduced

    # 总量预算：仍超限则按优先级继续裁剪低优先级产物的明细
    after = sum(_size(value) for value in artifacts.values())
    if int(total_chars) > 0 and after > int(total_chars):
        for name in _TOTAL_TRIM_ORDER:
            if after <= int(total_chars) or name not in artifacts:
                break
            value = artifacts[name]
            if not isinstance(value, dict):
                continue
            reduced = _DEEP[name](value)
            if _size(reduced) < _size(value):
                artifacts[name] = reduced
                if name not in compressed:
                    compressed.append(name)
                after = sum(_size(item) for item in artifacts.values())
    return CompressionResult(artifacts, before, after, compressed, untouched)


def truncate_tool_results(messages: List[Dict[str, Any]], *, max_chars: int = 4000) -> List[Dict[str, Any]]:
    """tool 结果统一截断：头 60% + 尾 40%，中间标注「省略 N 字符」（不改其它字段）。

    ``max_chars <= 0`` 时不截断（默认关闭，行为与现状一致）。
    """
    if max_chars <= 0 or not messages:
        return messages
    head_len = int(max_chars * 0.6)
    tail_len = max_chars - head_len
    out: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and len(content) > max_chars:
            omitted = len(content) - max_chars
            message = dict(message)
            message["content"] = (
                content[:head_len]
                + f"\n...(省略 {omitted} 字符)...\n"
                + content[len(content) - tail_len:]
            )
        out.append(message)
    return out
