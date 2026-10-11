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

实现取舍：项目唯一依赖是 PyYAML，不引入 MCP SDK，因此这里只实现健康检查所需的
三个方法（`initialize` / `tools/list` / `tools/call`），不做完整规范兼容。
- stdio：子进程 + 换行分隔的 JSON-RPC 2.0；
- sse：GET 建立事件流拿 POST 端点，POST 发请求，再从事件流读回响应。
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

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
PROTOCOL_VERSION = "2024-11-05"
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
class _StdioTransport:
    """stdio：子进程 + 换行分隔的 JSON-RPC。"""

    def __init__(self, command: str, args: List[str], env: Dict[str, str], timeout: int) -> None:
        self.command = command
        self.args = args
        self.env = env
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        merged = dict(os.environ)
        merged.update(self.env)
        merged.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            self._proc = subprocess.Popen(
                [self.command] + list(self.args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=merged,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise ConfigError(f"启动命令不存在: {self.command}（{exc}）") from exc
        except OSError as exc:
            raise ConfigError(f"启动 MCP 服务失败: {self.command}（{exc}）") from exc
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self._lines.put(line)
        except (ValueError, OSError):
            pass
        self._lines.put(None)

    def send(self, message: Dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def receive(self) -> str:
        line = self._lines.get(timeout=self.timeout)
        if line is None:
            raise ConfigError("MCP 服务进程已退出（stdout 关闭）")
        return line

    def stderr_tail(self, limit: int = 400) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            self._proc.wait(timeout=0.2)
            text = self._proc.stderr.read() or ""
        except subprocess.TimeoutExpired:
            return ""
        return text[-limit:]

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try:
                self._proc.kill()
            except OSError:
                pass
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


class _SSETransport:
    """sse：GET 建立事件流取 POST 端点，POST 发请求，再从事件流读回响应。"""

    def __init__(self, url: str, headers: Dict[str, str], timeout: int) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self._conn: Optional[http.client.HTTPConnection] = None
        self._resp: Optional[http.client.HTTPResponse] = None
        self._post_url = ""

    def start(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        if not parsed.hostname:
            raise ConfigError(f"url 缺少主机名: {self.url}")
        factory = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        try:
            self._conn = factory(parsed.hostname, parsed.port or None, timeout=self.timeout)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            self._conn.request("GET", path, headers={"Accept": "text/event-stream", **self.headers})
            self._resp = self._conn.getresponse()
        except OSError as exc:
            raise ConfigError(f"无法连接 MCP 服务: {self.url}（{exc}）") from exc
        if self._resp.status >= 400:
            raise ConfigError(f"MCP 服务返回 HTTP {self._resp.status}: {self.url}")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            event, data = self._read_event()
            if event == "endpoint" and data:
                self._post_url = urllib.parse.urljoin(self.url, data)
                return
        raise ConfigError(f"SSE 握手超时：{self.timeout}s 内未收到 endpoint 事件")

    def _read_event(self) -> Tuple[str, str]:
        assert self._resp is not None
        event, data_lines = "", []
        while True:
            raw = self._resp.readline()
            if not raw:
                raise ConfigError("SSE 连接已关闭（未读到完整响应）")
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            line = line.rstrip("\r\n")
            if not line:
                if event or data_lines:
                    return event, "\n".join(data_lines)
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())

    def send(self, message: Dict[str, Any]) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._post_url, data=body,
            headers={"Content-Type": "application/json", **self.headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.URLError as exc:
            raise ConfigError(f"向 MCP 服务发送请求失败: {self._post_url}（{exc}）") from exc

    def receive(self) -> str:
        _, data = self._read_event()
        return data

    def stderr_tail(self, limit: int = 400) -> str:
        return ""

    def close(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            except OSError:
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- 客户端
class MCPClient:
    """MCP 最小客户端（只实现健康检查所需的三个方法）。"""

    def __init__(self, server: Dict[str, Any]) -> None:
        self.server = server
        self.name = str(server.get("name", ""))
        self.timeout = int(server.get("timeout_s", DEFAULT_TIMEOUT_S))
        transport = str(server.get("transport", ""))
        if transport == "stdio":
            origin = f"MCP 服务 {self.name}"
            self._transport: Any = _StdioTransport(
                command=expand_env(server.get("command", ""), origin=f"{origin}.command"),
                args=[expand_env(item, origin=f"{origin}.args") for item in (server.get("args") or [])],
                env={str(k): expand_env(v, origin=f"{origin}.env.{k}")
                     for k, v in (server.get("env") or {}).items()},
                timeout=self.timeout,
            )
        elif transport == "sse":
            self._transport = _SSETransport(
                url=expand_env(server.get("url", ""), origin=f"MCP 服务 {self.name}.url"),
                headers={str(k): expand_env(v, origin=f"MCP 服务 {self.name}.headers.{k}")
                         for k, v in (server.get("headers") or {}).items()},
                timeout=self.timeout,
            )
        else:
            raise ConfigError(f"不支持的传输方式: {transport}（{' | '.join(TRANSPORTS)}）")
        self._id = 0

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def start(self) -> None:
        self._transport.start()

    def close(self) -> None:
        self._transport.close()

    # ---------------------------------------------------------------- 协议
    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self._transport.send(message)
        try:
            raw = self._transport.receive()
        except queue.Empty as exc:
            raise ConfigError(f"MCP 服务 {self.timeout}s 内无响应（method={method}）") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"MCP 服务返回了非 JSON 内容: {raw[:200]}") from exc
        if "error" in payload:
            error = payload["error"]
            raise ConfigError(f"MCP 返回错误 {error.get('code')}: {error.get('message')}")
        return payload

    def initialize(self) -> Dict[str, Any]:
        return self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mas", "version": __version__},
        })

    def list_tools(self) -> List[Dict[str, Any]]:
        payload = self.request("tools/list")
        return list((payload.get("result") or {}).get("tools") or [])

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return payload.get("result") or {}


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
