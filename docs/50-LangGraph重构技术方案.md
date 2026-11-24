# 50 · LangGraph 重构技术方案（编排层替换）

> 对象：`G:/mycv/cragent/multi_agent`（`mas/` + `frontend/` 合计 27065 行）
> 结论：**选 LangGraph 1.2.x，只替换编排层（替换 180 行循环 + 新增 560 行，约占全仓 2%），其余模块一行不动。**
> 所有代码位置与行号均已核验；工作量与占比为估算，已单独标注。

---

## 0. 三句话摘要

1. **框架选 LangGraph**，不是因为它最流行，而是因为 `flow.yaml` 的「节点 + pass/fail 条件边 + 回退环」与 `StateGraph` 是同构的，迁移不需要重新设计模型。
2. **只换编排，不换引擎**。`agents/`、`sandbox.py`、`verifier.py`、`expr.py`、`blackboard.py`、经验库、技能、前端全部保留——这些是项目唯一有辨识度的部分，框架替不了。
3. **迁移用双轨 + 开关 + A/B 对照**，`runtime.orchestrator: legacy | langgraph`。811 个现有用例必须全绿，任何一个断言都不改。

---

## 1. 现状核验（源码事实，非推断）

| 事实 | 位置 | 对重构的意义 |
|---|---|---|
| `Leader.run()` 是唯一编排入口，签名 `(requirement, *, as_text, run_id, images, cancel_event, registry, resume)` | `mas/leader.py:392` | 门面必须保持，CLI/前端/worker 全都不用改 |
| 手写状态机主循环 | `mas/leader.py:682` `while node_id and node_id not in flow.terminal and steps < max_steps` | **这就是要被替换的 180 行** |
| 节点执行复用：`_run_gate` / `_act_with_retry` / `_extract_inline` | `leader.py:1177` / `2010` / `2192` | 图节点内部直接调，零改动 |
| 回退判定 `_is_rollback`、不可修复判定 `_unfixable_failure_reason` | `leader.py:2319` / `2356` | 下沉成 `decide()` 纯函数 |
| 已有断点续跑：`_write_checkpoint` / `_load_checkpoint` / `--resume` | `leader.py:354` / `312` | **与 LangGraph checkpointer 功能重叠，必须定权威源** |
| 节点定义数据结构 `NodeConfig`（kind/role/inputs/outputs/transitions/criteria/retry/iterable） | `mas/config.py:229` | 直接喂给 `add_node` / `add_conditional_edges` |
| `FlowConfig.target(branch)`、`is_backward()` | `config.py:270` / `301` | 条件边 router 的唯一数据来源 |
| `CollaborationConfig.branch_of()` / `rollback_policy()` | `config.py:394` / `408` | 回退语义，不重写 |
| Agent 六步骨架 `act()`，同步、返回 `AgentResult` | `mas/agents/base.py:171` | **天然是 LangGraph 节点函数**，包一层即可 |
| `RunCanceledError` 必须在 `act()` 内原样穿透 | `base.py:215` | 它继承 `BaseException`；LangGraph 只 catch `Exception`，不会吞 |
| 回归基线 | `tests/` 53 个文件 / 811 个用例 | 验收门槛，不允许改断言 |

**关键判断**：这个项目已经把 LangGraph 的一半能力自己实现了（checkpoint/resume、事件流、并行注册表、取消）。重构的收益不在「从零获得能力」，而在「把手写的 180 行循环换成有 type、有 checkpointer、有可视化的标准件」。

---

## 2. 选型对比（8 个维度，1–5 主观评分，依据是下面逐条淘汰理由）

| 维度 | LangGraph 1.2 | Google ADK | CrewAI | AutoGen 0.4 | OpenAI Agents SDK | Temporal |
|---|---|---|---|---|---|---|
| 条件边 + 回退环表达力 | **5** | 4（LoopAgent） | 2（无条件边） | 3 | 2 | 5 |
| 类型化状态 + reducer | **5** | 4 | 3 | 3 | 3 | 2 |
| 持久化 / 断点续跑 | **5** | 4 | 2 | 3 | 2 | **5** |
| 人工介入（interrupt） | **5** | 4 | 2 | 3 | 3 | 4 |
| 配置驱动保留难度（越高越好） | 4 | 3 | 2 | 2 | 2 | 2 |
| 迁移侵入面（越小越好） | **4** | 3 | 2 | 2 | 3 | 1 |
| 生态 / 长期维护 | **5** | 3 | 4 | 3 | 4 | 5 |
| 依赖重量（越轻越好） | 3 | 3 | 4 | 2 | **5** | 2 |
| **合计** | **36** | 28 | 21 | 21 | 24 | 26 |

**淘汰理由（逐条具体）**

- **CrewAI**：`sequential` process 只能表达直线流水线，没有条件边。你的 `review fail → implement`、`test fail → optimize` 两个环无处落地；YAML 里也不能写 `transitions`。
- **AutoGen**：GroupChat 的发言者选择是非确定性的，而 MAS 的全部价值建立在「确定性交付 + 可核验门禁」上，用了等于自我削弱。
- **Google ADK**：`LoopAgent` 能表达回退，但绑定 A2A/GCP 心智，Python 生态与调试成熟度落后一代。
- **OpenAI Agents SDK**：没有显式图，`handoff` 语义是「移交控制权」，不是「按条件回到上一个节点」；回退环仍要自己写，省不了多少。
- **Temporal**：确定性与超时治理最强，但每个 LLM 调用要包成 activity，没有 agent 抽象；改造面比 LangGraph 大一个量级，不符合「最小改动」。

**LangGraph 的两个硬伤（如实记录）**

1. 状态必须可序列化（TypedDict of 标量/列表）。`Blackboard` 对象、`Path`、大文本**不能**进 state。
2. Router 在 superstep 边界会被重新计算，**router 里不能有副作用**（否则重跑会重复发事件）。

两条都能规避，见 §6。

---

## 3. 结构映射：`flow.yaml` → `StateGraph`

| 现有 | LangGraph | 说明 |
|---|---|---|
| `flow.entry` | `add_edge(START, entry)` | — |
| `flow.nodes[i]`（`kind: task`） | `add_node(i, make_task_node(...))` | 节点函数内部调 `_act_with_retry` |
| `flow.nodes[i]`（`kind: gate`） | 同一个 `add_node`，内部分流到 `_run_gate` | 不需要单独的 gate 类型 |
| `node.transitions: {pass: X, fail: Y}` | `add_conditional_edges(i, route, path_map)` | router 返回 `state["next_node"]` |
| `flow.terminal: ["end","halt"]` | 映射到 `END` | halt 的语义靠 `state["forced_fail"]` 保留 |
| `flow.max_steps` | `recursion_limit` | **必须显式设**，否则抛 `GraphRecursionError` |
| `iterations[node]` | `state["iterations"]`（reducer 合并） | — |
| `rollback_counts[node]` | `state["rollback_counts"]`（reducer 合并） | — |
| `checkpoint.json` | `SqliteSaver`（`run_dir/graph.sqlite`） | 单一权威源，见 §6.3 |
| `events.emit("node_start"/"node_end"/"rollback")` | 节点函数内照旧 emit | **不替换事件系统**，LangGraph 的 `stream` 只做旁路 |
| `ctx.run_state` | `state["run_state"]`（可序列化投影） | 节点入口写回 `ctx.run_state` |

---

## 4. 重构边界（最重要的一节）

### 4.1 只改这 4 个文件

| 文件 | 动作 | 量级 |
|---|---|---|
| `mas/leader.py` | `run()` 内部按开关分流；抽出 `_execute_node()` / `_decide()` 两个方法供图节点复用 | 改 ~120 行，删 ~180 行 |
| `mas/config.py` | 新增 `runtime.orchestrator` 读取与校验 | +15 行 |
| `config/system.yaml` | 新增 `runtime.orchestrator: "legacy"`（默认关闭） | +1 行 |
| `mas/orchestration/*`（新） | 见 §5 | +约 560 行 |

### 4.2 明确不动（一行都不碰）

```
mas/agents/*            六步骨架、ReAct、工具多轮、技能注入、经验检索
mas/sandbox.py          1472 行，进程隔离与超时
mas/verifier.py         1056 行，签名与 attest
mas/hidden.py           431 行，隐藏用例
mas/expr.py             门禁表达式 DSL（BoolOp 不短路等既有语义）
mas/blackboard.py       版本化产物
mas/events.py           事件日志与双写（MySQL/MQ sink）
mas/experience.py       1138 行，自学习闭环
mas/skills.py / context.py / prompts.py / mcp.py / llm.py
mas/verify/runner.py    execute_frozen_suite
frontend/*              server.py 1636 + app.js 1460，API 契约不变
mas/cli.py / worker.py / mq.py / store/*
```

### 4.3 契约必须保持（破坏了就白改）

1. `Leader.run()` 签名、返回值 `RunSummary` 不变。
2. `events.jsonl` 的事件序列与字段不变（前端时间线依赖它）。
3. `RunCanceledError` 仍是 `BaseException` 子类，且穿透到取消收尾。
4. 取消时跳过经验/指标写入（防污染自学习）。
5. `checkpoint.json` 继续存在（降级为审计快照，不删）。
6. `flow.yaml` 语义不变，`strict` / `lean` profile 仍可切换。

---

## 5. 目标架构与代码骨架

```
mas/orchestration/
├── state.py      RunState(TypedDict) + reducers        ~70 行
├── graph.py      build_graph(flow, deps) -> StateGraph ~110 行
├── nodes.py      make_task_node() 包装执行 + 决策        ~130 行
├── decision.py   decide(): 回退预算/escalation/审计      ~110 行
├── checkpoint.py LangGraph checkpointer ↔ checkpoint.json ~60 行
└── runner.py     run_graph(): 门面，对齐 RunSummary      ~80 行
```

### 5.1 `state.py`

```python
import operator
from typing import Annotated, Any, Dict, TypedDict

def merge_dict(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged

class RunState(TypedDict, total=False):
    run_id: str
    requirement_title: str
    steps: int
    iteration: int
    last_status: str            # "pass" | "fail"
    last_summary: str
    next_node: str              # router 只读这个
    pending_reason: str         # 下次回退的归因，等价于现有 pending_reason
    forced_fail: bool
    canceled: bool
    iterations: Annotated[Dict[str, int], merge_dict]
    rollback_counts: Annotated[Dict[str, int], merge_dict]
    run_state: Dict[str, Any]   # ctx.run_state 的可序列化投影
    decisions: Annotated[list, operator.add]   # 决策审计，落 events
```

**约束**：这里只能放标量、字典、列表。`Blackboard`、`Path`、`Sandbox` 一律不进 state，走 `deps`（闭包注入）。

### 5.2 `graph.py`

```python
from langgraph.graph import StateGraph, START, END

TERMINAL = {"end", "halt", ""}

def build_graph(cfg, deps):
    builder = StateGraph(RunState)
    flow = cfg.flow

    for node_id, node in flow.nodes.items():
        builder.add_node(node_id, make_task_node(node_id, node, deps))

    builder.add_edge(START, flow.entry)

    for node_id, node in flow.nodes.items():
        # 终止语义固定映射：即使某节点 transitions 里没写 halt，decide() 也可能兜底到 halt
        path_map = {"end": END, "halt": END}
        # 只遍历 pass/fail：NodeConfig.target() 内部已兜底 "default"
        for branch in ("pass", "fail"):
            target = node.target(branch)
            if target and target not in TERMINAL:
                path_map[target] = target
        # router 返回目标节点名（与 path_map 的 key 对齐），compile 期会校验未知节点
        builder.add_conditional_edges(node_id, route_from_state, path_map)

    return builder

def route_from_state(state: RunState):
    return state.get("next_node") or "halt"     # 纯函数，无副作用
```

**为什么 router 是纯函数**：LangGraph 在 superstep 边界重算 routing，router 里发事件会在恢复时重复。所有副作用（`rollback` 事件、经验降权、escalation）都放在节点函数内部的 `decide()` 里，router 只读一个字段。

### 5.3 `nodes.py`

```python
def make_task_node(node_id, node, deps):
    def _node(state: RunState):
        deps.restore_ctx(state)                       # ctx.run_state ← state["run_state"]
        deps.emit_node_start(node_id, state)

        if node.kind == "gate":
            outcome = deps.run_gate(node_id, node, state)
            status, summary = outcome["status"], outcome.get("summary", "")
        else:
            result, status = deps.act_with_retry(node_id, node, state)
            summary = result.summary
            deps.track_experience(result)
            deps.broadcast(node_id, result)

        deps.emit_node_end(node_id, state, status, summary)
        deps.extract_inline()                          # L1 即时经验
        decision = deps.decide(node_id, node, status, state)   # 见 5.4

        return {
            "node_id": node_id,
            "iteration": decision.iteration,
            "steps": state.get("steps", 0) + 1,
            "last_status": status,
            "last_summary": summary[:300],
            "next_node": decision.target,
            "pending_reason": decision.pending_reason,
            "forced_fail": decision.forced_fail,
            "iterations": {node_id: decision.iteration},
            "rollback_counts": decision.rollback_update,
            "run_state": deps.snapshot_run_state(),
            "decisions": [decision.to_dict()],
        }
    return _node
```

### 5.4 `decision.py`（把现有分支逻辑原样搬过来，不改语义）

```python
@dataclass
class Decision:
    target: str
    iteration: int
    forced_fail: bool
    pending_reason: str
    rollback_update: Dict[str, int]

def decide(node_id, node, status, state, deps) -> Decision:
    branch = deps.collaboration.branch_of(status)          # 等价 config.py:394
    target = node.target(branch) or "halt"

    if branch == "fail" and (why := deps.unfixable_reason(node_id, state)):
        deps.emit("escalation", node_id, why)              # 原 leader.py:744-752
        deps.publish_forced_gate("环境/证据通道缺口", why)
        return Decision("report" if deps.has_node("report") else "halt",
                        deps.next_iteration(node_id), True, "", {})

    if branch == "fail" and deps.is_rollback(node_id, target, state):
        if not deps.rollback_permitted(node_id, node):
            return Decision("halt", deps.next_iteration(node_id), False,
                            f"角色 {node.role} 无回退权限（can_rollback=false），流程终止", {})
        policy = deps.rollback_policy(node_id, node)
        attempt = state.get("rollback_counts", {}).get(node_id, 0) + 1
        deps.emit("rollback", node_id, target, policy.reason, attempt, policy.max_attempts)
        deps.broadcast("rollback", ...)
        deps.reinforce_negative(node_id)                   # 经验降权，原 leader.py:782
        reason = f"{policy.reason}（第 {attempt} 次回退）"
        if attempt > policy.max_attempts or deps.total_rollbacks(state) > deps.max_total_rollbacks:
            deps.emit("escalation", node_id, f"回退次数耗尽（{node_id} 已回退 {attempt - 1} 次）")
            deps.publish_forced_gate("回退次数耗尽", reason)
            return Decision("report", ..., True, reason, {node_id: attempt})
        return Decision(target, ..., False, reason, {node_id: attempt})

    return Decision(target, deps.next_iteration(node_id), False, "", {})
```

**这段是照搬 `leader.py:737-800`，逐条对照改写成函数，不新增判断。**

### 5.5 `runner.py`

```python
def run_graph(cfg, deps, init_state, *, run_id, resume=False):
    cp = build_checkpointer(cfg, deps.run_dir)      # SqliteSaver(run_dir/"graph.sqlite")
    graph = build_graph(cfg, deps).compile(checkpointer=cp)
    config = {
        "configurable": {"thread_id": run_id},
        "recursion_limit": min(int(cfg.flow.max_steps), int(cfg.runtime.get("max_steps", cfg.flow.max_steps))) * 2 + 10,
    }

    if resume and cp_has_state(cp, run_id):
        input_state = None                                  # 从 checkpoint 续跑，不重放输入
    else:
        input_state = init_state

    try:
        for _ in graph.stream(input_state, config, stream_mode="values"):
            pass                                            # 事件由节点内 emit，这里不消费
    except RunCanceledError:
        deps.mark_canceled()                                # 对齐 leader.py:816-830
    except GraphRecursionError:
        deps.force_fail("达到最大步数，强制终止")             # 对齐 leader.py:838-843
    return deps.finalize()                                  # 现有收尾逻辑原样复用
```

> `recursion_limit` 必须显式设。LangGraph 默认 25，比你的 `max_steps: 40` 小，不设会在长流程上提前抛 `GraphRecursionError`。

---

## 6. 三个真实难点与解法

### 6.1 Router 重算导致事件重复

**问题**：LangGraph 在 superstep 边界重算条件边。若 router 里 `events.emit("rollback")`，恢复时该事件会重复写进 `events.jsonl`，前端时间线出现两次回退。

**解法**：router 纯函数（只 `return state["next_node"]`），所有副作用放进节点函数。代价是「图的可读性」部分转移到 `decide()` 里——但 `decide()` 是纯计算 + 审计，可单测，比藏在 `while` 循环里更好。

**验证**：`tests/test_events.py` 加一条断言——同一 run 的 `rollback` 事件计数等于 `rollback_counts` 之和。

### 6.2 取消契约不能被框架吞掉

**问题**：`RunCanceledError` 继承 `BaseException`（`docs/41` 的硬约束，因为各层 `except Exception` 会吞它）。LangGraph 内部只 `catch Exception`，理论上不吞；但它的 `invoke` 外层若包了 `except Exception`，仍会漏。

**解法**：
1. `runner.run_graph` 显式 `except RunCanceledError`，转 `deps.mark_canceled()`。
2. **不要**给图节点配 LangGraph 的 `RetryPolicy`——重试策略会重新执行节点，与「取消立即生效」冲突。
3. `tests/test_cancel.py` 21 个用例必须全绿，其中「节点内流式中断」用例是硬门槛。

### 6.3 Checkpoint 双源冲突

**问题**：现有 `checkpoint.json`（`leader.py:354`）与 LangGraph `SqliteSaver` 会同时存在，`--resume` 时读哪个？

**解法**（分阶段，不一次到位）：

| 阶段 | 权威源 | `checkpoint.json` | `--resume` 行为 |
|---|---|---|---|
| P0–P1（尚未接 checkpointer） | `checkpoint.json` | 主 | 不变，读 json |
| P2 | `graph.sqlite` | 降级为**写审计快照** | 优先读 sqlite，无 state 时回落 json |
| P3 | `graph.sqlite` | 保留只读（人工排查用） | 只读 sqlite；json 缺失不影响 |

P2 阶段必须加一致性校验：两个源的 `next_node` 不一致时**拒绝续跑并报错**，不做猜测（沿用现有 `_resume_input` 的「绝不猜测」原则）。

### 6.4 interrupt 的隐藏坑（实施时必看）

LangGraph 官方的行为是：**`interrupt()` 恢复时，节点会从函数顶部重新执行**。所以：

- `interrupt()` 必须放在节点**最开头**；
- 节点前半段不能有不可重复的副作用（写盘、发事件、调模型）。

对 `acceptance` 节点这是安全的：它是 `kind: gate`，只读黑板做判定，没有副作用。**但 `review` 节点不能插 interrupt**（它会写 `review_report`），除非把「判定」和「落盘」拆成两个节点。

---

## 7. 分阶段实施

### P0 · 骨架与开关（0.5–1 天）

**做**：`state.py`、`graph.py`、`nodes.py` 空壳；`runtime.orchestrator` 开关；`legacy` 路径逐字节不变。
**验收**：
```bash
python -m mas run --requirement examples/requirements/order_pricing_service.md   # 走 legacy，行为不变
pytest tests/ -q                                                                  # 811 通过
```
**退出标准**：开关为 `legacy` 时，`git diff` 对事件输出零影响。

### P1 · 单节点闭环（2–3 天）

**做**：`decide.py` 搬分支逻辑；只接通 `intake → solution → halt` 三个节点；`stream` 跑通。
**验收**：同一需求双跑，`next_node` 序列与 legacy 完全一致（比对 `events.jsonl` 的 node 序列）。

### P2 · 全节点 + 回退环 + 门禁（3–5 天）

**做**：全部 8 个节点（intake / solution / implement / test / review / optimize / acceptance / report）接通；回退预算、escalation、forced_fail；checkpointer 接入（双写 + 一致性校验）。
**验收**：
- 3 个真实需求 A/B 对照（见 §8）
- `pytest tests/test_flow*.py tests/test_collaboration*.py tests/test_resume*.py`
- 构造 `review fail` 场景，验证 `rollback` 事件与 `rollback_counts` 一致

### P3 · 补短板（2–3 天）

**做**：`acceptance` 节点加 `interrupt`（人工放行，配置文件开关）；`stream_mode="updates"` 旁路接本地 OTel（不接 LangSmith 云，数据不出校）。
**验收**：人工拒绝后 `verdict=fail` 且不写经验库；接入后 trace 里能看到节点耗时。

### P4 · 收尾（可选）

**做**：删 legacy 循环（约 180 行）；`checkpoint.json` 转只读。
**前提**：P2 的 A/B 连续 5 次一致，且线上跑满两周。

**总工期估算：8–12 个工作日**（单人，不含 P4）。

---

## 8. 验证方案（这是方案能否被接受的关键）

### 8.1 单元回归

```bash
pytest tests/ -q          # 811 用例，一个断言都不许改
```

### 8.2 端到端 A/B 对照

```bash
# 两组必须用独立 workspace，否则经验库跨运行沉淀会伪造差异
python -m mas run --requirement examples/requirements/order_pricing_service.md \
  --workspace workspace/ab_legacy    # runtime.orchestrator=legacy
python -m mas run --requirement examples/requirements/order_pricing_service.md \
  --workspace workspace/ab_graph     # runtime.orchestrator=langgraph
```

对照项（全部要一致，除耗时）：

| 对照项 | 判定方式 |
|---|---|
| 节点访问序列 | `events.jsonl` 里 `node_start.node` 顺序 |
| 回退次数 / 目标 | `rollback` 事件与 `run_summary.rollbacks` |
| 门禁结论 | `acceptance_gate.passed` 与 `verdict` |
| 产物集合 | `artifacts/` 目录内 artifact 名与版本号 |
| 代码产物 | `code/` 下的文件树 hash |
| 用例执行结果 | `test_report` 通过/失败清单 |
| 事件类型序列 | 去掉时间戳后逐条比较 |

**已知会不一致的两项（可接受）**：`duration_s`（LangGraph 有额外序列化开销）、`steps` 的计数口径（若 `recursion_limit` 触发点不同）。

### 8.3 取消验证

`tests/test_cancel.py`（21 用例）全绿 + 手工跑一次「运行中终止」，确认 `run_summary.canceled=true`、`verdict=fail`、经验库无新增。

---

## 9. 风险台账

| 风险 | 概率 | 影响 | 处置 |
|---|---|---|---|
| LangGraph 1.x API 变动 | 中 | 编译失败 | 锁 `langgraph>=1.2,<2.0`；只用 StateGraph / 条件边 / checkpointer / interrupt 四个 API |
| state 塞入不可序列化对象 | 高（实施时易犯） | 续跑报错 | code review 红线 + 单测：`json.dumps(state, default=str)` 必须成功 |
| `GraphRecursionError` 与 `max_steps` 口径不符 | 中 | 长流程提前终止 | 显式设 `recursion_limit = max_steps * 2 + 10` |
| 事件顺序变化破坏前端 | 中 | 时间线错乱 | 前端不改，后端保证 `node_start`/`node_end` 顺序与 legacy 一致（§8.2 验证） |
| 双 checkpoint 不一致 | 中 | 续跑错乱 | P2 加一致性校验，不一致即报错不猜 |
| 依赖体积（langgraph + sqlite checkpointer） | 低 | 安装变慢 | 放 `optional-dependencies[graph]`，默认不装 |
| Windows 并发下 sqlite 锁 | 低 | 写入失败 | 用现有 `filelock` 封装，`open()` 放重试循环内 |

---

## 10. 明确不做

1. **不迁 langchain 模型层**。`LLMClient`（`mas/llm.py`）自己处理流式、取消探测、300s 网关约束，换成 `ChatOpenAI` 会丢掉这些，直接踩回 `docs/41` 之前的坑。
2. **不用 `create_agent` 替代 `base.act()`**。六步骨架 + ReAct + 工具多轮 + 技能注入是核心资产，`create_agent` 覆盖不了。
3. **不接 LangSmith 云**。数据出校，不满足本项目「证据不出本机」的边界；用本地 OTel 或继续用 `llm_traces.jsonl`。
4. **不改 `flow.yaml` 语义**。`profile`（strict/lean）仍靠配置切换，不改成子图（P4 之后可选）。
5. **不改前端 API**。`/api/run`、`/api/run?id=` 的返回结构一个字段都不动。
6. **不删 `checkpoint.json`**。降级为只读审计，给人工排查留证据。

---

## 11. 收益与代价（诚实评估）

**能拿到**

| 收益 | 说明 |
|---|---|
| 少 180 行手写循环 | `leader.py:682-810` 换成 `graph.stream` |
| 类型化状态 | `RunState` 有 schema，节点返回值有编译期检查 |
| 恢复粒度 | 现有 `checkpoint.json` 是「节点完成后写」；LangGraph 是 superstep 自动写，粒度相同但不用手写 |
| **人工审批**（真正补短板） | `interrupt()` 补上 `docs/27 §4` 认定「完全没有」的审批机制 |
| 可视化调试 | LangGraph Studio 能看图、看每步 state、时间旅行，替代手翻 `events.jsonl` |
| 并行 fan-out（未来） | `Send` 可直接做多需求并行，现在完全没能力 |

**要付**

| 代价 | 量级 |
|---|---|
| 新依赖 | langgraph + langgraph-checkpoint-sqlite，约 20MB，pydantic 已是传递依赖 |
| 配置驱动的可读性下降 | 图结构仍在 YAML，但「为什么走这条边」的部分逻辑从 `while` 体搬进 `decide()` |
| 迁移期双轨维护 | P0–P2 期间两条路径并存，改 `leader.py` 要同时照看 |
| 学习成本 | 团队/未来的自己要懂 Pregel superstep 语义，否则调试困难 |

**收益不覆盖的部分（如实）**

- 框架不提供验收：`expr.py`、`verifier.attest`、`hidden.py`、冻结用例仍全自研。
- 框架不提供执行：`sandbox.py` 1472 行进程隔离、超时、静态检查，一点都不省。
- 框架不提供自学习：`experience.py` 1138 行与本方案无关。

**一句话**：这次重构的定位是「**把手写的调度循环换成标准件，并顺带拿到人工审批和可视化**」，不是「用框架重建这个系统」。

---

## 12. 附录：文件变更清单

```
新增  mas/orchestration/__init__.py
新增  mas/orchestration/state.py           ~70 行
新增  mas/orchestration/graph.py          ~110 行
新增  mas/orchestration/nodes.py          ~130 行
新增  mas/orchestration/decision.py       ~110 行
新增  mas/orchestration/checkpoint.py      ~60 行
新增  mas/orchestration/runner.py          ~80 行
新增  tests/test_orchestration_graph.py   ~150 行（图结构与 router 纯函数性）
新增  tests/test_orchestration_ab.py      ~120 行（A/B 对照，标记 slow）

修改  mas/leader.py        run() 加分流；抽 _execute_node/_decide；删 while 循环
修改  mas/config.py        校验 runtime.orchestrator
修改  config/system.yaml   runtime.orchestrator: "legacy"
修改  requirements.txt     可选 extra：graph

不动  其余全部（见 §4.2）
```
