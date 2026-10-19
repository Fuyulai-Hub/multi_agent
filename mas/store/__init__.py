"""数据存储后端：MySQL（索引/结构层）与 Redis（协调层）。

接入约定（docs/25，与全仓「配置门控 + 逐字节兼容」惯例一致）：
- ``runtime.store.enabled=false`` 时，两个 build_* 均返回 None，调用方走既有
  jsonl / 内存路径，行为与改造前逐字节一致；
- 驱动缺失（未装 PyMySQL）或连接失败 → 只降级 + 返回 None + 留痕，
  **绝不抛异常打断流水线主流程**（数据库故障不该让交付失败）；
- 连接参数优先级：环境变量 > config/credentials.yaml 的 store 段 > system.yaml 默认值
  （与 LLM 凭据的三通道外注同款约定，密码只从环境变量/凭据文件来，不写进 system.yaml）。

典型用法::

    from mas.store import build_backends
    backends = build_backends(config)          # config 为 load_config 的结果
    backends.mysql, backends.redis             # 可用或 None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import load_credentials_raw
from ..errors import ConfigError

__all__ = ["StoreBackends", "build_backends", "build_mysql_backend", "build_redis_backend", "store_status"]


@dataclass
class StoreBackends:
    """一次解析后的存储后端集合（任一为 None 表示该后端不可用/未启用）。"""

    mysql: Any = None
    redis: Any = None
    settings: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.settings is None:
            self.settings = {}

    @property
    def any_enabled(self) -> bool:
        return self.mysql is not None or self.redis is not None


def resolve_store_settings(config: Any, config_dir: Optional[Any] = None) -> Dict[str, Any]:
    """合并三层配置来源，产出实际生效的存储设置。

    优先级（后者覆盖前者）：system.yaml 的 runtime.store 段 → credentials.yaml 的
    store 段 → 环境变量（键名由 *_env 指定）。
    """
    import os

    base: Dict[str, Any] = dict(getattr(config, "store", None) or {})
    creds: Dict[str, Any] = {}
    try:
        raw = load_credentials_raw(config_dir)
        creds = dict(raw.get("store") or {}) if isinstance(raw, dict) else {}
    except ConfigError:
        creds = {}

    def _merge(section: str, keys: Dict[str, str]) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(base.get(section) or {})
        merged.update({k: v for k, v in creds.get(section, {}).items() if v not in (None, "")})
        for field, env_name in keys.items():
            env_value = os.environ.get(str(merged.get(env_name) or "")) if merged.get(env_name) else None
            if not env_value and env_name in os.environ:
                env_value = os.environ[env_name]
            if env_value:
                merged[field] = env_value
        return merged

    mysql = _merge(
        "mysql",
        {
            "host": "host_env",
            "port": "port_env",
            "user": "user_env",
            "password": "password_env",
            "database": "database_env",
        },
    )
    redis = _merge("redis", {"url": "url_env"})
    return {
        "enabled": bool(base.get("enabled", False)),
        "mysql": mysql,
        "redis": redis,
        "heartbeat_ttl_s": int(base.get("redis", {}).get("heartbeat_ttl_s", 90) or 90),
        "cache_ttl_s": int(base.get("redis", {}).get("cache_ttl_s", 15) or 15),
    }


def _bound_to_canonical_workspace(config: Any) -> bool:
    """工作区绑定守卫：只有「配置指定的规范工作区」才接入数据库。

    测试与一次性脚本普遍用临时 workspace 跑流水线；若也连库，测试数据会写进
    真实库、且测试断言（jsonl 文件存在性）会因后端切换而失败。临时 workspace
    → 自动全禁用，行为与改造前逐字节一致。
    """
    try:
        from pathlib import Path

        canonical = (config.project_root / str(config.system.get("workspace", "workspace"))).resolve()
        return Path(config.workspace_dir).resolve() == canonical
    except Exception:  # noqa: BLE001 - 取不到路径时保守禁用
        return False


def build_mysql_backend(config: Any, config_dir: Optional[Any] = None) -> Any:
    """构建 MySQL 后端；未启用/驱动缺失/连接失败/非规范工作区时返回 None。"""
    settings = resolve_store_settings(config, config_dir)
    if not settings["enabled"] or not settings["mysql"].get("enabled", True):
        return None
    if not _bound_to_canonical_workspace(config):
        return None
    try:
        from .mysql_backend import MySQLBackend
    except ImportError:
        return None
    mysql_cfg = settings["mysql"]
    try:
        backend = MySQLBackend(
            host=str(mysql_cfg.get("host", "127.0.0.1")),
            port=int(mysql_cfg.get("port", 3306) or 3306),
            user=str(mysql_cfg.get("user", "root")),
            password=str(mysql_cfg.get("password", "")),
            database=str(mysql_cfg.get("database", "multi_agent")),
        )
        return backend if backend.health().get("ok") else None
    except Exception:  # noqa: BLE001 - 任何连接层失败都只降级，不打断主流程
        return None


def build_redis_backend(config: Any, config_dir: Optional[Any] = None) -> Any:
    """构建 Redis 后端（零依赖 RESP2 客户端）；未启用/连接失败/非规范工作区时返回 None。"""
    settings = resolve_store_settings(config, config_dir)
    if not settings["enabled"] or not settings["redis"].get("enabled", True):
        return None
    if not _bound_to_canonical_workspace(config):
        return None
    try:
        from .redis_backend import RedisBackend, RedisClient
    except ImportError:
        return None
    url = str(settings["redis"].get("url") or "redis://127.0.0.1:6379/0")
    try:
        client = RedisClient(url)
        client.ping()
        return RedisBackend(client, heartbeat_ttl_s=settings["heartbeat_ttl_s"])
    except Exception:  # noqa: BLE001 - 同上：降级不抛异常
        return None


def build_backends(config: Any, config_dir: Optional[Any] = None) -> StoreBackends:
    """一次性构建两个后端（任一不可用即 None）。"""
    return StoreBackends(
        mysql=build_mysql_backend(config, config_dir),
        redis=build_redis_backend(config, config_dir),
        settings=resolve_store_settings(config, config_dir),
    )


def store_status(config: Any, config_dir: Optional[Any] = None) -> Dict[str, Any]:
    """存储后端健康报告（CLI ``mas store status`` 与前端 /api/store/status 用）。"""
    settings = resolve_store_settings(config, config_dir)
    guard_ok = _bound_to_canonical_workspace(config)
    report: Dict[str, Any] = {
        "enabled": settings["enabled"],
        "mysql": None,
        "redis": None,
        "workspace_guard": guard_ok,
    }
    if settings["enabled"]:
        mysql = build_mysql_backend(config, config_dir)
        if mysql is not None:
            report["mysql"] = mysql.health()
        elif not guard_ok:
            report["mysql"] = {"ok": False, "reason": "非规范工作区（自动禁用，见 docs/25 工作区绑定守卫）"}
        else:
            report["mysql"] = {"ok": False, "reason": "连接失败或未启用"}
        redis = build_redis_backend(config, config_dir)
        if redis is not None:
            report["redis"] = {"ok": True, "url": settings["redis"].get("url"), "running_runs": len(redis.list_running())}
        elif not guard_ok:
            report["redis"] = {"ok": False, "reason": "非规范工作区（自动禁用，见 docs/25 工作区绑定守卫）"}
        else:
            report["redis"] = {"ok": False, "reason": "连接失败或未启用"}
    return report
