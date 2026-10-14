# 定时任务调度系统（cronsched）需求文档

## 1. 背景与目标

为单机 Python 应用提供一个零第三方依赖的定时任务调度库：用户用 5 段 cron 表达式或固定秒数间隔定义"什么时候跑哪个函数"，调度器按时间推进到点触发，通过线程池并发执行，支持单任务超时放弃、指数退避重试、三种错过周期（misfire）补救策略，并把任务配置与下次运行时间以 JSON 原子落盘，保证重启后不丢失；每次执行追加一条 JSONL 运行记录。系统同时提供一个 6 个子命令的 CLI 管理任务生命周期。

## 2. 技术约束

- 语言：Python 3.9+，交付形态为可通过 `import cronsched` 使用的库 + `python -m cronsched.cli` 命令行入口
- 纯标准库实现，零第三方依赖（禁止 import 任何第三方包）
- 时间语义：naive datetime（本地时间），分钟粒度；ISO 格式 `YYYY-MM-DDTHH:MM:SS`
- 可调用对象以名称引用（不序列化 callable），运行期经函数注册表解析
- 时钟、sleep、线程池必须可注入（测试用虚拟时钟与假 sleep 保证确定性）

## 3. 功能点

### FP-01 cron 表达式解析 parse_cron
- 描述：解析 5 段标准 cron 表达式（分 时 日 月 周）为各字段的命中集合
- 输入：expression: str
- 输出：dict，含 minute/hour/day/month/dow 五个 set（dow 已把 7 归一化为 0）
- 规则：段间以一个或多个空白分隔，段数不为 5 抛 CronParseError；每段支持 `*`、单值、范围 `a-b`、步长 `*/n` 与 `a-b/n`、列表 `a,b,c` 及组合；month 支持 JAN-DEC、dow 支持 SUN-SAT（大小写不敏感）；值域越界、`*/0`、逆序范围 `5-1`、数字段含字母均抛 CronParseError；支持宏（大小写不敏感）`@daily`=`0 0 * * *`、`@hourly`=`0 * * * *`、`@weekly`=`0 0 * * 0`、`@monthly`=`0 0 1 * *`、`@yearly`=`0 0 1 1 *`
- 样例：`"*/15 * * * *"` → minute={0,15,30,45}；`"0 9-17/2 * * *"` → hour={9,11,13,15,17}；`"@DAILY"` 等价 `"0 0 * * *"`；`"bad"`、`"* * * *"`（4 段）、`"60 * * * *"`、`"*/0 * * * *"`、`"5-1 * * * *"`、`"@daily extra"` 均抛 CronParseError
- 类别：功能

### FP-02 下一次触发时刻计算 next_after
- 描述：给定起始时刻，计算严格晚于它的下一个命中时刻（分钟粒度，秒固定为 0）
- 输入：fields: dict（FP-01 的输出）, after: datetime
- 输出：datetime
- 规则：忽略 after 的秒与微秒，从下一分钟起算；日(DOM)与周(DOW)均被限制（均非 `*`）时为「或」，否则为「与」；搜索上界 4 年，无命中抛 NoMatchError（如 `0 0 30 2 *`）；单次调用墙钟 < 5s
- 样例：`* * * * *` @ 10:00:30 → 10:01:00；`0 0 13 * 5` @ 2026-10-14（周三）→ 2026-10-16（周五，「或」语义先到者准）；`0 0 29 2 *` @ 2027-03-01 → 2028-02-29（闰年）
- 类别：功能

### FP-03 触发器 Trigger
- 描述：以触发器对象封装触发规则，调度器只问"下一次什么时候跑"
- 输入：CronTrigger(expression) / IntervalTrigger(seconds)
- 输出：对象，方法 get_next_fire_time(after: datetime) -> datetime
- 规则：CronTrigger 构造时即解析表达式，非法立即抛 CronParseError；表达式永不命中时 get_next_fire_time 抛 NoMatchError；IntervalTrigger 的 seconds 必须 > 0 否则抛 ValueError；IntervalTrigger 的下一次触发 = 上次 scheduled_time + interval（锚定推进防漂移）；两者均提供 to_dict()/from_dict() 序列化（cron 保留原始表达式字符串）
- 样例：CronTrigger("*/15 * * * *").get_next_fire_time(2026-10-14T10:07:00) → 2026-10-14T10:15:00；IntervalTrigger(1) 首次 next_run_time = 加入时刻 + 1s，触发后下一次 = 上次 scheduled_time + 1s
- 类别：功能

### FP-04 Job 模型与函数注册表
- 描述：一条配置说清一个任务；配置里只写函数名字，运行期按名字找到真正的函数
- 输入：name/func/trigger 必填；misfire_policy（discard/coalesce/fire_once，默认 discard）、misfire_grace（默认 60.0）、max_retries（默认 0）、retry_backoff（默认 1.0）、retry_backoff_max（默认 300.0）、timeout（默认 null）、enabled（默认 true）、args/kwargs（默认空）可选
- 输出：Job 对象，支持 to_dict()/from_dict() round-trip；FunctionRegistry 提供 register/get/unregister
- 规则：name 非空 ASCII 且库内唯一（同名 add 报错）；misfire_grace<0、max_retries<0、retry_backoff<=0、retry_backoff_max<=0、timeout<=0、misfire_policy 非法均抛 ValueError；get(name) 未注册抛 FunctionNotFoundError；to_dict() 中 func 为字符串、trigger 保留原始表达式
- 样例：含全部可选字段的 Job round-trip 后所有字段相等；缺 name/func/trigger 构造即抛错；misfire_grace=-1 抛 ValueError
- 类别：功能

### FP-05 JSON 持久化
- 描述：任务配置与 next_run_time 以 JSON 原子落盘；退出再进来原样还在
- 输入：path: str
- 输出：save(jobs) / load() -> list[Job]
- 规则：文件缺失视为空库（0 个 job）不报错；原子写（同目录临时文件 + os.replace），正常与失败路径均不得残留 .tmp；JSON 语法错误或 version != 1 或 job 记录结构非法或 trigger 表达式无法重新解析 → 抛 StoreError 且原文件字节不动；序列化用 sort_keys=True 保证确定性
- 样例：save 后同目录无 .tmp；手工写坏 JSON（`{"version":1,"jobs":[{"name":`）→ load 抛 StoreError 且文件字节前后一致；删除存储文件 → load 得空库
- 类别：功能

### FP-06 调度器主循环
- 描述：调度器看着时钟往前走，到点把任务交给执行器；测试中时间完全可控
- 输入：Scheduler(store_path, clock, executor, sleep)；tick(now=None)
- 输出：每个 tick 对 enabled=True 且 next_run_time <= now 的 Job 触发一次；tick 内多 Job 按 name 字典序；每 Job 每 tick 最多触发一次
- 规则：新建 Job 时 cron 取 next_after(now)、interval 取 now+interval；加载后 next_run_time 一律取落盘值不重算；enabled=False 时跳过（不运行、不推进 next_run_time），恢复后按 misfire 策略处理积压；trigger_now(name) 立即执行一次（禁用状态也执行）且不影响 next_run_time，任务不存在抛 JobNotFoundError；misfire 在 lateness = now - scheduled_time > misfire_grace 时生效：discard 不运行并跳到未来下一个匹配时刻；coalesce 每个 tick 为最早一个错过周期运行一次并前进一个周期（排水式）；fire_once 仅为最早一个错过周期运行一次后跳到未来；lateness <= grace 走正常迟到分支（执行一次并推进一个周期）；永不命中表达式加载后 next_run_time=null 且主循环跳过不崩溃
- 样例：暂停期间到期 tick 零运行记录且 next_run_time 不变；连续错过 10 个周期时 discard/coalesce/fire_once 运行次数分别为 0/10/1；lateness=grace 走正常分支
- 类别：功能

### FP-07 执行器（并发/超时/重试）
- 描述：多个任务同时到期并行跑；单个任务跑太久被判超时放弃；失败按指数退避重试
- 输入：Executor(max_workers=4, sleep=time.sleep)；submit(func, args, kwargs, timeout, max_retries, retry_backoff, retry_backoff_max)
- 输出：单次触发的结果（status/duration/error/attempts）
- 规则：基于 ThreadPoolExecutor，注入 None 表示无池内联执行（确定性测试用）；主循环提交后不阻塞等待；timeout 到点即放弃等待并记 status="timeout"（底层线程作为 daemon 继续执行，已知限制），超时计入一次尝试可触发重试；总尝试次数 = max_retries + 1；第 attempt 次失败后 sleep min(retry_backoff × 2^(attempt-1), retry_backoff_max)；一次触发的所有尝试共享同一条运行记录（attempts 字段体现次数）
- 样例：失败 3 次后第 4 次成功 → attempts=4 且 sleep 序列 [b, 2b, 4b]；挂起函数 timeout=0.2 → 调用方 <= 1s 内返回 status="timeout"；retry_backoff=100、retry_backoff_max=300、5 次重试全败 → sleep 序列 [100,200,300,300,300]、attempts=6
- 类别：功能

### FP-08 运行记录
- 描述：每次执行都留一行结构化 JSONL 日志，成败耗时一目了然
- 输入：RunLogger(path)
- 输出：append(record) / read() -> list[dict]
- 规则：每次执行一条单行 JSON 追加写不覆盖；字段：job/scheduled_time/started_at/finished_at/duration/status/attempts/error；status ∈ {success, error, timeout}，error 成功时为 null；时间字段 ISO 8601 naive；记录在"一次触发的所有尝试结束后"写入一次
- 样例：连续执行成功/异常/超时各 1 次 → JSONL 恰 3 行且 8 字段齐全；再次执行 → 行数变 4 且旧行字节不变
- 类别：功能

### FP-09 CLI
- 描述：命令行管任务（增删查、暂停恢复）并启动调度，退出码能让脚本可靠判断成败
- 输入：`python -m cronsched.cli [--store PATH] <subcommand> [options]`
- 输出：stdout 单行 ASCII 成功产出；stderr 单行 `error: <message>`
- 规则：子命令 add/list/remove/pause/resume/run；退出码 0 成功、2 用法与参数校验错误（缺必填、表达式非法、数值非法、同名 add、操作不存在任务）、1 运行时错误（存储损坏）；run --duration N 运行 N 秒后正常退出，收到 KeyboardInterrupt 优雅退出且退出码 0；内置注册 noop（成功）与 fail（抛 RuntimeError）两个演示函数
- 样例：add j1 → 0；重复 add → 2；remove ghost → 2；坏存储下 list → 1；run --duration 2（interval=1s 任务）→ 0 且日志 >= 1 行
- 类别：功能

## 4. 异常与边界要求（重要）

- 全部函数对非法输入必须抛出明确异常（CronParseError/NoMatchError/ValueError/StoreError/JobNotFoundError/FunctionNotFoundError），禁止静默吞错
- 边界：cron 字段边界值（0/59、0/23、1/31、1/12、0/7）、闰年 2 月 29、4 年窗口无匹配、lateness 恰好等于 grace、退避恰好达到上限、timeout=0.2s 的挂起函数、20 个任务同时到期
- 确定性：注入虚拟时钟/假 sleep/内联池时，同一场景重复执行产生逐条一致的运行记录

## 5. 验收标准

- 全部功能点实现且 `import cronsched` 可用，`python -m cronsched.cli --help` 退出码为 0
- 功能 / 异常 / 边界三类用例全部通过（每个功能点每类至少 1 条，覆盖率 100%）
- 无阻塞级代码审查问题，全部缺陷闭环
- 待澄清问题在交付前完成协商闭环并写入技术方案

## 6. 待澄清问题

- Q1：coalesce 策略「每次 tick 运行一次」与「一次排完所有积压」的粒度未明确，请研发与测试协商统一口径（建议每次 tick 最多排水一次）
- Q2：run --duration 到期时的退出行为（优雅停止并保存）是否需要等待在途任务完成，请协商统一口径（建议等待在途任务完成后退出）
