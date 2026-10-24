"""UI 证据采集器：对 web_fullstack 交付物做浏览器级验证（docs/15 方案 B2）。

定位：tester 沙箱测试（HTTP 响应体断言）之后的**追加确定性步骤**，不走 LLM
function-calling——UI 验证必须可复现、可审计，不能是模型的自由工具。

工作方式：
1. 以子进程启动被测服务（命令过沙箱白名单校验，env 走沙箱同一套脱敏逻辑）；
   服务须按契约在 stdout 打印一行 `MAS_UI_PORT=<port>`（端口 0 由 OS 分配后回读）；
2. 经 config/mcp.yaml 注册的浏览器 MCP 服务（如 playwright）执行 checks：
   每条 check = 顺序 tool 调用 + 对最终文本结果的子串断言，`{{base_url}}` 占位符
   会被替换为真实服务地址；
3. 每条 check 的最终文本结果落盘 run_dir/evidence/ui/，失败断言计数进
   ui_evidence.failed_asserts，供验收门禁第 9 条判定。

失败策略（F23 教训）：采集器任何异常都不允许产出"空证据的 pass"——
致命错误按全部 check 失败计（failed_asserts = max(1, len(checks))），
节点判 fail 并如实记录 error。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import SystemConfig
from .sandbox import Sandbox

__all__ = ["UiProber", "PORT_MARKER", "PROBE_VERSION"]

PORT_MARKER = "MAS_UI_PORT="
_PORT_RE = re.compile(re.escape(PORT_MARKER) + r"(\d+)")

#: 证据格式版本：门禁用它区分"probe 真实采集"与"agent 自填的空壳证据"。
#: 只有 UiProber 会写这个值；tester 的 disabled/降级分支一律留空串。
PROBE_VERSION = "uiprober/2"


class UiProber:
    """浏览器级 UI 证据采集（框架侧组件，由 tester 节点调用）。"""

    def __init__(self, config: SystemConfig, sandbox: Sandbox, run_dir: Path, events: Any = None) -> None:
        self.config = config
        self.sandbox = sandbox
        self.run_dir = Path(run_dir)
        self.events = events
        probe_cfg = dict(config.runtime.get("ui_probe") or {})
        self.server_name = str(probe_cfg.get("server", ""))
        self.timeout = int(probe_cfg.get("timeout", 300))
        self.console_tool = str(probe_cfg.get("console_tool", "browser_console_messages"))

    # ---------------------------------------------------------------- 入口
    def collect(self, *, service_cmd: List[str], checks: List[Dict[str, Any]],
                startup_timeout: int = 30) -> Dict[str, Any]:
        """执行全部 checks，返回 ui_evidence（mode 恒为 browser；fatal 时 failed_asserts>0）。

        证据全部由本采集器（框架进程）产生并落盘 evidence/ui/probe_evidence.json：
        - ``http_status``/``http_probe``：框架进程对被测服务发真实 HTTP GET 的状态码；
        - ``console_errors``：经浏览器 MCP 采集的页面 console error（best-effort，
          工具不可用时如实记录原因，不静默为空）；
        - ``checks``：每条 DOM/文本断言的名称、期望值、实际结果与证据文件；
        - ``probe_version``：证据格式版本，门禁据此拒绝"没有 probe 参与"的 ui_evidence。
        """
        checks = [c for c in (checks or []) if isinstance(c, dict)]
        evidence: Dict[str, Any] = {
            "mode": "browser", "server": self.server_name, "probe_version": PROBE_VERSION,
            "checks": [], "failed_asserts": 0, "screenshots": [], "error": "",
            "http_status": 0, "http_probe": {}, "console_errors": [], "console_note": "",
        }
        if not checks:
            evidence["error"] = "web_fullstack 需求启用了 ui_probe，但测试契约未提供 ui_checks"
            evidence["failed_asserts"] = 1
            self._save_probe_evidence(evidence)
            return evidence
        if not self.server_name:
            evidence["error"] = "runtime.ui_probe.server 未配置"
            evidence["failed_asserts"] = len(checks)
            self._save_probe_evidence(evidence)
            return evidence

        proc: Optional[subprocess.Popen] = None
        client = None
        try:
            proc = self._spawn_service(service_cmd)
            base_url = self._await_port(proc, startup_timeout)
            evidence["base_url"] = base_url
            # 真实 HTTP 状态由框架进程直接取证（不依赖浏览器 MCP、不依赖 agent 自报）
            evidence["http_probe"] = self._http_probe(base_url)
            evidence["http_status"] = int(evidence["http_probe"].get("status", 0))
            if not 200 <= evidence["http_status"] < 400:
                evidence["failed_asserts"] += 1
            client = self._connect_mcp()
            for check in checks:
                record = self._run_check(client, check, base_url)
                evidence["checks"].append(record)
                if not record["passed"]:
                    evidence["failed_asserts"] += 1
                if record.get("evidence_file"):
                    evidence["screenshots"].append(record["evidence_file"])
            evidence["console_errors"], evidence["console_note"] = self._collect_console_errors(client)
        except Exception as exc:  # noqa: BLE001 - 致命错误按全部失败计，绝不空证据判过
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            evidence["failed_asserts"] = max(1, len(checks))
            if self.events is not None:
                self.events.emit("ui_probe_error", severity="error",
                                 payload={"error": evidence["error"]})
        finally:
            self._cleanup(client, proc)
        self._save_probe_evidence(evidence)
        return evidence

    # ---------------------------------------------------------------- 框架侧 HTTP 取证
    @staticmethod
    def _http_probe(base_url: str, timeout: int = 10) -> Dict[str, Any]:
        """框架进程对被测服务发一次真实 GET，记录状态码/响应特征（probe 写，agent 不参与）。"""
        import urllib.error
        import urllib.request

        started = time.time()
        try:
            with urllib.request.urlopen(base_url, timeout=timeout) as response:  # noqa: S310
                body = response.read(4096).decode("utf-8", errors="replace")
                return {"url": base_url, "status": int(response.status),
                        "content_type": str(response.headers.get("Content-Type", "")),
                        "body_head": body[:200], "error": "",
                        "duration_ms": int((time.time() - started) * 1000)}
        except urllib.error.HTTPError as exc:
            return {"url": base_url, "status": int(exc.code), "content_type": "",
                    "body_head": "", "error": f"HTTPError: {exc.reason}",
                    "duration_ms": int((time.time() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001 - 连不上就是取不到证据，如实记录
            return {"url": base_url, "status": 0, "content_type": "", "body_head": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": int((time.time() - started) * 1000)}

    def _collect_console_errors(self, client: Any) -> tuple:
        """经浏览器 MCP 采集 console error（best-effort）：返回 (errors, note)。

        工具不存在/调用失败不判 fatal（checks 已各自入账），但必须留痕——
        "console_errors 为空"要能区分"页面没有报错"和"根本没采到"。
        """
        try:
            result = client.call_tool(self.console_tool, {})
            text = self._text_of(result)
        except Exception as exc:  # noqa: BLE001
            return [], f"console 采集工具 {self.console_tool} 不可用：{type(exc).__name__}: {exc}"
        errors = [line.strip()[:300] for line in text.splitlines()
                  if re.search(r"\b(error|severe)\b", line, re.IGNORECASE)]
        try:
            directory = self.run_dir / "evidence" / "ui"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "console_messages.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass
        return errors, f"console 采集工具 {self.console_tool}，原始输出见 evidence/ui/console_messages.txt"

    def _save_probe_evidence(self, evidence: Dict[str, Any]) -> None:
        """把完整证据落盘（probe 写证据，不由 agent 填）；写失败留痕不抛出。"""
        try:
            directory = self.run_dir / "evidence" / "ui"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "probe_evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            evidence.setdefault("error", "")
            evidence["error"] = (evidence["error"] + "; probe 证据落盘失败").lstrip("; ")

    # ---------------------------------------------------------------- 服务生命周期
    def _spawn_service(self, service_cmd: List[str]) -> subprocess.Popen:
        """白名单校验 → 子进程启动（端口回读由 _await_port 负责）。"""
        cmd = [str(part) for part in service_cmd]
        self.sandbox._check_command(cmd)          # 复用沙箱命令白名单（含 shell 硬拒）
        env = self.sandbox._build_env(None)       # 复用沙箱环境脱敏（剔除 KEY/TOKEN 等）
        return subprocess.Popen(
            cmd, cwd=str(self.sandbox.root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

    def _await_port(self, proc: subprocess.Popen, startup_timeout: int) -> str:
        """等待服务打印 MAS_UI_PORT 行；提前退出/超时都抛错（由 collect 统一记账）。"""
        deadline = time.time() + startup_timeout
        buffer: List[str] = []
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"被测服务提前退出（exit={proc.returncode}）：" + "".join(buffer)[-500:])
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            buffer.append(line)
            match = _PORT_RE.search(line)
            if match:
                return f"http://127.0.0.1:{match.group(1)}"
        raise RuntimeError(f"服务启动超时（{startup_timeout}s 内未打印 {PORT_MARKER}<port>）")

    def _connect_mcp(self):
        from .mcp import MCPClient, get_server
        client = MCPClient(get_server(self.config, self.server_name))
        client.start()
        client.initialize()
        return client

    @staticmethod
    def _cleanup(client: Any, proc: Optional[subprocess.Popen]) -> None:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- 单条 check
    def _run_check(self, client: Any, check: Dict[str, Any], base_url: str) -> Dict[str, Any]:
        name = str(check.get("name", "")) or "未命名检查"
        expect = str((check.get("expect") or {}).get("contains", ""))
        record: Dict[str, Any] = {"name": name, "passed": False, "detail": "", "evidence_file": "",
                                  "expect": expect, "tools": []}
        last_text = ""
        try:
            for step in (check.get("steps") or []):
                if not isinstance(step, dict) or not step.get("tool"):
                    raise ValueError(f"check「{name}」存在非法 step（缺 tool）")
                record["tools"].append(str(step["tool"]))
                arguments = self._subst(step.get("arguments") or {}, base_url)
                result = client.call_tool(str(step["tool"]), arguments)
                last_text = self._text_of(result)
                record["detail"] = last_text[-300:]
            if expect:
                record["passed"] = expect in last_text
                if not record["passed"]:
                    record["detail"] = f"期望响应包含「{expect}」，实际未命中；末尾文本：{last_text[-300:]}"
            else:
                record["passed"] = True   # 无断言的 check 以"全部 step 未抛错"为通过
            record["evidence_file"] = self._save_evidence(name, last_text, record["passed"])
        except Exception as exc:  # noqa: BLE001 - 单条 check 失败不中断其余采集
            record["detail"] = f"{type(exc).__name__}: {exc}"
        return record

    @staticmethod
    def _subst(arguments: Dict[str, Any], base_url: str) -> Dict[str, Any]:
        """把 arguments 里的 {{base_url}} 占位符替换为真实服务地址。"""
        def walk(value: Any) -> Any:
            if isinstance(value, str):
                return value.replace("{{base_url}}", base_url)
            if isinstance(value, dict):
                return {k: walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v) for v in value]
            return value
        return walk(dict(arguments))

    @staticmethod
    def _text_of(result: Dict[str, Any]) -> str:
        """抽取 MCP tools/call 结果里的文本内容（content: [{type:text,text:...}]）。"""
        parts = []
        for item in (result.get("content") or []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)

    def _save_evidence(self, name: str, text: str, passed: bool) -> str:
        directory = self.run_dir / "evidence" / "ui"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name)[:60] or "check"
            relative = f"evidence/ui/{slug}_{'pass' if passed else 'fail'}.txt"
            (self.run_dir / relative).write_text(text, encoding="utf-8")
            return relative
        except OSError:
            return ""
