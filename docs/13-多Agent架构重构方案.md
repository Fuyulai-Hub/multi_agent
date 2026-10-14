# 多 Agent 协作系统 · 架构重构方案

> 版本：v1.0　　编写日期：2026-10-14
> 视角：高级架构师 / 技术专家
> 目标：从「教学级 POC」重构为「可开源、可与市面主流多 Agent 框架对标」的工程化系统

---

## 0. 执行摘要（TL;DR）

### 0.1 一句话现状

这个项目**在范式上是对的，在工程上是脆的**。

它已经做出了很多同类开源项目没做对的事——确定性状态机编排、AST 白名单门禁求值、证据链留痕、不加证据不得通过验收。这些是"骨架正确"的标志。但它的**运行模型是单进程同步串行、状态是裸文件系统、Agent 之间靠类属性共享全局变量、自学习闭环写了一半没接线**。这三条决定了它现在最多是"一个跑得通的单机演示"，而不是"一个可以让人 clone 下来做二次开发的框架"。

`mas/` 9986 行 Python + `tests/` 2774 行 + `frontend/` 493 行，**仅依赖 `PyYAML>=6.0`**。零依赖是当前最大的技术资产，也是最大的技术债来源——因为很多该做的事（并发、持久化、检索、隔离）被"零依赖"这条隐性约束挡住了，只好用最朴素的方式手搓。

### 0.2 重构的核心判断

| 判断 | 内容 |
| --- | --- |
| **保什么** | 配置驱动状态机、门禁表达式、经验库分层模型、证据链设计、MCP/Skill 扩展层设计、零依赖的**可选性**（而非强制性） |
| **换什么** | 运行模型（同步串行 → 异步并发）、状态后端（裸 FS → Storage 抽象）、运行时上下文（类属性 → 显式 Runtime）、沙箱（本机子进程 → Executor 抽象）、检索（关键词 → 混合检索） |
| **补什么** | 经验反馈闭环（`reinforce` 接线）、事件流式 API、可观测性（trace/metrics）、Docker 隔离、真正的开放 API 层（SDK/HTTP） |
| **砍什么** | `Leader` 类属性运行时绑定、`perception`/`broadcast` 硬编码矩阵的强耦合、文档中已经过时的能力声明 |

### 0.3 建议的重构节奏

**不做大爆炸式重写**。分 4 个阶段、约 14 周，每个阶段结束时仓库都处于"可发布"状态：

- **P0（3 周）**：止血 + 立信 —— 修真实缺陷、把测试覆盖率拉起来、CI 上线、开源基础设施就位
- **P1（4 周）**：内核解耦 —— Runtime 上下文、Storage 抽象、异步执行器、经验闭环接线
- **P2（4 周）**：能力对标 —— 混合检索、Docker Executor、可观测性、流式事件 API
- **P3（3 周）**：开放生态 —— Python SDK、HTTP/SSE 服务端、插件体系、文档站

---

## 1. 现状评估

本章所有结论均基于代码事实（附 `file:line`）。凡与既有文档冲突的，以代码为准，并在 §1.5 明确标注哪些旧结论已经过时。

### 1.1 架构全貌

```
                       ┌────────────────────────────────────────┐
   需求文档 / 文字 ───► │  Leader（mas/leader.py, 732 行）        │
                       │  确定性状态机 + 门禁 + 回退 + 经验提炼   │
                       └───────────────┬────────────────────────┘
                                       │ while node not terminal
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        ▼
       ┌────────────┐          ┌────────────┐          ┌────────────┐
       │  Agent 群   │          │  协作设施   │          │  扩展层     │
       │ product     │◄────────►│ Blackboard │◄────────►│ Skill      │
       │ developer   │          │ MessageBus │          │ MCP        │
       │ reviewer    │          │ EventLog   │          │ ToolRunner │
       │ tester      │          │ Sandbox    │          └────────────┘
       │ reporter    │          │ Experience │
       └────────────┘          └────────────┘
                                       │
                                       ▼
                          workspace/（裸文件系统为唯一真相源）
```

**编排流**（`config/flow.yaml`，8 节点）：

```
intake → solution → implement → review ─┬─ pass ─► test ─┬─ pass ─► acceptance(gate) ─► report
                                        └─ fail ─► implement（回退 ×3）
                                                          └─ fail ─► optimize ─► test（回退 ×3）
                                                    acceptance fail ─► optimize（回退 ×2）
```

### 1.2 模块清单与职责

| 模块 | 行数 | 职责 | 健康度 |
| --- | --- | --- | --- |
| `mas/leader.py` | 732 | 状态机调度、门禁求值、回退、经验提炼、Skill 蒸馏 | ⚠️ 上帝对象 |
| `mas/config.py` | 748 | 配置加载/深合并/校验/凭据解析 | ✅ 良好 |
| `mas/sandbox.py` | 754 | 路径隔离、命令白名单、AST 静态分析、rlimits | ⚠️ 隔离强度不足 |
| `mas/experience.py` | 751 | L1/L2/L3 经验库、评分、晋升、提炼 | ⚠️ 闭环未接线 |
| `mas/agents/tester.py` | 598 | 测试执行、协商、缺陷追踪、根因分类 | ✅ 最成熟 |
| `mas/mcp.py` | 558 | MCP 最小客户端（stdio/sse，手写 JSON-RPC） | ✅ 良好 |
| `mas/skills.py` | 542 | SKILL.md 解析、校验、从运行蒸馏 | ✅ 良好 |
| `mas/llm.py` | 470 | 离线/OpenAI 兼容双 provider、SSE 流式、降级阶梯 | ✅ 良好 |
| `mas/agents/developer.py` | 384 | 方案设计/编码/修复、导入自检、定向回归 | ✅ 良好 |
| `mas/agents/base.py` | 334 | Agent 骨架、工具调用循环、LLM trace | ✅ 良好 |
| `mas/agents/reporter.py` | 239 | 报告生成、Skill 蒸馏、证据索引 | ✅ 良好 |
| `mas/prompts.py` | 216 | 提示词组装 | ✅ 良好 |
| `mas/blackboard.py` | 197 | 版本化产物存储 + sha256 摘要 + file_lock | ⚠️ `_latest` 仅进程内 |
| `mas/events.py` | 175 | 事件溯源 `events.jsonl` | ✅ 良好 |
| `mas/bus.py` | 157 | 消息总线 `messages.jsonl` | ✅ 良好 |
| `mas/toolrunner.py` | 144 | 函数调用闭环 | ✅ 良好 |
| `mas/concurrency.py` | 49 | `parallel_map`（ThreadPoolExecutor） | ✅ 已备未用 |
| `mas/filelock.py` | 91 | 跨进程文件锁（msvcrt/fcntl） | ✅ 良好 |
| `mas/schemas.py` | 86 | `AgentResult`/`NodeTask` | ✅ 良好 |
| `mas/agents/reviewer.py` | 167 | 静态审查（注入 AST 证据） | ⚠️ 审查深度弱 |
| `mas/agents/product.py` | 97 | 需求解析 | ✅ 良好 |
| `mas/cli.py` | 498 | CLI 入口 | ✅ 良好 |

### 1.3 已经做对的六件事（重构时必须保住）

这六项是项目的差异化资产，也是对外开源时最有说服力的部分。**任何重构都不得削弱它们。**

1. **确定性状态机编排，而不是让 LLM 自由路由**（`leader.py:run()`）
   流程走向由 `flow.yaml` 的 `transitions` 与 `gates` 决定，LLM 只在节点内做内容生成。这带来了可复现、可审计、成本可控三个性质。对比 CrewAI 的 `Process.hierarchical` 和 AutoGen 的 `GroupChat`——后两者把控制流交给 LLM，生产环境常见"跑飞"。

2. **AST 白名单表达式求值，而非 `eval()`**（`mas/expr.py`）
   门禁标准如 `test_report.coverage.boundary.rate >= 1.0` 经过 AST 解析后只允许白名单节点。这是很多同类项目踩过的坑——用 `eval()` 求值门禁条件，一旦 `flow.yaml` 可被外部输入就变成 RCE。

3. **"不加证据不得通过"的硬约束**（`tester.py`）
   ```python
   result.status = "pass" if (summary["total"] > 0 and summary["failed"] == 0
                              and negotiation["open"] == 0 and not verification["gap"]) else "fail"
   ```
   测试数量为 0 → 不通过；协商未闭环 → 不通过；技术选型非 Python 导致测试无法构成有效证据 → 不通过（`_verification_gap()`）。**这一条直接否定了 LLM Agent 最常见也最危险的失败模式：自说自话地宣布成功。**

4. **可证伪的经验闭环设计**（`experience.py`）
   经验条目带 `action_key`，不是"记住一个教训"，而是"记住一个可验证的动作"。这个设计意识超出了多数开源经验库。

5. **MCP + Skill 双扩展层，且是真实实现**
   `mcp.py` 手写 JSON-RPC 走 stdio/sse，完成了 `initialize`/`tools/list`/`tools/call`；`toolrunner.py` 把 MCP 工具转成 OpenAI tools schema，接进 `base.py:_generate_with_tools()` 的工具调用循环。**不是伪代码。**

6. **零第三方依赖**（`requirements.txt`：`PyYAML>=6.0`）
   便携性是真实优势：无 venv 也能跑、审计面极小、供应链风险接近零。**注意：这是资产，但不应升级为教条**（见 §5 ADR-7）。

### 1.4 真实缺陷清单（按严重度分级）

#### 🔴 P0-A｜运行时不安全：`Leader` 用类属性承载运行态

`mas/leader.py`：

```python
class Leader:
    _current_bus: Optional[MessageBus] = None
    _current_events: Optional[EventLog] = None
    _current_agents: Dict[str, Any] = {}
    _current_ctx: Optional[AgentContext] = None

    def _bind_runtime(self, ctx, bus, events, agents) -> None:
        self._current_ctx = ctx      # ← 写的是类属性
        ...
```

`_consult_factory()` 返回的闭包通过 `self._current_bus` 读取这些值。问题在于**赋值目标解析为类属性**（因为类定义了同名类属性，`self.x = v` 在实例字典里创建遮蔽——但这个遮蔽只在实例上生效，而任何按类名或新实例访问的路径拿到的是共享值）。

实证风险：两个 `Leader` 实例并发 `run()`，后启动的运行会覆盖前者的运行时绑定，导致 **A 运行的消息进 B 运行的总线**。`frontend/server.py:340` 已经在用工作线程——这条路迟早会被踩到。

**这是重构的第一优先级，因为它不是"能力不足"，是"隐性正确性缺陷"。**

#### 🔴 P0-B｜自学习闭环写了一半：`reinforce()` 无调用者

`mas/experience.py:450` 定义了 `reinforce()`（用下游结果回头修正经验置信度）。grep 全仓库：**唯一出现处是定义处本身**。

当前实际链路：

```
Leader._track_experience() → run_state["injected_experience_ids"] = [...]   ← 只是记了个账
                                                                             ↑ 到此为止，没有下文
```

**后果**：经验库只增不减、只升不降。一条被注入后导致回退的错误经验，与一条带来一次通过的正确经验，置信度演化完全相同。`promote()` 只按 `hits` 晋升——**使用次数多 ≠ 效果好**，一条被反复注入但每次都导致返工的经验会一路晋升到 L3。

这动摇的是项目的核心叙事（"自学习闭环"）。不修，开源后一定会被人问。

#### 🟠 P1-C｜状态后端是裸文件系统

- `blackboard.py` 的 `_latest` 缓存**仅进程内有效**，跨进程读不到最新版本，只能靠 glob + 排序兜底
- 无事务：写 `code_manifest` 与写 `implementation_summary` 是两次独立写盘，中间崩溃则产物集不一致
- 无查询能力：想回答"上一轮 review 的 blocking items 是什么"必须加载全部 JSON
- `run_state` 是字典塞进 JSON，无 schema 演进机制，加字段就得兼容旧文件

对单机演示够用，对"别人 clone 下来集成到自己系统"就不够用了——**开源项目的状态后端是一个 API 承诺，不是一个实现细节。**

#### 🟠 P1-D｜同步串行执行，并发能力已备未用

- `Leader.run()` 是 `while` 循环，单线程
- `mas/` 中**无 `async`/`await`**
- `concurrency.py:parallel_map()` 存在，但核心路径未使用；`ThreadPoolExecutor` 仅用于 AST 分析、MCP stdio 读取、前端工作线程
- 节点内多次 LLM 调用（协商 6 轮、工具调用 2 轮）串行等待

**成本后果**：一次 `max_steps: 40` 的运行，墙钟时间 ≈ Σ 所有 LLM 调用延迟。协商环节一次 6 轮问答就是 12 次串行调用。

**能力后果**：无法做"多候选方案并行生成再择优"，而这正是主流框架的标配（AutoGen 的 `n` 候选、Claude Agent SDK 的并行 subagent）。

#### 🟡 P2-E｜沙箱是"防手滑"，不是"防恶意"

已实现的（`sandbox.py`，确认有效）：
- ✅ 路径逃逸检查：`if self.root not in path.parents and path != self.root: raise SandboxError`
- ✅ 命令白名单：显式拒绝 `cmd/sh/bash/zsh/powershell/pwsh`，仅允许 `python/python3/py`（含版本后缀正则）
- ✅ 环境变量剥离：丢弃名称含 `KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL` 的变量
- ✅ rlimits：CPU/AS/FSIZE/NPROC（**仅 POSIX**；`threading.active_count() != 1` 时主动跳过以避免 `preexec_fn` 死锁）
- ✅ 网络导入静态检测：`network_import` AST 规则

**未实现的**：
- ❌ Windows 上完全无 rlimits（`_isolation_label()` 如实报告 `windows-no-rlimit`）
- ❌ 无文件系统写边界（LLM 生成的代码可以写 `C:\Windows\`）
- ❌ 无网络隔离（`import socket` 被静态标记，但 `__import__('so'+'cket')` 可绕过）
- ❌ 无内存/时间硬上限（Windows）
- ❌ 静态检测可被字符串拼接、`getattr` 动态调用绕过

诚实地说：**当前是"安全提示层"而非"安全边界"**。项目自己的 `_isolation_label()` 如实标注了这一点，这个诚实是好的，但不能替代真正的隔离。

#### 🟡 P2-F｜检索是关键词匹配，不是语义检索

`experience.py`：`_tokenize_for_match()`（英文按词、中文单字+双字）+ `_expand_with_synonyms()` + `_semantic_overlap()`（重叠系数 = 交集 / 较小集合）。

- 同义但非同形（"并发安全" vs "线程安全"）除非手工进同义词表否则检索不到
- 无嵌入、无向量、无 rerank
- 中文双字切分对长术语效果有限

**这是与主流框架差距最大的一项**。LangGraph 的 `BaseStore`、LlamaIndex 的 `VectorStoreIndex` 都把语义检索做成了基础设施。

#### 🟢 P3-G｜工程质量与开源就绪度

| 项 | 现状 | 开源要求 |
| --- | --- | --- |
| 测试 | `tests/` 2774 行、13 个文件，能跑通 | 覆盖率未知、无 CI 门禁 |
| CI | 无 | 必须 |
| LICENSE | 无 | 必须 |
| CONTRIBUTING / CODE_OF_CONDUCT | 无 | 必须 |
| SECURITY.md | 无 | 必须（这是执行用户代码的项目） |
| 打包 | 无 `pyproject.toml`，无 `mas/__main__.py` 入口声明 | 必须 |
| 类型检查 | 无 mypy/ruff 配置 | 建议 |
| 文档 | `docs/` 12 篇中文分析文档，质量高但**面向作者而非用户** | 需要用户向文档 |
| API 稳定性承诺 | 无 | 必须写明 |

### 1.5 既有文档的过时结论（需更正）

`docs/05-多Agent技术对标大厂分析.md` 中以下结论**已经被后续提交修复，不再成立**：

| 05 文档结论 | 当前事实 | 证据 |
| --- | --- | --- |
| "无函数调用能力" | 已实现完整工具调用闭环 | `base.py:_generate_with_tools()`、`toolrunner.py`、`llm.py` 流式聚合 `delta.tool_calls`；`system.yaml` 的 `tools_enabled` 默认 `false` 是配置开关而非能力缺失 |
| "权限未落地" | 已落地为硬守卫 | `base.py:permit()` 拦截 `can_write_code`/`can_write_tests`/`can_execute`/`can_rollback`/`can_block_acceptance`；`leader.py:_permission_allows()` 在门禁签字时校验 |
| "沙箱仅路径级" | 已扩展到命令白名单 + 环境变量剥离 + rlimits | `sandbox.py:_check_command()`、`_build_env()`、`_spawn_kwargs()`、`_isolation_label()` |

**仍成立的**：`reinforce()` 未接线、单进程串行、文件系统为状态、无语义检索、静态审查弱、测试覆盖率低。

---

## 2. 对标分析

### 2.1 能力矩阵

`●` 完整　`◐` 部分　`○` 缺失

| 能力维度 | 本项目 | LangGraph | CrewAI | AutoGen | OpenAI Agents SDK | Claude Agent SDK | Google ADK |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 编排模型 | 确定性状态机 `●` | 图 + 状态 `●` | 角色编排 `◐` | 对话驱动 `◐` | 交接链 `◐` | 自主循环 `◐` | 层级 Agent `◐` |
| 可复现性 | `●` 配置驱动 | `●` | `◐` | `○` | `◐` | `◐` | `◐` |
| 持久化/断点续跑 | `◐` 需重建 | `●` checkpointer | `◐` | `◐` | `○` | `◐` | `●` session |
| 人工介入 (HITL) | `○` | `●` interrupt | `●` | `●` | `◐` | `●` | `●` |
| 并发/并行 Agent | `○` | `●` | `●` | `●` | `●` | `●` subagent | `●` |
| 流式事件 API | `○` | `●` astream | `●` | `●` | `●` | `●` | `●` |
| 工具/函数调用 | `●` | `●` | `●` | `●` | `●` | `●` | `●` |
| MCP 支持 | `●` 自研客户端 | `●` 官方适配 | `◐` | `◐` | `●` | `●` 原生 | `●` |
| Skill 机制 | `●` SKILL.md | `○` | `○` | `○` | `○` | `●` 原生 | `○` |
| 记忆/经验 | `◐` 关键词 | `●` Store | `●` 多层记忆 | `◐` | `○` | `◐` | `●` Memory |
| 语义检索 | `○` | `●` | `●` | `○` | `○` | `○` | `●` |
| 沙箱隔离 | `◐` 本机 | `○` | `○` | `●` Docker | `◐` | `●` | `●` |
| 可观测性 | `◐` 自留 JSONL | `●` LangSmith | `●` | `◐` | `●` trace | `●` | `●` OTel |
| 人工评测/回归 | `◐` 门禁 | `●` eval | `◐` | `○` | `●` evals | `◐` | `●` evals |
| 多语言 SDK | `○` 仅 Python | `●` | `◐` | `●` | `●` | `●` | `●` |
| 依赖体量 | `●` 1 个 | 重 | 重 | 中 | 中 | 中 | 重 |

### 2.2 结论

**领先/持平项（3）**
1. **门禁 + 证据链**：本项目比所有对标框架都做得更硬。"不加证据不得通过"是产品化的正确方向。
2. **Skill 机制**：`SKILL.md` 6 字段规范 + 从成功运行蒸馏，与 Claude Agent SDK 的 Skill 思路同源，且更早落地了"仅在被明确要求时蒸馏"的克制。
3. **零依赖**：矩阵里唯一一个带 `●` 的依赖体量格。

**落后项（按修复优先级）**
1. **并发** → P1，阻塞"多候选并行择优"这个核心能力
2. **持久化** → P1，阻塞集成与被引用
3. **语义检索** → P2，阻塞"自学习"叙事的说服力
4. **沙箱** → P2，阻塞"敢让别人跑"的信任前提
5. **HITL / 流式 API** → P2，阻塞"能嵌进别人的产品"
6. **可观测性** → P2，阻塞线上排障

### 2.3 定位再思考：不要做"又一个通用框架"

对标不是"每一项都要追平"。开源市场的现实是：**通用多 Agent 框架已经过剩**，LangGraph/CrewAI/AutoGen 三家占住了生态位，后来者正面竞争没有胜算。

本项目的**唯一可行生态位**是：

> **"带证据链的、可审计的软件交付 Agent 流水线"**

不是"通用 Agent 编排框架"，而是**一个特定领域（需求 → 代码 → 测试 → 验收）的、以证据为验收前提的、可完全离线自持的交付系统**。

这决定了优先级：**门禁严谨性 > 交付质量 > 通用性 > 性能**。

诚实地讲，`# 2.1 能力矩阵` 里打的那些 `○`，有一部分（多语言 SDK、OTel 全家桶）**应该战略性地放弃**，而不是追平。而"并发"和"持久化"必须补——因为它们直接服务于本生态位的核心承诺（"可审计"要求状态可查、"可审计"要求并行任务的结果可合并）。

---

## 3. 重构目标与非目标

### 3.1 目标（可验收）

| # | 目标 | 验收标准 |
| --- | --- | --- |
| G1 | 消除运行时不安全 | 同一进程内并发 `run()` 两个不同需求，产物/事件/消息**零串扰**（写成测试） |
| G2 | 经验闭环真实闭合 | 注入经验后若导致回退，`reinforce()` 被调用并下调置信度；写成端到端测试 |
| G3 | 状态后端可替换 | 同一套上层代码在 `FileStore` 与 `SQLiteStore` 下行为一致（参数化测试） |
| G4 | 并发能力可用 | 支持"N 个候选并行生成 + 择优选入"，墙钟时间 ≈ 单候选而非 N 倍 |
| G5 | 隔离强度可选 | 配置切换 `local`/`docker` executor，Docker 模式下 LLM 代码无法写宿主 FS |
| G6 | 可嵌入 | 提供 Python SDK：`from mas import Pipeline; Pipeline(config).run(req)`；提供 HTTP/SSE 服务端 |
| G7 | 可观测 | 结构化 trace 导出（OTel 兼容格式）+ 每次运行的 token/成本/延迟汇总 |
| G8 | 开源就绪 | LICENSE/CI/CONTRIBUTING/SECURITY/pyproject 齐全；`pip install` 可用；主线测试通过率 100% |

### 3.2 非目标（明确不做，写进 README）

- ❌ **不做通用 Agent 框架**：不追求"A 也能做、B 也能做"，专注软件交付领域
- ❌ **不做 GUI 优先**：保留现有前端作为可视化面板，不投入做 IDE 级交互
- ❌ **不做云托管服务**：保持本地优先（local-first），不做 SaaS
- ❌ **不做多语言 SDK**：先只做 Python；用 HTTP API 让其他语言接入
- ❌ **不做微服务化**：单进程部署是一等公民，分布式是 P3+ 以后的事
- ❌ **不做 LLM 训练/微调**：只做编排，不做模型

**明确写下非目标，是开源项目最重要的一页。** 它决定了别人不会因为"你们怎么没有 X"而来提 issue。

---

## 4. 目标架构

### 4.1 分层视图

```
┌─────────────────────────────────────────────────────────────────────┐
│  接口层  CLI │ HTTP/SSE Server │ Python SDK │ Frontend Panel         │
├─────────────────────────────────────────────────────────────────────┤
│  编排层  Pipeline (状态机) │ Gate (门禁) │ Router (回退/升级)          │
│          Scheduler (并发调度) │ Checkpoint (断点)                     │
├─────────────────────────────────────────────────────────────────────┤
│  执行层  Agent Runtime (统一上下文) │ Agent 实现 (5 个角色)            │
│          ToolRunner (函数调用) │ Negotiation (协商) │ Reflection      │
├─────────────────────────────────────────────────────────────────────┤
│  能力层  Storage │ Memory/Experience │ Sandbox Executor │ LLM Provider│
│          (抽象接口 + 多种实现，全部可插拔)                            │
├─────────────────────────────────────────────────────────────────────┤
│  观测层  EventBus (流式) │ Trace │ Metrics │ Evidence Chain           │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 核心抽象（接口草案）

```python
# mas/core/contracts.py

class Runtime(Protocol):
    """一次运行的全部上下文。显式传递，绝不用类属性/全局。"""
    run_id: str
    config: SystemConfig
    store: ArtifactStore          # 取代 Blackboard 的裸 FS
    events: EventSink             # 可订阅，取代只写 jsonl
    bus: MessageBus
    memory: MemoryLayer           # 取代 ExperienceLibrary 单一实现
    sandbox: Sandbox              # 抽象，实现可为 local/docker/remote
    llm: LLMClient
    clock: Clock                  # 可注入，便于测试

class ArtifactStore(Protocol):
    """版本化产物存储。FileStore / SQLiteStore / PostgresStore 均可实现。"""
    async def put(self, key: str, value: dict, *, by: str) -> str: ...
    async def get(self, key: str, version: str | None = None) -> dict | None: ...
    async def history(self, key: str) -> list[VersionRef]: ...
    async def query(self, key_prefix: str, predicate: Callable) -> list[dict]: ...

class Node(Protocol):
    """状态机节点。与 Agent 解耦：一个节点可以不含 LLM。"""
    id: str
    async def execute(self, runtime: Runtime, state: RunState) -> NodeOutcome: ...

class Executor(Protocol):
    """Agent 执行器。串行 / 并行 / 竞速，不同策略不同实现。"""
    async def execute(self, node: Node, state: RunState) -> NodeOutcome: ...

class ArtifactReader(Protocol):
    """感知接口。取代 collaboration.yaml 的静态 perception 矩阵。"""
    async def perceive(self, role: str, keys: list[str]) -> Perception: ...
```

**关键设计原则：一切运行时状态都通过参数传递，一切能力都通过 Protocol 注入。**

### 4.3 目标目录结构

```
mas/
├── __init__.py            # 公开 API 导出（Pipeline, Config, Runtime）
├── __main__.py            # python -m mas
├── cli.py
├── core/
│   ├── contracts.py       # 所有 Protocol 定义 ← 新增，最重要
│   ├── runtime.py         # Runtime 实现 ← 新增
│   ├── pipeline.py        # 状态机（从 leader.py 拆出）
│   ├── scheduler.py       # 并发调度 ← 新增
│   ├── gate.py            # 门禁求值（从 leader.py 拆出）
│   ├── router.py          # 回退/升级（从 leader.py 拆出）
│   └── checkpoint.py      # 断点续跑 ← 新增
├── agents/                # 保留，基类改为接收 Runtime
├── storage/
│   ├── base.py            # ArtifactStore Protocol
│   ├── file_store.py      # 默认：兼容现有 workspace/ 布局
│   └── sqlite_store.py    # 可选：带索引与事务 ← 新增
├── memory/
│   ├── base.py            # MemoryLayer Protocol
│   ├── keyword.py         # 现有实现，改名保留
│   └── hybrid.py          # 关键词 + 向量 ← 新增（可选依赖）
├── execution/
│   ├── base.py            # Executor Protocol
│   ├── local.py           # 本机子进程（现有 sandbox 逻辑）
│   └── docker.py          # 容器隔离 ← 新增（可选依赖）
├── observability/
│   ├── trace.py           # OTel 兼容导出 ← 新增
│   └── metrics.py         # token/成本/延迟 ← 新增
├── extensions/            # skills.py + mcp.py + toolrunner.py 合并
└── expr.py                # 保留（AST 白名单，这是资产）
```

### 4.4 关键架构决策（ADR）

#### ADR-1：保留确定性状态机，不引入 LLM 动态路由

**决策**：`flow.yaml` 的状态机模型**原样保留**，仅在实现上重构。

**理由**：这是项目最核心的差异化。LangGraph 用 `Command(goto=...)` 让 LLM 决定下一步，CrewAI 的 hierarchical 让 manager LLM 分配任务——两者在生产环境都有"跑飞"问题。本项目的门禁-回退模型是**可证明有界的**（`max_steps` + `max_total_rollbacks` 双重兜底）。

**代价**：灵活性不如 LLM 路由，遇到 flow.yaml 未描述的分支需要改配置。

**取舍**：接受。领域固定（软件交付），流程本身就是已知的。

---

#### ADR-2：引入 `asyncio`，但保持同步 API 兼容

**决策**：内核改为 `async`，但对外暴露 `run()` 同步封装（内部 `asyncio.run`）。

**理由**：
- 并发是 G4 的前提。当前 `while` 循环 + 同步 HTTP 让并行无从下手
- 但破坏同步 API 会造成无谓的迁移成本，且 CLI 用户不需要 async

**实现**：

```python
class Pipeline:
    async def arun(self, requirement: str, *, as_text=False) -> RunSummary:
        ...   # 真正的实现

    def run(self, requirement: str, *, as_text=False) -> RunSummary:
        return asyncio.run(self.arun(requirement, as_text=as_text))  # 同步门面
```

**风险**：`sandbox.py` 的 rlimits 用 `preexec_fn`，与 `asyncio` 的 subprocess 有交互。**缓解**：`asyncio.create_subprocess_exec` 不经过线程池，反而消除了当前"多线程下跳过 rlimits"的问题——这是一个净收益。

---

#### ADR-3：消灭类属性运行时绑定

**决策**：`Leader._current_*` 全部删除，改为构造注入的 `Runtime` 对象。

**理由**：P0-A，隐性正确性缺陷。

**迁移**：

```python
# Before
class Leader:
    _current_bus = None
    def _bind_runtime(self, ctx, bus, events, agents): ...

# After
class Pipeline:
    def __init__(self, config): ...
    async def arun(self, requirement, *, as_text=False):
        runtime = Runtime(run_id=..., config=self.config, store=..., events=..., ...)
        # runtime 作为参数在调用链中传递，无共享可变状态
```

**验证**：新增测试 `test_concurrent_runs_isolated`——并发跑两个需求，断言两者的 events/messages/artifacts 完全隔离。

---

#### ADR-4：状态后端抽象，默认仍是文件系统

**决策**：定义 `ArtifactStore` Protocol，默认实现 `FileStore` **保持现有 `workspace/` 目录布局不变**。

**理由**：
- 现有布局（`v1.json`/`v2.json`/`latest.json` + sha256 + file_lock）设计是好的，没必要推翻
- 但必须有抽象层，否则外部集成者只能 fork
- 保持布局不变 → 已有 workspace 数据无需迁移，降低重构风险

**新增 `SQLiteStore`**（P1）带来：事务性多产物写入、按条件查询、"上一轮 review 的 blocking items"这类问题不用全量加载、跨进程 `latest` 语义正确（不再依赖进程内缓存）。

**配置**：

```yaml
runtime:
  store:
    backend: file        # file | sqlite
    path: workspace
```

---

#### ADR-5：混合检索，向量能力为可选依赖

**决策**：`MemoryLayer` 抽象 + `keyword.py`（现有逻辑）+ `hybrid.py`（关键词召回 → 向量重排）。

**理由**：P2-F。但**不能因此把 PyYAML 之外的依赖变成强制项**——向量模型下载几个 GB，会毁掉"零依赖便携"的资产。

**方案**：

```toml
# pyproject.toml
[project.optional-dependencies]
hybrid = ["sentence-transformers>=2.0"]   # 可选
docker = ["docker>=7.0"]                   # 可选
otel   = ["opentelemetry-sdk>=1.20"]       # 可选
all    = [...]
```

核心包仍只有 `PyYAML`。**用户不装可选依赖时，系统自动降级到关键词检索，功能不减只是效果差。**

这是对 §1.3 第 6 项资产的正解：**保留零依赖的能力，而不是保留零依赖的教条。**

---

#### ADR-6：Executor 抽象，Docker 隔离作为可选

**决策**：`Sandbox` 拆成 `Executor` Protocol，实现 `LocalExecutor`（现有）+ `DockerExecutor`。

**理由**：P2-E。当前隔离是"防手滑"级。要让别人敢 clone 下来跑，必须能选择真隔离。

```yaml
runtime:
  sandbox:
    executor: local        # local | docker
    docker:
      image: python:3.12-slim
      network: none
      memory_limit: 512m
      pids_limit: 64
      read_only_root: true
      mount: rw:/workspace
```

**注意**：现有代码里 `_isolation_label()` 如实上报实际隔离级别的做法**必须保留并在文档里宣传**——诚实标注隔离强度是安全产品的基本素养，很多项目做不到。

---

#### ADR-7：依赖策略——"核心零依赖，能力按需扩展"

**决策**：正式确立依赖分层策略，写进 README 与 `pyproject.toml`。

```
核心（必须）：PyYAML
标准（推荐）：无
可选（按能力）：
  hybrid → sentence-transformers
  docker → docker
  otel   → opentelemetry-sdk
  server → 无（用标准库 http.server / 或 fastapi 可选）
```

**理由**：见 ADR-5。零依赖是差异化资产，但让它决定架构能力就是本末倒置。

---

#### ADR-8：经验反馈闭环接线

**决策**：把 `reinforce()` 接进 `Pipeline` 的回退路径。

**设计**：

```python
# core/router.py 的 rollback 分支
async def on_rollback(self, runtime, from_node, to_node, reason):
    injected = runtime.state["injected_experience_ids"]
    if injected:
        # 这些经验没能阻止回退 → 置信度下调
        await runtime.memory.reinforce(injected, outcome="negative", evidence=reason)

# core/pipeline.py 的 terminal 分支
async def on_terminal(self, runtime, verdict):
    injected = runtime.state["injected_experience_ids"]
    outcome = "positive" if verdict == "pass" else "neutral"
    await runtime.memory.reinforce(injected, outcome=outcome, evidence=runtime.summary)
```

**同时修正 `promote()` 的晋升依据**：从纯 `hits` 改为 `hits × success_rate`。当前"使用次数多就晋升"会把反复导致返工的经验推上 L3，是自学习的反模式。

---

## 5. 分阶段路线图

### P0：止血与立信（3 周）

> 目标：让仓库先变成一个"可以放心给别人看"的仓库。

| 任务 | 产出 | 验收 |
| --- | --- | --- |
| P0.1 修 `Leader` 类属性绑定 | `Runtime` 显式传递 | 并发隔离测试通过 |
| P0.2 接线 `reinforce()` | 回退/终态双路径调用 | 端到端测试：注入坏经验 → 置信度下降 |
| P0.3 修 `promote()` 晋升依据 | `hits × success_rate` | 单元测试 |
| P0.4 CI 上线 | GitHub Actions：Windows + Ubuntu × Py3.10/3.11/3.12 | 全绿 |
| P0.5 覆盖率基线 | pytest-cov，门槛设为**当前值**（先立信再提升） | 报告可查 |
| P0.6 开源基础设施 | `LICENSE`(Apache-2.0)、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`、`CHANGELOG.md` | 齐全 |
| P0.7 打包 | `pyproject.toml`、`mas/__main__.py`、`pip install -e .` 可用 | 新环境验证 |
| P0.8 更正文档 05 | 标注已过时结论（见 §1.5） | 无错误声明 |

**为什么 P0 要先做开源基础设施**：因为 CI、LICENSE、贡献指南是"参与门槛"，越早建越好；且 P1/P2 的破坏性改动需要 CI 来兜底。

**为什么 LICENSE 选 Apache-2.0**：含明确的专利授权条款，企业采用顾虑最小。若希望更宽松可选 MIT，但**不要选 GPL**——会挡住商业采用。

---

### P1：内核解耦（4 周）

> 目标：把"运行时状态"和"能力实现"从代码里分离出来。

| 任务 | 产出 | 验收 |
| --- | --- | --- |
| P1.1 定义 `core/contracts.py` | 全部 Protocol | 类型检查通过 |
| P1.2 拆 `leader.py` | `pipeline.py`(状态机) + `gate.py` + `router.py` | 行为等价（回归测试对比 events.jsonl） |
| P1.3 引入 `asyncio` | `arun()` 内核 + `run()` 同步门面 | 现有测试全绿 |
| P1.4 `ArtifactStore` 抽象 | `storage/file_store.py`（保持布局）+ `sqlite_store.py` | 参数化测试两后端行为一致 |
| P1.5 `Executor` 抽象重构 | `execution/local.py` | 行为等价 |
| P1.6 并发调度 | `core/scheduler.py` + `Executor` 并行实现 | 多候选并行择优，墙钟 ≈ 单候选 |
| P1.7 感知解耦 | `ArtifactReader.perceive()` 取代静态矩阵 | 保持 `collaboration.yaml` 兼容 |

**P1.2 的行为等价怎么做**：拿现有 `workspace/runs/` 里的历史运行作为 golden file，重构后重放同一需求，比对 `events.jsonl` 的 `(event_type, agent, node, iteration)` 序列。**这是整个重构最关键的安全网。**

---

### P2：能力对标（4 周）

> 目标：把 §2.1 能力矩阵里的关键 `○` 补上。

| 任务 | 产出 | 验收 |
| --- | --- | --- |
| P2.1 混合检索 | `memory/hybrid.py` + 可选依赖 | 检索召回率对比基线提升可量化 |
| P2.2 Docker Executor | `execution/docker.py` | 逃逸测试：LLM 代码无法写宿主 |
| P2.3 EventBus 流式 | `events.subscribe()` + SSE 端点 | `curl -N` 能看到实时事件 |
| P2.4 可观测性 | `observability/trace.py` + `metrics.py` | 导出 OTel 格式；每次运行输出 token/成本/延迟 |
| P2.5 HITL | `interrupt`：门禁处可挂起等人确认 | 测试：挂起 → 恢复 → 继续 |
| P2.6 Checkpoint | 断点续跑：崩溃后从最后节点恢复 | 测试：中途 kill → 重启续跑一致 |
| P2.7 静态审查增强 | `reviewer.py` 接入更多规则（复杂度、重复、类型） | 覆盖现有实测 task 的缺陷样例 |

**P2.5 HITL 为什么重要**：这是"能嵌进别人产品"的必要条件。企业对自动化流水线的接受度，与"能不能在关键节点喊停"直接相关。当前 `acceptance` 门禁是一个天然的人工确认点。

---

### P3：开放生态（3 周）

| 任务 | 产出 | 验收 |
| --- | --- | --- |
| P3.1 Python SDK | `from mas import Pipeline` 稳定 API | 示例 notebook |
| P3.2 HTTP/SSE 服务端 | `mas serve` | OpenAPI 文档 |
| P3.3 插件体系 | 自定义 Node / Agent / Store / Executor 的注册机制 | 文档 + 示例插件 |
| P3.4 文档站 | mkdocs-material，按"用户/贡献者/架构"三分 | 部署到 GitHub Pages |
| P3.5 示例库 | 3 个不同领域的需求（Web/CLI/数据处理） | 端到端可跑 |
| P3.6 性能基准 | `benchmarks/`：token 成本、墙钟、并发度 | 数字进 README |

---

### 里程碑总览

```
周  1  2  3 │ 4  5  6  7 │ 8  9 10 11 │ 12 13 14
    P0 ██████│ P1 ████████│ P2 ████████│ P3 ███████
            ↑            ↑            ↑
        v0.2.0       v0.3.0       v0.4.0    v1.0.0
      开源可发布   内核解耦     能力对标   生态就绪
```

**每个里程碑都是一次 release。** 任何阶段结束时中止，仓库都处于可发布状态——这是渐进式重构的核心纪律。

---

## 6. 开源就绪检查清单

### 6.1 仓库根目录必备

```
multi_agent/
├── LICENSE                    ← Apache-2.0
├── README.md                  ← 重写：30 秒看懂 + 5 分钟跑起来
├── README.zh-CN.md            ← 中文版（项目原始语言）
├── CONTRIBUTING.md            ← 开发环境、测试、PR 流程、代码风格
├── CODE_OF_CONDUCT.md         ← Contributor Covenant 2.1
├── SECURITY.md                ← 漏洞上报流程（本项目执行 LLM 生成的代码，必须有）
├── CHANGELOG.md               ← Keep a Changelog 格式
├── GOVERNANCE.md              ← 决策流程、维护者角色（小项目也要写，体现严肃性）
├── ROADMAP.md                 ← 指向本方案 §5
├── pyproject.toml             ← 打包 + 工具配置
├── .gitignore / .editorconfig
├── .github/
│   ├── workflows/ci.yml
│   ├── workflows/release.yml
│   ├── ISSUE_TEMPLATE/{bug,feature}.yml
│   └── PULL_REQUEST_TEMPLATE.md
├── docs/
│   ├── index.md               ← 用户向
│   ├── quickstart.md
│   ├── concepts/{pipeline,gates,memory,sandbox}.md
│   ├── guides/{custom-node,mcp-integration,docker-executor}.md
│   ├── architecture/          ← 现在的 01-13 移到这里，标注为"设计文档"
│   └── blog/                  ← 可选：把 05/07/11 这些分析文章改写成博客
├── examples/
└── benchmarks/
```

**一个关键动作**：现在 `docs/` 里 13 篇高质量中文分析文档，是**面向作者**的（缺陷排查、阶段性汇报）。开源后必须区分：
- **用户向文档**（怎么写配置、怎么加 Agent）→ 新写
- **设计文档**（为什么这么设计）→ 现有文档改造后移入 `docs/architecture/`
- **工程日志**（07/11 这类排查报告）→ 改写成 `docs/blog/` 的技术博客，这部分**反而是最好的营销素材**——"我们如何发现并修复了 N 个致命缺陷"比功能介绍有说服力得多

### 6.2 README 结构（重写建议）

```markdown
# MAS · 多 Agent 软件交付流水线

> 用确定性状态机编排 5 个 Agent，把需求变成**带证据链的**验收报告。
> 核心包零第三方依赖（除 PyYAML）。

<p align="center"><视频/GIF：一次完整运行的事件流></p>

## 为什么是它
- 🎯 确定性编排 —— 流程可复现、成本可预测
- 🔒 不加证据不得通过 —— 门禁表达式 + 测试数量 + 协商闭环
- 📜 完整证据链 —— events / messages / prompts / artifacts / traces 全留痕
- 🔌 MCP + Skill 扩展 —— 已实现，非规划中
- 📦 零依赖核心 —— `pip install mas-agent`，无 venv 也能跑

## 5 分钟上手
（三条命令 + 输出截图）

## 与主流框架的差异
（直接引用 §2.1 矩阵，并说明各自适用场景——不要踩别人）

## 架构
（一张图）

## 文档 / 贡献 / 许可证
```

**README 的第一屏决定 90% 的 star。** 必须有一个 GIF 或 asciinema 录屏。

### 6.3 CI 策略

```yaml
# .github/workflows/ci.yml
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        python: ["3.10", "3.11", "3.12", "3.13"]
        extras: ["", ".[all]"]      # 验证零依赖与全依赖两条路径
    steps:
      - ruff check
      - mypy mas               # 先设宽松，逐步收紧
      - pytest --cov --cov-report=xml
      - pytest -m "not llm"    # 离线 provider 路径
```

**关键**：`llm` 标记的测试（需要真实 API）在 CI 中跳过，但用 `offline` provider 覆盖主流程。当前 `tests/` 已经这么做了（能看到离线 provider 跑通 7~11 步）。

### 6.4 版本与兼容性承诺

```markdown
## 稳定性承诺
- `mas.core.contracts` 中的所有 Protocol：v1.0 后遵循 SemVer
- `flow.yaml` / `system.yaml` / `collaboration.yaml` 配置 schema：向后兼容，废弃字段保留两个 minor 版本并告警
- `workspace/` 目录布局：v1.0 后不破坏性变更（FileStore）
- CLI 参数：不破坏性变更
- 内部模块（`mas.agents.*` 的实现细节）：不承诺
```

**明确"什么承诺、什么不承诺"，比含糊地承诺一切更有价值。**

---

## 7. 风险与权衡

| 风险 | 概率 | 影响 | 缓解 |
| --- | --- | --- | --- |
| 异步化引入新并发 bug | 中 | 高 | P1.2 的 golden file 重放 + 并发隔离测试；异步化与拆解分两个 PR |
| `sqlite_store` 引入行为差异 | 中 | 中 | 参数化测试强制两后端行为一致；默认仍用 file |
| 向量依赖毁掉"零依赖"卖点 | 高 | 中 | 严格可选（ADR-5）；README 明确标注核心依赖表 |
| Docker 依赖在 CI/Windows 不可用 | 高 | 低 | Docker Executor 测试标记 `@pytest.mark.docker`，CI 单独 job |
| 拆分 `leader.py` 引入回归 | 中 | 高 | golden file 重放；一次只拆一个关注点 |
| 开源后 issue 爆炸 | 中 | 中 | 明确非目标（§3.2）；提供 issue 模板；先关 issue 只收 PR 也可 |
| 门禁过严导致演示跑不通 | 中 | 中 | 保留 `lean` profile；演示路径用配置放宽而非改代码 |

### 最重要的权衡：**广度 vs 深度**

主线是**深度**。对标不是"补齐矩阵里的每个 `○`"，而是"在选定的生态位（可审计的软件交付流水线）上做到别人做不到"。

具体地说：**宁可把"门禁 + 证据链 + 经验闭环"做到业界最严，也不要把"多语言 SDK + 分布式调度 + 云托管"做到业界平均水平。**

---

## 8. 保留 / 替换 决策表

| 组件 | 决策 | 理由 |
| --- | --- | --- |
| `expr.py` AST 白名单求值 | ✅ **保留原样** | 资产，无可改进 |
| `flow.yaml` 状态机模型 | ✅ **保留原样** | 差异化核心 |
| 门禁 + 回退 + 升级策略 | ✅ **保留，实现拆分** | 差异化核心 |
| `tester.py` 的证据约束 | ✅ **保留，作为范式推广** | 最好的设计 |
| `sandbox.py` 的 AST 静态分析 | ✅ **保留，扩展规则** | 有价值 |
| `sandbox.py` 的隔离机制 | 🔄 **抽象为 Executor，新增 Docker** | 强度不足 |
| `experience.py` 分层 + action_key | ✅ **保留设计** | 设计好 |
| `experience.py` 的关键词检索 | 🔄 **保留为默认，新增 hybrid** | 效果不足但零依赖 |
| `reinforce()` | 🔧 **接线** | 死代码，闭环缺口 |
| `promote()` 晋升依据 | 🔧 **改为 hits × success_rate** | 当前会晋升反模式经验 |
| `leader.py` | 🔨 **拆成 pipeline/gate/router** | 上帝对象 |
| `Leader._current_*` 类属性 | ❌ **删除** | 隐性正确性缺陷 |
| `blackboard.py` | 🔄 **抽象为 ArtifactStore，FileStore 保持布局** | 无查询、无事务 |
| 同步 `while` 循环 | 🔄 **改 async，保留同步门面** | 无并发 |
| `concurrency.py` | ✅ **保留，接入调度器** | 已备未用 |
| `bus.py` / `events.py` | ✅ **保留，事件增加订阅能力** | 设计好（事件溯源） |
| `mcp.py` / `skills.py` / `toolrunner.py` | ✅ **保留，合并进 `extensions/`** | 已实现且正确 |
| `prompts.py` | ✅ **保留** | — |
| `frontend/` | ✅ **保留，后续接 SSE** | 可视化有价值 |
| `cli.py` | ✅ **保留，随 API 演进增补命令** | — |
| 零依赖策略 | 🔄 **降级为核心零依赖 + 可选扩展** | 从教条变为策略 |

---

## 9. 立即可执行的前三件事

如果只能做三件事，做这三件：

### 第一件：修 `Leader` 类属性绑定（1~2 天）

这是唯一的**隐性正确性缺陷**。它不是"能力不够"，是"看起来对但其实会错"。开源项目里这类问题最伤信任——因为你无法在 review 时看出来。

### 第二件：接线 `reinforce()`（1~2 天）

`promote()` 只按 `hits` 晋升，会让"反复注入但每次都导致返工"的经验爬到 L3。这不是"没做完"，是"做反了"。修好之后，项目最核心的叙事（自学习闭环）才第一次真正成立。

### 第三件：建 CI + 打 LICENSE（1 天）

一天的工作，但它把项目从"我的项目"变成"一个项目"。而且它是后面所有重构的安全网——没有 CI，P1 的破坏性改动无从验证。

---

## 附录 A：接口签名草案

```python
# mas/__init__.py —— 公开 API 面（v1.0 承诺 SemVer）
from .core.pipeline import Pipeline
from .core.contracts import (
    Runtime, ArtifactStore, Node, Executor, MemoryLayer, Sandbox, EventSink,
)
from .config import SystemConfig, load_config

__all__ = [
    "Pipeline", "SystemConfig", "load_config",
    "Runtime", "ArtifactStore", "Node", "Executor",
    "MemoryLayer", "Sandbox", "EventSink",
]
__version__ = "1.0.0"
```

```python
# mas/core/pipeline.py
class Pipeline:
    def __init__(
        self,
        config: SystemConfig,
        *,
        store: ArtifactStore | None = None,     # None → 按 config.runtime.store 构建
        executor: Executor | None = None,
        memory: MemoryLayer | None = None,
        sandbox: Sandbox | None = None,
        llm: LLMClient | None = None,
    ) -> None: ...

    async def arun(self, requirement: str, *, as_text: bool = False,
                   run_id: str | None = None) -> RunSummary: ...

    def run(self, requirement: str, *, as_text: bool = False,
            run_id: str | None = None) -> RunSummary:
        """同步门面。CLI 与简单集成用这个。"""

    def stream(self, requirement: str, **kw) -> Iterator[Event]:
        """流式事件迭代器。前端与可观测用这个。"""

    async def resume(self, run_id: str, *, from_node: str | None = None) -> RunSummary:
        """断点续跑。"""
```

```python
# mas/core/contracts.py
class MemoryLayer(Protocol):
    async def retrieve(self, *, role: str, tags: list[str], task_text: str,
                       tier_keys: list[str], budget_tokens: int) -> list[Experience]: ...
    async def add(self, entry: Experience) -> str: ...
    async def promote(self) -> int: ...
    async def reinforce(self, ids: list[str], *, outcome: Literal["positive","negative","neutral"],
                        evidence: Any) -> None: ...     # ← P0 接线
    async def decay(self, run_id: str) -> int: ...
```

```python
# mas/core/contracts.py
class Sandbox(Protocol):
    async def write(self, rel_path: str, content: str) -> Path: ...
    async def read(self, rel_path: str) -> str: ...
    async def run(self, cmd: list[str], *, env: dict | None = None,
                  timeout_s: int | None = None,
                  result_marker: str | None = None) -> RunResult: ...
    def analyze(self, rel_path: str) -> list[Finding]: ...
    def isolation_label(self) -> str: ...   # ← 保留，如实上报
```

---

## 附录 B：本方案的判断依据（关键代码位置）

| 结论 | 证据位置 |
| --- | --- |
| 类属性运行时绑定 | `mas/leader.py` `_current_bus` / `_bind_runtime()` |
| `reinforce()` 死代码 | `mas/experience.py:450` 定义；全仓无调用者 |
| 同步串行主循环 | `mas/leader.py` `run()` 的 `while` 循环 |
| `mas/` 无 async | 全目录 grep 无 `async def` / `await` |
| 并发能力已备未用 | `mas/concurrency.py:parallel_map()`；调用点仅 AST 分析 / MCP 读取线程 / 前端线程 |
| `_latest` 仅进程内 | `mas/blackboard.py` |
| 门禁 AST 白名单 | `mas/expr.py` |
| 证据约束 | `mas/agents/tester.py` `result.status = "pass" if ...` |
| 命令白名单 | `mas/sandbox.py` `_check_command()` |
| 环境变量剥离 | `mas/sandbox.py` `_build_env()` |
| rlimit 平台条件 | `mas/sandbox.py` `_spawn_kwargs()` / `_isolation_label()` |
| 工具调用闭环 | `mas/agents/base.py` `_generate_with_tools()`；`mas/toolrunner.py` |
| 权限守卫已落地 | `mas/agents/base.py` `permit()`；`mas/leader.py` `_permission_allows()` |
| 经验提炼规则 | `mas/experience.py` `extract_from_events()`（5 条规则） |
| Skill 六字段规范 | `mas/skills.py` `validate_skill()` |
| MCP 真实实现 | `mas/mcp.py`（stdio/sse 手写 JSON-RPC） |
| 测试规模 | `tests/` 2774 行 / 13 文件；`mas/` 9986 行 |
| 依赖 | `requirements.txt`：`PyYAML>=6.0` |

---

## 结语

这个项目最难得的地方，是它在**没有人要求的情况下**做出了"不加证据不得通过"这个约束。

绝大多数 LLM Agent 项目的失败模式是相同的：Agent 兴高采烈地宣布"任务完成"，而实际上什么都没验证。这个项目用门禁表达式、测试数量下限、协商闭环、验证缺口检测四道闸门把这条路堵死了。

**重构的目标不是让它变成 LangGraph，而是让这些正确的设计意图，能够被一个陌生人 clone 下来、读懂、扩展、信任。**

分布式、多语言、云托管——这些可以慢慢来。但"并发跑两个需求不会串数据"、"注入的坏经验会被降权"、"状态可以换后端"——这些是三周内必须修好的。

---

*本方案基于 2026-10-14 的代码快照。所有结论附代码位置，可复核。*
