# 配置驱动的多 Agent 协作系统 —— 全链路深度分析

> 分析对象：`G:/mycv/cragent/multi_agent`（Fuyulai-Hub / multi_agent 仓库）
> 分析视角：从**输入到输出的完整链路**、各 Agent 的**角色定位与协作机制**、**消息传递与数据流转**、**真实案例逐步演示**、**异常处理与恢复**、**性能特征与瓶颈**。
> 结论依据：以源码（`mas/`、`config/`）为事实来源，并以 `workspace/runs/` 中**真实运行日志**（`events.jsonl` / `timeline.md` / `run_summary.json`）作为可复现证据。所有断言均给出文件/行号或运行实例。

---

## 0. 一句话定位

这是一个 **Leader 统一调度的流水线式多 Agent 系统**：需求文本进入后，由 5 类角色 Agent（产品 / 研发 / 评审 / 测试 / 报告）沿配置化的有向流程依次作业，Agent 之间**不直接互相调用**，而是通过一个**版本化共享黑板（Blackboard）**共享交付物、通过一个**持久化消息总线（Bus）**广播状态。Leader 负责驱动状态机、做门禁判定、执行回退/重试/升级，并在每步之后把"当轮经验"提炼进一个**三级经验库（L1/L2/L3）**，下一轮/下一次运行再把经验分层注入 Prompt，形成自学习闭环。

核心约束（务必先理解）：**单进程、单线程、严格串行**。整条流水线在同一进程内按 `flow.yaml` 的顺序节点逐个执行，没有任何 Agent 并发执行。

---

## 1. 全局架构与组件拓扑

```
                         ┌──────────────────────── 一次运行 (Run) ────────────────────────┐
                         │                                                              │
 需求文本/文件           │   ┌─────────┐                                                │
  ──────────────────────▶│   │ Leader  │  状态机驱动 / 门禁判定 / 回退·重试·升级 / 经验闭环  │
                         │   │ (调度器) │◀──────────────┐                               │
                         │   └────┬────┘                │                               │
                         │        │ 按 flow.yaml 顺序   │ 回退边 (review/test/accept)   │
                         │        ▼                     │                               │
                         │   ┌──────────────────────────────────────────────────────┐  │
                         │   │ 角色 Agent（BaseAgent 执行骨架）                      │  │
                         │   │  product → developer → reviewer → tester → reporter    │  │
                         │   └───┬───────────┬────────────┬─────────────┬───────────┘  │
                         │       │           │            │             │              │
                         │       ▼           ▼            ▼             ▼              │
                         │  ┌────────────────────────── 共享基础设施 ─────────────────────┐
                         │  │  Blackboard(版本化产物)   Bus(消息)   EventLog(事件日志)    │
                         │  │  Experience(经验库)       LLMClient     Sandbox(沙箱执行)   │
                         │  └────────────────────────────────────────────────────────────┘
                         │                              │                               │
                         │              ┌───────────────┴───────────────┐               │
                         │              ▼                                ▼               │
                         │      OfflineProvider(确定性)        OpenAI兼容端点(真实大模型)   │
                         └──────────────────────────────────────────────────────────────┘
```

**关键事实（源码 `mas/leader.py:67-115`）：**
- 一次运行在 `Leader.run()` 内完成：实例化 `EventLog` / `MessageBus` / `Blackboard` / `Sandbox` / `ExperienceLibrary` / `PromptBuilder` / `LLMClient`，构造 `AgentContext`，再按 `self.config.flow.entry`（默认 `intake`）驱动主循环（L156-249）。
- `agents = {role_id: create_agent(role_id, ctx) for role_id in self.config.roles}`（L108）：所有角色 Agent 在运行开始时**一次性全部实例化**，由 Leader 按节点 `role` 字段取出对应 Agent 调用 `agent.act(task)`。
- 唯一的"全局可变状态"是 `AgentContext.run_state`（L101-105），用于累积回退次数、已注入经验、协商次数等跨节点统计。

---

## 2. 完整链路：从输入到输出的状态机

### 2.1 流程节点（来自 `config/flow.yaml`）

| 节点 id | 角色 | kind | 输入（感知产物） | 输出产物 | 通过分支 | 失败分支 |
|---|---|---|---|---|---|---|
| `intake` | product | task | `requirement_source` | `requirement_spec` | solution | halt |
| `solution` | developer | task | `requirement_spec` | `solution_design` | implement | halt |
| `implement` | developer | task(可重入) | spec / design / review_report / bug_reports / acceptance_gate | `code_manifest` / `implementation_summary` | review | review ⚠️ |
| `review` | reviewer | task(可重入) | spec / design / code_manifest / implementation_summary | `review_report` | test | **implement(回退)** |
| `test` | tester | task(可重入) | spec / design / code_manifest / review_report | `test_report` / `bug_reports` | acceptance | **optimize(回退)** |
| `optimize` | developer | task(可重入) | spec / code_manifest / test_report / bug_reports / review_report / acceptance_gate | `fix_summary` / `fix_test_report` / `code_manifest` | test | test |
| `acceptance` | reviewer | **gate** | spec / design / code / review / test / bug_reports | `acceptance_opinion` / `acceptance_gate` | report | **optimize(回退)** |
| `report` | reporter | task | 上述全部 | `acceptance_report` / `evidence_index` | end | end |

> ⚠️ **一个反直觉的设计点**：`implement` 的 `fail` 分支指向 `review`（见 `flow.yaml:41` `transitions: { pass: review, fail: review }`）。即"编码实现"无论成功失败都会进入"代码审查"。这是因为编码失败（如导入自检未过）仍以"被审查的对象"形式进入审查环节，由审查 Agent 给出阻塞结论再回退。其余"普通任务节点"（`intake`/`solution`）失败则**直接 halt 终止运行**。

### 2.2 门禁表达式（`config/flow.yaml:81-98`）

`acceptance` 是 `kind: gate` 节点，由 `Leader._run_gate()`（`mas/leader.py:381-442`）在**黑板最新快照 `snapshot()`** 上求值 8 条表达式（白名单 AST 安全求值，见 `mas/expr.py`）：

```python
test_report.summary.failed == 0
test_report.coverage.functional.rate >= 1.0
test_report.coverage.exception.rate >= 1.0
test_report.coverage.boundary.rate >= 1.0
review_report.blocking == 0
bug_reports.open == 0
solution_design.evaluation.selected != ''
test_report.negotiation.open == 0      # 未闭环的歧义会直接挡住验收
```

全部通过 **且** 签署意见非 `reject` → `passed=True` → 进入 `report`。任一不过 → 回退 `optimize`。

### 2.3 主循环骨架（`mas/leader.py:156-249`）

```
while node_id 且非 terminal 且 steps < max_steps(40):
    node = flow.node(node_id)
    iteration = ++iterations[node_id]
    发 node_start 事件
    if node.kind == "gate":   outcome = _run_gate(...)   # 表达式判定 + 签署
    else:                     result, status = _act_with_retry(...)  # 调 agent.act
    for topic in result.events:  _broadcast(...)         # 按协作矩阵广播
    发 node_end 事件
    _extract_inline(ctx)       # L1 即时经验提炼
    branch = collaboration.branch_of(status)  # pass/fail 归一化
    target = node.target(branch)
    if branch=="fail" 且 _is_rollback(...):   # 显式回退边 或 重入已执行节点
        检查 can_rollback 权限 → 拒绝则 halt
        累加 rollback_counts / run_state.rollbacks
        发 rollback 事件 + 广播
        if 超过 max_attempts 或 全局回退上限(8): 触发 escalation
    node_id = target
收尾：写 run_summary.json / timeline.md / 经验晋升 / 指标记录
```

---

## 3. 各 Agent 的角色定位与职责划分

所有角色共享同一执行骨架 `BaseAgent.act()`（`mas/agents/base.py:51-110`），差异只在 `perceive / retrieve_experience / build_prompt / build_context / generate / apply` 这几个钩子。下面按"动手前感知什么、产出什么、关键决策点"逐个说明。

### 3.1 产品 Agent（`product.py`）
- **角色定位**：需求翻译器。把非结构化的需求文档/文字 → 结构化 `requirement_spec`。
- **感知**：仅 `requirement_source`（Leader 在入口写入，L128-133）。
- **关键决策点**（`product.py:57-73`）：
  - 若解析出 **0 个功能点** → 直接 `fail`（需求不可解）。
  - 若 `require_samples_per_fp` 为真且某功能点**缺少样例** → `fail`（结构完备性检查）。
- **产出**：`requirement_spec`（功能点列表、验收标准、待澄清问题）。成功后广播 `requirement_ready`。

### 3.2 研发 Agent（`developer.py`）
- **角色定位**：技术决策者 + 编码者 + 修复者。同一个 Agent 承担 `solution` / `implement` / `optimize` 三个节点，靠 `task.node_id` 分派不同 `apply` 逻辑。
- **模式判定**（`developer.py:46-57`）：
  - `solution` → 方案设计；
  - `optimize` → **永远走 `fix`**（绝不再引入已修复缺陷）；
  - 否则：`bug_reports.open>0` → `fix`；`review.verdict==reject` → `rework`；否则首次 `first`。
- **关键决策点**：
  - 方案必须含 `evaluation.selected` 且覆盖全部 spec 功能点，否则 `fail`（`developer.py:96-107`）。
  - 实现必须产出文件；写完文件后做**导入自检**（`_self_check`，`developer.py:136-148`）：`python -c "import <package>"`。自检失败 → 节点 `fail`。
  - **修复阶段**（`_apply_fix`，L207-317）：对每个 `open` 缺陷跑**定向回归用例**（`--only <bug.test>`），全部回归通过且导入自检通过才 `pass`。产出 `code_manifest` / `fix_summary` / `fix_test_report`。

### 3.3 评审 Agent（`reviewer.py`）
- **角色定位**：静态审查 + 验收签署。承担 `review` / `solution_review`（strict 流程）/ `acceptance` 三个节点。
- **关键决策点**（`reviewer.py:39-62`）：
  - `review` 节点：调用 `sandbox.analyze(...)` 做**真实 AST 静态分析**（`reviewer.py:42`），把 `findings`/`blocking`/`verdict` 写入 `review_report`。`verdict=reject` 触发回退到 `implement`。
  - `acceptance` 节点（`_apply_signoff`，L104-112）：只产出 `acceptance_opinion`，**自身永远 `pass`**；最终门禁结论由 Leader 综合"表达式 + 签署意见"判定（L415）。
- **权限**：未声明 `can_block_acceptance` 的角色，其 `reject` 签署**无效**（`leader.py:402-413`）。

### 3.4 测试 Agent（`tester.py`）
- **角色定位**：独立的质量关卡。依据需求契约生成功能/异常/边界三类用例，**沙箱真实执行**，产出缺陷单。
- **关键决策点**（`tester.py:43-180`）：
  - 必须产出 `tests/*.py` 测试文件，否则 `fail`。
  - 执行后统计 `coverage`（按功能点维度算 rate）、`bug_reports`（用 `test_ref` 哈希做跨轮缺陷去重与闭环判定，`_track_bugs` L231-302）。
  - **协商**：若生成时带有歧义问题 → 通过 `ctx.consult` 向 developer 发起澄清（`_negotiate`，L183-214）；未闭环的歧义会使本节点 `fail`（L139 `negotiation["open"] == 0` 才能 pass），进而挡住验收门禁。
- **产出**：`test_report` / `bug_reports`。失败时广播 `test_failed` + `bug_found`。

### 3.5 报告 Agent（`reporter.py`）
- **角色定位**：证据汇总与验收报告。承担 `report` / `skill_distill`（off-flow 沉淀）两节点。
- **关键决策点**：把黑板上所有产物 + 事件统计 + 自学习指标汇总为 `acceptance_report.md` / `.json`，并生成 `evidence_index.jsonl`（逐产物版本、关键事件、测试执行记录的索引）。

### 3.6 Leader（调度器，非"角色 Agent"）
- 不生产业务产物，只做：状态机推进、门禁求值、回退/重试/升级、协商中介、经验提炼与晋升、运行级指标与报告。

---

## 4. 协作机制：Agent 间如何"感知彼此的工作"

三个 Agent **从不直接互相调用方法**（除显式的 `consult` 协商外）。协作完全通过三种共享设施完成。

### 4.1 共享黑板 Blackboard（版本化产物）`mas/blackboard.py`
- **存储**：每个产物名一个目录，保留**所有版本** `v1/v2/.../latest.json`（`blackboard.py:71-113`）。
- **跨进程安全**：`publish` 在文件锁内完成"版本号计算 + 落盘"（`_next_version` 扫磁盘最大版本+1，L115-124），避免并发写互相覆盖。
- **读取最终一致**：`get()` / `snapshot()` 在进程内缓存未命中时回读磁盘 `latest.json`，使其他进程新发布的产物对本进程可见（L127-172）。
- **下游感知上游**：Agent `perceive()` 调 `blackboard.require(inputs)`，缺失即暴露（`base.py` 的 `Perception.missing`）。
- **门禁求值直接吃快照**：`acceptance` 节点在 `ctx.blackboard.snapshot()` 上跑表达式。

### 4.2 消息总线 Bus（direct / broadcast / request-response）`mas/bus.py`
- **三种消息类型**（`Message.kind`）：
  - `direct`：点对点（`bus.send`）。
  - `broadcast`：群发（默认 `recipients=["all"]`，`bus.broadcast`）。
  - `request` / `response`：协商的请求-应答（带 `correlation_id` 关联）。
- **全落盘**：每条消息写 `messages.jsonl` 并同步发一条 `message` 事件（`bus.py:86-104`）——消息本身即审计证据。
- **`digest(role)`**：把与某角色相关的消息压缩成"已发生的工作进展"摘要，注入该角色 Prompt 的"协作消息摘要"段（`base.py` 的感知段），实现"感知他人已完成的工作"。
- **`inbox` / `unread`**：按 `recipients` 含该角色或 `all`、且非自己发送来过滤可读消息。

### 4.3 感知矩阵 + 广播矩阵（`config/collaboration.yaml`）
- **感知矩阵 `perception`**（L8-13）：规定每个角色动手前**必须读取**的上游产物列表（写进 Prompt 的"感知段"）。例如 tester 必须感知 `requirement_spec / solution_design / code_manifest / implementation_summary / review_report / bug_reports`。
- **广播矩阵 `broadcast`**（L16-28）：规定某个事件主题应通知哪些角色。例：`review_rejected → [developer, tester, reporter]`；`acceptance_passed → 全部四人`。
- **流转**：Agent 在 `result.events` 声明要广播的主题（如 `requirement_ready` / `design_ready` / `implementation_ready` / `review_rejected` / `test_failed` / `bug_found` / `bug_fixed` / `acceptance_passed/Rejected` / `rollback`），Leader 在节点结束后按矩阵 `broadcast_targets(topic)` 调 `bus.broadcast`（`leader.py:184-186, 524-528`）。

### 4.4 协商机制（请求-应答，Leader 中介）`mas/leader.py:445-473`
- 触发方固定为 **tester**（配置 `negotiation.requder_role: tester`），响应方为 **developer**（`collaboration.yaml:34-36`）。
- 流程：`tester` 在 `apply` 内调 `ctx.consult(requester="tester", target_role="developer", question=..., fp_id=...)` → `consult` 闭包通过 `bus.send(..., kind="request")` 发请求 → 调 `developer.consult(question, fp_id)` 拿到答案 → `bus.send(..., kind="response", correlation_id=...)` 回传 → 发 `negotiation_closed` 事件。
- `max_rounds: 2`，且 `require_closure: true`：未闭环的歧义会挡住验收门禁（`test_report.negotiation.open == 0`）。
- 作用：把"需求歧义"从"测试与研发各说各话"变成"显式问答 + 结论作为验收证据"。

---

## 5. 数据流转过程（以一次完整运行为例）

```
需求文件/文本
   │  load_requirement() 解析
   ▼
[leader] blackboard.publish("requirement_source")  ──▶ artifacts/requirement_source/v1.json
   │
   ▼  product.act
[product] 解析功能点 → blackboard.publish("requirement_spec")  ──▶ v1.json
   │           └─ broadcast requirement_ready → developer/reviewer/tester/reporter
   ▼  developer.act (solution)
[developer] 选型+方案 → publish("solution_design") ──▶ v1.json
   ▼  developer.act (implement)
[developer] 写文件到 sandbox/code/<pkg>/ → 导入自检 → publish("code_manifest","implementation_summary")
   │           └─ sandbox 真实执行 import；产物含 self_check 结果
   ▼  reviewer.act (review)
[reviewer] sandbox.analyze() 真实 AST 分析 → publish("review_report"){verdict,blocking,findings}
   │           └─ verdict=reject → 回退 implement
   ▼  tester.act (test)
[tester] 写 tests/*.py → sandbox 真实跑 unittest → 解析结果 → publish("test_report","bug_reports")
   │           └─ 失败用例 → bug_reported 事件 + bug_reports.open>0 → 回退 optimize
   ▼  developer.act (optimize, mode=fix)
[developer] 改文件 → 对每个 open bug 跑定向回归(--only) → publish("code_manifest","fix_summary","fix_test_report")
   ▼  tester.act (test 复测)
[tester] 重跑 → bug_reports.open==0 → 进入 acceptance
   ▼  reviewer.act (acceptance, gate)
[leader._run_gate] 在 snapshot() 上求值 8 条表达式 + 读取 acceptance_opinion → publish("acceptance_gate")
   │           └─ all pass → report
   ▼  reporter.act (report)
[reporter] 汇总 → publish("acceptance_report","evidence_index") → reports/acceptance_report.md
   ▼
[leader] 收尾：run_summary.json + timeline.md + experience.promote() + metrics
```

**数据三大载体**：
1. **Blackboard 产物**（`workspace/runs/<run_id>/artifacts/<name>/v*.json`）：业务交付物，版本化、可溯源。
2. **EventLog**（`events.jsonl`）：唯一事实来源，每一步都有 `node_start/node_end/gate_evaluated/rollback/bug_reported/...` 事件，含 `seq` 严格递增。
3. **Bus 消息**（`messages.jsonl`）：Agent 间通讯留痕。

**Prompt 留痕**：每次 LLM 调用都落盘 `prompts/<node>#<iter>-<role>-<stamp>.md`，含注入的经验 ID，使"经验闭环"可审计（`prompts.py:196-206`）。LLM 原始输出/思考链落盘 `llm_traces.jsonl`（`base.py:_trace_llm`）。

---

## 6. 真实案例逐步演示

下面用**真实运行 `workspace/runs/20261009-101824-15fa`**（verdict=**pass**, 11 步, 2 次回退, 5 个缺陷全部闭环）逐步演示。该运行使用 offline 确定性 Provider，需求为 `order_pricing_service`（订单金额计算与校验服务）。

| 步 | 节点 | 角色 | 真实发生（取自 `timeline.md`） | 关键决策 |
|---|---|---|---|---|
| 1 | intake | product | 解析出 **7 个功能点**、4 条验收标准、1 项待澄清 | 功能点非 0、样例齐全 → pass；广播 `requirement_ready` |
| 2 | solution | developer | 选型『标准库 Decimal + 正则（零第三方依赖）』，功能点覆盖 7/7 | `evaluation.selected` 非空且覆盖全 → pass |
| 3 | implement | developer | 写 2 文件 168 行（mode=first），**导入自检通过** | 文件存在 + import ok → 发布 `code_manifest`，广播 `implementation_ready` |
| 4 | review | reviewer | **verdict=reject，阻塞 10 项，发现 11 项** | 有阻塞问题 → `review_report.blocking>0`；Leader 触发**回退到 implement（第 1 次）** |
| 5 | implement | developer | mode=**rework**，写 2 文件 194 行（补文档字符串等） | 修复审查阻塞项后重新发布 `code_manifest` |
| 6 | review | reviewer | verdict=**pass**，阻塞 0 项（仅 1 项 minor） | 无阻塞 → 进入 test |
| 7 | test | tester | 生成 **74 用例（功能 31/异常 26/边界 17）**，通过 69 / 失败 5；歧义协商 1/1 闭环 | 失败 5 → `bug_reports.open=5` → **回退到 optimize（第 2 次）**；发 5 条 `bug_reported` |
| 8 | optimize | developer | mode=**fix**，定向回归 5/5 通过，发布 `fix_summary`/`fix_test_report` | 每个 open bug 跑 `--only` 回归全过 → pass |
| 9 | test | tester | 复测 74 用例 **74/0 失败**，`bug_reports.open=0` | 全过 → 进入 acceptance |
| 10 | acceptance | reviewer(gate) | 签署 pass；Leader 在 `snapshot()` 上 8 条表达式**全过** | `acceptance_gate.passed=True` → 进入 report |
| 11 | report | reporter | 生成 `acceptance_report.md` + `evidence_index`（50 条证据） | 运行结束 verdict=**pass**，耗时 2.61s（offline 确定性） |

**这条链路完整展示了系统设计的四个核心闭环**：
1. **审查回退闭环**（步 4→5→6）：审查拦截阻塞问题 → 研发 rework → 复审通过。
2. **测试→修复→复测闭环**（步 7→8→9）：测试暴露 5 缺陷 → 研发 fix + 定向回归 → 复测全过。
3. **协商闭环**（步 7）：测试发现的 1 个歧义通过 `consult` 向研发澄清并 1/1 闭环，且作为验收证据。
4. **自学习闭环**：审查 reject / 缺陷模式在步 4、7 被提炼为经验，步 11 结束时有 **4 条经验晋升**（promoted:4）。

### 反例：真实失败链路（异常/恢复演示）
运行 `20261012-212941-dd69`（provider=openai_compatible）：
- 步 1 intake 成功；步 2 `solution` 调真实 LLM 时收到 **`HTTP 504 Gateway Time-out`（openresty/1.27.1.2）**（`events.jsonl` seq 9）。
- 因 `solution` 是普通任务节点，失败分支为 `halt` → 运行以 **fail / 2 步** 终止（`run_summary.json` verdict=fail, steps=2）。
- 这暴露了真实 LLM 接入的最大风险（见 §8.3），以及"普通任务节点失败即终止"的设计取舍。

---

## 7. 异常处理与错误恢复机制

系统对异常做了**多层次**防御，从最细粒度到最粗粒度：

### 7.1 节点级原地重试（retry）`leader.py:531-553`
- `flow.yaml` 各节点可配 `retry.max_attempts`（默认 1，即不重试）。
- 失败时**原地重跑同一节点**（不改变流程位置），与"回退到上游"是两回事。
- 每次重试发 `node_retry` 事件。

### 7.2 回退（rollback）+ 权限 `leader.py:199-248, 555-571`
- 仅 `collaboration.rollback` 显式声明的边可回退：`review→implement(≤3)`、`test→optimize(≤3)`、`acceptance→optimize(≤2)`（`collaboration.yaml:40-52`）。
- **权限落地**：
  - `can_rollback=false` 的节点角色拒绝回退并 `halt`（`_rollback_permitted` L555-571）。
  - 普通任务节点（`intake`/`solution`）失败**不回退**，直接 `halt`。
- 每次回退发 `rollback` 事件 + 广播 `rollback` 主题。

### 7.3 升级策略（escalation）`leader.py:226-248`
- 单节点回退次数超 `max_attempts` **或** 全局回退超 `max_total_rollbacks(8)` → 触发升级。
- 默认 `on_exhausted: fail_run_and_report`：仍出具"验收不通过"报告并标注原因（发布 `acceptance_gate.passed=False, forced=True`），而非崩溃。
- 可配 `notify: [reporter, reviewer]`。

### 7.4 LLM 超时/截断降级链（针对真实大模型）`mas/llm.py`
真实端点（如 api.chat.csu.edu.cn，openresty 在 300s 强制断流）是主要故障源。处理：
- **可重试错误**（`_RetryableLLMError`）：504 / 读超时 / 429，按 `llm.retry.max_attempts`（默认 1）重试，耗尽后返回 `timed_out` 标记的结果，交由降级链而非让节点崩。
- **思考链占满 max_tokens 预算**：推理模型把 `reasoning_tokens` 计入 `max_tokens`，思考链打满 → 正文为空（`finish_reason=length`）。`_unusable()`（L229-243）识别这种"不可用"，按 `downgrade_reasoning_effort: "low,high"` 阶梯逐级换 `reasoning_effort` 重发（实测 low 约 35s 返回 12620 字符正文，high 约 115s 收敛）。
- **空正文诊断**：`_empty_output_error`（L311-325）把根因（思考链占满预算 / 网关超时）直接写进 `error`，避免静默失败。

### 7.5 Agent 兜底（try/except → node_error）`base.py:99-109`
`BaseAgent.act()` 用 `try/except` 包住整个执行：任何 Agent 异常都被转成 `result.status="fail"` + `error=f"{type}: {exc}"` 并发 `node_error` 事件，**绝不向上抛栈**导致整进程崩溃。生成结果无法解析为 JSON 也显式 `fail` 并告警（此前版本只置 fail 不告警，已在 `_generate_with_tools`/`act` 补上 `node_error`）。

### 7.6 全局兜底：最大步数 / 总回退上限 `leader.py:153, 255-259`
- `max_steps=40`：主循环步数上限，超过强制终止并 `forced_fail`。
- `max_total_rollbacks=8`：防回退死循环（配合 `forbid_self_loop: true` 禁止节点自转移）。

### 7.7 确定性离线 Provider 的"受控失败"
`providers/offline.py` 可配置 `inject_first_pass_defect: true` 与 `defect_ops: ["calc_shipping"]`、及 `strip_first_pass_docstrings: true`（`system.yaml:58-61`）：**首轮实现故意注入真实缺陷、缺文档字符串**，专门用来演示"审查驳回→修复→复测"的缺陷闭环。这是可关闭的演示开关，不是系统 bug。

---

## 8. 自学习闭环（L1/L2/L3）

经验库让系统"越跑越准"。链路（`experience.py` 模块 doc + `leader.py`）：

```
events.jsonl ──extract──▶ 分级经验库(L1/L2/L3) ──retrieve(打分)──▶ 分层注入 Prompt
      ▲                                                    │
      └────────── reinforce(hits/confidence) ◀─────────────┘
```

- **L1 即时经验**：每步之后 `_extract_inline()`（`leader.py:586-598`）**增量**处理本步新事件，提炼当轮缺陷模式 / 审查阻塞原因，同一次运行的后续节点即可注入。
- **L2 / L3 晋级**：运行结束 `_extract_run_end()` + `experience.promote()`；命中次数达阈值自动从 L1→L2→L3（`experience.py` 的 `tier` 配置 + `promote`）。
- **检索与注入**：`ExperienceLibrary.retrieve()` 按 `relevance × confidence × usage × recency` 打分，受 `tier.budget_tokens` 与 `max_items` 约束（`experience.py:290-363`），注入 Prompt 第 4 段，并落盘留痕（含经验 ID、层级、token 预算）。
- **确定性行为变化（关键）**：offline Provider 消费经验里的 `action_key`（如 `guard_calc_shipping__boundary`、`avoid_missing_function_docstring`），首轮起就补文档字符串、不再注入被守护的缺陷 → 第二轮步数从 12 降到 7（`README.md` 自学习闭环说明）。真实 LLM 路径则靠"经验写进 system prompt"间接影响行为。
- **可测量收益**：`workspace/reports/learning_report.md` 跨轮对比步数/回退/审查驳回/缺陷数；`experience_promoted` 事件在运行结束出现。

---

## 9. 性能特征与潜在瓶颈

### 9.1 性能特征（来自真实运行数据）
| 维度 | 观察（真实 run 数据） |
|---|---|
| 执行模型 | 单进程单线程**严格串行**；一次运行 ≈ 各节点耗时之和 |
| offline 确定性运行 | `20261009-101824-15fa` 全链路 11 步仅 **2.61s**（本地生成+沙箱执行，无网络） |
| 真实 LLM 运行 | 节点级单次调用常达 **3–5 分钟**；`20261012` 运行 2 步耗时 **535s**，其中包含 504 超时等待 |
| 节点耗时分布 | 瓶颈几乎全在 LLM 调用（生成大 JSON + 思考链），沙箱执行（import / unittest）通常 <1s |
| 回退放大 | 每多一轮审查/测试回退，整体耗时近似线性倍增（步数 11 的运行若全走真实 LLM 将达数十分钟） |

### 9.2 潜在瓶颈与风险
1. **真实 LLM 网关 300s 断流（最严重）**：openresty `proxy_read_timeout=300s`。非流式调用下模型"生成完前一字节不吐"会被当作总时长上限 → 504（运行 `20261012-212941-dd69` 即此）。缓解：流式调用 + 给足 `max_tokens`（实测 64000 档流式可用）。`mas/llm.py` 当前 `_post_with_retry` 用 `urllib` 同步阻塞且默认不重试，长任务会卡住主线程。
2. **思考链抢占输出预算**：推理模型 `reasoning_tokens` 计入 `max_tokens`，思考链打满 → 正文为空（`finish_reason=length`）。需靠 `downgrade_reasoning_effort` 阶梯补救，但**降级链仍可能三连空**，最终节点 fail。
3. **串行无并发**：5 个 Agent 从不并行；即便 `solution` 与后续节点无依赖也无法重叠执行。吞吐受限于单节点最慢 LLM 调用。
4. **同步阻塞网络 I/O**：`urllib.request.urlopen` 在 `timeout` 内阻塞主线程；无异步/批处理。
5. **磁盘 I/O 与文件锁**：每次 `publish` 都写盘 + 跨进程文件锁；`snapshot()` 每次门禁都扫磁盘目录；长运行会累积大量 `v*.json`。在 G: 盘（无回收站）等环境清理不便。
6. **重试配置默认关闭**：`llm.retry.max_attempts` 默认 1，意味着 504 发生即节点失败 → 普通节点直接 halt（如 §6 反例）。若要抗网关抖动，应显式开启 `llm.retry`。
7. **经验注入的 token 成本**：跨轮/L3 经验随运行累积，`tier.budget_tokens` 上限是软约束；经验过多会挤占主任务上下文。
8. **协商仅单向**：目前只有 `tester→developer` 一种协商；需求歧义若源自 product 阶段、或 developer 对 tester 的疑问，缺少反向通道。

### 9.3 配置驱动带来的"改流程不改代码"能力
- `flow.yaml` 增删节点 / 改 `transitions` / 改门禁表达式 → 直接改流程（`leader.py` 主循环通用）。
- `profiles/strict.yaml`（加"技术方案评审"节点 + 更严门禁）、`lean.yaml`（`skip: true` 裁剪审查节点，自动改接流转边，`config.py:_apply_skips` L548-563）。
- `collaboration.yaml` 改感知矩阵 / 广播矩阵 / 回退策略 / 升级策略。
- 这是该系统最大的架构亮点：流程编排与业务代码解耦。

---

## 10. 总结

**链路本质**：需求 →（产品结构化）→ 方案 →（研发编码+沙箱自检）→（评审 AST 静态拦截）→（测试真实执行+缺陷单）→（研发定向修复+回归）→（复测）→（门禁表达式+签署）→ 报告。所有协作通过"版本化黑板 + 持久化消息总线 + 协商中介"完成，单一 Leader 驱动状态机并兜底。

**设计优点**：
- 角色职责清晰、产物版本化可溯源、消息/事件全留痕（强可审计）。
- 配置驱动流程，编排与代码解耦，支持 Profile 裁剪。
- 多层异常兜底（retry / rollback / escalation / LLM 降级链 / Agent try-except / 步数·回退上限），单点故障不致崩溃。
- 自学习闭环（L1/L2/L3）使缺陷模式可沉淀、可测量复用。

**主要风险**：
- 真实 LLM 接入受网关 300s 断流与思考链抢占预算双重制约，是当前端到端可用性的最大瓶颈。
- 严格串行、同步阻塞网络，吞吐受限于最慢单节点；默认重试关闭使 504 直接终止普通节点。
- 协商通道单向、经验 token 成本随累积增长。

> 分析所用真实运行证据：
> - 全链路通过示例：`workspace/runs/20261009-101824-15fa`（pass / 11 步 / 2 回退 / 5 缺陷闭环）
> - 失败与回退示例：`workspace/runs/20261014-004436-9236`（review reject→rollback）、`20261012-212941-dd69`（504 → halt）
> - 源码事实来源：`mas/leader.py`、`mas/bus.py`、`mas/blackboard.py`、`mas/agents/*`、`mas/llm.py`、`mas/providers/offline.py`、`mas/experience.py`、`config/flow.yaml`、`config/collaboration.yaml`
