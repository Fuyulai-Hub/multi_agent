"""事件日志：全流程事实的唯一来源。

所有 Agent 动作、产物发布、门禁判定、回退、缺陷、经验沉淀都会写入 events.jsonl，
后续的“经验提炼”和“证据统计”都只依赖这份日志，保证可追溯。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

__all__ = ["Event", "EventLog"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Event:
    """一条事件记录。"""

    seq: int
    id: str
    ts: str
    run_id: str
    type: str
    agent: Optional[str] = None
    node: Optional[str] = None
    iteration: Optional[int] = None
    severity: str = "info"
    payload: Dict[str, Any] = field(default_factory=dict)
    refs: List[str] = field(default_factory=list)
    parent: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EventLog:
    """JSONL 事件日志，支持订阅回调。

    ``resume=True`` 用于断点续跑：从已有 ``events.jsonl`` 恢复事件与序号，
    保证新事件不重号（MySQL 的 (run_id, seq) 唯一键依赖该单调性）。
    尾部半行（进程被强杀时可能留下）会被跳过，不让一条残行阻断恢复。
    """

    def __init__(self, path: Path, run_id: str, echo: bool = False,
                 resume: bool = False) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.echo = echo
        self._events: List[Event] = []
        self._seq = 0
        self._subscribers: List[Callable[[Event], None]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if resume:
            self.load()

    # ---------------------------------------------------------------- 写入
    def emit(
        self,
        type_: str,
        *,
        agent: Optional[str] = None,
        node: Optional[str] = None,
        iteration: Optional[int] = None,
        severity: str = "info",
        payload: Optional[Dict[str, Any]] = None,
        refs: Optional[List[str]] = None,
        parent: Optional[str] = None,
    ) -> Event:
        self._seq += 1
        event = Event(
            seq=self._seq,
            id=f"evt-{self._seq:05d}",
            ts=_now(),
            run_id=self.run_id,
            type=type_,
            agent=agent,
            node=node,
            iteration=iteration,
            severity=severity,
            payload=payload or {},
            refs=list(refs or []),
            parent=parent,
        )
        self._events.append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
        if self.echo:
            print(f"[{event.ts}] #{event.seq} {event.type:<24} {event.agent or '-'}"
                  f"{f'@{event.node}' if event.node else ''}")
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:  # noqa: BLE001 - 订阅者异常不得影响主流程
                pass
        return event

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self._subscribers.append(callback)

    # ---------------------------------------------------------------- 读取
    def read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return [event.to_dict() for event in self._events]
        records: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # 强杀可能留下半行；跳过残行而不是让断点恢复失败
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def load(self) -> List[Event]:
        """把磁盘上的历史日志载入内存（供复盘/沉淀等只读场景复用 stats/filter/timeline）。"""
        self._events = []
        self._seq = 0
        for record in self.read():
            payload = dict(record or {})
            seq = int(record.get("seq") or self._seq + 1)
            self._seq = max(self._seq, seq)
            self._events.append(Event(
                seq=seq,
                id=str(record.get("id", f"evt-{seq:05d}")),
                ts=str(record.get("ts", "")),
                run_id=str(record.get("run_id", self.run_id)),
                type=str(record.get("type", "")),
                agent=record.get("agent"),
                node=record.get("node"),
                iteration=record.get("iteration"),
                severity=str(record.get("severity", "info")),
                payload=dict(payload.get("payload") or {}),
                refs=list(record.get("refs") or []),
                parent=record.get("parent"),
            ))
        return self.events

    @property
    def events(self) -> List[Event]:
        return list(self._events)

    def filter(self, types: Optional[List[str]] = None, agent: Optional[str] = None) -> List[Event]:
        result = self._events
        if types:
            wanted = set(types)
            result = [event for event in result if event.type in wanted]
        if agent:
            result = [event for event in result if event.agent == agent]
        return result

    def count(self, type_: str) -> int:
        return sum(1 for event in self._events if event.type == type_)

    def count_where(self, type_: str, key: str, value: Any) -> int:
        return sum(
            1
            for event in self._events
            if event.type == type_ and event.payload.get(key) == value
        )

    def timeline(self, limit: int = 80) -> List[str]:
        lines: List[str] = []
        for event in self._events[:limit]:
            label = event.payload.get("title") or event.payload.get("summary") or event.payload.get("reason") or ""
            lines.append(
                f"{event.ts} | {event.agent or 'leader':<10} | {event.type:<22} | {str(label)[:90]}"
            )
        return lines

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for event in self._events:
            by_type[event.type] = by_type.get(event.type, 0) + 1
        return {
            "total": len(self._events),
            "by_type": by_type,
            "warnings": sum(1 for event in self._events if event.severity == "warning"),
            "errors": sum(1 for event in self._events if event.severity == "error"),
        }
