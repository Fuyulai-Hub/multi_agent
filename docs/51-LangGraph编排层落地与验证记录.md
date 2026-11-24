# 51 · LangGraph 编排层落地与验证记录

> 对象：`G:/mycv/cragent/multi_agent`
> 依据：`docs/50-LangGraph重构技术方案.md`
> 结论：**按方案落地了编排层替换（legacy / langgraph 双轨 + 开关），默认仍是 legacy；两条路径在离线端到端 A/B 中产出逐项相同的运行结果。**
>
> **2026-11-25 更新**：§3 第 6 条（P4 删 legacy 循环）已执行，双轨收敛为 LangGraph 单路径，`runtime.orchestrator` 开关已删除，见 `docs/53-编排层单路径切换与legacy清理.md`。

---

## 0. 三句话摘要

1. 新增 `mas/orchestration/`（7 个文件 475 行），把 `flow.yaml` 的「节点 + pass/fail 边 + 回退环」映射成 `StateGraph`，并接上 `SqliteSaver` 检查点。
2. `leader.py` 的 180 行手写循环被拆成两半：**节点执行**（`_execute_node`，两条路径共用）与**分支决策**（`orchestration/decision.py`，两条路径共用），循环只剩调度 30 行。
3. 开关 `runtime.orchestrator: legacy | langgraph`（默认 `legacy`）。验证：全量回归 769 通过 / 16 跳过，离线 A/B（CLI 与进程内两组）事件脚本、产物版本、代码树 hash 完全一致。

---

## 1. 变更清单（实测量级）

| 文件 | 动作 | 行数 |
|---|---|---|
| `mas/orchestration/state.py` | 新增 | 32（`RunState` 7 个通道 + `merge_dict` reducer） |
| `mas/orchestration/decision.py` | 新增 | 110（回退预算/权限/escalation，原 `leader.py:737-800` 逐条搬入） |
| `mas/orchestration/nodes.py` | 新增 | 93（`GraphDeps` + 节点函数：执行 → 决策 → 写检查点） |
| `mas/orchestration/graph.py` | 新增 | 63（START/条件边/path_map，router 纯函数） |
| `mas/orchestration/checkpoint.py` | 新增 | 52（`graph.sqlite` 连接、快照读取、双源一致性校验） |
| `mas/orchestration/runner.py` | 新增 | 116（建图 → 跑图 → 取消/递归上限收口 → 交回 leader） |
| `mas/orchestration/__init__.py` | 新增 | 9（空壳，不导入子模块） |
| `mas/leader.py` | 改 | +157 / −133：抽出 `_execute_node` / `_cancel_requested`，循环改为「分流 + 30 行 legacy 循环」 |
| `mas/config.py` | 改 | +5（`runtime.orchestrator` 枚举校验） |
| `config/system.yaml` | 改 | +6（`orchestrator: "legacy"` 与注释） |
| `requirements.txt` | 改 | +6（可选依赖说明，注释形式） |
| `tests/test_orchestration_graph.py` | 新增 | 217（图结构 / router 纯函数性 / 取消 / 续跑 / 跨编排续跑） |
| `tests/test_orchestration_ab.py` | 新增 | 113（legacy vs langgraph 端到端对照） |

`agents/`、`sandbox.py`、`verifier.py`、`expr.py`、`blackboard.py`、`events.py`、`experience.py`、`skills.py`、`context.py`、`llm.py`、`fill/worker/mq/store`、`frontend/` —— **一行未动**（与 docs/50 §4.2 一致）。

---

## 2. 关键实现决策

### 2.1 真正被替换的只有 30 行

原循环体里有三件互不相关的事，混在一起才显得「必须保留 180 行」：

| 职责 | 落点 | 两条路径 |
|---|---|---|
| 执行一个节点（事件、gate/task 分流、广播、L1 经验提炼） | `Leader._execute_node` | 共用 |
| 决定下一步（pass/fail、回退预算、权限、escalation、强制门禁） | `orchestration.decision.decide` | 共用 |
| 驱动下一轮 | legacy `while` / LangGraph 条件边 | **唯一分歧点** |

因此 A/B 对照的不是「两套语义」，而是「同一套函数被两种方式驱动」——这也解释了为什么事件脚本能逐条相同。

### 2.2 router 是纯函数（docs/50 §6.1 的落地）

`route_from_state` 只读 `state["next_node"]` 与 `state["steps"]`，不 emit、不改 state。所有副作用（`rollback` 事件、经验降权、`escalation`、强制门禁发布）都在节点函数内部完成。回归用例 `test_router_reads_state_only` 直接断言「调用后 state 未被修改」。

### 2.3 取消契约（docs/41 不变）

- 节点内：`RunCanceledError`（`BaseException`）从 `LLMClient` 一路穿透 `act()` → 节点函数 → `graph.stream` → `runner.run_graph` 的 `except RunCanceledError`，**LangGraph 没有吞它**（`test_cancel_inside_node_propagates_through_graph` 钉死：第 2 次 LLM 调用被打断后不再有第 3 次，事件流出现 `node_interrupted`、无 `node_error`）。
- 节点边界：`_cancel_requested` 在节点函数入口判定，命中即返回 `{next_node: halt, canceled: true}`，不执行节点、不虚增步数（`test_cancel_at_node_boundary`：3 步退出、无 `node_interrupted`、不写知识库指标）。
- 收尾路径完全复用 leader 既有逻辑：`canceled=true` / `verdict=fail` / 跳过经验与指标 / `run_summary` 与时间线照常落盘。

### 2.4 检查点双源（docs/50 §6.3）

| 场景 | 权威源 | 行为 |
|---|---|---|
| langgraph 运行，`--resume` | `run_dir/graph.sqlite` | 优先；与 `checkpoint.json` 的 `next_node` 不一致即 `ConfigError`（不猜测） |
| legacy 跑挂（只有 json）后改用 langgraph 续跑 | `checkpoint.json` | 图入口按检查点的 `next_node` 落位（START 用条件边而非固定边） |
| 正常结束 | — | `checkpoint.json` 仍按节点写，收尾置 `status=completed` |

`graph.sqlite` 只在 langgraph 模式产生（CLI A/B 已实测：legacy 运行目录里没有该文件）。

---

## 3. 与 docs/50 的偏差（如实列出）

| 方案里的写法 | 实际实现 | 理由 |
|---|---|---|
| §5.1 的 `decisions: Annotated[list, operator.add]` 审计通道 | **未加** | 决策审计已由 `rollback`/`escalation`/`node_end` 事件承载（`events.jsonl` 是权威源），再进 state 只是给 checkpointer 增加体积，没有消费方 |
| §5.3 的 `deps.emit_node_start/run_gate/act_with_retry/...` 十余个回调 | `GraphDeps` 持 `leader` + `ctx` + `agents`，节点直接调 `Leader._execute_node` | 十余个转发方法是空壳门面，只会让「一处改动要改两处」；dataclass 字段全部是运行期必需参数（检查点写入需要 title/hash/started_at） |
| §5.5 `add_edge(START, entry)` | `add_conditional_edges(START, entry_route, all_ids)` | 为了支持「legacy 只写了 json 检查点的运行用 langgraph 续跑」；新运行没有 `next_node` 时仍返回 `flow.entry`，行为等价 |
| §5.5 只写 `recursion_limit` | 另在 router 里 `steps >= max_steps → halt` | legacy 的退出点是 `while ... and steps < max_steps`，只靠 `recursion_limit = max_steps*2+10` 会让图多跑节点、事件序列与 legacy 不一致 |
| §7 P3「acceptance 加 `interrupt()` 人工放行」 | **未做** | 需要前端/人工审批通道接入，且 `review` 节点有落盘副作用、`interrupt` 恢复会从函数顶部重跑（docs/50 §6.4 自己点名）。无审批入口时加 `interrupt` 只会让运行卡在图上 |
| §7 P4「删 legacy 循环」 | **未做** | 方案自己的前提是「P2 A/B 连续 5 次一致 + 线上两周」。当前只有单次 A/B 与回归证据，删掉等于自断回退路径 |

另：`docs/50` 写的「811 用例」是当时的估计；本次改动前的实测基线是 **758 通过 / 16 跳过**。

---

## 4. 验证记录

### 4.1 全量回归（legacy 默认路径不受影响）

```bash
python -m pytest tests/ -q
# 769 passed, 16 skipped in ~4min
```

改动前基线同为 **758 passed, 16 skipped**；新增 11 个用例（图结构 5 + 运行/取消/续跑 5 + A/B 1），断言零改动。

### 4.2 离线端到端 A/B（进程内，`tests/test_orchestration_ab.py`）

同一需求 `examples/requirements/order_pricing_service.md`，两个**独立 workspace**（避免经验库跨运行沉淀伪造差异）：

| 对照项 | legacy | langgraph |
|---|---|---|
| verdict | pass | pass |
| steps | 12 | 12 |
| rollbacks | 2 | 2 |
| bugs_total / fixed | 5 / 5 | 5 / 5 |
| iterations | 全节点逐项相等 | 同左 |
| 事件脚本（类型+节点+轮次，逐条） | — | **完全一致** |
| 回退事件（from/to/attempt/max_attempts） | 2 条 | **完全一致** |
| 产物版本集合 | 19 个产物 | **完全一致** |
| `code/` 文件树 hash | — | **完全一致** |
| `test_report.summary` | 74/74 | 74/74（仅 `duration_ms` 15 vs 16，方案 §8.2 允许） |

### 4.3 CLI 层 A/B（`.workbuddy/verify_orchestrator_ab.py`）

走真实入口 `python -m mas run`（真实 yaml + profile 覆盖），配置文件里只差 `runtime.orchestrator`：

```
legacy   : verdict=pass steps=12 rollbacks=2 bugs=5/5   graph.sqlite: False
langgraph: verdict=pass steps=12 rollbacks=2 bugs=5/5   graph.sqlite: True
节点序列 : intake→solution→implement→test→optimize→test→review→implement→test→review→acceptance→report（两组相同）
event script identical      : True
code tree hash identical    : True
artifact versions identical : True
```

### 4.4 取消与续跑

| 用例 | 结论 |
|---|---|
| 节点边界取消（第 4 个节点前） | `canceled=true`、`steps=3`、无 `node_interrupted`、无 `knowledge/metrics.jsonl` |
| 节点内 LLM 中断（第 2 次生成） | `node_interrupted` + `run_canceled`、无 `node_error`、LLM 调用停在 2 次 |
| 崩溃于 implement 后续跑（langgraph） | 从 `graph.sqlite` 恢复，`intake`/`solution` 不重跑，`checkpoint.json` 收尾为 `completed` |
| legacy 跑挂后用 langgraph 续跑 | 图入口落在 `implement`，`run_resumed.resumed_from=implement`，整条流程跑通 |

---

## 5. 已知边界（不是 bug，是取舍）

1. **依赖**：`langgraph>=1.2,<2.0` + `langgraph-checkpoint-sqlite` 是可选依赖，未安装时 legacy 路径完全不受影响（`runner` 惰性导入）；切开关而未安装会直接 `ModuleNotFoundError`，不静默降级。
2. **每次 langgraph 运行多一个 `graph.sqlite`**：与 `checkpoint.json` 并存，前者是续跑权威源，后者是人可读审计快照。
3. **可视化**：`StateGraph` 已可用 LangGraph Studio 打开（`build_graph` 返回未编译的 builder），但本机未接 Studio，未做该验证。
4. **未接 OTel / LangSmith**：按方案 §10.3 不接云端，事件流仍是 `events.jsonl`。

---

## 6. 证据文件

| 内容 | 路径 |
|---|---|
| CLI 层 A/B 脚本（真实 `python -m mas run`） | `.workbuddy/verify_orchestrator_ab.py` |
| 图结构 / router / 取消 / 续跑 | `tests/test_orchestration_graph.py` |
| 端到端 A/B 对照 | `tests/test_orchestration_ab.py` |
| 方案原文 | `docs/50-LangGraph重构技术方案.md` |
