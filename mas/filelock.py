"""跨进程文件锁：封装 ``filelock``（https://github.com/tox-dev/filelock）。

用途：让同一共享卷（workspace 目录）上的多个进程/实例安全地读写同一份状态文件
（黑板产物 latest.json、经验库 experience.jsonl），避免「后写覆盖先写」丢失数据。

实现说明（2026-11-25 重构）：锁原语不再自研，交给 ``filelock``——它已覆盖
Windows（``msvcrt.locking``）与 POSIX（``fcntl.flock``）的差异、打开失败重试
（Windows 杀软/索引器瞬时持有句柄导致的 PermissionError）与超时轮询。
本模块只保留项目自己的语义：

- 锁粒度是「一个 ``.lock`` 伴随文件」，不是目录锁；对 JSONL 这类 append/覆写文件足够；
- 获取超时抛 ``FileLockTimeout``，调用方应把它当作「并发冲突」处理，而不是静默重试；
- 锁文件本身不承载业务数据，可随时删除，不影响已锁定的目标文件。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout

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
    lock = FileLock(str(lock_path), timeout=max(0.0, timeout_s), poll_interval=poll_s)
    try:
        lock.acquire()
    except Timeout as exc:
        raise FileLockTimeout(
            f"获取文件锁超时（{timeout_s}s）：{lock_path}（另一进程可能正在写入）"
        ) from exc
    try:
        yield path
    finally:
        lock.release()
