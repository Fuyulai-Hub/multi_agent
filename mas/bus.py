"""Agent 消息总线：让所有 Agent 相互通信并感知他人已完成的工作。

- 直接消息（direct）/ 广播（broadcast）/ 请求-应答（request/response）
- 所有消息落盘 messages.jsonl，并同步写入事件日志，成为可审计证据
- `digest(role)` 生成“通讯摘要”，注入 Prompt 的感知段落

消息队列（MQ）通道（可选，默认关闭；配置见 runtime.mq）：
- 每条消息在落盘后发布到 Redis Stream ``mas:mq:agent:<run_id>``；
- 每个角色独立消费组 ``role-<role>``，`pump(role)` 从 broker 拉取消息并 ACK，
  `digest(role)` 读取该角色邮箱（真正经 broker 投递，而不是只读进程内 list）；
- 广播借助“每角色一个消费组”实现扇出；请求-应答用 correlation_id 关联；
- broker 不可用时自动退回原有进程内 list 语义（证据文件与 Prompt 行为不变）。
"""

from __future__ import annotations

import json
import re
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
    """带持久化的消息总线（进程内 list + 可选 Redis Stream 投递）。"""

    def __init__(
        self,
        path: Path,
        events: Optional[EventLog] = None,
        run_id: str = "",
        *,
        mq: Optional[Any] = None,
        mq_stream: Optional[str] = None,
    ) -> None:
        self.path = Path(path)
        self.events = events
        self.run_id = run_id
        self.mq = mq
        self.mq_stream = mq_stream or f"agent:{run_id}"
        self._messages: List[Message] = []
        self._counter = 0
        # MQ 消费侧状态：每个角色一个消费组，邮箱只保留最近 200 条，避免长运行无限增长
        self._mailboxes: Dict[str, List[Message]] = {}
        self._delivered: Dict[str, int] = {}
        self._mq_entry_ids: Dict[str, str] = {}
        self._mq_published = 0
        self._mq_errors = 0
        self._mq_warned = False
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
        self._publish_mq(message, node=node, iteration=iteration)
        return message

    # ---------------------------------------------------------------- MQ 生产
    def _publish_mq(self, message: Message, *, node: Optional[str], iteration: Optional[int]) -> None:
        """消息落盘后发布到 Redis Stream；失败只降级留痕，不打断发送方。"""
        if self.mq is None:
            return
        try:
            envelope = self.mq.publish(
                self.mq_stream,
                payload=message.payload,
                run_id=self.run_id,
                topic=message.topic,
                kind=message.kind,
                sender=message.sender,
                recipients=message.recipients,
                refs=message.refs,
                correlation_id=message.correlation_id,
                message_id=f"{self.run_id}:{message.id}" if self.run_id else message.id,
                headers={
                    "logical_id": message.id,
                    "node": node or "",
                    "iteration": iteration if iteration is not None else "",
                    "message_ts": message.ts,
                },
            )
            self._mq_published += 1
            self._mq_entry_ids[message.id] = str(envelope.get("_stream_id") or "")
        except Exception as exc:  # noqa: BLE001 - broker 故障不得打断主流程
            self._mq_errors += 1
            if not self._mq_warned and self.events is not None:
                self._mq_warned = True
                self.events.emit(
                    "mq_publish_failed", agent="message_bus", severity="warning",
                    payload={"stream": self.mq_stream, "error": f"{type(exc).__name__}: {exc}"},
                )

    # ---------------------------------------------------------------- MQ 消费
    @staticmethod
    def _group_name(role: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(role or "unknown"))
        return f"role-{safe}"

    def pump(self, role: str, *, count: int = 200) -> int:
        """从该角色的消费组拉取新消息并 ACK，返回本次真正投递给该角色的条数。

        - 每个角色独立消费组 => 广播天然扇出；
        - 非本角色消息也会 ACK（消费组不能只推进部分 offset）；
        - 消费组从 0 开始建，因此新角色能看到建组前的历史消息。
        """
        if self.mq is None:
            return 0
        group = self._group_name(role)
        try:
            entries = self.mq.consume(
                self.mq_stream, group, consumer=self.run_id or "agent",
                count=count, block_ms=None,
            )
        except Exception as exc:  # noqa: BLE001 - 消费失败退回本地 list
            self._mq_errors += 1
            if not self._mq_warned and self.events is not None:
                self._mq_warned = True
                self.events.emit(
                    "mq_consume_failed", agent="message_bus", severity="warning",
                    payload={"stream": self.mq_stream, "role": role,
                             "error": f"{type(exc).__name__}: {exc}"},
                )
            return 0
        delivered = 0
        acknowledged: List[Dict[str, Any]] = []
        for envelope in entries:
            acknowledged.append(envelope)
            recipients = list(envelope.get("recipients") or [])
            if str(envelope.get("sender")) == role:
                continue
            if role not in recipients and "all" not in recipients:
                continue
            message = self._message_from_envelope(envelope)
            mailbox = self._mailboxes.setdefault(role, [])
            mailbox.append(message)
            if len(mailbox) > 200:
                del mailbox[: len(mailbox) - 200]
            delivered += 1
        if acknowledged:
            try:
                self.mq.ack(self.mq_stream, group, acknowledged)
            except Exception:  # noqa: BLE001 - 未 ACK 会在下次 pump 重投（at-least-once）
                self._mq_errors += 1
        self._delivered[role] = self._delivered.get(role, 0) + delivered
        return delivered

    def _message_from_envelope(self, envelope: Dict[str, Any]) -> Message:
        headers = envelope.get("headers") or {}
        logical_id = str(headers.get("logical_id") or envelope.get("id") or "")
        return Message(
            id=logical_id,
            ts=str(headers.get("message_ts") or envelope.get("ts") or _now()),
            sender=str(envelope.get("sender") or ""),
            recipients=list(envelope.get("recipients") or []),
            topic=str(envelope.get("topic") or ""),
            kind=str(envelope.get("kind") or "direct"),
            payload=dict(envelope.get("payload") or {}),
            refs=list(envelope.get("refs") or []),
            correlation_id=envelope.get("correlation_id") or None,
        )

    def mailbox(self, role: str) -> List[Message]:
        """该角色经 broker 投递的邮箱（不含尚未 pump 的消息）。"""
        return list(self._mailboxes.get(role, []))

    def mq_entry_id(self, message_id: str) -> Optional[str]:
        return self._mq_entry_ids.get(message_id)

    def transport_stats(self) -> Dict[str, Any]:
        """MQ 传输指标：发布数、各角色投递数、消费组 pending、错误数。"""
        stats: Dict[str, Any] = {
            "enabled": self.mq is not None,
            "stream": self.mq_stream,
            "published": self._mq_published,
            "delivered": dict(self._delivered),
            "errors": self._mq_errors,
        }
        if self.mq is None:
            return stats
        groups: Dict[str, Any] = {}
        for role, messages in self._mailboxes.items():
            group = self._group_name(role)
            try:
                group_stats = self.mq.stats(self.mq_stream, group)
                groups[role] = {
                    "group": group,
                    "delivered": len(messages),
                    "pending": (group_stats.get("group") or {}).get("pending", 0),
                }
            except Exception:  # noqa: BLE001
                groups[role] = {"group": group, "delivered": len(messages)}
        stats["groups"] = groups
        return stats

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
        """把与该角色相关的消息压缩成“已发生的工作进展”摘要。

        MQ 开启时先经消费组拉取新消息（真实 broker 投递）；拉不到或 broker 故障时
        回退进程内 list，保证 Prompt 感知段不因 MQ 抖动而变空。
        """
        messages: Optional[List[Message]] = None
        if self.mq is not None:
            delivered = self.pump(role)
            messages = self._mailboxes.get(role)
            if delivered and self.events is not None:
                self.events.emit(
                    "mq_digest_delivered", agent="message_bus",
                    payload={"role": role, "delivered": delivered,
                             "mailbox": len(messages or []),
                             "group": self._group_name(role)},
                )
        if not messages:
            messages = self.inbox(role)
        lines = [message.brief() for message in messages][-limit:]
        text = "\n".join(lines) if lines else "(暂无需要感知的协作消息)"
        return text[:max_chars]

    def stats(self) -> Dict[str, Any]:
        by_topic: Dict[str, int] = {}
        for message in self._messages:
            by_topic[message.topic] = by_topic.get(message.topic, 0) + 1
        return {"total": len(self._messages), "by_topic": by_topic}
