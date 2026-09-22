"""Agent 消息总线：让所有 Agent 相互通信并感知他人已完成的工作。

- 直接消息（direct）/ 广播（broadcast）/ 请求-应答（request/response）
- 所有消息落盘 messages.jsonl，并同步写入事件日志，成为可审计证据
- `digest(role)` 生成“通讯摘要”，注入 Prompt 的感知段落
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .events import EventLog

__all__ = ["Message", "MessageBus"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Message:
    id: str
    ts: str
    sender: str
    recipients: List[str]
    topic: str
    kind: str = "direct"                 # direct | broadcast | request | response | system
    payload: Dict[str, Any] = field(default_factory=dict)
    refs: List[str] = field(default_factory=list)
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def brief(self) -> str:
        recipients = "all" if "all" in self.recipients else ",".join(self.recipients)
        body = json.dumps(self.payload, ensure_ascii=False, default=str)
        if len(body) > 160:
            body = body[:160] + "..."
        return f"[{self.ts[11:19]}] {self.sender} → {recipients} | {self.topic}: {body}"


class MessageBus:
    """带持久化的内存消息总线。"""

    def __init__(self, path: Path, events: Optional[EventLog] = None, run_id: str = "") -> None:
        self.path = Path(path)
        self.events = events
        self.run_id = run_id
        self._messages: List[Message] = []
        self._counter = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 发送
    def send(
        self,
        sender: str,
        topic: str,
        payload: Optional[Dict[str, Any]] = None,
        recipients: Optional[List[str]] = None,
        *,
        kind: str = "direct",
        refs: Optional[List[str]] = None,
        correlation_id: Optional[str] = None,
        node: Optional[str] = None,
        iteration: Optional[int] = None,
    ) -> Message:
        self._counter += 1
        message = Message(
            id=f"msg-{self._counter:04d}",
            ts=_now(),
            sender=sender,
            recipients=list(recipients or ["all"]),
            topic=topic,
            kind=kind,
            payload=payload or {},
            refs=list(refs or []),
            correlation_id=correlation_id,
        )
        self._messages.append(message)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.to_dict(), ensure_ascii=False, default=str) + "\n")
        if self.events is not None:
            self.events.emit(
                "message",
                agent=sender,
                node=node,
                iteration=iteration,
                payload={
                    "message_id": message.id,
                    "topic": topic,
                    "kind": kind,
                    "recipients": message.recipients,
                    "summary": payload.get("summary", "") if isinstance(payload, dict) else "",
                    "correlation_id": correlation_id,
                },
                refs=refs or [],
            )
        return message

    def broadcast(
        self,
        sender: str,
        topic: str,
        payload: Optional[Dict[str, Any]] = None,
        recipients: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Message:
        return self.send(
            sender,
            topic,
            payload,
            recipients or ["all"],
            kind="broadcast",
            **kwargs,
        )

    # ---------------------------------------------------------------- 读取
    def history(self) -> List[Message]:
        return list(self._messages)

    def inbox(self, role: str, *, limit: Optional[int] = None, topics: Optional[List[str]] = None) -> List[Message]:
        wanted = set(topics or [])
        messages = [
            message
            for message in self._messages
            if (role in message.recipients or "all" in message.recipients)
            and message.sender != role
            and (not wanted or message.topic in wanted)
        ]
        return messages[-limit:] if limit else messages

    def unread(self, role: str, since_id: Optional[str]) -> List[Message]:
        all_messages = self.inbox(role)
        if not since_id:
            return all_messages
        ids = [message.id for message in all_messages]
        if since_id in ids:
            return all_messages[ids.index(since_id) + 1 :]
        return all_messages

    def digest(self, role: str, limit: int = 10, max_chars: int = 1600) -> str:
        """把与该角色相关的消息压缩成“已发生的工作进展”摘要。"""
        lines = [message.brief() for message in self.inbox(role)][-limit:]
        text = "\n".join(lines) if lines else "(暂无需要感知的协作消息)"
        return text[:max_chars]

    def stats(self) -> Dict[str, Any]:
        by_topic: Dict[str, int] = {}
        for message in self._messages:
            by_topic[message.topic] = by_topic.get(message.topic, 0) + 1
        return {"total": len(self._messages), "by_topic": by_topic}
