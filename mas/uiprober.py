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

__all__ = ["UiProber", "PORT_MARKER"]

PORT_MARKER = "MAS_UI_PORT="
_PORT_RE = re.compile(re.escape(PORT_MARKER) + r"(\d+)")


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

    # ---------------------------------------------------------------- 入口
    def collect(self, *, service_cmd: List[str], checks: List[Dict[str, Any]],
                startup_timeout: int = 30) -> Dict[str, Any]:
        """执行全部 checks，返回 ui_evidence（mode 恒为 browser；fatal 时 failed_asserts>0）。"""
        checks = [c for c in (checks or []) if isinstance(c, dict)]
        evidence: Dict[str, Any] = {
            "mode": "browser", "server": self.server_name,
            "checks": [], "failed_asserts": 0, "screenshots": [], "error": "",
        }
        if not checks:
            evidence["error"] = "web_fullstack 需求启用了 ui_probe，但测试契约未提供 ui_checks"
            evidence["failed_asserts"] = 1
            return evidence
        if not self.server_name:
            evidence["error"] = "runtime.ui_probe.server 未配置"
            evidence["failed_asserts"] = len(checks)
            return evidence

        proc: Optional[subprocess.Popen] = None
        client = None
        try:
            proc = self._spawn_service(service_cmd)
            base_url = self._await_port(proc, startup_timeout)
            client = self._connect_mcp()
            evidence["base_url"] = base_url
            for check in checks:
                record = self._run_check(client, check, base_url)
                evidence["checks"].append(record)
                if not record["passed"]:
                    evidence["failed_asserts"] += 1
                if record.get("evidence_file"):
                    evidence["screenshots"].append(record["evidence_file"])
        except Exception as exc:  # noqa: BLE001 - 致命错误按全部失败计，绝不空证据判过
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            evidence["failed_asserts"] = max(1, len(checks))
            if self.events is not None:
                self.events.emit("ui_probe_error", severity="error",
                                 payload={"error": evidence["error"]})
        finally:
            self._cleanup(client, proc)
        return evidence

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
        record: Dict[str, Any] = {"name": name, "passed": False, "detail": "", "evidence_file": ""}
        last_text = ""
        try:
            for step in (check.get("steps") or []):
                if not isinstance(step, dict) or not step.get("tool"):
                    raise ValueError(f"check「{name}」存在非法 step（缺 tool）")
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
