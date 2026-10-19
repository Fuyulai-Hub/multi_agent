"""跨进程文件锁（零依赖，标准库实现）。

用途：让同一共享卷（workspace 目录）上的多个进程/实例安全地读写同一份状态文件
（黑板产物 latest.json、经验库 experience.jsonl），避免「后写覆盖先写」丢失数据。

平台实现：
- Windows: msvcrt.locking（LF 单区锁）
- POSIX:   fcntl.flock（LOCK_EX）

语义说明：
- 锁粒度是「一个 .lock 伴随文件」，不是目录锁；对 JSONL 这类 append/覆写文件足够；
- 获取失败（超时/争抢）时抛 FileLockTimeout，调用方应把异常当作「并发冲突」处理，
  而不是静默重试——静默重试会把冲突藏起来；
- 锁文件本身（*.lock）不承载业务数据，可随时删除，不影响已锁定的目标文件。
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

__all__ = ["FileLockTimeout", "file_lock"]


class FileLockTimeout(RuntimeError):
    """在超时时间内没能拿到文件锁（通常意味着另一个进程正持有它）。"""


@contextmanager
def file_lock(target: Path, *, timeout_s: float = 10.0, poll_s: float = 0.05) -> Iterator[Path]:
    """对 target 文件加排他锁：`with file_lock(path): ...` 期间独占读写权。

    target 可以尚不存在（锁文件会先被创建）。锁释放是自动的（退出 with 块），
    即使块内抛异常也会释放，不会留下死锁。
    """
    path = Path(target)
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout_s)
    handle = None
    try:
        while True:
            try:
                # open 也必须纳入重试：Windows 上并发打开同一 .lock 文件偶发
                # Errno 13 Permission denied（杀软/索引器瞬时持有句柄、打开竞争），
                # 原实现 open 在循环外，这类瞬时错误会直接穿透，绕过本函数的
                # 「获取失败 → 重试 → 超时抛 FileLockTimeout」语义
                # （实测：test_concurrent_runs_isolated_two_instances 约 10~20% 概率触发）。
                if handle is None:
                    handle = open(lock_path, "a+b")
                # msvcrt.locking 锁的是「当前文件位置起 nbytes」，必须归一化到 0，
                # 否则不同进程位置不一致会锁到不同区段，失去互斥语义（flock 不受位置影响）。
                handle.seek(0)
                _acquire(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(
                        f"获取文件锁超时（{timeout_s}s）：{lock_path}（另一进程可能正在写入）"
                    )
                time.sleep(poll_s)
        try:
            yield path
        finally:
            _release(handle)
    finally:
        if handle is not None:
            handle.close()


def _acquire(handle) -> None:
    if os.name == "nt":  # Windows: msvcrt 锁定 [0, 1) 单字节区即等效排他锁
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # POSIX: flock 排他
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # 进程退出时操作系统会代为释放；解锁失败不应掩盖块内真正的异常
