"""Redis Streams MQ 集成测试（真实 Redis；服务不可用时整体 skip）。

运行（受管 venv，Redis 由 docker 提供）：
    C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
        -m unittest tests.test_mq_integration -v

每个用例使用独立的 ``mas:test:<uuid>`` 前缀，结束后删除自己创建的键，
不影响生产流 ``mas:mq:*``。覆盖真实 broker 语义：

1. 生产 → 消费组 → XACK，PEL 归零；
2. 幂等生产者（SET NX 去重键）；
3. 失败 → ZSET 延迟重试 → 重投 → 达到上限进 DLQ；
4. 消费者崩溃后的 XAUTOCLAIM（PEL 认领、delivery_count 递增）；
5. 一次性运行密钥（read/take/delete）；
6. 真实 RunWorker：非法 payload → 死信；run_summary 已存在 → 幂等 ACK。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mas.config import load_config  # noqa: E402
from mas.mq import RunTaskQueue, StreamMQ  # noqa: E402
from mas.runhub import MemoryRunRegistry  # noqa: E402
from mas.store import build_redis_backend  # noqa: E402
from mas.worker import RunWorker  # noqa: E402


class RedisMQIntegrationTests(unittest.TestCase):
    backend = None

    @classmethod
    def setUpClass(cls) -> None:
        config = load_config(PROJECT_ROOT / "config")
        cls.backend = build_redis_backend(config, PROJECT_ROOT / "config")
        if cls.backend is None:
            raise unittest.SkipTest("Redis 不可用（runtime.store / runtime.mq 未启用或服务未启动）")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.backend is not None:
            closer = getattr(cls.backend, "close", None)
            if closer:
                closer()

    def setUp(self) -> None:
        self.prefix = f"mas:test:mq:{uuid.uuid4().hex[:10]}"
        self.mq = StreamMQ(self.backend, prefix=self.prefix, maxlen=1000,
                           stream_ttl_s=60, dedup_ttl_s=60)
        self._keys: list[str] = [self.mq.workers_key()]

    def tearDown(self) -> None:
        keys = set(self._keys) | {
            self.mq.stream_key("runs"), self.mq.retry_key("runs"), self.mq.dlq_key("runs"),
            self.mq.stream_key("ack"), self.mq.stream_key("dedup"), self.mq.stream_key("claim"),
        }
        try:
            for key in keys:
                self.backend.client.delete(key)
        except Exception:  # noqa: BLE001 - 清理失败不影响用例结论
            pass

    def test_01_publish_consume_ack_and_pending(self) -> None:
        published = self.mq.publish("ack", payload={"n": 1}, run_id="r1", topic="t")
        self.assertFalse(published["duplicate"])
        self.assertTrue(published["_stream_id"])
        self._keys.append(self.mq.stream_key("ack"))

        deliveries = self.mq.consume("ack", "g1", "c1", count=10, block_ms=10)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["payload"], {"n": 1})
        self.assertEqual(deliveries[0]["_stream_id"], published["_stream_id"])

        pending = self.mq.stats("ack", "g1")
        self.assertEqual(pending["group"]["pending"], 1)
        self.assertEqual(self.mq.ack("ack", "g1", deliveries), 1)
        self.assertEqual(self.mq.stats("ack", "g1")["group"]["pending"], 0)

    def test_02_idempotent_producer_dedup(self) -> None:
        self._keys.append(self.mq.stream_key("dedup"))
        self._keys.append(self.mq.dedup_key("same"))
        first = self.mq.publish("dedup", payload={"n": 1}, dedup_key="same")
        second = self.mq.publish("dedup", payload={"n": 2}, dedup_key="same")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.backend.client.xlen(self.mq.stream_key("dedup")), 1)

    def test_03_retry_backoff_then_dead_letter(self) -> None:
        queue = RunTaskQueue(self.mq, group="q-retry", max_attempts=2,
                             visibility_timeout_ms=1000, retry_backoff_s=[0.0], poll_ms=10)
        self._keys.append(self.mq.stream_key("runs"))
        self._keys.append(self.mq.retry_key("runs"))
        self._keys.append(self.mq.dlq_key("runs"))
        self._keys.append(self.mq.dedup_key("run:retry-run"))
        enqueued = queue.enqueue({"requirement": "x"}, run_id="retry-run")
        self.assertFalse(enqueued["duplicate"])

        first = queue.consume("w1", count=1, block_ms=10)[0]
        result = queue.fail(first, "boom")
        self.assertEqual(result["status"], "retried")
        self.assertEqual(queue.stats()["scheduled_retries"], 1)

        promoted = queue.promote_due_retries()
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["attempt"], 2)

        second = queue.consume("w1", count=1, block_ms=10)[0]
        self.assertEqual(second["attempt"], 2)
        result = queue.fail(second, "boom again")
        self.assertEqual(result["status"], "dead_lettered")
        self.assertEqual(queue.stats()["dlq_length"], 1)
        self.assertEqual(queue.stats()["scheduled_retries"], 0)
        self.assertEqual(self.mq.dlq_entries("runs")[0]["payload"]["attempts"], 2)

    def test_04_claim_stale_pel_after_consumer_crash(self) -> None:
        self._keys.append(self.mq.stream_key("claim"))
        self.mq.publish("claim", payload={"n": 1}, run_id="r1")
        first = self.mq.consume("claim", "cg", "crashed", count=1, block_ms=10)
        self.assertEqual(len(first), 1)
        # 模拟消费者崩溃：不 ACK，另一个消费者以 min_idle_ms=0 认领
        second = self.mq.consume("claim", "cg", "rescuer", count=1, block_ms=10, claim_idle_ms=0)
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0]["_claimed"])
        self.assertGreaterEqual(second[0]["_delivery_count"], 2)
        self.mq.ack("claim", "cg", second)
        self.assertEqual(self.mq.stats("claim", "cg")["group"]["pending"], 0)

    def test_05_secrets_are_one_shot_or_explicitly_deleted(self) -> None:
        self._keys.append(self.mq.secret_key("r-secret"))
        self.mq.store_secret("r-secret", "sk-test", ttl_s=60)
        self.assertEqual(self.mq.read_secret("r-secret"), "sk-test")
        self.assertEqual(self.mq.read_secret("r-secret"), "sk-test")   # 可重试场景保留
        self.assertEqual(self.mq.take_secret("r-secret"), "sk-test")
        self.assertIsNone(self.mq.read_secret("r-secret"))

        self._keys.append(self.mq.secret_key("r-secret2"))
        self.mq.store_secret("r-secret2", "sk-test2", ttl_s=60)
        self.mq.delete_secret("r-secret2")
        self.assertIsNone(self.mq.read_secret("r-secret2"))

    def test_06_worker_dead_letter_and_idempotent_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir(parents=True)
            config = load_config(PROJECT_ROOT / "config", workspace=str(workspace))
            queue = RunTaskQueue(self.mq, group="q-worker", max_attempts=1,
                                 visibility_timeout_ms=1000, retry_backoff_s=[0.0], poll_ms=10)
            self._keys += [self.mq.stream_key("runs"), self.mq.dlq_key("runs"),
                           self.mq.retry_key("runs"), self.mq.dedup_key("run:bad-run"),
                           self.mq.dedup_key("run:done-run")]
            registry = MemoryRunRegistry()
            worker = RunWorker(config, queue=queue, registry=registry,
                               worker_id="it-worker", log=lambda *args: None)
            self._keys += [self.mq.worker_key("it-worker")]

            queue.enqueue({
                "workspace": str(workspace), "requirement": "", "profile": None,
            }, run_id="bad-run")
            self.assertEqual(worker.poll_once(block_ms=10), 1)
            self.assertEqual(worker.counters["failed"], 1)
            self.assertEqual(queue.stats()["dlq_length"], 1)
            self.assertEqual(self.mq.stats("runs", "q-worker")["group"]["pending"], 0)

            run_dir = workspace / "runs" / "done-run"
            run_dir.mkdir(parents=True)
            (run_dir / "run_summary.json").write_text('{"verdict": "pass"}', encoding="utf-8")
            queue.enqueue({
                "workspace": str(workspace), "requirement": "ignored", "profile": None,
            }, run_id="done-run")
            self.assertEqual(worker.poll_once(block_ms=10), 1)
            self.assertEqual(worker.counters["skipped"], 1)
            self.assertEqual(self.mq.stats("runs", "q-worker")["group"]["pending"], 0)
            worker.cleanup()
            self._keys.append(self.mq.workers_key())


if __name__ == "__main__":
    unittest.main()
