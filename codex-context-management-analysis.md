# Codex 源码上下文管理机制分析

> 分析对象：`codex-code/codex/codex-rs`（OpenAI Codex CLI 的 Rust monorepo，本工作区副本）。
> 所有结论均给出 `文件:行号`，可逐条回查。

---

## 0. 结论速览

Codex 的上下文管理不是"一个模块"，而是**四层叠加**：

| 层 | 职责 | 核心文件 |
|---|---|---|
| ① 写入即截断 | 工具/命令输出在进入历史前就被砍到预算内 | `utils/output-truncation/src/lib.rs`、`tools/mod.rs:125` |
| ② 历史不变量 | 每次发请求前规范化：call/output 配对、孤儿清理、模态剥离 | `core/src/context_manager/history.rs:968`、`normalize.rs` |
| ③ token 计量与阈值 | 服务端 usage 为权威，本地字节估算兜底；按阈值判定是否该压缩 | `core/src/session/context_window.rs`、`history.rs:939` |
| ④ 压缩/换窗 | 到阈值后按能力三路分流：直接换窗 / 服务端压缩 / 本地模型摘要 | `core/src/session/turn.rs:1514`、`compact*.rs` |

一句话概括触发逻辑：**每个 turn 的每次采样前后都算一次 token 账，超过「自动压缩限额（默认上下文窗口的 90%）+ fallback buffer」或「有效窗口（默认 95%）」就压缩；压缩本身也是一次普通模型请求（或一次直接换窗）。**

---

## 1. 一次模型请求的上下文是怎么拼出来的

### 1.1 入口：turn 循环

`core/src/session/turn.rs:561-567`（每次采样）：

```rust
let sampling_request_input: Vec<ResponseItem> = async {
    sess.clone_history()
        .await
        .for_prompt(&step_context.settings.model_info.input_modalities)
}
```

历史是唯一的上下文来源；`for_prompt` 返回的是**规范化后的请求副本**，落盘的 rollout 仍保留完整 payload（见 `history.rs:517-518` 注释："Tool output truncation applies only to live history, preserving full rollout payloads"）。

### 1.2 for_prompt 的三步规范化

`context_manager/history.rs:603-617` → `normalize_history`（`history.rs:968-982`）：

```rust
fn normalize_history(&mut self, input_modalities: &[InputModality]) {
    // all function/tool calls must have a corresponding output
    normalize::ensure_call_outputs_present(items);
    // Paired outputs must have a corresponding call; named external outputs stand alone.
    normalize::remove_orphan_outputs(items);
    // strip images when model does not support them
    normalize::strip_images_when_unsupported(input_modalities, items);
    // strip audio when model does not support it
    normalize::strip_audio_when_unsupported(input_modalities, items);
}
```

- `ensure_call_outputs_present`（`normalize.rs:21`）：发现没有 output 的 FunctionCall，**插入一条合成 output，内容为 `"aborted"`**，id 用固定 UUID5 命名空间生成（`normalize.rs:19`，注释明确说改这个值会失效 prompt cache）。
- `remove_orphan_outputs`：反向清理没有 call 的 output。

### 1.3 组装成 Responses 请求

`core/src/client.rs:956-1012` `build_responses_request`：

```rust
let mut input = prompt.get_formatted_input_for_request(model_info);
...
let mut prefix = Vec::new();
// tools（responses_lite 模式下作为 AdditionalTools 进 input）
if !prompt.base_instructions.text.is_empty() {
    let mut instructions = ContextualUserFragment::into(BaseInstructionsFragment(
        prompt.base_instructions.text.clone(),
    ));
    instructions.set_id(Some(ResponseItemId::with_suffix(
        "msg",
        Uuid::new_v5(&prefix_namespace, prompt.base_instructions.text.as_bytes()),
    )));
    prefix.push(instructions);
}
input.splice(0..0, prefix);
```

关键点：

1. **system instructions 不是独立字段，而是 `input[0]` 的一条 developer 消息**。`ResponsesApiRequest`（`codex-api/src/common.rs:279-304`）根本没有 `instructions` 字段，只有 `input`/`tools`/`reasoning`/`store`/`include`。
2. instructions 的 item id 用 `thread_id + 文本内容` 的 UUID5 生成——**同文本同 id，保证重试/恢复会话时 prompt cache 不失效**（`client.rs:973-978`）。
3. 非 OpenAI provider 会逐个 item 清掉 `encrypted_content`（保留 Reasoning/Compaction 的），避免把密文发给不支持的端点（`client.rs:1013-1032`）。

---

## 2. token 计量：服务端权威 + 本地估算兜底

### 2.1 权威值来自服务端 usage

流式 `Completed` 事件带回 `token_usage`，`compact.rs:891-899`（普通路径在 session 里同构处理）：

```rust
sess.record_observed_response_completed(...).await;
sess.update_token_usage_info(turn_context, token_usage.as_ref()).await?;
```

`update_token_info`（`history.rs:886-896`）把它合并进 `TokenUsageInfo`（`new_or_append`，非覆盖）。

### 2.2 总账：`get_total_token_usage`

`history.rs:939-956`：

```rust
pub(crate) fn get_total_token_usage(&self, server_reasoning_included: bool) -> i64 {
    let last_tokens = self.token_info.as_ref().map(|i| i.last_token_usage.total_tokens).unwrap_or(0);
    let items_after_last_model_generated_tokens = self.items_after_last_model_generated_item()...;
    if server_reasoning_included {
        last_tokens + items_after_last_model_generated_tokens
    } else {
        last_tokens + self.get_non_last_reasoning_items_tokens() + items_after_last_model_generated_tokens
    }
}
```

即：**服务端最后一次报的 total + 那次之后本地新追加条目的估算**；若服务端不把历史 reasoning 计入，还要补上"最后一个用户轮次之前"的加密 reasoning 估算（`get_non_last_reasoning_items_tokens`，`history.rs:898-922`）。

### 2.3 本地估算：模型可见字节 / 4

`history.rs:1074-1077`：

```rust
pub(crate) fn estimate_item_token_count(item: &ResponseItem) -> i64 {
    let model_visible_bytes = estimate_response_item_model_visible_bytes(item);
    approx_tokens_from_byte_count_i64(model_visible_bytes)
}
```

`approx_tokens_from_byte_count`（`utils/string/src/truncate.rs:96-100`）= `ceil(bytes/4)`；`approx_token_count(text)`（同文件 87-90）同样 `(len+3)/4`。**这就是 multi_agent 项目 `mas/context.py` "抄 Codex 字节/4" 的出处。**

分类型估算细节（`history.rs:1102-1216`）：

| 条目类型 | 估算方式 |
|---|---|
| Message 文本 | `text.len()` 字节 |
| Message 图片 | `detail:original` → 解 base64 按 32px patch 数，上限 10,000 patch（`ORIGINAL_IMAGE_MAX_PATCHES`）；否则固定 `RESIZED_IMAGE_BYTES_ESTIMATE = 7373` 字节 |
| 音频 | `estimate_audio_bytes` |
| Reasoning（有密文） | `len*3/4 - 650`（`estimate_reasoning_length`，`history.rs:1058-1064`） |
| Reasoning（明文） | **0，不计入**（`history.rs:1208-1211`） |
| Compaction 密文 | `ceil(len/3)` 再按 /4（`estimate_compaction_content_length`，`protocol.rs:3503-3505`；注释说明密文含 replay cache，约 3 倍膨胀） |
| FunctionCall | `name + namespace + arguments` 字节 |
| FunctionCallOutput | output body + call_id/name/namespace |
| AdditionalTools/ToolSearch/LocalShellCall | 序列化 JSON 字节 |

`estimate_token_count_with_base_instructions`（`history.rs:661-683`）还有一条去重：历史里已经记录过 instructions（或工具声明）时，base 计 0，避免重复计量。

---

## 3. 阈值判定：`session/context_window.rs`（全文 131 行，核心中的核心）

```rust
// context_window.rs:53-117
let active_context_tokens = sess.get_total_token_usage().await;
// scope 二选一
match config.model_auto_compact_token_limit_scope {
    Total => (active_context_tokens, model_info.auto_compact_token_limit(), None),
    BodyAfterPrefix => {
        let baseline = window.prefill_input_tokens.unwrap_or(active_context_tokens);
        (active_context_tokens.saturating_sub(baseline), scope_limit, window.prefill_input_tokens)
    }
};
// 有效窗口 = 上下文窗口 × effective_context_window_percent / 100（默认 95）
let full_context_window_limit = model_info.resolved_context_window()
    .map(|cw| cw.saturating_mul(model_info.effective_context_window_percent) / 100);
let token_limit_reached = buffered_auto_compact_limit.is_some_and(|l| auto_compact_scope_tokens >= l)
    || full_context_window_limit_reached;
let turn_end_compaction_threshold_reached = post_turn_percent > 0
    && (token_limit_reached || full_context_window_limit.is_some_and(|limit| {
        active * 100 >= limit * post_turn_percent
    }));
```

配套常量与取值：

| 名称 | 值 | 位置 |
|---|---|---|
| `auto_compact_token_limit()` | `min(配置值, 上下文窗口 × 90%)` | `protocol/openai_models.rs:529-540` |
| `effective_context_window_percent` | 默认 **95** | `openai_models.rs:393-395` |
| fallback buffer | `token_budget.auto_compact_fallback_buffer_tokens`，只在配了 fallback prompt 时叠加 | `context_window.rs:98-103` |
| `model_post_turn_compact_threshold_percent` | 默认 **0 = 关闭** turn 末压缩 | `config/mod.rs:4437-4439` |
| `async_compaction_threshold_percent` | 默认 **65**（占有效窗口百分比） | `config/mod.rs:226, 3335-3341` |

`base_window_tokens_remaining = min(scope 剩余, 全窗剩余)`，同时喂给两个消费方：模型可调用的 `get_context_remaining` 工具（`tools/handlers/get_context_remaining.rs:86-94`）和 token budget 提醒（见 §10）。

---

## 4. 触发时机全景（`session/turn.rs`）

| 时机 | 判断 | 代码 |
|---|---|---|
| **PreTurn**（turn 开始、新用户输入入史之前） | `run_pre_sampling_compact`：先按"上一模型 comp_hash 变了 / 换成更小窗口模型且已超限"决定是否用**旧模型**先压缩一次；再按 `token_limit_reached` 或异步候选就绪触发常规压缩 | `turn.rs:200-207`、`1378-1409`、`1423-1505` |
| **MidTurn**（每次采样后） | `should_roll_over = needs_follow_up && (take_new_context_window_request() \|\| token_limit_reached \|\| candidate.is_some())` | `turn.rs:666-723` |
| **PostTurn**（模型不再 follow-up） | `model_post_turn_compact_threshold_percent > 0` 且达到百分比（默认关闭） | `turn.rs:788-831` |
| **错误恢复** | 采样返回 `ContextWindowExceeded` 且处于 Guardian 预算耗尽场景 → 压缩一次后重试，只试一次防死循环 | `turn.rs:836-866` |
| **压缩过程中再超窗** | 本地压缩请求拿到 `ContextWindowExceeded`：`turn_input_len > 1` 就 `remove_first_item()` 从头删一条再重试；只剩 1 条则 `set_total_tokens_full` 把账强制记满然后报错 | `compact.rs:359-371` |
| **异步后台预压缩** | `Feature::AsyncCompaction && provider 支持 RemoteCompaction V2 && !TokenBudget`，且用量 ≥ 65% 有效窗；按 `history_version + model_slug + comp_hash + auth generation` 四要素判候选是否过期 | `session/async_compaction.rs:40-155` |

开关总闸：`auto_compaction_enabled = thread_extension_data 里没有 AutoCompactionDisabled`；`auto_summarization_enabled = auto_compaction_enabled && !TokenBudget`（`turn.rs:181-187`）。

---

## 5. 压缩的三路分流（`turn.rs:1514-1610` `run_auto_compact`）

```rust
if turn_context.config.features.enabled(Feature::TokenBudget) {
    // ① 不摘要，直接开新上下文窗口
    crate::compact_token_budget::run_inline_auto_compact_task(...).await?;
    return Ok(true);
}
match turn_context.provider.capabilities().remote_compaction {
    RemoteCompactionSupport::V2 => { /* ② 服务端压缩 */ }
    RemoteCompactionSupport::Unsupported => { /* ③ 本地模型摘要 */ }
}
```

Guardian 基础会话且 `!should_compact_guardian_input` 时直接 `Ok(false)` 跳过（`turn.rs:1524-1529`）。

---

## 6. 本地摘要压缩（`compact.rs`，924 行）

### 6.1 摘要请求就是一次普通 turn

`compact.rs:118-153`：把 `config.compact_prompt` 或默认 `SUMMARIZATION_PROMPT`（`prompts/templates/compact/prompt.md`，内容是"做一次 CONTEXT CHECKPOINT COMPACTION，写交接摘要"）包成 `UserInput::Text`，走 `run_compact_task_inner`：pre/post compact hooks → `emit ContextCompaction item` → 建 `ModelClientSession` → `drain_to_completed` 循环（重试用 `backoff(retries)`，预算 `stream_max_retries`）。

注意 `compact.rs:301-306`：压缩用的 input 是 `history.for_prompt()`，并且把**已执行完的工具调用**（`executed_tool_calls.attach_to_compaction_prompt`）也挂上去，避免摘要后调用/输出配对断裂。

### 6.2 压缩后历史的构造

`compact.rs:407-436`：

```rust
let mut new_history = build_compacted_history(Vec::new(), &user_messages, &summary_text);
...
let (new_history, world_state_baseline) = build_compaction_replacement_history(...).await;
sess.replace_compacted_history(new_history, ...).await;
sess.recompute_token_usage(&replacement_step_context.turn).await;
```

`build_compacted_history_with_limit`（`compact.rs:715-819`）保留策略：

- **只保留用户消息**，逆序（新→旧）选取，预算 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`（`compact.rs:64`）；
- 旧摘要消息（`is_summary_message`，以 `SUMMARY_PREFIX\n` 开头）被跳过，不会嵌套摘要；
- 单条 `tokens <= remaining` 且内容全是 `InputText` → **原样保留**（含原 id）；否则只保留文本部分并按 `remaining` 做 annotated 截断（保留 content kind/source 标注），超预算时打 `mark_retained_sources_incomplete()`；
- 最后压入一条 `CompactionSummary`（`summary_text = SUMMARY_PREFIX + "\n" + summary_suffix`）；空摘要退化为 `"(no summary available)"`。

`insert_initial_context_before_last_real_user_or_summary`（`compact.rs:651-700`）负责把**当前 session 的世界状态上下文重新注入**，插入点优先是"最后一个真实用户/agent 消息"之前，保证系统/环境上下文不丢。

收尾：`advance_auto_compact_window()` 记窗口号，并发一条 Warning 事件提醒"长线程多次压缩会掉准确性，尽量开新线程"（`compact.rs:442-445`）。

### 6.3 PostTurn 的特殊处理

`compact.rs:393-403`：PostTurn 阶段强制要求压缩响应里必须有非空 assistant 摘要，否则整个压缩判失败（`"Post-turn compaction completed without an assistant summary"`）。

---

## 7. 服务端压缩（`compact_remote_v2.rs` 及同目录文件）

常量（`compact_remote_v2.rs:87-91`）：

```rust
pub(crate) const RETAINED_MESSAGE_TOKEN_BUDGET: usize = 64_000;
const MAX_RETAINED_AGENT_MESSAGE_TOKENS: i64 = 10_000;
const MAX_REMOTE_COMPACTION_V2_STREAM_RETRIES: u64 = 2;
```

特色机制在 `compact_remote_history.rs`：

- `history_item_groups`（`:41-55`）：把一条 tool 输出和紧跟其后的 `ImageResizeNotice` developer 消息**成组计量**，删改不拆散；
- `trim_function_call_history_to_fit_context_window`（`:70-136`）：从**新到旧**遍历，把超限的 FunctionCallOutput/CustomToolCallOutput/ToolSearchOutput 整体替换成固定文案 `"Output exceeded the available model context and was truncated"`（保留 success 标志），直到估算落回上下文窗口内；带 `RetainClientToolOutputs` 标记的 retained output 是边界，不动；
- 改写后 `history.replace_annotated(items)`，返回 `(改写条数, 估算省下的 token)`。

---

## 8. 工具输出截断：双层防线

### 8.1 第一层：产出时截断

- `tools/mod.rs:125`：`formatted_truncate_text(&content, truncation_policy)`；
- `unified_exec/process.rs:341`：命令输出 snippet 同样处理；
- MCP 结果：`truncate_mcp_tool_result`（`output-truncation/src/lib.rs:45-87`）——整体序列化超限时换成 text preview，并用循环缩放预算避开 JSON 转义导致的二次膨胀。

`formatted_truncate_text`（`:23-34`）会在头部加一行 `Warning: truncated output (original token count: N)\nTotal output lines: M`。

### 8.2 第二层：入历史时再截断

`history.rs:568-577`（`record_item_with_metadata`）：

```rust
if let ResponseItem::FunctionCallOutput { output, .. }
| ResponseItem::CustomToolCallOutput { output, .. } = &mut processed.item
{
    let policy = metadata.and_then(|m| m.history_truncation_token_limit)
        .map(TruncationPolicy::Tokens)
        .unwrap_or_else(|| with_serialization_allowance(policy));
    truncate_function_output_payload(output, policy, estimate_audio_token_count);
}
```

- `with_serialization_allowance` = `policy * 1.2`（`output-truncation/src/lib.rs:19-21`），20% 留给序列化和头部；
- `TruncationPolicy` 支持 `Bytes` / `Tokens` 两种模式（`protocol.rs:3509-3512`），具体值由后端 `/models` 下发的 `truncation_policy` 决定（`openai_models.rs:452`）；
- 截断形状：**头尾各保留一半**（`split_budget`，`truncate.rs:143-145`），中间替换为 `…N tokens truncated…` 标记，且**标记不计入预算**（`truncate.rs:17-29`）；
- content items 逐条过预算：文本超了截到剩余额度，音频按 token 成本整条丢弃，最后补 `[omitted N text items ...]` / `[omitted N audio items ...]`（`lib.rs:240-249`）。

---

## 9. 历史结构本身的不变量（`context_manager/history.rs`）

- `is_api_message`（`:1033-1056`）：`role == "system"` 的消息、`CompactionTrigger`、`Other` **永远不进历史**；`ConfigurationUpdate` 必须有 harness 元数据才收。
- `record_item_with_metadata`（`:555-598`）顺序：API 过滤 → tool output 截断 → 双轨 review_history（Guardian）→ retained_context 记账 → push。`items` 是 `Arc<Vec<ResponseItemEnvelope>>`，快照便宜。
- `remove_first_item`（`:685-697`）：删最老一条，并调用 `normalize::remove_corresponding_for` **成对删掉配对项**，维持 call/output 配对（压缩超窗重试就靠它）。
- `history_version`（`:646-648`）单调递增，`replace_annotated`/`replace_compacted` 都会 +1，是异步压缩候选有效性的判据。
- `drop_last_n_user_turns`（`:777-884`）：rollback 用，按"用户轮边界"回退，并向上清理紧邻的 contextual developer/user 消息；若裁到混合 bundle 会清掉 `reference_context_item` 基线，迫使下次全量重注入（`:1001-1029`）。

---

## 10. 本 fork 的特色：World State / ContextualUserFragment

`core/src/context/` 下有 50+ 个 `ContextualUserFragment` 实现（`base_instructions`、`environment_context`、`current_time_reminder`、`memory`、`user_goal`、`token_budget_context`……），每个片段自带 `content_kind`、起止 markers 和 role（多为 developer）。

- **按 section diff 渲染**：`render_step_world_state`（`history.rs:462`）每步只把**发生变化的 section** 作为新 item 写入历史（`record_step_world_state_if_changed`，`session/mod.rs:3762-3794`），不变的上下文不重复占用 token。
- **token budget 片段**（`context/token_budget_context.rs`）：
  - `TokenBudgetContext`：以 `<context_window>` 标签向模型声明 first/previous/current 三个 window id（`:75-87`）；
  - `TokenBudgetRemainingContext`："You have N tokens left in this context window."（`:184-191`）；
  - `TokenBudgetReminder` / `AutoCompactFallbackPrompt`（`:195-262`）。
- **`session/token_budget.rs:161-224` `maybe_record`**：`remaining ≤ reminder_threshold_tokens` 时发一次提醒（`claim_token_budget_reminder` 保证只发一次）；`remaining == 0` 时发 `auto_compact_fallback_prompt`（提示模型"先落盘再换窗"）。
- **模型可自省**：`get_context_remaining` 工具把 `base_window_tokens_remaining` 直接返给模型（`tools/handlers/get_context_remaining.rs:86-96`）。

---

## 11. 配置项速查

| 配置 | 默认 | 作用 |
|---|---|---|
| `model_context_window` | 无 | 覆盖模型上下文窗口 |
| `model_auto_compact_token_limit` | 无（缺省 = 窗口 90%） | 自动压缩限额 |
| `model_auto_compact_token_limit_scope` | `Total` | `Total` 全量计 / `BodyAfterPrefix` 只计窗口 prefill 之后 |
| `model_post_turn_compact_threshold_percent` | 0（关） | turn 末压缩阈值 |
| `features.async_compaction.model_async_compact_threshold_percent` | 65 | 后台预压缩启动阈值 |
| `features.token_budget.*` | 关 | 开启后压缩 = 直接换窗（不摘要） |
| `compact_prompt` / `experimental_compact_prompt_file` | 内置模板 | 自定义摘要 prompt |
| 模型侧 `truncation_policy` | 后端下发 | 工具输出截断策略（Bytes/Tokens） |
| 模型侧 `effective_context_window_percent` | 95 | 有效窗口比例 |

---

## 12. 测试锚点（改这块代码前先跑这些）

- `core/tests/suite/token_budget.rs`：阈值、reminder、fallback、window id 渲染的端到端用例（桩 `context_window = 128_000`、`reminder_threshold = 6_144`、`fallback_buffer = 16_384`）。
- `core/src/compact_tests.rs`、`compact_remote_history_tests.rs`、`compact_remote_v2_image_budget_tests.rs`：压缩构造与替换逻辑。
- `core/src/context_manager/history_tests.rs`（133KB）：历史不变量、token 估算、rollback。
- `core/src/session/async_compaction_tests.rs`：后台压缩启停与候选校验。
- `app-server/tests/suite/v2/compaction.rs`：协议层压缩事件。

---

## 13. 与 multi_agent 项目 `mas/context.py` 的差异（供对照）

| 维度 | Codex | multi_agent（`mas/context.py`） |
|---|---|---|
| token 估算 | 模型可见字节 / 4（ceil） | 相同（明确抄自此） |
| 阈值口径 | 窗口 90%（压缩限额）与 95%（有效窗）双阈值 | 先扣 `max_tokens` 再算 usable（64000 占 24% 的场景） |
| 摘要生成 | 模型生成（一次真实请求） | 规则抽取（规避网关 300s 超时），留了 `summary_fn` 注入点 |
| 生效点 | `for_prompt`/`_build_payload` 每次请求 | 同为 `llm.py:_build_payload` 唯一生效点 |
| 附加能力 | world-state section diff、异步预压缩、双轨 review history | 未实现（属可选演进方向） |
