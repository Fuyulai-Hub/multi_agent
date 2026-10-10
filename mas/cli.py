"""命令行入口。

用法：
    python -m mas run --requirement examples/requirements/order_pricing_service.md
    python -m mas run --requirement "文字需求描述……" --text
    python -m mas run --requirement ... --profile strict
    python -m mas demo                       # 三轮演示（文档 → 复用经验 → 文字需求输入）
    python -m mas experience list|stats|report|prune
    python -m mas config show|validate [--profile lean]
    python -m mas report list|show [run_id|latest]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .config import SystemConfig, load_config
from .errors import ConfigError
from .experience import ExperienceLibrary
from .leader import Leader, RunSummary

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


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    _apply_credential_overrides(config, args)
    leader = Leader(config)
    summary = leader.run(args.requirement, as_text=args.text, run_id=args.run_id)
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
    print("=" * 72)
    print("演示 1/3：需求文档输入（首轮，含注入缺陷 → 审查驳回 → 缺陷修复闭环）")
    print("=" * 72)
    summaries.append(Leader(config).run(str(requirement_path)))

    for index in range(2, args.runs + 1):
        print("\n" + "=" * 72)
        print(f"演示 {index}/{args.runs + 1}：同一需求再次运行（经验库注入 → 观察自学习收益）")
        print("=" * 72)
        summaries.append(Leader(config).run(str(requirement_path)))

    print("\n" + "=" * 72)
    print(f"演示 {args.runs + 1}/{args.runs + 1}：文字需求描述输入（--text，验证双输入通道）")
    print("=" * 72)
    text = requirement_path.read_text(encoding="utf-8")
    summaries.append(Leader(config).run(text, as_text=True))

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
    run_p.add_argument("--api-key", default=None, help="覆盖 API Key（等价环境变量 OPENAI_API_KEY，优先级更高）")
    run_p.add_argument("--base-url", default=None, help="覆盖 BASE_URL（等价环境变量 OPENAI_BASE_URL，优先级更高）")
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
