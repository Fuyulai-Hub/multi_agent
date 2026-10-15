"""环境探测：run 启动时确认交付形态所需的工具链是否真实可用（docs/15 B1）。

设计动机（F23/task04 教训的机制化）：非 Python 交付或浏览器级 UI 验证依赖外部
工具链（node / Playwright MCP）。缺失时必须在流程早期就写成可判定的事实并归类
「环境或构建问题」，而不是等研发在回滚压力下改写需求刷绿门禁。

探测结果发布为黑板 artifact `env_probe`，tester 的语言闸（_verification_gap）与
UI 证据采集（uiprober）都以它为前提。探测本身永远不抛异常——不可用就是不可用，
如实记录 reason 即可。
"""

from __future__ import annotations

import shutil
from typing import Any, Dict

from .config import SystemConfig
from .sandbox import Sandbox

__all__ = ["probe_environment"]


def _probe_node(sandbox: Sandbox) -> Dict[str, Any]:
    """node 可用性：PATH 里有 + 白名单放行 + `--version` 真实跑通，三者都满足才算可用。"""
    entry: Dict[str, Any] = {"available": False, "version": "", "reason": ""}
    if not shutil.which("node"):
        entry["reason"] = "PATH 中未找到 node"
        return entry
    if sandbox.command_policy_enabled and "node" not in sandbox.allowed_commands:
        entry["reason"] = "node 不在沙箱命令白名单（runtime.sandbox_command_policy.allowed）"
        return entry
    try:
        result = sandbox.run(["node", "--version"], timeout=20)
    except Exception as exc:  # noqa: BLE001 - 探测失败如实记录，不拖垮启动
        entry["reason"] = f"{type(exc).__name__}: {exc}"
        return entry
    version = result.stdout.strip() or result.stderr.strip()
    if result.exit_code == 0 and version.startswith("v"):
        entry.update({"available": True, "version": version})
    else:
        entry["reason"] = f"node --version 退出码 {result.exit_code}: {version[:120]}"
    return entry


def _probe_ui_server(config: SystemConfig) -> Dict[str, Any]:
    """Playwright 等浏览器 MCP 服务可用性（仅 ui_probe.enabled 时探测）。"""
    probe_cfg = dict(config.runtime.get("ui_probe") or {})
    entry: Dict[str, Any] = {"enabled": bool(probe_cfg.get("enabled", False)),
                             "server": str(probe_cfg.get("server", "")),
                             "available": False, "tools": [], "reason": ""}
    if not entry["enabled"] or not entry["server"]:
        entry["reason"] = "runtime.ui_probe 未启用或未指定 server"
        return entry
    try:
        from .mcp import check_server
        report = check_server(config, entry["server"])
    except Exception as exc:  # noqa: BLE001
        entry["reason"] = f"{type(exc).__name__}: {exc}"
        return entry
    entry["available"] = bool(report.get("ok"))
    entry["tools"] = [str(item.get("name", "")) for item in (report.get("tools") or [])][:40]
    if not entry["available"]:
        entry["reason"] = f"{report.get('stage', '')}: {report.get('reason', '')}（{report.get('hint', '')}）"
    return entry


def probe_environment(config: SystemConfig, sandbox: Sandbox) -> Dict[str, Any]:
    """汇总探测结果；`javascript_ready` 是 tester 语言闸放行 python+javascript 的单一依据。"""
    node = _probe_node(sandbox)
    ui_server = _probe_ui_server(config)
    return {
        "node": node,
        "ui_server": ui_server,
        "javascript_ready": bool(node["available"]),
        "browser_ui_ready": bool(node["available"] and ui_server["available"]),
        "python": sandbox.python_version(),
    }
