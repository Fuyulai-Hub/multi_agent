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

__all__ = ["RunWorker", "RunLease", "PermanentTaskError", "make_worker_id"]


class PermanentTaskError(RuntimeError):
    """重试也不会成功的任务错误（payload 非法 / 工作区不匹配 / 配置缺失）。"""


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class RunLease:
    """运行租约：同一 run 同时只允许一个 worker 执行。

    - ``acquire`` 失败 = 另一个 worker 持有租约；调用方不得执行，也不得 ACK
      （消息留在 PEL，等持有者完成或崩溃后由语义重新投递）；
    - 持锁期间守护线程周期性续租，并顺带刷新运行注册表心跳——单个 LLM 节点
      可能远超 90s 的心跳 TTL，只在节点边界心跳会让面板误判运行已死亡；
    - Redis 抖动时降级为「不阻塞执行」（MQ 本身也在同一次故障里），但会记录
      ``degraded`` 原因，供 worker 事件与日志取证。
    """

    def __init__(
        self,
        backend: Any,
        run_id: str,
        *,
        worker_id: str,
        ttl_ms: int = 300000,
        renew_s: float = 30.0,
        registry: Any = None,
        log: Any = print,
    ) -> None:
        self.backend = backend
        self.run_id = run_id
        self.worker_id = worker_id
        self.ttl_ms = int(ttl_ms)
        self.renew_s = max(0.05, float(renew_s))
        self.registry = registry
        self.log = log
        self.token: Optional[str] = None
        self.lost = False
        self.degraded = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return self.backend is not None and self.token is not None

    def acquire(self) -> bool:
        if self.backend is None:
            return True                      # 离线队列（测试替身）没有租约原语
        try:
            self.token = self.backend.acquire_lock(f"run:{self.run_id}", ttl_ms=self.ttl_ms)
        except Exception as exc:  # noqa: BLE001 - Redis 抖动时降级，不让运行停摆
            self.degraded = f"{type(exc).__name__}: {exc}"
            self.log(f"[worker] 运行租约不可用，降级执行 {self.run_id}：{self.degraded}")
            return True
        return self.token is not None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._renew_loop, name=f"mas-lease-{self.run_id[:12]}", daemon=True
        )
        self._thread.start()

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.renew_s):
            try:
                renewed = self.backend.renew_lock(f"run:{self.run_id}", self.token, self.ttl_ms)
            except Exception as exc:  # noqa: BLE001 - 网络抖动交给下一轮重试
                self.log(f"[worker] 租约续租失败 {self.run_id}：{type(exc).__name__}: {exc}")
                continue
            if not renewed:
                self.lost = True
                self.log(f"[worker] 运行租约已丢失 {self.run_id}（可能已过期并被其他 worker 接管）")
                return
            if self.registry is not None:
                try:
                    info = self.registry.get(self.run_id) or {}
                    self.registry.heartbeat(
                        self.run_id, status="running", node=info.get("node")
                    )
                except Exception:  # noqa: BLE001 - 心跳失败不影响任务
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.renew_s + 1))
            self._thread = None
        if self.token is not None and self.backend is not None:
            try:
                self.backend.release_lock(f"run:{self.run_id}", self.token)
            except Exception:  # noqa: BLE001 - 释放失败靠 TTL 兜底
                pass
            self.token = None


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
        self.lease_ttl_ms = max(30000, int(float(settings.get("lease_ttl_s", 300)) * 1000))
        self.lease_renew_s = max(5.0, float(settings.get("lease_renew_s", 30)))
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
        lease = RunLease(
            getattr(self.queue.mq, "backend", None), run_id,
            worker_id=self.worker_id, ttl_ms=self.lease_ttl_ms,
            renew_s=self.lease_renew_s, registry=self.registry, log=self.log,
        )
        if not lease.acquire():
            # 另一个 worker 正在执行同一 run：不 ACK、不进重试，让消息留在 PEL。
            # 持有者完成后会 ACK；若它崩溃，租约与 PEL 可见性超时后再接管。
            self.counters["skipped"] += 1
            self._event("worker_task_lease_busy", run_id,
                        {"worker_id": self.worker_id, "attempt": attempt,
                         "reason": "同一运行的租约被其他 worker 持有"})
            self.log(f"[worker] 跳过 {run_id}：租约被其他 worker 持有（消息保留在 PEL）")
            return "lease_busy"
        lease.start()
        self._event("worker_task_started", run_id,
                    {"worker_id": self.worker_id, "attempt": attempt,
                     "lease": "degraded" if lease.degraded else ("locked" if lease.enabled else "none"),
                     "lease_error": lease.degraded,
                     "queued_at": (delivery.get("headers") or {}).get("enqueued_at")})
        self.log(f"[worker] 开始任务 {run_id}（attempt {attempt}/{self.queue.max_attempts}）")
        started = time.time()
        try:
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
        finally:
            lease.stop()
        if lease.lost:
            self._event("worker_task_lease_lost", run_id,
                        {"worker_id": self.worker_id, "attempt": attempt,
                         "note": "租约在任务结束前已丢失；结果已落盘，但可能与其他 worker 重叠执行"})
            self.log(f"[worker] 警告：{run_id} 的租约在结束前丢失")

        elapsed = round(time.time() - started, 2)
        self.queue.ack(delivery)
        self.queue.mq.delete_secret(run_id)
        self.counters["finished"] += 1
        self._event("worker_task_finished", run_id,
                    {"worker_id": self.worker_id, "verdict": getattr(summary, "verdict", None),
                     "steps": getattr(summary, "steps", None), "duration_s": elapsed,
                     "attempt": attempt, "lease_lost": lease.lost})
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

        # 有可恢复检查点时续跑，不再归档半截目录；否则按既有逻辑归档后重跑。
        resume = self._is_resumable(run_id)
        if resume:
            self._event("worker_task_resumed", run_id,
                        {"worker_id": self.worker_id, "attempt": attempt})
            self.log(f"[worker] 从检查点续跑 {run_id}")
        elif self._archive_partial_run(run_id, attempt):
            try:
                self.queue.mq.reset_run_streams(run_id)
            except Exception:  # noqa: BLE001 - 流清理失败不影响重跑
                pass

        images = payload.get("images") or None
        leader = Leader(config)
        try:
            if requirement_path:
                return leader.run(requirement_path, as_text=False, run_id=run_id,
                                  images=images, registry=self.registry, resume=resume)
            return leader.run(requirement, as_text=True, run_id=run_id,
                              images=images, registry=self.registry, resume=resume)
        except ConfigError as exc:
            # 检查点/需求 hash 不一致属于不可重试：重跑只会得到同一个错误
            raise PermanentTaskError(f"运行无法继续（配置/检查点）：{exc}") from exc

    # ---------------------------------------------------------------- 幂等/归档
    @staticmethod
    def _runs_dir(workspace: Path) -> Path:
        return workspace / "runs"

    def _is_resumable(self, run_id: str) -> bool:
        """checkpoint.status == running 且有需求快照 → 可续跑。"""
        path = self._runs_dir(self.workspace) / run_id / "checkpoint.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (str(data.get("run_id") or "") == run_id
                and str(data.get("status") or "") == "running"
                and (self._runs_dir(self.workspace) / run_id / "input" / "requirement.md").exists())

    def _archive_partial_run(self, run_id: str, attempt: int) -> bool:
        """把上次未完成（无 run_summary）的运行目录移到 run_attempts/，返回是否归档。

        只有「有实质内容」的目录才归档；前端预创建的空 events.jsonl 直接复用。
        绝不删除：归档保留完整的事件与产物，便于排查重试前的失败现场。
        可续跑的运行（检查点 status=running）不归档——否则续跑起点就没了。
        """
        run_dir = self._runs_dir(self.workspace) / run_id
        if not run_dir.is_dir() or (run_dir / "run_summary.json").exists():
            return False
        if self._is_resumable(run_id):
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
