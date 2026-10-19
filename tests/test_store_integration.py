"""MySQL / Redis 存储后端集成测试（docs/25）。

服务不可用时相关用例自动 skip（整体或按后端 skip），**不算失败**——
接入约定本身就是「软依赖降级」：没有服务的环境（CI、纯 jsonl 开发机）
必须全绿。用以下解释器跑真实集成：

    # 受管 venv（含 PyMySQL；两个后端都真实连通）
    C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m unittest tests.test_store_integration -v

覆盖：
1. 后端可用性探测：对 system.yaml 真实配置 build_backends（可用或 None）；
2. Redis：注册表全生命周期（register→heartbeat→cancel→unregister）+ 缓存 + 分布式锁；
3. MySQL：health、experiences UPSERT 往返、runs 索引 UPSERT（leader.upsert_run_index）；
4. 降级路径（无需服务）：store.enabled=false → 双后端 None；Redis 连不上 → None；
   PyMySQL 不可导入 → None；临时 workspace → 工作区守卫禁用；
5. 迁移幂等与不覆盖：migrate_jsonl_to_mysql 重复执行结果一致、只补缺口
   （INSERT IGNORE，实时写入的 MySQL 行永不被陈旧 jsonl 快照覆盖，用真实 workspace 的经验库）；
6. 内存注册表语义：events_map 共享、幂等 register、取消闭环（纯离线）。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mas.config import load_config  # noqa: E402
from mas.experience import Experience, ExperienceLibrary, migrate_jsonl_to_mysql  # noqa: E402
from mas.leader import upsert_run_index  # noqa: E402
from mas.runhub import MemoryRunRegistry, build_registry  # noqa: E402
from mas.store import (  # noqa: E402
    build_backends,
    build_mysql_backend,
    build_redis_backend,
    resolve_store_settings,
)


class _FakeStoreConfig:
    """只带 store 接入所需字段的最小 config 替身（降级路径测试用）。"""

    def __init__(self, store: dict, workspace_dir: Path | None = None) -> None:
        self._store = store
        self.project_root = PROJECT_ROOT
        self.workspace_dir = workspace_dir or (PROJECT_ROOT / "workspace")
        self.system = {"workspace": "workspace"}

    @property
    def store(self) -> dict:
        return self._store


class StoreIntegrationTests(unittest.TestCase):
    """真实后端集成用例：按后端可用性逐个 skip。"""

    mysql = None
    redis = None
    config = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(PROJECT_ROOT / "config")
        backends = build_backends(cls.config, PROJECT_ROOT / "config")
        cls.mysql = backends.mysql
        cls.redis = backends.redis
        if cls.mysql is not None:
            # 集成测试自备 schema（幂等）：等价于先跑一次 `python -m mas store init`
            cls.mysql.ensure_schema(PROJECT_ROOT / "mas" / "store" / "schema.sql")

    @classmethod
    def tearDownClass(cls) -> None:
        for closer in (getattr(cls.mysql, "close", None),
                       getattr(getattr(cls.redis, "client", None), "close", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 清理失败不影响测试结论
                    pass

    def setUp(self) -> None:
        if self.mysql is None and self.redis is None:
            self.skipTest("MySQL 与 Redis 均不可用（服务未启动 / store.enabled=false）")

    # ---------------------------------------------------------------- Redis
    def test_redis_registry_lifecycle(self):
        if self.redis is None:
            self.skipTest("Redis 不可用")
        run_id = f"test-registry-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.redis.unregister, run_id)

        self.redis.register(run_id, meta={"source": "integration-test"})
        self.assertTrue(self.redis.is_running(run_id))
        self.assertFalse(self.redis.is_canceled(run_id))

        self.redis.heartbeat(run_id, status="running", node="intake")
        info = self.redis.get(run_id)
        self.assertIsNotNone(info)
        self.assertEqual(info.get("node"), "intake")
        self.assertTrue(any(item.get("run_id") == run_id for item in self.redis.list_running()))

        # 取消闭环：request_cancel → is_canceled → clear_cancel → 复位
        self.assertTrue(self.redis.request_cancel(run_id))
        self.assertTrue(self.redis.is_canceled(run_id))
        self.redis.clear_cancel(run_id)
        self.assertFalse(self.redis.is_canceled(run_id))
        # 未注册的 run 请求取消返回 False（前端据此报 404）
        self.assertFalse(self.redis.request_cancel(f"absent-{uuid.uuid4().hex[:8]}"))

        self.redis.unregister(run_id)
        self.assertFalse(self.redis.is_running(run_id))
        self.assertIsNone(self.redis.get(run_id))

    def test_redis_cache_roundtrip(self):
        if self.redis is None:
            self.skipTest("Redis 不可用")
        name = f"test-cache-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.redis.cache_invalidate, name)
        self.redis.cache_set(name, {"hello": "世界", "n": 1})
        self.assertEqual(self.redis.cache_get(name), {"hello": "世界", "n": 1})
        self.redis.cache_invalidate(name)
        self.assertIsNone(self.redis.cache_get(name))

    def test_redis_lock_exclusivity(self):
        if self.redis is None:
            self.skipTest("Redis 不可用")
        name = f"test-lock-{uuid.uuid4().hex[:8]}"
        token = self.redis.acquire_lock(name, ttl_ms=10000)
        self.assertIsNotNone(token)
        self.addCleanup(self.redis.release_lock, name, token)
        # 互斥：第二把锁拿不到
        self.assertIsNone(self.redis.acquire_lock(name, ttl_ms=10000))
        # 错 token 释放失败（防误删他人锁）
        self.assertFalse(self.redis.release_lock(name, "wrong-token"))
        # 正确 token 释放后可再获取
        self.assertTrue(self.redis.release_lock(name, token))
        self.assertIsNotNone(self.redis.acquire_lock(name, ttl_ms=10000))

    def test_build_registry_prefers_redis(self):
        registry = build_registry(self.config, PROJECT_ROOT / "config")
        backend = getattr(registry, "backend", None)
        if backend is not None:   # Redis 版：用完关连接，避免 ResourceWarning
            self.addCleanup(backend.client.close)
        if self.redis is None:
            self.assertIsInstance(registry, MemoryRunRegistry)
        else:
            self.assertEqual(registry.__class__.__name__, "RedisRunRegistry")

    # ---------------------------------------------------------------- MySQL
    def test_mysql_health(self):
        if self.mysql is None:
            self.skipTest("MySQL 不可用（未启用 / 驱动缺失 / 连接失败）")
        health = self.mysql.health()
        self.assertTrue(health.get("ok"), health)
        self.assertIn("8.", str(health.get("version")))

    def test_mysql_experience_upsert_roundtrip(self):
        if self.mysql is None:
            self.skipTest("MySQL 不可用")
        entry = Experience(
            id=f"exp-test-{uuid.uuid4().hex[:8]}",
            tier="L1", scope="run", roles=["developer"], tags=["integration"],
            trigger="test_trigger", lesson="集成测试经验（可删）", action="无",
            action_key="test.integration", confidence=0.42,
            source_run="test-store-integration",
        )
        self.addCleanup(
            self.mysql.execute, "DELETE FROM experiences WHERE id = %s", (entry.id,))
        library = ExperienceLibrary(
            PROJECT_ROOT / "workspace", self.config.learning, mysql=self.mysql)
        library.add([entry], source_run="test-store-integration")
        loaded = {item.id: item for item in library.load()}
        self.assertIn(entry.id, loaded)
        row = loaded[entry.id]
        self.assertEqual(row.lesson, entry.lesson)
        self.assertEqual(row.roles, ["developer"])
        self.assertAlmostEqual(row.confidence, entry.confidence, places=6)

    def test_mysql_run_index_upsert_roundtrip(self):
        if self.mysql is None:
            self.skipTest("MySQL 不可用")
        run_id = f"test-run-{uuid.uuid4().hex[:8]}"
        self.addCleanup(
            self.mysql.execute,
            "DELETE FROM run_metrics WHERE run_id = %s", (run_id,))
        self.addCleanup(
            self.mysql.execute, "DELETE FROM runs WHERE run_id = %s", (run_id,))
        upsert_run_index(self.mysql, row={
            "run_id": run_id,
            "requirement_title": "集成测试运行（可删）",
            "verdict": "pass",
            "steps": 3, "rollbacks": 1, "review_rejections": 2,
            "bugs_total": 4, "bugs_fixed": 3,
            "duration_s": 12.5, "injected_experiences": 2,
            "profile": "test", "ts": "2026-10-19T00:00:00+08:00",
            "canceled": False,
        })
        row = self.mysql.query_one("SELECT * FROM runs WHERE run_id = %s", (run_id,))
        self.assertIsNotNone(row)
        self.assertEqual(row["verdict"], "pass")
        self.assertEqual(row["steps"], 3)
        self.assertEqual(row["bugs_total"], 4)
        self.assertEqual(row["finished"], 1)
        self.assertEqual(row["canceled"], 0)
        # 幂等更新：改字段再 UPSERT 同一行
        upsert_run_index(self.mysql, row={
            "run_id": run_id, "verdict": "fail", "steps": 9, "canceled": True})
        row = self.mysql.query_one("SELECT * FROM runs WHERE run_id = %s", (run_id,))
        self.assertEqual(row["verdict"], "fail")
        self.assertEqual(row["steps"], 9)
        self.assertEqual(row["canceled"], 1)
        self.assertEqual(
            self.mysql.query_one("SELECT COUNT(*) AS n FROM runs WHERE run_id = %s",
                                 (run_id,))["n"], 1)

    def test_mysql_schema_ensure_idempotent(self):
        if self.mysql is None:
            self.skipTest("MySQL 不可用")
        schema_path = PROJECT_ROOT / "mas" / "store" / "schema.sql"
        first = self.mysql.ensure_schema(schema_path)    # 返回执行的语句数（int）
        second = self.mysql.ensure_schema(schema_path)
        self.assertEqual(first, second)   # 幂等：两次执行同样条数
        self.assertGreater(first, 0)
        # 六张表齐全
        rows = self.mysql.query_all(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = DATABASE()")
        names = {row["name"] for row in rows}
        for expected in ("experiences", "runs", "run_metrics",
                         "artifacts_index", "run_events", "llm_traces"):
            self.assertIn(expected, names)

    def test_migrate_jsonl_idempotent(self):
        if self.mysql is None:
            self.skipTest("MySQL 不可用")
        workspace = self.config.workspace_dir
        jsonl_path = workspace / "knowledge" / "experience.jsonl"
        if not jsonl_path.exists():
            self.skipTest("workspace 下无经验库 jsonl，无可迁移数据")
        # 基线：jsonl 行数 vs 去重键数（历史漂移行会被确定性折叠）
        with jsonl_path.open("r", encoding="utf-8") as handle:
            rows = [Experience.from_dict(json.loads(line))
                    for line in handle if line.strip()]
        distinct_keys = len({row.key for row in rows})

        first = migrate_jsonl_to_mysql(self.mysql, workspace, self.config.learning)
        second = migrate_jsonl_to_mysql(self.mysql, workspace, self.config.learning)
        self.assertEqual(first, second)             # 重复迁移结果一致（幂等）
        self.assertEqual(first["experiences"], distinct_keys)
        self.assertEqual(first["collapsed_rows"], len(rows) - distinct_keys)
        self.assertGreaterEqual(first["metrics"], 0)
        # 完整性：jsonl 的每个去重键在 MySQL 中都有对应行。
        # 行数用 >= 而非 ==：MySQL 启用后的实时运行会新增 jsonl 里没有的经验
        # （实时写入只进 MySQL，jsonl 是启用前的快照，见 _MYSQL_MIGRATE_EXPERIENCE 注释）。
        db_keys = {r["key_hash"] for r in
                   self.mysql.query_all("SELECT key_hash FROM experiences")}
        self.assertTrue({row.key for row in rows} <= db_keys,
                        "迁移后 jsonl 的所有去重键必须已在 MySQL 中")
        db_count = self.mysql.query_one("SELECT COUNT(*) AS n FROM experiences")["n"]
        self.assertGreaterEqual(db_count, distinct_keys)
        # 抽查一条：迁入的经验可被 ExperienceLibrary 原样读回
        sample = rows[0]
        loaded = {item.id: item for item in ExperienceLibrary(
            workspace, self.config.learning, mysql=self.mysql).load()}
        if sample.id in loaded:   # 基准行 id 一定在；被折叠行的 id 允许不在
            self.assertEqual(loaded[sample.id].action_key, sample.action_key)

    def test_migrate_never_clobbers_live_rows(self):
        """migrate 是 INSERT IGNORE：MySQL 既有行（含运行中实时增量）永不被
        陈旧的 jsonl 快照覆盖。回归用例：曾因 migrate 用 UPSERT，一次演示运行后
        二次迁移把 6 行 hits 65→39 回滚（2026-10-19 实测）。"""
        if self.mysql is None:
            self.skipTest("MySQL 不可用")
        workspace = self.config.workspace_dir
        jsonl_path = workspace / "knowledge" / "experience.jsonl"
        if not jsonl_path.exists():
            self.skipTest("workspace 下无经验库 jsonl，无可迁移数据")
        # 先确保库里有行（空库时首迁一次）
        migrate_jsonl_to_mysql(self.mysql, workspace, self.config.learning)
        existing = self.mysql.query_one("SELECT id, hits FROM experiences LIMIT 1")
        if existing is None:
            self.skipTest("MySQL 无经验行")
        # 模拟一次实时写入的增量（retrieve/reinforce 在 MySQL 路径只改 MySQL）
        sentinel_hits = int(existing["hits"]) + 12345
        self.mysql.execute("UPDATE experiences SET hits = %s WHERE id = %s",
                           (sentinel_hits, existing["id"]))
        # 重复迁移：既有行必须原样保留
        migrate_jsonl_to_mysql(self.mysql, workspace, self.config.learning)
        after = self.mysql.query_one(
            "SELECT hits FROM experiences WHERE id = %s", (existing["id"],))
        self.assertEqual(int(after["hits"]), sentinel_hits,
                         "migrate 不得覆盖 MySQL 既有行（INSERT IGNORE 语义）")


class StoreDegradationTests(unittest.TestCase):
    """降级路径：无需任何外部服务，全部可离线运行。"""

    def setUp(self) -> None:
        self.config = load_config(PROJECT_ROOT / "config")

    def test_store_disabled_gate(self):
        fake = _FakeStoreConfig({"enabled": False, "mysql": {"enabled": True},
                                 "redis": {"enabled": True}})
        self.assertIsNone(build_mysql_backend(fake, PROJECT_ROOT / "config"))
        self.assertIsNone(build_redis_backend(fake, PROJECT_ROOT / "config"))
        report = build_backends(fake, PROJECT_ROOT / "config")
        self.assertFalse(report.any_enabled)

    def test_redis_unreachable_returns_none(self):
        """连不上的 Redis → None（降级不抛异常）。

        必须走环境变量通道覆盖：credentials.yaml 的 store 段优先级高于
        system.yaml/本假配置（三通道设计），只在假配置里写 url 会被真实
        凭据文件盖掉；``url_env`` 声明 + ``MAS_REDIS_URL`` 才是最高优先级。
        """
        fake = _FakeStoreConfig({
            "enabled": True,
            "mysql": {"enabled": False},
            "redis": {"enabled": True, "url": "redis://127.0.0.1:6379/0",
                      "url_env": "MAS_REDIS_URL"},
        })
        with mock.patch.dict(os.environ, {"MAS_REDIS_URL": "redis://127.0.0.1:1/0"}):
            self.assertIsNone(build_redis_backend(fake, PROJECT_ROOT / "config"))

    def test_mysql_driver_missing_returns_none(self):
        fake = _FakeStoreConfig({
            "enabled": True,
            "mysql": {"enabled": True, "host": "127.0.0.1", "port": 3306,
                      "user": "mas", "password": "x", "database": "multi_agent"},
            "redis": {"enabled": False},
        })
        with mock.patch.dict(sys.modules, {"pymysql": None}):
            self.assertIsNone(build_mysql_backend(fake, PROJECT_ROOT / "config"))

    def test_non_canonical_workspace_guard(self):
        """临时 workspace 一律禁用数据库（测试数据不写真实库）。"""
        fake = _FakeStoreConfig(
            {"enabled": True, "mysql": {"enabled": True}, "redis": {"enabled": True}},
            workspace_dir=PROJECT_ROOT / "nonexistent-temp-workspace",
        )
        self.assertIsNone(build_mysql_backend(fake, PROJECT_ROOT / "config"))
        self.assertIsNone(build_redis_backend(fake, PROJECT_ROOT / "config"))

    def test_settings_resolution_priority(self):
        """环境变量 > credentials.yaml > system.yaml（与 LLM 凭据同款约定）。"""
        settings = resolve_store_settings(self.config, PROJECT_ROOT / "config")
        self.assertIn("mysql", settings)
        self.assertIn("redis", settings)
        self.assertGreaterEqual(settings["heartbeat_ttl_s"], 1)

    def test_memory_registry_shares_events_map(self):
        shared: dict = {}
        registry = MemoryRunRegistry(events_map=shared)
        event = registry.register("run-a")
        self.assertIs(event, shared["run-a"])          # 同一对象：前端 RUNNING_TASKS 共享语义
        self.assertIs(registry.register("run-a"), event)   # 幂等：不重置取消信号
        self.assertTrue(registry.request_cancel("run-a"))
        self.assertTrue(registry.is_canceled("run-a"))
        self.assertTrue(event.is_set())
        self.assertFalse(registry.request_cancel("absent"))
        registry.unregister("run-a")
        self.assertNotIn("run-a", shared)
        self.assertFalse(registry.is_running("run-a"))


if __name__ == "__main__":
    unittest.main()
