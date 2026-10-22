"""断点续跑 / 运行租约 / 幂等（docs/37 P0-3）的离线回归。

不依赖 Redis / MySQL / 真实 LLM：
- Leader 检查点：节点转换后原子落盘；崩溃后从 next_node 续跑，输入与已完成节点不重跑；
- 事件序号：续跑后继续单调递增，强杀留下的半行被跳过（否则 MySQL 唯一键会丢事件）；
- Worker：租约互斥（拿不到锁不 ACK、不执行）、续租/心跳、可恢复运行不归档；
- 需求 hash 不一致 / 检查点缺失或已完成 → 拒绝恢复（ConfigError / 死信）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mas.config import load_config  # noqa: E402
from mas.errors import ConfigError  # noqa: E402
from mas.events import EventLog  # noqa: E402
from mas.leader import Leader  # noqa: E402
from mas.runhub import MemoryRunRegistry  # noqa: E402
from mas.worker import RunLease, RunWorker  # noqa: E402

REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


def _offline_config(workspace: Path):
    config = load_config(PROJECT_ROOT / "config", workspace=str(workspace))
    config.llm["provider"] = "offline"
    config.runtime["verbose"] = False
    return config


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _events(run_dir: Path):
    return [json.loads(line) for line in
            (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


class LeaderCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config = _offline_config(self.workspace)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _crash_at_third_agent(self):
        """在第 3 个任务节点（implement）执行时模拟进程崩溃，返回 (run_dir, calls)。"""
        original = Leader._act_with_retry
        calls = {"n": 0}

        def crashing(leader_self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated crash during implement")
            return original(leader_self, *args, **kwargs)

        with mock.patch.object(Leader, "_act_with_retry", crashing):
            with self.assertRaises(RuntimeError):
                Leader(self.config).run(str(REQUIREMENT))
        run_dirs = list((self.workspace / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        return run_dirs[0], calls

    def test_resume_skips_completed_nodes_and_keeps_event_sequence(self):
        run_dir, calls = self._crash_at_third_agent()
        self.assertEqual(calls["n"], 3)
        checkpoint = _read_json(run_dir / "checkpoint.json")
        self.assertEqual(checkpoint["status"], "running")
        self.assertEqual(checkpoint["next_node"], "implement")
        self.assertEqual(checkpoint["steps"], 2)
        self.assertEqual(checkpoint["iterations"], {"intake": 1, "solution": 1})
        self.assertEqual(len(checkpoint["requirement_sha256"]), 64)
        self.assertFalse((run_dir / "run_summary.json").exists())

        seqs_before = [event["seq"] for event in _events(run_dir)]
        summary = Leader(self.config).run(
            str(REQUIREMENT), run_id=run_dir.name, resume=True
        )
        self.assertEqual(summary.verdict, "pass", summary.error)
        self.assertGreaterEqual(summary.steps, 3)

        events = _events(run_dir)
        seqs = [event["seq"] for event in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))          # 续跑不重号
        self.assertEqual(seqs[:len(seqs_before)], seqs_before)
        self.assertIn("run_resumed", [event["type"] for event in events])
        resumed = [e for e in events if e["type"] == "run_resumed"][-1]
        self.assertEqual(resumed["payload"]["resumed_from"], "implement")
        self.assertEqual(resumed["payload"]["completed_steps"], 2)

        starts = [e["node"] for e in events if e["type"] == "node_start"]
        self.assertEqual(starts.count("intake"), 1)          # 输入与已完成节点不重跑
        self.assertEqual(starts.count("solution"), 1)
        # 崩溃时正在执行的 implement 重跑一次；完整流程本身还有一次审查驳回回退
        self.assertEqual(starts.count("implement"), 3)
        # requirement_source 只发布一次（续跑跳过输入阶段）
        versions = list((run_dir / "artifacts" / "requirement_source").glob("v*.json"))
        self.assertEqual(len(versions), 1)
        finished = _read_json(run_dir / "checkpoint.json")
        self.assertEqual(finished["status"], "completed")
        self.assertTrue((run_dir / "run_summary.json").exists())

    def test_resume_rejects_changed_requirement(self):
        run_dir, _ = self._crash_at_third_agent()
        (run_dir / "input" / "requirement.md").write_text("被篡改的需求", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            Leader(self.config).run(str(REQUIREMENT), run_id=run_dir.name, resume=True)
        self.assertIn("不一致", str(caught.exception))
        self.assertEqual(_read_json(run_dir / "checkpoint.json")["status"], "running")

    def test_resume_without_checkpoint_is_rejected(self):
        with self.assertRaises(ConfigError):
            Leader(self.config).run(str(REQUIREMENT), run_id="missing-run", resume=True)

    def test_resume_rejects_completed_checkpoint(self):
        run_dir, _ = self._crash_at_third_agent()
        checkpoint = _read_json(run_dir / "checkpoint.json")
        checkpoint["status"] = "completed"
        (run_dir / "checkpoint.json").write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            Leader(self.config).run(str(REQUIREMENT), run_id=run_dir.name, resume=True)
        self.assertIn("completed", str(caught.exception))


class EventLogResumeTests(unittest.TestCase):
    def test_resume_continues_sequence(self):
        path = Path(tempfile.mkdtemp()) / "events.jsonl"
        log = EventLog(path, "run-seq")
        log.emit("one")
        log.emit("two")

        resumed = EventLog(path, "run-seq", resume=True)
        self.assertEqual([event.seq for event in resumed.events], [1, 2])
        event = resumed.emit("three")
        self.assertEqual(event.seq, 3)
        self.assertEqual(event.id, "evt-00003")
        self.assertEqual([item["seq"] for item in EventLog(path, "run-seq").read()], [1, 2, 3])

    def test_resume_skips_torn_tail_line(self):
        path = Path(tempfile.mkdtemp()) / "events.jsonl"
        path.write_text(
            '{"seq": 1, "type": "one"}\n'
            '{"seq": 2, "type": "two"}\n'
            '{"seq": 3, "type": "tor', encoding="utf-8",
        )
        resumed = EventLog(path, "run-torn", resume=True)
        self.assertEqual([event.seq for event in resumed.events], [1, 2])
        self.assertEqual(resumed.emit("three").seq, 3)


class _FakeBackend:
    def __init__(self, available: bool = True, renew_ok: bool = True) -> None:
        self.available = available
        self.renew_ok = renew_ok
        self.acquired: list[tuple] = []
        self.renewed: list[tuple] = []
        self.released: list[tuple] = []

    def acquire_lock(self, name: str, ttl_ms: int = 10000):
        self.acquired.append((name, ttl_ms))
        return "token-1" if self.available else None

    def renew_lock(self, name: str, token: str, ttl_ms: int = 10000):
        self.renewed.append((name, token, ttl_ms))
        return self.renew_ok

    def release_lock(self, name: str, token: str):
        self.released.append((name, token))
        return True


class _FakeClient:
    """WorkerRegistry 的最小键值替身。"""

    def __init__(self) -> None:
        self.values: dict = {}
        self.sets: dict = {}

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def srem(self, key, *members):
        for member in members:
            self.sets.get(key, set()).discard(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))


class _FakeMQ:
    def __init__(self, backend=None) -> None:
        self.backend = backend
        self.client = _FakeClient()
        self.deleted_secrets: list[str] = []

    def read_secret(self, run_id: str):
        return None

    def worker_key(self, worker_id: str) -> str:
        return f"fake:worker:{worker_id}"

    def workers_key(self) -> str:
        return "fake:workers"

    def stream_key(self, name: str) -> str:
        return f"fake:{name}"

    def delete_secret(self, run_id: str) -> None:
        self.deleted_secrets.append(run_id)

    def reset_run_streams(self, run_id: str) -> int:
        return 0


class _FakeQueue:
    def __init__(self, backend=None) -> None:
        self.mq = _FakeMQ(backend)
        self.max_attempts = 3
        self.acked: list[dict] = []
        self.failed: list[tuple] = []
        self.dead: list[tuple] = []
        self.worker_events: list[tuple] = []

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


class _RecordingLeader:
    """替换 Leader：只记录摘要续跑参数并落一份 summary，不跑真实流水线。"""

    calls: list[dict] = []

    def __init__(self, config) -> None:
        self.config = config

    def run(self, requirement, **kwargs):
        type(self).calls.append({"requirement": requirement, **kwargs})
        run_dir = Path(self.config.runs_dir) / kwargs["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_summary.json").write_text(
            json.dumps({"verdict": "pass", "steps": 1}), encoding="utf-8")
        return SimpleNamespace(verdict="pass", steps=1)


class WorkerLeaseAndResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.config = _offline_config(self.workspace)
        _RecordingLeader.calls = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _worker(self, backend=None):
        queue = _FakeQueue(backend)
        worker = RunWorker(self.config, queue=queue, registry=MemoryRunRegistry(),
                           worker_id="lease-worker", log=lambda *args: None)
        return worker, queue

    @staticmethod
    def _prepare_run(run_dir: Path, *, status: str = "running") -> None:
        (run_dir / "input").mkdir(parents=True, exist_ok=True)
        (run_dir / "input" / "requirement.md").write_text("需求", encoding="utf-8")
        (run_dir / "events.jsonl").write_text('{"seq": 1, "type": "node_start"}\n',
                                             encoding="utf-8")
        (run_dir / "checkpoint.json").write_text(json.dumps({
            "version": 1, "run_id": run_dir.name, "status": status,
            "requirement_title": "需求", "requirement_sha256": "x",
            "next_node": "intake", "steps": 1,
        }, ensure_ascii=False), encoding="utf-8")

    def test_lease_busy_leaves_message_unacked_and_skips_execution(self):
        backend = _FakeBackend(available=False)
        worker, queue = self._worker(backend)
        run_dir = self.workspace / "runs" / "run-busy"
        self._prepare_run(run_dir)
        delivery = {"run_id": "run-busy", "payload": {"requirement": "需求"}, "attempt": 1}

        with mock.patch("mas.worker.Leader", _RecordingLeader):
            self.assertEqual(worker._handle(delivery), "lease_busy")
        self.assertEqual(queue.acked, [])
        self.assertEqual(_RecordingLeader.calls, [])
        self.assertEqual(worker.counters["skipped"], 1)
        self.assertTrue(run_dir.exists())                    # 未被归档/破坏

    def test_lease_acquired_resume_true_and_released(self):
        backend = _FakeBackend()
        worker, queue = self._worker(backend)
        run_dir = self.workspace / "runs" / "run-resume"
        self._prepare_run(run_dir)
        delivery = {"run_id": "run-resume", "payload": {"requirement": "需求"}, "attempt": 2}

        with mock.patch("mas.worker.Leader", _RecordingLeader):
            self.assertEqual(worker._handle(delivery), "finished")
        self.assertEqual(len(queue.acked), 1)
        self.assertEqual(backend.acquired[0][0], "run:run-resume")
        self.assertTrue(backend.released)
        self.assertEqual(len(_RecordingLeader.calls), 1)
        self.assertTrue(_RecordingLeader.calls[0]["resume"])
        self.assertFalse((self.workspace / "run_attempts").exists())
        self.assertIn("run-resume", queue.mq.deleted_secrets)

    def test_completed_checkpoint_is_archived_not_resumed(self):
        backend = _FakeBackend()
        worker, queue = self._worker(backend)
        run_dir = self.workspace / "runs" / "run-done-cp"
        self._prepare_run(run_dir, status="completed")
        delivery = {"run_id": "run-done-cp", "payload": {"requirement": "需求"}, "attempt": 2}

        with mock.patch("mas.worker.Leader", _RecordingLeader):
            self.assertEqual(worker._handle(delivery), "finished")
        self.assertFalse(_RecordingLeader.calls[0]["resume"])
        archives = list((self.workspace / "run_attempts" / "run-done-cp").glob("attempt-2-*"))
        self.assertEqual(len(archives), 1)

    def test_run_lease_renews_and_heartbeats(self):
        backend = _FakeBackend()
        registry = MemoryRunRegistry()
        registry.register("run-lease")
        lease = RunLease(backend, "run-lease", worker_id="w", ttl_ms=30000,
                         renew_s=0.05, registry=registry, log=lambda *args: None)
        self.assertTrue(lease.acquire())
        lease.start()
        time.sleep(0.2)
        lease.stop()
        self.assertGreaterEqual(len(backend.renewed), 1)
        self.assertEqual(backend.released[0][0], "run:run-lease")
        self.assertFalse(lease.lost)

    def test_run_lease_marks_lost_when_renew_fails(self):
        backend = _FakeBackend(renew_ok=False)
        lease = RunLease(backend, "run-lost", worker_id="w", ttl_ms=30000,
                         renew_s=0.05, log=lambda *args: None)
        self.assertTrue(lease.acquire())
        lease.start()
        time.sleep(0.2)
        lease.stop()
        self.assertTrue(lease.lost)


if __name__ == "__main__":
    unittest.main()
