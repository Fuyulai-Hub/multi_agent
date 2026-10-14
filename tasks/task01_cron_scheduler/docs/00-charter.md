# 00 · 任务章程（Task Charter）

> 角色：本文件是端到端任务的最高约束，由编排者（orchestrator）制定。
> 后续需求、设计、实现、测试、验收各阶段产物不得与本文件冲突；若需变更，必须记录变更理由并回写本文件。
> 制定时间：2026-10-14 · 任务编号：task01_cron_scheduler

---

## 1. 任务定义

**选定任务：定时任务调度系统（Cron Scheduler）**（候选另有「C++ 智能指针实现」「简易电商系统」）。

选型理由：

1. 可用 Python 纯标准库完整实现，零第三方依赖，符合团队工程约定；
2. 验收可 100% 自动执行（单元测试 + 虚拟时钟模拟 + CLI 子进程 + 自检 demo），无需人工判断；
3. 覆盖工程难点全面：文法解析、时间计算、并发执行、失败重试、持久化恢复，失败模式丰富，足以演练闭环迭代；
4. 不依赖本机 C++ 编译工具链，环境风险低（本机无 g++/MinGW，MSVC 需手工注入环境变量）。

## 2. 范围（Scope）

### 2.1 必须交付（In Scope）

| # | 功能 | 说明 |
|---|---|---|
| F1 | cron 表达式引擎 | 5 段标准语法解析 + 下一次触发时刻计算 |
| F2 | 触发器 | CronTrigger / IntervalTrigger 两种 |
| F3 | Job 模型与函数注册表 | 任务配置 + 按名引用可调用对象 |
| F4 | JSON 持久化 | 任务配置与 next_run_time 落盘，原子写 |
| F5 | 调度器主循环 | 虚拟时钟可测；真实时钟可运行 |
| F6 | 执行器 | 线程池并发、超时、指数退避重试 |
| F7 | 运行记录 | 每次执行一条 JSONL 记录 |
| F8 | CLI | add / list / remove / pause / resume / run 六个子命令 |
| F9 | demo.py | 虚拟时钟 24h 自检演示（自带期望值断言） |
| F10 | acceptance.py | 自动验收脚本（退出码即门禁） |
| F11 | 测试套件 | unittest，≥ 40 用例，全绿 |
| F12 | 文档 | 需求 / 设计 / 实现说明 / 测试报告 / 修复日志 / 验收报告 / 终报 |

### 2.2 明确不做（Out of Scope）

分布式/多进程调度、Web UI、认证授权、时区数据库（统一 naive 本地时间）、第三方依赖、注册为操作系统服务。

## 3. 环境与工程约定

| 项 | 约定 |
|---|---|
| Python 解释器 | `C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe`（实测 3.13.14） |
| 依赖 | 仅标准库；**禁止 pip install 任何东西** |
| 工作根目录 | `G:/mycv/cragent/multi_agent/tasks/task01_cron_scheduler/`（下称「任务根目录」） |
| 仓库隔离 | 不得修改本仓库既有文件（mas/、config/、docs/、frontend/、tests/ 等），只允许在 tasks/task01_cron_scheduler/ 下新增文件 |
| 字符编码 | 代码、标识符、测试名、程序 stdout 一律 ASCII；中文仅用于 Markdown 文档 |
| 测试框架 | unittest（标准库） |
| 测试运行方式 | 任务根目录执行 `$PY -m unittest discover -s tests -v` |
| 时间语义 | naive datetime（本地时间），分钟粒度 |
| 阶段产物 | 每阶段落盘为文件 + 向编排者回报摘要，关键信息不得只停留在对话中 |

## 4. 关键语义决策（需求级，S1–S12）

> 这些是**需求级**决策，设计阶段不得反转，只能细化实现。测试与验收以本节为准。

- **S1 cron 语法**：5 段（分 时 日 月 周），字段支持 `*`、单值 `a`、范围 `a-b`、步长 `*/n` 与 `a-b/n`、列表 `a,b,c` 及上述组合；月份名 JAN–DEC、星期名 SUN–SAT（大小写不敏感，周日可取 0 或 7）；支持宏 `@daily` `@hourly` `@weekly` `@monthly` `@yearly`。
- **S2 DOM/DOW 关系**：按 Vixie cron 规则——日(DOM)与周(DOW)均被限制（均非 `*`）时二者为「或」，否则为「与」。
- **S3 next_after(dt)**：返回严格晚于 dt 的下一个匹配时刻（分钟粒度，dt 的秒以下部分忽略后从下一分钟起算）；若 4 年内无匹配（如 `0 0 30 2 *`）抛 `NoMatchError`。
- **S4 IntervalTrigger(seconds)**：首次触发在「加入时刻 + interval」；持久化恢复后按落盘的 next_run_time 继续，不重算。
- **S5 misfire 策略**（仅在 `lateness = now - scheduled_time > misfire_grace` 时生效；lateness ≤ grace 按正常迟到运行：执行一次并推进一个周期）：
  - `discard`：不运行，next_run_time 直接跳到未来下一个匹配时刻；
  - `coalesce`：每次 tick 为**最早一个**错过周期运行一次，之后 next_run_time 前进一个周期（排水式，直到追平当前时刻）；
  - `fire_once`：仅为最早一个错过周期运行一次，随后 next_run_time 跳到未来下一个匹配时刻。
- **S6 超时**：timeout 到点即放弃等待并记录 `status="timeout"`；底层线程作为 daemon 继续执行（已知限制，必须在设计文档与 README 中明示）；超时计入一次尝试，可触发重试。
- **S7 重试**：总尝试次数 = max_retries + 1；退避序列 = `backoff × 2^(attempt-1)`，上限 `retry_backoff_max`（默认 300s）；退避通过**可注入的 sleep 调用**完成（测试用虚拟时钟推进）。
- **S8 持久化**：保存 Job 全部配置字段 + next_run_time；原子写（同目录临时文件 + `os.replace`）；文件缺失视为空库；JSON 损坏抛 `StoreError` 且保留原文件；可调用对象以**名称**引用（不序列化 callable），运行期解析失败记为一条失败运行记录（不崩溃）。
- **S9 并发**：`ThreadPoolExecutor`，max_workers 可配（默认 4）；测试中可注入 None 表示无池内联执行（保证确定性）。
- **S10 CLI**：子命令 add / list / remove / pause / resume / run；退出码：0 成功、2 用法与参数校验错误、1 运行时错误（如存储文件损坏）；run 支持 `--duration` 秒数，收到 KeyboardInterrupt 时优雅退出且退出码为 0。
- **S11 暂停**：`enabled=False` 时调度主循环跳过该 Job（不运行、不推进 next_run_time）；恢复后由 misfire 策略按恢复时刻处理积压。
- **S12 trigger_now**：立即执行一次（禁用状态也执行），不影响 next_run_time；任务不存在则报错。

## 5. 验收标准（量化，A1–A10）

| ID | 验收项 | 量化标准 | 验证方式 |
|---|---|---|---|
| A1 | cron 解析 | ≥ 30 个解析用例（合法/非法/命名/宏/边界）全部通过 | unittest |
| A2 | next_after 正确性 | ≥ 20 组随机（表达式, 起点）与「逐分钟暴力扫描」ground truth 完全一致；永不匹配表达式在有界时间内抛 NoMatchError | unittest |
| A3 | 调度正确性 | 虚拟时钟模拟 24h、≥ 5 个混合任务，触发时刻与 ground truth 完全一致（零偏差） | unittest |
| A4 | misfire 策略 | 同一停机场景（连续错过 10 个周期）三种策略运行次数分别为 0 / 10 / 1，且记录中的 scheduled_time 为各自最早错过周期 | unittest |
| A5 | 重试与超时 | 失败 k 次后成功：attempts=k+1 且退避序列为 [b, 2b, ...]（虚拟睡眠记录）；挂起函数 timeout=0.2s 时 ≤1s 内返回 timeout 且重试继续 | unittest |
| A6 | 持久化 | 保存→新实例加载→触发行为一致；原子写不留 .tmp 残留；损坏文件抛 StoreError；缺失文件视为空库 | unittest |
| A7 | 并发 | 20 个任务同时到期、池大小 4、单任务 50ms：全部完成且墙钟 < 2s | unittest |
| A8 | CLI 端到端 | add/list/remove/run 子进程退出码正确；`run --duration 2`（interval=1s 任务）产出合法 JSONL 且 ≥ 1 条记录 | subprocess |
| A9 | 零依赖 | scheduler/ 与 tests/ 的 import 图仅含标准库；无第三方包 | 静态检查（acceptance.py） |
| A10 | 全量门禁 | `unittest discover` 全绿且用例数 ≥ 40；`acceptance.py` 退出码 0 且输出全部 PASS | 命令 |

## 6. 协作流程与阶段产物

| 阶段 | 负责 agent | 输入 | 产物（文件） |
|---|---|---|---|
| 0 章程 | orchestrator | — | `docs/00-charter.md` |
| 1 需求拆解 | pm | 章程 | `docs/01-requirements.md` |
| 2 方案设计 | architect | 章程 + 需求 | `docs/02-design.md` |
| 3 编码实现 | coder | 章程 + 需求 + 设计 | `scheduler/**`、`demo.py`、`README.md`、`docs/03-impl-notes.md` |
| 4 测试 | tester | 全部已有产物 | `tests/**`、`docs/04-test-report.md` |
| 5 修复迭代 | orchestrator + fixer | 测试报告 | 最小补丁、`docs/04b-fix-log.md` |
| 6 验收 | reviewer | 全部 | `docs/05-acceptance.md` |
| 7 终报 | orchestrator | 全部 | `REPORT.md` |

**闭环规则**：任一环节失败 → 记录现象 → 根因定位（四类归因：需求理解偏差 / 设计方案缺陷 / 代码实现问题 / 环境或构建问题）→ 给出针对性最小修复 → 重新执行并验证 → 迭代直至通过。每次修复只做必要修改，不做无关重构，并在修复日志中说明改动原因与影响范围。

## 7. 完成定义（DoD）

1. `acceptance.py` 退出码为 0，输出清单全部 PASS；
2. `unittest discover` 全绿且用例数 ≥ 40；
3. `demo.py` 自检通过（期望值与实际一致）；
4. 全部阶段文档（00–05 + 修复日志 + 终报）齐备；
5. 仓库 `git status` 显示仅新增 `tasks/task01_cron_scheduler/`，未改动任何既有文件。
