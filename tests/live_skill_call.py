"""真实链路测试：真实 LLM 调用技能（skill function calling 全链路）。

与 tests/test_skill_tools.py（桩 LLM）不同，本脚本**必须联网并消耗真实模型额度**，
因此文件名不带 test_ 前缀、不参与 unittest 自动收集，需手工运行：

    python tests/live_skill_call.py

链路（全部真实组件，唯一外部依赖是 HPC LLM 端点）：
    config/skills/contract-schema-guard（真实技能 + 真实 scripts/guard.py）
      → Leader._build_tool_runner 真实装配（skill_<id> 注册为 function）
      → 真实 ProductAgent.act（真实 PromptBuilder / 黑板 / 事件日志）
      → 真实 LLM（config/credentials.yaml 的端点）决策发起 tool_calls
      → mas.sandbox.Sandbox 真实执行 guard.py（cwd=技能目录）
      → stdout 回灌为 tool 消息 → 模型续跑产出 requirement_spec

运行结束后留痕：run_dir 下 events.jsonl / llm_traces.jsonl / prompts/*.md；
stdout 打印逐项证据与最终判定（PASS/FAIL），退出码 0/1 对应判定。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mas.agents import create_agent  # noqa: E402
from mas.blackboard import Blackboard  # noqa: E402
from mas.bus import MessageBus  # noqa: E402
from mas.config import load_config  # noqa: E402
from mas.events import EventLog  # noqa: E402
from mas.experience import ExperienceLibrary  # noqa: E402
from mas.leader import Leader  # noqa: E402
from mas.llm import LLMClient  # noqa: E402
from mas.prompts import PromptBuilder  # noqa: E402
from mas.sandbox import Sandbox  # noqa: E402
from mas.schemas import AgentContext, NodeTask  # noqa: E402

TOOL_NAME = "skill_contract_schema_guard"

REQUIREMENT_TEXT = """\
# 报价单样例校验工具

## 背景
客户发来一批报价单样例数据，字段质量参差不齐（数值字段混入字符串、列表元素类型不统一）。
业务方需要一个「报价单结构校验」内部工具：读取报价 JSON，按字段契约输出结构校验报告
（缺失字段 / 类型不符 / 需跳过的畸形元素），供录入系统在入库前做闸口。

## 字段契约（v1）
- orders：list，必填；元素为对象（dict）
- summary.total：int，必填；缺失时按默认值 0 处理

## 客户样例（未校验，直接来自客户）
{"orders": ["订单A", "订单B"], "summary": {"failed": 1}}

## 要求
1. 评估前先调用技能工具 skill_contract_schema_guard 做一次真实结构校验：
   - contract_spec 传上述字段契约（orders 为 expected_type=list、required=true、element_type=dict；
     summary.total 为 expected_type=int、required=true、default=0）；
   - payload 传上面「客户样例」的 JSON；
   - node_id 传 "intake"。
2. 把工具返回的校验报告结论（哪些字段类型不符、跳过了多少畸形元素、是否可放行）
   写进本次需求评估的背景与相关功能点描述中。
"""


def _load_events(run_dir: Path):
    path = run_dir / "events.jsonl"
    records = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _load_traces(run_dir: Path):
    path = run_dir / "llm_traces.jsonl"
    records = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def main() -> int:
    config = load_config(PROJECT_ROOT / "config", workspace=PROJECT_ROOT / "workspace")
    config.runtime["verbose"] = False
    skills_cfg = dict(config.runtime.get("skills") or {})
    skills_cfg.update({"enabled": True, "tools_enabled": True})
    config.runtime["skills"] = skills_cfg
    config.llm["tool_max_rounds"] = 3   # 容忍多轮工具请求，最后一次总结仍能返回

    run_id = "live-skill-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = config.runs_dir / run_id
    for sub in ("input", "code", "reports", "evidence"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    print(f"[LIVE] run_dir = {run_dir}")
    print(f"[LIVE] 端点 = {config.llm.get('base_url') or '(credentials.yaml)'}"
          f"｜模型 = {config.llm.get('model')}｜stream = {config.llm.get('stream')}")

    events = EventLog(run_dir / "events.jsonl", run_id, echo=False)
    bus = MessageBus(run_dir / "messages.jsonl", events, run_id)
    blackboard = Blackboard(run_dir / "artifacts", events, run_id)
    sandbox = Sandbox(run_dir / "code", config, events=events)
    experience = ExperienceLibrary(config.workspace_dir, config.learning, events, run_id)
    prompts = PromptBuilder(config, run_dir, enabled=True)
    llm = LLMClient(config)

    ctx = AgentContext(
        config=config, run_id=run_id, run_dir=run_dir, events=events, blackboard=blackboard,
        bus=bus, llm=llm, sandbox=sandbox, prompts=prompts, experience=experience,
        log=lambda level, message: print(f"  [{level}] {message}"),
        run_state={},
    )
    agents = {role_id: create_agent(role_id, ctx) for role_id in config.roles}

    # 真实装配（与 Leader.run 同一方法）：技能工具注册进 ToolRunner
    ctx.tools = Leader(config)._build_tool_runner(events)
    if ctx.tools is None:
        print("[LIVE][FAIL] 工具装配失败：_build_tool_runner 返回 None")
        return 1
    tool_names = [tool["function"]["name"] for tool in ctx.tools.schemas]
    print(f"[LIVE] 已注册工具: {tool_names}")
    if TOOL_NAME not in tool_names:
        print(f"[LIVE][FAIL] 未注册期望的技能工具 {TOOL_NAME}")
        return 1

    # 发布需求输入（与 Leader.run 一致：requirement_source 进黑板，intake 感知它）
    blackboard.publish(
        "requirement_source",
        {"title": "报价单样例校验工具", "text": REQUIREMENT_TEXT, "source": "text",
         "chars": len(REQUIREMENT_TEXT), "run_id": run_id},
        producer="leader", node="intake", iteration=0,
        summary=f"需求输入（{len(REQUIREMENT_TEXT)} 字符）",
    )

    task = NodeTask(node_id="intake", title="需求评估与拆解", role="product",
                    kind="task", iteration=1, inputs=["requirement_source"],
                    outputs=["requirement_spec"])
    agent = agents["product"]
    print("[LIVE] 开始真实调用（intake 节点，真实 LLM + 沙箱）……")
    result = agent.act(task)

    # ---------------------------------------------------------------- 证据汇总
    records = _load_events(run_dir)
    traces = _load_traces(run_dir)
    calls = [r for r in records if r.get("type") == "tool_calls_requested"]
    executed = [r for r in records if r.get("type") == "skill_tool_executed"]
    registered = [r for r in records if r.get("type") == "skill_tool_registered"]
    sandbox_execs = [r for r in records if r.get("type") == "sandbox_exec"]

    print("\n======================= 真实链路证据 =======================")
    print(f"1) 技能工具注册      : {[r['payload']['tool'] for r in registered]}")
    for record in calls:
        for call in record.get("payload", {}).get("calls", []):
            args = call.get("arguments")
            args_keys = sorted(args) if isinstance(args, dict) else args
            print(f"2) 模型发起 tools/call: {call.get('name')}（参数键 {args_keys}）")
    for record in executed:
        payload = record.get("payload", {})
        print(f"3) 沙箱执行技能脚本   : skill={payload.get('skill')} script={payload.get('script')} "
              f"exit={payload.get('exit_code')} ok={payload.get('ok')} "
              f"耗时={payload.get('duration_ms')}ms stdout={payload.get('stdout_chars')}字符")
    for record in sandbox_execs:
        print(f"   沙箱执行事件        : cmd={record.get('payload', {}).get('cmd')} "
              f"exit={record.get('payload', {}).get('exit_code')}")
    print(f"4) LLM 调用次数       : {len(traces)}")
    for index, trace in enumerate(traces, start=1):
        print(f"   - 第{index}轮: tool_calls={trace.get('tool_calls')} "
              f"finish={trace.get('finish_reason')} 输出{trace.get('output') and len(trace['output'])}字符")

    spec = blackboard.get("requirement_spec") or {}
    print(f"5) 节点结果           : status={result.status} "
          f"error={result.error[:200] if result.error else ''}")
    print(f"   summary            : {result.summary[:200]}")
    if spec:
        blob = json.dumps(spec, ensure_ascii=False)
        mentions = [token for token in ("类型不符", "畸形", "跳过", "cases[0]", "orders[0]",
                                        "summary.total", "预期", "校验")
                    if token in blob]
        print(f"6) 产物引用校验结论   : {mentions or '（未直接命中关键词，需人工核验 background/fp 描述）'}")

    # ---------------------------------------------------------------- 判定
    print("\n======================= 判定 =======================")
    ok = True
    if not calls:
        print("[FAIL] 模型未发起任何 tool_calls（工具未被真实调用）")
        ok = False
    else:
        names = [call.get("name") for record in calls
                 for call in record.get("payload", {}).get("calls", [])]
        if TOOL_NAME not in names:
            print(f"[FAIL] 工具调用名单里没有 {TOOL_NAME}: {names}")
            ok = False
        else:
            print(f"[PASS] 模型真实发起 tools/call: {names}")
    if not executed or not any(r["payload"].get("ok") for r in executed):
        print("[FAIL] 技能脚本未被沙箱成功执行")
        ok = False
    else:
        print("[PASS] 技能脚本经沙箱真实执行且退出码 0（stdout 已回灌给模型）")
    if len(traces) < 2:
        print(f"[WARN] LLM 调用仅 {len(traces)} 轮：模型未在工具结果上续跑")
    else:
        print(f"[PASS] 工具结果回灌后模型续跑（共 {len(traces)} 轮 LLM 调用）")
    if result.status != "pass":
        print(f"[WARN] intake 节点最终 status={result.status}（工具链路本身已达成，"
              f"节点失败原因见上）")

    print(f"\n[{'PASS' if ok else 'FAIL'}] 证据目录: {run_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
