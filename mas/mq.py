"""Redis Streams 消息队列：让 Agent 协作与运行生命周期真正跑在 broker 上。

设计边界（与 docs/26 的差异及理由）：
- docs/26 的结论是「当前规模不需要 MQ」。本模块不是把 ``MessageBus`` 的同步
  ``send()`` 换成异步投递，而是在其**旁边**增加一条真实 MQ 传输：消息仍然同步
  落盘 messages.jsonl（证据 SSOT 不变），同时发布到 Redis Stream；每个角色用
  独立消费组消费自己收件箱，请求-应答通过 correlation_id 关联。
- 运行任务从 API 进程搬到独立 worker：``RunTaskQueue`` + 消费组 + ACK + PEL
  认领 + 延迟重试 + 死信，解决「长任务被 HTTP 进程生命周期绑死」的真实问题。

可靠性语义：at-least-once。消费失败按指数退避延迟重投；达到上限进 DLQ；
worker 崩溃时消息留在 PEL，空闲超过 visibility timeout 后被其他消费者认领。
业务侧用 run_summary.json + 运行目录归档做幂等与去重。

降级契约与 mas.store 一致：MQ 不可用返回 None，调用方走既有 jsonl/内存路径，
绝不因 broker 故障打断流水线。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "MQUnavailable",
    "StreamMQ",
    "WorkerRegistry",
    "RunTaskQueue",
    "DEFAULTS",
    "resolve_mq_settings",
    "build_stream_mq",
    "build_agent_bus_mq",
    "build_run_queue",
    "enqueue_run_task",
    "mq_status",
]

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "mode": "auto",                    # auto | queue | thread
    "stream_prefix": "mas:mq",
    "consumer_group": "mas-workers",
    "max_attempts": 3,
    "visibility_timeout_ms": 600000,   # 10 分钟：worker 崩溃后的 PEL 认领门槛
    "retry_backoff_s": [5, 30, 120],
    "dedup_ttl_s": 86400,
    "stream_maxlen": 20000,
    "stream_ttl_s": 604800,            # 7 天：agent/event 流按 run 隔离，过期自动回收
    "queue_ttl_s": 3600,               # 排队中的运行注册键 TTL（长于心跳 90s）
    "lease_ttl_s": 300,                # 运行租约 TTL：worker 崩溃后最多 5 分钟可被接管
    "lease_renew_s": 30,               # 租约续租/运行心跳间隔（须小于 heartbeat_ttl 90s）
    "poll_ms": 1000,
    "agent_bus": {"enabled": True},
}


class MQUnavailable(RuntimeError):
    """MQ 未启用/连不上（调用方应降级到进程内路径）。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_loads(raw: str, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _worker_identity() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- 配置
def resolve_mq_settings(config: Any, config_dir: Optional[Any] = None) -> Dict[str, Any]:
    """合并 runtime.mq 与 Redis 连接配置（连接参数复用 store.redis 三通道）。"""
    raw = dict((getattr(config, "runtime", {}) or {}).get("mq") or {})
    settings = dict(DEFAULTS)
    settings.update(raw)
    settings["agent_bus"] = {**DEFAULTS["agent_bus"], **(raw.get("agent_bus") or {})}
    try:
        from .store import resolve_store_settings
        store = resolve_store_settings(config, config_dir)
        settings["enabled"] = bool(settings.get("enabled")) and bool(store.get("enabled")) \
            and bool(store.get("redis", {}).get("enabled", True))
        settings["redis_url"] = store["redis"].get("url")
        settings["workspace_bound"] = True
    except Exception:  # noqa: BLE001 - 配置解析失败按未启用处理
        settings["enabled"] = False
        settings["redis_url"] = None
        settings["workspace_bound"] = False
    return settings


def build_stream_mq(config: Any, config_dir: Optional[Any] = None,
                    settings: Optional[Dict[str, Any]] = None) -> Optional["StreamMQ"]:
    """构建 StreamMQ；未启用/Redis 不可用/非规范工作区时返回 None。"""
    settings = settings or resolve_mq_settings(config, config_dir)
    if not settings.get("enabled"):
        return None
    try:
        from .store import build_redis_backend
        backend = build_redis_backend(config, config_dir)
    except Exception:  # noqa: BLE001 - 软依赖降级
        return None
    if backend is None:
        return None
    return StreamMQ(
        backend,
        prefix=str(settings.get("stream_prefix") or "mas:mq"),
        maxlen=int(settings.get("stream_maxlen") or 20000),
        stream_ttl_s=int(settings.get("stream_ttl_s") or 604800),
        dedup_ttl_s=int(settings.get("dedup_ttl_s") or 86400),
    )


def build_agent_bus_mq(config: Any, config_dir: Optional[Any] = None) -> Optional["StreamMQ"]:
    settings = resolve_mq_settings(config, config_dir)
    if not settings.get("agent_bus", {}).get("enabled", True):
        return None
    return build_stream_mq(config, config_dir, settings=settings)


def build_run_queue(config: Any, config_dir: Optional[Any] = None,
                    settings: Optional[Dict[str, Any]] = None) -> Optional["RunTaskQueue"]:
    settings = settings or resolve_mq_settings(config, config_dir)
    mq = build_stream_mq(config, config_dir, settings=settings)
    if mq is None:
        return None
    return RunTaskQueue(
        mq,
        group=str(settings.get("consumer_group") or "mas-workers"),
        max_attempts=int(settings.get("max_attempts") or 3),
        visibility_timeout_ms=int(settings.get("visibility_timeout_ms") or 600000),
        retry_backoff_s=list(settings.get("retry_backoff_s") or [5, 30, 120]),
        queue_ttl_s=int(settings.get("queue_ttl_s") or 3600),
        poll_ms=int(settings.get("poll_ms") or 1000),
    )


# ---------------------------------------------------------------- Stream 核心
class StreamMQ:
    """Redis Stream 生产/消费原语（消费组、ACK、PEL 认领、延迟重试、DLQ、幂等）。"""

    def __init__(self, backend: Any, *, prefix: str = "mas:mq", maxlen: int = 20000,
                 stream_ttl_s: int = 604800, dedup_ttl_s: int = 86400) -> None:
        self.backend = backend
        self.client = backend.client
        self.prefix = prefix.rstrip(":")
        self.maxlen = int(maxlen)
        self.stream_ttl_s = int(stream_ttl_s)
        self.dedup_ttl_s = int(dedup_ttl_s)
        self._expiring: set = set()
        self._expire_lock = threading.Lock()

    # ---- keys
    def stream_key(self, name: str) -> str:
        return f"{self.prefix}:{name}"

    def retry_key(self, name: str) -> str:
        return f"{self.prefix}:retry:{name}"

    def dlq_key(self, name: str) -> str:
        return f"{self.prefix}:dlq:{name}"

    def dedup_key(self, key: str) -> str:
        return f"{self.prefix}:dedup:{key}"

    def worker_key(self, worker_id: str) -> str:
        return f"{self.prefix}:worker:{worker_id}"

    def workers_key(self) -> str:
        return f"{self.prefix}:workers"

    def secret_key(self, run_id: str) -> str:
        return f"{self.prefix}:secret:{run_id}"

    # ---- 生产
    def publish(
        self,
        stream: str,
        *,
        payload: Dict[str, Any],
        run_id: str = "",
        topic: str = "",
        kind: str = "event",
        sender: str = "system",
        recipients: Optional[List[str]] = None,
        refs: Optional[List[str]] = None,
        correlation_id: Optional[str] = None,
        message_id: Optional[str] = None,
        attempt: int = 1,
        headers: Optional[Dict[str, Any]] = None,
        dedup_key: Optional[str] = None,
        ttl_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发布一条消息；``dedup_key`` 存在时同键只入队一次（幂等生产者）。"""
        envelope = {
            "id": message_id or uuid.uuid4().hex,
            "ts": _now_iso(),
            "run_id": run_id,
            "topic": topic,
            "kind": kind,
            "sender": sender,
            "recipients": list(recipients or []),
            "payload": payload or {},
            "refs": list(refs or []),
            "correlation_id": correlation_id,
            "attempt": int(attempt),
            "headers": dict(headers or {}),
        }
        if dedup_key:
            key = self.dedup_key(dedup_key)
            existing = self.client.get(key)
            if existing:
                return {"duplicate": True, "id": existing, "stream": self.stream_key(stream)}
            if not self.client.set_nx_ex(key, str(envelope["id"]), int(ttl_s or self.dedup_ttl_s)):
                existing = self.client.get(key)
                return {"duplicate": True, "id": existing, "stream": self.stream_key(stream)}
        entry_id = self._xadd(self.stream_key(stream), envelope)
        envelope["stream"] = self.stream_key(stream)
        envelope["_stream"] = envelope["stream"]
        envelope["_stream_id"] = entry_id
        envelope["duplicate"] = False
        return envelope

    def _xadd(self, key: str, envelope: Dict[str, Any]) -> str:
        entry_id = self.client.xadd(key, self._encode_fields(envelope), maxlen=self.maxlen)
        self._ensure_expiry(key)
        return entry_id

    def _ensure_expiry(self, key: str) -> None:
        if self.stream_ttl_s <= 0:
            return
        with self._expire_lock:
            if key in self._expiring:
                return
            self._expiring.add(key)
        try:
            self.client.expire(key, self.stream_ttl_s)
        except Exception:  # noqa: BLE001 - TTL 只影响回收，不影响消息
            with self._expire_lock:
                self._expiring.discard(key)

    @staticmethod
    def _encode_fields(envelope: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": envelope.get("id", ""),
            "ts": envelope.get("ts", ""),
            "run_id": envelope.get("run_id", ""),
            "topic": envelope.get("topic", ""),
            "kind": envelope.get("kind", ""),
            "sender": envelope.get("sender", ""),
            "recipients": json.dumps(envelope.get("recipients") or [], ensure_ascii=False),
            "payload": json.dumps(envelope.get("payload") or {}, ensure_ascii=False, default=str),
            "refs": json.dumps(envelope.get("refs") or [], ensure_ascii=False),
            "correlation_id": envelope.get("correlation_id") or "",
            "attempt": str(int(envelope.get("attempt") or 1)),
            "headers": json.dumps(envelope.get("headers") or {}, ensure_ascii=False, default=str),
        }

    @staticmethod
    def _decode(entry_id: str, fields: Dict[str, str]) -> Dict[str, Any]:
        return {
            "id": fields.get("id") or entry_id,
            "ts": fields.get("ts", ""),
            "run_id": fields.get("run_id", ""),
            "topic": fields.get("topic", ""),
            "kind": fields.get("kind", ""),
            "sender": fields.get("sender", ""),
            "recipients": _json_loads(fields.get("recipients"), []),
            "payload": _json_loads(fields.get("payload"), {}),
            "refs": _json_loads(fields.get("refs"), []),
            "correlation_id": fields.get("correlation_id") or None,
            "attempt": int(fields.get("attempt") or 1),
            "headers": _json_loads(fields.get("headers"), {}),
            "_stream_id": entry_id,
            "_delivery_count": 1,
        }

    # ---- 消费
    def ensure_group(self, stream: str, group: str, start: str = "0") -> bool:
        """建消费组；start=0 使新组能读到建组前的历史（Agent 收件箱语义）。"""
        return self.client.xgroup_create(self.stream_key(stream), group, start, mkstream=True)

    def consume(self, stream: str, group: str, consumer: str, *,
                count: int = 1, block_ms: Optional[int] = None,
                claim_idle_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        """消费新消息 + （可选）认领空闲过久的 PEL 消息。"""
        self.ensure_group(stream, group)
        items: List[Dict[str, Any]] = []
        if claim_idle_ms is not None:
            items.extend(self.claim_stale(stream, group, consumer, claim_idle_ms, count=count))
        reply = self.client.xreadgroup(
            group, consumer, {self.stream_key(stream): ">"}, count=count, block_ms=block_ms,
        )
        for _stream_key, entries in reply:
            for entry_id, fields in entries:
                items.append(self._decode(entry_id, fields))
        return items

    def claim_stale(self, stream: str, group: str, consumer: str,
                    min_idle_ms: int, *, count: int = 10) -> List[Dict[str, Any]]:
        """XAUTOCLAIM：接管崩溃消费者遗留的 PEL 消息，并带上真实投递次数。"""
        key = self.stream_key(stream)
        cursor = "0-0"
        claimed: List[Dict[str, Any]] = []
        for _ in range(4):
            try:
                cursor, entries, _deleted = self.client.xautoclaim(
                    key, group, consumer, int(min_idle_ms), cursor, count=count,
                )
            except Exception:  # noqa: BLE001 - Redis < 6.2 或瞬时错误：退化为只读新消息
                break
            for entry_id, fields in entries:
                envelope = self._decode(entry_id, fields)
                try:
                    detail = self.client.xpending_detail(key, group, entry_id)
                    if detail:
                        envelope["_delivery_count"] = int(detail["delivery_count"])
                except Exception:  # noqa: BLE001
                    pass
                envelope["_claimed"] = True
                claimed.append(envelope)
            if cursor == "0-0" or not entries:
                break
        return claimed

    def cleanup_consumer(self, stream: str, group: str, consumer: str) -> bool:
        """移除已退出的消费者（PEL 非空时拒绝，保证未 ACK 消息仍可被认领）。"""
        key = self.stream_key(stream)
        try:
            summary = self.client.xpending(key, group)
        except Exception:  # noqa: BLE001 - 组不存在/瞬时错误
            return False
        for item in summary.get("consumers") or []:
            if str(item.get("consumer")) == consumer and int(item.get("count") or 0) > 0:
                return False
        try:
            self.client.xgroup_delconsumer(key, group, consumer)
            return True
        except Exception:  # noqa: BLE001
            return False

    def ack(self, stream: str, group: str, deliveries: Iterable[Any]) -> int:
        ids = [
            item["_stream_id"] if isinstance(item, dict) else str(item)
            for item in deliveries
        ]
        ids = [item for item in ids if item]
        if not ids:
            return 0
        return self.client.xack(self.stream_key(stream), group, *ids)

    # ---- 重试 / 死信
    def schedule_retry(self, stream: str, envelope: Dict[str, Any], delay_s: float) -> Dict[str, Any]:
        """把失败消息放进 ZSET 延迟队列（score=可见时间），原消息由调用方 ACK。"""
        retry_envelope = self._clean_envelope(envelope)
        retry_envelope["attempt"] = int(envelope.get("attempt") or 1) + 1
        retry_envelope["headers"] = {
            **(retry_envelope.get("headers") or {}),
            "retry_token": uuid.uuid4().hex,
            "scheduled_at": _now_iso(),
        }
        member = json.dumps(retry_envelope, ensure_ascii=False, default=str, sort_keys=True)
        self.client.zadd(self.retry_key(stream), {member: time.time() + max(0.0, float(delay_s))})
        return retry_envelope

    def promote_due_retries(self, stream: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        """把到期的延迟重试消息写回主 Stream（先 XADD 再 ZREM，at-least-once）。"""
        key = self.retry_key(stream)
        members = self.client.zrangebyscore(key, "-inf", time.time(), start=0, count=limit)
        promoted: List[Dict[str, Any]] = []
        for member in members:
            try:
                envelope = json.loads(member)
            except ValueError:
                self.client.zrem(key, member)
                continue
            entry_id = self._xadd(self.stream_key(stream), self._clean_envelope(envelope))
            self.client.zrem(key, member)
            envelope["_stream"] = self.stream_key(stream)
            envelope["_stream_id"] = entry_id
            promoted.append(envelope)
        return promoted

    def publish_dead_letter(self, stream: str, envelope: Dict[str, Any],
                            error: str, attempts: int) -> Dict[str, Any]:
        payload = {
            "original": self._clean_envelope(envelope),
            "error": str(error)[:4000],
            "attempts": int(attempts),
            "failed_at": _now_iso(),
        }
        dlq_envelope = {
            "id": f"{envelope.get('id') or uuid.uuid4().hex}:dlq",
            "ts": _now_iso(),
            "run_id": envelope.get("run_id", ""),
            "topic": envelope.get("topic", ""),
            "kind": "dead_letter",
            "sender": "mq",
            "recipients": [],
            "payload": payload,
            "refs": [],
            "correlation_id": envelope.get("correlation_id"),
            "attempt": int(attempts),
            "headers": {"source_stream": stream},
        }
        entry_id = self._xadd(self.dlq_key(stream), dlq_envelope)
        dlq_envelope["_stream_id"] = entry_id
        return dlq_envelope

    def dlq_entries(self, stream: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        entries = self.client.xrange(self.dlq_key(stream), "-", "+", count=int(limit))
        return [self._decode(entry_id, fields) for entry_id, fields in entries][::-1]

    @staticmethod
    def _clean_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in envelope.items() if not key.startswith("_")}

    # ---- 一次性密钥（避免 API Key 明文进入共享队列）
    def store_secret(self, run_id: str, value: str, ttl_s: int = 3600) -> None:
        self.client.set(self.secret_key(run_id), str(value), ex=int(ttl_s))

    def take_secret(self, run_id: str) -> Optional[str]:
        try:
            return self.client.getdel(self.secret_key(run_id))
        except Exception:  # noqa: BLE001 - 旧版 Redis 无 GETDEL 时退化为 GET+DEL
            value = self.client.get(self.secret_key(run_id))
            if value is not None:
                self.client.delete(self.secret_key(run_id))
            return value

    def read_secret(self, run_id: str) -> Optional[str]:
        """读取运行密钥但保留（worker 崩溃重试时仍需同一把 Key）。"""
        return self.client.get(self.secret_key(run_id))

    def delete_secret(self, run_id: str) -> None:
        self.client.delete(self.secret_key(run_id))

    # ---- 观测
    def stats(self, stream: str, group: Optional[str] = None) -> Dict[str, Any]:
        key = self.stream_key(stream)
        report: Dict[str, Any] = {"stream": key, "length": 0, "groups": [], "pending": None}
        try:
            report["length"] = self.client.xlen(key)
        except Exception:  # noqa: BLE001
            return report
        try:
            report["groups"] = self.client.xinfo_groups(key)
        except Exception:  # noqa: BLE001 - key 不存在
            report["groups"] = []
        if group:
            try:
                groups = {str(item.get("name")): item for item in report["groups"]}
                if group in groups:
                    report["group"] = {
                        "name": group,
                        "consumers": int(groups[group].get("consumers") or 0),
                        "pending": int(groups[group].get("pending") or 0),
                        "last_delivered_id": groups[group].get("last-delivered-id"),
                    }
                    report["pending"] = self.client.xpending(key, group)
                else:
                    report["group"] = {"name": group, "exists": False}
            except Exception:  # noqa: BLE001
                pass
        return report

    def reset_run_streams(self, run_id: str) -> int:
        """重试前清理该运行的 Agent/事件流（历史消息已在运行目录归档）。"""
        return int(self.client.delete(
            self.stream_key(f"agent:{run_id}"), self.stream_key(f"events:{run_id}")
        ) or 0)


# ---------------------------------------------------------------- Worker 注册表
class WorkerRegistry:
    """worker 存活注册：心跳键 TTL + 集合索引，供 API 判断是否需要内嵌 worker。"""

    def __init__(self, mq: StreamMQ, *, ttl_s: int = 30) -> None:
        self.mq = mq
        self.ttl_s = int(ttl_s)

    def register(self, worker_id: str, **meta: Any) -> None:
        payload = {"worker_id": worker_id, "status": "starting", "updated_at": time.time(), **meta}
        self.mq.client.set(self.mq.worker_key(worker_id),
                           json.dumps(payload, ensure_ascii=False), ex=self.ttl_s)
        self.mq.client.sadd(self.mq.workers_key(), worker_id)

    def heartbeat(self, worker_id: str, **meta: Any) -> None:
        payload = {"worker_id": worker_id, "status": "idle", "updated_at": time.time(), **meta}
        self.mq.client.set(self.mq.worker_key(worker_id),
                           json.dumps(payload, ensure_ascii=False), ex=self.ttl_s)
        self.mq.client.sadd(self.mq.workers_key(), worker_id)

    def unregister(self, worker_id: str) -> None:
        self.mq.client.delete(self.mq.worker_key(worker_id))
        self.mq.client.srem(self.mq.workers_key(), worker_id)

    def list(self) -> List[Dict[str, Any]]:
        workers: List[Dict[str, Any]] = []
        for worker_id in self.mq.client.smembers(self.mq.workers_key()):
            raw = self.mq.client.get(self.mq.worker_key(worker_id))
            if raw is None:
                self.mq.client.srem(self.mq.workers_key(), worker_id)
                continue
            try:
                workers.append(json.loads(raw))
            except ValueError:
                continue
        return workers


# ---------------------------------------------------------------- 运行任务队列
class RunTaskQueue:
    """运行任务队列：生产（API）与消费（worker）共享的领域封装。"""

    STREAM = "runs"
    EVENTS_STREAM = "worker-events"

    def __init__(
        self,
        mq: StreamMQ,
        *,
        group: str = "mas-workers",
        max_attempts: int = 3,
        visibility_timeout_ms: int = 600000,
        retry_backoff_s: Optional[List[float]] = None,
        queue_ttl_s: int = 3600,
        poll_ms: int = 1000,
    ) -> None:
        self.mq = mq
        self.group = group
        self.max_attempts = max(1, int(max_attempts))
        self.visibility_timeout_ms = int(visibility_timeout_ms)
        self.retry_backoff_s = [float(item) for item in (retry_backoff_s or [5, 30, 120])]
        self.queue_ttl_s = int(queue_ttl_s)
        self.poll_ms = int(poll_ms)

    # ---- 生产
    def enqueue(self, payload: Dict[str, Any], *, run_id: str,
                dedup_key: Optional[str] = None) -> Dict[str, Any]:
        return self.mq.publish(
            self.STREAM,
            payload=payload,
            run_id=run_id,
            topic="run_task",
            kind="task",
            sender=str(payload.get("source") or "api"),
            message_id=run_id,
            headers={"schema": "run_task.v1", "enqueued_at": _now_iso()},
            dedup_key=dedup_key or f"run:{run_id}",
            ttl_s=self.queue_ttl_s,
        )

    # ---- 消费
    def consume(self, consumer: str, *, count: int = 1,
                block_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.mq.consume(
            self.STREAM, self.group, consumer,
            count=count, block_ms=self.poll_ms if block_ms is None else block_ms,
            claim_idle_ms=self.visibility_timeout_ms,
        )

    def ack(self, delivery: Any) -> int:
        return self.mq.ack(self.STREAM, self.group, [delivery])

    def fail(self, delivery: Dict[str, Any], error: str) -> Dict[str, Any]:
        """失败处理：未达上限 → 延迟重试；达到上限 → 死信并 ACK 原消息。"""
        attempt = max(int(delivery.get("attempt") or 1), int(delivery.get("_delivery_count") or 1))
        if attempt >= self.max_attempts:
            return self.dead_letter(delivery, error, attempt)
        delay = self.retry_backoff_s[min(attempt - 1, len(self.retry_backoff_s) - 1)]
        retry = self.mq.schedule_retry(self.STREAM, delivery, delay)
        self.ack(delivery)
        return {"status": "retried", "attempt": attempt, "delay_s": delay, "retry": retry}

    def dead_letter(self, delivery: Dict[str, Any], error: str,
                    attempt: Optional[int] = None) -> Dict[str, Any]:
        """不可重试的失败（非法 payload/工作区不匹配）直接进死信并 ACK。"""
        attempt = int(attempt or delivery.get("attempt") or 1)
        dlq = self.mq.publish_dead_letter(self.STREAM, delivery, error, attempt)
        self.ack(delivery)
        return {"status": "dead_lettered", "attempt": attempt, "dlq": dlq}

    def promote_due_retries(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        return self.mq.promote_due_retries(self.STREAM, limit=limit)

    def publish_worker_event(self, event_type: str, *, run_id: str,
                             payload: Dict[str, Any], sender: str) -> None:
        try:
            self.mq.publish(
                self.EVENTS_STREAM, payload=payload, run_id=run_id, topic=event_type,
                kind="worker", sender=sender, message_id=f"{run_id}:{event_type}:{uuid.uuid4().hex[:8]}",
            )
        except Exception:  # noqa: BLE001 - 观测事件失败不影响任务
            pass

    # ---- 运维观测
    def stats(self) -> Dict[str, Any]:
        report = self.mq.stats(self.STREAM, self.group)
        try:
            report["dlq_length"] = self.mq.client.xlen(self.mq.dlq_key(self.STREAM))
        except Exception:  # noqa: BLE001
            report["dlq_length"] = 0
        try:
            report["scheduled_retries"] = self.mq.client.zcard(self.mq.retry_key(self.STREAM))
        except Exception:  # noqa: BLE001
            report["scheduled_retries"] = 0
        report["max_attempts"] = self.max_attempts
        report["visibility_timeout_ms"] = self.visibility_timeout_ms
        return report

    def register_queued_run(self, registry: Any, run_id: str) -> None:
        """排队即注册（取消按钮在 worker 领取前也生效）；TTL 覆盖排队时长。"""
        try:
            registry.register(run_id, {"status": "queued"}, ttl_s=self.queue_ttl_s)
        except TypeError:      # 兼容不支持 ttl_s 的自定义 registry
            registry.register(run_id, {"status": "queued"})


def enqueue_run_task(
    config: Any,
    payload: Dict[str, Any],
    *,
    run_id: str,
    api_key: Optional[str] = None,
    config_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """便捷入口：构建 MQ → 存一次性密钥 → 幂等入队。"""
    queue = build_run_queue(config, config_dir)
    if queue is None:
        raise MQUnavailable("MQ 未启用或 Redis 不可用")
    if api_key:
        queue.mq.store_secret(run_id, api_key, ttl_s=queue.queue_ttl_s)
    return queue.enqueue(payload, run_id=run_id)


def mq_status(config: Any, config_dir: Optional[Any] = None,
              run_id: Optional[str] = None) -> Dict[str, Any]:
    """CLI/API 共用的 MQ 健康与积压报告。"""
    settings = resolve_mq_settings(config, config_dir)
    report: Dict[str, Any] = {
        "enabled": bool(settings.get("enabled")),
        "mode": settings.get("mode"),
        "stream_prefix": settings.get("stream_prefix"),
        "consumer_group": settings.get("consumer_group"),
    }
    if not settings.get("enabled"):
        return report
    mq = build_stream_mq(config, config_dir, settings=settings)
    if mq is None:
        report["enabled"] = False
        report["reason"] = "Redis 不可用或非规范工作区（见 store status）"
        return report
    queue = build_run_queue(config, config_dir, settings=settings)
    report["runs"] = queue.stats() if queue else {}
    report["workers"] = WorkerRegistry(mq).list()
    report["agent_bus"] = {"enabled": bool(settings.get("agent_bus", {}).get("enabled", True))}
    if run_id:
        agent_stream = mq.stream_key(f"agent:{run_id}")
        event_stream = mq.stream_key(f"events:{run_id}")
        try:
            agent_length = mq.client.xlen(agent_stream)
        except Exception:  # noqa: BLE001 - 流不存在
            agent_length = 0
        try:
            event_length = mq.client.xlen(event_stream)
        except Exception:  # noqa: BLE001
            event_length = 0
        try:
            groups = mq.client.xinfo_groups(agent_stream)
        except Exception:  # noqa: BLE001
            groups = []
        report["agent_bus"].update({
            "run_id": run_id,
            "agent_stream": {"key": agent_stream, "length": agent_length},
            "event_stream": {"key": event_stream, "length": event_length},
            "groups": groups,
        })
    return report
