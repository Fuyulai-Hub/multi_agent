"""框架托管的外部服务生命周期（``full_stack_spa`` 冻结套件专用）。

由框架安装进沙箱（``.mas_verify/mas_testkit.py``），供冻结测试文件 ``import`` 使用。
测试文件本身只发 HTTP 请求，进程管理全部在这里，理由是历史上两类**必然阻断**都出在
「测试自己起服务」这件事上（docs/46 实测）：

1. Windows 下裸名 ``npm`` 起子进程 —— CreateProcess 不查 PATHEXT，必然 FileNotFoundError；
2. ``stdout=PIPE`` 却没人读 —— 服务启动日志（实测 8656 字节）写满匿名管道后，
   子进程永久阻塞在 ``write()``，端口永远不监听；同一份后端 stdout 指向文件时 3.0s 就绪。

本模块的做法：命令经白名单校验与 ``shutil.which`` 解析；stdout/stderr 直接重定向到
框架分配的日志文件（无管道、无死锁、日志留证）；就绪判据是服务打印 ``MAS_UI_PORT=<port>``；
回收时按进程组整树结束（Windows ``taskkill /T /F``，POSIX ``killpg``）。

服务命令由框架通过环境变量注入（来源是测试契约的 ``ui_service_cmd``），测试不得写死端口。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["PORT_MARKER", "Service", "ServiceError", "Response", "start_service"]

PORT_MARKER = "MAS_UI_PORT="
DEFAULT_TIMEOUT = 90.0
_READY_ENV = "MAS_SERVICE_CMD"
_LOG_ENV = "MAS_SERVICE_LOG"
_CWD_ENV = "MAS_SERVICE_CWD"
_ALLOWED_ENV = "MAS_SERVICE_ALLOWED"
#: 开机日志最多回读这么多字节做诊断（不参与就绪判定之外的任何逻辑）
_LOG_TAIL_BYTES = 16384
_PATHEXT_SUFFIXES = (".cmd", ".bat", ".exe", ".com")


class ServiceError(RuntimeError):
    """服务无法启动/无法就绪/响应不可解析。"""


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise ServiceError(
                f"响应不是合法 JSON（status={self.status}）：{self.text[:300]}") from exc


class Service:
    """一次已就绪的外部服务。测试只需 ``service.get("/api/xxx")``。"""

    def __init__(self, process: subprocess.Popen, port: int, log_path: Path, log_file: Any) -> None:
        self.process = process
        self.port = port
        self.log_path = Path(log_path)
        self._log_file = log_file
        self._stopped = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            body = None
            merged = dict(headers or {})
            if json_body is not None:
                body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                merged.setdefault("Content-Type", "application/json")
            conn.request(method.upper(), path, body=body, headers=merged)
            raw = conn.getresponse()
            return Response(raw.status, {k.lower(): v for k, v in raw.getheaders()}, raw.read())
        finally:
            conn.close()

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)

    def logs(self, tail: int = 4000) -> str:
        """服务日志尾部（失败断言时把它写进 message，排障不再靠猜）。"""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-tail:]

    def stop(self) -> None:
        """结束服务进程树并释放日志句柄；幂等。"""
        if self._stopped:
            return
        self._stopped = True
        _terminate(self.process)
        try:
            self._log_file.close()
        except OSError:
            pass

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


def start_service(
    cmd: Optional[Sequence[str]] = None,
    *,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    marker: str = PORT_MARKER,
    env: Optional[Dict[str, str]] = None,
) -> Service:
    """启动被测服务并等到它打印 ``MAS_UI_PORT=<port>``。

    ``cmd`` 省略时使用框架注入的 ``MAS_SERVICE_CMD``（即需求契约里的 ``ui_service_cmd``）。
    失败一律抛 :class:`ServiceError`，message 里带退出码与日志尾部。
    """
    command = _resolve_command(cmd)
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "wb")
    env_full = dict(os.environ)
    if env:
        env_full.update({str(k): str(v) for k, v in env.items()})

    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command, cwd=_resolve_cwd(cwd), env=env_full,
            stdout=log_file, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **kwargs,
        )
    except OSError as exc:
        log_file.close()
        raise ServiceError(f"服务启动失败（{command[0]}）：{exc}") from exc

    try:
        port = _await_ready(process, log_path, marker, float(timeout or DEFAULT_TIMEOUT))
    except BaseException:
        _terminate(process)
        log_file.close()
        raise
    return Service(process, port, log_path, log_file)


# ---------------------------------------------------------------- 内部实现
def _resolve_command(cmd: Optional[Sequence[str]]) -> List[str]:
    if cmd is None:
        raw = str(os.environ.get(_READY_ENV) or "").strip()
        if not raw:
            raise ServiceError(
                f"未提供服务启动命令：框架应向测试进程注入 {_READY_ENV}"
                "（来源是测试契约的 ui_service_cmd，需声明为字符串数组）")
        cmd = json.loads(raw)
    if isinstance(cmd, str) or not isinstance(cmd, (list, tuple)) or not cmd:
        raise ServiceError(f"服务启动命令必须是字符串数组，实际为：{cmd!r}")
    parts = [str(part) for part in cmd]
    executable = shutil.which(parts[0])
    if not executable:
        raise ServiceError(f"服务启动命令不可用：未找到可执行文件 {parts[0]!r}")
    parts[0] = executable
    _check_allowed(parts[0])
    return parts


def _check_allowed(executable: str) -> None:
    """命令白名单由框架注入（``MAS_SERVICE_ALLOWED``），与沙箱同一条口径。"""
    allowed = [item.strip().lower() for item in
               str(os.environ.get(_ALLOWED_ENV) or "").split(",") if item.strip()]
    if not allowed:
        return
    name = Path(executable).name.lower()
    for suffix in _PATHEXT_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name not in allowed:
        raise ServiceError(
            f"服务启动命令被拒绝：{Path(executable).name!r} 不在沙箱命令白名单 {allowed} 内"
            "（runtime.sandbox_command_policy.allowed）")


def _resolve_cwd(cwd: Optional[str]) -> str:
    """服务工作目录：显式参数 > 框架注入的交付根 > 当前目录。

    隐藏套件的 runner 以冻结目录为 cwd（用例从那里加载），服务必须回到交付根启动，
    否则相对路径的入口文件（``node server.js``）会解析到冻结目录里。
    """
    return str(cwd or os.environ.get(_CWD_ENV) or os.getcwd())


def _log_path() -> Path:
    configured = str(os.environ.get(_LOG_ENV) or "").strip()
    if configured:
        return Path(configured)
    return Path(os.getcwd()) / ".mas_verify_service.log"


def _await_ready(process: subprocess.Popen, log_path: Path, marker: str, timeout: float) -> int:
    pattern = re.compile(re.escape(marker) + r"(\d+)")
    deadline = time.time() + timeout
    offset = 0
    while time.time() < deadline:
        if process.poll() is not None:
            raise ServiceError(
                f"被测服务提前退出（exit={process.returncode}）：{_tail(log_path)}")
        chunk, offset = _read_new(log_path, offset)
        match = pattern.search(chunk)
        if match:
            return int(match.group(1))
        time.sleep(0.05)
    raise ServiceError(
        f"服务启动超时（{timeout:.0f}s 内未打印 {marker}<port>）：{_tail(log_path)}")


def _read_new(log_path: Path, offset: int) -> tuple:
    try:
        with open(log_path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return "", offset
    return data.decode("utf-8", errors="replace"), offset + len(data)


def _tail(log_path: Path) -> str:
    try:
        data = log_path.read_bytes()[-_LOG_TAIL_BYTES:]
    except OSError:
        return "(无日志)"
    text = data.decode("utf-8", errors="replace").strip()
    return text or "(无输出)"


def _terminate(process: subprocess.Popen, timeout: float = 5.0) -> None:
    """结束整棵进程树：dev server 常再 fork 子进程，只杀父进程会留下占端口的孤儿。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


if __name__ == "__main__":      # pragma: no cover - 手工排查用
    service = start_service()
    print(json.dumps({"base_url": service.base_url, "pid": service.pid}))
    print("CTRL+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        service.stop()
        sys.exit(0)
