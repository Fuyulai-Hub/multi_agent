# 35 · 上下文管理：按 Codex 的分层机制做最小改动

> 目标：把「进模型的请求体」关进 token 预算里。
> 参考实现：仓库内 `codex-code/codex/codex-rs`（OpenAI Codex CLI 源码）。
> 改动量：新增 1 个模块（441 行）+ 3 处插入式接线（`mas/llm.py` +16 行、`mas/agents/base.py` +41 行、
> `config/system.yaml` +29 行），**既有代码 0 删除 0 改写**。

---

## 一、改造前：框架里没有"上下文管理"，只有三处各自为政的截断

| 现有机制 | 位置 | 是否受预算约束 |
|---|---|---|
| 感知段单产物字符上限 10000 | `base.perceive` → `blackboard.brief` | ❌ 固定值，与窗口无关 |
| 技能注入段总字符上限 88000 | `base.build_prompt` | ❌ 固定值，与窗口无关 |
| tool 结果截断 4000（头 60% + 尾 40%） | `base._generate_with_tools` → `compress.truncate_tool_results` | ❌ 固定值 |
| `llm.context_window: 262144` | `config/system.yaml` | ❌ **只被前端进度条读**，不参与任何判定 |

也就是说：窗口大小写在配置里，但没有任何代码去拦"请求体超过窗口"这件事。
真跑飞了只能等端点回 400。而 `prompts.py` 里那个 `_estimate_tokens`
（`cjk + other/3.2`）只用于落盘展示字段 `token_estimate`，不参与决策。

**缺的不是"更聪明的压缩"，是"先量、再判、再动手"这整条链。**

---

## 二、Codex 是怎么做的（源码对照）

Codex 的上下文管理是 **4 套彼此独立**的机制，名字很像但完全不是一回事：

| 机制 | 作用对象 | 源码位置 | 调模型？ |
|---|---|---|---|
| **truncate** | 单条 tool output（写进历史时） | `utils/output-truncation/src/lib.rs`、`utils/string/src/truncate.rs` | ❌ 纯规则 |
| **trim** | 提交前把 tool output 换成占位符直到塞得下 | `core/src/compact_remote_history.rs:70` | ❌ 纯规则 |
| **compact** | 整段历史 → 一条摘要 | `core/src/compact.rs:264` | ✅ 真实请求一次 |
| rollout compression | 磁盘 `.jsonl` 的 zstd 压缩 | `rollout/src/compression.rs` | ❌ **与上下文无关** |

关键常量与默认值（都在源码里核过）：

| 项 | 值 | 位置 |
|---|---|---|
| `APPROX_BYTES_PER_TOKEN` | **4**（token = UTF-8 字节数 / 4） | `utils/string/src/truncate.rs:6` |
| `auto_compact_token_limit` | **context_window × 90%**（与配置值取 min） | `protocol/src/openai_models.rs:529` |
| `effective_context_window_percent` | **95**（硬上限） | `openai_models.rs:393` |
| `COMPACT_USER_MESSAGE_MAX_TOKENS` | 20 000 | `core/src/compact.rs:64` |
| `truncation_policy` | `{mode: tokens, limit: 10000}` ×1.2 序列化余量 | `models.json:15` |
| `async_compaction_threshold_percent` | 65（后台预压缩，本次未做） | `core/src/config/mod.rs:226` |

三层调用顺序（这是最值得抄的骨架）：

```
tool 执行完  -> [truncate] 单条 ≤ policy*1.2（保留头 + 保留尾，中间挖掉）
提交前      -> [trim]     从最新往回把 tool output 换成占位符，直到估算 ≤ 窗口
超阈值      -> [compact]  调模型生成摘要，只保留 user 消息 + 末尾追加摘要
```

判定优先信**服务端返回的 `usage`**，本地字节/4 只是拿不到 usage 时的兜底
（`session/mod.rs:5012` vs `:5144`）。

---

## 三、本框架的最小改动落点

### 3.1 新增 `mas/context.py`（441 行）

照抄上面的三层骨架，做成 `ContextManager.fit(messages)`：

```
fit(messages)
 ├─ 未启用 或 估算 ≤ compact_limit   -> 原样返回（逐字节不变）
 ├─ 阶段① truncate  tool 结果头 60% + 尾 40%（只作用于 role=tool）
 ├─ 阶段② trim      按「工具往返」整轮丢弃，至少留最近 keep_recent_rounds 轮
 ├─ 阶段③ compact   把丢弃内容折叠成一条摘要挂回（只要丢过就挂）
 └─ 仍超 hard_limit -> 标 overflow，只告警不再切
```

三个必须守住的细节（都是会真出事的点）：

1. **`system` 与首个 `user` 永不被改。** 这两段是角色人设 + 任务指令 + 交付契约，
   切一刀就把末尾的 JSON 契约切坏了——比超窗更致命。`_split_prefix()` 负责划界。
2. **trim 必须整轮丢弃。** `assistant(tool_calls)` 与其后的 `tool` 消息同生共死：
   单独删掉 assistant，tool 消息的 `tool_call_id` 就找不到归属，严格实现的端点直接 400。
   `_group_units()` 按此切单元。
3. **丢过内容就必须挂摘要。** 体积回到预算内 ≠ 信息没丢。这条是"不静默"的底线。

### 3.2 接线（3 处，全部为插入）

| 文件 | 插入点 | 行为 |
|---|---|---|
| `mas/llm.py` | `__init__` | 构造 `ContextManager`（代码默认 `enabled=False`） |
| | `generate` 开头 | 清空 `last_fit`（防止上一轮/上一节点的治理结果被误报） |
| | `generate` 末尾 | `context.observe(generation)` —— 用端点真实 `prompt_tokens` 校准 |
| | `_build_payload` | **唯一生效点**：`fit(messages)` 后再组装 payload |
| `mas/agents/base.py` | `_trace_llm` | trace 增加 `context` 字段（预算 + 估算 + 实测，恒记录） |
| | `act()` | 调 `_trace_context()`：治理真发生过才发 `context_compacted` 事件 + 写入 `metrics.context` |
| `config/system.yaml` | `runtime.context` | 见下 |

`_build_payload` 是**唯一**组装请求体的地方，所以只要在那里拦一道，
openai_compatible 的每一次请求（含工具多轮、ReAct 多步、降级重发）都必然经过治理。

### 3.3 配置（`config/system.yaml` → `runtime.context`）

```yaml
context:
  enabled: true
  compact_ratio: 0.90          # 达可用预算 90% 起动手（Codex auto_compact_token_limit）
  hard_ratio: 0.95             # 硬上限；超出只告警
  reserve_output: true         # 先从窗口扣掉 max_tokens(64000) 再算可用预算
  tool_result_chars: 4000      # 阶段①单条 tool 结果预算
  keep_recent_rounds: 1        # 阶段②至少保留最近几轮工具往返
  summary_max_chars: 2000      # 阶段③摘要字符上限
  calibrate: false             # 用端点 prompt_tokens 校准粗估（默认关，保持确定性）
```

代码默认 `enabled=False`，`system.yaml` 实配 `true`——与 `runtime.skills` 同一约定：
不配置即关闭，配置了才生效，既有行为不受影响。

---

## 四、与 Codex 的两处**有意**分歧（不是没做到，是不该照抄）

### 分歧一：阈值先扣 `max_tokens`

Codex 的 90% / 95% 是按整个 `context_window` 算的。本框架 `max_tokens=64000`，
占 262144 窗口的 **24%**——不预留的话，"输入还剩 24% 就撞墙"会被判成"还有余量"。
所以本实现先算 `usable = window - max_tokens`，再取百分比：

```
usable         = 262144 - 64000 = 198144
compact_limit  = 178329   (90%)
hard_limit     = 188236   (95%)
```

`reserve_output: false` 可退回 Codex 原口径。

### 分歧二：摘要用规则抽取，不让模型生成

Codex 的摘要是**一次真实模型请求**（`compact.rs:826`）。本实现默认用规则抽取
（动作 / 观察 / 结论三类事实），理由是可验证的：

- 本端点网关 `proxy_read_timeout=300s` 是**已实测**的失败模式（docs/34 记过 3×300s 全空）；
- 而压缩恰恰只在"上下文已经很大"时才触发——那时再发一次请求，
  是把自己主动送进最容易超时的区间。

规则抽取的好处：确定性、零额外调用、可回放、可断言。
需要模型摘要时给 `ContextManager.summary_fn` 传一个函数即可，**不必改本模块**。

---

## 五、验证

### 5.1 单元测试：43/43 通过（`tests/test_context.py`）

锁定的契约：

| # | 契约 | 用例 |
|---|---|---|
| 1 | 估算口径 = 字节/4（`abcd`→1，`abcde`→2，`中文`→2） | `test_bytes_divided_by_four_matches_codex` |
| 2 | **关闭时恒等**（窗口给到 10 token 也不动手） | `test_disabled_even_when_massively_over_window` |
| 3 | **未超限不动** | `test_under_limit_no_change` |
| 4 | 阶段①头尾保留（错误堆栈在末尾，必须留住） | `test_truncate_stage_shrinks_tool_output_keeping_head_and_tail` |
| 5 | 阶段①够了就不许继续丢历史 | `test_truncate_stage_alone_satisfies_budget` |
| 6 | 阶段②整轮丢弃、`tool_call_id` 配对不散、保留最近轮 | `test_trim_drops_oldest_round_as_a_unit` |
| 7 | 阶段③丢过必挂摘要（即使已回到预算内） | `test_summary_is_appended_even_when_trim_already_fits` |
| 8 | `system` + 首个 `user` 永不被改 | `test_system_and_first_user_never_modified` |
| 9 | overflow 只告警，契约完好（`{"files": []}` 仍在） | `test_overflow_flagged_not_contract_broken` |
| 10 | 校准默认关、开启后收敛 | `test_calibration_converges_when_enabled` |
| 11 | 真实 `LLMClient` 端到端：发出去的请求体确是治理后的 | `test_llm_client_fits_payload_before_sending` |
| 12 | offline 不触网、不受影响 | `test_offline_provider_untouched` |

### 5.2 实测：真实流水线各节点 Prompt 体积

跑一遍真实流水线（技能注入按项目默认开启 = 最坏情况），读运行目录落盘的
`prompts/*.md`（即真正下发的 system + user），用本模块的估算器量一遍：

| 节点 | 字符 | 估算 token |
|---|---|---|
| report#1 | 62 093 | **17 606**（峰值） |
| implement#2 | 47 774 | 14 004 |
| implement#1 | 42 088 | 12 277 |
| optimize#1 | 36 249 | 11 195 |
| review#1 / #2 | 29 492 / 28 794 | 9 776 / 9 733 |
| test#2 / #1 | 25 059 / 22 670 | 8 211 / 7 575 |
| acceptance#1 | 18 361 | 6 511 |
| intake#1 | 18 002 | 6 201 |
| solution#1 | 18 812 | 5 883 |
| consultation#0 ×2 | 2 478 / 1 751 | 1 087 / 830 |

**峰值 17 606 token = compact_limit(178 329) 的 9.87%。**
即：开启治理后当前流水线**一次都不会被改动**——它现在是一张纯安全网，
只在大上下文（工具多轮堆积、技能满配、长报告回灌）时才出手。

### 5.3 真实数据回放：把窗口压到峰值的 0.8 倍

把上面那些**真实 Prompt** 灌回 `ContextManager`，外加两轮工具历史（30 000 字符一条），
窗口压到 14 084（compact_limit 12 675）：

| 节点 | stages | before → after | dropped | 摘要 | 骨架完好 |
|---|---|---|---|---|---|
| report#1 | truncate → trim → compact → **overflow** | 25 249 → 17 825 | 2 | 289 字符 | ✅ |
| implement#2 | truncate → trim → compact → **overflow** | 21 647 → 14 223 | 2 | 289 字符 | ✅ |
| implement#1 | truncate → trim → compact | 19 920 → 12 496 | 2 | 289 字符 | ✅ |

前两个标 overflow 是**正确行为**：它们的 system+user 本身就超过这个被人为压小的窗口，
再切就切坏交付契约了，所以只告警不动手。三个节点 `system` / `user` 均逐字节完好。

### 5.4 全量回归

```
Ran 436 tests in 90.172s
OK
```

### 5.5 改动面

```
config/system.yaml    |  29 +++   （0 删除）
mas/llm.py            |  16 +++   （0 删除）
mas/agents/base.py    | +41 插入   （0 删除）
mas/context.py        | 441 行（新增）
tests/test_context.py | 576 行（新增）
```

---

## 六、已知边界（没做 / 做不到 / 不建议做）

1. **offline provider 全程不生效。** 它不构造请求体（`_generate_offline` 直接走
   `OfflineProvider`），因此治理与留痕都不触发。与 ReAct 同口径：离线永远退化单轮。
   需要治理证据必须用 `openai_compatible`（哪怕对本地桩）。
2. **留痕粒度是"最近一次生成"。** `last_fit` 在每次 `generate` 开头清空，
   所以一个节点内多轮工具调用时，只有触发治理的那一轮被记下来。
   要逐轮累积需改 `LLMClient` 的生命周期语义，本次未动。
3. **未做后台预压缩。** Codex 的 `async_compaction_threshold_percent=65%`
   会在后台提前跑压缩；本框架是同步的，只在提交前那一刻动手。
4. **未做服务端远程压缩。** Codex 的 `compact_remote_v2` 依赖服务端能力，本端点没有。
5. **未做跨节点历史。** 本框架每个节点的 Prompt 是从黑板重建的（无跨节点消息历史），
   所以"上下文堆积"只发生在单个节点内的工具多轮；这与 Codex 的长会话是不同问题。
6. **估算仍是粗下界。** 字节/4 对中文会低估（一个中文字 0.75 token），
   `calibrate: true` 可用端点返回的 `prompt_tokens` 收敛，但默认关闭以保持确定性。
