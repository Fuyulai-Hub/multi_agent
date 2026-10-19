"""MQ 离线单测：不依赖 Redis（真实 Stream 语义见 tests/test_mq_integration.py）。

覆盖：
1. MessageBus 的 MQ 生产/消费路径（发布→消费组投递→ACK→digest），
   broker 故障时降级且只告警一次；
2. 事件流 sink：全量事件发布到 events:<run_id>，失败计数不打断主流程；
3. Worker 的幂等/归档/取消/不可重试失败→死信；
4. CLI 子命令（worker / mq / run --enqueue）装配；
5. 前端模块导入时 MQ 全局为 None（未 main() 时行为与改造前一致）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mas.bus import MessageBus  # noqa: E402
from mas.cli import build_parser  # noqa: E402
from mas.config import load_config  # noqa: E402
from mas.events import EventLog  # noqa: E402
from mas.leader import _make_mq_event_sink  # noqa: E402
from mas.runhub import MemoryRunRegistry  # noqa: E402
from mas.worker import RunWorker  # noqa: E402


class FakeClient:
    """WorkerRegistry 需要的最小键值能力。"""

    def __init__(self) -> None:
        self.values: dict = {}
        self.sets: dict = {}

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, *keys):
        count = 0
        for key in keys:
            count += 1 if self.values.pop(key, None) is not None else 0
            self.sets.pop(key, None)
        return count

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def srem(self, key, *members):
        for member in members:
            self.sets.get(key, set()).discard(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))


class FakeMQ:
    """进程内 StreamMQ 替身：按消费组独立推进 offset，模拟扇出与 ACK。"""

    def __init__(self) -> None:
        self.client = FakeClient()
        self.entries: list[dict] = []
        self.published: list[dict] = []
        self.acked: list[tuple] = []
        self._offsets: dict = {}
        self.fail_publish = False
        self.deleted_secrets: list[str] = []
        self.secrets: dict = {}

    def stream_key(self, name: str) -> str:
        return f"fake:{name}"

    def retry_key(self, name: str) -> str:
        return f"fake:retry:{name}"

    def dlq_key(self, name: str) -> str:
        return f"fake:dlq:{name}"

    def worker_key(self, worker_id: str) -> str:
        return f"fake:worker:{worker_id}"

    def workers_key(self) -> str:
        return "fake:workers"

    def publish(self, stream, **kwargs):
        if self.fail_publish:
            raise ConnectionError("broker down")
        envelope = {
            **kwargs,
            "stream": self.stream_key(stream),
            "_stream_id": f"{len(self.entries) + 1}-0",
            "duplicate": False,
        }
        self.entries.append(envelope)
        self.published.append(envelope)
        return envelope

    def consume(self, stream, group, consumer, *, count=1, block_ms=None, claim_idle_ms=None):
        start = self._offsets.get(group, 0)
        batch = self.entries[start:start + count]
        self._offsets[group] = start + len(batch)
        return [dict(item) for item in batch]

    def ack(self, stream, group, deliveries):
        ids = [item["_stream_id"] if isinstance(item, dict) else item for item in deliveries]
        self.acked.extend(ids)
        return len(ids)

    def stats(self, stream, group=None):
        return {"stream": self.stream_key(stream), "length": len(self.entries),
                "group": {"name": group, "pending": 0}}

    def store_secret(self, run_id, value, ttl_s=3600):
        self.secrets[run_id] = value

    def read_secret(self, run_id):
        return self.secrets.get(run_id)

    def delete_secret(self, run_id):
        self.deleted_secrets.append(run_id)
        self.secrets.pop(run_id, None)

    def reset_run_streams(self, run_id):
        return 0

    def cleanup_consumer(self, stream, group, consumer):
        return True


class FakeQueue:
    """RunTaskQueue 替身：只记录 worker 的调度决策。"""

    STREAM = "runs"

    def __init__(self, mq: FakeMQ | None = None) -> None:
        self.mq = mq or FakeMQ()
        self.group = "test-workers"
        self.max_attempts = 3
        self.poll_ms = 1
        self.visibility_timeout_ms = 1000
        self.queue_ttl_s = 60
        self.deliveries: list[dict] = []
        self.acked: list[dict] = []
        self.failed: list[tuple] = []
        self.dead: list[tuple] = []
        self.worker_events: list[tuple] = []

    def consume(self, consumer, *, count=1, block_ms=None):
        batch, self.deliveries = self.deliveries[:count], self.deliveries[count:]
        return batch

    def ack(self, delivery):
        self.acked.append(delivery)
        return 1

    def fail(self, delivery, error):
        self.failed.append((delivery, error))
        return {"status": "retried", "attempt": 1, "delay_s": 0}

    def dead_letter(self, delivery, error, attempt=None):
        self.dead.append((delivery, error))
        self.acked.append(delivery)
        return {"status": "dead_lettered", "attempt": attempt or 1}

    def promote_due_retries(self):
        return []

    def publish_worker_event(self, event_type, *, run_id, payload, sender):
        self.worker_events.append((event_type, run_id, payload))

    def register_queued_run(self, registry, run_id):
        registry.register(run_id, {"status": "queued"}, ttl_s=self.queue_ttl_s)

    def stats(self):
        return {}


class MessageBusMQTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = EventLog(self.root / "events.jsonl", "run-mq")
        self.mq = FakeMQ()
        self.bus = MessageBus(self.root / "messages.jsonl", self.events, "run-mq", mq=self.mq)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_publish_then_group_delivery_and_ack(self) -> None:
        message = self.bus.send(
            "product", "requirement_ready", {"summary": "需求已就绪"},
            recipients=["developer"], node="intake", iteration=1,
        )
        self.assertEqual(len(self.mq.published), 1)
        envelope = self.mq.published[0]
        self.assertEqual(envelope["topic"], "requirement_ready")
        self.assertEqual(envelope["headers"]["logical_id"], message.id)
        self.assertEqual(envelope["headers"]["node"], "intake")

        self.assertEqual(self.bus.pump("developer"), 1)
        mailbox = self.bus.mailbox("developer")
        self.assertEqual([item.id for item in mailbox], [message.id])
        self.assertTrue(self.mq.acked)      # pump 必须 ACK，避免 PEL 堆积

        # 发送方不会收到自己的消息（即使消费组从头读到）
        self.assertEqual(self.bus.pump("product"), 0)

    def test_digest_uses_broker_mailbox_and_broadcast_fans_out(self) -> None:
        self.bus.broadcast("leader", "rollback", {"summary": "回退到 implement"})
        text = self.bus.digest("reviewer")
        self.assertIn("rollback", text)
        self.assertIn("leader", text)
        delivered = [e for e in self.events.events if e.type == "mq_digest_delivered"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].payload["role"], "reviewer")
        stats = self.bus.transport_stats()
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["published"], 1)
        self.assertEqual(stats["delivered"].get("reviewer"), 1)
        self.assertEqual(stats["errors"], 0)

    def test_broker_failure_degrades_and_warns_once(self) -> None:
        self.mq.fail_publish = True
        self.bus.send("leader", "notice", {"summary": "a"})
        self.bus.send("leader", "notice", {"summary": "b"})
        warnings = [e for e in self.events.events if e.type == "mq_publish_failed"]
        self.assertEqual(len(warnings), 1)          # 只告警一次，避免刷屏
        self.assertEqual(self.bus.transport_stats()["errors"], 2)
        # 证据链仍完整：messages.jsonl 落盘不受 broker 影响
        self.assertEqual(len(self.bus.history()), 2)

    def test_digest_falls_back_to_local_inbox_when_broker_unavailable(self) -> None:
        self.bus.send("product", "note", {"summary": "local"}, recipients=["tester"])
        self.mq.fail_publish = True
        self.mq.consume = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
        text = self.bus.digest("tester")
        self.assertIn("note", text)


class EventSinkTests(unittest.TestCase):
    def test_event_sink_publishes_with_sequence_headers(self) -> None:
        mq = FakeMQ()
        events = EventLog(Path(tempfile.mkdtemp()) / "events.jsonl", "run-evt")
        sink = _make_mq_event_sink(mq, "run-evt")
        events.subscribe(sink)
        events.emit("node_start", agent="developer", node="implement", iteration=2,
                    payload={"title": "编码"}, refs=["evt-ref"])
        envelope = mq.published[-1]
        self.assertEqual(envelope["topic"], "node_start")
        self.assertEqual(envelope["sender"], "developer")
        self.assertEqual(envelope["headers"]["node"], "implement")
        self.assertEqual(envelope["headers"]["iteration"], 2)
        self.assertEqual(envelope["headers"]["event_id"], "evt-00001")
        self.assertEqual(envelope["refs"], ["evt-ref"])
        self.assertEqual(sink.stats["published"], 1)
        self.assertEqual(sink.stats["errors"], 0)

    def test_event_sink_swallows_broker_errors(self) -> None:
        mq = FakeMQ()
        mq.fail_publish = True
        sink = _make_mq_event_sink(mq, "run-evt")
        events = EventLog(Path(tempfile.mkdtemp()) / "events.jsonl", "run-evt")
        events.subscribe(sink)
        events.emit("run_started")
        self.assertEqual(sink.stats["errors"], 1)


class WorkerOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.config = load_config(PROJECT_ROOT / "config", workspace=str(self.workspace))
        self.mq = FakeMQ()
        self.queue = FakeQueue(self.mq)
        self.registry = MemoryRunRegistry()
        self.worker = RunWorker(self.config, queue=self.queue, registry=self.registry,
                                log=lambda *args: None, worker_id="test-worker")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_archive_partial_run_and_reuse_queue_placeholder(self) -> None:
        run_dir = self.workspace / "runs" / "run-archive"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text('{"seq": 1, "type": "node_start"}\n', encoding="utf-8")
        self.assertTrue(self.worker._archive_partial_run("run-archive", 2))
        self.assertFalse(run_dir.exists())
        archives = list((self.workspace / "run_attempts" / "run-archive").glob("attempt-2-*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "ARCHIVED.md").exists())

        placeholder = self.workspace / "runs" / "run-queued"
        placeholder.mkdir(parents=True)
        (placeholder / "events.jsonl").touch()
        (placeholder / "queued.json").write_text('{"title": "t"}', encoding="utf-8")
        self.assertFalse(self.worker._archive_partial_run("run-queued", 1))
        self.assertTrue(placeholder.exists())

    def test_idempotent_skip_when_summary_exists(self) -> None:
        run_dir = self.workspace / "runs" / "run-done"
        run_dir.mkdir(parents=True)
        (run_dir / "run_summary.json").write_text('{"run_id": "run-done", "verdict": "pass"}',
                                                   encoding="utf-8")
        delivery = {"run_id": "run-done", "payload": {}, "attempt": 1}
        self.assertEqual(self.worker._handle(delivery), "duplicate")
        self.assertEqual(self.queue.acked, [delivery])
        self.assertEqual(self.worker.counters["skipped"], 1)

    def test_invalid_payload_goes_to_dead_letter(self) -> None:
        delivery = {"run_id": "run-bad", "payload": {"workspace": str(self.workspace)}, "attempt": 1}
        self.assertEqual(self.worker._handle(delivery), "dead_lettered")
        self.assertEqual(len(self.queue.dead), 1)
        self.assertIn("requirement", self.queue.dead[0][1])
        self.assertEqual(self.worker.counters["failed"], 1)

    def test_workspace_mismatch_is_permanent(self) -> None:
        delivery = {
            "run_id": "run-ws", "attempt": 1,
            "payload": {"workspace": str(self.workspace / "other"), "requirement": "x"},
        }
        self.assertEqual(self.worker._handle(delivery), "dead_lettered")
        self.assertIn("不一致", self.queue.dead[0][1])

    def test_queued_cancel_writes_canceled_summary(self) -> None:
        run_dir = self.workspace / "runs" / "run-cancel"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").touch()
        (run_dir / "queued.json").write_text('{"title": "待取消需求", "profile": null}',
                                             encoding="utf-8")
        self.registry.register("run-cancel", {"status": "queued"})
        self.registry.request_cancel("run-cancel")
        delivery = {"run_id": "run-cancel", "payload": {}, "attempt": 1}
        self.assertEqual(self.worker._handle(delivery), "canceled")
        summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["canceled"])
        self.assertEqual(summary["verdict"], "fail")
        self.assertEqual(summary["requirement_title"], "待取消需求")
        self.assertEqual(summary["steps"], 0)
        self.assertFalse(self.registry.is_running("run-cancel"))

    def test_secret_deleted_after_final_ack(self) -> None:
        self.mq.secrets["run-secret"] = "sk-test"
        run_dir = self.workspace / "runs" / "run-secret"
        run_dir.mkdir(parents=True)
        (run_dir / "run_summary.json").write_text("{}", encoding="utf-8")
        self.worker._handle({"run_id": "run-secret", "payload": {}, "attempt": 1})
        self.assertIn("run-secret", self.mq.deleted_secrets)


class CliAndFrontendDefaultsTests(unittest.TestCase):
    def test_cli_parses_worker_mq_enqueue(self) -> None:
        parser = build_parser()
        worker = parser.parse_args(["worker", "--once", "--wait-ms", "100"])
        self.assertEqual(worker.command, "worker")
        self.assertTrue(worker.once)
        self.assertEqual(worker.wait_ms, 100)
        mq = parser.parse_args(["mq", "status", "--run-id", "r1"])
        self.assertEqual(mq.command, "mq")
        self.assertEqual(mq.run_id, "r1")
        run = parser.parse_args(["run", "-r", "x", "--enqueue"])
        self.assertTrue(run.enqueue)

    def test_frontend_mq_globals_default_off(self) -> None:
        """直接导入/调用 start_run 时（测试路径）不得自动接入 MQ。"""
        import frontend.server as server

        self.assertIsNone(server.RUN_QUEUE)
        self.assertIsNone(server.WORKER)
        self.assertEqual(server.MQ_SETTINGS, {})


if __name__ == "__main__":
    unittest.main()
