"""Redis 后端：零依赖 RESP2 客户端 + MAS 领域操作。

两部分：
1. ``RedisClient`` —— 仅用标准库 socket 实现的 Redis 协议客户端（RESP2）。
   项目既有约定是「除 PyYAML 外零依赖」，Redis 的文本协议足够简单，
   自实现约 150 行即可覆盖所需命令，避免为一个协调层引入第三方包；
   环境里已装 ``redis`` 包时不冲突（本类不 import 它）。
2. ``RedisBackend`` —— 面向 MAS 的领域封装：
   - 运行注册表（register / heartbeat / request_cancel / is_canceled / list_running）
   - 读缓存（cache_get / cache_set / cache_invalidate）
   - 分布式锁（acquire_lock / release_lock，Lua 保证 token 校验的原子性）

异常契约：连接层失败抛 ``RedisError``，由调用方决定降级（见 mas/store/__init__.py）。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

__all__ = ["RedisError", "RedisClient", "RedisBackend"]


class RedisError(RuntimeError):
    """Redis 连接/协议层错误（调用方按「后端不可用」降级处理）。"""


_RELEASE_LOCK_LUA = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] "
    "then return redis.call('DEL', KEYS[1]) else return 0 end"
)

_RENEW_LOCK_LUA = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] "
    "then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end"
)


class RedisClient:
    """最小 RESP2 客户端：connect + execute(*args)，支持 AUTH/SELECT。"""

    def __init__(self, url: str = "redis://127.0.0.1:6379/0", timeout_s: float = 5.0) -> None:
        parsed = urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = int(parsed.port or 6379)
        self.db = int((parsed.path or "/0").lstrip("/") or 0)
        self.password = unquote(parsed.password) if parsed.password else None
        self.timeout_s = timeout_s
        self._sock: Optional[socket.socket] = None
        self._reader = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 连接
    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            sock.settimeout(self.timeout_s)
            self._sock = sock
            self._reader = sock.makefile("rb")
            if self.password:
                self.execute("AUTH", self.password)
            if self.db:
                self.execute("SELECT", self.db)
        except OSError as exc:
            self.close()
            raise RedisError(f"无法连接 Redis {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        for handle in (self._reader, self._sock):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._reader = None
        self._sock = None

    def __enter__(self) -> "RedisClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- 协议
    def execute(self, *args: Any) -> Any:
        """执行一条命令并返回解析后的回复（线程安全）。"""
        with self._lock:
            self.connect()
            try:
                self._send(args)
                return self._read_reply()
            except (OSError, RedisError) as exc:
                self.close()
                raise RedisError(f"Redis 命令失败 {args[:1]}: {exc}") from exc

    def _send(self, args: tuple) -> None:
        parts = [f"*{len(args)}\r\n".encode("utf-8")]
        for arg in args:
            payload = str(arg).encode("utf-8")
            parts.append(f"${len(payload)}\r\n".encode("utf-8") + payload + b"\r\n")
        assert self._sock is not None
        self._sock.sendall(b"".join(parts))

    def _read_reply(self) -> Any:
        assert self._reader is not None
        line = self._reader.readline()
        if not line:
            raise RedisError("连接已被对端关闭")
        prefix, body = line[:1], line[1:-2]
        if prefix == b"+":
            return body.decode("utf-8", errors="replace")
        if prefix == b"-":
            raise RedisError(body.decode("utf-8", errors="replace"))
        if prefix == b":":
            return int(body)
        if prefix == b"$":
            length = int(body)
            if length == -1:
                return None
            data = self._reader.read(length + 2)
            return data[:-2].decode("utf-8", errors="replace")
        if prefix == b"*":
            count = int(body)
            if count == -1:
                return None
            return [self._read_reply() for _ in range(count)]
        raise RedisError(f"无法解析的 RESP 回复: {line!r}")

    # ---------------------------------------------------------------- 常用命令
    def ping(self) -> bool:
        return self.execute("PING") == "PONG"

    def get(self, key: str) -> Optional[str]:
        return self.execute("GET", key)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        return bool(self.execute("SET", key, value, "EX", int(ex)) if ex else self.execute("SET", key, value))

    def setnx(self, key: str, value: str, px_ms: Optional[int] = None) -> bool:
        args = ("SET", key, value, "NX") + (("PX", int(px_ms)) if px_ms else ())
        return self.execute(*args) is not None

    def expire(self, key: str, ttl_s: int) -> bool:
        return bool(self.execute("EXPIRE", key, int(ttl_s)))

    def delete(self, *keys: str) -> int:
        return int(self.execute("DEL", *keys) or 0)

    def sadd(self, key: str, *members: str) -> int:
        return int(self.execute("SADD", key, *members) or 0)

    def srem(self, key: str, *members: str) -> int:
        return int(self.execute("SREM", key, *members) or 0)

    def smembers(self, key: str) -> List[str]:
        return [str(item) for item in (self.execute("SMEMBERS", key) or [])]

    def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        return self.execute("EVAL", script, numkeys, *args)

    def getdel(self, key: str) -> Optional[str]:
        """原子读取并删除（一次性凭据/任务密钥用）。"""
        return self.execute("GETDEL", key)

    def set_nx_ex(self, key: str, value: str, ttl_s: int) -> bool:
        """SET NX EX：幂等去重键。"""
        return self.execute("SET", key, value, "NX", "EX", int(ttl_s)) is not None

    # ---------------------------------------------------------------- ZSET
    # 用途：延迟重试调度（score = 下次可见的 unix 时间戳）
    def zadd(self, key: str, mapping: Dict[str, float]) -> int:
        args: List[Any] = ["ZADD", key]
        for member, score in mapping.items():
            args.extend([float(score), member])
        return int(self.execute(*args) or 0)

    def zrem(self, key: str, *members: str) -> int:
        return int(self.execute("ZREM", key, *members) or 0)

    def zrangebyscore(self, key: str, minimum: Any, maximum: Any,
                      *, start: Optional[int] = None, count: Optional[int] = None) -> List[str]:
        args: List[Any] = ["ZRANGEBYSCORE", key, minimum, maximum]
        if count is not None:
            args += ["LIMIT", int(start or 0), int(count)]
        return [str(item) for item in (self.execute(*args) or [])]

    def zcard(self, key: str) -> int:
        return int(self.execute("ZCARD", key) or 0)

    # ---------------------------------------------------------------- Stream
    def xadd(self, key: str, fields: Dict[str, Any], *, maxlen: Optional[int] = None) -> str:
        args: List[Any] = ["XADD", key]
        if maxlen is not None:
            args += ["MAXLEN", "~", int(maxlen)]
        args.append("*")
        for field, value in fields.items():
            args.extend([field, "" if value is None else str(value)])
        return str(self.execute(*args))

    def xgroup_create(self, key: str, group: str, start: str = "0", *, mkstream: bool = True) -> bool:
        args: List[Any] = ["XGROUP", "CREATE", key, group, start]
        if mkstream:
            args.append("MKSTREAM")
        try:
            return self.execute(*args) == "OK"
        except RedisError as exc:
            if "BUSYGROUP" in str(exc):
                return False
            raise

    def xreadgroup(self, group: str, consumer: str, streams: Dict[str, str],
                   *, count: int = 10, block_ms: Optional[int] = None) -> List[Any]:
        """XREADGROUP：返回 [(stream_key, [(entry_id, fields_dict), ...]), ...]。"""
        args: List[Any] = ["XREADGROUP", "GROUP", group, consumer, "COUNT", int(count)]
        if block_ms is not None:
            args += ["BLOCK", int(block_ms)]
        args.append("STREAMS")
        keys = list(streams)
        args.extend(keys)
        args.extend(str(streams[key]) for key in keys)
        reply = self.execute(*args)
        if not reply:
            return []
        parsed: List[Any] = []
        for item in reply:
            if not item or len(item) != 2:
                continue
            stream_key, entries = item
            parsed.append((str(stream_key), self._normalize_stream_entries(entries)))
        return parsed

    def xgroup_delconsumer(self, key: str, group: str, consumer: str) -> int:
        """删除消费组内的消费者（仅在其 PEL 为空时安全，调用方负责判定）。"""
        return int(self.execute("XGROUP", "DELCONSUMER", key, group, consumer) or 0)

    def xack(self, key: str, group: str, *entry_ids: str) -> int:
        if not entry_ids:
            return 0
        return int(self.execute("XACK", key, group, *entry_ids) or 0)

    def xautoclaim(self, key: str, group: str, consumer: str, min_idle_ms: int,
                   start: str = "0-0", *, count: int = 10) -> Any:
        """XAUTOCLAIM：认领空闲超过 min_idle_ms 的 PEL 消息（消费者崩溃后的重投）。"""
        reply = self.execute(
            "XAUTOCLAIM", key, group, consumer, int(min_idle_ms), start, "COUNT", int(count)
        )
        if not reply or len(reply) < 2:
            return "0-0", [], []
        next_cursor = str(reply[0])
        entries = self._normalize_stream_entries(reply[1])
        deleted = [str(item) for item in (reply[2] if len(reply) > 2 else []) or []]
        return next_cursor, entries, deleted

    def xpending(self, key: str, group: str) -> Dict[str, Any]:
        """XPENDING 摘要：总数、最小/最大 id、各消费者持有量。"""
        reply = self.execute("XPENDING", key, group)
        if not reply:
            return {"count": 0, "min_id": None, "max_id": None, "consumers": []}
        consumers = []
        for item in reply[3] or []:
            if item and len(item) >= 2:
                consumers.append({"consumer": str(item[0]), "count": int(item[1])})
        return {
            "count": int(reply[0] or 0),
            "min_id": reply[1],
            "max_id": reply[2],
            "consumers": consumers,
        }

    def xpending_detail(self, key: str, group: str, entry_id: str) -> Optional[Dict[str, Any]]:
        """单条 PEL 详情（含投递次数）：用于判断是否已达最大重试次数。"""
        reply = self.execute("XPENDING", key, group, entry_id, entry_id, 1)
        if not reply:
            return None
        item = reply[0]
        return {
            "id": str(item[0]), "consumer": str(item[1]),
            "idle_ms": int(item[2]), "delivery_count": int(item[3]),
        }

    def xlen(self, key: str) -> int:
        return int(self.execute("XLEN", key) or 0)

    def xrange(self, key: str, start: str = "-", end: str = "+",
               *, count: Optional[int] = None) -> List[Any]:
        args: List[Any] = ["XRANGE", key, start, end]
        if count is not None:
            args += ["COUNT", int(count)]
        return self._normalize_stream_entries(self.execute(*args) or [])

    def xinfo_groups(self, key: str) -> List[Dict[str, Any]]:
        reply = self.execute("XINFO", "GROUPS", key)
        groups: List[Dict[str, Any]] = []
        for raw in reply or []:
            record: Dict[str, Any] = {}
            for index in range(0, len(raw) - 1, 2):
                record[str(raw[index])] = raw[index + 1]
            groups.append(record)
        return groups

    def xtrim(self, key: str, maxlen: int) -> int:
        return int(self.execute("XTRIM", key, "MAXLEN", "~", int(maxlen)) or 0)

    @staticmethod
    def _normalize_stream_entries(entries: Any) -> List[Any]:
        normalized: List[Any] = []
        for entry in entries or []:
            if not entry or len(entry) != 2:
                continue
            entry_id, raw_fields = entry
            if raw_fields is None:      # 已删除的 PEL 条目
                normalized.append((str(entry_id), {}))
                continue
            fields: Dict[str, str] = {}
            for index in range(0, len(raw_fields), 2):
                fields[str(raw_fields[index])] = str(raw_fields[index + 1])
            normalized.append((str(entry_id), fields))
        return normalized


class RedisBackend:
    """MAS 领域操作：运行注册表 / 读缓存 / 分布式锁。"""

    def __init__(self, client: RedisClient, *, key_prefix: str = "mas", heartbeat_ttl_s: int = 90) -> None:
        self.client = client
        self.prefix = key_prefix
        self.heartbeat_ttl_s = int(heartbeat_ttl_s)

    # ---------------------------------------------------------------- 运行注册表
    def _run_key(self, run_id: str) -> str:
        return f"{self.prefix}:run:{run_id}"

    def _running_key(self) -> str:
        return f"{self.prefix}:runs:running"

    def register(self, run_id: str, meta: Optional[Dict[str, Any]] = None,
                 ttl_s: Optional[int] = None) -> None:
        """注册运行；``ttl_s`` 供任务队列使用（排队中的运行 TTL 长于心跳 TTL）。"""
        payload = {"run_id": run_id, "status": "running", "node": None, "updated_at": time.time()}
        payload.update(meta or {})
        self.client.set(self._run_key(run_id), json.dumps(payload, ensure_ascii=False),
                        ex=int(ttl_s or self.heartbeat_ttl_s))
        self.client.sadd(self._running_key(), run_id)

    def heartbeat(self, run_id: str, status: str = "running", node: Optional[str] = None) -> None:
        raw = self.client.get(self._run_key(run_id))
        payload: Dict[str, Any] = {}
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
        payload.update({"run_id": run_id, "status": status, "node": node, "updated_at": time.time()})
        self.client.set(self._run_key(run_id), json.dumps(payload, ensure_ascii=False), ex=self.heartbeat_ttl_s)
        self.client.sadd(self._running_key(), run_id)

    def unregister(self, run_id: str) -> None:
        self.client.delete(self._run_key(run_id), f"{self._run_key(run_id)}:cancel")
        self.client.srem(self._running_key(), run_id)

    def request_cancel(self, run_id: str) -> bool:
        """请求取消：键不存在（未注册/已结束）返回 False，调用方据此报 404。

        取消键独立于运行心跳键：长时间节点即使让心跳键短暂过期，已设置的取消
        仍可重复请求成功（幂等），不会被误报为「运行不存在」。
        """
        cancel_key = f"{self._run_key(run_id)}:cancel"
        if self.client.get(self._run_key(run_id)) is None:
            return self.client.get(cancel_key) is not None
        self.client.set(cancel_key, "1", ex=3600)
        return True

    def is_canceled(self, run_id: str) -> bool:
        return self.client.get(f"{self._run_key(run_id)}:cancel") is not None

    def clear_cancel(self, run_id: str) -> None:
        self.client.delete(f"{self._run_key(run_id)}:cancel")

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(self._run_key(run_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def is_running(self, run_id: str) -> bool:
        return self.client.get(self._run_key(run_id)) is not None

    def list_running(self) -> List[Dict[str, Any]]:
        runs: List[Dict[str, Any]] = []
        for run_id in self.client.smembers(self._running_key()):
            info = self.get(run_id)
            if info is not None:
                runs.append(info)
            else:  # 键已过期但集合成员残留（TTL 到期先于清理）：顺手收敛
                self.client.srem(self._running_key(), run_id)
        return runs

    # ---------------------------------------------------------------- 读缓存
    def _cache_key(self, name: str) -> str:
        return f"{self.prefix}:cache:{name}"

    def cache_get(self, name: str) -> Any:
        raw = self.client.get(self._cache_key(name))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def cache_set(self, name: str, value: Any, ttl_s: Optional[int] = None) -> None:
        self.client.set(self._cache_key(name), json.dumps(value, ensure_ascii=False, default=str),
                        ex=int(ttl_s) if ttl_s else None)

    def cache_invalidate(self, *names: str) -> None:
        if names:
            self.client.delete(*(self._cache_key(name) for name in names))

    # ---------------------------------------------------------------- 分布式锁
    def _lock_key(self, name: str) -> str:
        return f"{self.prefix}:lock:{name}"

    def acquire_lock(self, name: str, ttl_ms: int = 10000) -> Optional[str]:
        """SET NX PX 获取锁，成功返回 token（释放时须带 token 校验）。"""
        token = uuid.uuid4().hex
        return token if self.client.setnx(self._lock_key(name), token, px_ms=ttl_ms) else None

    def renew_lock(self, name: str, token: str, ttl_ms: int = 10000) -> bool:
        """持锁人续租：token 不匹配（锁已过期并被他人拿走）时返回 False。

        长任务（LLM 单节点可达数十分钟）必须续租，否则租约到期后另一个
        worker 会认为持有者已死并重跑同一运行。
        """
        result = self.client.eval(
            _RENEW_LOCK_LUA, 1, self._lock_key(name), token, int(ttl_ms)
        )
        return bool(result)

    def release_lock(self, name: str, token: str) -> bool:
        """Lua 原子释放：只有持锁人（token 匹配）能删，防误删他人锁。"""
        result = self.client.eval(_RELEASE_LOCK_LUA, 1, self._lock_key(name), token)
        return bool(result)
