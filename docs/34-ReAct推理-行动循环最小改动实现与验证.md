# 34 · ReAct（推理-行动）循环：最小改动实现与验证

## 一、改造前：框架已有的不是 ReAct

`mas/agents/base.py:_generate_with_tools` 已经有一个工具闭环（docs/21），形态是：

```text
generate(tools=…) → 模型返回 tool_calls → ToolRunner.execute 真实执行 → tool 消息回传 → 续跑
```

它和 ReAct 的差别不是"能不能调工具"，而是**推理过程对框架是否可见**：

| 维度 | 改造前（工具闭环） | ReAct |
| --- | --- | --- |
| 决策依据 | 模型直接吐 tool_calls，正文常为空 | 每步先输出 Thought，再决定 Action |
| 框架可见性 | 只落 `tool_calls_requested`（工具名 + 入参） | 落 thought / action / observation 三元组 |
| 观察的用处 | 回传进 messages，但对人不可见 | 既进 messages，也进事件流可审计 |
| 步数语义 | 复用 `llm.tool_max_rounds` | `runtime.react.max_steps` 独立控制 |

改造前如果模型没有输出正文（绝大多数 OpenAI 兼容端点的行为），整条轨迹里就只有
"调了什么工具"，没有"为什么调"。**这就是本次要补的部分**——不是新增能力，而是把已有能力
的推理过程显式化、可审计化。

## 二、最小改动原则与落点

约束：默认关闭时代码路径逐字节不变；不新增角色、不新增流程节点、不动既有行。

实际改动（纯新增，**0 行删除**）：

```
config/system.yaml        +14 行   runtime.react 配置段（默认 enabled: false）
mas/agents/base.py       +254 行   ReAct 循环 + Thought 抽取 + 契约恢复
tests/test_react_loop.py  新增      11 个用例（真实组件，非 mock）
```

既有代码只被"插入"过三处，没有一行被改写：

1. `_generate_with_tools` 开头加 2 行分流（未开启时直接落到原逻辑）；
2. `build_prompt` 里 directive 追加 ReAct 协议指令（未开启时不追加）；
3. `_assistant_tool_message` 之前插入 ReAct 方法块。

### 2.1 配置（`config/system.yaml` → `runtime.react`）

```yaml
react:
  enabled: false              # 默认关闭：走改造前闭环
  nodes: []                   # 空=全部节点；非空=仅这些节点走 ReAct
  max_steps: 4                # 步数上限（不含步数用尽后的收尾轮）
  thought_max_chars: 2000     # 思考落事件的字符上限（0=不截断）
  observation_max_chars: 1500 # 观察结果落事件的字符上限
  require_thought: false      # true=发起 Action 却无思考时告警（不阻断）
```

**生效范围是三重交集**：`react.enabled` ∧ `react.nodes`（空=全部）∧ `llm.tools_nodes`
（工具下发白名单）。任一不放行就退化为单轮生成——这条保证了开启配置不会把没有工具
的节点拖进死循环。

### 2.2 循环骨架

```python
for step in range(1, max_steps + 1):
    generation = llm.generate(…, tools=tools, extra_messages=extra_messages)
    thought = self._extract_thought(generation)          # Thought
    if not generation.tool_calls:                        # 收敛：不再要工具
        # 正文常是「思考 + ```json 块」混排 → 兜底恢复 JSON
        if generation.data is None: generation.data = recover_final_json(generation.text)
        emit("react_final", …); return generation
    extra_messages.append(self._assistant_tool_message(generation))   # Action 入栈
    tool_messages = runner.execute(generation.tool_calls)             # 真实执行
    emit("react_step", {"thought":…, "actions":…, "observations":…})  # Observation
    extra_messages.extend(tool_messages)
# 步数用尽仍在要工具 → 沿用改造前兜底：追加一次不带 tools 的生成强制收敛
```

三个设计取舍：

- **Thought 从哪来**：`_extract_thought` 按优先级取 ①正文 JSON 的 `thought/reasoning/思考`
  等字段 → ②非 JSON 的正文原文 → ③推理模型的 `reasoning_content`。把"显式输出"排在
  "内部思考链"之前，是因为前者可控、后者是模型私有行为。
- **协议指令不硬编码进 PromptBuilder**：拼到 `directive` 里，因此随 `prompts/*.md`
  一同落盘，留痕与实际下发的内容一致（不会出现"盘上看不到、实际发了"的情况）。
- **`require_thought` 只校验 Action 步**：Thought 的语义是"行动前的推理"，
  收敛出结论的那一步本身是答案，不该被强制要求写思考。

## 三、验证

### 3.1 单元测试（11/11 通过，真实组件）

复用 `tests/test_function_calling_e2e.py` 的套路：本地 OpenAI 协议桩服务 + 真实
`LLMClient`（urllib 真实发包）+ 真实 `ToolRunner`（拉起 `tests/fake_mcp_server.py`
真实 MCP 子进程），不是 mock。

| 用例 | 断言 |
| --- | --- |
| `test_react_emits_thought_action_observation` | 三步齐全；第二次请求 messages 为 `[system, user, assistant, tool]`，assistant 的 content 就是 Thought |
| `test_react_step_limit_forces_text_only_final_round` | 步数用尽仍要工具 → `react_step_limit` 事件 + 收尾轮不带 tools |
| `test_react_requires_thought_warning` | 只在 Action 步告警，收敛步不告警 |
| `test_extract_thought_prefers_json_field_then_text` | 三级抽取优先级 |
| `test_react_final_recovers_json_from_thought_text` | 混排正文恢复 JSON，节点不判失败 |
| `test_recover_final_json_variants` | 代码块 / 括号平衡 / 字符串内花括号 / 坏片段 |
| `test_disabled_by_default_keeps_legacy_loop` | 关闭时无 `react_*` 事件、directive 不含协议、按 `tool_rounds` 口径 |
| `test_node_not_in_whitelist_falls_back` / `test_react_without_tools_degrades_to_single_round` | 白名单与工具可用性两道闸门 |
| `test_offline_provider_with_react_enabled` | 离线 Provider 不触网、不执行工具 |
| `test_legacy_path_unaffected_by_recover` | `recover_final_json` 不在非 ReAct 路径生效（改造前口径不变） |

### 3.2 离线端到端对照（两档一致）

`.workbuddy/verify_react.py`：offline 确定性 Provider 跑完整流水线，跑两遍对照
（输入 `examples/requirements/order_pricing_service.md`）。

| 指标 | A 档：ReAct 关闭 | B 档：ReAct 开启 |
| --- | --- | --- |
| verdict | pass | pass |
| steps / rollbacks / bugs | 11 / 2 / 5 | 11 / 2 / 5 |
| 节点迭代分布 | intake 1、solution 1、implement 2、review 2、test 2、optimize 1、acceptance 1、report 1 | 完全一致 |
| 交付代码文件数 | 3 | 3 |

两档 summary 关键字段比较结果：**True**（一致）。

### 3.3 真机验证（真实端点 + 真实 MCP 子进程）

`.workbuddy/verify_react_live.py`：用 `config/credentials.yaml` 的 Key 打真实端点，
工具是真实 MCP 子进程（echo / ping）。跑出 3 步轨迹：

```text
react_step #1  thought(18字) → echo(parameter=freight_rule) → 观察 94 字符
react_step #2  thought(58字，"回显成功，返回 {...}，说明工具链路正常") → ping() → 观察 63 字符
react_final #3 thought(553字，"ping 返回 {}…服务调用本身无报错") → JSON 结论
节点状态: pass    metrics: {"react_steps": 3}
```

第 2 步的思考明确引用了第 1 步的观察结果——**观察驱动下一步推理**，这是 ReAct 区别于
普通工具调用的核心特征，在真实模型上得到了确认。

### 3.4 全量回归

```text
Ran 384 tests in 85.575s
OK
```

（改造前基线 381，差值 3 即本次新增用例中被 `discover` 计入的部分。）

## 四、真机暴露并修复的缺陷：收敛步 JSON 解析失败

首次真机验证的结果是 `节点状态: fail，error='模型未返回可解析的 JSON（finish_reason=stop，
正文 872 字符）'`——有结论，解析不出来。

根因：ReAct 收敛步的自然形态是「先写观察/思考，再给 ```json 代码块」，而
`mas/llm.py:parse_json_block` 只处理"整段是 JSON"或"首个 `{` 到最后一个 `}`"。
真机正文里前有反引号包裹的 `{}` 干扰，这个子串夹着中文，必然解析失败。

修复：`recover_final_json()`（base.py，仅在 ReAct 收敛步调用）

1. 优先取 ```json / ``` 代码块内容；
2. 否则全局括号平衡扫描（正确处理字符串内的 `{}`），取首个可解析对象；
3. 存在未闭合坏片段时逐起点重试（起点数 ≤64、单起点窗口 ≤20000 字符，防 O(n²)）。

修复后同一真机脚本：`节点状态: pass`，`最终 JSON 顶层键: ['function_points', 'title']`。

**这个修复没有扩大到全局**：`test_legacy_path_unaffected_by_recover` 明确断言关闭 ReAct 时
同样的混排正文仍按改造前口径判失败，避免悄悄改变既有判定标准。

## 五、前端开关与默认开启（第二轮）

### 5.1 开关接入前端「模型配置」弹窗

照 `docs/22` 技能开关的既有范式实现，字段同名同义、无转换层：

| 前端控件 | HTTP（/api/react） | system.yaml |
| --- | --- | --- |
| `#cfg-react-enabled` | `enabled`（bool） | `runtime.react.enabled` |
| `#cfg-react-require-thought` | `require_thought`（bool） | `runtime.react.require_thought` |
| `#cfg-react-max-steps` | `max_steps`（int ≥1） | `runtime.react.max_steps` |

`nodes` 与 `tools_nodes` 只回显不写回（`nodes` 是列表、且需与 `llm.tools_nodes`
取交集，前端不做）。面板会**直接算出实际生效节点**并显示，避免"开了开关却没生效"的困惑。

改动：`frontend/server.py` 三个函数 + 2 处路由 + docstring；`index.html` 一个 field 块；
`app.js` CFG 映射 / 读取 / 联动 / 保存；`style.css` 一段样式。

### 5.2 默认开启

`config/system.yaml` 的 `runtime.react.enabled` 已置 **true**（代码层"未配置时"的默认值
仍为 false，与 `skills` / `ui_probe` / `context_compression` 的既有约定一致：代码默认关闭，
由配置决定开箱行为）。

**默认开启立刻暴露的第一件事**：`config/mcp.yaml` 是 `servers: []`，没有任何工具可下发，
因此当前默认状态下 ReAct 会退化为单轮——开关是开的，但没有实际作用。这是配置现状，
不是代码缺陷；要真正跑起来必须同时有 MCP 服务（或本地工具）且节点在 `tools_nodes` 内。

**默认开启对既有测试的影响（真实发生，已修）**：三个原本验证「改造前工具闭环协议」的用例
被分流到 ReAct 分支而失败（`tool_rounds` 变 `None`）：

```
FAIL: test_full_loop_real_mcp_and_wire_protocol          (test_function_calling_e2e)
FAIL: test_tool_rounds_exhausted_forces_text_only_final_round (test_function_calling_e2e)
FAIL: test_full_loop_skill_call_via_sandbox              (test_skill_tools)
```

处理方式是**在这两个文件的 setUp 里显式 `runtime.pop("react")`**（不是放宽断言）——
它们验证的目标是 ReAct 之前的协议，ReAct 自身的协议由 `tests/test_react_loop.py` 覆盖。

## 六、真实长任务验证：暴露的问题与修复

完整流水线在真实端点上一档要 30~40 分钟（A 档实测：10 个节点耗时 30+ 分钟仍未收尾），
两档对照不可承受；而 ReAct 的风险集中在 implement / optimize 这类重节点，因此改为
**聚焦验证**：复用 A 档已产出的真实上游产物，只重跑 implement 节点
（真实 DeveloperAgent、真实 Prompt、真实沙箱写文件、真实 MCP 工具、真实 ReAct 循环）。
工具用 `.workbuddy/mcp_code_tools.py`（`list_delivery_files` / `read_code_file` /
`check_symbol`），对编码任务真有用途，模型才有真实理由发起多步 Action。

### 6.1 修复前：节点 fail

```text
节点状态: fail   error='模型未返回可解析的 JSON（finish_reason=tool_calls，正文 213 字符）'
react_steps=3，末步发起 8 个 tool_calls，prompt_tokens=34243
```

3 步 ReAct 全部用于"调查现状"（累计 20 次 `read_code_file`），**步数用尽时模型仍在请求工具**。
收尾兜底本应触发，但它的判定沿用了改造前的 `not text.strip()`（正文为空）——
而 ReAct 每步都要求写 Thought，**正文必然非空（213 字符）→ 收尾轮永不触发**。

这是本次改造引入的高频失败路径：改造前模型常常正文全空（纯 tool_calls），
改造后协议要求先写思考，于是"正文空"这个判据在 ReAct 下几乎不可能成立。

**修复 1（代码，1 行条件）**：判定改为「没有可解析的结论」

```python
# 改前：if generation.tool_calls and not (generation.text or "").strip():
if generation is not None and generation.tool_calls and generation.data is None:
    …追加一次不带 tools 的收尾生成…
```

### 6.2 修复 1 之后：收尾轮触发了，但产出是空壳

```text
节点状态: fail   error='实现未产出任何文件'
末轮 finish_reason=stop、tool_calls=0、completion_tokens=257
```

收尾轮只吐了 257 个 token 的空壳 JSON。原因：上下文里已堆了 20 条工具观察
（prompt_tokens 30k+），仅"不带 tools"并不足以让模型输出完整交付物。

**修复 2（代码，一条指令）**：收尾轮追加显式收敛指令
（`REACT_CLOSING_DIRECTIVE`：立即停止探索、所有文件给完整内容、不得用占位符代替）。

### 6.3 修复后：节点 pass

```text
节点状态: pass   耗时 197.2s
summary: 编码实现（fix）：23 个文件、1323 行代码、功能点映射 7 个；导入自检通过
末轮 completion_tokens=23218（修复前 257）、react_steps=3
```

### 6.4 不改代码的结论：长任务节点默认不放开 ReAct

实测暴露的策略问题：3 步 ReAct 全被"探索"吃掉（20 次工具调用），真正产出全靠收尾轮，
整体耗时 197 秒且上下文膨胀到 30k+ tokens。**implement / optimize 这类"产物巨大、
需要一次性全量输出"的节点不适合开 ReAct**。

因此 `llm.tools_nodes` 保持 `["intake"]`（既有设计：下游节点保留 `response_format`
的 JSON 强约束），ReAct 的实际生效范围仍是 intake——需求澄清类节点产物小、
需要外部查证，才是 ReAct 的适用场景。本次验证把 implement 放开只是为了压出问题。

> 补充：`_generate_with_tools`（改造前路径）有**同源**的收尾判定（同样是"正文为空"），
> 但改造前模型不写 Thought、正文常全空，触发条件仍能成立；本次未改动它，
> 以免改变既有路径的行为口径。若后续 ReAct 之外的节点也出现同类失败，再按同一判据处理。

## 七、已知边界（未做 / 做不到）

- **offline Provider 无法演示 ReAct 轨迹**：`LLMClient._generate_offline` 忽略
  `tools` 与 `extra_messages`（docs/21 的既有设计），所以离线跑也退化成单轮。
  要看到完整轨迹必须用 `openai_compatible` + 真实 MCP 服务。
- **`tools` 与 `response_format` 互斥**（端点约束）：ReAct 步内没有 JSON 强约束，
  靠 Prompt 契约 + `recover_final_json` 兜底，不是靠协议保证。
- **未验证项**：真实长任务（implement 全量代码）下 ReAct 的耗时与收敛性——
  真机只跑了轻量节点（requirement_spec 类）。多步 ReAct 叠加长生成会显著增加
  网关超时风险，这是推断，不是实测结论。
- 步数用尽后的收尾轮复用了改造前 `tool_max_rounds` 用尽的兜底逻辑，未新增策略。
