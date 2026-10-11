# Agent 与 Skill 现状盘点、改进建议与验证方式

> 本文结论全部来自当前仓库文件的实际核对（核对命令与数据出处见文末附录），不含推断。
> 第 2 部分的技能优先级不是拍脑袋排的，而是由 `workspace/knowledge/metrics.jsonl` 的
> 11 次真实运行统计与失败节点的错误事件反推出来的。

---

## 0. 结论摘要

| 问题 | 结论 |
| --- | --- |
| 有没有给 agent 配 skill？ | **没有。** 系统只有「技能注册表」，没有「技能消费链路」 |
| 唯一存在的技能 | `config/skills/demo-delivery/SKILL.md`（手写示例），**未绑定任何 agent** |
| 触发条件是否生效？ | **完全未生效。** 全仓唯一被执行的技能动作是 `skill_distill`（沉淀），且由命令行显式触发 |
| 根本原因 | 缺一层绑定：没有任何代码把 `config/skills/*` 读进 Agent 运行时或 Prompt |

一句话：**目前是「能存技能、不能用技能」。**

---

## 1. 现状盘点

### 1.1 技能资产：只有 1 个

```
config/skills/
└── demo-delivery/
    ├── SKILL.md            # source: manual，含 smoke.command
    └── scripts/hello.py    # 冒烟脚本，退出码 0 表示通过
```

`config/skills/demo-delivery/SKILL.md` 实测元信息：

| 字段 | 值 |
| --- | --- |
| `name` / `version` / `source` | `demo-delivery` / `0.1.0` / `manual` |
| `trigger.keywords` | 示例、demo、交付 |
| `trigger.intents` | `delivery` |
| `trigger.requires_artifacts` | `requirement_spec` |
| `steps` | 6 条（对应 intake→report 的交付路径） |
| `smoke.command` | `python scripts/hello.py` |

已实测通过：

```bash
python -m mas skill list                    # 列出 1 个
python -m mas skill check demo-delivery     # parse → validate → smoke 全 OK
```

### 1.2 绑定关系：零绑定

5 个角色配置文件均**没有**任何 skill 相关字段：

| 文件 | 顶层键（实际存在的 11 个） | 是否含 skill 字段 |
| --- | --- | --- |
| `config/roles/product.yaml` | id / name / mission / responsibilities / inputs / outputs / permissions / prompt / perception / learning / policies | 否 |
| `config/roles/developer.yaml` | 同上 | 否 |
| `config/roles/reviewer.yaml` | 同上 | 否 |
| `config/roles/tester.yaml` | 同上 | 否 |
| `config/roles/reporter.yaml` | 同上 | 否 |

`config/flow.yaml` 的 8 个节点同样没有 skill 相关键（只有 role / inputs / outputs / transitions / gate / retry）。

### 1.3 谁实际引用了 `mas/skills.py`（全仓）

| 引用方 | 位置 | 用途 |
| --- | --- | --- |
| `mas/cli.py` | `cmd_skill` | 增删改查 + `check`（**命令行入口，不是运行时**） |
| `mas/leader.py:310` | `distill_skill()` | off-flow 沉淀，由 CLI 显式调用 |
| `mas/agents/reporter.py:117` | `_apply_skill_distill()` | 把沉淀结果**写盘** |
| `mas/providers/offline.py:1112` | `_skill_distill()` | 沉淀用的离线算子 |
| `mas/config.py:488` | `skills_dir` 属性 | 仅提供路径 |

**关键观察：没有任何一处是「读取技能内容去影响 Agent 行为」。**

### 1.4 缺失项（按严重程度）

| # | 缺失 | 证据 | 影响 |
| --- | --- | --- | --- |
| **M1** | 没有「技能 → Agent」绑定机制 | `mas/prompts.py` 的 `_user_prompt()` 只有 `# 经验注入` 段（182-183 行），无技能段；`BaseAgent.retrieve_experience()` 只查 `ExperienceLibrary` | 技能写了也不会被任何 agent 看到 |
| **M2** | 没有运行时触发判定 | 全仓无读取 `trigger` 的代码 | `trigger.keywords / intents / requires_artifacts` 是纯文档，不起任何作用 |
| **M3** | 离线 Provider 不会因技能改变行为 | `OfflineProvider.provide()` 按 `task_kind` 分发固定的 10 个 handler，不读 prompt 中的自定义段落 | 离线模式下注入技能是**装饰性的**，输出零变化 |
| **M4** | 没有调用留痕 | `events.jsonl` 无 `skill_invoked` 类事件；prompt 文件头只记 `injected=[经验id]`，无技能 id | 无法回答「这次运行用没用技能、用了哪个」 |
| **M5** | 本系统没有 Skill 工具调用 | 全链路是「LLM 结构化 JSON 输出 → 容错解析 → 确定性 Python 执行」，无 function calling | 不能按「Skill 工具调用」口径验证，要换成本项目自己的观测面 |

> M5 尤其重要：本项目与「模型主动调 Skill 工具」的形态不同，不存在 `tool_use` 记录。

---

## 2. 改进建议

### 2.0 优先级不是猜的：先看真实失败数据

`workspace/knowledge/metrics.jsonl` 共 11 次运行，**8 次未通过**。三次典型的失败运行：

| run_id | 步数 | 回退 | 缺陷数 | 结论 |
| --- | --- | --- | --- | --- |
| `20261011-003150-9343` | 9 | 5 | **0** | 未通过 |
| `20261011-004933-fc39` | 9 | 3 | **0** | 未通过 |
| `20261011-010835-13e0` | 15 | 9 | **0** | 未通过 |
| `20261011-050247-1610`（离线） | 7 | 0 | 0 | 通过 |

**关键发现：失败运行的 `bugs_total` 全是 0。** 也就是说，这些失败**不是**「交付代码有缺陷没修掉」，而是别的原因。逐条扒 `events.jsonl` 的 `node_error` 得到的真实分布：

| 失败原因 | 出现节点 | 性质 |
| --- | --- | --- |
| `TimeoutError: The read operation timed out` | implement / optimize / test | LLM 调用超时（基础设施层） |
| `AttributeError: 'str' object has no attribute 'get'` | test | **框架自身的确定性崩溃** |
| `模型未返回可解析的 JSON` | implement | 输出契约不健壮 |
| `本轮不具备代码审查的输入条件：上游仅提供 requirement_spec 与 solution_design…` | review | 上游失败导致的级联回退 |

那条 `AttributeError` 两次运行都复现，来源已定位到 `mas/agents/tester.py:125`：

```python
for case in parsed.get("cases", []) if case.get("status") != "passed"
```

模型返回的 `cases` 是**字符串列表**而不是 dict 列表，`case.get(...)` 当场崩溃。
根因是：**解析出 JSON 之后，没有对着交付契约做结构/类型校验就直接消费。**

这条数据直接决定了下面的优先级排序——排在最前的技能针对的就是这个崩溃，而不是泛泛的「写好代码」。

### 2.1 前置：先补绑定链路（M1~M4）

技能内容再好，没有绑定也进不了 Agent。三处最小改动（均为新增，不重构）：

1. `config/roles/<role>.yaml` 增加 `skills: [{id: <skill-id>, nodes: [...]}]`——沿用本项目「配置驱动」的既有惯例；
2. `mas/prompts.py` 在 `# 经验注入` 段之后增加同格式的 `# 技能注入` 段，并在文件头注释里加 `skills=[...]`；
3. `BaseAgent.build_prompt()` 按 role + node 选出命中技能，把 `steps` 正文拼进 prompt。

### 2.2 推荐的 4 个技能（按优先级）

排序依据：**有实测证据支持 × 下游影响面 × 能否写出可断言的输出**。

---

#### P1 · `contract-schema-guard`｜横切，首责 tester @ test，次责 developer @ implement

| 项 | 内容 |
| --- | --- |
| 适用场景 | 消费 LLM 结构化输出之前——任何「解析成功但结构不对」的情况 |
| 触发条件 | 所有 `kind=task` 节点在拿到 `generation.data` 后、写产物前；前置条件：契约已在 `contracts` 中声明 |
| 覆盖范围 | 输入：解析后的 JSON + 该节点的交付契约；输出：校验报告（写入 `result.metrics["schema_guard"]`） |
| 具体动作 | ①字段缺失/类型不符时**不抛异常**，改为结构化失败，给出字段路径（如 `cases[0]` 应为 dict 实为 str）；②可修复的做降级（跳过非法项并记录），不可修复的才判节点失败；③必须在 `node_error` 里给出可定位的字段路径而不是裸 AttributeError |
| 与现有能力的差异 | 现有只有 `parse_json_block` 的**语法级**容错，没有**结构级/类型级**校验；本技能补的正是这一层 |
| **为什么排第一** | 这是两次失败运行里唯一重复出现、且已定位到具体代码行的确定性崩溃；其他原因（超时）属基础设施，用技能解决收益有限 |

**可验证的输出特征**：`events.jsonl` 中出现带 `field_path` 的结构化失败；`raise AttributeError` 不再出现。

---

#### P2 · `spec-scope-guard`｜产品 Agent @ intake

| 项 | 内容 |
| --- | --- |
| 适用场景 | 需求规模过大，导致下游 implement 单次生成超时或产出非法 JSON |
| 触发条件 | `role=product && node=intake`，发布 `requirement_spec` 前 |
| 覆盖范围 | 输入：`requirement_source`；输出：给 `requirement_spec` 增加 `scope_guard` 字段 |
| 具体动作 | ①统计功能点数与估算代码量；②超过阈值时在 `open_questions` 里显式标注「建议分批交付」并给出分批建议；③每条验收标准必须能翻译成可求值表达式，不可测项单独列出 |
| 为什么要这个 | 实测链路很直白：`20261011-010835-13e0` 拆出 **13 个功能点** → implement 生成 11 文件 / 520 行 → 先是 `模型未返回可解析的 JSON`，随后 `test` 崩溃 → 15 步 9 回退。需求侧的粒度控制能直接削掉这条链路 |
| 与现有能力的差异 | 现有 `clarify_before_implement` 只是一句「先澄清再实现」的提醒，不做规模量化，也不产出分批建议 |

**可验证的输出特征**：`requirement_spec.scope_guard` 含 `function_point_count / estimated_loc / batching_suggestion / unmeasurable_criteria[]`。

---

#### P3 · `graceful-input-degradation`｜横切，先把 reviewer 的既有做法固化成规范

| 项 | 内容 |
| --- | --- |
| 适用场景 | 上游节点失败、所需黑板产物缺失时，下游如何不失控地降级 |
| 触发条件 | 任意 `kind=task` 节点，`perception.missing` 非空 |
| 覆盖范围 | 输入：`perception.missing`；输出：降级说明产物 + 明确的 `node_end` 摘要 |
| 具体动作 | ①缺失输入时输出「本轮不具备 X 的输入条件：缺 A、B」并**说明缺什么、等谁产出**；②不臆造内容填充；③给出是否需要回退的建议而不是无条件判失败 |
| 为什么排第三 | `reviewer` 当前**已经这么做了**（实测 node_end：「本轮不具备代码审查的输入条件：上游仅提供 requirement_spec 与 solution_design…」），但这是一处孤例，tester / developer 遇到同类情况是直接崩（`AttributeError`）。本技能本质是**把已验证的正确做法推广为规范**，风险最低 |
| 与现有能力的差异 | 不是新增能力，是把散在一处的正确行为变成可复用、可断言的规范 |

**可验证的输出特征**：`perception.missing` 非空时，`node_end` 摘要必含「输入条件」字样与缺失项列表；不再出现无摘要的空 `node_end`（实测中 implement 的 `node_end` summary 为空串）。

---

#### P4 · `evidence-cited-review`｜Reviewer Agent @ review

| 项 | 内容 |
| --- | --- |
| 适用场景 | 审查结论必须可追溯到需求：每条阻塞问题要能对上具体功能点 |
| 触发条件 | `role=reviewer && node=review`；前置产物 `code_manifest` + `requirement_spec` |
| 覆盖范围 | 输入：静态分析 finding + `requirement_spec`；输出：`review_report` 的 blocking 项增加溯源字段 |
| 具体动作 | 每条 blocking finding 必须带：①`rule`；②`file:line`；③**违反的需求功能点 id**；④修复建议。缺任一项降级为 non-blocking |
| 与现有能力的差异 | `mas/sandbox.py` 的静态分析已给出 rule / file / line 三要素，**缺的是与需求功能点的溯源**；本技能只补这一段，不重复做静态分析 |
| 为什么排第四 | 有真实价值（降低返工），但当前失败数据里没有证据表明它是瓶颈，故列最后 |

**可验证的输出特征**：`review_report.blocking_items[]` 每项含 `fp_id`；无溯源的 finding 落在 `non_blocking` 而非 `blocking`。

---

### 2.3 明确不推荐的「伪技能」

| 候选 | 不推荐原因 |
| --- | --- |
| 「写代码要加注释 / 文档字符串」 | 已是静态规则 `missing_function_docstring` + 经验 `avoid_*`，重复 |
| 「注意边界值」 | 已有 `boundary_tri_state` / `guard_*__boundary` 经验，离线 Provider 已按此生成三类用例 |
| 「报告要引用证据」 | reporter 已有 `_evidence_index()` 产出证据索引，属既有能力 |
| 「先写回归用例再修缺陷」 | **当前无证据支持**：失败运行的 `bugs_total` 全为 0，尚未出现过「交付代码有缺陷没修掉」的场景。等真实 bug 闭环样本出现再做 |
| 「用 MCP 查外部资料」 | MCP 目前只有配置与 `check` 握手，**没有任何 Agent 会去调用它**，先接消费链路再谈技能 |

---

## 3. 验证方式

### 3.1 先说清楚：本项目没有 Skill 工具调用

全链路形态是「LLM 结构化 JSON 输出 → 容错解析 → 确定性 Python 执行」，**不使用 function calling**，
因此 transcript 里不存在 `Skill` 工具调用块。要验证技能是否被使用，只能看下面的证据面，
且**必须同时命中两类以上**才算数。

### 3.2 现有可观测证据面（均已实测存在）

以 `workspace/runs/<run_id>/` 为例：

| 观测面 | 路径 | 能看到什么 | 当前局限 |
| --- | --- | --- | --- |
| **LLM trace** | `llm_traces.jsonl` | 每次调用的 `agent / node / task_kind / provider / tokens / reasoning / output / error` | 只证明「调了 LLM」，不证明「用了哪个技能」 |
| **Prompt 原文** | `prompts/<node>#<iter>-<role>-<hhmmss>.md` | SYSTEM/USER 全文；文件头记录 `injected=[经验id]` | **目前没有技能段、头里也没有技能 id** |
| **事件流** | `events.jsonl` | `node_start / node_end / artifact_published / rollback / node_error / gate_evaluated …` | **目前没有 `skill_invoked` 事件** |
| **黑板产物** | `artifacts/<name>/vN.json` 与 `latest.json` | 每个版本的结构化产物全文 | 只能看到结果，看不出成因 |

实测存在的唯一一条技能相关 trace（来自显式执行 `skill add --from-run`）：

```json
{"agent":"reporter","node":"skill_distill","task_kind":"skill_distill",
 "provider":"offline","model":"offline-deterministic","tokens":822,"latency_ms":16}
```

这条**不是触发条件自动产生的**——正好印证 M2。

### 3.3 判定「agent 真的用了 skill」的三条标准

三条必须同时成立，缺一条只能算「疑似」：

1. **注入可见**——`prompts/<node>#<iter>-*.md` 里出现该技能的标题或步骤原文（证明进了上下文）；
2. **输出可辨**——产物里出现只有该技能才会产出的字段（如 `scope_guard`、`schema_guard`），而不是仅仅「文风变好」；
3. **链路可溯**——`events.jsonl` 里有一条 `skill_invoked`，记录「谁、哪个节点、哪个技能、第几轮、因何触发」。

反过来，**描述与触发条件是否匹配**这样判：取发生时刻的上下文（该节点的 role / node / 已就绪产物），
逐条比对技能 front-matter 里的 `trigger.keywords`、`trigger.intents`、`trigger.requires_artifacts`，
命中项要能在 `skill_invoked` 事件的 `trigger_match` 字段里看到，而不是靠事后推断。

### 3.4 让验证变容易的四条写法建议

1. **给技能加机器可读的审计事件**（治 M4）
   在 `BaseAgent.act()` 命中技能后发一条与 `artifact_published` 同构的事件：

   ```python
   payload={"skill_id": sid, "version": ver, "trigger_match": hit, "node": node, "iteration": it}
   ```

   之后判定只需一条命令：

   ```bash
   python -c "import json;[print(json.loads(l)['payload']) for l in open('workspace/runs/<run_id>/events.jsonl',encoding='utf-8') if 'skill_invoked' in l]"
   ```

2. **让 prompt 文件头同时记录技能 id**
   仿照现有 `injected=[...]`，增加 `skills=[...]`，这样 `head -1 prompts/*.md` 就能直接看出本次带了哪些技能。

3. **每个技能必须声明「输出签名」而不是只有描述**
   即「本技能生效后，哪个产物会多出哪个字段、结构是什么」，写进 SKILL.md 的 `checkpoints`。
   这样验收是断言，不是人工读 prose——上面 4 个建议各自指定了一个输出字段，就是这个目的。

4. **离线 Provider 要能消费技能，否则注入是装饰性的**（治 M3）
   沿用已成熟的 `action_key` 思路：让技能也声明 `action_key`，
   把 `OfflineProvider._experience_actions()` 扩展成同时收集技能动作，handler 按 key 改变行为。
   这样离线模式也能端到端验证「技能导致了行为差异」。

### 3.5 改完之后的一键自检

```bash
# 1) 这次运行带了哪些技能
grep -h "^<!-- run=" workspace/runs/<run_id>/prompts/*.md

# 2) 有没有 skill_invoked 事件（当前必然为 0）
grep -c "skill_invoked" workspace/runs/<run_id>/events.jsonl

# 3) 产物里有没有该技能承诺的字段（当前必然 False）
python -c "import json;d=json.load(open('workspace/runs/<run_id>/artifacts/requirement_spec/latest.json',encoding='utf-8'));print('scope_guard' in d)"

# 4) 崩溃型错误有没有消失
grep -c "AttributeError" workspace/runs/<run_id>/events.jsonl
```

> 第 2、3 条在当前仓库必然返回 `0` / `False`，第 4 条在两次历史失败运行里必然 ≥1
> ——这三条正是「零绑定」现状的可复现证明。

---

## 附录 A：核对命令

```bash
# 技能资产
ls -R config/skills
python -m mas skill list
python -m mas skill check demo-delivery

# 角色配置是否含 skill 字段
grep -n "skill" config/roles/*.yaml      # 无输出
grep -n "^[a-z_]*:" config/roles/product.yaml   # 11 个顶层键，无 skills

# flow.yaml 是否含 skill 字段
grep -n "skill" config/flow.yaml         # 无输出

# 谁引用了 skills 模块
grep -rn "skills" mas/ --include=*.py    # 仅 cli / leader / reporter / offline / config

# Prompt 组装有没有技能段
grep -n "技能\|skill" mas/prompts.py     # 无输出
```

## 附录 B：失败模式数据来源

```bash
# 运行级统计（本文 2.0 表格出处）
python -c "import json;[print(json.loads(l)) for l in open('workspace/knowledge/metrics.jsonl',encoding='utf-8')]"

# 节点级错误（本文 TimeoutError / AttributeError 出处）
python -c "
import json
p='workspace/runs/20261011-003150-9343/events.jsonl'
for l in open(p,encoding='utf-8'):
    e=json.loads(l)
    if e['type']=='node_error': print(e['node'], e['payload'])
"

# 崩溃代码位置
sed -n '125p' mas/agents/tester.py
```

## 附：本项目的 LLM 交互事实（避免按错误口径验证）

- 无 function calling / tool_use；
- 交互形态：交付契约 JSON 结构化输出 → `parse_json_block` 容错解析 → 确定性 Python 工具执行（沙箱写文件 / 子进程 / AST 静态分析 / 缺陷跟踪）；
- 因此「Skill 工具调用」在本项目中没有对应物，正确证据面是 **prompt 段落 + 事件 + 产物字段**三者交叉。
