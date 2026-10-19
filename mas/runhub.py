"""运行注册表：跨进程的运行状态/取消协调（docs/25 协调层）。

两个后端，同一接口：
- ``MemoryRunRegistry`` —— 进程内实现。可传入外部 dict 作为事件存储
  （前端 ``RUNNING_TASKS`` 即以此共享，保持既有测试与行为逐字节兼容）；
- ``RedisRunRegistry`` —— 跨进程实现：``mas:run:<id>`` 心跳键（TTL）+ 取消键，
  让 CLI / 前端 / 其他 worker 发起的运行互相可见、可取消。

``build_registry`` 永不返回 None：Redis 不可用时退回内存后端（零外部依赖，
单进程场景与改造前完全一致）。

生命周期契约（Leader 与前端共同遵守）：
- ``register``   运行开始（幂等：重复注册返回既有取消信号，不重置）；
- ``heartbeat``  每个节点边界刷新（Redis 后端靠 TTL 判活，替代 mtime 猜测）；
- ``unregister`` 运行结束（正常/异常/取消都要走到；幂等）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

__all__ = ["MemoryRunRegistry", "RedisRunRegistry", "build_registry"]


class MemoryRunRegistry:
    """进程内注册表：事件可外接共享 dict（前端 RUNNING_TASKS），元数据自带。"""

    def __init__(self, events_map: Optional[Dict[str, threading.Event]] = None) -> None:
        self._events: Dict[str, threading.Event] = events_map if events_map is not None else {}
        self._meta: Dict[str, Dict[str, Any]] = {}

    def register(self, run_id: str, meta: Optional[Dict[str, Any]] = None,
                 ttl_s: Optional[int] = None) -> Optional[threading.Event]:
        # ttl_s 仅为与 RedisRunRegistry 保持同一签名；内存版没有过期语义。
        if run_id not in self._events:
            self._events[run_id] = threading.Event()
        entry = self._meta.setdefault(run_id, {})
        entry.update({"status": "running", "node": None, "updated_at": time.time()})
        entry.update(meta or {})
        return self._events[run_id]

    def heartbeat(self, run_id: str, status: str = "running", node: Optional[str] = None) -> None:
        if run_id not in self._events:
            self.register(run_id)
        self._meta[run_id].update({"status": status, "node": node, "updated_at": time.time()})

    def unregister(self, run_id: str) -> None:
        self._events.pop(run_id, None)
        self._meta.pop(run_id, None)

    def request_cancel(self, run_id: str) -> bool:
        event = self._events.get(run_id)
        if event is None:
            return False
        event.set()
        self._meta.setdefault(run_id, {})["cancel_requested"] = True
        return True

    def is_canceled(self, run_id: str) -> bool:
        event = self._events.get(run_id)
        return bool(event is not None and event.is_set())

    def clear_cancel(self, run_id: str) -> None:
        event = self._events.get(run_id)
        if event is not None:
            event.clear()

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        if run_id not in self._events:
            return None
        return {"run_id": run_id, **self._meta.get(run_id, {})}

    def is_running(self, run_id: str) -> bool:
        return run_id in self._events

    def list_running(self) -> List[Dict[str, Any]]:
        return [self.get(run_id) for run_id in list(self._events)]


class RedisRunRegistry:
    """跨进程注册表：心跳键 TTL 判活 + 取消键；接口与内存版一致。"""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def register(self, run_id: str, meta: Optional[Dict[str, Any]] = None,
                 ttl_s: Optional[int] = None) -> None:
        self.backend.register(run_id, meta, ttl_s=ttl_s)

    def heartbeat(self, run_id: str, status: str = "running", node: Optional[str] = None) -> None:
        self.backend.heartbeat(run_id, status=status, node=node)

    def unregister(self, run_id: str) -> None:
        self.backend.unregister(run_id)

    def request_cancel(self, run_id: str) -> bool:
        return bool(self.backend.request_cancel(run_id))

    def is_canceled(self, run_id: str) -> bool:
        return bool(self.backend.is_canceled(run_id))

    def clear_cancel(self, run_id: str) -> None:
        self.backend.clear_cancel(run_id)

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.backend.get(run_id)

    def is_running(self, run_id: str) -> bool:
        return bool(self.backend.is_running(run_id))

    def list_running(self) -> List[Dict[str, Any]]:
        return list(self.backend.list_running())


def build_registry(config: Any, config_dir: Optional[Any] = None) -> Any:
    """按配置构建注册表：Redis 可用则跨进程，否则进程内（永不返回 None）。"""
    try:
        from .store import build_redis_backend
        backend = build_redis_backend(config, config_dir)
    except Exception:  # noqa: BLE001 - 协调层不可用不该影响主流程
        backend = None
    if backend is not None:
        return RedisRunRegistry(backend)
    return MemoryRunRegistry()


def default_cancel_checker(registry: Any, run_id: str) -> Callable[[], bool]:
    """供 Leader 在节点边界消费的取消判定闭包（registry 为 None 时恒 False）。"""
    if registry is None:
        return lambda: False
    return lambda: bool(registry.is_canceled(run_id))
