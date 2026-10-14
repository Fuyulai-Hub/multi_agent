# 多 Agent 项目：代码实读分析、对标与简历提炼（AI Agent 工程师向）

> 分析对象：`multi_agent`（配置驱动的多 Agent 任务协作系统，代码目录 `mas/`）。
> 口径：独立作者 / 负责人，目标岗位 AI Agent 工程师。
> 所有结论均标注 `文件:行号`；指标仅引用本地实测与 `workspace/` 真实运行留痕，未验证处标注 [待补充]。
> 本报告生成当日（2026-10-14）本地实测：`python -m unittest discover -s tests` → **Ran 152 tests, OK**（37.8s）。

---

## 1. 代码走读摘要

### 1.1 入口与整体结构

| 入口 | 文件 | 说明 |
| --- | --- | --- |
| CLI | `mas/cli.py`（497 行） | `python -m mas run / demo / report / experience / config / skill / mcp` |
| 一键演示 | `run_demo.py`（14 行）→ `mas demo` | 三轮演示：文档输入 → 经验复用 → 文字需求 |
| 前端面板 | `frontend/server.py`（493 行） | 仅标准库 `ThreadingHTTPServer`（`server.py:1-16` 列出 /api/runs、/api/experience、/api/llm 等接口），零额外依赖 |
| 调度核心 | `mas/leader.py`（820 行） | 状态机 / 门禁 / 回退 / 协商 / 经验闭环 |

生产代码约 9,700 行（`mas/` 全部 .py），测试约 2,400 行（13 个 test 文件、152 用例）。**第三方依赖仅 PyYAML**（`requirements.txt:1`）。

### 1.2 核心模块逐文件要点

- **`mas/leader.py`** — 唯一调度器。`run()` 装配事件日志、消息总线、黑板、沙箱、经验库、PromptBuilder、LLM 客户端与 ToolRunner（`leader.py:104-127`）；主循环按 `flow.yaml` 推进状态机（`leader.py:169-268`）：task 节点交给角色 Agent，gate 节点用表达式判定（`_run_gate`，`leader.py:406-467`）；fail 分支走回退策略（含每边上限与全局上限、耗尽后 `fail_run_and_report` 升级，`leader.py:222-267`）。回退时做**归因降权**：只降权 trigger 与失败事件类型匹配的经验，不误伤无关经验（`leader.py:52-56、604-671`）。节点级原地 retry 与流程级 rollback 是两套机制（`_act_with_retry`，`leader.py:549-571`；回退边判定修正见 `leader.py:801-813` 的实测注释）。
- **`mas/config.py`（748 行）** — 配置单一入口：system/flow/collaboration/learning/roles 合并为强类型对象；Profile **深合并**（`deep_merge`，`config.py:58-65`）；`skip: true` 节点被裁剪后自动重接流转边（`_apply_skips`，`config.py:548-574`）；加载期全量结构校验（`_validate`，`config.py:676-735`）；LLM 凭据三通道解析「文件 < 环境变量 < 运行时注入」（`resolve_credentials`，`config.py:141-159`）。
- **`mas/expr.py`（246 行）** — 门禁表达式求值器：AST 白名单（禁 import/赋值/任意属性），变量按 `产物.字段.子字段` 点路径在黑板快照上取值，缺字段返回可读原因而非抛异常（`expr.py:114-220`）；配置加载期还做表达式静态合法性校验（`check_config_expressions`，`expr.py:223-243`）。
- **`mas/blackboard.py`（197 行）** — 版本化共享黑板：publish 在**跨进程文件锁**内完成「磁盘版本号 max+1 + 落盘 v{n}.json + latest.json」（`blackboard.py:72-124`）；读取缓存未命中回落磁盘 latest.json，实现多实例最终一致（`blackboard.py:127-172`）。
- **`mas/bus.py`（157 行）** — 消息总线：direct/broadcast/request-response 三类，全部落盘 `messages.jsonl` 并同步写事件日志（`bus.py:60-104`）；`digest()` 生成通讯摘要注入 Prompt 感知段（`bus.py:147-151`）。
- **`mas/filelock.py`（91 行）** — 零依赖跨进程文件锁：Windows `msvcrt.locking` / POSIX `fcntl.flock`，超时抛 `FileLockTimeout` 而非静默重试（`filelock.py:32-91`），保护黑板产物、经验库、指标文件。
- **`mas/prompts.py`（216 行）** — 五段式 Prompt：角色系统段 / 任务指令 / 感知段（黑板+总线摘要）/ 分层经验注入段 / 交付契约（`prompts.py:1-9`）；每次 Prompt 连同注入经验 ID、token 估算落盘 `prompts/*.md` 作审计证据（`prompts.py:196-206`）。
- **`mas/llm.py`（470 行）** — 统一 offline / openai_compatible 两 Provider。**流式 SSE 请求**逐 chunk 组装、兼容服务端不透流式回退一次性 JSON（`llm.py:297-385`）；**思考预算治理**：`finish_reason=length`（思考链吃满 max_tokens 致空正文）与网关 504 统一视为"不可用"，按 `reasoning_effort` 降级阶梯重发（`llm.py:120-127、198-248`）；429/5xx/超时按配置退避重试（`_post_with_retry`，`llm.py:428-449`）；tool_calls 归一化（`llm.py:451-470`）。
- **`mas/experience.py`（847 行）** — 自学习闭环核心。L1/L2/L3 三级经验库、四维打分检索（相关性 0.45 / 置信度 0.25 / 频次 0.15 / 新鲜度 0.15，`experience.py:316-399`）、**逐层 token 预算**注入（L3=420/L2=360/L1=260，总 1000，`learning.yaml:13-36`）；中文 bigram + 同义词扩展的零依赖语义软匹配（`experience.py:87-101、444-457`）；规则化提炼 5 类经验（`experience.py:587-728`）；`reinforce()` 正负反馈调置信度（`experience.py:472-546`）；**晋级依据 hits×success_rate 而非纯 hits**（`promote`，`experience.py:549-584`）；全链读写在文件锁内（`experience.py:227-230`）。
- **`mas/sandbox.py`（754 行）** — 五级隔离：路径级 resolve 防越界 / 命令白名单只放行 Python 解释器（显式拒绝 shell，`sandbox.py:276-303`）/ 环境变量剔除 KEY/TOKEN/SECRET 等敏感词（`sandbox.py:304-314`）/ POSIX rlimit 限 CPU·内存·文件大小·进程数（多线程场景自动降级并在事件里如实标注，`sandbox.py:316-359`）/ 静态检测 network_import（`sandbox.py:29-32、543-555`）。另有两类"实测踩坑后加的"AST 规则：测试代码内联起外部程序的**白名单绕过检测**（`_command_bypass_findings`，`sandbox.py:597-639`）与标准库**未 import 检测**（`_unbound_json_findings`，`sandbox.py:646-670`）。AST 静态分析按文件并行（`parallel_map`，`sandbox.py:389-424`）。
- **`mas/agents/base.py`（334 行）** — 所有角色的统一骨架：感知→经验检索→Prompt 组装→生成（含 **function calling 多轮闭环**：tool_calls→ToolRunner 真实执行→role=tool 回传→续跑，并修复 assistant-tool_calls 消息顺序，`base.py:104-160`）→落地→发布→广播；每次 LLM 调用（含思考链、token 明细、降级标记）追加 `llm_traces.jsonl` 留痕（`base.py:165-199`）；角色 permissions 经 `permit()` 真正生效（`base.py:308-334`）。
- **`mas/agents/tester.py`（598 行）** — 独立生成三类用例并**真实沙箱执行**。交付契约内嵌结果回传协议与参考实现（`tester.py:20-145`）；执行前 AST 体检（`_preexec_diagnosis`，`tester.py:352-365`）；四类**根因归类**：测试套件缺陷 / 环境构建问题 / 交付物无法验证（`_root_cause`，`tester.py:373-415`）；**选型语言一致性**判定——非 Python 选型下 Python 用例全过不构成验收证据（`_verification_gap`，`tester.py:417-433`）；无执行证据不得判 pass（`tester.py:301-304`）；需求歧义协商走总线请求-应答，应答正文 ≥8 字符才算闭环（`tester.py:436-495` + `collaboration.yaml:31-39`）。
- **`mas/agents/reviewer.py`（167 行）** — 审查证据直接进 Prompt：框架对沙箱真实文件跑 AST 静态分析 + 交付源码原文（40K 字符预算）补进 user 段（`reviewer.py:66-117`，注释记录了"reviewer 拿不到源码只能空驳"的三轮实测根因）。
- **`mas/agents/developer.py`（384 行）** — 三态模式判定（first/rework/fix，optimize 节点永远走 fix 通道防重新引入已修缺陷，`developer.py:47-57`）；编码后沙箱导入自检（非 Python 交付物如实标"不适用"而非误报失败，`developer.py:160-186`）；修复时对每个缺陷跑**定向回归** `--only` 并产出回归自测报告（`developer.py:245-358`）；`consult()` 应答测试 Agent 的澄清请求。
- **`mas/mcp.py`（558 行）+ `mas/toolrunner.py`（144 行）** — 零依赖**最小 MCP 客户端**：stdio（子进程+换行分隔 JSON-RPC）与 sse（事件流+POST）两种传输，只实现 `initialize/tools/list/tools/call`（`mcp.py:405-481`）；`check_server` 按 parse→validate→connect→handshake→smoke 五阶段报错并给修复建议（`mcp.py:484-558`）；`${VAR}` 运行时展开避免凭据落盘（`mcp.py:75-83`）。ToolRunner 把 MCP `tools/list` 转 OpenAI schema 并真实执行回传，单服务不可用只跳过不阻塞（`toolrunner.py:37-69、84-129`）；Leader 侧默认不装配、拉不到工具自动降级为无工具运行（`leader.py:506-540`）。
- **`mas/skills.py`（542 行）** — 技能注册表：SKILL.md front-matter 六必填字段校验（`skills.py:67`）、冒烟测试复用 Sandbox（`skills.py:255-284`）、`load_run_facts` 把一次验收通过的运行只保留**最终通过路径**、剔除回退试错与绝对路径后沉淀为技能（`skills.py:366-473`）；沉淀只在用户显式 `skill add --from-run` 时发生（`leader.py:358-371`）。
- **`mas/providers/offline.py`（1161 行）** — 确定性 Provider：接口与 LLM 完全一致（收 PromptBundle 返 JSON），解析需求→匹配算子→生成**真实可运行代码与真实可执行测试**，可配置注入首轮缺陷演示 bug 闭环；关键设计：它**消费经验注入的 action_key**（如 `guard_calc_shipping__boundary`），使"经验注入"产生确定性、可回归的行为变化（`offline.py:15-19`）。

### 1.3 数据流与控制流

```
需求文本 → requirement_source(黑板)
  → 状态机循环(leader.py:169)：node_start → Agent.act()
      act(): 感知(黑板require+总线digest) → 经验检索(token预算) → 五段Prompt(落盘)
             → LLM.generate(offline|openai_compatible|+tools闭环) → apply()
                 apply(): 写沙箱文件 → 真实执行/AST分析 → 发布版本化产物 → 声明广播事件
      → 广播矩阵分发 → L1 即时经验提炼 → pass/fail 分支 → rollback/retry/escalation
  → acceptance gate：8 条 AST 白名单表达式在黑板 snapshot 上求值 + reviewer 签署(可否决)
  → report：证据索引(evidence/index.jsonl) + 验收报告 + run_summary/timeline
  → 收尾：终态反馈 reinfore → promote 晋级 → metrics.jsonl → learning_report.md
```

唯一事实来源是 `events.jsonl`（`events.py:1-5`）：经验提炼、证据统计、时间线、前端面板全部只依赖它与黑板产物。

---

## 2. 项目背景与为什么做多 Agent

市面 LLM 应用普遍把 Agent 等同于「对话」，而本项目的出发点是：**把一个中等规模软件需求（定时调度、电商计价、限流服务、C++ 智能指针）端到端跑成经过真实验证的交付物**。单 Agent 生成代码存在三个结构性问题：

1. **自己既当运动员又当裁判**——生成的代码、测试、结论出自同一上下文，无法互相制衡；本项目拆成产品/研发/评审/测试/报告 5 个角色，测试 Agent 的期望值**只能来自需求契约、禁止读实现代码反推**（`tester.py:140`）。
2. **幻觉与"假绿"**——LLM 声称写完即通过。本项目所有结论必须有可回放证据：真实沙箱执行 + stdout 标记协议回传（`tester.py:213-240`）、真实 AST 分析（`sandbox.py:389`）、门禁表达式对黑板快照求值（`leader.py:406-467`）。实测抓出过最危险的失败模式：C++ 需求被研发"换成 Python 复刻"骗到 8 项门禁全绿，而 `.hpp` 从未编译——修复为语言选型一致性硬判定（`tester.py:417-433`）。
3. **不学习**——每次运行从零试错。本项目以事件日志为源做分级经验库 + 置信度反馈，让"上轮踩的坑"在下一轮注入 Prompt 并被 Provider 实际消费（`experience.py` 全链路 + `offline.py:15-19`）。

配置驱动（改 YAML 即改流程）与零依赖（仅 PyYAML）是刻意的工程约束：**流程可审计、环境可复现、无框架锁定**。

---

## 3. 与市面多 Agent 框架的区别与优势

| 维度 | AutoGen | CrewAI | LangGraph | MetaGPT | ChatDev | **本项目** |
| --- | --- | --- | --- | --- | --- | --- |
| 编排范式 | 群聊/对话驱动 | 角色+顺序/层级任务 | 底层图原语，自搭状态 | SOP+角色扮演 | 聊天链瀑布 | **配置驱动状态机**：节点/门禁/回退全在 YAML，改配置即改流程（`config.py:548-574`、`profiles/strict.yaml` 插节点、`lean.yaml` 裁节点自动重接线） |
| Agent 间通信 | 消息会话 | 任务输出传递 | 手写 state 传递 | 共享消息池 | 双人聊天 | **黑板（版本化产物）+ 消息总线（广播矩阵/请求-应答）双通道**，全部落盘可审计（`blackboard.py`、`bus.py`、`collaboration.yaml:8-28`） |
| 质量判定 | LLM 评判为主 | LLM 评判 | 自行实现 | 评审角色 | 阶段评审 | **表达式门禁**：8 条 AST 白名单安全求值判据 + 角色签署可否决（`flow.yaml:82-98`、`expr.py:114-199`），判据来自真实执行证据而非 LLM 自评 |
| 工具调用 | 内置插件体系 | 工具注册 | LangChain tools | 有 | 弱 | **自研最小 MCP 客户端 + OpenAI function calling 多轮闭环**，零 SDK 依赖、失败自动降级（`mcp.py:405-481`、`base.py:104-142`） |
| 记忆/自学习 | Memory 组件（存取为主） | 短期记忆 | checkpointer | 经验回放较弱 | 无 | **闭环且可反悔**：事件→提炼→分层注入（token 预算）→按结果归因调置信度→按 hits×success_rate 晋级（`experience.py:472-584`） |
| 验证真实性 | 取决于接入 | 取决于接入 | 取决于接入 | 部分执行 | 部分执行 | **沙箱真实执行+五级隔离**，无证据强制判 fail（`sandbox.py:114-124`、`tester.py:301-304`） |
| 依赖体积 | 重 | 中 | 中 | 重 | 中 | **生产依赖仅 PyYAML**，约 9.7K 行，全模块可通读 |

**一句话定位**：LangGraph 给你"画图的权利"，AutoGen/CrewAI 给你"开会的权利"，本项目给的是"**验收的权利**"——流程、门禁、证据、回退、学习全部显式化、配置化、可审计。差异化最强的一点是**对"假通过"的对抗设计**（第 5 节 F 类问题清单），这在任何主流框架里都不是开箱即用的。

**代价也要如实讲**：对话式框架灵活度高、生态工具多；本项目是垂直流水线（软件交付场景），换场景需要重写角色契约与门禁表达式；无并行节点执行、无分布式调度。这些在简历口径里不主动写、面试被问如实答（第 8 节）。

---

## 4. 具体实现、技术栈、能力

### 4.1 架构与编排
- Leader-Follower 流水线：`intake → solution → implement → review → test → acceptance(gate) → report`，另有 optimize 修复回环（`flow.yaml`）；回退按边配置上限（review/test ×3、acceptance ×2）+ 全局上限 8（`collaboration.yaml:42-69`）。
- 角色职责、人设、准则、审查策略、订阅层级、**permissions（can_write_code/can_execute/can_rollback/can_block_acceptance）** 均为 YAML 配置并真正在代码里执法（`roles/*.yaml` + `base.py:308-334` + `leader.py:39-44、426-438、573-589`）。
- Profile 机制：strict 在方案与编码间插入 `solution_review` 节点、lean 裁剪 `review` 并把 8 项门禁收敛为 3 项——演示"新增/裁剪/改门禁=改配置"（`config/profiles/*.yaml`、`config.py:548-574`）。

### 4.2 通信协议
- 黑板：产物名 + 版本序列（`v1.json…/latest.json`），记录 producer/node/iteration/sha256 摘要（`blackboard.py:36-57`）；跨进程安全由文件锁 + 磁盘版本号 + 读回落保证（`blackboard.py:9-13`）。
- 总线：`direct / broadcast / request-correlation-response` 三类，落盘 `messages.jsonl`；广播目标由矩阵配置（`collaboration.yaml:16-28`）。
- 协商：tester→developer 请求-应答，Leader 中介留痕 `negotiation_closed` 事件；`min_answer_chars=8` 防空答复刷闭环（`collaboration.yaml:37`、`tester.py:475-486`）。

### 4.3 工具调用 / MCP / 技能
MCP（stdio/sse）→ `tools/list` 转 OpenAI schema → 随 chat/completions 下发 → `tool_calls` 由 ToolRunner 经 `tools/call` 真实执行 → `role=tool` 回传续跑（上限 `tool_max_rounds=2`）。失败降级路径全部留事件：服务不可用跳过、空工具集不下发、装配异常不阻塞运行（`leader.py:506-540`）。技能层独立于流水线：六字段 SKILL.md 规范、静态校验+冒烟、成功运行按"最终通过路径"沉淀。

### 4.4 记忆与检索（非向量 RAG）
经验库=JSONL 持久化的结构化条目（lesson/action/action_key/trigger/confidence/hits_success/fails）。检索为零依赖语义软匹配：中文单字+bigram 分词、同义词扩展（`learning.yaml:83-89`）、重叠系数相似度、四维加权打分、**逐层 token 预算裁剪**（超预算条目记入 `skipped` 留痕）。与向量 RAG 的取舍：规模小（百条级）时精确可控可回归，无 embedding 依赖。

### 4.5 评测与可观测
- 每节点产物 + 每轮测试 stdout/stderr/result.json + 证据索引 `evidence/index.jsonl` + `run_summary.json` + `timeline.md`。
- LLM 层：`llm_traces.jsonl` 记录思考链、finish_reason、prompt/completion/reasoning token 拆分、降级标记（`base.py:165-199`）。
- 跨运行指标：`metrics.jsonl`（29 次真实运行留痕）+ `learning_report.md` 自动对比表。
- 前端面板：运行列表/事件流/报告/经验库/模型配置（含 API Key 连通性探测、动态模型下拉、上下文用量进度条）。

### 4.6 技术栈清单
Python 3.10+（标准库为主）、PyYAML、urllib 手写 OpenAI 兼容客户端（含 SSE 解析）、subprocess 沙箱 + rlimit/msvcrt/fcntl、ast 静态分析与表达式求值器、JSON-RPC 2.0（MCP）、http.server 面板、unittest（152 用例）。

---

## 5. 难点与解决方案（均有代码/注释实证；实测时间戳来自提交历史与 docs/10-12）

### 5.1 可靠性难点：对抗"假通过"（本项目最有含金量的部分）
| 问题（真实踩坑） | 解决方案 | 证据 |
| --- | --- | --- |
| 0 用例执行时 `failed==0` 被误判 pass，32 个缺陷被自动闭环 | "无执行证据不得判 pass"；缺陷闭环判定仅在本轮真实执行到用例时生效 | `tester.py:301-304、577-588` |
| C++ 需求被换成 Python 复刻刷绿 8 项门禁 | 选型语言一致性硬判定 `_verification_gap`，非 Python 直接 fail 并写明验证口径缺口 | `tester.py:417-433` |
| 测试文件内联 `subprocess.run(["g++"])` 绕过白名单，失败只剩 19 条 FileNotFoundError | 执行前 AST 体检检出 `sandbox_command_bypass/unresolved`；四类根因归类（环境 vs 交付物 vs 测试套件缺陷），避免让研发改"不存在的 g++" | `sandbox.py:597-639`、`tester.py:185-196、373-415` |
| LLM 写回传协议却漏 `import json`，结果静默为 0 用例 | `{module}_not_imported` 保守静态检测 + 协议违规发 `test_protocol_violation` 事件写明原因 | `sandbox.py:646-670`、`tester.py:224-237` |
| 空答复被计成协商闭环、门禁虚报 | `min_answer_chars=8` 实质判定 + 框架按请求顺序覆写 status | `tester.py:471-486` |
| reviewer 拿不到源码只能空驳（三轮同措辞） | 审查节点把 AST 证据+源码原文 40K 预算注入 Prompt，且留痕到 prompt 文件 | `reviewer.py:66-117` |
| LLM 输出结构不可预测致节点崩溃（negotiation 输出成字符串） | 契约外防御：结构非法项跳过不崩节点 | `tester.py:174-175、438-443、523-525` |

### 5.2 成本/延迟难点：真实端点的工程驯服
- 网关 300s 强断 + 推理模型思考链吃满 max_tokens 返回空正文（实测 GLM：60,735 字符思考链、finish_reason=length、正文 0 字符、221s）→ **流式 SSE**（TTFB<1s 不受总时长限制）+ **reasoning_effort 降级阶梯 low→high**（实测 low 33s/high 115s 可收敛）+ 超时与截断统一进降级链而非崩节点（`llm.py:108-127、198-248、297-385`；`system.yaml:31-43` 记录了实测数据）。
- 空正文不静默：诊断信息把"思考链占满预算"根因直接写进 error（`llm.py:412-426`）。
- 感知段默认 900 字符把 12 功能点需求截到只剩 3 个（全程信息残缺）→ 可配置 `prompt_artifact_max_chars=10000`（`base.py:229-231`、`system.yaml:82`）。
- 经验注入总量 1000 token 预算、逐层裁剪并记录 skipped——注入本身也要控成本（`experience.py:340-377`）。

### 5.3 工程难点
- **并发与多实例**：黑板/经验库/指标全部走跨进程文件锁（msvcrt/fcntl 双实现），版本号取磁盘 max+1 防并发覆盖；协商运行时引用经闭包显式传递，Leader 实例并发复用不串证据链（`filelock.py`、`blackboard.py:72-124`、`leader.py:470-477`）。
- **回退语义**：转发边重进已执行节点被误判回退→无权限角色触发 permission_denied 掐死闭环→修正为"仅 collaboration.rollback 显式声明的边算回退"（`leader.py:801-813` 注释含实测 run 号）。
- **自学习反模式**：纯 hits 晋级会把"每次都导致返工"的经验推上 L3→改 hits×success_rate；全量降权误伤无关经验→改按失败事件类型归因匹配 trigger，同运行不重复降权（`experience.py:549-584`、`leader.py:47-56` 注释含实证）。
- **可复现回归**：自然语言理解部分用确定性 offline Provider 替代（代码生成/测试执行/审查/门禁全部真实），使 152 个单测可在 CI 语义下 38s 跑完（本地实测）。

### 5.4 代码未体现、建议补充
- 多语言/多工具链真实验证（g++/ctest 接入沙箱白名单的受控扩展）——当前仅"识别缺口"不解缺口。
- 节点级并行编排（当前严格串行状态机）。
- token 成本核算（有 usage 留痕，无货币化统计与预算熔断）：[待补充]。
- 经验注入的消融实验（注入 on/off 对照的显著性检验，当前只有前后对比）。
- 人审节点（human-in-the-loop gate）。
- 失败任务的重试调度/断点续跑（run 级重来，无 step 级恢复）。

---

## 6. 可量化指标建议（左列=代码/留痕可直接支撑；右列=口径建议）

已实测、可写进简历的（出处：`workspace/knowledge/metrics.jsonl`、`workspace/reports/learning_report.md`、本报告首实测记录）：

- **152 个 unittest 全绿**（2026-10-14 本地实测，37.8s）；生产依赖仅 1 个第三方库（PyYAML）；生产代码约 9.7K 行。
- **29 次真实运行留痕**，覆盖 4 个需求域（定时调度 / 电商 / 限流 / C++ 智能指针）；其中真实 LLM 端到端通过案例：eshop 9/11/11 步、ratelimit 13 步、cron 21 步（`metrics.jsonl`）。
- **自学习收益（离线同任务对照）**：order_pricing 首轮通过运行 11 步、2 回退、1 审查驳回、5 缺陷（2.61s）→ 后续运行 **7 步、0 回退、0 驳回、0 缺陷（0.95s）**，即步数 -36%、回退/缺陷清零（`learning_report.md` 表格；README 写的"12→7"是更早一轮口径，引用时以留痕表为准）。
- 经验库规模 **83 条（L3×20 / L2×2 / L1×61）、累计命中 1,216 次、平均置信度 0.845**（`learning_report.md`）。
- 验收门禁 **8 项表达式判据**；回退三重上限（单边 3/3/2 + 全局 8 + max_steps 40）。
- LLM 可靠性：实测空正文根因案例（60,735 字符思考链吃满 16K 预算）→ 降级后 33s 返回 12,620 字符正文（`system.yaml:33-41` 注释）。

[待补充] 建议补测的口径：真实 LLM 下同类任务前后运行的步数对照（目前离线对照最干净，LLM 运行受端点延迟噪声影响——见项目记忆"单节点最长 68 分钟"，勿以耗时为证据）；token 成本每任务均值；门禁拦截率（拦下的不合格交付 / 总交付）。

---

## 7. 简历项目描述（可粘贴版）

**项目名称**：配置驱动的多 Agent 软件交付流水线（独立作者，个人全栈项目）
**一句话**：从 0 设计实现 Leader 调度 5 角色 Agent（产品/研发/评审/测试/报告）的多 Agent 系统，将中等规模软件需求经"需求拆解→方案→编码→AST 审查→沙箱测试→表达式门禁→验收报告"全自动闭环交付，以真实执行证据对抗 LLM 幻觉与"假通过"，并用分级经验库构建可度量的自学习闭环；生产依赖仅 PyYAML，约 9.7K 行 + 152 测试全绿。

**Bullet（STAR 精简口径）**：

- **设计**配置驱动的多 Agent 编排架构：YAML 定义流程节点/门禁/回退/广播矩阵，Profile 深合并支持增删节点与门禁改写（8 项验收判据以 AST 白名单表达式在版本化黑板快照上安全求值），配合 Leader 状态机三重回退上限（单边/全局/步数）与角色级 permissions 执法，实现"改配置即改流程、全程无人工干预"。
- **实现**黑板（版本化产物+跨进程文件锁）与消息总线（广播/请求-应答协商）双通道通信协议，5 角色经统一"感知→经验注入→五段 Prompt→生成→真实执行→发布"骨架协作；测试 Agent 期望值仅来自需求契约，杜绝自证式通过；29 次真实运行覆盖 4 个需求域完成端到端验收。
- **构建**事件日志驱动的分级（L1/L2/L3）自学习闭环：规则化提炼→中文 bigram+同义词扩展的零依赖语义检索→逐层 token 预算注入（≤1000）→按失败归因降权、按通过奖励，晋级采用 hits×success_rate 防"返工经验上位"反模式；离线同任务实测步数 11→7（-36%）、回退 2→0、缺陷 5→0，经验库沉淀 83 条、累计命中 1,216 次。
- **优化**真实 LLM 接入可靠性：手写 OpenAI 兼容客户端，流式 SSE 规避网关 300s 强断，`reasoning_effort` 降级阶梯治理推理模型"思考链吃满输出预算返回空正文"（实测 60K 字符链→降级后 33s 正常产出），超时/截断统一进降级链、429/5xx 退避重试，保障 implement 级 64K token 长生成稳定收敛。
- **落地**证据优先的验证与安全体系：沙箱五级隔离（路径/命令白名单/凭据环境变量剔除/POSIX rlimit/网络依赖 AST 检测），并针对实测"假通过"案例新增执行前 AST 体检、命令白名单绕过检测、选型语言一致性判定与四类根因归类（需求/设计/代码/环境），将"0 用例刷绿门禁""Python 复刻骗过 C++ 验收"等隐蔽缺陷在框架层拦截。
- **实现**零 SDK 的最小 MCP 客户端（stdio/sse JSON-RPC 双传输、五阶段健康检查）与 OpenAI function calling 多轮闭环（tools/list→schema 转换→真实执行→tool 回传续跑），以及技能（Skill）沉淀机制——仅从验收通过的运行提炼"最终通过路径"为可复用 SKILL.md 并冒烟校验。

**技术栈**：Python（标准库 + PyYAML）· 多 Agent 编排（状态机/黑板/消息总线）· OpenAI 兼容 API 与 SSE 流式 · Function Calling · MCP（JSON-RPC 2.0）· AST 静态分析与安全表达式求值 · 沙箱隔离（subprocess/rlimit/文件锁）· 自学习记忆（分级经验库/token 预算检索）· unittest（152 用例）

---

## 8. 面试追问问题与回答要点

1. **"和 LangGraph/AutoGen/MetaGPT 比，你重新发明的价值在哪？"**
   要点：不是重发明编排，而是补了主流框架的三块空白——①验收判定是表达式+真实执行证据而非 LLM 自评（`flow.yaml:82-98`）；②自学习带反馈归因与反模式防护（success_rate 晋级、trigger 归因降权）；③对"假通过"的对抗设计（第 5.1 节六项，全部来自实测）。坦承代价：垂直于软件交付场景、无并行/分布式。
2. **"你的'自学习'怎么证明不是 prompt 玄学？"**
   要点：可测量、可归因、可回放。注入留痕到 prompts/*.md（经验 ID+预算），消费留痕到 llm_traces 与事件，效果对比在 metrics.jsonl/learning_report.md；离线 Provider 声明式消费 action_key，使同任务 11 步→7 步、缺陷 5→0 成为确定性可回归结论（`offline.py:15-19`）。主动说明局限：LLM 运行受端点延迟噪声影响，最干净对照是离线；消融实验未做，[建议补充]。
3. **"多 Agent 通信为什么不直接用群聊？"**
   要点：对话不可审计且 token 随角色数平方增长。黑板传"产物"（版本化、可追溯谁在第几轮改了什么）、总线传"状态"（广播矩阵按配置裁剪接收方）、协商走请求-应答带 correlation_id——每条都落盘成证据（`blackboard.py:72-113`、`bus.py:60-104`）。
4. **"经验库检索为什么不用 embedding/向量库？"**
   要点：规模百条级，bigram+同义词+四维打分的重叠系数方案零依赖、可解释（skipped 原因留痕）、可单测；向量检索会引入依赖和不可控召回。若库到万级会换 ANN，但结构化字段（tier/scope/role/confidence）过滤这层设计仍保留。
5. **"沙箱安全到什么程度？别夸大。"**
   要点：如实说五级隔离是"弱隔离+如实标注"：无网络命名空间，所以用静态 network_import 检测让高危可见（`sandbox.py:27-32` 注释原文）；rlimit 在 Windows/多线程场景自动跳过并在事件里标 `rlimit-skipped-multithread`（`sandbox.py:316-359`）——隔离等级本身是审计字段而不是口号。生产要接容器/gVisor/firejail，改造点就是 Sandbox.run 一处。
6. **"最难的一个 bug？"**
   推荐讲 task04"假通过"：C++ 需求在回退压力下被 Python 复刻刷绿全部 8 项门禁并签署验收（`tester.py:417-433` 注释记录了 verdict=pass/覆盖率 1.0 而 .hpp 从未编译）。修复思路不是加提示词，而是把"验证口径与选型语言一致性"做成测试节点的硬判定——修在证据源而非放宽门禁（该取舍与项目记忆一致）。
7. **"回退死循环怎么防？"**
   三重上限（单边 max_attempts/全局 8/max_steps 40）+ 升级策略 fail_run_and_report 仍出报告标注原因（`leader.py:245-267`）+ forbid_self_loop 配置校验（`config.py:706-710`）+ 回退边必须是显式声明（防把正常迭代误判回退而掐死闭环，`leader.py:801-813`）。
8. **"如果再迭代一版，先做什么？"**（对应 docs/13 重构方向）
   要点：①多语言工具链进白名单让 C++ 类需求可真实验证；②step 级断点续跑；③token 成本核算与预算熔断；④注入消融实验把自学习收益做成统计显著。
9. **"152 个测试都测什么？"**
   要点：分层——配置/表达式/流水线（`test_pipeline.py` 端到端）、经验反馈（`test_experience.py`）、沙箱加固（`test_hardening.py` 644 行：白名单/绕过/env 剔除/rlimit）、流式与 token 预算（`test_token_budget.py`）、function calling 闭环用 fake MCP server 打桩（`tests/fake_mcp_server.py` + `test_function_calling_e2e.py`）、tester 鲁棒性（406 行，专测"假通过"场景）。
10. **"你如何保证 Agent 的结论不越权？"**
    要点：角色 permissions 在三个执法点生效——沙箱写/执行（`base.py:308-334`）、回退（`leader.py:573-589`）、验收否决权（`leader.py:426-438`，无 can_block_acceptance 的 reject 会被发 `permission_denied` 事件作废）。

---

### 附：本次分析核对过的真实执行记录

```
$ python -m unittest discover -s tests
Ran 152 tests in 37.777s
OK

$ metrics.jsonl 运行数: 29（含 4 需求域 + 离线 demo 多轮）
learning_report.md: 经验库 83 条 {L3:20, L1:61, L2:2}，累计命中 1216，平均置信度 0.845
order_pricing 离线对照: 11步/2回退/5缺陷/2.61s → 7步/0回退/0缺陷/0.95s
git 最近提交: 0490c86（端到端闭环验证盲区 + LLM 流式输出）
```
