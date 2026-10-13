# 多 Agent 任务协作系统（multi_agent）——代码实读分析与简历提炼

> 本文全部结论基于对 `G:\mycv\cragent\multi_agent` 源码的逐文件实读（2026-10-13），
> 关键量化数据均来自真实执行验证（两轮离线运行 + 45 个单元测试），未做夸大。
> 无法从代码或实测确认的指标统一以 **【待补充】** 占位。

---

## 〇、结论速览

| 维度 | 结论 |
| --- | --- |
| 项目定位 | 配置驱动（Configuration-Driven）的多 Agent 软件交付流水线：Leader 统一调度 5 个角色 Agent，全自动完成「需求评估 → 技术方案 → 编码实现 → 代码审查 → 业务测试 → 缺陷修复 → 验收门禁 → 验收报告」 |
| 技术栈 | Python 3 纯标准库 + PyYAML（**唯一第三方依赖**）；AST 静态分析、子进程沙箱、JSONL 事件日志、http.server 前端、 unittest 测试 |
| 规模（实读） | 核心 `mas/` 8,267 行 / 27 个模块；前端 493 行；测试 8 个文件 791 行（45 个用例，实测 52.8s 全过）；配置 659 行 YAML |
| 架构一句话 | 「YAML 状态机（flow.yaml）+ 版本化黑板（blackboard）+ 消息总线（bus）+ 事件日志（events.jsonl）+ 三级经验库（experience）」五基础设施支撑一个同步调度内核 |
| 最有区分度的设计 | ① 门禁表达式用 **AST 白名单求值器**替代 `eval`/模型自评；② 经验库给每条经验挂 **结构化 `action_key`**，被确定性执行体真实消费，使「自学习」产生**可测量、可证伪**的行为变化（实测二轮 11→7 步、5→0 缺陷）；③ 离线确定性 Provider 用 **`impl`/`bug_impl` 成对算子 + 首轮真实注入缺陷**，让 bug→修复→回归闭环可重复演示 |
| 实测关键指标 | 二轮运行：步数 11→7（−36%）、缺陷 5→0、审查驳回 1→0、耗时 3.98s→1.0s（−75%）；单轮生成 74 条真实可执行用例（功能 31/异常 26/边界 17），三类覆盖率均 100% |
| 已知短板（诚实面） | 真实 LLM（openai_compatible）端到端稳定性不足（metrics.jsonl 中多次运行 9~16 步、回退 3~9 次未通过验收）；经验反馈 `reinforce()` 已实现但无调用点；检索为关键词/标签打分，无向量语义召回 |

---

## 一、项目概况与技术栈

### 1.1 项目概况

- **形态**：CLI（`python -m mas run/demo/report/experience/skill/mcp/config`）+ 纯标准库 Web 面板（`frontend/server.py`）。
- **双 Provider 设计**：
  - `offline`：内置确定性 Provider（`mas/providers/offline.py`，1,161 行），无 API Key 也能端到端跑通全流程，作为回归基线与演示载体；
  - `openai_compatible`：走标准 `/chat/completions`（`urllib` 直实现，无 SDK），处理任意需求；默认配置指向中南大学 HPC 端点（`api.chat.csu.edu.cn/v1`，DeepSeek），并捕获推理模型思考链（`reasoning_content`）。
- **开发周期**：git 历史显示 2026-09-23 首次提交 → 2026-10-11 接入 MCP 与 Skill 系统（3 个提交）。
- **需求域**：以「订单金额计算与校验服务」为标杆需求（7 个功能点：金额解析/会员折扣/运费/优惠券校验/分摊/总价/订单校验），离线 Provider 通过规则解析结构化 Markdown 需求文档（`### FP-xx` 小节）驱动全流程。

### 1.2 技术栈清单

| 层 | 技术选型 | 说明 |
| --- | --- | --- |
| 语言/运行时 | Python 3（CPython） | 沙箱内以子进程执行交付代码与测试 |
| 唯一三方依赖 | PyYAML | 全部配置（系统/流程/协作/学习/角色/Profile/Skill/MCP）均为 YAML |
| 调度内核 | 同步 while 状态机（`leader.py`） | 无异步、无多进程，单次运行单线程推进 |
| 代码分析 | `ast` 标准库 | 12 条静态规则 + 白名单表达式求值 |
| 执行沙箱 | `subprocess` + 路径越界校验 + 超时 | 默认 180s，marker 协议回传结构化结果 |
| 持久化 | JSONL（events/messages/metrics/experience/llm_traces）+ 版本化 JSON 产物 | 追加写，崩溃可恢复、可重放 |
| 前端 | `http.server`（ThreadingHTTPServer） | 10 个 API（7 GET + 3 POST），零额外依赖 |
| 测试 | `unittest` | 45 个用例覆盖配置/表达式/经验库/Skill/MCP/端到端 |
| LLM 接入 | `urllib` 手写 OpenAI 兼容客户端 | 含凭据三通道解析、脱敏、`/models` 连通性探测 |

---

## 二、整体架构

### 2.1 分层视图

```
┌──────────────────────────────────────────────────────────────────────┐
│ 配置层  config/（system/flow/collaboration/learning/roles/profiles）  │
│         ↓ load_config：深合并 + Profile 覆盖 + skip 节点重接 + 17 类加载期校验 │
├──────────────────────────────────────────────────────────────────────┤
│ 调度层  Leader（leader.py）                                           │
│   · flow.yaml 驱动的节点状态机（task/gate 两种节点）                    │
│   · pass/fail 分支、回退（次数上限+升级）、协商中介、L1 经验即时提炼      │
├──────────────────────────────────────────────────────────────────────┤
│ Agent 层  BaseAgent 骨架（base.py）+ 5 角色                            │
│   product → developer(solution/implement/optimize) → reviewer          │
│   → tester → reporter                                                  │
│   统一骨架：感知 → 经验检索 → Prompt 组装 → 生成 → 落地(写文件/执行)     │
│            → 发布产物 → 声明广播                                        │
├──────────────────────────────────────────────────────────────────────┤
│ 基础设施层                                                            │
│   Blackboard 版本化产物(sha256)  MessageBus 三类消息   EventLog 事件日志 │
│   Sandbox 执行+AST 分析         ExperienceLibrary L1/L2/L3 经验库       │
│   PromptBuilder 五段式 Prompt   LLMClient 双 Provider                   │
├──────────────────────────────────────────────────────────────────────┤
│ 扩展层  Skill 系统（SKILL.md 规范/校验/冒烟/运行沉淀）  MCP 最小客户端     │
│ 表现层  frontend（纯标准库面板 + 后台触发运行）                          │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流（一次运行的生命周期）

1. **intake**（product）：需求文档/文字 → `requirement_spec`（功能点 FP-xx、验收标准 AC-xx、待澄清问题 Q-x、歧义标记）；
2. **solution**（developer）：技术选型多候选打分 + 方案评估 + 功能点覆盖映射 → `solution_design`；
3. **implement**（developer）：生成真实可运行代码落沙箱 + 导入自检 → `code_manifest`；
4. **review**（reviewer）：真实 AST 静态分析（12 规则）+ 需求一致性（功能点→函数映射）→ `review_report`，阻塞项非零即 reject；
5. **test**（tester）：依据需求契约独立生成功能/异常/边界用例（不读研发代码），沙箱真实执行 unittest → `test_report` + `bug_reports`；发现歧义经 Leader 中介向研发发起请求-应答协商；
6. **optimize**（developer，回退入口）：修复 open 缺陷并做定向回归（`--only` 单条用例）→ `fix_summary`/`fix_test_report`；
7. **acceptance**（gate）：8 条表达式在黑板快照上求值 + reviewer 签署意见，双重判定 → `acceptance_gate`；
8. **report**（reporter）：汇总 7 类证据生成验收报告 + 证据索引 + 自学习效果报告；
9. 收尾：运行级经验提炼 → 晋级（L1→L2→L3）→ 指标落 `metrics.jsonl` → `run_summary.json` + `timeline.md`。

---

## 三、核心模块职责

| 模块 | 行数 | 职责 | 关键实现 |
| --- | --- | --- | --- |
| `mas/leader.py` | 612 | 统一调度器 | 状态机主循环；门禁求值与签署；回退判定/计数/升级；协商中介闭包；L1 增量提炼 + 运行末提炼；`run_summary`/`timeline` 产出 |
| `mas/agents/base.py` | 219 | Agent 执行骨架 | 感知（黑板 require + bus digest）→ 经验检索 → Prompt 组装 → LLM 生成 → apply → 发布/广播；异常一律降级为节点失败；LLM 调用全量留痕（`llm_traces.jsonl`，含思考链，截断 12,000 字符） |
| `mas/agents/developer.py` | 343 | 方案设计/编码/修复 | 模式判定 first/rework/fix；方案功能点覆盖强校验；写入沙箱 + 导入自检；缺陷修复定向回归（`--only`）；协商应答（`consult`） |
| `mas/agents/tester.py` | 302 | 独立测试 | 契约驱动三类用例；沙箱真实执行 + 结果 marker 解析；三类覆盖率统计；缺陷全生命周期跟踪（open/fixed/复测确认）；歧义协商 |
| `mas/agents/reviewer.py` | 112 | 审查/评审/签署 | 审查上下文注入真实 AST 分析结果；verdict=pass/reject；签署意见（门禁综合判定） |
| `mas/agents/product.py` | 97 | 需求评估 | 功能点拆解；结构完备性策略（每功能点必须有样例） |
| `mas/agents/reporter.py` | 239 | 报告/沉淀 | 证据统计（事件+产物+测试运行）；验收报告 + 证据索引 + 全局归档；成功运行→Skill 沉淀 |
| `mas/blackboard.py` | 142 | 版本化共享内存 | 每产物保留全部版本（v1/v2…+latest.json+sha256 摘要）；`require()` 暴露缺失；`brief()` 受控截断（900 字符/产物）供 Prompt 感知 |
| `mas/bus.py` | 157 | 消息总线 | direct/broadcast/request-response（correlation_id 关联）；全部落 `messages.jsonl`；`digest(role)` 生成角色相关通讯摘要（10 条/1600 字符） |
| `mas/events.py` | 175 | 事件日志 | seq 递增 + severity + refs；订阅回调（订阅者异常隔离）；`timeline()`/`stats()` 供复盘 |
| `mas/experience.py` | 686 | 自学习闭环 | 三级经验库；加权打分检索（0.45/0.25/0.15/0.15）；分层+全局 token 双预算；同源合并、命中晋级；`improvement_summary` 收益对比；自学习效果报告 |
| `mas/prompts.py` | 216 | Prompt 工程 | 五段式组装（角色系统提示/任务指令/感知段/经验注入段/交付契约）；每次 Prompt 落盘（含注入经验 ID 与 token 估算） |
| `mas/llm.py` | 169 | 模型接入 | offline/openai_compatible 双 Provider；JSON 容错解析（剥代码围栏/截取首对象）；思考链捕获 |
| `mas/providers/offline.py` | 1,161 | 确定性 Provider | 规则解析需求；7 算子库生成真实代码与用例；首轮缺陷注入与文档字符串剥离；经验 `action_key` 消费；阈值三态用例派生 |
| `mas/providers/ops.py` | 512 | 算子契约库 | 每算子 `impl`（正确实现）/`bug_impl`（缺陷实现）/`cases`（真值契约）三元组；测试依据契约构造，不读实现 |
| `mas/expr.py` | 246 | 安全表达式 | AST 白名单求值器（13 类节点 + 14 个内置函数）；字段缺失返回可读原因；配置加载期预校验 |
| `mas/sandbox.py` | 377 | 执行沙箱 + 静态分析 | 路径越界拒绝；subprocess 超时/编码控制；marker JSON 提取（含多行累积容错）；12 条 AST 静态规则 |
| `mas/config.py` | 740 | 配置引擎 | 深合并（dict 递归/list 覆盖）；Profile 覆盖；skip 节点删除 + 流转边自动重接；17 类加载期校验；凭据三通道解析与脱敏 |
| `mas/skills.py` | 542 | Skill 系统 | SKILL.md front-matter 规范（6 必填字段）；validate + smoke 冒烟；成功运行→技能沉淀（环境绑定信息参数化剔除） |
| `mas/mcp.py` | 558 | MCP 最小客户端 | stdio（子进程 + 读线程 + 队列）与 SSE（endpoint 握手）双传输；initialize/tools/list/tools/call；`${VAR}` 环境变量展开（未设置即报错）；五阶段健康检查 |
| `mas/cli.py` | 497 | 命令行 | run/demo/report/experience/skill/mcp/config 子命令；`--api-key` 等运行时凭注注入 |
| `frontend/server.py` | 493 | Web 面板 | 10 个只读/写 API；后台线程触发运行（预分配 run_id + 空事件文件立即可见）；中断运行检测（600s 无新事件判定）；凭据管理（脱敏回显 + `/models` 探测） |

---

## 四、关键算法与设计亮点

### 4.1 配置驱动状态机：流程拓扑零代码变更

- **深合并语义明确**（`deep_merge`）：dict 递归合并、list/标量整体覆盖——Profile 既能改单字段（`review.max_attempts`），也能整体替换门禁清单（`gate.criteria`）。
- **裁剪节点自动重接流转边**（`_apply_skips`）：`skip: true` 节点删除后，扫描所有指向它的 transitions，按 `pass → success → default → fail` 顺序取被裁节点的后继接上；找不到后继直接抛配置错误，不留悬空边；同步清理引用已裁节点的回退策略。
- **加载期 17 类结构校验**（`_validate`）：入口存在、角色已定义、流转目标可达、task 必须声明 outputs、gate 必须声明 criteria、禁止自环、门禁表达式语法+白名单、回退引用合法、广播/感知目标合法、经验层级合法、provider 合法——全部问题一次性汇总抛出。
- **效果**：`strict` Profile 插入「技术方案评审」节点（8→9 节点、门禁换入方案评审项）；`lean` Profile 裁剪「代码审查」节点（8→7 节点、门禁 8→3 条、注入预算 1000→600 token）。调度代码零改动。

### 4.2 门禁判定：AST 白名单求值替代 `eval` 与模型自评

- 门禁（`kind: gate`）= 8 条表达式在黑板快照上求值 + reviewer 签署意见，双重判定（`_run_gate`）。
- 求值器（`expr.py`）基于 `ast.NodeVisitor`：仅放行 Constant/Name/Attribute/Subscript/BinOp/UnaryOp/BoolOp/Compare/IfExp/List/Tuple/Set/Dict/Call（Call 限定 14 个内置函数），`generic_visit` 兜底拒绝其余一切语法——导入、赋值、lambda、推导式全部被禁，配置文件无法成为代码执行入口。
- 字段按 `artifact.字段.子字段` 点路径取值，缺失时转成 `ExprResult(ok=False, error="字段缺失: xxx")`，门禁给出可读失败原因而非崩溃；配置加载期先跑一遍语法+白名单预校验（`check_config_expressions`）。

### 4.3 三级经验库：让「经验注入」可测量、可证伪

```
events.jsonl --规则化提炼--> L1/L2/L3 经验库 --加权检索+token 双预算--> Prompt 注入
      ^                                                              |
      └────────────────── 同源合并(+0.1)/命中晋级(+0.05) <────────────┘
```

- **五类提炼规则**（`extract_from_events`）：审查拦截→提交前自检；缺陷模式→实现守护；验收未通过→前置校验；歧义协商→编码前澄清；成功路径/效率→选型复用。边界类缺陷额外派生「阈值三态」经验。
- **检索打分**：`0.45×相关性(角色 0.5+标签 0.3+关键词 0.2) + 0.25×置信度 + 0.15×使用频次 + 0.15×新鲜度(e^(−days/30))`，再乘层级权重（L1 1.5 / L2 1.25 / L3 1.0）。
- **token 双预算**：分层预算（L3 420 / L2 360 / L1 260）+ 全局 1,000；超预算条目不静默丢弃，进 `skipped` 列表注明原因（置信度不足/非本次运行/超条数/超预算），可审计「为什么没注入」。
- **结构化动作标识 `action_key`**（区分度核心）：经验除自然语言外带机器可读动作键（`avoid_missing_function_docstring`、`guard_calc_shipping__boundary`、`boundary_tri_state`），离线 Provider 生成前读取注入集合——命中 `guard_*` 的功能点不再注入缺陷、命中 `avoid_*_docstring` 首轮即补文档字符串。**经验直接改变生成行为，而非靠模型自觉**。
- **闭环量化**：每轮指标（步数/回退/驳回/缺陷/耗时/注入数）落 `metrics.jsonl`，`improvement_summary()` 对比最近两轮差值，`learning_report.md` 输出「N 项指标下降，闭环是否生效」的机械结论。

### 4.4 多 Agent 协作：黑板 + 总线 + 协商，零直接耦合

- 5 个角色 Agent 互不直接调用：产物走**版本化黑板**（谁在第几轮改了什么可追溯），状态走**消息总线**（12 类事件按广播矩阵分发）。
- **感知矩阵**（`collaboration.perception`）：每个角色只读自己需要的上游产物；缺失项显式写入 Prompt「请勿臆造，按缺失处理」。
- **请求-应答协商**：测试发现需求歧义 → Leader 中介（`consult` 闭包，`correlation_id` 关联请求与应答）→ 研发 `consult()` 应答；未闭环歧义作为门禁项（`test_report.negotiation.open == 0`）直接阻塞验收。

### 4.5 错误处理：分级回退 + 升级 + 全局兜底

| 机制 | 实现 |
| --- | --- |
| per-node 回退 | review→implement×3、test→optimize×3、acceptance→optimize×2，各自带中文回退原因注入下一轮指令 |
| 全局兜底 | 全局回退上限 8、最大步数 40（`max_steps`），双保险防死循环 |
| 升级策略 | 回退耗尽走 `fail_run_and_report`：仍出具带 `forced: true` 标记的不通过验收报告，而非静默崩溃 |
| 异常隔离 | Agent 任何异常统一降级为节点失败 + `node_error` 事件；事件订阅者异常被吞掉不影响主流程；LLM 留痕失败不影响主流程 |
| 状态归一化 | `status_aliases` 把模型返回的 pass/success/ok/approved/done 等别名归一到 pass/fail 分支 |
| 沙箱防护 | 路径越界拒绝（`resolve()`）、180s 超时（exit 124 + 可读原因）、`PYTHONIOENCODING=utf-8` 防 Windows 编码崩 |

### 4.6 性能与成本控制（上下文工程）

- **感知按需读取**（不全量塞黑板）+ **产物摘要受控截断**（900 字符/产物并显式标注「截断，共 N 字符」）+ **消息摘要**（角色相关 10 条/1600 字符）+ **经验双层 token 预算**——四道防线防 Prompt 膨胀，同时用「显式标注截断/缺失」防模型臆造补齐。
- token 估算用启发式（中文 1 字 1 token、英文 3.2 字符 1 token），**刻意不引入 tiktoken**——零依赖取舍，精度让位于可部署性。
- 离线 Provider 端到端一轮 ~1~4s（实测 3.98s→1.0s），可作 CI 回归基线。

---

## 五、具有技术深度与区分度的实现细节

1. **`impl`/`bug_impl` 成对算子 + 首轮真实注入缺陷**：`ops.py` 每个算子同时注册正确实现与缺陷实现（如 `calc_shipping` 缺陷版把「不足 500g 按 500g 向上取整」改成 `int()` 截断）；首轮实现按配置注入缺陷（`inject_first_pass_defect`）+ 剥离文档字符串，从而在**无需真实 LLM** 的情况下真实演示「测试发现 → 缺陷单 → 修复 → 定向回归 → 复测闭环」与「审查驳回 → 返工」，且每轮结果确定可复现。
2. **阈值三态用例派生**：测试 Agent 从算子契约摘要中正则提取阈值（首重 1kg），按 `t / t−ε / t+ε` 三态自动派生边界用例，期望值由 oracle（`impl` 命名空间执行）计算——边界用例不手写、不拍脑袋。
3. **marker 协议回传结构化结果**：测试文件内置 `<<<MASS_RESULT>>>` 标记输出 JSON；沙箱 `_extract_marker` 先试单行 JSON，失败则逐行累积重试，兼容多行 JSON；`--only 名称子串` 支持定向回归（修复时只跑缺陷对应用例）。
4. **经验的行为闭环设计**：注入的经验 ID 记入 `run_state`（injected_total/tiers），Prompt 落盘含注入清单，Provider 消费 `action_key`——「注入→行为变化→指标变化」全链路留痕，自学习效果可证伪（二轮 11→7 步、5→0 缺陷）。
5. **凭据三通道 + 脱敏 + 连通性探测**：`credentials.yaml（gitignore）< 环境变量 < 运行时注入（CLI/前端表单）`，`mask_secret` 脱敏回显，前端 `POST /api/llm/test` 探测 BASE_URL `/models` 验证 Key 可用性——密钥不落码、不进日志。
6. **成功运行一键沉淀为 Skill**：`load_run_facts` 只保留最终通过路径（同节点取最后迭代），把 run_dir/run_id 等环境绑定信息参数化为 `{{run_dir}}` 占位符，剔除试错细节；产物经 front-matter 6 必填字段校验 + smoke 冒烟后才算装载成功。
7. **MCP 零 SDK 最小客户端**：不引 MCP SDK，手写 stdio（子进程 + 守护读线程 + 队列 + 优雅终止降级 kill）与 SSE（GET 拿 endpoint 事件 → POST → 事件流读响应）双传输；`${VAR}` 未设置即报错并指名变量，避免静默空串；`mcp check` 五阶段（parse→validate→connect→handshake→smoke）失败即给修复建议。
8. **前端后台触发运行**：`POST /api/run` 在后台线程启动流水线，预分配 run_id 并预创建空 `events.jsonl`——运行在启动瞬间即以「进行中」状态可见；600s 无新事件且无 summary 判定为中断，避免僵尸「进行中」记录。
9. **LLM 思考链留痕**：`llm_traces.jsonl` 记录 provider/model/延迟/tokens/思考链/原始输出（各截断 12,000 字符），推理模型（GLM `reasoning_content` 等）的思考过程可复盘——对调试「长 JSON 生成被思考链挤占」类问题直接有用。

---

## 六、量化指标（实测 + 占位）

### 6.1 实测数据（2026-10-13 本机复现）

| 指标 | 首轮运行 | 第二轮运行 | 变化 |
| --- | --- | --- | --- |
| 状态机步数 | 11 | 7 | **−36%** |
| 缺陷总数（闭环） | 5（5/5） | 0 | **−100%** |
| 审查驳回次数 | 1 | 0 | **−100%** |
| 回退次数 | 2 | 0 | **−100%** |
| 端到端耗时 | 3.98s | 1.00s | **−75%** |
| 注入历史经验 | 8 条 | 20 条 | +150% |

其他实测：单轮生成 **74 条真实可执行 unittest 用例**（功能 31 / 异常 26 / 边界 17），功能/异常/边界三类覆盖率均 **100%**；**45 个单元测试全部通过**（52.8s）；配置加载期校验覆盖 **17 类**结构错误；静态分析 **12 条**规则；广播矩阵 **12 类**事件；门禁表达式 **8 条**（lean 3 条 / strict 8 条）。

### 6.2 可引用规模数字

- 核心 `mas/` **8,267 行** Python（27 个模块）；前端 493 行；测试 791 行；配置 659 行。
- 5 个角色 Agent + 1 个调度器；8 节点流水线（strict 9 / lean 7）；7 个离线算子。
- 唯一第三方依赖：**PyYAML**。

### 6.3 【待补充】占位（勿编造）

- 真实 LLM 模式的端到端验收通过率（当前 metrics.jsonl 中 openai_compatible 运行多为未通过，见 §8）；
- 支持的第三方 MCP 服务数量、Skill 沉淀数量（当前 4 个手写 Skill + 运行沉淀能力）；
- 使用人数/团队规模/生产环境部署情况。

---

## 七、简历项目经历（成稿，可直接粘贴）

### 7.1 一句话概述

> **配置驱动的多 Agent 软件交付系统（独立开发）**：以 Leader 统一调度 5 个角色 Agent，将「需求评估 → 技术方案 → 编码 → 代码审查 → 业务测试 → 缺陷修复 → 验收门禁 → 报告归档」全流程自动化，门禁判定基于自研 AST 白名单表达式求值而非模型自评，并通过事件日志驱动的三级经验库构建可量化验证的自学习闭环；全项目零第三方框架依赖（仅 PyYAML）。

### 7.2 描述要点（5 条，主动语态）

1. **设计配置驱动的 Leader 统一调度架构，将 8 节点交付流水线抽象为 YAML 状态机，实现流程拓扑零代码变更。** 用 `flow.yaml` 声明节点/角色/流转/门禁/回退，调度器按配置推进；通过 Profile 深合并（dict 递归、list 覆盖）与「裁剪节点自动重接流转边」支持流程可插拔——strict Profile 插入方案评审节点（8→9）、lean Profile 裁剪审查节点（8→7）并收敛门禁（8→3 条）；加载期完成 17 类结构校验（入口/角色/流转/自环/表达式白名单），把配置错误前置到启动阶段。
2. **构建黑板 + 消息总线的解耦协作机制，实现版本化上下文共享与 Agent 间请求-应答协商。** 5 个角色 Agent 零直接调用：产物走版本化黑板（全版本 + sha256 摘要，支持跨轮追溯），12 类关键事件按广播矩阵分发；每个角色按感知矩阵只读所需上游产物，缺失项显式声明「请勿臆造」；测试 Agent 发现需求歧义时经 Leader 中介向研发发起协商（correlation_id 关联），未闭环歧义直接作为门禁项阻塞验收。
3. **实现证据式验收门禁与分级回退容错，用 AST 白名单求值替代 `eval` 与模型自评判分。** 8 条表达式在黑板快照上求值 + reviewer 签署双重判定；求值器基于 `ast.NodeVisitor` 白名单（13 类节点 + 14 个内置函数，禁导入/赋值/lambda），字段缺失返回可读原因；per-node 回退策略（review→implement×3 等）+ 全局回退上限 8 + 最大步数 40 兜底，回退耗尽走升级策略仍出具不通过报告；代码审查基于真实 AST 静态分析（12 条规则）不受模型随机性影响。
4. **设计 L1/L2/L3 三级经验库自学习闭环，使经验注入产生可测量、可证伪的行为变化。** 从事件日志规则化提炼 5 类经验，按「相关性 0.45 + 置信度 0.25 + 频次 0.15 + 新鲜度 0.15」加权检索，受分层与全局（1,000 token）双预算约束注入 Prompt；为每条经验增加结构化 `action_key` 动作标识并被确定性执行体真实消费；实测第二轮运行步数 11→7（−36%）、缺陷 5→0、审查驳回 1→0、耗时 3.98s→1.0s（−75%）。
5. **建设全链路可观测性与零依赖扩展能力，使每次 Agent 决策可追溯到证据。** 单次运行产出 7 类证据（事件流/消息记录/Prompt 快照含注入清单/LLM 追踪含思考链/产物全版本/测试执行日志/证据索引），45 个单元测试覆盖核心模块；纯标准库实现 MCP 最小客户端（stdio JSON-RPC + SSE 双传输、五阶段健康检查）与 Skill 系统（SKILL.md 规范校验 + 冒烟 + 成功运行一键沉淀），配套零依赖 Web 面板（10 个 API、后台触发运行、中断检测）。

### 7.3 技术栈关键词（简历检索用）

```
Python 3 / 多 Agent 系统（Multi-Agent System）/ Agent 编排与调度 / Leader-Worker
配置驱动（Configuration-Driven）/ YAML Profile 深合并 / 流水线状态机
黑板模式（Blackboard）/ 消息总线（Message Bus）/ 发布-订阅 / 请求-应答协商
上下文工程（Context Engineering）/ Token 预算管理 / 结构化输出契约 / Prompt 工程
分层记忆（L1/L2/L3 Tiered Memory）/ 经验库 / 自学习闭环 / 加权检索打分
事件溯源（Event Sourcing）/ JSONL 事件日志 / 可观测性 / 证据链 / Tracing
AST 静态分析 / 白名单安全求值（Safe Evaluation）/ 代码审查自动化 / 质量门禁（Quality Gate）
沙箱执行（subprocess）/ 回退与重试 / 升级策略（Escalation）/ 优雅降级
LLM / OpenAI-Compatible API / 离线确定性 Provider / 推理模型思考链捕获 / 凭据外注与脱敏
MCP（Model Context Protocol）客户端 / Skill 系统 / 零依赖设计 / Python 标准库 / unittest
```

### 7.4 项目信息栏

- **项目角色**：独立设计并实现（个人项目）
- **周期**：2026-09 ~ 2026-10（git 历史 3 个提交）
- **规模**：核心 8,267 行 Python + 493 行前端 + 791 行测试；5 Agent + 1 调度器；8 节点流水线
- **依赖**：Python 3 + PyYAML（唯一第三方依赖）

### 7.5 避坑清单（写简历/面试时）

1. **不要说「使用了 LLM Function Calling / Tool Use」**——系统未用 function calling API，真实形态是「结构化 JSON 输出契约 + 确定性 Python 工具执行（沙箱/静态分析/MCP）」；这样表述反而更能体现设计判断。
2. **不要声称「高并发/分布式」**——Leader 是单进程同步 while 循环，`_consult_factory` 依赖类属性，同进程不能并行两个 run。
3. **不要编造通过率、耗时、用户量**——用 §6.3 占位或如实说明。
4. **优先用可核实量化**：11→7 步、5→0 缺陷、3.98s→1.0s、74 用例三类覆盖率 100%、45 测试通过、8,267 行、唯一依赖 PyYAML。
5. **主动说明已知短板**（见 §8）比回避更有说服力。

---

## 八、已知短板与诚实边界（面试被问到时主动讲）

| 短板 | 事实依据 | 改进方向 |
| --- | --- | --- |
| 真实 LLM 端到端稳定性不足 | metrics.jsonl 中 openai_compatible 运行多次未通过验收（9~16 步、回退 3~9 次、单轮 535s~2910s）；长 JSON 生成 + 刚性门禁下模型输出波动被放大 | 输出契约分层校验/局部重生成；门禁表达式按严重度分级；max_tokens 与思考链预算分离 |
| 经验反馈闭环只接一半 | `reinforce()` 已实现但全仓库无调用点，置信度实际只通过「同源合并 +0.1 / 晋级 +0.05」增长 | 打通「注入 → 行为变化 → 奖励回写」在线反馈 |
| 检索无语义能力 | 关键词 + 标签重叠打分，同义表述召回不到 | 引入 embedding 检索（与零依赖目标权衡） |
| token 估算为启发式 | 中文 1 字 1 token、英文 3.2 字符 1 token，无真实 tokenizer | 按 provider 接真实计数接口 |
| 单进程同步调度 | 无法并行多 run；前端触发运行用后台线程缓解 | 运行级隔离上下文，支持并发 run |

---

## 附录 A：验证方法与复现命令

```bash
# 1) 框架自测（45 个用例，含端到端离线流水线）
python -m unittest discover -s tests -v

# 2) 两轮离线运行，验证自学习闭环（首轮注入缺陷与审查驳回，二轮归零）
python - <<'PY'
import sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from mas.config import load_config
from mas.leader import Leader
tmp = tempfile.TemporaryDirectory()
cfg = load_config(Path("config"), workspace=Path(tmp.name) / "workspace")
cfg.llm["provider"] = "offline"; cfg.runtime["verbose"] = False
req = Path("examples/requirements/order_pricing_service.md")
for i in (1, 2):
    s = Leader(cfg).run(str(req))
    print(f"round{i}: verdict={s.verdict} steps={s.steps} bugs={s.bugs_total}"
          f" rejections={s.review_rejections} rollbacks={s.rollbacks}"
          f" duration={s.metrics['duration_s']}s injected={s.metrics['injected_experiences']}")
PY

# 3) 配置与流程视图
python -m mas config show --profile strict
python -m mas experience stats
python -m mas report show latest
```

> 实测输出（2026-10-13）：round1 verdict=pass steps=11 bugs=5 rejections=1 rollbacks=2 duration=3.98s；round2 verdict=pass steps=7 bugs=0 rejections=0 rollbacks=0 duration=1.0s injected=20。
