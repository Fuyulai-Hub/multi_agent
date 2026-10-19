"""MySQL 后端：连接管理 + 通用查询原语。

定位（docs/25）是「索引/结构层」：经验库、运行索引、产物索引、事件索引，
不承载文件系统证据层（events.jsonl / artifacts/v*.json 保持 SSOT 不动）。

设计：
- 线程本地连接（``threading.local``）：前端 ThreadingHTTPServer 与流水线各自持连接，
  互不共享 cursor；断线后下一次调用自动重连；
- ``transaction()`` 上下文管理器：替代经验库原来对 experience.jsonl 的全局文件锁，
  用 InnoDB 行锁 + ``SELECT ... FOR UPDATE`` 达到同样的「load→merge→save 原子性」；
- 驱动为**可选软依赖**（PyMySQL）：未安装时 ``mas.store.build_mysql_backend``
  直接返回 None，全系统回落到 jsonl 路径，不报错。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["MySQLError", "MySQLBackend"]


class MySQLError(RuntimeError):
    """MySQL 连接/执行层错误。"""


class MySQLBackend:
    """线程本地连接的薄封装；只提供通用原语，领域语义由调用方组装。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "multi_agent",
        charset: str = "utf8mb4",
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database
        self.charset = charset
        self.connect_timeout_s = connect_timeout_s
        self._local = threading.local()

    # ---------------------------------------------------------------- 连接
    def _connect(self):
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - 由 build_* 提前拦截
            raise MySQLError("缺少依赖 PyMySQL（pip install PyMySQL）") from exc
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset=self.charset,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=int(self.connect_timeout_s),
            autocommit=True,
        )

    def connection(self):
        """取（并按需重建）当前线程的连接。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping()   # 不传 reconnect=True：PyMySQL 1.1+ 已弃用该参数，
                              # 且弃用分支下 commit 后 autocommit 状态可能残留；
                              # ping 失败走下方重建，语义等价且无警告。
                return conn
            except Exception:  # noqa: BLE001 - 断线则丢弃重建
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._local.conn = None
        conn = self._connect()
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._local.conn = None

    def health(self) -> Dict[str, Any]:
        started = time.time()
        try:
            row = self.query_one("SELECT VERSION() AS version, DATABASE() AS db, NOW(3) AS now_at")
            return {
                "ok": True,
                "version": row.get("version") if row else None,
                "database": row.get("db") if row else None,
                "latency_ms": round((time.time() - started) * 1000, 1),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- 查询原语
    def execute(self, sql: str, params: Optional[Iterable[Any]] = None) -> int:
        conn = self.connection()
        try:
            with conn.cursor() as cursor:
                affected = cursor.execute(sql, tuple(params) if params else ())
            return int(affected or 0)
        except MySQLError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MySQLError(f"{type(exc).__name__}: {exc}") from exc

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> int:
        conn = self.connection()
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return 0
        try:
            with conn.cursor() as cursor:
                affected = cursor.executemany(sql, materialized)
            return int(affected or 0)
        except Exception as exc:  # noqa: BLE001
            raise MySQLError(f"{type(exc).__name__}: {exc}") from exc

    def query_all(self, sql: str, params: Optional[Iterable[Any]] = None) -> List[Dict[str, Any]]:
        conn = self.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params) if params else ())
                return list(cursor.fetchall())
        except Exception as exc:  # noqa: BLE001
            raise MySQLError(f"{type(exc).__name__}: {exc}") from exc

    def query_one(self, sql: str, params: Optional[Iterable[Any]] = None) -> Optional[Dict[str, Any]]:
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    @property
    def in_transaction(self) -> bool:
        """当前线程是否处于显式事务中（经验库据此走 SELECT ... FOR UPDATE）。"""
        return bool(getattr(self._local, "in_tx", False))

    @contextmanager
    def transaction(self):
        """显式事务：进入 BEGIN，正常结束 COMMIT，异常 ROLLBACK，退出恢复 autocommit。

        用法（经验库 mysql 后端，替代原来的文件锁）::

            with mysql.transaction():
                library.load()      # 事务内走 SELECT ... FOR UPDATE
                ... 合并/反馈 ...
                library.save()      # 提交即原子生效

        不依赖 pymysql 的 begin() 语义差异：显式 SET autocommit + BEGIN + COMMIT，
        退出时恢复 autocommit，避免「提交后仍留在隐式事务里」导致后续写入不可见。
        """
        conn = self.connection()
        conn.autocommit(False)
        self._local.in_tx = True
        try:
            with conn.cursor() as cursor:
                cursor.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - 回滚失败不掩盖块内真正的异常
                pass
            raise
        finally:
            self._local.in_tx = False
            try:
                conn.autocommit(True)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 结构管理
    def ensure_schema(self, schema_path: Path) -> List[str]:
        """执行 schema.sql 中的 DDL（幂等：全部 IF NOT EXISTS）。

        返回已执行的语句数。按行剥离 ``--`` 注释后以分号切分；
        本项目 DDL 不含字符串内分号，简单切分安全。
        """
        text = Path(schema_path).read_text(encoding="utf-8")
        statements: List[str] = []
        buffer: List[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            buffer.append(line)
            if stripped.endswith(";"):
                statements.append("\n".join(buffer))
                buffer = []
        if buffer and "".join(buffer).strip():
            statements.append("\n".join(buffer))
        executed = 0
        for statement in statements:
            if statement.strip():
                self.execute(statement)
                executed += 1
        return executed

    @staticmethod
    def to_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)
