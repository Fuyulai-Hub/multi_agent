"""运行 worker：从 Redis Stream 消费 run_task，把长任务从 API 进程解耦。

为什么需要它：Leader.run() 是同步单链循环，真实 LLM 下单节点可达数十分钟。
把运行绑在 HTTP 进程的 daemon 线程上，面板一重启任务就丢。本模块让 API 只做
「入队」这一件快事，worker 独立进程消费、心跳、ACK；崩溃时消息留在 PEL，
超过 visibility timeout 后由其他消费者认领（at-least-once）。

可靠性边界：
- 消费成功（无论验收 pass/fail）→ ACK；业务失败不重试（重试只针对基础设施故障）；
- 运行异常 → 延迟重试（ZSET），达到上限进 DLQ；
- 重试前把上次未完成的运行目录归档到 ``workspace/run_attempts/`` 并清理该 run
  的 Agent/事件流，保证重跑使用干净的证据链；
- 幂等：``run_summary.json`` 已存在则直接 ACK（上一次可能崩溃在 ACK 之前）；
- 取消：排队/执行中的 run 由 registry 的取消键判定，worker 在领取前与 Leader
  在节点边界各检查一次。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .config import SystemConfig, load_config
from .errors import ConfigError
from .leader import Leader
from .mq import (
    MQUnavailable,
    RunTaskQueue,
    WorkerRegistry,
    build_run_queue,
    resolve_mq_settings,
)
from .runhub import build_registry

__all__ = ["RunWorker", "PermanentTaskError", "make_worker_id"]


class PermanentTaskError(RuntimeError):
    """重试也不会成功的任务错误（payload 非法 / 工作区不匹配 / 配置缺失）。"""


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class RunWorker:
    """单进程 worker：顺序消费运行任务（一个任务跑完再取下一个）。"""

    def __init__(
        self,
        config: SystemConfig,
        *,
        config_dir: Optional[str | Path] = None,
        worker_id: Optional[str] = None,
        queue: Optional[RunTaskQueue] = None,
        registry: Optional[Any] = None,
        log: Any = print,
    ) -> None:
        self.config = config
        self.config_dir = Path(config_dir).resolve() if config_dir else config.config_dir
        self.workspace = config.workspace_dir
        settings = resolve_mq_settings(config, self.config_dir)
        self.settings = settings
        self.queue = queue or build_run_queue(config, self.config_dir, settings=settings)
        if self.queue is None:
            raise MQUnavailable(
                "MQ 未启用或 Redis 不可用（检查 config/system.yaml 的 runtime.mq 与 store.redis；"
                "可用 `python -m mas mq status` 查看）"
            )
        self.registry = registry if registry is not None else build_registry(config, self.config_dir)
        self.worker_registry = WorkerRegistry(self.queue.mq)
        self.worker_id = worker_id or make_worker_id()
        self.log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.counters = {"finished": 0, "failed": 0, "skipped": 0, "canceled": 0}

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> threading.Thread:
        """后台线程启动（前端内嵌 worker 用），返回线程对象。"""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(target=self.run_forever, name="mas-worker", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_forever(self) -> None:
        """阻塞式消费循环；SIGINT/SIGTERM 触发优雅退出。"""
        self.worker_registry.register(
            self.worker_id, queue=self.queue.mq.stream_key(self.queue.STREAM),
            group=self.queue.group, workspace=str(self.workspace),
        )
        self.queue.publish_worker_event(
            "worker_started", run_id="",
            payload={"worker_id": self.worker_id, "workspace": str(self.workspace),
                     "group": self.queue.group},
            sender=self.worker_id,
        )
        self.log(f"[worker] {self.worker_id} 已启动｜group={self.queue.group}"
                 f"｜stream={self.queue.mq.stream_key(self.queue.STREAM)}")
        previous = self._install_signal_handlers()
        try:
            while not self._stop.is_set():
                self.poll_once()
        finally:
            self._cleanup_worker()
            self.queue.publish_worker_event(
                "worker_stopped", run_id="",
                payload={"worker_id": self.worker_id, "counters": dict(self.counters)},
                sender=self.worker_id,
            )
            self._restore_signal_handlers(previous)
            self.log(f"[worker] {self.worker_id} 已退出｜{json.dumps(self.counters, ensure_ascii=False)}")

    def _install_signal_handlers(self) -> Dict[int, Any]:
        previous: Dict[int, Any] = {}
        if threading.current_thread() is not threading.main_thread():
            return previous
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                previous[int(sig)] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: self.stop())
            except (ValueError, OSError):  # pragma: no cover - 非主线程/受限平台
                previous.pop(int(sig), None)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: Dict[int, Any]) -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass

    # ---------------------------------------------------------------- 消费
    def poll_once(self, *, block_ms: Optional[int] = None) -> int:
        """执行一轮：心跳 → 提升到期重试 → 领取并处理任务；返回处理条数。"""
        self.worker_registry.heartbeat(self.worker_id, status="idle")
        try:
            self.queue.promote_due_retries()
        except Exception as exc:  # noqa: BLE001 - 重试调度失败不阻塞新任务
            self.log(f"[worker] 提升延迟重试失败：{type(exc).__name__}: {exc}")
        deliveries = self.queue.consume(self.worker_id, count=1, block_ms=block_ms)
        for delivery in deliveries:
            self._handle(delivery)
        return len(deliveries)

    def _handle(self, delivery: Dict[str, Any]) -> str:
        run_id = str(delivery.get("run_id") or "").strip()
        payload = dict(delivery.get("payload") or {})
        attempt = int(delivery.get("attempt") or 1)
        if not run_id:
            self.queue.dead_letter(delivery, "任务缺少 run_id")
            self.counters["failed"] += 1
            return "dead_lettered"
        if self._is_canceled(run_id):
            self.queue.ack(delivery)
            self._cleanup_canceled(run_id)
            self.counters["canceled"] += 1
            self._event("worker_task_canceled", run_id, {"worker_id": self.worker_id})
            self.log(f"[worker] 已取消排队任务 {run_id}")
            return "canceled"
        summary_path = self.workspace / "runs" / run_id / "run_summary.json"
        if summary_path.exists():
            self.queue.ack(delivery)
            self.queue.mq.delete_secret(run_id)
            self.counters["skipped"] += 1
            self._event("worker_task_skipped", run_id,
                        {"worker_id": self.worker_id, "reason": "run_summary.json 已存在（幂等）"})
            self.log(f"[worker] 跳过已完成任务 {run_id}（幂等）")
            return "duplicate"

        self.worker_registry.heartbeat(self.worker_id, status="busy", run_id=run_id)
        self._event("worker_task_started", run_id,
                    {"worker_id": self.worker_id, "attempt": attempt,
                     "queued_at": (delivery.get("headers") or {}).get("enqueued_at")})
        self.log(f"[worker] 开始任务 {run_id}（attempt {attempt}/{self.queue.max_attempts}）")
        started = time.time()
        try:
            summary = self._execute(run_id, payload, attempt, delivery)
        except PermanentTaskError as exc:
            result = self.queue.dead_letter(delivery, str(exc), attempt)
            self.queue.mq.delete_secret(run_id)
            self.counters["failed"] += 1
            self._event("worker_task_dead_lettered", run_id,
                        {"worker_id": self.worker_id, "error": str(exc), "attempt": attempt})
            self.log(f"[worker] 任务不可重试，已进死信 {run_id}：{exc}")
            return str(result.get("status") or "dead_lettered")
        except Exception as exc:  # noqa: BLE001 - 基础设施/未预期异常都走重试
            error = f"{type(exc).__name__}: {exc}"
            result = self.queue.fail(delivery, error)
            if result.get("status") == "dead_lettered":
                self.queue.mq.delete_secret(run_id)
                self.counters["failed"] += 1
            self._event(
                "worker_task_dead_lettered" if result.get("status") == "dead_lettered"
                else "worker_task_retried",
                run_id,
                {"worker_id": self.worker_id, "error": error[:2000], "attempt": attempt,
                 "delay_s": result.get("delay_s"), "max_attempts": self.queue.max_attempts},
            )
            self.log(f"[worker] 任务失败 {run_id}：{error[:300]}"
                     f"（{result.get('status')}，attempt {attempt}）")
            return str(result.get("status") or "failed")

        elapsed = round(time.time() - started, 2)
        self.queue.ack(delivery)
        self.queue.mq.delete_secret(run_id)
        self.counters["finished"] += 1
        self._event("worker_task_finished", run_id,
                    {"worker_id": self.worker_id, "verdict": getattr(summary, "verdict", None),
                     "steps": getattr(summary, "steps", None), "duration_s": elapsed,
                     "attempt": attempt})
        self.log(f"[worker] 完成 {run_id}｜verdict={getattr(summary, 'verdict', None)}"
                 f"｜{elapsed}s")
        return "finished"

    def _execute(self, run_id: str, payload: Dict[str, Any], attempt: int,
                 delivery: Dict[str, Any]) -> Any:
        workspace = str(payload.get("workspace") or "").strip()
        if workspace and Path(workspace).resolve() != self.workspace:
            raise PermanentTaskError(
                f"任务工作区 {workspace} 与 worker 工作区 {self.workspace} 不一致"
            )
        requirement_path = str(payload.get("requirement_path") or "").strip()
        requirement = str(payload.get("requirement") or "").strip()
        if not requirement_path and not requirement:
            raise PermanentTaskError("任务缺少 requirement / requirement_path")
        if requirement_path and not Path(requirement_path).exists():
            raise PermanentTaskError(f"需求文件不存在: {requirement_path}")

        profile = payload.get("profile") or None
        try:
            config = load_config(self.config_dir, profile=profile, workspace=str(self.workspace))
        except ConfigError as exc:
            raise PermanentTaskError(f"运行配置加载失败（profile={profile}）：{exc}") from exc
        provider = str(payload.get("provider") or "").strip()
        model = str(payload.get("model") or "").strip()
        if provider:
            config.llm["provider"] = provider
        if model:
            config.llm["model"] = model
        base_url = str(payload.get("base_url") or "").strip()
        if base_url:
            config.llm_overrides["base_url"] = base_url
        api_key = self.queue.mq.read_secret(run_id)
        if api_key:
            config.llm_overrides["api_key"] = api_key

        # 重试前归档半截运行目录并重置该 run 的 broker 流，保证证据链干净。
        if self._archive_partial_run(run_id, attempt):
            try:
                self.queue.mq.reset_run_streams(run_id)
            except Exception:  # noqa: BLE001 - 流清理失败不影响重跑
                pass

        images = payload.get("images") or None
        leader = Leader(config)
        if requirement_path:
            return leader.run(requirement_path, as_text=False, run_id=run_id,
                              images=images, registry=self.registry)
        return leader.run(requirement, as_text=True, run_id=run_id,
                          images=images, registry=self.registry)

    # ---------------------------------------------------------------- 幂等/归档
    def _archive_partial_run(self, run_id: str, attempt: int) -> bool:
        """把上次未完成（无 run_summary）的运行目录移到 run_attempts/，返回是否归档。

        只有「有实质内容」的目录才归档；前端预创建的空 events.jsonl 直接复用。
        绝不删除：归档保留完整的事件与产物，便于排查重试前的失败现场。
        """
        run_dir = self.workspace / "runs" / run_id
        if not run_dir.is_dir() or (run_dir / "run_summary.json").exists():
            return False
        progressed = False
        for path in run_dir.rglob("*"):
            try:
                if not path.is_file() or path.stat().st_size == 0:
                    continue
                # queued.json / 空 events.jsonl 是入队占位，不算上次执行的实质进展
                if path.name == "queued.json":
                    continue
                progressed = True
                break
            except OSError:
                continue
        if not progressed:
            return False
        target = self.workspace / "run_attempts" / run_id / f"attempt-{attempt}-{_stamp()}"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(run_dir), str(target))
        except OSError as exc:
            raise RuntimeError(f"归档未完成运行目录失败 {run_dir}: {exc}") from exc
        (target / "ARCHIVED.md").write_text(
            f"# 未完成运行归档\n\n- run_id: {run_id}\n- attempt: {attempt}\n"
            f"- archived_at: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"- 原因：worker 重试前归档上一次中断的运行（无 run_summary.json）\n",
            encoding="utf-8",
        )
        self.log(f"[worker] 已归档未完成运行 {run_id} → {target}")
        return True

    def _cleanup_worker(self) -> None:
        """退出清理：仅当该消费者 PEL 为空时移除消费组内身份（避免组消费者无限累积）。"""
        try:
            self.queue.mq.cleanup_consumer(self.queue.STREAM, self.queue.group, self.worker_id)
        except Exception:  # noqa: BLE001
            pass
        self.worker_registry.unregister(self.worker_id)

    def cleanup(self) -> None:
        """供 ``worker --once`` 等短生命周期调用方显式清理。"""
        self._cleanup_worker()

    def _is_canceled(self, run_id: str) -> bool:
        try:
            return bool(self.registry.is_canceled(run_id))
        except Exception:  # noqa: BLE001 - 注册表抖动时不误取消
            return False

    def _cleanup_canceled(self, run_id: str) -> None:
        """排队中即被取消：补一份 canceled 摘要，面板不再把它显示为「进行中」。"""
        run_dir = self.workspace / "runs" / run_id
        try:
            if run_dir.is_dir() and not (run_dir / "run_summary.json").exists():
                queued: Dict[str, Any] = {}
                queued_path = run_dir / "queued.json"
                if queued_path.exists():
                    try:
                        queued = json.loads(queued_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        queued = {}
                title = str(queued.get("title") or "")
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                summary = {
                    "run_id": run_id, "run_dir": str(run_dir), "requirement_title": title,
                    "verdict": "fail", "steps": 0, "rollbacks": 0, "review_rejections": 0,
                    "bugs_total": 0, "bugs_fixed": 0, "report_path": "", "timeline_path": "",
                    "iterations": {}, "error": "", "forced_fail": False, "canceled": True,
                    "metrics": {
                        "run_id": run_id, "ts": str(queued.get("queued_at") or now),
                        "requirement_title": title, "profile": queued.get("profile"),
                        "canceled": True, "duration_s": 0.0,
                    },
                }
                (run_dir / "run_summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:  # noqa: BLE001 - 收尾记录失败不影响取消
            pass
        finally:
            try:
                self.registry.unregister(run_id)
            except Exception:  # noqa: BLE001
                pass

    def _event(self, event_type: str, run_id: str, payload: Dict[str, Any]) -> None:
        self.queue.publish_worker_event(event_type, run_id=run_id,
                                        payload=payload, sender=self.worker_id)

    def status(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "running": self.running,
            "workspace": str(self.workspace),
            "group": self.queue.group,
            "stream": self.queue.mq.stream_key(self.queue.STREAM),
            "counters": dict(self.counters),
        }
