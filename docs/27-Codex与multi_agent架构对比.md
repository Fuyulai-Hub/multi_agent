# Codex CLI 与 multi_agent 架构对比分析

## 0. 分析前提（先说清楚证据来源与边界）

| 项 | 值 |
|---|---|
| Codex 源码 | `codex-code/codex`（github.com/openai/codex），main 分支 `70b3bb6`，2026-10-18 22:36 UTC |
| 核心实现 | `codex-rs/`，Rust，100+ crate；主力逻辑在 `core/`（约 300 个 .rs） |
| multi_agent 源码 | 本仓库 `mas/`，Python 3.13，零第三方依赖（仅 PyYAML） |
| 方法 | 逐文件定位 + 贴真实代码片段，所有结论均带 `文件:行号`；未实现的能力明确标注"未见实现" |

**一个必须先纠正的直觉**：Codex 的 Agent 主循环**不在** `core/src/agent/`（该目录只管子 Agent / 多智能体委派与管控），真正的 turn 循环在 `core/src/session/turn.rs`。按目录名去猜会找错地方。

**规模量级差异**（这是后面所有对比的底色）：Codex 是 100+ crate、9293 文件的工业级 Rust 工程；multi_agent 是 26 个 .py 文件的单进程 Python 系统。因此本文的对比目的**不是**"把 Codex 搬过来"，而是回答两个问题：
1. 哪些设计差异解释了 multi_agent 当前踩的坑（尤其是"任务跑完仍判不通过"）；
2. 哪些机制是**低成本可迁移**的，哪些在 Python 单进程架构下根本不该做。

---

## 1. 总览：十个维度的对照

| 维度 | Codex CLI | multi_agent | 差距性质 |
|---|---|---|---|
| 主循环终止条件 | **资源预算**：无轮数上限，靠 token 预算 + 压缩兜底 | **步数预算**：`max_steps=40` + 回退上限 8 + 节点 `max_attempts` | 设计哲学不同（见 §2） |
| 工具调用 | `parallel_tool_calls: true` + `FuturesOrdered` 保序并发 | 串行 `for` 循环，无异步 | 架构级（无 asyncio） |
| 沙箱 | 平台级真隔离：Linux landlock+seccomp+bwrap / macOS seatbelt / Windows 受限令牌 | `subprocess` + 命令白名单 + AST 静态检查；Windows 无资源限制 | 量级差 |
| 网络隔离 | MITM 代理 + 域名 allow/deny，5 种阻断原因 | 无（源码注释明示做不到），仅静态标记给 reviewer | 能力缺口 |
| 审批 | `AskForApproval` 四档 + `Granular` 五子开关 + session 级批准缓存 + Guardian 自动审批 | **无**（`leader.py:172` 自述"全流程无人工干预"） | 能力缺口 |
| 上下文压缩 | 双路径：远程 V2（服务端）/ 本地再调模型 / token-budget 换新窗口；阈值 = 窗口×9/10 | 本地正则压缩，**默认关闭** | 开关未开 + 能力差 |
| 项目指令文件 | `AGENTS.md` 向上遍历 + `AGENTS.override.md` 覆盖，上限 32 KiB | **未见实现**（grep 零命中） | 能力缺口 |
| 长期记忆 | `memories/` crate：两阶段流水线（抽取 → 全局合并），落 `~/.codex/memories/` + SQLite | `experience.py`：JSONL + bigram 重叠系数检索 | 形态不同 |
| 多 Agent | 真实实现：`spawn_agent` 工具 + `AgentControl` + 消息板 + 角色 toml | 固定流水线串行调度，Agent 间靠 Blackboard + 消息摘要 | 形态不同 |
| 配置层级 | 9 层显式优先级栈（Packaged→Mdm→System→Enterprise→User→Project→CLI→Legacy×2） | 6 个 YAML + profile 深合并 + `credentials.yaml` 覆盖 | 够用 |

---

## 2. Agent 循环

### 2.1 Codex：无计数器的 `loop` + 语义终止

主循环在 `core/src/session/turn.rs:461`，外层是一个**没有任何计数器**的 `loop`：

```rust
// core/src/session/turn.rs:461-470
let mut next_step_context = Some(first_step_context);
loop {
    let pending_input = if can_drain_pending_input {
        sess.input_queue.get_pending_input(&sess.active_turn).await.0
    } else { Vec::new() };
    if run_hooks_and_record_inputs(&sess, &turn_context, ..., &pending_input,
        PersistContext::SteeredUserInput).await { break; }
```

每轮的动作是：从 history 重建 `ResponseItem` 列表（`turn.rs:561`）→ `run_sampling_request` 采样 → 处理工具调用 → 判定 `needs_follow_up`：

```rust
// core/src/session/turn.rs:611, 725-727
let needs_follow_up = model_needs_follow_up || has_pending_input;
...
if !needs_follow_up {
    last_agent_message = sampling_request_last_agent_message;
    let stop_outcome = run_turn_stop_hooks(...).await;
```

**关键点：全仓库没有 `max_turns` / `MAX_TOOL_CALLS` 这类常量。**唯一的硬上限是单次采样流的重试次数：

```rust
// model-provider-info/src/lib.rs:67, 73
const DEFAULT_STREAM_MAX_RETRIES: u64 = 5;
const MAX_STREAM_MAX_RETRIES: u64 = 100;
```

工具轮数**无界**，靠两件事兜底：上下文压缩（见 §5）和 token 预算。

取消是**双 token** 设计：`cancellation_token`（硬中止）与 `preempt`（软抢占，允许先排空当前响应）——

```rust
// core/src/session/turn.rs:1817-1825
let retry = handle_response_stream_error(...).or_cancel(&preempt).or_cancel(&cancellation_token).await?;
if cancellation_token.is_cancelled() { return Err(CodexErr::TurnAborted); }
if preempt.is_cancelled() {
    return Ok((SamplingRequestResult { needs_follow_up: true, last_agent_message: None }, ...));
}
```

**这个双 token 值得注意**：软抢占会把已生成的上下文保留下来（`needs_follow_up: true`），硬取消才丢弃。对比 multi_agent 的取消是"节点边界检查 + 收尾"，粒度更粗但没有中间态复杂度的负担。

流式 SSE 解析在 `codex-api/src/sse/responses.rs:541` 的 `process_sse_with_treatment`，用 `eventsource()` 解帧 + `timeout(idle_timeout, stream.next())` 强制空闲超时——和 multi_agent `mas/llm.py` 手写 SSE 增量拼接是同一层职责，只是 Rust 侧把空闲超时单独抽成参数。

### 2.2 multi_agent：步数 / 回退双闸门

`Leader.run()` 是单线程同步 `while`，由 `config/flow.yaml` 的状态机驱动：

```python
# mas/leader.py:398, 395-396
while node_id and node_id not in flow.terminal and steps < max_steps:
...
max_steps = min(int(flow.max_steps), int(self.config.runtime.get("max_steps", flow.max_steps)))
max_total_rollbacks = int(collaboration.guards.get("max_total_rollbacks", 8))
```

`forced_fail` 有 4 个触发点：环境/证据通道缺口不可修复（`:463`）、回退次数耗尽（`:501`）、达到 max_steps（`:530`）、角色拒绝回退（`:475`）。

### 2.3 对比判断：**这是本次分析最重要的一条**

两者的循环控制哲学**根本不同**：

- Codex 用**资源预算**（token / 上下文窗口）做软约束，配合压缩让循环自然收敛；
- multi_agent 用**步数与回退次数**做硬闸门，闸门一到就直接 `forced_fail`。

这正好解释了 2026-10-19 定位到的那个根因：test 节点因 `ui_probe.enabled=false` 导致证据通道缺口，用例 30 条全过仍判 fail，此后 3 轮 optimize 全在改代码、"越改越差"，最后撞 `max_attempts=3` 被判死。

**在 Codex 的模型里这类问题不会发生在这个位置**——因为它不靠"回退几次"来判定失败，而是靠"模型是否还要继续"（`needs_follow_up`）。当然这不意味着 Codex 更好：无上限循环的风险是失控成本，Codex 用 token 预算和压缩来对冲，multi_agent 用步数上限来对冲，**两者都是自洽的**。multi_agent 真正的缺陷不是"有步数上限"，而是：

1. **闸门判定与真实完成度脱钩**——`tester.py:436-439` 在证据通道缺失时无条件 fail，而 `leader.py` 只看 fail/pass，不看 fail 的原因；
2. **已有兜底但运行期没生效**——`leader.py:1064-1086` 的 `_unfixable_failure_reason` 本应在 test #1 就收尾到 report（探针实测 4/4 返回非空），但运行期 4 轮全走 rollback。

也就是说：**问题不在循环结构，在失败分类与兜底路径没有接上**。这一点比"抄 Codex 的无上限循环"重要得多——把 `max_steps` 从 40 改大只会多烧几小时。

---

## 3. 沙箱

### 3.1 Codex：平台级真隔离，三 crate 分层

```
protocol（类型定义） → codex-sandboxing（策略 + 命令变换） → 平台专用 crate（实际执行）
```

`SandboxType` 定义在 `protocol/src/sandbox.rs:27`：

```rust
pub enum SandboxType { None, MacosSeatbelt, LinuxSeccomp, WindowsRestrictedToken, WindowsMxc }
```

平台分派（`sandboxing/src/manager.rs:50-64`）**明确返回 None 表示无沙箱**：

```rust
if cfg!(target_os = "macos") { Some(SandboxType::MacosSeatbelt) }
else if cfg!(target_os = "linux") { Some(SandboxType::LinuxSeccomp) }
else if cfg!(target_os = "windows") {
    if windows_sandbox_enabled { Some(SandboxType::WindowsRestrictedToken) } else { None }
}
```

**值得注意：Codex 在 Windows 上默认也是没有沙箱的**（需显式开启 `windows_sandbox_enabled`，走受限令牌 + Job object）。multi_agent 在 Windows 上 `sandbox.py:328` 直接 `return {}`，两者在 Windows 上的处境其实相似——差异在 Linux/macOS 侧 Codex 有真隔离，multi_agent 有 rlimit 但多线程时会降级跳过（`sandbox.py:332-333`）。

写路径限制用 `writable_roots` 表达，且**额外阻断元数据目录**：

```rust
// protocol/src/protocol.rs:1208-1225
WorkspaceWrite { writable_roots: Vec<AbsolutePathBuf>, network_access: bool, ... }
/// ... (e.g. `.codex`, `.git`, notably `.git/hooks`)
pub struct WritableRoot {
```

网络走 MITM 代理，域名级匹配，阻断原因有 5 类（`network-proxy/src/reasons.rs:1-9`）：`denied` / `not_allowed` / `not_allowed_local` / `method_not_allowed` / `proxy_disabled`。

命令执行前的风险评估是**前缀规则 + 危险命令启发式**两级（`execpolicy/src/policy.rs:305-331`）：先匹配 `prefix_rule`，未命中才回落到启发式。三档判定 `Decision { Allow, Prompt, Forbidden }`（`execpolicy/src/decision.rs:9`）。

**沙箱失败可以升级重试**（这个设计很实用）：

```rust
// core/src/tools/orchestrator.rs:429-441
if !tool.escalate_on_failure() { ... return Err(ToolError::Codex(err)); }
if !tool.wants_no_sandbox_approval(approval_policy) { ... return Err(...); }
// 随后把权限改写成 RequireEscalated 再走一次审批（orchestrator.rs:506-517）
*sandbox_permissions = SandboxPermissions::RequireEscalated;
```

### 3.2 multi_agent：进程级约束 + 静态检查

`subprocess.run(shell=False)` + 命令白名单（`config/system.yaml:84`，默认 `["python","python3","py","node"]`）+ AST 规则扫描（`sandbox.py:33` 的 `_NETWORK_MODULES` 等）。源码注释对能力边界说得很诚实：

```python
# mas/sandbox.py:31
# 沙箱无网络隔离（标准库无跨平台网络命名空间方案）
# mas/sandbox.py:328-329
if os.name == "nt" or not self.rlimits:
    return {}
```

### 3.3 差距与可迁移性

| Codex 机制 | multi_agent 可迁移性 | 说明 |
|---|---|---|
| 平台级 landlock/seatbelt | ❌ 不该做 | Python 标准库无对应能力，需额外依赖 |
| 域名 allow/deny 网络策略 | ⚠️ 半可行 | 可用 `urllib` 层拦截，但子进程可绕过；收益有限 |
| `writable_roots` + 阻断 `.git` | ✅ **低成本高价值** | 纯路径校验，落盘前判定，约 20 行 |
| 沙箱失败 → 升级重试 | ✅ 可借鉴 | multi_agent 目前沙箱失败直接进回退，白吃一轮预算 |
| 危险命令前缀规则表 | ✅ 可借鉴 | 现有是白名单，可加"需上报"中间档 |

注意一个**反直觉的点**：multi_agent 的沙箱比 Codex 更"严"的地方也有——Codex 允许执行任意命令（靠沙箱隔离），multi_agent 只允许 python/node（白名单）。两者是不同安全模型：**Codex = 允许一切 + 隔离后果；multi_agent = 禁止危险动作 + 不隔离后果**。前者更强但需要 OS 支持，后者在 Windows 上漏洞明显（白名单里的 python 可以自己 `import socket`）。

---

## 4. 审批机制

### 4.1 Codex：四档策略 + 五子开关 + session 缓存 + Guardian

```rust
// protocol/src/protocol.rs:1097-1120
pub enum AskForApproval {
    UnlessTrusted,   // 非可信项目：一律问
    #[default]
    OnRequest,       // 默认：模型自己决定何时问
    Granular(GranularApprovalConfig),  // 细粒度
    Never,           // 从不问（依赖沙箱兜底）
}
// protocol/src/protocol.rs:1122-1137
pub struct GranularApprovalConfig {
    pub sandbox_approval: bool, pub rules: bool, pub skill_approval: bool,
    pub request_permissions: bool, pub mcp_elicitations: bool,
}
```

审批事件 `EventMsg::ExecApprovalRequest`（`protocol/src/protocol.rs:1597`），UI 回 `ReviewDecision`（`:4269`），含 7 种结果：

```rust
Approved / ApprovedExecpolicyAmendment { .. } / ApprovedForSession /
ApprovedMcpPolicyAmendment / NetworkPolicyAmendment { .. } / Denied { .. } / TimedOut / Abort
impl Default for ReviewDecision { fn default() -> Self { Self::Denied { .. } } }  // 默认拒绝
```

**默认拒绝**（fail-closed）这一点是所有审批系统的正确默认值。

session 内批准缓存是真实实现（`core/src/tools/sandboxing.rs:41`）：

```rust
pub(crate) struct ApprovalStore { map: HashMap<String, ReviewDecision> }  // key 是 serde 序列化串
// sandboxing.rs:89-115
let already_approved = { let store = services.tool_approvals.lock().await;
    keys.iter().all(|key| matches!(store.get(key), Some(ReviewDecision::ApprovedForSession))) };
```

Guardian（`core/src/guardian/decision.rs:20`）是**自动审批复核器**——独立线程 `codex-approval-review` 跑评审，可代替用户决策；但契约明确写着不表态就回落给用户：

```rust
/// None requests the existing user flow. No contributor is never an implicit allow.
```

MCP 工具甚至支持**跨会话持久化批准**（`ApprovedMcpPolicyAmendment` 写回策略文件）。

命令安全判定分两条路：patch 走 `core/src/safety.rs:79`（判据是"是否全落在 writable roots 内 + 沙箱可用"），shell 走 `core/src/exec_policy.rs:777`（前缀规则 + 危险命令启发式）。**没有简单的命令白名单表**——是靠规则引擎 + 启发式。

### 4.2 multi_agent：完全没有

```python
# mas/leader.py:172
"""流水线调度器（全流程无人工干预）。"""
```

grep `confirm|approval|approve|审批|人工` 在 `mas/`、`config/` 下仅 4 处命中，全部是提示文案或状态别名，不是机制。

### 4.3 该不该补？

**结论：不该全量补，但两处值得做。**

Codex 的审批是为**交互式 CLI**设计的——有真人坐在终端前。multi_agent 的定位是"全流程无人工干预"批处理，加完整审批等于改产品定位。

但有两点是批处理系统同样需要的：

1. **危险动作的事前拦截**（非审批，是策略）：对应 Codex 的 `Decision::Forbidden`。multi_agent 现在只有"允许/不允许"两态，缺"明确禁止并给出原因"这一态。例如子进程绕过检测（`sandbox.py:634` 的 `sandbox_command_bypass`）目前只产出一个 finding，severity 由 reviewer 的 LLM 填——**这正是记忆里记录的"拦截是高概率不是必然"问题**。
2. **运行级批准缓存的对象化**：Codex 用 `ApprovalStore` 统一管理 exec / patch / MCP 三类批准。multi_agent 若将来做 Web UI 确认按钮，需要这个抽象，否则会在三处各写一遍。

---

## 5. 上下文管理

### 5.1 Codex：三条压缩路径 + 片段化上下文拼装

**上下文拼装**用"片段（fragment）"模式：`core/src/context/` 下 55 个片段文件，各自渲染成一个可注入的小段，由 `Session::build_initial_context_with_world_state`（`core/src/session/mod.rs:4473`）按 role 归并拼进 developer / user 消息。这与多 Agent 系统的"各角色贡献一段 prompt"是同一个思路，只是 Codex 把它做成了显式的一等公民（每个片段一个文件）。

`ContextManager` 本体（`core/src/context_manager/history.rs:91`）是一个 `Arc<Vec<ResponseItemEnvelope>>` 线性 transcript，改写时 bump `history_version`。丢弃策略很朴素——**从最旧 item 开始删**：

```rust
// core/src/context_manager/history.rs:685-688
pub(crate) fn remove_first_item(&mut self) {
    // Remove the oldest item (front of the list). Items are ordered from oldest → newest
```

**压缩有三条真实路径**（`core/src/session/turn.rs:1540-1615` 三路分支）：

| 路径 | 实现 | 是否再调模型 |
|---|---|---|
| `Feature::TokenBudget` | `compact_token_budget.rs`：直接换新上下文窗口 | 否（"假压缩"） |
| 远程 V2 | `compact_remote_v2.rs`：走 provider capability `remote_compaction` | 服务端压缩 |
| 本地 | `compact.rs`：`SUMMARIZATION_PROMPT` + 复用 client_session | **是** |

触发阈值：

```rust
// protocol/src/openai_models.rs:529-540
let context_limit = self.resolved_context_window().map(|w| (w * 9) / 10);   // 窗口 × 90%
// core/src/config/mod.rs:226
const DEFAULT_ASYNC_COMPACTION_THRESHOLD_PERCENT: u8 = 65;                  // 后台压缩 65%
```

压缩后**保留什么**很明确（`compact.rs:702-819`）：丢弃全部 assistant / tool 消息，保留 `initial_context` + 从最新往回取的用户消息（token 预算 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000` 内）+ 摘要。是 **token 预算驱动**，不是"保留最近 N 条"。

压缩失败会**换模型重试**（`compact_model_fallback.rs:9-33`），条件严格：必须换了模型 + 当前是 OpenAI provider + codex backend 或 API key 认证。

会话持久化是 JSONL rollout，落 `~/.codex/sessions/rollout-{时间戳}-{thread_id}.jsonl`（`rollout/src/recorder.rs:82`），恢复 = 读 JSONL → `InitialHistory::Resumed` → 回灌 ContextManager。另有 `state` crate 把 rollout 元数据镜像进 SQLite 做检索。

### 5.2 multi_agent：本地规则压缩，且默认关闭

`mas/compress.py:12` 模块注释写得很清楚：

```
本模块不 import LLM（LLM 摘要列为可选第二步，默认关闭）。
```

触发条件是**总闸 + 阈值**两级，总闸关着就恒等返回：

```python
# mas/compress.py:145-146
if not enabled or not found:
    return CompressionResult(dict(found), before, before, [], list(found.keys()))
```

`config/system.yaml:126` → `context_compression.enabled: false`。策略是白名单正则压缩（`_STRATEGIES`）→ 超 `per_item_chars=10000` 走深度压缩 → 超 `total_chars=32000` 按 `_TOTAL_TRIM_ORDER` 裁剪。tool 结果截断是头 60% + 尾 40%（`compress.py:199`）。

### 5.3 对比判断

**差距不在算法，在"是否开启"和"是否闭环"。**

- Codex 的压缩是**闭环**的：上下文窗口 90% 自动触发 → 压缩 → 循环继续。这是它能用"无上限循环"的前提。
- multi_agent 的压缩模块写好了但开关关着，于是上下文只能靠各种 `max_chars` 硬截（`tool_result_max_chars=4000`、`prompt_artifact_max_chars=10000`）。

**但这里有个必须诚实说的点**：Codex 的本地压缩路径要**再调一次模型**（`run_inline_auto_compact_task`），在 multi_agent 的 HPC 端点上，一次大 `max_tokens` 请求实测要 400s+（记忆里 64000 档流式实测 422.5s）。所以"开启 LLM 摘要压缩"在 multi_agent 当前的端点条件下**代价很高**，不宜照搬。

**低成本可做的两件事**：

1. **打开本地规则压缩**（改 `system.yaml:126` 一个键）。风险是记忆里记录的用例集漂移问题——压缩改的是输入，会让每轮 prompt 变化，导致测试基线不稳。建议配合固定用例集一起做。
2. **借鉴 Codex 的"丢弃顺序"显式化**。Codex 丢弃最旧 item 时会调 `normalize::remove_corresponding_for`（`normalize.rs:243`）删掉对应的孤儿 tool output。multi_agent 的 `_TOTAL_TRIM_ORDER = ["test_report","review_report","bug_reports"]` 是**按类型**砍，可能砍掉 tool call 却留下它的 output（或反之），造成 history 不一致。这个"配对删除"逻辑约 30 行可移植，且是本地规则、不调模型。

---

## 6. 记忆与项目指令

### 6.1 Codex：AGENTS.md（项目指令）+ memories crate（长期记忆）两套

**AGENTS.md 体系**（`core/src/agents_md.rs`）：

```rust
// core/src/agents_md.rs:42-49
pub const DEFAULT_AGENTS_MD_FILENAME: &str = "AGENTS.md";
pub const LOCAL_AGENTS_MD_FILENAME: &str = "AGENTS.override.md";
```

发现方式是**从 cwd 向上遍历到 project root，逐目录收集**（`agents_md.rs:189-232`），大小上限：

```rust
// config/src/config_toml.rs:75
pub const DEFAULT_PROJECT_DOC_MAX_BYTES: usize = 32 * 1024;   // 32 KiB
```

逐环境扣减 `remaining`，扣完即停。另有线程级指令独立上限 10 000 token（**超限报错而非截断**，这与项目级的截断策略不同）。

**长期记忆 `memories/` crate** 是真实的两阶段流水线（`memories/README.md:40-42`）：

```
Phase 1: Rollout Extraction (per-thread) —— 每个 rollout → raw_memory + rollout_summary + rollout_slug
Phase 2: Global Consolidation —— 合并 stage-1 输出，再 spawn 一个内部 consolidation sub-agent
         改写 MEMORY.md / memory_summary.md / skills/
```

落盘 `~/.codex/memories/`（`memories/write/src/lib.rs:119`）+ SQLite，且带 git 版本库做 baseline。注入 prompt 时有界截断：

```rust
// core/src/context/memory.rs:12
/// Memory-owned model context, capped below 10k tokens even for byte-heavy text.
```

**注意 Phase 2 用了一个内部 sub-agent 来改写记忆**——记忆的"提炼"本身是 LLM 任务，不是规则任务。

**Skills**：真实实现，`$skill-name` 显式提及 + 隐式调用两种触发，且 SKILL.md 可声明 `SkillDependencies { tools: ... }`（`skills/src/model.rs:79`）走 MCP 挂载——即技能可以是可执行的，不只是文本注入。

### 6.2 multi_agent：experience.py（经验库）+ skills（技能库）

经验库是 JSONL + 文件锁，检索用**中文 bigram 重叠系数**（非 Jaccard）：

```python
# mas/experience.py:556-561
query = _expand_with_synonyms(_tokenize_for_match(task_text), self._synonyms)
return len(query & haystack) / max(1, min(len(query), len(haystack)))
```

写入去重键 `Experience.key`（scope + action_key + dedupe），命中则合并（置信度累加、tags 并集）。打分是加权：role 0.5 / tag 0.3 / keyword 0.2 + 置信度 + 30 天指数衰减。注入预算 1000 token。

技能库 `mas/skills.py`：文本注入（trigger 匹配）+ 工具注册（front-matter 声明 `tool.script` → `skill_<id>` 工具）双轨并存。

**AGENTS.md 类机制：未见实现**——grep `AGENTS\.md|CLAUDE.md|project_instruction` 在 `mas/`、`config/`、`docs/`、`README.md` 下零命中。

### 6.3 对比判断

| | Codex | multi_agent |
|---|---|---|
| 项目指令 | AGENTS.md 向上遍历，用户可写，32 KiB | 无 |
| 长期记忆写入时机 | 后台异步，P1→P2 两阶段，LLM 提炼 | 每个节点结束同步写，规则抽取 |
| 记忆提炼者 | LLM sub-agent | 无（规则字段抽取） |
| 检索 | （未深挖，走 SQLite + 文件） | bigram 重叠系数 + 同义词扩展 |
| 技能是否可执行 | 是（MCP 依赖声明） | 是（沙箱脚本） |

**最值得迁移的一条：项目级指令文件（AGENTS.md）。** 理由：

- 成本极低（向上遍历 + 拼字符串 + 32 KiB 上限，约 40 行）；
- 正好补上 multi_agent 当前的一个真实痛点——**intake 把"俄罗斯方块"误判为 `full_stack_spa`**（记忆里记录的根因）。如果有项目级指令文件，用户可以直接写"本项目是单页游戏，不做前后端分离"，无需改 `intent.py` 的分类规则；
- 与 multi_agent 已有的 `skills.max_chars=4000` 截断机制同构，不需要新的预算体系。

相比之下，**两阶段 LLM 记忆提炼不建议现在做**：multi_agent 的单次 LLM 调用成本太高（见 §5.3），而 `experience.py` 的规则抽取在当前规模（55 次运行）下够用。真要做应该是先解决"独立 worker 进程"（docs/26 已论证），把后台异步跑起来之后再谈。

---

## 7. 工具系统（附带对比，因为影响 §2）

**Codex**：注册表 `ToolRegistry`（IndexMap）+ `ToolRouter`，注册入口 `build_tool_router`（`spec_plan.rs:148`），四类注册函数（shell / mcp resource / core utility / collaboration）。`web_search` 是 **hosted 服务端工具**，不注册为本地 handler——这是个有意思的取舍：把搜索完全交给服务端，本地只做渲染。

并行调用是真并发保序：

```rust
// core/src/session/turn.rs:2746, 1687
let mut in_flight: FuturesOrdered<InFlightFuture<'static>> = FuturesOrdered::new();
...
parallel_tool_calls: true,
```

**multi_agent**：`mas/toolrunner.py:118` 严格串行 `for` 循环，全项目零 `asyncio` / `await`。工具来源三类：MCP（`config/mcp.yaml` 当前全注释，实际未启用）、内置本地（`web_search`）、技能工具 `skill_<id>`。

**并行工具调用在 multi_agent 不建议补**：引入 asyncio 会牵动整个 `mas/` 的同步假设（沙箱、文件锁、事件落盘全部是同步的），收益是省几个串行调用的时间，而真正的耗时大头是 LLM 请求本身（单次 400s+）。**投入产出比极差**。

---

## 8. 多 Agent 协作

**Codex 是真的实现了**（不是接口预留）：

```rust
// core/src/tools/handlers/multi_agents.rs:76-84
pub(crate) use close_agent::Handler as CloseAgentHandler;
pub(crate) use resume_agent::Handler as ResumeAgentHandler;
pub(crate) use send_input::Handler as SendInputHandler;
pub(crate) use spawn::Handler as SpawnAgentHandler;
pub(crate) use wait::Handler as WaitAgentHandler;
```

spawn 是真起子线程（`codex_delegate.rs:46`），且**强制要求子 Agent 的审批策略为 `Never`**（子 Agent 不能再弹审批给人类）。并发默认值：

```rust
// core/src/config/mod.rs:256-266
DEFAULT_AGENT_MAX_THREADS: Option<usize> = Some(6);
DEFAULT_MULTI_AGENT_V2_MAX_CONCURRENT_THREADS_PER_SESSION: usize = 4;
DEFAULT_AGENT_MAX_DEPTH: i32 = 1;
```

消息板有远端 + 内存双实现（`agent_message_board.rs:38`），角色从 `*.toml` 加载（`agent-roles/src/discovery.rs:9`）。

**multi_agent 是固定流水线串行**：`Leader.run` 同一时刻只有一个 Agent 在 `act()`，Agent 间靠 Blackboard 产物 + MessageBus 广播（`bus.py` 是内存 + 同步落盘 JSONL，无队列无订阅）。

**这个差异不是缺陷，是产品定位不同**：Codex 是"模型自主决定要不要派子任务"的开放循环；multi_agent 是"预定义软件工程流水线"的确定性编排。后者在可预测性和可观测性上更强（有明确的节点、门禁、事件流），代价是无法处理流水线之外的任务形态——**而这正是 intake 误判 `full_stack_spa` 后整条链路崩掉的原因：流水线里没有"单页游戏"这个分支**。

如果要改，方向不是"抄 Codex 的动态派发"，而是**让流水线节点内的行为可分化**（记忆里已记录：`flow.transitions` 只有 pass/fail 两条边，不支持条件路由，所以只能做节点内行为分化 + 门禁 expr 分支）。

---

## 9. 可迁移改造清单（按性价比排序）

| 优先级 | 改造项 | 来源 | 成本 | 解决什么 |
|---|---|---|---|---|
| P0 | **接上失败分类兜底**：让 `_unfixable_failure_reason` 在运行期真正生效 | 非抄 Codex，是本仓库既有代码 | 中（先定位为何运行期未生效） | 4 次运行白烧 3.9h 后 forced_fail |
| P0 | **项目级指令文件（AGENTS.md 等价物）** | `agents_md.rs:42` | ~40 行 | intake 意图误判无法被用户纠正 |
| P1 | **丢弃顺序配对化**：删 tool call 时同步删 output | `normalize.rs:243` | ~30 行 | history 不一致风险 |
| P1 | **writable_roots + 阻断 `.git`/`.workbuddy`** | `protocol.rs:1208` | ~20 行 | 沙箱写路径无边界校验 |
| P1 | **危险动作显式 `Forbidden` 态**（区别于"产出 finding 让 LLM 判"） | `execpolicy/decision.rs:9` | 小 | 拦截是概率性的，非确定性 |
| P2 | 打开本地规则压缩 | 本仓库既有 | 1 个配置键 | 上下文只能硬截 |
| P2 | 沙箱失败 → 升级重试（而非直接进回退） | `orchestrator.rs:429` | 中 | 沙箱失败白吃一轮回退预算 |
| ❌ | 并行工具调用 | `turn.rs:2746` | 大（需引入 asyncio） | 收益远小于成本 |
| ❌ | 平台级沙箱隔离 | `landlock.rs`/`seatbelt.rs` | 极大 | Python 标准库无对应能力 |
| ❌ | LLM 摘要压缩 / 两阶段记忆提炼 | `compact.rs`/`memories/` | 大（单次调用 400s+） | 端点成本高，收益不确定 |
| ❌ | 完整人工审批 | `AskForApproval` | 大 | 与"全流程无人工干预"定位冲突 |

---

## 10. 不确定性声明

1. **Codex 的 `responses/compact` 端点路径未直接看到字符串常量**——远程压缩的实现走 `ResponsesStreamRequest::RemoteCompactionV2` + provider capability 探测，端点 URL 由 client 层封装，本文未深挖 client 层。
2. **Codex 记忆的检索侧（读路径）未深挖**——`memories/read` crate 只确认了注入截断策略（<10k token），检索算法未核实。
3. **`DEFAULT_ASYNC_COMPACTION_THRESHOLD_PERCENT = 65`** 与 `auto_compact_token_limit`（90%）是两个不同阈值，本文按源码注释理解（后台提前压缩 65% / 硬触发 90%），未做运行时验证。
4. **Guardian 的评审模型与判据未核实**——只确认了它是独立线程跑的自动审批复核器，且"不表态即回落用户"，未看评审 prompt。
5. 所有 Codex 代码结论均基于 main 分支 `70b3bb6` 的静态阅读，**未编译、未运行**。

---

## 附：关键文件速查

| 主题 | Codex | multi_agent |
|---|---|---|
| 主循环 | `core/src/session/turn.rs:461` | `mas/leader.py:398` |
| 工具循环 | `core/src/session/turn.rs:2746`（FuturesOrdered） | `mas/agents/base.py:138` |
| 沙箱策略 | `sandboxing/src/manager.rs:50` | `mas/sandbox.py:328` |
| 命令风险评估 | `core/src/exec_policy.rs:777` | `mas/sandbox.py:289` |
| 审批策略 | `protocol/src/protocol.rs:1097` | 无 |
| 批准缓存 | `core/src/tools/sandboxing.rs:41` | 无 |
| 上下文拼装 | `core/src/session/mod.rs:4473` | `mas/prompts.py` |
| 压缩调度 | `core/src/session/turn.rs:1540` | `mas/compress.py:145` |
| 项目指令 | `core/src/agents_md.rs:42` | 无 |
| 长期记忆 | `memories/write/src/lib.rs:119` | `mas/experience.py:243` |
| 多 Agent | `core/src/tools/handlers/multi_agents.rs:76` | `config/flow.yaml:11` |
| 配置层级 | `config/src/config_layer_source.rs:30` | `mas/config.py:602` |
