"""命令行入口。

用法：
    python -m mas run --requirement examples/requirements/order_pricing_service.md
    python -m mas run --requirement "文字需求描述……" --text
    python -m mas run --requirement ... --profile strict
    python -m mas demo                       # 三轮演示（文档 → 复用经验 → 文字需求输入）
    python -m mas experience list|stats|report|prune
    python -m mas store status|init|migrate  # MySQL/Redis 接入：健康检查 / 建表 / 历史数据迁移
    python -m mas worker [--once]            # 独立 worker：消费 Redis Stream 运行任务
    python -m mas run ... --enqueue          # 只入队，由独立 worker 执行
    python -m mas mq status [--run-id ...]   # 消息队列积压/消费组/worker 健康
    python -m mas config show|validate [--profile lean]
    python -m mas report list|show [run_id|latest]
    python -m mas skill list|show|add|edit|remove|check
    python -m mas mcp list|show|add|remove|check
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import __version__
from .config import SystemConfig, load_config
from .errors import ConfigError
from .experience import ExperienceLibrary
from .leader import Leader, RunSummary
from .runhub import build_registry
from .mcp import (
    add_server,
    check_server,
    describe_server,
    get_server,
    load_servers,
    mcp_path,
    remove_server,
    validate_server,
)
from .skills import (
    check_skill,
    describe_skill,
    edit_skill_meta,
    list_skills,
    load_skill,
    parse_skill_text,
    remove_skill,
    save_skill,
    validate_skill,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_config_dir(path: str) -> Path:
    candidate = Path(path)
    if candidate.exists():
        return candidate.resolve()
    fallback = PROJECT_ROOT / path
    if fallback.exists():
        return fallback.resolve()
    raise ConfigError(f"配置目录不存在: {path}")


def _load(args: argparse.Namespace) -> SystemConfig:
    return load_config(
        _resolve_config_dir(args.config),
        profile=getattr(args, "profile", None),
        workspace=getattr(args, "workspace", None),
    )


# ---------------------------------------------------------------- run / demo
def _apply_credential_overrides(config: SystemConfig, args: argparse.Namespace) -> None:
    """把 CLI 传入的 --api-key/--base-url 作为运行时注入（优先级高于环境变量与凭据文件）。"""
    api_key = getattr(args, "api_key", None)
    base_url = getattr(args, "base_url", None)
    if api_key:
        config.llm_overrides["api_key"] = api_key
    if base_url:
        config.llm_overrides["base_url"] = base_url


def _enqueue_run(config: SystemConfig, args: argparse.Namespace) -> int:
    """``run --enqueue``：把运行投递到 Redis Stream，由独立 worker 执行。"""
    import secrets

    from datetime import datetime

    from .mq import MQUnavailable, enqueue_run_task

    stamp = datetime.now().strftime(str(config.runtime.get("run_id_format", "%Y%m%d-%H%M%S")))
    run_id = args.run_id or f"{stamp}-{secrets.token_hex(2)}"
    payload: Dict[str, Any] = {
        "source": "cli",
        "requirement": args.requirement,
        "as_text": bool(args.text),
        "workspace": str(config.workspace_dir),
        "profile": config.profile,
        "images": list(args.image or []),
    }
    if args.base_url:
        payload["base_url"] = args.base_url
    try:
        result = enqueue_run_task(
            config, payload, run_id=run_id, api_key=args.api_key,
            config_dir=_resolve_config_dir(args.config),
        )
    except MQUnavailable as exc:
        print(f"入队失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "run_id": run_id,
        "status": "duplicate" if result.get("duplicate") else "queued",
        "stream": result.get("stream"),
        "entry_id": result.get("_stream_id"),
        "workspace": str(config.workspace_dir),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    if getattr(args, "enqueue", False):
        return _enqueue_run(config, args)
    _apply_credential_overrides(config, args)
    leader = Leader(config)
    # 运行注册表（docs/25 协调层）：Redis 可用则跨进程心跳/可取消，否则进程内版
    # （行为与改造前一致）。CLI 发起的运行同样可在面板「终止任务」。
    registry = build_registry(config, _resolve_config_dir(args.config))
    summary = leader.run(args.requirement, as_text=args.text, run_id=args.run_id,
                         images=args.image or None, registry=registry)
    print(json.dumps({
        "run_id": summary.run_id,
        "verdict": summary.verdict,
        "steps": summary.steps,
        "rollbacks": summary.rollbacks,
        "bugs_total": summary.bugs_total,
        "bugs_fixed": summary.bugs_fixed,
        "report": str(summary.report_path),
        "run_dir": summary.run_dir,
    }, ensure_ascii=False, indent=2))
    return 0 if summary.verdict == "pass" else 1


def cmd_demo(args: argparse.Namespace) -> int:
    config = _load(args)
    _apply_credential_overrides(config, args)
    requirement = args.requirement
    requirement_path = Path(requirement)
    if not requirement_path.is_absolute():
        requirement_path = PROJECT_ROOT / requirement
    if not requirement_path.exists():
        print(f"示例需求不存在: {requirement_path}")
        return 2

    summaries: List[RunSummary] = []
    registry = build_registry(config, _resolve_config_dir(args.config))
    print("=" * 72)
    print("演示 1/3：需求文档输入（首轮，含注入缺陷 → 审查驳回 → 缺陷修复闭环）")
    print("=" * 72)
    summaries.append(Leader(config).run(str(requirement_path), registry=registry))

    for index in range(2, args.runs + 1):
        print("\n" + "=" * 72)
        print(f"演示 {index}/{args.runs + 1}：同一需求再次运行（经验库注入 → 观察自学习收益）")
        print("=" * 72)
        summaries.append(Leader(config).run(str(requirement_path), registry=registry))

    print("\n" + "=" * 72)
    print(f"演示 {args.runs + 1}/{args.runs + 1}：文字需求描述输入（--text，验证双输入通道）")
    print("=" * 72)
    text = requirement_path.read_text(encoding="utf-8")
    summaries.append(Leader(config).run(text, as_text=True, registry=registry))

    print("\n" + "=" * 72)
    print("多轮运行对比（自学习闭环效果）")
    print("=" * 72)
    print("| 运行 | 输入 | 验收 | 步数 | 回退 | 审查驳回 | 缺陷 | 注入经验 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, summary in enumerate(summaries, start=1):
        mode = "文字" if index == len(summaries) else "文档"
        print(
            f"| {summary.run_id} | {mode} | {'通过' if summary.verdict == 'pass' else '未通过'} "
            f"| {summary.steps} | {summary.rollbacks} | {summary.review_rejections} "
            f"| {summary.bugs_total} | {summary.metrics.get('injected_experiences', 0)} |"
        )
    learning_report = config.reports_dir / "learning_report.md"
    print(f"\n自学习报告：{learning_report}")
    print(f"经验库：{config.workspace_dir / 'knowledge' / 'experience.jsonl'}")
    return 0 if all(s.verdict == "pass" for s in summaries) else 1


# ---------------------------------------------------------------- experience
def _library(args: argparse.Namespace) -> ExperienceLibrary:
    config = _load(args)
    return ExperienceLibrary(config.workspace_dir, config.learning)


def cmd_experience(args: argparse.Namespace) -> int:
    library = _library(args)
    action = args.action or "list"
    if action == "list":
        entries = library.entries
        if not entries:
            print("经验库为空（先运行一次流水线）")
            return 0
        for entry in entries:
            print(
                f"[{entry.tier}/{entry.scope}] {entry.id} conf={entry.confidence:.2f} hits={entry.hits} "
                f"roles={','.join(entry.roles)} tags={','.join(entry.tags[:4])}\n    {entry.lesson}"
                + (f"\n    动作: {entry.action}（{entry.action_key}）" if entry.action else "")
            )
        print(f"\n共 {len(entries)} 条")
    elif action == "stats":
        print(json.dumps(library.stats(), ensure_ascii=False, indent=2))
    elif action == "report":
        print(library.learning_report_markdown())
    elif action == "prune":
        removed = library.prune(min_confidence=float(getattr(args, "min_confidence", 0.2)))
        print(f"已清理 {removed} 条低价值经验")
    return 0


# ---------------------------------------------------------------- store
def cmd_store(args: argparse.Namespace) -> int:
    """存储后端管理（docs/25）：status 健康检查 / init 建表 / migrate 历史数据迁移。

    三个子命令都只读配置与数据库，不改流水线行为；MySQL 不可用时给出可执行的
    排查清单（与 LLM 凭据排查同款风格）。
    """
    config = _load(args)
    config_dir = _resolve_config_dir(args.config)
    action = args.action or "status"

    if action == "status":
        from .store import store_status

        report = store_status(config, config_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        if not report.get("enabled"):
            print("\n（store.enabled=false：未接入数据库，流水线按既有 jsonl/内存路径运行）")
            return 0
        if not report.get("workspace_guard", True):
            print("\n（当前 workspace 不是 system.yaml 指定的规范工作区，已自动禁用数据库接入）")
            return 1
        healthy = all(bool((report.get(key) or {}).get("ok")) for key in ("mysql", "redis"))
        return 0 if healthy else 1

    if action == "init":
        from .store import build_mysql_backend

        mysql = build_mysql_backend(config, config_dir)
        if mysql is None:
            print("MySQL 后端不可用，无法初始化 schema。请逐项排查：")
            print("  1. config/system.yaml → runtime.store.enabled / runtime.store.mysql.enabled 是否为 true")
            print("  2. 服务是否已启动：docker ps --filter name=mas-mysql（启停见 docs/25 附录）")
            print("  3. 连接参数：环境变量 MAS_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE，")
            print("     或 config/credentials.yaml 的 store.mysql 段")
            print("  4. PyMySQL 是否已装入当前解释器：pip install PyMySQL")
            print("  5. 当前 --workspace 须为规范工作区（默认即为，勿用临时目录）")
            return 1
        schema_path = Path(__file__).resolve().parent / "store" / "schema.sql"
        executed = mysql.ensure_schema(schema_path)   # int：执行的 DDL 语句数
        print(f"schema 已就绪：{schema_path}（执行 {executed} 条 DDL，幂等可重复）")
        health = mysql.health()
        print(json.dumps(health, ensure_ascii=False, indent=2, default=str))
        return 0 if health.get("ok") else 1

    if action == "migrate":
        from .experience import migrate_jsonl_to_mysql
        from .leader import upsert_run_index
        from .store import build_mysql_backend

        mysql = build_mysql_backend(config, config_dir)
        if mysql is None:
            print("MySQL 后端不可用，无法迁移（排查清单见 `python -m mas store init` 的输出）")
            return 1
        # 1) 经验库 experiences.jsonl + 指标 metrics.jsonl（INSERT IGNORE：只补缺口，
        #    永不覆盖 MySQL 既有行——启用后实时写入只进 MySQL，jsonl 是陈旧快照）
        stats = migrate_jsonl_to_mysql(mysql, config.workspace_dir, config.learning)
        # 2) runs 目录 → runs / run_metrics 表（run_summary.json 为投影源，SSOT 不动；
        #    run_summary 每轮运行都重写，UPSERT 幂等且不会覆盖更新的值）
        migrated_runs = 0
        runs_dir = config.runs_dir
        if runs_dir.exists():
            for summary_path in sorted(runs_dir.glob("*/run_summary.json")):
                try:
                    data = json.loads(summary_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                run_id = str(data.get("run_id") or summary_path.parent.name)
                metrics = data.get("metrics") or {}
                canceled = bool(data.get("canceled"))
                upsert_run_index(mysql, row={
                    "run_id": run_id,
                    "requirement_title": data.get("requirement_title"),
                    "verdict": data.get("verdict"),
                    "steps": data.get("steps") or 0,
                    "rollbacks": data.get("rollbacks") or 0,
                    "review_rejections": data.get("review_rejections") or 0,
                    "bugs_total": data.get("bugs_total") or 0,
                    "bugs_fixed": data.get("bugs_fixed") or 0,
                    "duration_s": metrics.get("duration_s") or 0.0,
                    "injected_experiences": metrics.get("injected_experiences") or 0,
                    "profile": metrics.get("profile"),
                    "ts": metrics.get("ts") or "",
                    "canceled": canceled,
                })
                if not canceled and metrics:
                    mysql.execute(
                        "INSERT INTO run_metrics (run_id, metrics) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE metrics = VALUES(metrics)",
                        (run_id, mysql.to_json(metrics)),
                    )
                migrated_runs += 1
        print(f"迁移完成（幂等，可重复执行；既有行不覆盖，只补缺口）：经验 {stats['experiences']} 条"
              f"（折叠历史漂移行 {stats['collapsed_rows']} 条）、"
              f"指标 {stats['metrics']} 条、运行 {migrated_runs} 轮")
        print("源文件未改动：SSOT 仍是 workspace 下的 jsonl / run_summary.json。")
        return 0
    return 1


# ---------------------------------------------------------------- worker / mq
def cmd_worker(args: argparse.Namespace) -> int:
    """独立 worker：消费 Redis Stream 的运行任务（``--once`` 用于验证单条闭环）。"""
    from .mq import MQUnavailable
    from .worker import RunWorker

    config = _load(args)
    config_dir = _resolve_config_dir(args.config)
    registry = build_registry(config, config_dir)
    try:
        worker = RunWorker(config, config_dir=config_dir, registry=registry,
                           worker_id=args.worker_id or None)
    except MQUnavailable as exc:
        print(f"worker 启动失败：{exc}", file=sys.stderr)
        return 2
    if args.poll_ms is not None:
        worker.queue.poll_ms = int(args.poll_ms)
    if args.once:
        worker.worker_registry.register(
            worker.worker_id, queue=worker.queue.mq.stream_key(worker.queue.STREAM),
            group=worker.queue.group, workspace=str(worker.workspace),
        )
        try:
            handled = worker.poll_once(block_ms=int(args.wait_ms))
        finally:
            worker.cleanup()
        print(json.dumps({**worker.status(), "handled": handled},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    worker.run_forever()
    return 0


def cmd_mq(args: argparse.Namespace) -> int:
    """MQ 健康与积压：运行流、消费组 PEL、DLQ、延迟重试、在线 worker、Agent 事件流。"""
    from .mq import mq_status

    config = _load(args)
    report = mq_status(config, _resolve_config_dir(args.config), run_id=args.run_id)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("enabled") else 1


# ---------------------------------------------------------------- config
def cmd_config(args: argparse.Namespace) -> int:
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"配置校验失败：\n{exc}")
        return 1
    if args.action == "validate":
        print("配置校验通过。来源文件：")
        for path in config.source_files:
            print(f"  - {path}")
        return 0
    print(config.describe())
    return 0


# ---------------------------------------------------------------- report
def cmd_report(args: argparse.Namespace) -> int:
    config = _load(args)
    runs_dir = config.runs_dir
    if not runs_dir.exists():
        print("暂无运行记录（workspace/runs 不存在）")
        return 0
    action = args.action or "list"
    if action == "list":
        rows = sorted(runs_dir.glob("*/run_summary.json"))
        if not rows:
            print("暂无运行记录")
            return 0
        print("| 运行 | 需求 | 验收 | 步数 | 回退 | 缺陷 | 报告 |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for row in rows:
            data = json.loads(row.read_text(encoding="utf-8"))
            print(
                f"| {data.get('run_id')} | {str(data.get('requirement_title', ''))[:24]} "
                f"| {'通过' if data.get('verdict') == 'pass' else '未通过'} | {data.get('steps')} "
                f"| {data.get('rollbacks')} | {data.get('bugs_total')} | {data.get('report_path', '-')} |"
            )
        return 0
    target = args.run_id or "latest"
    run_dir = None
    if target == "latest":
        candidates = sorted(runs_dir.glob("*/reports/acceptance_report.md"))
        run_dir = candidates[-1].parent.parent if candidates else None
    else:
        run_dir = runs_dir / target
    report = (run_dir / "reports" / "acceptance_report.md") if run_dir else None
    if not report or not report.exists():
        print(f"未找到验收报告：{target}")
        return 1
    print(report.read_text(encoding="utf-8"))
    return 0


# ---------------------------------------------------------------- skill
def cmd_skill(args: argparse.Namespace) -> int:
    config = _load(args)
    skills_dir = config.skills_dir
    action = args.action

    if action == "list":
        skills = list_skills(skills_dir)
        if not skills:
            print(f"暂无技能（目录：{skills_dir}）")
            return 0
        print("| 技能 | 名称 | 来源 | 版本 | 冒烟 |")
        print("| --- | --- | --- | --- | --- |")
        for skill in skills:
            print(f"| {skill.id} | {skill.name} | {skill.source} | {skill.meta.get('version', '-')} "
                  f"| {'有' if (skill.meta.get('smoke') or {}).get('command') else '无'} |")
        print(f"\n共 {len(skills)} 个（目录：{skills_dir}）")
        return 0

    if action == "show":
        print(describe_skill(load_skill(skills_dir, args.skill_id)))
        return 0

    if action == "add":
        if args.from_run:
            summary = Leader(config).distill_skill(args.from_run)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if summary["status"] == "pass" else 1
        if not args.yaml:
            print("add 需要二选一：--yaml <元信息文件> 或 --from-run <run_id>")
            return 1
        raw = Path(args.yaml).read_text(encoding="utf-8")
        if raw.lstrip().startswith("---"):
            meta, body = parse_skill_text(raw, origin=str(args.yaml))
        else:
            meta, body = yaml.safe_load(raw) or {}, ""
        if not isinstance(meta, dict):
            print("技能元信息必须是映射(dict)")
            return 1
        skill_id = args.skill_id or str(meta.get("name") or "")
        if not skill_id:
            print("缺少技能 id：请用 --id 指定，或在元信息中填 name")
            return 1
        path = save_skill(skills_dir, skill_id, meta, body, overwrite=args.overwrite)
        problems = validate_skill(load_skill(skills_dir, skill_id))
        print(f"已保存：{path}")
        print("元信息校验：" + ("通过" if not problems else "未通过 → " + "；".join(problems)))
        return 0

    if action == "edit":
        values = _parse_assign(args.set or [])
        if not values:
            print("请用 --set key=value 指定要修改的字段")
            return 1
        print(f"已更新：{edit_skill_meta(skills_dir, args.skill_id, values)}")
        return 0

    if action == "remove":
        print(f"已删除：{remove_skill(skills_dir, args.skill_id)}")
        return 0

    if action == "check":
        return _print_check(check_skill(skills_dir, args.skill_id, config))
    return 1


# ---------------------------------------------------------------- mcp
def cmd_mcp(args: argparse.Namespace) -> int:
    config = _load(args)
    action = args.action

    if action == "list":
        servers = load_servers(config)
        if not servers:
            print(f"未配置 MCP 服务（配置文件：{mcp_path(config)}）")
            return 0
        print("| 服务 | 传输 | 启用 | 超时 | 要点 |")
        print("| --- | --- | --- | --- | --- |")
        for server in servers:
            detail = (f"{server.get('command', '')} {' '.join(server.get('args') or [])}".strip()
                      if str(server.get("transport")) == "stdio" else str(server.get("url", "")))
            print(f"| {server.get('name')} | {server.get('transport')} | {server.get('enabled', True)} "
                  f"| {server.get('timeout_s', 10)}s | {detail} |")
        return 0

    if action == "show":
        print(describe_server(get_server(config, args.name)))
        return 0

    if action == "add":
        server: Dict[str, Any] = {
            "name": args.name,
            "transport": args.transport,
            "enabled": True,
        }
        if args.mcp_command:
            server["command"] = args.mcp_command
        if args.args:
            server["args"] = shlex.split(args.args)
        if args.env:
            server["env"] = _parse_assign(args.env)
        if args.url:
            server["url"] = args.url
        if args.header:
            server["headers"] = _parse_assign(args.header)
        if args.timeout_s:
            server["timeout_s"] = args.timeout_s
        problems = validate_server(server)
        if problems:
            print("配置非法：" + "；".join(problems))
            return 1
        print(f"已保存：{add_server(config, server, overwrite=args.overwrite)}")
        return 0

    if action == "remove":
        print(f"已删除：{remove_server(config, args.name)}")
        return 0

    if action == "check":
        params: Optional[Dict[str, Any]] = None
        if args.params:
            try:
                params = json.loads(args.params)
            except json.JSONDecodeError as exc:
                print(f"--params 不是合法 JSON: {exc}")
                return 1
        return _print_check(check_server(config, args.name, call=args.call, params=params))
    return 1


# ---------------------------------------------------------------- 装载结果输出
def _print_check(report: Dict[str, Any]) -> int:
    target = f"{report['kind']}:{report['name']}"
    for item in report.get("checks", []):
        flag = "OK  " if item["ok"] else "FAIL"
        print(f"  [{flag}] {item['stage']}: {item['detail']}")
    if report["ok"]:
        print(f"装载成功：{target}")
        if report.get("tools"):
            print(f"  可用工具：{', '.join(report['tools'])}")
        if report.get("reason"):
            print(f"  提示：{report['reason']}")
        return 0
    print(f"装载失败：{target}")
    print(f"  失败阶段：{report['stage']}")
    print(f"  原因    ：{report['reason']}")
    print(f"  修复建议：{report['hint']}")
    return 1


def _parse_assign(items: List[str]) -> Dict[str, str]:
    """把 ["K=V", ...] 解析成字典。"""
    result: Dict[str, str] = {}
    for item in items:
        key, _, value = str(item).partition("=")
        if not key:
            continue
        result[key.strip()] = value
    return result


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas",
        description="配置驱动的多 Agent 任务协作系统（产品/研发/评审/测试/报告，Leader 统一调度）",
    )
    parser.add_argument("--version", action="version", version=f"mas {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="运行一次需求交付流水线")
    run_p.add_argument("--requirement", "-r", required=True, help="需求文档路径，或配合 --text 的文字需求描述")
    run_p.add_argument("--text", action="store_true", help="把 --requirement 当作文字需求描述")
    run_p.add_argument("--config", default="config", help="配置目录（默认 config）")
    run_p.add_argument("--profile", default=None, help="配置 Profile（strict / lean）")
    run_p.add_argument("--workspace", default=None, help="工作区目录（默认 workspace）")
    run_p.add_argument("--run-id", default=None, help="指定运行 ID")
    run_p.add_argument("--image", action="append", default=[],
                       help="需求附图（可重复；后缀限 png/jpg/jpeg/webp/gif，单张 ≤5MB）")
    run_p.add_argument("--api-key", default=None, help="覆盖 API Key（等价环境变量 OPENAI_API_KEY，优先级更高）")
    run_p.add_argument("--base-url", default=None, help="覆盖 BASE_URL（等价环境变量 OPENAI_BASE_URL，优先级更高）")
    run_p.add_argument("--enqueue", action="store_true",
                       help="只投递到 Redis Stream，由 `python -m mas worker` 执行（需 runtime.mq 启用）")
    run_p.set_defaults(func=cmd_run)

    demo_p = sub.add_parser("demo", help="多轮演示：文档输入 + 经验复用 + 文字输入")
    demo_p.add_argument("--runs", type=int, default=2, help="文档模式运行次数（默认 2）")
    demo_p.add_argument("--requirement", default="examples/requirements/order_pricing_service.md")
    demo_p.add_argument("--config", default="config")
    demo_p.add_argument("--profile", default=None)
    demo_p.add_argument("--workspace", default=None)
    demo_p.add_argument("--api-key", default=None, help="覆盖 API Key（见 config/credentials.yaml 优先级说明）")
    demo_p.add_argument("--base-url", default=None, help="覆盖 BASE_URL")
    demo_p.set_defaults(func=cmd_demo)

    exp_p = sub.add_parser("experience", help="经验库管理")
    exp_p.add_argument("action", nargs="?", default="list", choices=["list", "stats", "report", "prune"])
    exp_p.add_argument("--config", default="config")
    exp_p.add_argument("--profile", default=None)
    exp_p.add_argument("--workspace", default=None)
    exp_p.add_argument("--min-confidence", type=float, default=0.2)
    exp_p.set_defaults(func=cmd_experience)

    store_p = sub.add_parser("store", help="存储后端（MySQL/Redis）管理：状态 / 建表 / 历史数据迁移")
    store_p.add_argument("action", nargs="?", default="status", choices=["status", "init", "migrate"])
    store_p.add_argument("--config", default="config")
    store_p.add_argument("--profile", default=None)
    store_p.add_argument("--workspace", default=None)
    store_p.set_defaults(func=cmd_store)

    worker_p = sub.add_parser("worker", help="运行 worker：消费 Redis Stream 任务（ACK/PEL/重试/死信）")
    worker_p.add_argument("--once", action="store_true", help="只消费一轮后退出（验证/运维用）")
    worker_p.add_argument("--wait-ms", type=int, default=0,
                          help="--once 时阻塞等待新消息的毫秒数（默认 0，不等待）")
    worker_p.add_argument("--poll-ms", type=int, default=None, help="覆盖 runtime.mq.poll_ms")
    worker_p.add_argument("--worker-id", default=None, help="指定 worker 标识（默认 host-pid-随机）")
    worker_p.add_argument("--config", default="config")
    worker_p.add_argument("--profile", default=None)
    worker_p.add_argument("--workspace", default=None)
    worker_p.set_defaults(func=cmd_worker)

    mq_p = sub.add_parser("mq", help="消息队列健康：运行流积压 / 消费组 PEL / DLQ / worker / Agent 事件流")
    mq_p.add_argument("action", nargs="?", default="status", choices=["status"])
    mq_p.add_argument("--run-id", default=None, help="附带该运行的 Agent/事件流消费组详情")
    mq_p.add_argument("--config", default="config")
    mq_p.add_argument("--profile", default=None)
    mq_p.add_argument("--workspace", default=None)
    mq_p.set_defaults(func=cmd_mq)

    cfg_p = sub.add_parser("config", help="配置查看与校验")
    cfg_p.add_argument("action", nargs="?", default="show", choices=["show", "validate"])
    cfg_p.add_argument("--config", default="config")
    cfg_p.add_argument("--profile", default=None)
    cfg_p.add_argument("--workspace", default=None)
    cfg_p.set_defaults(func=cmd_config)

    rep_p = sub.add_parser("report", help="验收报告查看")
    rep_p.add_argument("action", nargs="?", default="list", choices=["list", "show"])
    rep_p.add_argument("run_id", nargs="?", default="latest")
    rep_p.add_argument("--config", default="config")
    rep_p.add_argument("--profile", default=None)
    rep_p.add_argument("--workspace", default=None)
    rep_p.set_defaults(func=cmd_report)

    skl_p = sub.add_parser("skill", help="技能（Skill）管理：添加 / 查看 / 修改 / 删除 / 装载校验")
    skl_p.add_argument("action", choices=["list", "show", "add", "edit", "remove", "check"])
    skl_p.add_argument("skill_id", nargs="?", default=None,
                       help="技能 id（= 目录名）；add 时省略则取元信息中的 name")
    skl_p.add_argument("--yaml", default=None, help="add 时的元信息文件（SKILL.md 或纯 YAML）")
    skl_p.add_argument("--from-run", default=None, help="add 时从一次已通过验收的运行沉淀")
    skl_p.add_argument("--set", action="append", default=[], help="edit 时修改字段，形如 key=value（可重复）")
    skl_p.add_argument("--overwrite", action="store_true", help="add 时允许覆盖同名技能")
    skl_p.add_argument("--config", default="config")
    skl_p.add_argument("--profile", default=None)
    skl_p.add_argument("--workspace", default=None)
    skl_p.set_defaults(func=cmd_skill)

    mcp_p = sub.add_parser("mcp", help="MCP 服务管理：添加 / 查看 / 删除 / 握手冒烟")
    mcp_p.add_argument("action", choices=["list", "show", "add", "remove", "check"])
    mcp_p.add_argument("name", nargs="?", default=None, help="服务名称")
    mcp_p.add_argument("--transport", default=None, choices=["stdio", "sse"])
    # dest 不能用 command：会覆盖顶层 subparsers 的 dest="command"
    mcp_p.add_argument("--command", dest="mcp_command", default=None, help="stdio：启动命令")
    mcp_p.add_argument("--args", default=None, help="stdio：启动参数（整串，按 shell 规则切分）")
    mcp_p.add_argument("--env", action="append", default=[], help="环境变量，形如 K=V（支持 ${VAR} 引用）")
    mcp_p.add_argument("--url", default=None, help="sse：服务地址（http/https）")
    mcp_p.add_argument("--header", action="append", default=[], help="sse：请求头，形如 K=V")
    mcp_p.add_argument("--timeout-s", type=int, default=None, help="超时秒数（默认 10）")
    mcp_p.add_argument("--overwrite", action="store_true", help="add 时允许覆盖同名服务")
    mcp_p.add_argument("--call", default=None, help="check 时额外冒烟调用的工具名")
    mcp_p.add_argument("--params", default=None, help="--call 的工具入参（JSON 字符串）")
    mcp_p.add_argument("--config", default="config")
    mcp_p.add_argument("--profile", default=None)
    mcp_p.add_argument("--workspace", default=None)
    mcp_p.set_defaults(func=cmd_mcp)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
