# 01 · 需求规格说明（Requirements）

> 角色：本文档是端到端任务 task01_cron_scheduler 的需求事实来源，由 pm agent 依据 `docs/00-charter.md` 编写。
> 后续方案设计、编码、测试、验收各阶段产物不得与本文档及章程冲突；如需变更，必须按章程第 4 节回写章程并记录理由。
> 编写时间：2026-10-14 · 任务编号：task01_cron_scheduler
>
> 阅读约定：
> - 章程条目以 S1–S12（关键语义决策）、A1–A10（验收标准）、F1–F12（In Scope 功能）引用；
> - 「（假设：…）」表示章程未明确、由本文档自行选择的口径，实现与测试按此执行；
> - 「编排者确认项 C1–C6」表示章程存在歧义、本文档已给出解释但需编排者拍板，汇总见 7.3；
> - 命令行、字段名、JSON 键名、异常名均为 ASCII；叙述为中文。

---

## 1. 需求概述

本系统是一个运行在单机上的纯 Python 标准库定时任务调度系统：运维与开发人员用 5 段 cron 表达式或固定秒数间隔来定义「什么时候跑哪个函数」，调度器按时间推进到点触发，通过可配置大小的线程池并发执行，支持单任务超时放弃、指数退避自动重试、三种错过周期（misfire）补救策略，并把任务配置与下次运行时间以 JSON 原子落盘，保证进程重启后任务不丢失、节奏不错乱；每次执行都追加一条 JSONL 运行记录供审计。系统提供 6 个子命令的 CLI 管理任务生命周期，并自带虚拟时钟 24 小时自检 demo 与全自动验收脚本，验收结果 100% 可由机器判定。它解决的核心问题是：在没有第三方依赖、不引入操作系统服务的约束下，用可测试、可恢复、可观测的方式把「定时/周期执行 Python 函数」这件事做对。

## 2. 角色与使用场景

| 角色 | 关注点 |
|---|---|
| 运维工程师 / SRE | 任务不因重启丢失；停机后的积压行为可预期 |
| 后端开发工程师 | 失败自动重试；超时不拖垮调度器 |
| 数据工程师 | 周期报表补跑策略；运行记录可审计 |
| 值班工程师 | 临时手动触发一次；CLI 脚本化管理 |

**场景 1 · 每晚 3 点跑数据同步（运维/SRE）**
每晚 03:00 同步报表数据（`0 3 * * *`）。某晚机器在 02:59 重启，恢复后：昨晚已错过的周期按既定策略处理（默认丢弃），今后的周期按原节奏继续，不需要人工重新配置。→ 涉及 FR-07、FR-08、FR-09。

**场景 2 · 失败自动重试（后端开发）**
任务调用外部 API，偶发 502。配置 `max_retries=3`、`retry_backoff=1s` 后，失败按 1s、2s、4s 退避自动重试，大部分抖动自愈，无需人工介入；全部失败才记一条 error 记录。→ 涉及 FR-12。

**场景 3 · 停机后补齐报表（数据工程师）**
报表任务停机 2 小时，连续错过 10 个周期。选择 `coalesce` 策略，恢复后按时间顺序把 10 个周期全部补跑（排水式），保证数据完整；若选 `fire_once` 只补最早一次，选 `discard` 直接跳到未来。→ 涉及 FR-09。

**场景 4 · 临时手动触发一次（值班工程师）**
线上任务处于暂停期，但需要立即执行一次做数据校正。调用 `trigger_now`：禁用状态也立即执行，且不影响该任务既定的 `next_run_time`。→ 涉及 FR-08（S12）。

**场景 5 · CLI 脚本化管理与冒烟（运维）**
在 Shell 脚本中用 `add/list/pause/resume/remove` 管理任务；发布前用 `run --duration 60` 做 60 秒冒烟，退出码可直接接入 CI 门禁。→ 涉及 FR-14。

---

## 3. 功能需求（FR-01 ~ FR-14）

> 章程 2.1 的 F1–F12 全部列为必须交付，因此 14 条 FR 均为 P0；F9–F12（demo / acceptance / 测试套件 / 文档）非交互功能，经测试场景（第 5 节）与非功能需求（第 6 节）覆盖，映射见 3.15。

### FR-01 cron 表达式解析

- **描述**：用户能用标准 5 段 cron 语法（含步长、列表、命名、宏）写下触发规则，非法写法被明确拒绝并告知原因。
- **详细规则**（依据 S1）：
  1. 固定 5 段、顺序为 `minute hour day-of-month month day-of-week`，段间以空白分隔；段数不为 5 抛 `CronParseError`；
  2. 每段支持：`*`、单值 `a`、范围 `a-b`、步长 `*/n` 与 `a-b/n`、列表 `a,b,c` 及上述组合（如 `1-5,10-20/2`）；
  3. 值域：minute 0–59、hour 0–23、day-of-month 1–31、month 1–12、day-of-week 0–7；越界抛 `CronParseError`；
  4. 月份名 `JAN`–`DEC`、星期名 `SUN`–`SAT`，大小写不敏感；仅名称段接受字母，数字段出现字母抛 `CronParseError`（假设：大小写不敏感仅作用于名称段，不扩大到整个表达式）；
  5. DOW 的 `7` 归一化为 `0`；范围如 `5-7` 展开为 `{5,6,0}`（假设：先按 0–7 校验再归一化）；
  6. 步长 n 必须为正整数（`*/0` 非法）；`a-b` 要求 b ≥ a，逆序抛 `CronParseError`；
  7. 宏（大小写不敏感，假设）：`@daily` = `0 0 * * *`、`@hourly` = `0 * * * *`、`@weekly` = `0 0 * * 0`、`@monthly` = `0 0 1 * *`、`@yearly` = `0 0 1 1 *`；宏独占一行，后跟其他段非法；
  8. 表达式整体 `strip()`，内部以一个或多个空白分隔（假设：容忍连续空白/Tab）。
- **验收标准**：
  - A1：≥ 30 个解析用例（合法/非法/命名/宏/边界）全部通过（unittest）；
  - 抽样：`*/15 * * * *` → 分 `{0,15,30,45}`；`0 9-17/2 * * *` → 时 `{9,11,13,15,17}`；`0 0 1 JAN SUN` → 月 `{1}`、周 `{0}`；`@weekly` 与 `0 0 * * 0` 展开集合相同；
  - 以下均抛 `CronParseError`：空串、`* * * *`（4 段）、`* * * * * *`（6 段）、`60 * * * *`、`* 24 * * *`、`0 0 32 1 *`、`0 0 1 13 *`、`0 0 * * 8`、`*/0 * * * *`、`5-1 * * * *`、`a * * * *`、`@daily extra`。
- **优先级**：P0

### FR-02 下一次触发时刻计算（next_after）

- **描述**：给定任意起始时刻，算出触发规则下严格晚于它的下一个命中时刻；永远不命中的规则被明确报告，而不是死循环。
- **详细规则**（依据 S2、S3）：
  1. 输入 dt 为 naive datetime；忽略其秒与微秒后，从「下一分钟」起算。例：dt=10:00:30、规则 `* * * * *` → 10:01:00；dt=10:00:00 → 10:01:00（结果严格大于 dt）；
  2. 输出时刻秒固定为 0（分钟粒度）；
  3. DOM/DOW 关系（S2）：日与周**均非** `*` 时为「或」（任一段命中即命中）；否则为「与」；
  4. 搜索上界 4 年：上界点 = 起算点推后 4 个日历年后仍无命中，抛 `NoMatchError`（假设：按日历年后推，自动含闰年）；
  5. 实现应字段递进而非逐分钟蛮扫，但可观测行为必须与逐分钟蛮扫完全一致（A2 对拍保证）。
- **验收标准**：
  - A2：≥ 20 组随机（表达式, 起点）与「逐分钟暴力扫描」ground truth 完全一致；永不匹配表达式在有界时间内抛 `NoMatchError`；
  - 抽样：`0 0 13 * 5` 自 2026-10-14（周三）起，首次命中 2026-10-16（周五，「或」语义：13 号或周五先到者准）；
  - `0 0 1 * *` @ 2026-10-14T10:00 → 2026-11-01T00:00；
  - `0 0 29 2 *` @ 2027-03-01T00:00 → 2028-02-29T00:00（闰年）；
  - `0 0 30 2 *` 任意起点 → `NoMatchError`，单次调用墙钟 < 5s。
- **优先级**：P0

### FR-03 CronTrigger

- **描述**：系统以「触发器」对象封装触发规则，调度器只问它「下一次什么时候跑」。
- **详细规则**：
  1. `CronTrigger(expression)` 构造时即解析表达式，非法立即抛 `CronParseError`（fail-fast，不把错误留到运行期）；
  2. `get_next_fire_time(after)` 委托 FR-02 的 next_after；
  3. 表达式永不命中时：`get_next_fire_time` 抛 `NoMatchError`；调度器捕获后将对应 Job 标记为「无未来触发」——`next_run_time` 置 null，主循环跳过该 Job，不产生运行记录、不崩溃（假设；CLI `list` 显示 `next_run_time` 为 null）；
  4. 落盘保留原始表达式字符串（含宏原文），加载时重新解析（假设：保留原文保证 round-trip 无损）。
- **验收标准**：
  - `CronTrigger("*/15 * * * *")` 的 `get_next_fire_time(2026-10-14T10:07:00)` 返回 `2026-10-14T10:15:00`（秒=0）；
  - `CronTrigger("bad")` 构造即抛 `CronParseError`；
  - `CronTrigger("0 0 30 2 *")` 的 `get_next_fire_time(任意)` 抛 `NoMatchError`；经调度器加载后该 Job `next_run_time` 为 null 且后续 tick 零触发。
- **优先级**：P0

### FR-04 IntervalTrigger

- **描述**：任务按固定秒数周期重复；重启后按上次保存的节奏继续，不因为重启而顺延。
- **详细规则**（依据 S4）：
  1. `IntervalTrigger(seconds)`：seconds 必须 > 0，否则抛 `ValueError`；
  2. 首次触发时刻 = 加入时刻 + interval（新建 Job 时由调度器以当前时刻计算，见 FR-08）；
  3. 持久化恢复后按落盘 `next_run_time` 继续，**不**用「加载时刻 + interval」重算（S4）；
  4. 正常推进锚定计划时刻：`next = 上次 scheduled_time + interval`（假设：不用 `now + interval`，防止漂移累积；misfire 时按 FR-09）。
- **验收标准**：
  - 虚拟时钟 10:00:00 加入 interval=1s 的 Job → `next_run_time`=10:00:01；触发后下一个为 10:00:02（锚定推进，非 now+1s）；
  - 落盘 `next_run_time`=10:00:05 后于 10:09:00 新建实例加载 → 该值仍为 10:00:05（不重算为 10:09:01），届时按 FR-09 处理。
- **优先级**：P0

### FR-05 Job 模型

- **描述**：一条配置说清一个任务：名字、跑什么、何时跑、失败与错过怎么办。
- **详细规则**：
  1. 必填：`name`（非空 ASCII 字符串，库内唯一）、`func`（注册表名称）、`trigger`（cron / interval 之一）；
  2. 可选字段、类型、默认值见 4.2 schema 表；章程未给出默认值的均为本文档假设并纳入确认项 C1：`misfire_policy` 默认 `discard`、`misfire_grace` 默认 60.0 秒、`max_retries` 默认 0、`retry_backoff` 默认 1.0 秒、`retry_backoff_max` 默认 300.0 秒（S7 明确）、`timeout` 默认 null、`enabled` 默认 true；
  3. 支持 `args`（JSON 数组）与 `kwargs`（JSON 对象）传给 func，默认空（假设：章程未明确，按通用调度器惯例补充，仅限 JSON 可序列化值；确认项 C3）；
  4. 值域校验：`misfire_grace` ≥ 0、`max_retries` ≥ 0、`retry_backoff` > 0、`retry_backoff_max` > 0、`timeout` > 0 或 null、`misfire_policy` ∈ {`discard`, `coalesce`, `fire_once`}；违规抛 `ValueError`（CLI 层转退出码 2）；
  5. 同名重复添加报错（API 抛错、CLI 退出码 2），不做静默覆盖（假设；确认项 C6）；
  6. `to_dict()` / `from_dict()` 支持序列化 round-trip，供 FR-07 使用。
- **验收标准**：
  - 含全部可选字段的 Job 经 `to_dict()` → `from_dict()` 后所有字段相等（含 trigger 与全部重试/超时参数）；dict 键集合与 4.2 一致；
  - 缺 `name`/`func`/`trigger` 任一构造即抛错；`misfire_grace=-1`、`max_retries=-1`、`retry_backoff=0`、`timeout=0`、`timeout=-1`、`misfire_policy="nope"` 均抛错；
  - 同名 add 两次，第二次报错。
- **优先级**：P0

### FR-06 函数注册表

- **描述**：配置里只写函数名字，运行期按名字找到真正的函数；名字对不上时只记一条失败，绝不拖垮调度器。
- **详细规则**（依据 S8）：
  1. 注册表为「名称 → callable」的内存映射：`register(name, func)`（重名覆盖，假设）、`get(name)`、`unregister(name)`；
  2. 名称为非空 ASCII 字符串；**不序列化 callable**，落盘只存名称（S8）；
  3. 运行期 `get(name)` 失败（未注册）→ 该次执行记为一条失败运行记录（`status="error"`，`error` 含未找到的函数名），调度器与后续任务不受影响、不崩溃（S8）；
  4. 解析失败按普通失败计入 attempts、可触发重试（假设：无法预知注册表后续是否补充，口径与普通异常一致）；
  5. 内置演示函数（假设，供 CLI 与 demo 使用）：至少 `noop`（空操作，成功）与 `fail`（总是抛 `RuntimeError`）；其余函数由使用方在启动前注册（见风险 R10）。
- **验收标准**：
  - 注册后触发 → 记录 `status="success"`、`attempts=1`；
  - job `func="missing"` 触发 → 恰好 1 条 `status="error"` 记录、`error` 含 `"missing"`，同一 tick 内其他 job 正常执行，进程不退出；
  - `to_dict()` 中 `func` 值为字符串。
- **优先级**：P0

### FR-07 JSON 持久化

- **描述**：退出再进来，任务和「下次什么时候跑」都原样还在；写盘中途出事也不会把配置文件写坏。
- **详细规则**（依据 S8）：
  1. 保存 Job 全部配置字段 + `next_run_time`；文件格式见 4.2；
  2. 原子写：同目录临时文件 `<store>.tmp` 写入完成后 `os.replace` 覆盖目标；正常与失败路径均不得残留 `.tmp`（A6）；
  3. 文件缺失 → 视为空库（0 个 job），不报错（S8）；
  4. JSON 损坏（语法错误、`version` 非 1、job 记录结构非法、trigger 表达式无法重新解析）→ 抛 `StoreError`，且原文件保持原字节不动，不删不改（S8；加载期表达式非法即损坏为假设口径）；
  5. 序列化确定性：`json.dumps(..., sort_keys=True)`，同一状态落盘字节一致（对应非功能 N3）；
  6. 保存时机（假设）：add/remove/pause/resume 等变更后立即保存；主循环每个 tick 结束时统一保存本 tick 推进过的 `next_run_time`；CLI 子命令退出前完成保存；
  7. 单写者假设：同一时刻只有一个进程写同一存储文件；不做文件锁（见风险 R8）。
- **验收标准**：
  - A6：保存 → 新实例加载 → 触发行为一致（同一虚拟时钟序列下，恢复实例的运行记录与原实例继续运行的记录逐条相同）；
  - save 后同目录 `glob("*.tmp")` 为空；
  - 手工写坏 JSON（如 `{"version":1,"jobs":[{"name":`）→ load 抛 `StoreError`，文件字节前后一致；`version:2` 同样抛 `StoreError`；
  - 删除存储文件 → load 得到空库，后续 add 正常。
- **优先级**：P0

### FR-08 调度器主循环

- **描述**：调度器看着时钟往前走，到点就把任务交给执行器；测试中时间完全可控。
- **详细规则**（依据 S5、S9、S11、S12）：
  1. 主循环以 tick 推进：每个 tick 取当前时刻 now，对每个 `enabled=True` 且 `next_run_time ≤ now` 的 Job 触发一次；随后按 FR-09 或正常规则推进其 `next_run_time`；
  2. tick 内多 Job 处理顺序 = 按 `name` 字典序（确定性，假设）；
  3. 每个 Job 每个 tick 最多触发一次；推进后若仍 ≤ now，留待后续 tick（假设，与 coalesce 口径一致）；
  4. 时钟注入：核心只依赖注入的 Clock（`now()`）；测试用虚拟时钟，CLI `run` 用真实时钟，行为同构；
  5. 暂停（S11）：`enabled=False` 时跳过——不运行、不推进 `next_run_time`；恢复后以恢复时刻为 now，按 FR-09 处理积压；
  6. `trigger_now(name)`（S12）：立即执行一次（禁用状态也执行），不影响 `next_run_time`；任务不存在抛 `JobNotFoundError`；其运行记录 `scheduled_time` = 触发时刻（假设）；
  7. 新建 Job 时计算 `next_run_time`：cron 取 `next_after(now)`，interval 取 `now + interval`（假设）；
  8. 加载后所有 `next_run_time` 一律取落盘值，不重算（假设，与 S4 精神一致）；
  9. 主循环形态（真实时钟模式）：`while not stopped: tick(now()); sleep(tick_interval)`；`stop()` 用于优雅退出。
- **验收标准**：
  - A3：虚拟时钟 24h、≥ 5 个混合任务，触发时刻与 ground truth 零偏差（见 TS-19）；
  - 暂停中的 Job 在到期 tick 零运行记录、`next_run_time` 不变；resume 后按策略处理积压；
  - `trigger_now` 对禁用 Job 执行 1 次且 `next_run_time` 前后不变；不存在 name 抛 `JobNotFoundError`。
- **优先级**：P0

### FR-09 misfire（错过周期）策略

- **描述**：停机或暂停攒了一堆没跑的周期，恢复时可以选：全丢、全补、只补最早一次。
- **详细规则**（依据 S5）：
  1. 生效条件：`lateness = now - scheduled_time > misfire_grace`；`lateness ≤ grace` 走正常迟到分支：执行一次并把 `next_run_time` 推进一个周期；
  2. `discard`：不运行；`next_run_time` 跳到「下一个严格晚于 now 的匹配时刻」；不产生运行记录；
  3. `coalesce`：每个 tick 为**最早一个**错过周期运行一次，随后 `next_run_time` 前进一个周期；排水式直至追平 now（每次 tick 最多执行一次——歧义解释见确认项 C2）；
  4. `fire_once`：仅为最早一个错过周期运行一次，随后 `next_run_time` 跳到「下一个严格晚于 now 的匹配时刻」；
  5. 「一个周期」的口径：cron = `next_after(本次 scheduled_time)`；interval = `scheduled_time + interval`；
  6. 测试计数口径（供 A4）：只统计 `scheduled_time` 早于恢复时刻的执行，恢复时刻及之后到期的正常周期不计入。
- **验收标准**：
  - A4：同一停机场景（连续错过 10 个周期）三种策略运行次数分别为 0 / 10 / 1；
  - `discard`：无任何错过周期的运行记录，结束后 `next_run_time > now`；
  - `coalesce`：10 条记录，`scheduled_time` 按时间递增覆盖全部错过周期，首条 = 最早错过周期，最终 `next_run_time > now`；
  - `fire_once`：恰好 1 条记录，`scheduled_time` = 最早错过周期，随后 `next_run_time > now`；
  - 边界：`lateness = grace` 走正常分支（运行 1 次并推进一个周期）。
- **优先级**：P0

### FR-10 执行器与并发

- **描述**：多个任务同时到期就并行跑，池多大可以配；测试时可以关掉线程换取确定性。
- **详细规则**（依据 S9）：
  1. 基于 `ThreadPoolExecutor`，`max_workers` 可配，默认 4；
  2. 可注入 `None` 表示无池内联执行（调用线程内同步执行）——仅供确定性逻辑测试（假设）；
  3. 主循环提交后不阻塞等待完成（fire-and-forget），结果由执行器异步收集并写运行记录；
  4. 单次执行契约：`run(func, args, kwargs, timeout)` → 单次结果（`status` / `duration` / `error`）；超时规则见 FR-11，重试包装见 FR-12。
- **验收标准**：
  - A7：20 个任务同时到期、池大小 4、单任务 50ms → 全部完成且墙钟 < 2s（理论下限约 4×50ms=200ms，2s 留足余量）；
  - 注入 `None` 时 tick 内同步执行完毕，tick 返回后运行记录已落盘（确定性）。
- **优先级**：P0

### FR-11 超时

- **描述**：单个任务跑太久就被判超时放弃，调度器不被拖死。
- **详细规则**（依据 S6）：
  1. `timeout` 到点即放弃等待并记录 `status="timeout"`；底层线程作为 daemon 继续执行（已知限制，必须在设计文档与 README 明示）；
  2. 超时计入一次尝试，可触发重试（S6）；
  3. `timeout=None` 表示不限时；`timeout ≤ 0` 非法（`ValueError`）；
  4. 实现口径：`future.result(timeout=...)` 超时 → 放弃等待该 future，不 cancel 底层线程（假设：cancel 对已开始任务无效，语义等价于放弃等待）；
  5. 内联模式（无池）下 timeout 不生效（假设：超时测试必须使用真实线程池，见风险 R6）；
  6. 超时记录的 `error` 文本包含 "timeout"（如 `timeout after 0.2s`，假设）。
- **验收标准**：
  - A5：挂起函数（`sleep(10)`）、`timeout=0.2`、`max_retries=1`、`retry_backoff=0.1` → 调用方 ≤ 1s 内拿到结果；记录 `status="timeout"`、`attempts=2`、`error` 含 "timeout"，且重试确实继续。
- **优先级**：P0

### FR-12 重试与指数退避

- **描述**：任务失败不立刻放弃，按 1×、2×、4×… 的退避自动重试，单次退避有上限。
- **详细规则**（依据 S7）：
  1. 总尝试次数 = `max_retries + 1`；
  2. 第 attempt 次失败后、第 attempt+1 次尝试前 sleep `min(retry_backoff × 2^(attempt-1), retry_backoff_max)`（假设：上限作用于单次退避时长，而非累计）；
  3. 退避通过**可注入的 sleep 调用**完成；测试注入记录型假 sleep（仅记录请求时长、不改变时钟），用虚拟时钟推进（S7）；
  4. 一次「触发」的所有尝试共享同一条运行记录（`attempts` 字段体现次数）；最终成功 `status="success"`，全部失败 `status="error"`；
  5. 超时（FR-11）同样计入尝试次数；
  6. 重试发生在执行器外围（同一周期内循环调用单次执行 + 退避 sleep），不重新入队。
- **验收标准**：
  - A5：失败 k 次后成功 → `attempts=k+1`，虚拟 sleep 记录序列 = `[b, 2b, …, 2^(k-1)·b]`；
  - 封顶：`retry_backoff=100`、`retry_backoff_max=300`、`max_retries=5` 且全部失败 → sleep 序列 `[100,200,300,300,300]`、`attempts=6`、`status="error"`；
  - 成功场景 `status="success"`。
- **优先级**：P0

### FR-13 运行记录

- **描述**：每次执行都留一行结构化日志，成败耗时一目了然。
- **详细规则**（依据 F7）：
  1. JSONL：每次执行一条单行 JSON，追加写不覆盖；文件不存在则创建；
  2. 字段见 4.3：`job` / `scheduled_time` / `started_at` / `finished_at` / `duration` / `status` / `attempts` / `error`；
  3. `status` ∈ {`success`, `error`, `timeout`}；`error` 成功时为 null；
  4. 时间字段 ISO 8601 naive（`YYYY-MM-DDTHH:MM:SS`）；`duration` 为秒（float）；
  5. 记录在「一次触发的所有尝试结束后」写入一次（与 FR-12 单记录口径一致）。
- **验收标准**：
  - 连续执行 3 次（成功/异常/超时各 1）→ JSONL 恰 3 行，每行 `json.loads` 成功且 8 字段齐全，`status` 分别为 `success`/`error`/`timeout`；
  - 再次执行 → 行数变 4（追加语义），旧行字节不变。
- **优先级**：P0

### FR-14 CLI

- **描述**：命令行管任务（增删查、暂停恢复）并启动调度，退出码能让脚本可靠判断成败。
- **详细规则**（依据 S10，完整用法见 4.1）：
  1. 子命令：`add` / `list` / `remove` / `pause` / `resume` / `run`；
  2. 退出码：0 成功；2 用法与参数校验错误（缺必填、表达式非法、同名、删不存在、JSON 解析失败、数值非法）；1 运行时错误（存储文件损坏、未分类异常）；
  3. `run --duration N`：运行 N 秒后正常退出（0）；收到 KeyboardInterrupt 优雅退出且退出码 0（S10）；
  4. 成功产出写 stdout（单行 ASCII）；错误写 stderr，格式 `error: <message>`（假设）；
  5. `trigger_now` **不**暴露为 CLI 子命令（S10 仅列六个子命令；确认项 C4）；
  6. 入口为 `python -m scheduler.cli`，工作目录为任务根目录（假设）。
- **验收标准**：
  - A8：add/list/remove/run 子进程退出码正确；`run --duration 2`（interval=1s 任务）产出合法 JSONL 且 ≥ 1 条记录；
  - 具体：正常 add → 0；重复 add → 2；remove 不存在 → 2；坏存储下 list → 1；`run --duration 2` → 0 且日志 ≥ 1 行。
- **优先级**：P0

### 3.15 章程范围覆盖映射

| 章程项 | 覆盖位置 |
|---|---|
| F1 cron 引擎 | FR-01、FR-02；TS-01 ~ TS-11 |
| F2 触发器 | FR-03、FR-04；TS-12、TS-13 |
| F3 Job 模型与注册表 | FR-05、FR-06；TS-14 ~ TS-16 |
| F4 JSON 持久化 | FR-07；TS-17、TS-18 |
| F5 调度主循环 | FR-08、FR-09；TS-19 ~ TS-25 |
| F6 执行器 | FR-10、FR-11、FR-12；TS-26 ~ TS-28 |
| F7 运行记录 | FR-13；TS-16、TS-26、TS-27、TS-29 |
| F8 CLI | FR-14；TS-29 |
| F9 demo.py | TS-30；N4 |
| F10 acceptance.py | TS-30；A9、A10 |
| F11 测试套件 | TS-01 ~ TS-30（≥ 40 用例的落地场景）；A10 |
| F12 文档 | 本文档 + 02–05、04b、REPORT 阶段产物（编排者流程保证） |

---

## 4. I/O 契约

### 4.1 CLI 契约

调用形式（任务根目录执行）：

```
python -m scheduler.cli [--store PATH] [--log PATH] <subcommand> [options]
```

全局参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--store PATH` | `cron_jobs.json` | 任务存储文件（相对工作目录）（假设，确认项 C1） |
| `--log PATH` | `cron_runs.jsonl` | 运行记录文件，仅 `run` 使用（假设，确认项 C1） |

退出码总表（与 S10 一致）：

| 退出码 | 含义 | 典型情形 |
|---|---|---|
| 0 | 成功 | 命令完成；`run` 正常结束或收到 KeyboardInterrupt |
| 2 | 用法与参数校验错误 | 缺必填 flag、cron 与 interval 缺一或互斥、表达式非法、数值非法、JSON 解析失败、同名 add、操作不存在的任务 |
| 1 | 运行时错误 | 存储文件损坏（`StoreError`）、日志不可写、未分类异常 |

stdout 约定：仅放成功产出，单行 ASCII。stderr 约定：失败时单行 `error: <message>`（ASCII），不写 traceback（假设）。

#### 4.1.1 add

```
python -m scheduler.cli add --name NAME --func FUNC (--cron EXPR | --interval SECONDS)
    [--misfire discard|coalesce|fire_once] [--grace SECONDS]
    [--max-retries N] [--backoff SECONDS] [--backoff-max SECONDS]
    [--timeout SECONDS] [--disabled] [--args JSON_ARRAY] [--kwargs JSON_OBJECT]
```

| 项 | 契约 |
|---|---|
| 必填 | `--name`、`--func`，且 `--cron` 与 `--interval` 二选一（互斥） |
| 可选默认 | `--misfire discard`、`--grace 60`、`--max-retries 0`、`--backoff 1`、`--backoff-max 300`、`--timeout` 无（不限）、`--disabled` 不给出即 enabled |
| stdout（成功） | `added <name> next_run=<ISO8601>`（`next_run` 为按当前时刻计算的下次运行时刻；永不命中时为 `null`） |
| 退出码 | 0 成功；2 参数/校验错误（含表达式非法、同名已存在）；1 存储损坏 |

#### 4.1.2 list

```
python -m scheduler.cli list
```

| 项 | 契约 |
|---|---|
| stdout | 每个 Job 一行 JSON（JSONL），按 `name` 升序；空库无输出 |
| 行内字段 | `{"name":...,"func":...,"trigger":{"type":"cron","expression":"..."}|{"type":"interval","seconds":N},"next_run_time":"ISO"|null,"enabled":true|false,"misfire_policy":"...","misfire_grace":60.0,"max_retries":0,"timeout":null}` |
| 退出码 | 0 成功；1 存储损坏 |

#### 4.1.3 remove / pause / resume

```
python -m scheduler.cli remove --name NAME
python -m scheduler.cli pause  --name NAME
python -m scheduler.cli resume --name NAME
```

| 项 | 契约 |
|---|---|
| stdout（成功） | 分别 `removed <name>` / `paused <name>` / `resumed <name>` |
| 退出码 | 0 成功；2 缺 `--name` 或任务不存在（确认项 C5）；1 存储损坏 |

#### 4.1.4 run

```
python -m scheduler.cli run [--duration SECONDS] [--tick SECONDS]
```

| 项 | 契约 |
|---|---|
| 可选默认 | `--duration` 无（运行至中断）；`--tick 1.0`（主循环步长，假设） |
| 行为 | 启动时加载存储（损坏 → 退出码 1）；到期任务提交执行，运行记录写 `--log`；stdout 不写运行记录 |
| 退出码 | 0：`--duration` 到期正常结束，或收到 KeyboardInterrupt 优雅退出（S10）；1：启动失败/运行时错误 |
| 函数来源 | 内置注册表（至少 `noop`、`fail`）；其余函数需使用方自行注册（见 R10） |

其他：`--help` 或 `-h` 打印用法并退出码 0；未给子命令退出码 2（假设）。

### 4.2 持久化 JSON Schema

文件顶层结构：

```json
{
  "version": 1,
  "jobs": [ { "...": "..." } ]
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `version` | int | 是 | `1` | 格式版本；非 1 抛 `StoreError`（假设） |
| `jobs` | array | 是 | `[]` | Job 对象列表 |

Job 对象字段：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `name` | string | 是 | — | 任务名，非空 ASCII，库内唯一 |
| `func` | string | 是 | — | 函数注册表名称（不存 callable） |
| `trigger` | object | 是 | — | `{"type":"cron","expression":"0 3 * * *"}` 或 `{"type":"interval","seconds":3600}`；保存原始表达式（宏原文保留） |
| `args` | array | 否 | `[]` | 位置参数，JSON 可序列化 |
| `kwargs` | object | 否 | `{}` | 关键字参数 |
| `misfire_policy` | string | 否 | `"discard"` | 枚举 `discard` / `coalesce` / `fire_once` |
| `misfire_grace` | number | 否 | `60.0` | 秒；≥ 0 |
| `max_retries` | int | 否 | `0` | 总尝试次数 = `max_retries + 1` |
| `retry_backoff` | number | 否 | `1.0` | 秒；退避基数，> 0 |
| `retry_backoff_max` | number | 否 | `300.0` | 秒；单次退避上限，> 0 |
| `timeout` | number \| null | 否 | `null` | 秒；> 0 或 null |
| `enabled` | bool | 否 | `true` | `false` = 暂停 |
| `next_run_time` | string \| null | 否（由调度器维护） | `null` | ISO `YYYY-MM-DDTHH:MM:SS`；无未来触发为 `null`；加载时缺失按 `null` |

约束：序列化 `sort_keys=True`；`trigger.expression` 加载时重新解析，非法抛 `StoreError`（假设）；`next_run_time` 恢复时取落盘值不重算。

### 4.3 运行记录 JSONL Schema

每次执行一行 JSON（UTF-8 编码、内容 ASCII、以 `\n` 结尾、追加写）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `job` | string | 任务名 |
| `scheduled_time` | string (ISO) | 本次执行对应的计划触发时刻（`trigger_now` 为触发时刻） |
| `started_at` | string (ISO) | 实际开始时刻（取自注入时钟） |
| `finished_at` | string (ISO) | 结束时刻（取自注入时钟） |
| `duration` | number | 执行耗时（秒，float） |
| `status` | string | `success` / `error` / `timeout` |
| `attempts` | int | 本次执行的尝试次数（含超时尝试；无重试时为 1） |
| `error` | string \| null | 失败信息（异常 message/repr 或 timeout 文案）；成功为 `null` |

示例（三行）：

```json
{"job":"sync","scheduled_time":"2026-10-14T03:00:00","started_at":"2026-10-14T03:00:00","finished_at":"2026-10-14T03:00:02","duration":2.0,"status":"success","attempts":1,"error":null}
{"job":"fetch","scheduled_time":"2026-10-14T03:00:00","started_at":"2026-10-14T03:00:00","finished_at":"2026-10-14T03:00:03","duration":3.0,"status":"error","attempts":4,"error":"RuntimeError: boom"}
{"job":"hang","scheduled_time":"2026-10-14T03:00:00","started_at":"2026-10-14T03:00:00","finished_at":"2026-10-14T03:00:00","duration":0.2,"status":"timeout","attempts":2,"error":"timeout after 0.2s"}
```

### 4.4 函数注册表契约（对应 S8）

| 项 | 契约 |
|---|---|
| 映射 | 名称（非空 ASCII 字符串）→ callable；`register(name, func)`（重名覆盖，假设）、`get(name)`、`unregister(name)` |
| 未注册 | `get(name)` 抛 `FunctionNotFoundError` |
| 解析失败降级 | 运行期 `get` 失败 → 记一条 `status="error"`、`error` 含函数名的运行记录；调度器不崩溃、后续任务不受影响（S8）；计入 attempts、可重试（假设） |
| 序列化 | 不序列化 callable；落盘与 `list` 输出仅含名称字符串 |
| 内置函数 | `noop`（成功）、`fail`（抛 `RuntimeError`）（假设） |

### 4.5 异常与退出码对照

| 异常 / 情形 | 抛出层 | CLI 退出码 |
|---|---|---|
| `CronParseError` | 表达式解析 | 2 |
| `NoMatchError` | `next_after` | 不冒泡到 CLI：Job 标记无未来触发（FR-03） |
| `StoreError` | 存储加载/保存 | 1 |
| `FunctionNotFoundError` | 注册表 | 不冒泡：降级为 error 运行记录（FR-06） |
| `JobNotFoundError` | `trigger_now` / remove / pause / resume | 2 |
| `ValueError` | 参数与值域校验 | 2 |
| 未分类异常 | 任意 | 1 |

---

## 5. 测试场景清单（TS-01 ~ TS-30）

> 每条场景含：前置条件 / 操作 / 期望结果 / 对应验收标准 / 对应 FR。全部落地为 unittest 用例，合计需满足 A10 的「≥ 40 用例」（允许一条 TS 拆成多个用例）。除标注「子进程」外均在进程内执行。

### TS-01 cron 基础语法解析

- 前置：解析器可用。
- 操作：解析 `* * * * *`、`0 12 * * *`、`30 8,12,18 * * *`、`0 0 1,15 * *`，读取各字段展开集合。
- 期望：分别为 分{0-59}/时{0-23}/日{1-31}/月{1-12}/周{0-6}；分{0}时{12}；时{8,12,18}；日{1,15}。
- 验收标准：A1。对应 FR：FR-01。

### TS-02 范围与步长

- 操作：解析 `0 9-17/2 * * *`、`*/15 * * * *`、`0 0-6 * * *`、`10-20/5 8 * * *`。
- 期望：时{9,11,13,15,17}；分{0,15,30,45}；时{0..6}；分{10,15,20}。
- 验收标准：A1。对应 FR：FR-01。

### TS-03 命名月份/星期与宏

- 操作：解析 `0 0 1 jan mon`、`0 0 * DEC SAT`、`@daily`、`@hourly`、`@weekly`、`@monthly`、`@yearly`、`@DAILY`。
- 期望：月{1}周{1}；月{12}周{6}；五个宏分别等价 `0 0 * * *`、`0 * * * *`、`0 0 * * 0`、`0 0 1 * *`、`0 0 1 1 *`；`@DAILY` 与 `@daily` 等价。
- 验收标准：A1。对应 FR：FR-01。

### TS-04 DOW 0/7 归一化

- 操作：解析 `0 0 * * 0`、`0 0 * * 7`、`0 0 * * 5-7`；对前二者取同一时刻的 `next_after`。
- 期望：前二者周集合均为 {0} 且 `next_after` 结果相同；`5-7` 展开为 {5,6,0}。
- 验收标准：A1。对应 FR：FR-01。

### TS-05 非法输入拒绝

- 操作：逐个解析：空串、`* * * *`（4 段）、`* * * * * *`（6 段）、`60 * * * *`、`* 24 * * *`、`0 0 32 1 *`、`0 0 1 13 *`、`0 0 * * 8`、`*/0 * * * *`、`5-1 * * * *`、`a * * * *`、`@daily extra`。
- 期望：全部抛 `CronParseError`，错误信息 ASCII 且指明原因。
- 验收标准：A1。对应 FR：FR-01。

### TS-06 合法边界值

- 操作：解析 `59 23 31 12 *`、`0 0 29 2 *`、`0 0 31 * *`、`*/1 * * * *`。
- 期望：均解析成功；`0 0 29 2 *` 仅在闰年 2/29 命中（由 TS-09/TS-11 验证行为）。
- 验收标准：A1。对应 FR：FR-01。

### TS-07 DOM/DOW 「或」语义

- 前置：表达式 `0 0 13 * 5`（日、周均受限）。
- 操作：自 2026-10-14（周三）起连续取 5 次 `next_after`。
- 期望：命中序列为 2026-10-16（周五）、10-23、10-30、11-06（周五）、11-13（13 号且周五，仅算一次）——「13 号或周五」先到者准。
- 验收标准：A2。对应 FR：FR-02。

### TS-08 DOM/DOW 「与」语义

- 操作：`0 0 13 * *` 与 `0 0 * * 5` 自 2026-10-14 起取 `next_after`；再验证 `0 0 13 * 1-5`（均受限 → 或）。
- 期望：`0 0 13 * *` → 2026-11-13T00:00（仅 13 号）；`0 0 * * 5` → 2026-10-16T00:00（仅周五）；`0 0 13 * 1-5` → 2026-10-16T00:00（或语义取先到）。
- 验收标准：A2。对应 FR：FR-02。

### TS-09 next_after 语义与跨周期

- 操作：`* * * * *` @ 10:00:30；同表达式 @ 10:00:00；`0 0 1 * *` @ 2026-10-14T10:00；`0 0 29 2 *` @ 2027-03-01T00:00；`59 23 31 12 *` @ 2026-12-31T23:59。
- 期望：依次为 10:01:00、10:01:00、2026-11-01T00:00、2028-02-29T00:00、2027-12-31T23:59；所有结果秒=0 且严格大于输入。
- 验收标准：A2。对应 FR：FR-02。

### TS-10 next_after 与暴力扫描对拍

- 前置：参考实现为独立的「逐分钟暴力扫描」（从起算下一分钟起逐分钟检查命中，窗口 4 年）；随机表达式从「高频可命中」模板池采样（小时级/日级/周级/步长/命名/DOM-DOW 组合），保证参考实现可快速命中；起点随机到分钟。
- 操作：生成 ≥ 20 组随机（表达式, 起点），逐一比对 `next_after` 与参考实现。
- 期望：逐组完全一致；参考实现未命中（窗口内无匹配）时 `next_after` 抛 `NoMatchError`。
- 验收标准：A2。对应 FR：FR-02。

### TS-11 永不匹配表达式

- 操作：`0 0 30 2 *`、`0 0 31 4 *`（4 月 31 日）@ 任意起点。
- 期望：抛 `NoMatchError`，单次调用墙钟 < 5s（有界，不死循环）。
- 验收标准：A2。对应 FR：FR-02。

### TS-12 CronTrigger 接口

- 操作：构造 `CronTrigger("*/15 * * * *")` 并调 `get_next_fire_time(2026-10-14T10:07:00)`；构造 `CronTrigger("bad")`；构造 `CronTrigger("0 0 30 2 *")` 并调 `get_next_fire_time`。
- 期望：返回 `2026-10-14T10:15:00`（秒=0）；非法表达式构造即抛 `CronParseError`；永不命中抛 `NoMatchError`。
- 验收标准：A1、A3。对应 FR：FR-03。

### TS-13 IntervalTrigger 首次触发与恢复不重算

- 前置：虚拟时钟；存储可用。
- 操作：10:00:00 加入 interval=1s 的 Job，触发两次；把 `next_run_time` 落盘为 10:00:05，10:09:00 新建实例加载。
- 期望：首次 `next_run_time`=10:00:01，第二次触发后为 10:00:02（锚定推进）；加载后仍为 10:00:05（不重算为 10:09:01）。
- 验收标准：A3、A6。对应 FR：FR-04（+FR-07）。

### TS-14 Job 序列化 round-trip

- 操作：构造含全部可选字段的 Job（args/kwargs、`coalesce`、grace=30、max_retries=2、backoff=2、backoff_max=10、timeout=5、enabled=False、给定 next_run_time）→ `to_dict()` → `from_dict()`。
- 期望：round-trip 后全部字段相等；dict 键集合与 4.2 一致；trigger 保留原始表达式字符串。
- 验收标准：A6。对应 FR：FR-05。

### TS-15 Job 参数校验

- 操作：分别缺 `name`/`func`/`trigger` 构造 Job；构造 `misfire_grace=-1`、`max_retries=-1`、`retry_backoff=0`、`timeout=0`、`timeout=-1`、`misfire_policy="nope"` 的 Job；构造 `IntervalTrigger(0)`、`IntervalTrigger(-5)`。
- 期望：全部抛 `ValueError`（或其子类），错误信息 ASCII。
- 验收标准：A6。对应 FR：FR-05、FR-04。

### TS-16 函数注册表：正常解析与失败降级

- 前置：注册 `noop`（成功）与 `fail`（抛 `RuntimeError`）；两个 Job：A `func=noop`、B `func=missing`。
- 操作：一次 tick 使二者同时到期。
- 期望：A 记录 `status="success"`、`attempts=1`；B 恰好 1 条 `status="error"`、`error` 含 `"missing"`；进程不崩溃；落盘 `func` 为字符串。
- 验收标准：A3。对应 FR：FR-06。

### TS-17 持久化原子写与恢复一致性

- 操作：add 3 个 Job（含各自 `next_run_time`）→ save → 检查目录无 `*.tmp` → 新实例 load → 在同一虚拟时钟序列下 tick 24h。
- 期望：无 `.tmp` 残留；加载后 `next_run_time` 与原实例一致；恢复实例的运行记录与「原实例继续运行」的参照记录逐条相同。
- 验收标准：A6。对应 FR：FR-07。

### TS-18 存储损坏与缺失

- 操作：(a) 存储写入损坏 JSON `{"version":1,"jobs":[{"name":` → load；(b) 写入 `{"version":2,"jobs":[]}` → load；(c) 删除存储文件 → load。
- 期望：(a)(b) 抛 `StoreError` 且文件字节前后一致；(c) 返回空库（0 Job），后续 add 正常。
- 验收标准：A6。对应 FR：FR-07。

### TS-19 24h 虚拟时钟集成

- 前置：虚拟时钟自 2026-10-14T00:00:00 起以 1 分钟步长推进 1440 步；注册计数器函数；使用内联执行（池=None）与假 sleep 保证确定性。
- 操作：≥ 5 个混合 Job：`0 * * * *`（时，24 次）、`*/15 * * * *`（96 次）、`0 3 * * *`（1 次）、`30 2 * * 3`（周三 02:30，1 次）、interval=900s（96 次）；另加 1 个 paused Job 与 1 个 `func=fail`、`max_retries=2` 的 Job。
- 期望：每个 Job 的实际触发时刻集合与独立 ground truth（cron 用暴力扫描/interval 用递推）完全一致，零偏差；paused Job 零触发；失败 Job 每次触发 `attempts=3`。
- 验收标准：A3。对应 FR：FR-08（联动 FR-09、FR-12）。

### TS-20 暂停 / 恢复语义

- 前置：Job `* * * * *`、misfire=`discard`、grace=0；虚拟时钟 10:00 启动，10:01–10:05 正常触发 5 次后 save。
- 操作：pause；时钟直接推到 10:16:00（错过 10:06–10:15 共 10 个周期）；新实例加载 resume；tick；再推进到 10:17。
- 期望：pause 期间（10:06–10:15 各 tick）零运行记录、`next_run_time` 恒为 10:06 不变；resume 后错过周期（10:06–10:15）零运行，`next_run_time` 由 10:06 直接跳到 10:17（> now；10:16 周期同样被跳过，因为 `next_after(10:16:00)` = 10:17:00）；时钟推进到 10:17:00 后正常触发 1 次。
- 验收标准：A3、A4。对应 FR：FR-08（+FR-09）。

### TS-21 trigger_now 三种行为

- 操作：对 enabled Job 与 paused Job 各 `trigger_now` 一次；对不存在 name `"ghost"` 调 `trigger_now`。
- 期望：前二者各产生 1 条记录（paused 也执行），且 `next_run_time` 前后不变；`"ghost"` 抛 `JobNotFoundError`。
- 验收标准：A3。对应 FR：FR-08。

### TS-22 misfire=discard 停机场景

- 前置：同 TS-20（`* * * * *`、discard、grace=0；10:01–10:05 正常 5 次后 save）。
- 操作：「停机」——时钟推到 10:16:00；新实例加载恢复；tick 一次；时钟推进到 10:17 再 tick。
- 期望：`scheduled_time < 10:16` 的执行次数 = 0，无任何错过周期记录；resume 后 `next_run_time` = 10:17（> now；10:16 周期同样被跳过）；时钟推进到 10:17:00 后正常触发 1 次，`scheduled_time` = 10:17:00。
- 验收标准：A4。对应 FR：FR-09。

### TS-23 misfire=coalesce 停机场景

- 前置：同 TS-22，但 misfire=`coalesce`。
- 操作：恢复后时钟停在 10:16:00，连续调用 tick 直至追平；再推进到 10:17。
- 期望：错过周期执行 10 次（每次 tick 一次，需 ≥ 10 次 tick 调用）；首条记录 `scheduled_time`=10:06，10 条记录按 10:06→10:15 递增；追平后 `next_run_time`=10:17；第 11 次 tick 触发 10:16 周期（正常分支，不计入错过统计）；10:17 正常触发。
- 验收标准：A4。对应 FR：FR-09。

### TS-24 misfire=fire_once 停机场景

- 前置：同 TS-22，但 misfire=`fire_once`。
- 操作：恢复后 tick 一次；推进到 10:17 再 tick。
- 期望：错过周期执行 1 次，`scheduled_time` = 10:06；随后 `next_run_time` = 10:17（> now；10:16 周期被跳过）；时钟推进到 10:17:00 后正常触发 1 次，`scheduled_time` = 10:17:00。
- 验收标准：A4。对应 FR：FR-09。

### TS-25 lateness ≤ grace 正常迟到

- 前置：Job `* * * * *`、grace=120s；虚拟时钟 10:05:30，`next_run_time`=10:05:00（lateness=30s）。
- 操作：tick 一次。另测边界：now=10:07:00、`next_run_time`=10:05:00（lateness=120s=grace）tick 一次。
- 期望：均走正常分支——各运行 1 次，`scheduled_time` 为当期时刻，`next_run_time` 推进一个周期（10:06:00 / 10:06:00），无 misfire 记录。
- 验收标准：A4。对应 FR：FR-09。

### TS-26 重试与退避序列

- 前置：计数函数前 k 次抛异常、第 k+1 次成功；注入记录型假 sleep（只记录、不推进时钟）；内联执行。
- 操作：(a) k=3、max_retries=5、retry_backoff=2、retry_backoff_max=10，触发一次；(b) retry_backoff=100、retry_backoff_max=300、max_retries=5、函数总是失败，触发一次。
- 期望：(a) `attempts=4`、假 sleep 序列 [2,4,8]、`status="success"`；(b) sleep 序列 [100,200,300,300,300]、`attempts=6`、`status="error"`。
- 验收标准：A5。对应 FR：FR-12。

### TS-27 超时处理

- 前置：挂起函数（`sleep(10)`）；**真实线程池**（max_workers≥1）；timeout=0.2、max_retries=1、retry_backoff=0.1（真实 sleep）。
- 操作：触发一次，测量调用方返回的墙钟时间。
- 期望：≤ 1s 内返回；记录 `status="timeout"`、`attempts=2`、`error` 含 "timeout"；底层线程继续执行（不验证其终止，已知限制 R1）。
- 验收标准：A5。对应 FR：FR-11。

### TS-28 并发执行

- 前置：20 个 Job（func 各 `sleep(50ms)`）；`max_workers=4`；构造/加载使其 `next_run_time` 对齐同一时刻。
- 操作：一次 tick 后等待全部完成，测量墙钟。
- 期望：20 条 `status="success"` 记录；墙钟 < 2s。
- 验收标准：A7。对应 FR：FR-10。

### TS-29 CLI 子进程端到端

- 前置：临时工作目录；以子进程方式调用 `python -m scheduler.cli`（解释器按章程指定）。
- 操作：(a) `add --name j1 --func noop --interval 1`；(b) 重复 add j1；(c) `list`；(d) `remove --name ghost`；(e) `pause --name j1` 与 `resume --name j1`；(f) 写坏存储后 `list`；(g) `add --name j2 --func noop --cron "bad"`；(h) `run --duration 2`（j1，interval=1）。
- 期望：退出码依次为 0 / 2 / 0 / 2 / 0、0 / 1 / 2 / 0；(c) 的 stdout 含 `"name": "j1"` 的合法 JSON 行；(h) 退出码 0 且 `cron_runs.jsonl` 为合法 JSONL、≥ 1 行且每行含 4.3 全部字段。
- 验收标准：A8。对应 FR：FR-14。

### TS-30 验收门禁：demo / acceptance / 零依赖 / 全量

- 前置：全部代码就位；指定解释器可用。
- 操作：(a) 运行 `python demo.py`；(b) 运行 `python acceptance.py`；(c) 运行 `python -m unittest discover -s tests -v`。
- 期望：(a) 退出码 0，24h 虚拟时钟自检断言全部通过（自带期望值）；(b) 退出码 0，输出逐项 `PASS`，含零依赖静态检查（`scheduler/` 与 `tests/` 的 import 图仅标准库）；(c) 全绿且用例数 ≥ 40。
- 验收标准：A9、A10。对应 FR：FR-08、FR-14（章程 F9–F11 落地）。

---

## 6. 非功能需求

| ID | 需求 | 说明 |
|---|---|---|
| N1 | 零依赖 | 仅 Python 标准库；禁止 pip install；`scheduler/` 与 `tests/` 的 import 图可静态验证（A9） |
| N2 | ASCII 输出 | 代码、标识符、测试名、程序 stdout 一律 ASCII；中文仅用于 Markdown 文档 |
| N3 | 确定性 | 在注入虚拟时钟/假 sleep/内联池的前提下，同一场景重复执行产生逐条一致的运行记录；无随机源；tick 内 Job 按 `name` 排序；落盘 `sort_keys=True` |
| N4 | 可测试性 | 时钟、sleep、线程池均可注入；提供无池内联模式；next_after 可用逐分钟暴力扫描对拍；demo/acceptance 可被机器自动判定 |
| N5 | 性能（建议值） | `next_after` 单次 < 10ms（典型表达式）；24h 虚拟时钟模拟（1440 tick）墙钟 < 60s；硬门禁以 A7（< 2s）为准 |
| N6 | 可维护性 | 模块边界清晰：解析 / 触发器 / Job 与注册表 / 存储 / 调度 / 执行 / CLI 分离；阶段文档（F12）齐备 |

## 7. 风险与假设

### 7.1 风险（R1 ~ R10）

| ID | 风险 | 影响 | 应对 |
|---|---|---|---|
| R1 | timeout 无法真正杀死线程（S6：daemon 继续执行） | 挂起函数长期占用池线程；若断言「进程可立即退出」会误判 | 记录 `status="timeout"` 后放弃等待；设计文档与 README 明示；测试只验证返回时长、记录与重试继续，不验证线程终止；daemon 不阻止进程退出 |
| R2 | naive 时间无时区 | 跨时区部署、夏令时场景不符合直觉 | 全程 naive datetime，ISO 字符串不带时区后缀；文档明示；不做时区转换（Out of Scope） |
| R3 | DOW=7 归一化与范围展开（`5-7` → {5,6,0}） | 星期计算易错 | 单一归一化函数；TS-04 专项 + TS-10 对拍覆盖 |
| R4 | S5 coalesce 排水粒度歧义（「每次 tick 一次」vs「一次排完」） | A4 测试口径不确定 | 选「每次 tick 一次」，标确认项 C2；TS-23 按此口径编写 |
| R5 | 章程未给出多项默认值（grace/policy/retries/backoff/timeout/tick/文件路径） | 实现与测试可能各取默认 | 全部显式列出（4.1/4.2），标确认项 C1；测试一律显式传参，不依赖默认值 |
| R6 | 内联模式下 timeout 不生效 | 测试可能误判超时逻辑 | 文档与注释明示；timeout 测试强制真实线程池（TS-27） |
| R7 | 虚拟时钟与真实时钟行为差异（抖动、lateness） | 真实模式下迟到触发 misfire 分支 | 统一 Clock 抽象，核心不直接取系统时间；真实模式由 grace（默认 60s）吸收正常抖动 |
| R8 | 单写者假设：CLI 与 run 并发写同一存储会互相覆盖 | 数据竞争丢更新 | 文档明示单写者；不做文件锁（Out of Scope）；如确需多写者，走章程变更流程 |
| R9 | 4 年窗口逐分钟蛮扫性能差（约 210 万次/表达式） | 测试慢 | 实现用字段递进；参考实现仅在测试中使用，且随机表达式采样保证快速命中；`NoMatchError` 有界（< 5s，TS-11） |
| R10 | CLI `run` 无法加载任意用户函数（注册表是进程内映射） | CLI 只能跑内置函数 | 内置 `noop`/`fail` 满足 A8；文档说明使用方需自行注册后再启动 |

### 7.2 关键假设汇总（章程未明确处）

1. 大小写不敏感仅作用于月份/星期名称段，不扩大到整个表达式；
2. 表达式 `strip()` 后以一个或多个空白分隔；
3. 4 年上界按日历年后推计算；
4. 正常推进锚定 `scheduled_time`（cron: `next_after(scheduled)`；interval: `scheduled + interval`）；
5. 每个 Job 每个 tick 最多触发一次；未追平留待后续 tick；
6. tick 内多 Job 按 `name` 字典序处理；
7. 新建 Job 时计算 `next_run_time`；加载时一律取落盘值不重算；
8. 注册失败（未注册函数）计入 attempts、可重试；
9. 永不命中表达式 → Job 标记无未来触发（`next_run_time=null`），静默停摆不崩溃；
10. 保存时机：变更即存 + 每 tick 末统一存；
11. 单次退避上限作用于单次时长（非累计）；
12. 加载期表达式非法视为存储损坏（`StoreError`）；
13. `trigger_now` 记录 `scheduled_time` = 触发时刻；
14. CLI 错误信息格式 `error: <message>`，成功产出单行 ASCII；
15. A4 测试计数口径：只统计 `scheduled_time` 早于恢复时刻的执行。

### 7.3 编排者确认项（C1 ~ C6）

| ID | 歧义 | 本文档选择的解释 |
|---|---|---|
| C1 | 章程未给出默认值（`misfire_grace`、`misfire_policy`、`max_retries`、`retry_backoff`、`timeout`、`--tick`、`--store`/`--log` 路径） | 分别取 60.0s、`discard`、0、1.0s、null、1.0s、`cron_jobs.json`/`cron_runs.jsonl` |
| C2 | S5 coalesce「每次 tick 运行一次」与「直到追平当前时刻」的粒度歧义 | 每次 tick 最多排水一次，需多次 tick 追平 |
| C3 | Job 是否需要 args/kwargs | 支持（可选，默认空），仅限 JSON 可序列化值 |
| C4 | S12 trigger_now 是否暴露为 CLI 子命令 | 不暴露（S10 仅列六个子命令），仅作为 API |
| C5 | remove/pause/resume 操作不存在任务的退出码 | 2（参数校验错误），而非 1 |
| C6 | CLI add 同名任务的行为 | 报错（退出码 2），不静默覆盖 |

## 8. 明确不做（Out of Scope）

与章程 2.2 完全一致，未新增范围：

1. 分布式 / 多进程调度；
2. Web UI；
3. 认证授权；
4. 时区数据库（统一 naive 本地时间）；
5. 第三方依赖（仅标准库）；
6. 注册为操作系统服务。

---

## 附：本文档对后续阶段的约束

- 方案设计（02）不得反转 S1–S12 与本文件 4.x 契约；CLI flag 名、JSON 键名、异常名、退出码如需调整，必须回写本文档与章程并记录理由；
- 编码（03）按 4.x 契约实现；测试（04）按第 5 节场景落地并满足 A10 用例数；验收（05）以 A1–A10 与本文档验收标准为准。

---

## 附 B：编排者审校记录（2026-10-14，阶段 1 门禁）

编排者对本文档做了独立审校（阶段门禁），发现并修正 1 处需求级内部矛盾，未反转任何语义决策：

| # | 问题 | 定位 | 修正 |
|---|---|---|---|
| G1 | TS-20 / TS-22 / TS-24 的期望值自相矛盾：既断言 discard / fire_once 恢复后 `next_run_time` 跳到 10:17（> now），又断言「10:16、10:17 各正常触发 1 次」。按 FR-09 规则 2/4，`next_after(10:16:00)` = 10:17:00，10:16 周期在跳过之列，不可能再触发。 | 需求文档缺陷（期望值笔误，非语义歧义） | 三处期望统一改为「10:16 周期被跳过；时钟推进到 10:17:00 后正常触发 1 次」 |

另确认无误的关键点：2026-10-14 为周三（TS-07 / TS-08 的星期推演正确）；TS-23 coalesce 的「第 11 次 tick 触发 10:16」与 grace=0 边界自洽；TS-25 的 lateness=grace 走正常分支自洽。

对架构阶段的附加要求：cron 字段名称须同时支持命名范围（如 `MON-FRI`、`JAN-MAR`）；TS 未覆盖该组合，但不得视为非法输入。
