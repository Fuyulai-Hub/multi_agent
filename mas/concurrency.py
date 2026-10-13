"""并发执行原语（IO 密集场景的线程池并行）。

适用场景：LLM HTTP 调用、沙箱子进程执行、按文件的 AST 静态分析——
这些任务的瓶颈都是 IO 等待，用线程池并行即可获得接近线性的加速，
无需把系统重构为多进程（多进程会破坏 Blackboard/Bus 的进程内状态模型）。

约束与取舍：
- 只提供 map 语义（保序），不提供 fire-and-forget：调用方需要全部结果才能继续；
- 单个任务异常不会拖垮整批：异常按原位置写回结果列表，由调用方决定处理；
- 线程数可配置，默认 min(8, len(items))：沙箱与 LLM 调用对并发敏感，不宜过大。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, List, Optional

__all__ = ["parallel_map", "default_workers"]


def default_workers() -> int:
    """默认并行度：CPU 密集部分很少，主要是 IO 等待，取 min(8, cpu+4)。"""
    return min(8, (os.cpu_count() or 2) + 4)


def parallel_map(
    fn: Callable[[Any], Any],
    items: Iterable[Any],
    *,
    max_workers: Optional[int] = None,
) -> List[Any]:
    """保序并行 map：results[i] 对应 items[i]；单个任务异常写回该位置（不拖垮整批）。

    单元素或空输入直接串行执行（避免线程池启动开销）。
    """
    materialized = list(items)
    if len(materialized) <= 1:
        return [fn(item) for item in materialized]
    workers = max_workers or default_workers()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, item) for item in materialized]
        results: List[Any] = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - 异常按原位写回，由调用方决定处理
                results.append(exc)
        return results
