"""MCP（Model Context Protocol）服务配置与最小客户端。

配置落在 `config/mcp.yaml`，一个服务一条：

```yaml
servers:
  - name: demo                 # 服务名称（唯一，[a-z0-9-]+）
    transport: stdio           # stdio | sse
    enabled: true
    command: python            # stdio 必填
    args: ["server.py"]
    env:                       # 追加到子进程环境
      TOKEN: "${MY_TOKEN}"     # ${VAR} 运行时从环境变量展开，不落明文
    timeout_s: 10
  - name: remote
    transport: sse
    enabled: false
    url: https://example.com/sse   # sse 必填
    headers: {Authorization: "Bearer ${TOKEN}"}
```

实现方式（2026-11-25 重构）：传输与 JSON-RPC 交给官方 `mcp` SDK
（`stdio_client` / `sse_client` + `ClientSession`），本模块只保留项目语义：
配置 CRUD、`${VAR}` 展开、同步门面、五阶段错误定位。SDK 是 asyncio 接口，
而 MAS 调度是同步线程，因此 `_AsyncSession` 用 anyio 的 BlockingPortal
承载事件循环做桥接；本模块不再解析任何 JSON-RPC 帧。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import anyio
from anyio.from_thread import start_blocking_portal

import yaml
from mcp import ClientSession, types as mcp_types
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client

from . import __version__
from .config import SystemConfig, mask_secret
from .errors import ConfigError

__all__ = [
    "MCP_FILE",
    "TRANSPORTS",
    "MCPClient",
    "load_servers",
    "save_servers",
    "get_server",
    "add_server",
    "remove_server",
    "expand_env",
    "validate_server",
    "check_server",
    "describe_server",
]

MCP_FILE = "mcp.yaml"
TRANSPORTS = ("stdio", "sse")
DEFAULT_TIMEOUT_S = 10

_NAME = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# --------------------------------------------------------------------------- 环境变量
def expand_env(value: Any, *, origin: str = "") -> str:
    """展开 `${VAR}` 引用；变量未设置时报错并指出变量名（避免静默拿到空串）。"""
    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"环境变量未设置: {name}（被 {origin or '配置'} 引用）")
        return os.environ[name]

    return _ENV_REF.sub(replace, str(value))


# --------------------------------------------------------------------------- 配置读写
def mcp_path(config: SystemConfig) -> Path:
    return config.config_dir / MCP_FILE


def load_servers(config: SystemConfig) -> List[Dict[str, Any]]:
    """读取服务清单；文件不存在时返回空列表（未配置 MCP 属正常情况）。"""
    path = mcp_path(config)
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} 解析失败: {exc}") from exc
    servers = raw.get("servers") or []
    if not isinstance(servers, list):
        raise ConfigError(f"{path} 的 servers 必须是列表")
    return [dict(item) for item in servers]


def save_servers(config: SystemConfig, servers: List[Dict[str, Any]]) -> Path:
    path = mcp_path(config)
    header = (
        "# =============================================================================\n"
        "# MCP 服务配置（服务名称 / 传输方式 / 启动命令 / 参数 / 环境变量）\n"
        "#\n"
        "# transport: stdio —— 本地子进程，command 必填，args 为参数列表\n"
        "# transport: sse   —— 远端事件流，url 必填\n"
        "# env / headers 中的 ${VAR} 在运行时从环境变量展开，避免明文落盘\n"
        "# 新增后可用 `python -m mas mcp check <name>` 做参数校验与握手冒烟\n"
        "# =============================================================================\n"
    )
    body = yaml.safe_dump({"servers": servers}, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body, encoding="utf-8")
    return path


def get_server(config: SystemConfig, name: str) -> Dict[str, Any]:
    for server in load_servers(config):
        if str(server.get("name")) == name:
            return server
    raise ConfigError(f"未配置的 MCP 服务: {name}（可用 `python -m mas mcp list` 查看）")


def add_server(config: SystemConfig, server: Dict[str, Any], *, overwrite: bool = False) -> Path:
    problems = validate_server(server)
    if problems:
        raise ConfigError("MCP 服务配置非法：" + "；".join(problems))
    servers = load_servers(config)
    name = str(server["name"])
    for index, existing in enumerate(servers):
        if str(existing.get("name")) == name:
            if not overwrite:
                raise ConfigError(f"MCP 服务已存在: {name}（如需覆盖请显式指定 --overwrite）")
            servers[index] = server
            break
    else:
        servers.append(server)
    return save_servers(config, servers)


def remove_server(config: SystemConfig, name: str) -> Path:
    servers = load_servers(config)
    kept = [item for item in servers if str(item.get("name")) != name]
    if len(kept) == len(servers):
        raise ConfigError(f"未配置的 MCP 服务: {name}")
    return save_servers(config, kept)


def validate_server(raw: Dict[str, Any]) -> List[str]:
    """参数校验，返回问题列表（为空即通过）。"""
    problems: List[str] = []
    name = str(raw.get("name") or "")
    if not name:
        problems.append("缺少必填字段: name")
    elif not _NAME.match(name):
        problems.append(f"服务名称非法: {name}（只允许小写字母、数字与连字符）")

    transport = str(raw.get("transport") or "")
    if not transport:
        problems.append("缺少必填字段: transport")
    elif transport not in TRANSPORTS:
        problems.append(f"transport 非法: {transport}（{' | '.join(TRANSPORTS)}）")

    if transport == "stdio":
        if not str(raw.get("command") or ""):
            problems.append("transport=stdio 时必须配置 command")
        args = raw.get("args")
        if args is not None and (not isinstance(args, list) or not all(isinstance(a, str) for a in args)):
            problems.append("args 必须是字符串列表")
    elif transport == "sse":
        url = str(raw.get("url") or "")
        if not url:
            problems.append("transport=sse 时必须配置 url")
        elif not url.startswith(("http://", "https://")):
            problems.append(f"url 必须是 http(s):// 开头: {url}")

    env = raw.get("env")
    if env is not None:
        if not isinstance(env, dict):
            problems.append("env 必须是映射(dict)")
        elif not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            problems.append("env 的键与值都必须是字符串")

    headers = raw.get("headers")
    if headers is not None and not isinstance(headers, dict):
        problems.append("headers 必须是映射(dict)")

    try:
        timeout = int(raw.get("timeout_s", DEFAULT_TIMEOUT_S))
        if timeout <= 0:
            problems.append("timeout_s 必须是正整数")
    except (TypeError, ValueError):
        problems.append("timeout_s 必须是整数")

    return problems


def describe_server(server: Dict[str, Any]) -> str:
    """服务详情（env / headers 中的值一律脱敏）。"""
    lines = [
        f"服务      : {server.get('name')}",
        f"传输方式  : {server.get('transport')}（enabled={server.get('enabled', True)}）",
        f"超时      : {server.get('timeout_s', DEFAULT_TIMEOUT_S)}s",
    ]
    if str(server.get("transport")) == "stdio":
        lines.append(f"启动命令  : {server.get('command')} {' '.join(server.get('args') or [])}".rstrip())
        env = server.get("env") or {}
        lines.append(f"环境变量({len(env)}):")
        lines.extend(f"  - {key}={mask_secret(str(value))}" for key, value in env.items())
    else:
        lines.append(f"地址      : {server.get('url')}")
        headers = server.get("headers") or {}
        lines.append(f"请求头({len(headers)}):")
        lines.extend(f"  - {key}: {mask_secret(str(value))}" for key, value in headers.items())
    return "\n".join(lines)


# --------------------------------------------------------------------------- 传输层
class _StderrLog:
    """把 MCP 子进程的 stderr 落到临时文件，失败时读取末尾片段。

    SDK 的 ``errlog`` 会直接作为子进程 stderr 传给 anyio，必须是带真实文件描述符
    的文件对象（``io.StringIO`` 会报 UnsupportedOperation: fileno），因此这里用
    命名临时文件，而不是内存缓冲。
    """

    def __init__(self, limit: int = 400) -> None:
        self._limit = limit
        handle, name = tempfile.mkstemp(prefix="mas-mcp-", suffix=".log")
        os.close(handle)
        self._path = Path(name)
        self._handle = self._path.open("wb")

    def stream(self) -> Any:
        return self._handle

    def tail(self) -> str:
        try:
            self._handle.flush()
            raw = self._path.read_bytes()
        except (OSError, ValueError):
            return ""
        return raw.decode("utf-8", errors="replace")[-self._limit:].strip()

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass
        try:
            self._path.unlink()
        except OSError:
            pass


def _leaf_errors(exc: BaseException) -> List[BaseException]:
    """把 anyio / 官方 SDK 的 ExceptionGroup 摊平成叶子异常。"""
    if isinstance(exc, BaseExceptionGroup):
        leaves: List[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(_leaf_errors(inner))
        return leaves
    return [exc]


class _AsyncSession:
    """把官方 SDK 的异步会话桥接成同步调用。

    MAS 的调度、工具循环与取证都是同步代码；官方 ``mcp`` 客户端是 asyncio。
    这里用 anyio 的 ``BlockingPortal``（成熟的事件循环线程桥）承载长生命周期的
    ``stdio_client`` / ``sse_client`` + ``ClientSession``；本模块不再自维护
    事件循环与线程生命周期。

    上下文必须由**同一个任务**进入和退出（anyio 的 cancel scope 有任务归属），
    因此 ``_main`` 在 portal 里常驻，sync 侧只通过 ``portal.call`` 发子协程。
    """

    def __init__(self, factory: Any, timeout_s: float, *, command: str = "",
                 log_tail: Any = None) -> None:
        self._factory = factory
        self._timeout = float(timeout_s)
        self._command = command
        self._log_tail = log_tail
        self._portal_cm: Any = None
        self._portal: Any = None
        self._task: Any = None
        self._stop: Optional[anyio.Event] = None
        self._ready = threading.Event()
        self._session: Optional[ClientSession] = None
        self._error: Optional[BaseException] = None
        self._stopping = False

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        try:
            self._portal_cm = start_blocking_portal(backend="asyncio", name="mcp-session")
            self._portal = self._portal_cm.__enter__()
            self._task = self._portal.start_task_soon(self._main)
        except BaseException as exc:          # noqa: BLE001 - 统一转 ConfigError
            self._error = exc
            self.close()
            raise ConfigError(self._describe(exc)) from exc
        if not self._ready.wait(self._timeout):
            self.close()
            raise ConfigError(f"MCP 服务 {self._timeout}s 内未就绪")
        if self._error is not None:
            message = self._describe(self._error)
            self.close()
            raise ConfigError(message)

    async def _main(self) -> None:
        self._stop = anyio.Event()
        try:
            async with self._factory() as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(
                    read, write, read_timeout_seconds=self._timeout,
                    client_info=mcp_types.Implementation(name="mas", version=__version__),
                ) as session:
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as exc:          # noqa: BLE001 - 转交同步侧
            if not self._stopping:
                self._error = exc
            self._ready.set()

    def close(self) -> None:
        self._stopping = True
        if self._portal is not None and self._stop is not None:
            try:
                self._portal.call(self._set_stop)
            except BaseException:             # noqa: BLE001 - 关闭失败不掩盖原错误
                pass
        if self._task is not None:
            try:
                self._task.result(timeout=5)
            except BaseException:             # noqa: BLE001 - 已取消/超时都继续收尾
                pass
        if self._portal is not None:
            try:
                self._portal.call(self._portal.stop, True)
            except BaseException:             # noqa: BLE001
                pass
        if self._portal_cm is not None:
            try:
                self._portal_cm.__exit__(None, None, None)
            except BaseException:             # noqa: BLE001
                pass
        self._portal = None
        self._portal_cm = None
        self._task = None
        self._session = None

    async def _set_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    # ---------------------------------------------------------------- 调用
    def call(self, fn: Any) -> Any:
        if self._error is not None:
            raise ConfigError(self._describe(self._error))
        if self._session is None or self._portal is None:
            raise ConfigError("MCP 会话尚未建立")
        try:
            return self._portal.call(self._call_with_timeout, fn, self._session)
        except BaseException as exc:          # noqa: BLE001 - 统一转 ConfigError
            raise ConfigError(self._describe(exc)) from exc

    async def _call_with_timeout(self, fn: Any, session: ClientSession) -> Any:
        with anyio.fail_after(self._timeout):
            return await fn(session)

    def _describe(self, exc: BaseException) -> str:
        leaves = _leaf_errors(exc)
        for leaf in leaves:
            if isinstance(leaf, FileNotFoundError):
                return f"启动命令不存在: {self._command}"
            if isinstance(leaf, PermissionError):
                return f"启动 MCP 服务被拒绝: {self._command}（{leaf}）"
            if isinstance(leaf, TimeoutError):
                return f"MCP 服务 {self._timeout}s 内无响应"
        text = "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves) or str(exc)
        message = f"MCP 服务错误: {text}"
        tail = self._log_tail() if self._log_tail is not None else ""
        if tail:
            message += f"（stderr 末尾：{tail}）"
        return message


# --------------------------------------------------------------------------- 客户端
class MCPClient:
    """官方 MCP SDK 的同步门面：start / initialize / list_tools / call_tool / close。"""

    def __init__(self, server: Dict[str, Any]) -> None:
        self.server = server
        self.name = str(server.get("name", ""))
        self.timeout = int(server.get("timeout_s", DEFAULT_TIMEOUT_S))
        transport = str(server.get("transport", ""))
        self._log: Optional[_StderrLog] = None
        if transport == "stdio":
            origin = f"MCP 服务 {self.name}"
            command = expand_env(server.get("command", ""), origin=f"{origin}.command")
            args = [expand_env(item, origin=f"{origin}.args") for item in (server.get("args") or [])]
            env = dict(os.environ)
            env.update({str(k): expand_env(v, origin=f"{origin}.env.{k}")
                        for k, v in (server.get("env") or {}).items()})
            # Windows 子进程默认按 locale（GBK）写 stdout；SDK 按 UTF-8 解码会直接崩。
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")
            params = StdioServerParameters(
                command=command, args=args, env=env,
                cwd=server.get("cwd") or None,
                encoding="utf-8", encoding_error_handler="replace",
            )
            self._log = _StderrLog()
            self._session = _AsyncSession(
                lambda: stdio_client(params, errlog=self._log.stream()), self.timeout,
                command=command, log_tail=self._log.tail,
            )
        elif transport == "sse":
            origin = f"MCP 服务 {self.name}"
            url = expand_env(server.get("url", ""), origin=f"{origin}.url")
            headers = {str(k): expand_env(v, origin=f"{origin}.headers.{k}")
                       for k, v in (server.get("headers") or {}).items()}
            self._session = _AsyncSession(
                lambda: sse_client(url, headers=headers, timeout=self.timeout,
                                   sse_read_timeout=max(300.0, float(self.timeout))),
                self.timeout,
            )
        else:
            raise ConfigError(f"不支持的传输方式: {transport}（{' | '.join(TRANSPORTS)}）")

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def start(self) -> None:
        try:
            self._session.start()
        except ConfigError:
            if self._log is not None:
                self._log.close()       # 启动失败也要回收临时 stderr 文件
            raise

    def close(self) -> None:
        self._session.close()
        if self._log is not None:
            self._log.close()

    def stderr_tail(self, limit: int = 400) -> str:
        return self._log.tail()[:limit] if self._log is not None else ""

    # ---------------------------------------------------------------- 协议
    def initialize(self) -> Dict[str, Any]:
        result = self._session.call(lambda session: session.initialize())
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._session.call(lambda session: session.list_tools())
        return [tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                for tool in result.tools]

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._session.call(
            lambda session: session.call_tool(name, arguments or {})
        )
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)


# --------------------------------------------------------------------------- 装载与冒烟
def check_server(
    config: SystemConfig,
    name: str,
    *,
    call: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """自动装载：解析 → 参数校验 → 连接 → 握手 → 冒烟，失败时给出阶段与修复建议。"""
    report: Dict[str, Any] = {
        "ok": False,
        "kind": "mcp",
        "name": name,
        "stage": "",
        "checks": [],
        "reason": "",
        "hint": "",
        "tools": [],
    }

    def fail(stage: str, reason: str, hint: str) -> Dict[str, Any]:
        report["stage"] = stage
        report["reason"] = reason
        report["hint"] = hint
        report["checks"].append({"stage": stage, "ok": False, "detail": reason})
        return report

    try:
        server = get_server(config, name)
    except ConfigError as exc:
        return fail("parse", str(exc), f"确认 {mcp_path(config)} 中存在 name={name} 的服务条目")
    report["checks"].append({"stage": "parse", "ok": True, "detail": f"transport={server.get('transport')}"})

    problems = validate_server(server)
    if problems:
        return fail("validate", "参数校验未通过：" + "；".join(problems),
                    "stdio 需 command，sse 需 http(s):// 开头的 url；名称只允许小写字母、数字与连字符")
    report["checks"].append({"stage": "validate", "ok": True, "detail": "参数校验通过"})

    try:
        client = MCPClient(server)
        client.start()
    except ConfigError as exc:
        hint = ("把 command 换成绝对路径，或确认该命令已安装并加入 PATH"
                if str(server.get("transport")) == "stdio"
                else "确认 url 可达、服务已启动，且 headers 中的令牌有效")
        return fail("connect", str(exc), hint)

    stage = "handshake"
    try:
        client.initialize()
        report["checks"].append({"stage": "handshake", "ok": True, "detail": "initialize 握手成功"})
        stage = "smoke"
        tools = client.list_tools()
        tool_names = [str(tool.get("name")) for tool in tools]
        report["tools"] = tool_names
        report["checks"].append({
            "stage": "smoke", "ok": True,
            "detail": f"tools/list 返回 {len(tool_names)} 个工具" + (f"：{', '.join(tool_names)}" if tool_names else ""),
        })
        if call:
            result = client.call_tool(call, params)
            report["call_result"] = result
            report["checks"].append({"stage": "smoke", "ok": True, "detail": f"tools/call {call} 调用成功"})
    except ConfigError as exc:
        report["stage"] = stage
        report["reason"] = str(exc)
        report["hint"] = "适当调大 timeout_s；stdio 服务可在 stderr 中查看启动失败原因"
        report["checks"].append({"stage": stage, "ok": False, "detail": str(exc)})
        return report
    finally:
        client.close()

    report["ok"] = True
    report["stage"] = "done"
    return report
