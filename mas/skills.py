"""技能（Skill）注册表：目录规范、持久化、参数校验与冒烟。

目录规范（一个技能一个目录）：

    config/skills/<skill-id>/
    ├── SKILL.md          # 必需：YAML front-matter（元信息）+ 正文
    ├── scripts/          # 可选：可执行脚本（冒烟命令默认以此为工作目录执行）
    ├── references/       # 可选：参考资料
    └── assets/           # 可选：模板资源

SKILL.md 的 front-matter 必填字段与需求条目一一对应：

| 字段 | 含义 |
| --- | --- |
| `name` | 名称 |
| `description` | 描述 |
| `when_to_use` | 适用场景 |
| `trigger` | 触发条件（keywords / intents / requires_artifacts） |
| `inputs` | 入参说明（每项含 name / type / required / description） |
| `steps` | 执行步骤（非空字符串列表） |

可选字段：`version`、`source`（manual | distilled）、`source_run`、`outputs`、
`error_handling`、`checkpoints`、`smoke`、`tags`。正文用于补充说明，不参与校验。

本模块不依赖 LLM，全部为确定性实现；冒烟执行复用 `mas.sandbox.Sandbox`。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .config import SystemConfig
from .errors import ConfigError, SandboxError
from .sandbox import Sandbox

__all__ = [
    "SKILL_FILE",
    "REQUIRED_FIELDS",
    "Skill",
    "parse_skill_text",
    "render_skill_text",
    "load_skill",
    "list_skills",
    "save_skill",
    "remove_skill",
    "edit_skill_meta",
    "validate_skill",
    "check_skill",
    "describe_skill",
    "load_run_artifacts",
    "load_run_facts",
    "distill_meta",
]

SKILL_FILE = "SKILL.md"
SOURCE_MANUAL = "manual"
SOURCE_DISTILLED = "distilled"

# 必填字段，对应需求中的「名称 / 描述 / 适用场景 / 触发条件 / 入参说明 / 执行步骤」
REQUIRED_FIELDS = ("name", "description", "when_to_use", "trigger", "inputs", "steps")

# front-matter：`---\n<yaml>\n---\n<正文>`
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

_SKILL_ID = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")


@dataclass
class Skill:
    """一个已落盘的技能。"""

    id: str
    path: Path
    meta: Dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def name(self) -> str:
        return str(self.meta.get("name") or self.id)

    @property
    def description(self) -> str:
        return str(self.meta.get("description") or "")

    @property
    def source(self) -> str:
        return str(self.meta.get("source") or SOURCE_MANUAL)

    @property
    def dir(self) -> Path:
        return self.path.parent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "source_run": self.meta.get("source_run"),
            "version": self.meta.get("version"),
            "tags": list(self.meta.get("tags") or []),
            "has_smoke": bool((self.meta.get("smoke") or {}).get("command")),
            "path": str(self.path),
        }


# --------------------------------------------------------------------------- 解析与渲染
def parse_skill_text(text: str, *, origin: str = SKILL_FILE) -> Tuple[Dict[str, Any], str]:
    """拆分 SKILL.md 的 front-matter 与正文。"""
    match = _FRONT_MATTER.match(text or "")
    if not match:
        raise ConfigError(f"{origin} 缺少 YAML front-matter（文件需以 `---` 开头并以 `---` 结束元信息块）")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{origin} 的 front-matter 不是合法 YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise ConfigError(f"{origin} 的 front-matter 顶层必须是映射(dict)")
    return meta, text[match.end():]


def render_skill_text(meta: Dict[str, Any], body: str = "") -> str:
    """把元信息与正文渲染成 SKILL.md 文本。"""
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip("\n")
    return f"---\n{dumped}\n---\n\n{(body or '').strip()}\n"


# --------------------------------------------------------------------------- 读写
def load_skill(skills_dir: Path, skill_id: str) -> Skill:
    path = Path(skills_dir) / skill_id / SKILL_FILE
    if not path.exists():
        raise ConfigError(f"技能不存在: {skill_id}（未找到 {path}）")
    meta, body = parse_skill_text(path.read_text(encoding="utf-8"), origin=str(path))
    return Skill(id=skill_id, path=path, meta=meta, body=body)


def list_skills(skills_dir: Path) -> List[Skill]:
    """按目录扫描全部技能；单个技能解析失败不中断整体列出。"""
    base = Path(skills_dir)
    if not base.exists():
        return []
    skills: List[Skill] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        path = entry / SKILL_FILE
        if not path.exists():
            continue
        try:
            meta, body = parse_skill_text(path.read_text(encoding="utf-8"), origin=str(path))
        except ConfigError:
            continue
        skills.append(Skill(id=entry.name, path=path, meta=meta, body=body))
    return skills


def save_skill(
    skills_dir: Path,
    skill_id: str,
    meta: Dict[str, Any],
    body: str = "",
    *,
    overwrite: bool = False,
) -> Path:
    """落盘一个技能；`overwrite=False` 时已存在即报错（防止覆盖手写成果）。"""
    if not _SKILL_ID.match(skill_id):
        raise ConfigError(f"技能 id 非法: {skill_id}（只允许小写字母、数字与连字符，且以字母或数字开头）")
    directory = Path(skills_dir) / skill_id
    path = directory / SKILL_FILE
    if path.exists() and not overwrite:
        raise ConfigError(f"技能已存在: {skill_id}（如需覆盖请显式指定 --overwrite）")
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(render_skill_text(meta, body), encoding="utf-8")
    return path


def remove_skill(skills_dir: Path, skill_id: str) -> Path:
    """删除一个技能目录（连带 scripts/references/assets）。"""
    directory = Path(skills_dir) / skill_id
    if not directory.exists():
        raise ConfigError(f"技能不存在: {skill_id}")
    if not (directory / SKILL_FILE).exists():
        raise ConfigError(f"{directory} 不是合法技能目录（缺少 {SKILL_FILE}）")
    shutil.rmtree(directory)
    return directory


def edit_skill_meta(skills_dir: Path, skill_id: str, values: Dict[str, Any]) -> Path:
    """修改已有技能的 front-matter 顶层标量字段（正文保持不变）。"""
    skill = load_skill(skills_dir, skill_id)
    for key, value in values.items():
        if isinstance(value, (dict, list)):
            raise ConfigError(f"暂不支持通过 --set 修改复合字段: {key}（请直接编辑 {skill.path}）")
        skill.meta[key] = value
    skill.path.write_text(render_skill_text(skill.meta, skill.body), encoding="utf-8")
    return skill.path


# --------------------------------------------------------------------------- 校验
def validate_skill(skill: Skill) -> List[str]:
    """元信息校验，返回问题列表（为空即通过）。"""
    problems: List[str] = []
    meta = skill.meta

    if not _SKILL_ID.match(skill.id):
        problems.append(f"技能目录名非法: {skill.id}（只允许小写字母、数字与连字符）")
    for name in REQUIRED_FIELDS:
        if name not in meta or meta[name] in (None, "", [], {}):
            problems.append(f"缺少必填字段: {name}")

    if isinstance(meta.get("trigger"), dict):
        trigger = meta["trigger"]
        if not (trigger.get("keywords") or trigger.get("intents")):
            problems.append("trigger 至少要有 keywords 或 intents 之一")
    elif "trigger" in meta:
        problems.append("trigger 必须是映射(dict)")

    inputs = meta.get("inputs")
    if isinstance(inputs, list):
        for index, item in enumerate(inputs):
            if not isinstance(item, dict) or not item.get("name"):
                problems.append(f"inputs[{index}] 必须是含 name 的映射(dict)")
    elif inputs is not None:
        problems.append("inputs 必须是列表")

    steps = meta.get("steps")
    if isinstance(steps, list):
        if steps and not all(isinstance(step, str) and step.strip() for step in steps):
            problems.append("steps 的每一项必须是非空字符串")
    elif steps is not None:
        problems.append("steps 必须是列表")

    smoke = meta.get("smoke")
    if smoke is not None:
        if not isinstance(smoke, dict) or not smoke.get("command"):
            problems.append("smoke 必须是含 command 的映射(dict)")
        elif not isinstance(smoke["command"], list) or not smoke["command"]:
            problems.append("smoke.command 必须是非空列表（如 [python, -c, \"print(1)\"]）")

    source = meta.get("source")
    if source is not None and source not in (SOURCE_MANUAL, SOURCE_DISTILLED):
        problems.append(f"source 非法: {source}（manual | distilled）")

    return problems


# --------------------------------------------------------------------------- 装载与冒烟
def _smoke_skill(skill: Skill, config: SystemConfig) -> Dict[str, Any]:
    """执行冒烟：以技能目录为工作目录运行 `smoke.command` 并断言退出码。"""
    smoke = skill.meta.get("smoke") or {}
    command = [str(item) for item in (smoke.get("command") or [])]
    expected = int(smoke.get("expect_exit", 0))
    sandbox = Sandbox(skill.dir, config)
    try:
        result = sandbox.run(command, timeout=int(smoke.get("timeout_s", 0)) or None)
    except SandboxError as exc:
        return {
            "ran": False,
            "ok": False,
            "reason": str(exc),
            "hint": "检查 smoke.command 的首个参数是否为可执行文件；本机找不到时请改用绝对路径",
        }
    ok = result.exit_code == expected
    return {
        "ran": True,
        "ok": ok,
        "detail": (f"冒烟通过：{' '.join(command)}（退出码 {result.exit_code}，耗时 {result.duration_ms}ms）"
                   if ok else ""),
        "cmd": " ".join(command),
        "exit_code": result.exit_code,
        "expected_exit": expected,
        "duration_ms": result.duration_ms,
        "stdout_tail": result.tail(result.stdout, 400),
        "stderr_tail": result.tail(result.stderr, 400),
        "reason": "" if ok else f"冒烟退出码 {result.exit_code} != 期望 {expected}",
        "hint": "" if ok else "查看 stderr 定位失败原因，或修正 smoke.expect_exit",
    }


def check_skill(skills_dir: Path, skill_id: str, config: SystemConfig) -> Dict[str, Any]:
    """自动装载：解析 → 参数校验 → 冒烟，任一阶段失败即给出可执行的修复建议。"""
    report: Dict[str, Any] = {
        "ok": False,
        "kind": "skill",
        "name": skill_id,
        "stage": "",
        "checks": [],
        "reason": "",
        "hint": "",
        "path": "",
    }

    try:
        skill = load_skill(skills_dir, skill_id)
    except ConfigError as exc:
        report["stage"] = "parse"
        report["reason"] = str(exc)
        report["hint"] = f"确认 {Path(skills_dir) / skill_id / SKILL_FILE} 存在且以 `---` 包裹 YAML 元信息"
        return report
    report["path"] = str(skill.path)
    report["checks"].append({"stage": "parse", "ok": True, "detail": "front-matter 解析成功"})

    problems = validate_skill(skill)
    report["checks"].append({
        "stage": "validate",
        "ok": not problems,
        "detail": "元信息校验通过" if not problems else "；".join(problems),
    })
    if problems:
        report["stage"] = "validate"
        report["reason"] = "元信息校验未通过：" + "；".join(problems)
        report["hint"] = "补齐 front-matter 必填字段：" + "、".join(REQUIRED_FIELDS)
        return report

    if not (skill.meta.get("smoke") or {}).get("command"):
        report["ok"] = True
        report["stage"] = "smoke"
        report["checks"].append({
            "stage": "smoke", "ok": True, "detail": "未声明 smoke.command，跳过动态冒烟（仅静态校验）",
        })
        report["reason"] = "未声明 smoke.command，只做了静态校验，未做动态验证"
        return report

    outcome = _smoke_skill(skill, config)
    report["checks"].append({
        "stage": "smoke", "ok": bool(outcome["ok"]),
        "detail": outcome.get("detail") or outcome.get("reason") or "冒烟通过",
    })
    report["smoke"] = outcome
    if not outcome["ok"]:
        report["stage"] = "smoke"
        report["reason"] = outcome["reason"]
        report["hint"] = outcome["hint"]
        return report

    report["ok"] = True
    report["stage"] = "done"
    return report


# --------------------------------------------------------------------------- 沉淀
def load_run_artifacts(run_dir: Path) -> Dict[str, Any]:
    """读取一次历史运行的黑板产物（取每个产物的最新版本）。"""
    artifacts_dir = Path(run_dir) / "artifacts"
    facts: Dict[str, Any] = {}
    if not artifacts_dir.exists():
        return facts
    for entry in sorted(artifacts_dir.iterdir()):
        latest = entry / "latest.json"
        if not latest.exists():
            continue
        try:
            facts[entry.name] = json.loads(latest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return facts


def _final_node_path(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从事件日志中还原「最终通过路径」：同一节点只保留其最后一次迭代。"""
    order: List[str] = []
    info: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if str(record.get("type")) != "node_start":
            continue
        node = str(record.get("node") or "")
        if not node:
            continue
        iteration = int(record.get("iteration") or 1)
        if node not in info:
            order.append(node)
            info[node] = {"iteration": iteration, "title": node}
        info[node]["iteration"] = max(info[node]["iteration"], iteration)
        title = str((record.get("payload") or {}).get("title") or "")
        if title:
            info[node]["title"] = title
    return [{"node": node, "iteration": info[node]["iteration"], "title": info[node]["title"]}
            for node in order]


def _parameterize(text: str, run_dir: Path, workspace_dir: Optional[Path]) -> str:
    """把与本次运行绑定的路径与 ID 换成占位符，避免沉淀出环境相关的细节。"""
    result = str(text or "")
    for value, placeholder in (
        (str(run_dir), "{{run_dir}}"),
        (str(run_dir.parent), "{{runs_dir}}"),
        (str(workspace_dir) if workspace_dir else "", "{{workspace}}"),
        (run_dir.name, "{{run_id}}"),
    ):
        if value:
            result = result.replace(value.replace("\\", "/"), placeholder).replace(value, placeholder)
    return result


def load_run_facts(run_dir: Path, workspace_dir: Optional[Path] = None) -> Dict[str, Any]:
    """把一次「验收通过」的运行提炼成沉淀事实。

    保留：最终通过路径的节点序列、需求功能点、门禁校验点、缺陷类型对应的防御。
    剔除：被回退的失败迭代、缺陷试错细节、绝对路径 / run_id / 时间戳等环境绑定信息。
    """
    run_dir = Path(run_dir)
    summary: Dict[str, Any] = {}
    summary_path = run_dir / "run_summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            summary = {}

    events_path = run_dir / "events.jsonl"
    records: List[Dict[str, Any]] = []
    if events_path.exists():
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    artifacts = load_run_artifacts(run_dir)
    spec = artifacts.get("requirement_spec") or {}
    gate = artifacts.get("acceptance_gate") or {}
    bugs = artifacts.get("bug_reports") or {}

    path = _final_node_path(records)
    title = _parameterize(str(spec.get("title") or summary.get("requirement_title") or ""),
                          run_dir, workspace_dir)

    checkpoints = [
        f"{item.get('id')}：{item.get('expr')}"
        for item in (gate.get("criteria") or []) if isinstance(item, dict) and item.get("expr")
    ]
    categories = sorted({
        str(item.get("category")) for item in (bugs.get("items") or [])
        if isinstance(item, dict) and item.get("category")
    })
    error_handling = [
        f"{label}类缺陷：实现时同步覆盖该场景并补充回归用例" for label in categories
    ]
    if any(str(record.get("type")) == "rollback" for record in records):
        error_handling.append("门禁未通过时按回退策略回到修复节点，不要绕过门禁直接放行")

    steps = [
        f"{item['title']}（节点 {item['node']}，最终第 {item['iteration']} 轮通过）"
        for item in path
    ]
    tags = [str(tag) for tag in (spec.get("tags") or [])]

    return {
        "run_id": run_dir.name,
        "verdict": str(summary.get("verdict", "")),
        "title": title,
        "tags": tags,
        "node_path": [item["node"] for item in path],
        "steps": [_parameterize(step, run_dir, workspace_dir) for step in steps],
        "checkpoints": [_parameterize(item, run_dir, workspace_dir) for item in checkpoints],
        "error_handling": error_handling,
        "bug_categories": categories,
        "function_points": [
            {"id": item.get("id"), "title": str(item.get("title") or "")}
            for item in (spec.get("function_points") or []) if isinstance(item, dict)
        ],
        "metrics": {key: summary.get(key) for key in ("steps", "rollbacks", "bugs_total", "bugs_fixed")},
    }


def distill_meta(facts: Dict[str, Any]) -> Dict[str, Any]:
    """把沉淀事实组装成 SKILL.md 的 front-matter（未填字段由使用者补充）。"""
    title = str(facts.get("title") or "未命名需求")
    tags = list(facts.get("tags") or [])
    return {
        "name": f"distilled-{facts.get('run_id', 'run')}",
        "description": f"复用《{title}》的已验证交付路径：{' → '.join(facts.get('node_path') or [])}",
        "version": "0.1.0",
        "source": SOURCE_DISTILLED,
        "source_run": facts.get("run_id", ""),
        "when_to_use": [
            f"交付与《{title}》结构相近的需求（同类功能点：{len(facts.get('function_points') or [])} 个）",
            "希望复用一次已验收通过的流程，而不是从头试错",
        ],
        "trigger": {
            "keywords": tags or ["distilled"],
            "intents": ["delivery"],
            "requires_artifacts": ["requirement_spec"],
        },
        "inputs": [{
            "name": "requirement",
            "type": "string",
            "required": True,
            "default": "{{workspace}}/input/requirement.md",
            "description": "需求描述文本或需求文档路径",
        }],
        "steps": list(facts.get("steps") or []),
        "error_handling": list(facts.get("error_handling") or []),
        "checkpoints": list(facts.get("checkpoints") or []),
        "tags": tags,
    }


def describe_skill(skill: Skill) -> str:
    """技能详情（供 `skill show` 输出）。"""
    lines = [
        f"技能      : {skill.name}（id={skill.id}）",
        f"来源      : {skill.source}" + (f"（沉淀自运行 {skill.meta.get('source_run')}）" if skill.meta.get("source_run") else ""),
        f"版本      : {skill.meta.get('version', '-')}",
        f"路径      : {skill.path}",
        f"描述      : {skill.description}",
    ]
    when_to_use = skill.meta.get("when_to_use") or []
    lines.append("适用场景  :")
    if when_to_use:
        lines.extend(f"  - {item}" for item in when_to_use)
    else:
        lines.append("  - （未填写）")
    trigger = skill.meta.get("trigger") or {}
    lines.append(f"触发条件  : keywords={trigger.get('keywords') or []} intents={trigger.get('intents') or []}")
    inputs = skill.meta.get("inputs") or []
    lines.append(f"入参({len(inputs)}):")
    for item in inputs:
        if isinstance(item, dict):
            lines.append(
                f"  - {item.get('name')}（{item.get('type', 'any')}"
                f"{'，必填' if item.get('required') else '，可选'}）：{item.get('description', '')}"
            )
    steps = skill.meta.get("steps") or []
    lines.append(f"执行步骤({len(steps)}):")
    lines.extend(f"  {index}. {step}" for index, step in enumerate(steps, start=1))
    for key in ("error_handling", "checkpoints"):
        items = skill.meta.get(key) or []
        if items:
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)
