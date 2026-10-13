"""前端数据服务：仅依赖 Python 标准库。

用法：
    python frontend/server.py [--port 8000] [--workspace workspace]

API：
    GET /api/runs                    运行列表（含摘要指标）
    GET /api/run?id=<run_id>         运行详情（摘要 + 事件流 + 验收报告 + 时间线）
    GET /api/experience              分级经验库
    GET /api/metrics                 每轮效果指标
    GET /api/learning-report         自学习效果报告（Markdown）
    GET /api/llm                     当前 LLM 配置与凭据（Key 脱敏/回显、来源标注）
    POST /api/llm                    保存 provider/model 到 system.yaml，凭据到 credentials.yaml
    POST /api/llm/test               用给定凭据探测 BASE_URL 的 /models，验证 Key 是否可用
静态文件：/ 直接映射本目录（index.html / app.js / style.css）
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FRONTEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FRONTEND_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# 事件日志超过该时长无新增且没有 run_summary，视为运行已中断（服务进程被杀等），
# 避免这类记录在列表里长期停在"进行中"（正常跑的流水线每隔几秒就有新事件）
RUN_STALE_SECONDS = 600


def run_finished(run_dir: Path, run_id: str) -> bool:
    """判定一次运行是否已结束：产出 run_summary，或异常终止（error.log / 事件长期无新增）。"""
    if (run_dir / "run_summary.json").exists():
        return True
    if (run_dir.parent / f"{run_id}.error.log").exists():
        return True
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return False
    try:
        return (time.time() - events_path.stat().st_mtime) > RUN_STALE_SECONDS
    except OSError:
        return True


def _running_summary(run_dir: Path, run_id: str) -> dict:
    """尚无 run_summary.json 的运行：从事件日志推导实时摘要（运行中或已中断）。"""
    title = ""
    steps = 0
    ts = None
    for event in read_jsonl(run_dir / "events.jsonl"):
        payload = event.get("payload") or {}
        if not title and event.get("type") == "run_started":
            title = payload.get("requirement") or ""
        if not ts:
            ts = event.get("ts")
        if event.get("type") == "node_start":
            steps += 1
    ended = run_finished(run_dir, run_id)   # True = 已中断结束（无验收结论，按未通过计）
    return {
        "run_id": run_id,
        "requirement_title": title,
        "verdict": "fail" if ended else None,
        "steps": steps,
        "rollbacks": 0,
        "review_rejections": 0,
        "bugs_total": 0,
        "bugs_fixed": 0,
        "duration_s": None,
        "injected_experiences": None,
        "profile": None,
        "ts": ts,
        "finished": ended,            # False = 进行中
        "status": "已完成" if ended else "进行中",
        "error": ended,
    }


def load_runs(workspace: Path) -> list[dict]:
    runs_dir = workspace / "runs"
    if not runs_dir.exists():
        return []
    runs = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        run_id = run_dir.name
        summary_path = run_dir / "run_summary.json"
        if summary_path.exists():
            try:
                data = json.loads(summary_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            metrics = data.get("metrics") or {}
            runs.append({
                "run_id": data.get("run_id") or run_id,
                "requirement_title": data.get("requirement_title"),
                "verdict": data.get("verdict"),
                "steps": data.get("steps"),
                "rollbacks": data.get("rollbacks"),
                "review_rejections": data.get("review_rejections"),
                "bugs_total": data.get("bugs_total"),
                "bugs_fixed": data.get("bugs_fixed"),
                "duration_s": metrics.get("duration_s"),
                "injected_experiences": metrics.get("injected_experiences"),
                "profile": metrics.get("profile"),
                "ts": metrics.get("ts"),
                "finished": True,
                "status": "已完成",
                "error": False,
            })
        elif (run_dir / "events.jsonl").exists():
            runs.append(_running_summary(run_dir, run_id))
    runs.sort(key=lambda r: r.get("run_id") or "", reverse=True)
    return runs


def load_run_detail(workspace: Path, run_id: str) -> dict | None:
    run_dir = workspace / "runs" / run_id
    summary_path = run_dir / "run_summary.json"
    events_path = run_dir / "events.jsonl"
    if not summary_path.exists() and not events_path.exists():
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
    report_path = run_dir / "reports" / "acceptance_report.md"
    timeline_path = run_dir / "timeline.md"
    # 无 run_summary.json 即仍在运行（error.log / 事件长期无新增视为已中断结束）
    finished = run_finished(run_dir, run_id)
    return {
        "summary": summary,
        "finished": finished,
        "status": "已完成" if finished else "进行中",
        "events": read_jsonl(events_path),
        "llm_traces": read_jsonl(run_dir / "llm_traces.jsonl"),
        "report": report_path.read_text(encoding="utf-8") if report_path.exists() else "",
        "timeline": timeline_path.read_text(encoding="utf-8") if timeline_path.exists() else "",
    }


VALID_PROVIDERS = {"offline", "openai_compatible"}


SYSTEM_YAML = PROJECT_ROOT / "config" / "system.yaml"

# 常用模型预设（供前端下拉快速选择，仍可自由输入其他模型名）
MODEL_PRESETS = [
    "DeepSeek",
    "Qwen",
    "GLM",
    "glm-5.3-flash",
    "glm-4.5-flash",
    "gpt-4o-mini",
    "gpt-4o",
]

# API Key / BASE_URL 的注入通道（前端界面填写保存 → config/credentials.yaml）
CREDENTIALS_YAML = PROJECT_ROOT / "config" / "credentials.yaml"


def load_llm_defaults() -> dict:
    """读取当前生效的 LLM 配置与凭据（供前端初始化模型/凭据控件）。"""
    from mas.config import credentials_path, load_config, load_credentials, mask_secret, resolve_credentials

    config = load_config(PROJECT_ROOT / "config")
    llm = config.llm or {}
    # 按优先级解析：凭据文件 < 环境变量 < 运行时注入（此处无运行时注入）
    api_key, base_url = resolve_credentials(llm, PROJECT_ROOT / "config")
    creds = load_credentials(PROJECT_ROOT / "config")
    return {
        "provider": llm.get("provider") or "offline",
        "model": llm.get("model") or "",
        "base_url": base_url,
        "api_key": api_key,                      # 本地面板回显，便于修改/切换其他 Key
        "api_key_set": bool(api_key),
        "api_key_masked": mask_secret(api_key),
        "token_name": creds.get("token_name") or "",
        "api_key_env": llm.get("api_key_env") or "OPENAI_API_KEY",
        "base_url_env": llm.get("base_url_env") or "OPENAI_BASE_URL",
        # 上下文窗口（token）：端点不返回该值，由用户在「模型配置」弹窗维护
        "context_window": int(llm.get("context_window") or 262144),
        "credential_file": str(credentials_path(PROJECT_ROOT / "config")),
        "providers": sorted(VALID_PROVIDERS),
        "model_presets": MODEL_PRESETS,
    }


def save_llm_defaults(
    provider: str | None,
    model: str | None,
    base_url: str | None,
    api_key: str | None = None,
    token_name: str | None = None,
    context_window: int | None = None,
) -> dict:
    """把 provider/model/base_url/context_window 写回 config/system.yaml，凭据写回 config/credentials.yaml。"""
    import re

    from mas.config import save_credentials

    if provider and provider not in VALID_PROVIDERS:
        raise ValueError(f"provider 非法: {provider}（{' | '.join(sorted(VALID_PROVIDERS))}）")
    if context_window is not None:
        if int(context_window) < 1024:
            raise ValueError("context_window 不能小于 1024")
        context_window = int(context_window)
    if api_key or base_url or token_name:
        save_credentials(
            PROJECT_ROOT / "config",
            api_key=api_key,
            base_url=base_url,
            token_name=token_name,
        )
    lines = SYSTEM_YAML.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_llm = False
    for line in lines:
        if re.match(r"^llm:\s*$", line):
            in_llm = True
        elif re.match(r"^\S[^:]*:", line):  # 新的顶层键，离开 llm 段
            in_llm = False
        if in_llm and line.startswith("  ") and not line.strip().startswith("#"):
            key = line.strip().split(":", 1)[0]
            if key == "provider":
                line = f'  provider: "{provider}"'
            elif key == "model" and model:
                line = f'  model: "{model}"'
            elif key == "base_url" and base_url:
                line = f'  base_url: "{base_url}"'
            elif key == "context_window" and context_window:
                line = f"  context_window: {context_window}"
        out.append(line)
    SYSTEM_YAML.write_text("\n".join(out) + "\n", encoding="utf-8")
    return load_llm_defaults()


def test_llm_connection(api_key: str | None = None, base_url: str | None = None) -> dict:
    """用给定（或已保存的）凭据探测 BASE_URL 的 /models 接口，用于前端「测试连接」。"""
    from mas.config import load_config, mask_secret, resolve_credentials

    config = load_config(PROJECT_ROOT / "config")
    key, url = resolve_credentials(
        config.llm, PROJECT_ROOT / "config", override={"api_key": api_key, "base_url": base_url}
    )
    if not key:
        return {"ok": False, "error": "缺少 API Key（请填写后重试）", "base_url": url}
    request = urllib.request.Request(f"{url}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}", "base_url": url}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "base_url": url}
    models = [str(item.get("id")) for item in (body.get("data") or []) if item.get("id")]
    return {
        "ok": True,
        "base_url": url,
        "api_key_masked": mask_secret(key),
        "models": models,
        "model_count": len(models),
    }


def start_run(
    workspace: Path,
    profile: str | None,
    requirement_text: str,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str:
    """在后台线程中启动一次流水线，返回预分配的 run_id。"""
    from mas.config import load_config
    from mas.leader import Leader

    config = load_config(PROJECT_ROOT / "config", profile=profile, workspace=str(workspace))
    if provider:
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"provider 非法: {provider}（{' | '.join(sorted(VALID_PROVIDERS))}）")
        config.llm["provider"] = provider
    if model:
        config.llm["model"] = model
    # 运行时注入凭据：仅本次运行生效，不落盘（落盘走「设为默认」）
    if api_key:
        config.llm_overrides["api_key"] = api_key
    if base_url:
        config.llm_overrides["base_url"] = base_url
    stamp = datetime.now().strftime(str(config.runtime.get("run_id_format", "%Y%m%d-%H%M%S")))
    run_id = f"{stamp}-{secrets.token_hex(2)}"
    # 先落目录与空事件文件：运行记录在启动瞬间即可见于列表（状态"进行中"）
    run_dir = workspace / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").touch(exist_ok=True)

    def worker() -> None:
        try:
            Leader(config).run(requirement_text, as_text=True, run_id=run_id)
        except Exception as exc:  # 后台线程异常落盘，便于排查
            (workspace / "runs").mkdir(parents=True, exist_ok=True)
            (workspace / "runs" / f"{run_id}.error.log").write_text(str(exc), encoding="utf-8")

    threading.Thread(target=worker, daemon=True).start()
    return run_id


class Handler(BaseHTTPRequestHandler):
    workspace: Path = PROJECT_ROOT / "workspace"
    server_version = "mas-frontend/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        return

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (FRONTEND_DIR / rel).resolve()
        if FRONTEND_DIR not in target.parents and target != FRONTEND_DIR:
            self._send_text("forbidden", 403)
            return
        if not target.is_file() or target.suffix not in STATIC_TYPES:
            self._send_text("not found", 404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[target.suffix])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802（标准库命名）
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)

        if route == "/api/runs":
            self._send_json(load_runs(self.workspace))
        elif route == "/api/run":
            run_id = (query.get("id") or [""])[0]
            detail = load_run_detail(self.workspace, run_id)
            if detail is None:
                self._send_json({"error": f"run 不存在: {run_id}"}, 404)
            else:
                self._send_json(detail)
        elif route == "/api/experience":
            self._send_json(read_jsonl(self.workspace / "knowledge" / "experience.jsonl"))
        elif route == "/api/metrics":
            self._send_json(read_jsonl(self.workspace / "knowledge" / "metrics.jsonl"))
        elif route == "/api/learning-report":
            path = self.workspace / "reports" / "learning_report.md"
            self._send_text(path.read_text(encoding="utf-8") if path.exists() else "")
        elif route == "/api/llm":
            self._send_json(load_llm_defaults())
        elif route == "/api/example":
            # 离线 Provider 只识别结构化 Markdown（### FP-xx 功能点），提供示例作为输入模板
            path = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"
            self._send_text(path.read_text(encoding="utf-8") if path.exists() else "")
        else:
            self._serve_static(route)

    def do_POST(self) -> None:  # noqa: N802（标准库命名）
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/run", "/api/llm", "/api/llm/test"):
            self._send_text("not found", 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"error": "请求体不是合法 JSON"}, 400)
            return
        if parsed.path == "/api/llm/test":
            self._send_json(
                test_llm_connection(
                    (data.get("api_key") or "").strip() or None,
                    (data.get("base_url") or "").strip() or None,
                )
            )
            return
        if parsed.path == "/api/llm":
            provider = (data.get("provider") or "").strip()
            model = (data.get("model") or "").strip() or None
            base_url = (data.get("base_url") or "").strip() or None
            api_key = (data.get("api_key") or "").strip() or None
            token_name = (data.get("token_name") or "").strip() or None
            raw_window = data.get("context_window")
            try:
                context_window = int(raw_window) if raw_window not in (None, "") else None
            except (TypeError, ValueError):
                self._send_json({"error": "context_window 必须是整数"}, 400)
                return
            try:
                self._send_json(save_llm_defaults(provider, model, base_url, api_key, token_name, context_window))
            except (ValueError, OSError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        requirement = str(data.get("requirement") or "").strip()
        if not requirement:
            self._send_json({"error": "requirement 不能为空"}, 400)
            return
        profile = data.get("profile") or None
        provider = (data.get("provider") or "").strip() or None
        model = (data.get("model") or "").strip() or None
        api_key = (data.get("api_key") or "").strip() or None
        base_url = (data.get("base_url") or "").strip() or None
        try:
            run_id = start_run(
                self.workspace, profile, requirement,
                provider=provider, model=model, api_key=api_key, base_url=base_url,
            )
        except Exception as exc:
            self._send_json({"error": f"启动失败：{exc}"}, 400)
            return
        self._send_json({"run_id": run_id, "status": "started"})


def main() -> int:
    parser = argparse.ArgumentParser(description="多 Agent 系统前端数据服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--workspace", default=str(PROJECT_ROOT / "workspace"))
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    if not workspace.exists():
        print(f"工作区不存在: {workspace}（先运行 python -m mas demo 生成数据）")
        return 2
    Handler.workspace = workspace

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"前端面板：http://{args.host}:{args.port}  （数据目录：{workspace}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
