"""Redis 后端：官方 ``redis`` 客户端 + MAS 领域操作。

两部分：
1. ``RedisClient`` —— 基于官方 ``redis`` 包（redis-py）的薄封装：只做 URL 解析、
   连接探测、异常归一化与返回类型整形，不再自实现 RESP2 协议。
   2026-11-25 重构：原约 100 行 socket/RESP2 解析代码删除，换成业界客户端
   （连接池、超时、重连、RESP2/RESP3 兼容都由上游维护）。
2. ``RedisBackend`` —— 面向 MAS 的领域封装：
   - 运行注册表（register / heartbeat / request_cancel / is_canceled / list_running）
   - 读缓存（cache_get / cache_set / cache_invalidate）
   - 分布式锁（acquire_lock / release_lock，Lua 保证 token 校验的原子性）

异常契约：连接层失败抛 ``RedisError``，由调用方决定降级（见 mas/store/__init__.py）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import redis

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
    """redis-py 的薄封装：connect + execute(*args)，URL 解析与异常归一化。"""

    def __init__(self, url: str = "redis://127.0.0.1:6379/0", timeout_s: float = 5.0) -> None:
        parsed = urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = int(parsed.port or 6379)
        self.db = int((parsed.path or "/0").lstrip("/") or 0)
        self.password = unquote(parsed.password) if parsed.password else None
        self.timeout_s = timeout_s
        self._client: Optional[redis.Redis] = None

    # ---------------------------------------------------------------- 连接
    def connect(self) -> None:
        if self._client is not None:
            return
        try:
            client = redis.Redis(
                host=self.host, port=self.port, db=self.db, password=self.password or None,
                socket_connect_timeout=self.timeout_s, socket_timeout=self.timeout_s,
                decode_responses=True, health_check_interval=30,
            )
            client.ping()          # 构造本身不建连，显式探测让失败在装配期暴露
        except (redis.exceptions.RedisError, OSError) as exc:
            raise RedisError(f"无法连接 Redis {self.host}:{self.port}: {exc}") from exc
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except (redis.exceptions.RedisError, OSError):
                pass
            self._client = None

    def __enter__(self) -> "RedisClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- 命令
    def execute(self, *args: Any) -> Any:
        """执行一条命令并返回回复（redis-py 连接池保证线程安全）。"""
        self.connect()
        try:
            return self._client.execute_command(*args)
        except (redis.exceptions.RedisError, OSError) as exc:
            raise RedisError(f"Redis 命令失败 {args[:1]}: {exc}") from exc

    def _call(self, fn: Any) -> Any:
        """调用 redis-py 的高阶方法（需要传递 options 的命令走这里）。"""
        self.connect()
        try:
            return fn(self._client)
        except (redis.exceptions.RedisError, OSError) as exc:
            raise RedisError(f"Redis 命令失败: {exc}") from exc

    # ---------------------------------------------------------------- 常用命令
    def ping(self) -> bool:
        # redis-py 对 +OK 类回复返回 True；保留字符串比较以兼容旧桩/其他实现
        result = self.execute("PING")
        return result is True or result == "PONG"

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
            result = self.execute(*args)
            return result is True or result == "OK"
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
        # 用高阶方法而非 execute_command：redis-py 的 XPENDING 回调依赖 options，
        # 裸调用会把「带 start/end/count 的详情查询」误当摘要解析（实测 IndexError）。
        reply = self._call(lambda client: client.xpending(key, group))
        if not reply:
            return {"count": 0, "min_id": None, "max_id": None, "consumers": []}
        if isinstance(reply, dict):          # redis-py 形态 {pending,min,max,consumers}
            consumers = []
            for item in reply.get("consumers") or []:
                if isinstance(item, dict):
                    consumers.append({"consumer": str(item.get("name")),
                                      "count": int(item.get("pending") or 0)})
                elif item and len(item) >= 2:
                    consumers.append({"consumer": str(item[0]), "count": int(item[1])})
            return {
                "count": int(reply.get("pending") or 0),
                "min_id": reply.get("min"),
                "max_id": reply.get("max"),
                "consumers": consumers,
            }
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
        reply = self._call(
            lambda client: client.xpending_range(key, group, entry_id, entry_id, 1)
        )
        if not reply:
            return None
        item = reply[0]
        if isinstance(item, dict):          # redis-py 的 parse_xpending_range 形态
            return {
                "id": str(item.get("message_id")), "consumer": str(item.get("consumer")),
                "idle_ms": int(item.get("time_since_delivered") or 0),
                "delivery_count": int(item.get("times_delivered") or 0),
            }
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
            # redis-py 已把 XINFO GROUPS 解析成 {name, consumers, pending, ...}；
            # 裸 RESP 客户端给的是扁平 [k, v, k, v]。两种都归一化成 dict。
            if isinstance(raw, dict):
                groups.append({str(name): value for name, value in raw.items()})
                continue
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
            # redis-py 的 stream 响应解析器返回 {field: value}；裸 RESP 客户端返回
            # 扁平 [field, value, ...]。两种形态都要接住（升级客户端不影响上层）。
            if isinstance(raw_fields, dict):
                normalized.append((str(entry_id), {str(k): str(v) for k, v in raw_fields.items()}))
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
