"""前端数据服务：仅依赖 Python 标准库。

用法：
    python frontend/server.py [--host 127.0.0.1] [--port 8000] [--workspace workspace]

访问控制：
    默认仅绑定回环地址（127.0.0.1 / localhost / ::1），无需令牌即可打开面板；
    绑定非回环地址（如 0.0.0.0）必须设置环境变量 MAS_PANEL_TOKEN，否则拒绝启动。
    设置令牌后，所有 /api/* 请求需携带 Authorization: Bearer <token>、
    X-Panel-Token: <token>，或先访问 /?token=<token> 换取 HttpOnly Cookie。
    静态页面（/、app.js、style.css）保持公开，不含运行数据；跨域默认只允许同源，
    额外来源可通过 MAS_PANEL_ORIGINS（逗号分隔）放行。

API：
    GET /api/runs                    运行列表（含摘要指标）
    GET /api/run?id=<run_id>         运行详情（摘要 + 事件流 + 验收报告 + 时间线）
    GET /api/experience              分级经验库
    GET /api/metrics                 每轮效果指标
    GET /api/learning-report         自学习效果报告（Markdown）
    GET /api/llm                     当前 LLM 配置与凭据（Key 只返回掩码与是否已设置，不回显明文）
    GET /api/skills                  技能开关（runtime.skills.enabled / tools_enabled，与 system.yaml 同名同义）
    GET /api/tools                   工具调用总开关（llm.tools_enabled，与 system.yaml 同名同义）
    GET /api/react                   ReAct 开关（runtime.react.enabled / max_steps / require_thought，与 system.yaml 同名同义）
    GET /api/store/status            MySQL/Redis 存储后端健康报告（docs/25）
    GET /api/mq/status               消息队列健康：运行流积压 / PEL / DLQ / worker（docs/26）
    POST /api/run                     发起一次流水线（requirement 文字 / requirement_path 上传文件，二选一；可另带 images 附图路径数组）
                                       runtime.mq 启用时只入队（返回 status=queued），由 worker 消费执行
    POST /api/run/cancel              终止进行中的运行（置取消信号；Leader 立即中断节点内模型调用，docs/41）
    POST /api/upload                  上传需求文档（base64 JSON，落盘 workspace/uploads/）
    POST /api/upload/remove           取消一次已上传的需求文档（删除 uploads/ 内暂存文件）
    POST /api/upload_image            上传对话附图（base64 JSON，图片后缀白名单，落盘 workspace/uploads/）
    POST /api/llm                     保存 provider/model 到 system.yaml，凭据到 credentials.yaml
    POST /api/llm/test               用给定凭据探测 BASE_URL 的 /models，验证 Key 是否可用
    POST /api/skills                 保存技能开关到 system.yaml（下一次运行生效，无需重启服务）
    POST /api/tools                  保存工具调用总开关（llm.tools_enabled）到 system.yaml
    POST /api/react                  保存 ReAct 开关到 system.yaml（下一次运行生效，无需重启服务）
静态文件（前后端分离后可关闭）：
    默认 --dist 指向项目根 frontend/，/ 映射到该目录（index.html / app.js / style.css）；
    传 --no-static 则本进程只提供 /api/*，静态资源由独立前端宿主（如 python -m http.server）
    提供，接口响应携带 CORS 头供跨源调用。两种模式下接口路径与响应体完全一致。
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

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

# 本服务进程内「进行中运行」注册表：run_id -> 取消信号（threading.Event）。
# POST /api/run/cancel 在此设置信号；Leader 在下一个节点边界检查到后退出并收尾
# （run_summary.canceled=true）。只登记本进程 POST /api/run 发起的运行——
# CLI 或其他进程启动的运行不在此表，接口对这类请求如实报错（不伪造成送达）。
RUNNING_TASKS: dict[str, threading.Event] = {}

# 存储后端与运行注册表（docs/25）：由 main() 启动时构建，None = 未接入/不可用。
# 不做 import 时自动构建——测试与脚本直接调用 load_runs/start_run 时零外部依赖，
# 行为与改造前逐字节一致。REGISTRY 非 None 时：
# - Redis 可用 → RedisRunRegistry（CLI 等跨进程发起的运行在本面板可见、可终止）；
# - 否则 → MemoryRunRegistry(events_map=RUNNING_TASKS)（与上表共享，同进程路径不变）。
BACKENDS: object | None = None
REGISTRY: object | None = None

# 消息队列（docs/26）：main() 启动时装配；None = 未启用/不可用，POST /api/run 走既有线程路径。
# RUN_QUEUE 非 None 时运行任务只入队；mode=auto 时本进程内嵌一个 worker（独立进程见 mas worker）。
RUN_QUEUE: object | None = None
MQ_SETTINGS: dict = {}
WORKER: object | None = None

# ---------------- 面板访问控制（默认回环免令牌；对外绑定必须显式给令牌）----------------
PANEL_TOKEN_ENV = "MAS_PANEL_TOKEN"        # 面板访问令牌；非回环绑定下为必填
PANEL_ORIGINS_ENV = "MAS_PANEL_ORIGINS"    # 额外允许的跨域来源，逗号分隔（如 https://ops.example）
PANEL_COOKIE = "mas_panel_token"
# profile 是 config/profiles/<name>.yaml 的文件名分量：只允许安全字符，杜绝 ../ 穿越。
_PROFILE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# run_id 会拼进 workspace/runs/<id>：只允许文件名安全字符，挡住 ../ 目录穿越。
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")


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


def read_queued(run_dir: Path) -> dict:
    """读取排队标记（MQ 入队时写入）：队列中的运行在 worker 领取前也能在列表里看到。"""
    path = run_dir / "queued.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _running_summary(run_dir: Path, run_id: str) -> dict:
    """尚无 run_summary.json 的运行：从事件日志推导实时摘要（排队/运行中/已中断）。"""
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
    queued = read_queued(run_dir)
    is_queued = bool(queued) and steps == 0 and not ended
    title = title or str(queued.get("title") or "")
    ts = ts or queued.get("queued_at")
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
        "profile": queued.get("profile"),
        "ts": ts,
        "finished": ended,            # False = 进行中
        "status": "已完成" if ended else ("排队中" if is_queued else "进行中"),
        "queued": is_queued,
        "canceled": False,            # 取消标记只由 Leader 收尾写入 run_summary.canceled
        "error": ended,
    }


def _run_sort_key(run: dict) -> tuple:
    """运行列表排序键：按开始时间倒序，run_id 仅作兜底。

    不能按 run_id 字符串排序：历史运行存在两套命名——CLI 演示运行是
    ``task01-...``~``task06-...``，面板/流水线运行是 ``20261016-HHMMSS-xxxx``。
    字符串倒序下 ``'t' > '2'``，17 条 task0x 旧运行会全部压在两套格式的最新运行之上，
    刚产生的运行被埋到列表中段（45 条列表里排到第 18/19 位），用户翻不到即误判"未写入"。
    ``ts`` 取 run_summary.metrics.ts（运行开始时间，ISO 8601，同格式字符串可比）。
    """
    return (str(run.get("ts") or ""), str(run.get("run_id") or ""))


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
            canceled = bool(data.get("canceled"))
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
                "status": "已终止" if canceled else "已完成",
                "canceled": canceled,
                "error": False,
            })
        elif (run_dir / "events.jsonl").exists():
            runs.append(_running_summary(run_dir, run_id))
    runs.sort(key=_run_sort_key, reverse=True)
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
    canceled = bool((summary or {}).get("canceled"))
    queued = read_queued(run_dir)
    is_queued = bool(queued) and not finished and not read_jsonl(events_path)
    return {
        "run_id": run_id,             # 终止按钮需要 run_id（运行中 summary 不存在，不能从 summary 取）
        "summary": summary,
        "finished": finished,
        "canceled": canceled,
        "queued": is_queued,
        "status": "已终止" if canceled else (
            "已完成" if finished else ("排队中" if is_queued else "进行中")),
        "events": read_jsonl(events_path),
        "llm_traces": read_jsonl(run_dir / "llm_traces.jsonl"),
        "report": report_path.read_text(encoding="utf-8") if report_path.exists() else "",
        "timeline": timeline_path.read_text(encoding="utf-8") if timeline_path.exists() else "",
    }


VALID_PROVIDERS = {"offline", "openai_compatible"}

# 对话窗口附图上传的单张大小上限：与 config/system.yaml 的 llm.vision.max_image_bytes
# 默认值（5MB）一致；进入流水线时 mas/sources.py 的 prepare_images 会按配置再校验一次。
IMAGE_MAX_BYTES = 5 * 1024 * 1024


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
        # 不回显明文 Key：面板只需知道「是否已设置 + 掩码」，留空提交即沿用已保存值。
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
    SYSTEM_YAML.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return load_llm_defaults()


# ---------------- 技能开关（runtime.skills.enabled / tools_enabled）----------------
# 与「模型配置」同一模式：读取走 load_config、写回走 system.yaml 的行级替换（保留注释与格式）。
# 生效方式：面板服务与 /api/run 均不缓存配置——每次 POST /api/run 都在 start_run() 里
# 重新 load_config()，因此保存后「下一次运行」即生效，无需重启面板或 CLI；
# 正在运行中的流水线其 config 已在启动时构建，不受此次变更影响。


def load_skills_switch() -> dict:
    """读取技能开关（字段名与 system.yaml 的 runtime.skills 子键完全一致）。"""
    from mas.config import load_config

    config = load_config(PROJECT_ROOT / "config")
    skills = dict(config.runtime.get("skills") or {})
    return {
        "enabled": bool(skills.get("enabled", False)),
        "tools_enabled": bool(skills.get("tools_enabled", False)),
    }


def load_mq_status(run_id: str | None = None) -> dict:
    """消息队列健康报告：运行流积压/PEL、DLQ、延迟重试、在线 worker、Agent 事件流。"""
    try:
        from mas.config import load_config
        from mas.mq import mq_status

        config = load_config(PROJECT_ROOT / "config", workspace=str(Handler.workspace))
        report = mq_status(config, PROJECT_ROOT / "config", run_id=run_id)
        report["embedded_worker"] = WORKER.status() if WORKER is not None else None
        return report
    except Exception as exc:  # noqa: BLE001 - 状态接口永不 500
        return {"enabled": False, "reason": f"{type(exc).__name__}: {exc}"}


def load_store_status() -> dict:
    """MySQL/Redis 存储后端健康报告（docs/25；供面板展示接入状态）。

    只读探测：不影响任何进行中的运行；任何异常都收敛成 ``ok=False`` 的报告项，
    不向面板抛 500（面板应始终能渲染）。
    """
    try:
        from mas.config import load_config
        from mas.store import store_status

        config = load_config(PROJECT_ROOT / "config", workspace=str(Handler.workspace))
        return store_status(config, PROJECT_ROOT / "config")
    except Exception as exc:  # noqa: BLE001 - 状态接口永不 500
        return {
            "enabled": False,
            "mysql": {"ok": False, "reason": f"{type(exc).__name__}: {exc}"},
            "redis": {"ok": False, "reason": f"{type(exc).__name__}: {exc}"},
        }


def _rewrite_skills_switches(text: str, enabled: bool, tools_enabled: bool) -> str:
    """行级替换 runtime.skills 下的两个开关，保留注释与其余内容原样。

    必须跟踪三层缩进定位（runtime → skills → 键）后精确替换，不能只按
    「4 空格缩进的 enabled」匹配：llm.vision.enabled、runtime.ui_probe.enabled、
    runtime.context_compression.enabled 等同名键在其他段同样存在，误改任意一个
    都会造成不可预期的行为变化。目标键缺失时报 ValueError（不猜测插入位置）。
    """
    import re

    found = {"enabled": False, "tools_enabled": False}
    out: list[str] = []
    top = ""       # 当前顶层键（无缩进）
    second = ""    # 当前二级键（2 空格缩进）
    for line in text.splitlines():
        if line.lstrip().startswith("#"):       # 注释行不改变层级状态
            out.append(line)
            continue
        m_top = re.match(r"^([^\s#][^:]*):", line)
        m_second = re.match(r"^  ([^\s#][^:]*):", line)
        m_third = re.match(r"^    ([^\s#][^:]*):", line)
        if m_top:
            top, second = m_top.group(1), ""
        elif m_second:
            second = m_second.group(1)
        elif m_third and top == "runtime" and second == "skills":
            key = m_third.group(1)
            if key in found:
                value = enabled if key == "enabled" else tools_enabled
                line = f"    {key}: {str(value).lower()}"
                found[key] = True
        out.append(line)
    missing = [key for key, hit in found.items() if not hit]
    if missing:
        raise ValueError(f"config/system.yaml 的 runtime.skills 缺少字段: {', '.join(missing)}")
    return "\n".join(out) + "\n"


def save_skills_switch(enabled: bool, tools_enabled: bool) -> dict:
    """把两个开关写回 system.yaml（写回后下一次运行生效，无需重启，见本节注释）。

    ``newline="\\n"``：Windows 下 ``Path.write_text`` 默认会把 ``\\n`` 转成 ``\\r\\n``，
    整份配置文件的换行符会因此被改写（git 全文件 diff）。写回承诺是「只改目标行、
    其余逐字节保留」，必须显式关闭换行转换（save_llm_defaults 同款修复）。
    """
    if not isinstance(enabled, bool) or not isinstance(tools_enabled, bool):
        raise ValueError("enabled / tools_enabled 必须是布尔值（true/false）")
    original = SYSTEM_YAML.read_text(encoding="utf-8")
    SYSTEM_YAML.write_text(
        _rewrite_skills_switches(original, enabled, tools_enabled),
        encoding="utf-8", newline="\n",
    )
    return load_skills_switch()


# ---------------- 工具调用总开关（llm.tools_enabled）----------------
# 与技能开关同一模式：读取走 load_config、写回走 system.yaml 行级替换，保存后下一次运行生效。
# 它是 function calling 的总闸：关闭后 Leader 不装配 ToolRunner（MCP 服务与技能工具都不下发）。


def load_tools_switch() -> dict:
    """读取工具调用总开关（字段名与 system.yaml 的 llm.tools_enabled 同名同义）。"""
    from mas.config import load_config

    config = load_config(PROJECT_ROOT / "config")
    return {"enabled": bool(config.llm.get("tools_enabled", False))}


def _rewrite_tools_switch(text: str, enabled: bool) -> str:
    """行级替换 llm.tools_enabled，保留注释与其余内容原样。

    必须跟踪顶层键定位（llm → 2 空格缩进的 tools_enabled）后精确替换：runtime.skills
    下还有一个同名 ``tools_enabled``（4 空格缩进），只按键名匹配会误改技能工具开关。
    目标键缺失时报 ValueError（不猜测插入位置）。
    """
    import re

    found = False
    out: list[str] = []
    top = ""       # 当前顶层键（无缩进）
    for line in text.splitlines():
        if line.lstrip().startswith("#"):       # 注释行不改变层级状态
            out.append(line)
            continue
        m_top = re.match(r"^([^\s#][^:]*):", line)
        m_second = re.match(r"^  ([^\s#][^:]*):", line)
        if m_top:
            top = m_top.group(1)
        elif m_second and top == "llm" and m_second.group(1) == "tools_enabled":
            line = f"  tools_enabled: {str(enabled).lower()}"
            found = True
        out.append(line)
    if not found:
        raise ValueError("config/system.yaml 的 llm 段缺少字段: tools_enabled")
    return "\n".join(out) + "\n"


def save_tools_switch(enabled: bool) -> dict:
    """把 llm.tools_enabled 写回 system.yaml（下一次运行生效，无需重启，见技能开关注释）。"""
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值（true/false）")
    original = SYSTEM_YAML.read_text(encoding="utf-8")
    SYSTEM_YAML.write_text(
        _rewrite_tools_switch(original, enabled),
        encoding="utf-8", newline="\n",
    )
    return load_tools_switch()


# ---------------- ReAct 开关（runtime.react.enabled / max_steps / require_thought）----------------
# 与技能开关同一模式：读取走 load_config、写回走 system.yaml 行级替换，保存后下一次运行生效。
# nodes（节点白名单）仍手改 yaml：其值是列表且需与 llm.tools_nodes 取交集，前端不做。
# 注意 ReAct 的实际生效范围是三重交集：enabled ∧ nodes ∧ llm.tools_nodes（见 docs/34）。


def load_react_switch() -> dict:
    """读取 ReAct 开关（字段名与 system.yaml 的 runtime.react 子键完全一致）。"""
    from mas.config import load_config

    config = load_config(PROJECT_ROOT / "config")
    react = dict(config.runtime.get("react") or {})
    return {
        "enabled": bool(react.get("enabled", False)),
        "max_steps": int(react.get("max_steps", 4) or 4),
        "require_thought": bool(react.get("require_thought", False)),
        # 只读回显：让面板能显示"实际会在哪些节点生效"，不参与写回
        "nodes": [str(node) for node in (react.get("nodes") or [])],
        "tools_nodes": [str(node) for node in (config.llm.get("tools_nodes") or [])],
    }


def _rewrite_react_switch(text: str, enabled: bool, max_steps: int, require_thought: bool) -> str:
    """行级替换 runtime.react 下的三个键，保留注释与其余内容原样。

    与 `_rewrite_skills_switches` 同样的三层缩进定位（runtime → react → 键）：
    同名键 `enabled` 在 llm.vision / runtime.ui_probe / runtime.context_compression
    等段同样存在，只按缩进匹配会误改。目标键缺失时报 ValueError（不猜测插入位置）。
    """
    import re

    found = {"enabled": False, "max_steps": False, "require_thought": False}
    values = {"enabled": str(enabled).lower(), "max_steps": str(max_steps),
              "require_thought": str(require_thought).lower()}
    out: list[str] = []
    top = ""
    second = ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):       # 注释行不改变层级状态
            out.append(line)
            continue
        m_top = re.match(r"^([^\s#][^:]*):", line)
        m_second = re.match(r"^  ([^\s#][^:]*):", line)
        m_third = re.match(r"^    ([^\s#][^:]*):", line)
        if m_top:
            top, second = m_top.group(1), ""
        elif m_second:
            second = m_second.group(1)
        elif m_third and top == "runtime" and second == "react":
            key = m_third.group(1)
            if key in found:
                # 只换值、保留该行值之后的全部原文（对齐空格 + 行尾注释），
                # 使往返替换逐字节还原（压缩对齐空格会让往返不等价）。
                head = re.match(r"^(    [^\s#][^:]*:\s*)", line)
                if head:
                    prefix = head.group(1)
                    rest = line[len(prefix):]
                    tail_match = re.match(r"^[^\s#]+(\s*#.*)?$", rest)
                    tail = (tail_match.group(1) or "") if tail_match else ""
                    line = f"{prefix}{values[key]}{tail}"
                found[key] = True
        out.append(line)
    missing = [key for key, hit in found.items() if not hit]
    if missing:
        raise ValueError(f"config/system.yaml 的 runtime.react 缺少字段: {', '.join(missing)}")
    return "\n".join(out) + "\n"


def save_react_switch(enabled: bool, max_steps: int, require_thought: bool) -> dict:
    """把 ReAct 开关写回 system.yaml（下一次运行生效；``newline="\\n"`` 防整份换行符改写）。"""
    if not isinstance(enabled, bool) or not isinstance(require_thought, bool):
        raise ValueError("enabled / require_thought 必须是布尔值（true/false）")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
        raise ValueError("max_steps 必须是 >= 1 的整数")
    original = SYSTEM_YAML.read_text(encoding="utf-8")
    SYSTEM_YAML.write_text(
        _rewrite_react_switch(original, enabled, max_steps, require_thought),
        encoding="utf-8", newline="\n",
    )
    return load_react_switch()


# ---------------- 联网搜索开关（config/inputs.yaml 的 websearch.enabled）----------------
# 与技能 / 工具 / ReAct 开关同一范式：读取走 yaml、写回走行级替换（保留注释与对齐），
# 保存后下一次运行生效——Leader 每次 run 都重新 load_inputs 并装配 ToolRunner，无需重启。
# 前端只暴露 enabled 一个写回字段：provider / api_key 涉及厂商选型与凭据，仍手改 yaml。
# 可用性判定同时看 llm.tools_enabled（function calling 总闸）与 provider 是否需要 Key，
# 面板据此显示「开了但不生效」的真实原因，而不是只回显一个勾选框。

INPUTS_YAML = PROJECT_ROOT / "config" / "inputs.yaml"


def load_websearch_switch() -> dict:
    """读取联网搜索开关与实际可用性（``key_present`` 只报布尔，绝不回显 Key 明文）。"""
    import yaml

    from mas import websearch as ws
    from mas.config import load_config
    from mas.mcp import expand_env

    raw: dict = {}
    if INPUTS_YAML.exists():
        raw = yaml.safe_load(INPUTS_YAML.read_text(encoding="utf-8")) or {}
    section = dict(raw.get("websearch") or {})
    provider = str(section.get("provider") or "bing_web")
    keyless = provider in ws.KEYLESS_PROVIDERS
    key_present = False
    reason = ""
    if keyless:
        reason = f"免 Key 通道（provider={provider}）"
    else:
        detail = ""
        try:
            key_present = bool(
                expand_env(section.get("api_key", ""), origin="inputs.yaml websearch.api_key")
            )
        except Exception as exc:  # noqa: BLE001 - 环境变量缺失等一律降级为「不可用 + 原因」
            detail = f"api_key 无法展开：{exc}"
        if not key_present:
            reason = (f"provider={provider} 需要订阅 Key，但 websearch.api_key 未配置"
                      + (f"（{detail}）" if detail else "")
                      + "；免 Key 请把 provider 改为 bing_web")

    config = load_config(PROJECT_ROOT / "config")
    tools_enabled = bool(config.llm.get("tools_enabled", False))
    tools_nodes = [str(node) for node in (config.llm.get("tools_nodes") or [])]
    enabled = bool(section.get("enabled", False))

    if not enabled:
        reason = "开关关闭"
    elif not tools_enabled:
        reason = "config/system.yaml 的 llm.tools_enabled=false（function calling 总闸关闭）"
    return {
        "enabled": enabled,
        "provider": provider,
        "keyless": keyless,
        "key_present": key_present,
        "pre_enrich": bool(section.get("pre_enrich", False)),
        "max_results": int(section.get("max_results", 5) or 5),
        # 只读回显：工具实际下发到哪些节点（空列表 = 全部节点）
        "tools_enabled": tools_enabled,
        "tools_nodes": tools_nodes,
        "available": bool(enabled and tools_enabled and (keyless or key_present)),
        "reason": reason,
    }


def _rewrite_websearch_switch(text: str, enabled: bool) -> str:
    """行级替换 ``websearch.enabled``，保留注释与其余内容原样（含行尾注释与对齐空格）。

    必须跟踪顶层键定位：``feishu`` 段下也有同名 ``enabled``（同样 2 空格缩进），
    只按缩进匹配会误改飞书开关。目标键缺失时报 ValueError（不猜测插入位置）。
    """
    found = False
    out: list[str] = []
    top = ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):       # 注释行不改变层级状态
            out.append(line)
            continue
        m_top = re.match(r"^([^\s#][^:]*):", line)
        m_second = re.match(r"^  ([^\s#][^:]*):", line)
        if m_top:
            top = m_top.group(1)
        elif m_second and top == "websearch" and m_second.group(1) == "enabled":
            head = re.match(r"^(  [^\s#][^:]*:\s*)", line)
            if head:
                prefix = head.group(1)
                rest = line[len(prefix):]
                tail_match = re.match(r"^[^\s#]+(\s*#.*)?$", rest)
                tail = (tail_match.group(1) or "") if tail_match else ""
                line = f"{prefix}{str(enabled).lower()}{tail}"
            found = True
        out.append(line)
    if not found:
        raise ValueError("config/inputs.yaml 的 websearch 段缺少字段: enabled")
    return "\n".join(out) + "\n"


def save_websearch_switch(enabled: bool) -> dict:
    """把 ``websearch.enabled`` 写回 inputs.yaml（下一次运行生效；``newline="\\n"`` 防换行符改写）。"""
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值（true/false）")
    original = INPUTS_YAML.read_text(encoding="utf-8")
    INPUTS_YAML.write_text(
        _rewrite_websearch_switch(original, enabled),
        encoding="utf-8", newline="\n",
    )
    return load_websearch_switch()


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
    requirement_path: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    images: list[str] | None = None,
    registry: object | None = None,
) -> str:
    """在后台线程中启动一次流水线，返回预分配的 run_id。

    ``requirement_path`` 非空时走 CLI 同款文件通道（``as_text=False``），
    为空时行为与现状逐字节一致（文字需求 ``as_text=True``）。
    ``images`` 为对话窗口上传的附图路径数组（经 ``POST /api/upload_image`` 落盘），
    透传给 ``Leader.run(images=...)``：校验/复制/元数据发布由 ``sources.prepare_images`` 负责，
    不传或为空时与改造前逐字节一致。

    每次运行创建独立取消信号并登记进 ``RUNNING_TASKS``（``POST /api/run/cancel`` 置位），
    线程结束时注销——注册表只覆盖本进程发起、且仍在运行中的任务。

    ``registry``（docs/25 协调层，默认 None 行为不变）：非 None 时透传给
    ``Leader.run(registry=...)``，使本次运行在 Redis 后端下跨进程可见、可被
    CLI/其他终端取消；Leader 自己在 run() 的 finally 里注销，此处只提前登记。
    """
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

    cancel_event = threading.Event()
    RUNNING_TASKS[run_id] = cancel_event
    if registry is not None:
        try:
            registry.register(run_id)   # 幂等；Redis 后端即跨进程心跳 + 可取消
        except Exception:  # noqa: BLE001 - 协调层不可用不影响本进程取消通道
            pass

    def worker() -> None:
        try:
            if requirement_path:
                Leader(config).run(requirement_path, as_text=False, run_id=run_id, images=images,
                                   cancel_event=cancel_event, registry=registry)
            else:
                Leader(config).run(requirement_text, as_text=True, run_id=run_id, images=images,
                                   cancel_event=cancel_event, registry=registry)
        except Exception as exc:  # 后台线程异常落盘，便于排查
            (workspace / "runs").mkdir(parents=True, exist_ok=True)
            (workspace / "runs" / f"{run_id}.error.log").write_text(str(exc), encoding="utf-8")
        finally:
            RUNNING_TASKS.pop(run_id, None)   # 线程结束即注销（成功/异常/终止均走到这里）

    threading.Thread(target=worker, daemon=True).start()
    return run_id


def enqueue_run(
    workspace: Path,
    profile: str | None,
    requirement_text: str,
    requirement_path: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    images: list[str] | None = None,
    registry: object | None = None,
    queue: object | None = None,
) -> dict:
    """把一次运行投递到 Redis Stream（任务队列），不在 HTTP 进程内执行。

    - API Key 存 Redis 一次性键（``mas:mq:secret:<run_id>``，TTL=queue_ttl_s），
      任务 payload 只带 run_id 等非秘密字段，共享队列里永远看不到明文 Key；
    - 排队即注册（registry，TTL 覆盖排队时长），因此「终止任务」在 worker 领取前也生效；
    - 幂等：同一 run_id 的 dedup 键保证重复提交只入队一次。
    """
    from mas.config import load_config
    from mas.mq import build_run_queue

    config = load_config(PROJECT_ROOT / "config", profile=profile, workspace=str(workspace))
    if provider:
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"provider 非法: {provider}（{' | '.join(sorted(VALID_PROVIDERS))}）")
    active_queue = queue or RUN_QUEUE or build_run_queue(config, PROJECT_ROOT / "config")
    if active_queue is None:
        raise ValueError("MQ 未启用或 Redis 不可用（见 GET /api/mq/status）")
    stamp = datetime.now().strftime(str(config.runtime.get("run_id_format", "%Y%m%d-%H%M%S")))
    run_id = f"{stamp}-{secrets.token_hex(2)}"
    payload = {
        "source": "frontend",
        "requirement": requirement_text,
        "as_text": not bool(requirement_path),
        "workspace": str(Path(workspace).resolve()),
        "profile": profile or config.profile,
        "images": list(images or []),
    }
    if requirement_path:
        payload["requirement_path"] = str(requirement_path)
    if provider:
        payload["provider"] = provider
    if model:
        payload["model"] = model
    if base_url:
        payload["base_url"] = base_url
    if registry is not None:
        try:
            active_queue.register_queued_run(registry, run_id)   # 先注册：排队期间即可取消
        except Exception:  # noqa: BLE001 - 协调层故障不影响入队
            pass
    try:
        if api_key:
            active_queue.mq.store_secret(run_id, api_key, ttl_s=active_queue.queue_ttl_s)
        result = active_queue.enqueue(payload, run_id=run_id)
    except Exception:
        if registry is not None:
            try:
                registry.unregister(run_id)
            except Exception:  # noqa: BLE001
                pass
        raise
    if not result.get("duplicate"):
        # 队列中的运行在列表里可见：空事件文件满足 detail 端点，queued.json 提供标题/排队时间
        run_dir = Path(workspace) / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.jsonl").touch(exist_ok=True)
        title = Path(requirement_path).stem if requirement_path else requirement_text.strip()[:60]
        (run_dir / "queued.json").write_text(json.dumps({
            "run_id": run_id, "title": title, "queued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "frontend", "profile": profile or config.profile,
            "transport": "redis-stream", "stream": result.get("stream"),
            "entry_id": result.get("_stream_id"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "run_id": run_id,
        "status": "duplicate" if result.get("duplicate") else "queued",
        "transport": "redis-stream",
        "stream": result.get("stream"),
        "entry_id": result.get("_stream_id"),
    }


def cancel_run(run_id: str) -> dict:
    """请求终止一次运行：置取消信号，Leader 立即中断节点内的模型调用（docs/41）。

    返回 ``{"run_id", "status": "canceling"}``（异步语义：信号已送达，LLM 层在流式
    读取/退避等待中检测到即中断；不含模型调用的执行段跑完后停止）。
    先查本进程 ``RUNNING_TASKS``（快路径）；未命中再查 ``REGISTRY``——Redis 后端下
    CLI/其他进程发起的运行同样可在此终止。两者都没有时抛 ``ValueError``——
    已完成，或从未注册（如实报错，不伪造送达）。
    """
    event = RUNNING_TASKS.get(run_id)
    if event is not None:
        event.set()
        return {"run_id": run_id, "status": "canceling"}
    registry = REGISTRY
    if registry is not None:
        try:
            if registry.request_cancel(run_id):
                return {"run_id": run_id, "status": "canceling"}
        except Exception:  # noqa: BLE001 - 协调层异常按「不在运行中」处理
            pass
    raise ValueError(f"运行 {run_id} 不在本服务进行中（可能已完成，或由 CLI/其他进程启动）")


def remove_upload(workspace: Path, path: str) -> dict:
    """取消一次已上传但尚未发送的需求文档：删除 ``workspace/uploads/`` 内的暂存文件。

    只允许删 uploads 目录内文件（resolve 后做目录校验，拒绝路径穿越）；文件不存在时
    幂等返回 ``removed=False``（重复点击/并发取消不报错）。
    """
    uploads_dir = (workspace / "uploads").resolve()
    target = Path(path).resolve()
    if target != uploads_dir and uploads_dir not in target.parents:
        raise ValueError(f"只能移除 uploads 目录内的文件: {path}")
    existed = target.is_file()
    if existed:
        target.unlink()
    return {"removed": existed, "path": str(target)}


def is_loopback_host(host: str) -> bool:
    """是否为回环绑定地址（只有这些地址允许在无令牌模式下对外提供服务）。"""
    name = (host or "").strip().strip("[]").lower()
    return name in {"127.0.0.1", "localhost", "::1"}


def bind_security_error(host: str, token: str) -> str:
    """非回环绑定且无令牌时返回拒绝原因（空串 = 允许启动）。"""
    if is_loopback_host(host) or (token or "").strip():
        return ""
    return (f"--host {host} 非回环地址，必须设置环境变量 {PANEL_TOKEN_ENV}"
            "（否则面板的 API Key/运行数据将对网络开放）")


def allowed_panel_origins(host: str, port: int) -> set[str]:
    """跨域白名单：回环绑定时自动包含同端口别名，另叠加 MAS_PANEL_ORIGINS。"""
    origins = set()
    for item in (os.environ.get(PANEL_ORIGINS_ENV) or "").split(","):
        item = item.strip().rstrip("/")
        if item:
            origins.add(item)
    if is_loopback_host(host):
        bare = (host or "").strip().strip("[]").lower()
        shown = f"[{bare}]" if ":" in bare else bare
        origins.update({
            f"http://{shown}:{port}",
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        })
    return origins


def normalize_profile(raw: object) -> str | None:
    """校验运行 profile 名（会拼进 config/profiles/<name>.yaml，必须杜绝路径穿越）。"""
    profile = str(raw or "").strip()
    if not profile:
        return None
    if not _PROFILE_RE.fullmatch(profile):
        raise ValueError("profile 只能包含字母/数字/下划线/连字符（1-64 个字符）")
    return profile


def normalize_run_id(raw: object) -> str:
    """校验 run_id（会拼进 workspace/runs/<id>，必须杜绝路径穿越）。"""
    run_id = str(raw or "").strip()
    if not run_id or not _RUN_ID_RE.fullmatch(run_id) or run_id in (".", ".."):
        raise ValueError("run_id 非法（只允许字母/数字/点/下划线/连字符）")
    return run_id


def restrict_panel_path(workspace: Path, raw: str, kind: str) -> str:
    """把面板提交的文件路径收敛到 ``workspace/uploads`` 内，返回解析后的绝对路径。

    resolve() 会展开 ``..`` 与符号链接，因此目录前缀校验同时挡住路径穿越与
    指向外部的链接；``is_file()`` 拒绝目录以及不存在的路径。上传落盘见
    ``sources.save_upload``（返回绝对路径），CLI/测试直接调用 start_run 不受此限制。
    """
    uploads_dir = (workspace / "uploads").resolve()
    try:
        target = Path(raw).resolve()
    except (OSError, ValueError) as exc:
        raise ValueError(f"{kind}路径非法: {raw}") from exc
    if target != uploads_dir and uploads_dir not in target.parents:
        raise ValueError(f"{kind}只能引用 workspace/uploads 内的文件: {raw}")
    if not target.is_file():
        raise ValueError(f"{kind}不存在: {raw}")
    return str(target)


def _secure_equals(given: str, expected: str) -> bool:
    """常量时间比较（先编码，避免非 ASCII 触发 compare_digest 的 TypeError）。"""
    return hmac.compare_digest(
        (given or "").encode("utf-8"), (expected or "").encode("utf-8")
    )


class Handler(BaseHTTPRequestHandler):
    workspace: Path = PROJECT_ROOT / "workspace"
    token: str = ""                    # main() 从 MAS_PANEL_TOKEN 注入；空 = 回环免令牌模式
    allowed_origins: set[str] = set()  # main() 按绑定地址与 MAS_PANEL_ORIGINS 注入
    # 前端静态资源目录；None = 只提供 /api/*（前后端分离：页面由独立前端宿主托管）
    static_dir: Path | None = FRONTEND_DIR
    server_version = "mas-frontend/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        return

    # ------------------------------------------------------------ 认证/CORS 基础设施
    def _request_token(self) -> str:
        """从 Authorization / X-Panel-Token / Cookie 中取面板令牌。"""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        header = (self.headers.get("X-Panel-Token") or "").strip()
        if header:
            return header
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == PANEL_COOKIE:
                return unquote(value.strip())
        return ""

    def _authorized(self) -> bool:
        """未配置令牌时只依赖回环绑定；配置后所有 /api/* 一律校验。"""
        return not self.token or _secure_equals(self._request_token(), self.token)

    def _origin_allowed(self) -> bool:
        """同源或显式白名单才放行；无 Origin（CLI/测试）视为同源。"""
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if not origin:
            return True
        if origin in self.allowed_origins:
            return True
        parsed = urlparse(origin)
        host = (self.headers.get("Host") or "").strip().lower()
        return parsed.scheme in ("http", "https") and bool(parsed.netloc) \
            and parsed.netloc.lower() == host

    def _cors_headers(self) -> None:
        """只回显白名单来源；默认不再发送 ``Access-Control-Allow-Origin: *``。"""
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            # 分离部署必须显式声明 credentials：前端跨源调用若带 Cookie 需它，
            # 而通配符来源 ``*`` 与 credentials 互斥（此处只回显白名单具体来源，合法）。
            self.send_header("Access-Control-Allow-Credentials", "true")

    def _reject_unauthorized(self) -> None:
        self._send_json(
            {"error": "需要面板访问令牌：访问 /?token=<MAS_PANEL_TOKEN> 或携带 "
                      "Authorization: Bearer / X-Panel-Token"},
            401,
        )

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="mas-panel"')
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="mas-panel"')
        self.end_headers()
        self.wfile.write(body)

    def _handle_login(self, provided: str) -> None:
        """GET /?token=... ：校验后种 HttpOnly Cookie 并 303 回 /（令牌不出现在后续 URL）。"""
        if not self.token or not _secure_equals(provided, self.token):
            self._send_text("invalid panel token", 401)
            return
        cookie = (f"{PANEL_COOKIE}={quote(self.token, safe='')}; Path=/; HttpOnly; "
                  f"SameSite=Strict; Max-Age=86400")
        if (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https":
            cookie += "; Secure"
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802（标准库命名）
        # 预检不是实际接口调用，只做来源校验：跨源分离部署时前端宿主的 OPTIONS
        # 必须拿到 204 + CORS 头，否则浏览器直接掐断后续请求。
        if not self._origin_allowed():
            self._send_text("forbidden", 403)
            return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Panel-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self, path: str) -> None:
        root = self.static_dir
        if root is None:                    # 分离部署：本进程不托管页面
            self._send_text("not found", 404)
            return
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
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

        if route in ("/", "/index.html") and "token" in query:
            self._handle_login((query.get("token") or [""])[0])
            return
        if route.startswith("/api/") and not self._authorized():
            self._reject_unauthorized()
            return

        if route == "/api/runs":
            self._send_json(load_runs(self.workspace))
        elif route == "/api/run":
            try:
                run_id = normalize_run_id((query.get("id") or [""])[0])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
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
        elif route == "/api/skills":
            self._send_json(load_skills_switch())
        elif route == "/api/tools":
            self._send_json(load_tools_switch())
        elif route == "/api/react":
            self._send_json(load_react_switch())
        elif route == "/api/websearch":
            self._send_json(load_websearch_switch())
        elif route == "/api/store/status":
            self._send_json(load_store_status())
        elif route == "/api/mq/status":
            self._send_json(load_mq_status((query.get("run_id") or [None])[0]))
        elif route == "/api/example":
            # 离线 Provider 只识别结构化 Markdown（### FP-xx 功能点），提供示例作为输入模板
            path = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"
            self._send_text(path.read_text(encoding="utf-8") if path.exists() else "")
        else:
            self._serve_static(route)

    def do_POST(self) -> None:  # noqa: N802（标准库命名）
        parsed = urlparse(self.path)
        if not self._authorized():
            self._reject_unauthorized()
            return
        # 无令牌（回环模式）时用 Origin 兜底防 CSRF；有令牌时令牌本身就是门槛，
        # 同源/反代场景不再依赖 Origin（否则 https 反代到回环会被误拒）。
        if not self.token and not self._origin_allowed():
            self._send_json({"error": "拒绝跨站请求：Origin 不在允许列表"}, 403)
            return
        if parsed.path not in ("/api/run", "/api/run/cancel", "/api/upload", "/api/upload/remove",
                               "/api/upload_image", "/api/llm", "/api/llm/test", "/api/skills",
                               "/api/tools", "/api/react", "/api/websearch"):
            self._send_text("not found", 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"error": "请求体不是合法 JSON"}, 400)
            return
        if parsed.path == "/api/run/cancel":
            try:
                run_id = normalize_run_id(data.get("run_id"))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            try:
                self._send_json(cancel_run(run_id))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 404)
            return
        if parsed.path == "/api/upload/remove":
            path = str(data.get("path") or "").strip()
            if not path:
                self._send_json({"error": "path 不能为空"}, 400)
                return
            try:
                self._send_json(remove_upload(self.workspace, path))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        if parsed.path == "/api/upload":
            self._handle_upload(data)
            return
        if parsed.path == "/api/upload_image":
            self._handle_upload_image(data)
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
        if parsed.path == "/api/skills":
            enabled = data.get("enabled")
            tools_enabled = data.get("tools_enabled")
            if not isinstance(enabled, bool) or not isinstance(tools_enabled, bool):
                self._send_json(
                    {"error": "enabled / tools_enabled 必须是布尔值（true/false）"}, 400)
                return
            try:
                self._send_json(save_skills_switch(enabled, tools_enabled))
            except (ValueError, OSError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        if parsed.path == "/api/tools":
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json({"error": "enabled 必须是布尔值（true/false）"}, 400)
                return
            try:
                self._send_json(save_tools_switch(enabled))
            except (ValueError, OSError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        if parsed.path == "/api/react":
            enabled = data.get("enabled")
            max_steps = data.get("max_steps")
            require_thought = data.get("require_thought")
            if not isinstance(enabled, bool) or not isinstance(require_thought, bool):
                self._send_json(
                    {"error": "enabled / require_thought 必须是布尔值（true/false）"}, 400)
                return
            if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
                self._send_json({"error": "max_steps 必须是 >= 1 的整数"}, 400)
                return
            try:
                self._send_json(save_react_switch(enabled, max_steps, require_thought))
            except (ValueError, OSError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        if parsed.path == "/api/websearch":
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json({"error": "enabled 必须是布尔值（true/false）"}, 400)
                return
            try:
                self._send_json(save_websearch_switch(enabled))
            except (ValueError, OSError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        requirement = str(data.get("requirement") or "").strip()
        requirement_path = str(data.get("requirement_path") or "").strip()
        # 两段式上传（docs/16 的 04 文档）：requirement_path 走文件通道，与文字需求二选一
        if requirement and requirement_path:
            self._send_json({"error": "requirement 与 requirement_path 只能二选一"}, 400)
            return
        if not requirement and not requirement_path:
            self._send_json({"error": "requirement 不能为空（图片为附图，需与需求文字/文档一同发送）"}, 400)
            return
        # 对话附图（docs/16 的 01 文档）：images 为 /api/upload_image 落盘后的路径数组。
        # 只做类型校验；后缀/大小/张数语义由 prepare_images 在流水线内校验（单一事实来源），
        # 其 ConfigError 会以 run_failed 事件 + fail 摘要呈现在运行详情里。
        raw_images = data.get("images")
        images: list[str] | None = None
        if raw_images is not None:
            if not isinstance(raw_images, list) or not all(
                isinstance(item, str) and item.strip() for item in raw_images
            ):
                self._send_json(
                    {"error": "images 必须是文件路径字符串数组（先经 POST /api/upload_image 上传）"}, 400)
                return
            images = [item.strip() for item in raw_images] or None
        # 路径限制只放在 HTTP 边界（start_run 的 Python 调用方保留原有绝对路径语义）：
        # requirement_path / images 必须指向 workspace/uploads 内已存在的文件；
        # profile 会拼进 config/profiles/<name>.yaml，同样拒绝路径穿越。
        try:
            profile = normalize_profile(data.get("profile"))
            if requirement_path:
                requirement_path = restrict_panel_path(self.workspace, requirement_path, "需求文档")
            if images:
                images = [restrict_panel_path(self.workspace, item, "附图") for item in images]
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        provider = (data.get("provider") or "").strip() or None
        model = (data.get("model") or "").strip() or None
        api_key = (data.get("api_key") or "").strip() or None
        base_url = (data.get("base_url") or "").strip() or None
        if RUN_QUEUE is not None:
            # MQ 路径：HTTP 线程只负责入队（毫秒级），长任务由 worker 消费执行
            try:
                result = enqueue_run(
                    self.workspace, profile, requirement, requirement_path=requirement_path or None,
                    provider=provider, model=model, api_key=api_key, base_url=base_url,
                    images=images, registry=REGISTRY, queue=RUN_QUEUE,
                )
            except Exception as exc:
                self._send_json({"error": f"入队失败：{exc}"}, 400)
                return
            self._send_json(result)
            return
        try:
            run_id = start_run(
                self.workspace, profile, requirement, requirement_path=requirement_path or None,
                provider=provider, model=model, api_key=api_key, base_url=base_url, images=images,
                registry=REGISTRY,
            )
        except Exception as exc:
            self._send_json({"error": f"启动失败：{exc}"}, 400)
            return
        self._send_json({"run_id": run_id, "status": "started"})

    def _handle_upload(self, data: dict) -> None:
        """POST /api/upload：base64 JSON 通道（绕开 Python 3.13 起已移除的 cgi multipart 解析）。"""
        from mas.errors import ConfigError
        from mas.sources import save_upload

        filename = str(data.get("filename") or "").strip()
        content_base64 = data.get("content_base64") or ""
        if not filename:
            self._send_json({"error": "filename 不能为空"}, 400)
            return
        try:
            self._send_json(save_upload(self.workspace, filename, content_base64))
        except ConfigError as exc:
            self._send_json({"error": str(exc)}, 400)

    def _handle_upload_image(self, data: dict) -> None:
        """POST /api/upload_image：对话窗口附图上传（base64 JSON，落盘 workspace/uploads/）。

        与 ``/api/upload``（需求文档）分工：后者走需求文档后缀白名单（不含图片），
        本路由走图片后缀白名单 ``mas.sources.IMAGE_SUFFIXES`` 与单张 5MB 上限
        （与 system.yaml ``llm.vision.max_image_bytes`` 默认值一致；进入流水线时
        ``prepare_images`` 还会按配置复核后缀/大小/张数）。返回体与 /api/upload 同构，
        ``kind`` 恒为 ``"image"``（save_upload 的 kind 面向需求文档路由，对图片无意义）。
        """
        from mas.errors import ConfigError
        from mas.sources import IMAGE_SUFFIXES, save_upload

        filename = str(data.get("filename") or "").strip()
        content_base64 = data.get("content_base64") or ""
        if not filename:
            self._send_json({"error": "filename 不能为空"}, 400)
            return
        try:
            meta = save_upload(self.workspace, filename, content_base64,
                               allowed_suffixes=set(IMAGE_SUFFIXES), max_bytes=IMAGE_MAX_BYTES)
            meta["kind"] = "image"
            self._send_json(meta)
        except ConfigError as exc:
            self._send_json({"error": str(exc)}, 400)


def main() -> int:
    parser = argparse.ArgumentParser(description="多 Agent 系统前端数据服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--workspace", default=str(PROJECT_ROOT / "workspace"))
    # 前后端分离开关：默认行为与改造前逐字节一致（同源托管页面）。
    parser.add_argument("--dist", default=str(FRONTEND_DIR),
                        help="静态资源目录（默认本目录）；前后端分离时由独立前端宿主提供")
    parser.add_argument("--no-static", action="store_true",
                        help="只提供 /api/*，不托管页面（前后端分离部署）")
    args = parser.parse_args()

    # 失败关闭：非回环绑定会暴露运行数据与写接口，没有令牌就不启动。
    panel_token = (os.environ.get(PANEL_TOKEN_ENV) or "").strip()
    security_error = bind_security_error(args.host, panel_token)
    if security_error:
        print(f"拒绝启动：{security_error}")
        return 2
    Handler.token = panel_token
    Handler.allowed_origins = allowed_panel_origins(args.host, args.port)
    Handler.static_dir = None if args.no_static else Path(args.dist).resolve()

    workspace = Path(args.workspace).resolve()
    if not workspace.exists():
        print(f"工作区不存在: {workspace}（先运行 python -m mas demo 生成数据）")
        return 2
    Handler.workspace = workspace

    # 存储后端 + 运行注册表（docs/25）：任何失败都降级为 None，面板按既有路径工作。
    # Redis 可用 → 跨进程注册表（CLI 发起的运行在本面板可见、可终止）；
    # 否则 → 内存版共享 RUNNING_TASKS（同进程取消路径逐字节不变）。
    global BACKENDS, REGISTRY, RUN_QUEUE, MQ_SETTINGS, WORKER
    try:
        from mas.config import load_config
        from mas.runhub import MemoryRunRegistry, RedisRunRegistry
        from mas.store import build_backends

        config = load_config(PROJECT_ROOT / "config", workspace=str(workspace))
        BACKENDS = build_backends(config, PROJECT_ROOT / "config")
        if BACKENDS.redis is not None:
            REGISTRY = RedisRunRegistry(BACKENDS.redis)
        else:
            REGISTRY = MemoryRunRegistry(events_map=RUNNING_TASKS)

        # 消息队列（docs/26）：mode=thread 保持旧行为；auto/queue 走 Redis Stream 入队。
        # auto 额外在本进程内嵌一个 worker（独立进程仍可横向扩容，消费组自动分摊）。
        from mas.mq import build_run_queue, resolve_mq_settings

        MQ_SETTINGS = resolve_mq_settings(config, PROJECT_ROOT / "config")
        if MQ_SETTINGS.get("enabled") and MQ_SETTINGS.get("mode") != "thread":
            RUN_QUEUE = build_run_queue(config, PROJECT_ROOT / "config", settings=MQ_SETTINGS)
            if RUN_QUEUE is not None and MQ_SETTINGS.get("mode", "auto") == "auto":
                from mas.worker import RunWorker

                WORKER = RunWorker(config, queue=RUN_QUEUE, registry=REGISTRY)
                WORKER.start()
    except Exception:  # noqa: BLE001 - 存储/MQ 不可用不影响面板启动
        BACKENDS, REGISTRY, RUN_QUEUE, WORKER = None, None, None, None

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"前端面板：http://{args.host}:{args.port}  （数据目录：{workspace}）")
    if Handler.static_dir is None:
        print("静态资源：未托管（--no-static，仅提供 /api/*；页面由独立前端宿主提供）")
    else:
        print(f"静态资源：{Handler.static_dir}")
    if panel_token:
        print(f"访问控制：已启用令牌认证（{PANEL_TOKEN_ENV}）；"
              f"首次访问请打开 http://{args.host}:{args.port}/?token=<令牌>")
    else:
        print("访问控制：仅回环地址、无令牌（所有 /api/* 仅同源可访问）")
    if BACKENDS is not None and BACKENDS.any_enabled:
        print(f"存储后端：MySQL={'已连接' if BACKENDS.mysql is not None else '未连接'}，"
              f"Redis={'已连接' if BACKENDS.redis is not None else '未连接'}"
              f"（注册表：{'跨进程' if REGISTRY.__class__.__name__ == 'RedisRunRegistry' else '进程内'}）")
    elif BACKENDS is not None:
        print("存储后端：未启用或连接失败（store.enabled / 服务状态见 GET /api/store/status），"
              "流水线按既有 jsonl/内存路径运行")
    if RUN_QUEUE is not None:
        mode = str(MQ_SETTINGS.get("mode") or "auto")
        print(f"消息队列：Redis Streams｜mode={mode}"
              f"｜stream={RUN_QUEUE.mq.stream_key(RUN_QUEUE.STREAM)}｜group={RUN_QUEUE.group}")
        if WORKER is not None:
            print(f"内嵌 worker：{WORKER.worker_id}（POST /api/run 只入队，本进程消费执行）")
        else:
            print("未启动内嵌 worker：请运行 `python -m mas worker` 消费队列"
                  "（面板仍可查看 GET /api/mq/status）")
    else:
        print("消息队列：未启用（POST /api/run 在面板进程内启动线程执行）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if WORKER is not None:
            WORKER.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
