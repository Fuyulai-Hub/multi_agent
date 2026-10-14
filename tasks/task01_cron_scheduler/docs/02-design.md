# 02 · 方案设计（Design）

> 角色：本文档是 task01_cron_scheduler 的方案设计，由架构 agent 依据 `docs/00-charter.md`（S1–S12 / A1–A10）与 `docs/01-requirements.md`（FR-01~14 / 4.x 契约 / TS-01~30，含附 B 编排者修正）编写。
> 下游约定：编码 agent（03）按本文档签名逐字实现；测试 agent（04）按本文档契约与第 9 节映射逐字使用；验收 agent（06）以章程 A1–A10 与需求 4.x 为准。
> 本文档不反转 S1–S12 与 4.x 契约；所有自行选择的口径以 `（设计假设：…）` 标注，汇总见第 12 节偏差声明。
> 标识符、签名、JSON 键名、命令、异常名均为 ASCII；叙述为中文。编写时间：2026-10-14。

---

## 1. 文档目的与阅读指南

| 读者 | 必读章节 | 说明 |
|---|---|---|
| 编码 agent | 2–8 全章 | 第 4 章签名逐字实现；第 5 章算法按伪代码落地 |
| 测试 agent | 4、7、9 章 | 第 4 章是被测 API 的唯一事实来源；第 9 章是 TS→用例映射与夹具施工图 |
| 验收 agent | 3、9、11 章 | 模块边界、测试映射、风险与偏差 |

术语约定：

- `Job`：一条定时任务配置（名字、函数、触发器、失败/错过策略、`next_run_time`）。
- `tick`：调度器的一次时间推进处理，取 `now = clock.now()` 并处理所有到期 Job。
- `scheduled_time`：某次执行对应的计划触发时刻（= 触发前 Job 的 `next_run_time`）。
- `lateness`：`(now - scheduled_time).total_seconds()`， misfire 判定量。
- `蛮扫参考实现`：tests/ 内独立的逐分钟暴力扫描，仅用于 A2 对拍。

---

## 2. 总体架构

### 2.1 目录布局（编码 agent 唯一允许新建的文件集合）

```
tasks/task01_cron_scheduler/
├── demo.py                     # F9 虚拟时钟 24h 自检（自带期望值断言）
├── acceptance.py               # F10 自动验收脚本（退出码即门禁）
├── scheduler/
│   ├── __init__.py             # 公共 API 再导出
│   ├── errors.py               # 异常层级（全部模块的地基）
│   ├── clocks.py               # Clock 协议 / RealClock / VirtualClock
│   ├── cron_parser.py          # cron 表达式解析 + next_after 字段递进算法
│   ├── triggers.py             # Trigger / CronTrigger / IntervalTrigger
│   ├── registry.py             # FunctionRegistry（名称 -> callable）
│   ├── jobs.py                 # Job 模型 + to_dict/from_dict
│   ├── runlog.py               # RunRecord / RunLogger（JSONL 运行记录）
│   ├── store.py                # JobStore（JSON 原子写持久化）
│   ├── executor.py             # JobExecutor（线程池/内联、超时、重试）
│   ├── scheduler.py            # CronScheduler（主循环、tick、misfire 分支）
│   ├── builtins.py             # noop / fail 内置函数
│   └── cli.py                  # argparse 六子命令 + 退出码映射
├── tests/
│   ├── __init__.py             # 空文件，使 tests 成为包
│   ├── support.py              # 公共夹具（非测试文件，discover 不收集）
│   ├── test_cron_parser.py     # TS-01~06
│   ├── test_next_after.py      # TS-07~11
│   ├── test_triggers.py        # TS-12~13
│   ├── test_jobs.py            # TS-14~15
│   ├── test_registry.py        # TS-16
│   ├── test_store.py           # TS-17~18
│   ├── test_scheduler_core.py  # TS-19~21
│   ├── test_misfire.py         # TS-22~25
│   ├── test_executor.py        # TS-26~28
│   ├── test_cli.py             # TS-29
│   └── test_gates.py           # TS-30
└── docs/                       # 00~05、04b、REPORT（本阶段只写 02-design.md）
```

> （设计假设：`tests/__init__.py` 为空文件；测试文件内跨文件引用一律 `from tests.support import ...` 绝对包导入，`support.py` 内 `from scheduler.xxx import ...`；从任务根目录执行 `$PY -m unittest discover -s tests -v` 时 cwd 在 `sys.path`，导入成立。DEV-12）

### 2.2 模块分层框图

```
┌────────────────────────────────────────────────────────────────────────┐
│  入口层（任务根目录）                                                    │
│    demo.py                acceptance.py            scheduler/cli.py    │
│    （虚拟时钟自检）        （自动验收门禁）           （六子命令 + 退出码）│
└───────┬───────────────────────┬───────────────────────┬────────────────┘
        │ import                │ subprocess 调用        │ import
        ▼                       ▼                       ▼
┌────────────────────────────────────────────────────────────────────────┐
│  调度层   scheduler/scheduler.py                                        │
│    CronScheduler —— 唯一的编排者：add/remove/pause/resume/              │
│    trigger_now/tick/run/stop/close/save                                │
└───┬───────┬───────┬───────┬───────┬───────┬───────┬────────────────────┘
    │       │       │       │       │       │       │
    ▼       ▼       ▼       ▼       ▼       ▼       ▼
┌───────┐┌───────┐┌───────┐┌───────┐┌───────┐┌───────┐┌───────────────────┐
│jobs.py││trigger││store  ││executor││registry││runlog ││clocks.py          │
│       ││s.py   ││.py    ││.py    ││.py    ││.py    ││Clock/Real/Virtual │
└──┬────┘└──┬────┘└──┬────┘└──┬────┘└──┬────┘└───────┘└───────────────────┘
   │        │        │        │        │
   ▼        ▼        ▼        ▼        ▼
┌──────────────────────────────────────────────┐
│  基础层  errors.py   cron_parser.py          │
│           （异常层级）  （字段递进 next_after） │
└──────────────────────────────────────────────┘
```

### 2.3 模块职责表

| # | 模块 | 职责（一句话） | 不负责 |
|---|---|---|---|
| M1 | `errors.py` | 定义全部自定义异常及其继承关系 | 不含任何逻辑 |
| M2 | `clocks.py` | 时间来源抽象：`Clock.now()`；真实/虚拟两种实现 | 不管调度、不管 sleep |
| M3 | `cron_parser.py` | 5 段语法（含命名/宏/步长）解析为字段集合；`next_after` 字段递进跳跃 | 不知 Job/调度存在 |
| M4 | `triggers.py` | `get_next_fire_time(after)` 统一触发器接口；cron/interval 两种实现 | 不管 misfire、不管执行 |
| M5 | `registry.py` | 名称→callable 内存映射，运行期解析 | 不序列化、不管调用 |
| M6 | `jobs.py` | Job 配置模型 + 值域校验 + JSON round-trip | 不管触发计算、不管执行 |
| M7 | `runlog.py` | `RunRecord` 数据结构 + JSONL 追加（持锁） | 不管重试聚合 |
| M8 | `store.py` | Job 列表的 JSON 原子读写与加载校验 | 不管 next_run_time 计算 |
| M9 | `executor.py` | 单次执行（超时）+ 重试循环 + 退避 + 记录回调 | 不管何时触发、不管持久化 |
| M10 | `scheduler.py` | tick 主流程、到期判定、misfire 分支、推进与落库 | 不管单次执行细节 |
| M11 | `builtins.py` | `noop`/`fail` 内置函数注册 | 不管其他函数 |
| M12 | `cli.py` | 参数解析、子命令编排、退出码映射 | 不含调度逻辑 |

顶层 `demo.py` / `acceptance.py` 只 import `scheduler` 包与标准库，不含可复用业务逻辑。

### 2.4 依赖方向（无环，编码必须遵守）

允许的 import 边（不在表中的 import 一律禁止，A9 静态检查会发现问题）：

```
errors.py      -> （无）
clocks.py      -> （无）
cron_parser.py -> errors
registry.py    -> errors
runlog.py      -> （无）
triggers.py    -> errors, cron_parser
jobs.py        -> errors, triggers
store.py       -> errors, jobs, triggers
executor.py    -> errors, registry, clocks, runlog
scheduler.py   -> errors, clocks, triggers, jobs, registry, store, executor, runlog
builtins.py    -> registry
cli.py         -> errors, clocks, triggers, jobs, registry, runlog, store, executor, scheduler, builtins
__init__.py    -> 上述全部（仅再导出）
demo.py        -> scheduler（包级）
acceptance.py  -> scheduler（包级）+ 标准库
```

环检测说明：以 `errors/clocks/registry/cron_parser/runlog` 为叶子，向上仅 `triggers -> cron_parser`、`jobs -> triggers`、`store -> jobs/triggers`、`executor -> registry/clocks/runlog`、`scheduler -> 全部`，构成 DAG。禁止出现 `cron_parser -> triggers`、`triggers -> jobs`、`store -> scheduler`、`executor -> scheduler` 等反向边。

### 2.5 公共 API 再导出（`scheduler/__init__.py`）

```python
__all__ = [
    "SchedulerError", "CronParseError", "NoMatchError", "StoreError",
    "FunctionNotFoundError", "JobNotFoundError", "JobAlreadyExistsError",
    "Clock", "RealClock", "VirtualClock",
    "CronExpression", "parse_field", "MACROS",
    "Trigger", "CronTrigger", "IntervalTrigger", "trigger_from_dict",
    "FunctionRegistry", "Job", "MISFIRE_POLICIES",
    "RunRecord", "RunLogger", "JobStore", "STORE_VERSION",
    "JobExecutor", "CronScheduler", "register_builtins",
]
```

---

## 3. 公共基础模块契约

### 3.1 `scheduler/errors.py`

```python
class SchedulerError(Exception):
    """Base class for all scheduler errors; message must be ASCII."""

class CronParseError(SchedulerError, ValueError):
    """Raised when a cron expression is malformed (parse layer)."""

class NoMatchError(SchedulerError):
    """Raised by next_after when no match exists within the 4-year bound."""

class StoreError(SchedulerError):
    """Raised when the job store cannot be loaded or saved."""

class FunctionNotFoundError(SchedulerError, LookupError):
    """Raised when a function name is not present in the registry."""

class JobNotFoundError(SchedulerError, LookupError):
    """Raised when a job name does not exist in the scheduler."""

class JobAlreadyExistsError(SchedulerError, ValueError):
    """Raised when add_job is called with a name already in use."""
```

设计要点：

- `CronParseError` / `JobAlreadyExistsError` 同时继承 `ValueError`，使「统一捕 `ValueError` → CLI 退出码 2」的映射成立（TS-15 断言 `ValueError` 或其子类，兼容）。
- 全部异常消息 ASCII，不含换行（CLI 单行 `error: <message>` 契约）。

### 3.2 `scheduler/clocks.py`

```python
import datetime as _dt

class Clock:
    """Structural protocol (docstring-declared, no runtime ABC):
    now() -> datetime  (naive, local time, minute-or-second granularity)."""

class RealClock:
    def now(self) -> "_dt.datetime":
        """Return datetime.now() (naive local time)."""

class VirtualClock:
    def __init__(self, start: "_dt.datetime") -> None:
        """start: naive datetime; initial now()."""
    def now(self) -> "_dt.datetime":
        """Return the current virtual moment."""
    def advance(self, seconds: float) -> None:
        """Move now() forward by seconds (>= 0); negative -> ValueError.
        Accepts int/float. Monotonic within a test."""
    def set(self, moment: "_dt.datetime") -> None:
        """Set now() to moment (may move backward; test convenience)."""
```

不变量与说明：

- INV-C1：`VirtualClock.now()` 在无 `advance/set` 调用时返回值不变（纯读）。
- INV-C2：`advance` 的参数为秒（float/int），分钟粒度模拟传 `60`；秒以下模拟（interval=1s）通过一次 `advance(60)` 跨越多个计划时刻。
- 线程安全口径（设计假设）：`VirtualClock` 仅保证「多线程读 `now()` 与单线程 `advance/set`」安全——`_now` 为单个 datetime 属性，赋值在 CPython GIL 下原子；`advance/set` 不持锁，禁止两个线程同时写。真实池模式下 executor 工作线程只读 `now()`。DEV 级说明见第 6 章。
- `Clock` 不定义为 `typing.Protocol` 运行时依赖之外的 ABC：用普通类 + docstring 声明结构，产品与测试的时钟（鸭子类型）均可注入（N4 可测试性）。

---

## 4. 模块 API 契约（编码逐字实现 / 测试逐字使用）

> 本章每个签名中的参数名、默认值、返回类型、抛出异常均为契约的一部分。私有辅助函数给出职责说明（编码可实现细节，但不得改公开行为）。

### 4.1 `scheduler/cron_parser.py`

```python
import datetime as dt

MACROS: dict[str, str] = {
    "@YEARLY":  "0 0 1 1 *",
    "@ANNUALLY":"0 0 1 1 *",
    "@MONTHLY": "0 0 1 * *",
    "@WEEKLY":  "0 0 * * 0",
    "@DAILY":   "0 0 * * *",
    "@HOURLY":  "0 * * * *",
}
# 注：@ANNUALLY 为 @yearly 的别名（Vixie 惯例，S1 只要求 @yearly；
#     别名不改变任何 S1 契约，属兼容性补充）。

class CronField:
    """frozen dataclass: one parsed cron field."""
    values: frozenset[int]     # 展开后的允许值集合
    restricted: bool           # False 当且仅当原始字段 strip() 后恰为 "*"
    raw: str                   # 原始字段文本（调试用）

def parse_field(raw: str, lo: int, hi: int,
                names: dict[str, int] | None = None) -> CronField:
    """Parse one cron field.
    raw: the field text (no surrounding whitespace expected, stripped anyway).
    lo/hi: inclusive value bounds (dow uses lo=0, hi=7 with post-normalization).
    names: optional case-insensitive name map (month JAN-DEC / dow SUN-SAT).
    Raises CronParseError on any malformed part.
    私有辅助: _parse_part(part, lo, hi, names) -> set[int];
              _value(token, lo, hi, names) -> int (名称/数字/越界校验)."""

class CronExpression:
    def __init__(self, expression: str) -> None:
        """Parse expression immediately (fail-fast); raises CronParseError.
        Accepts macros (case-insensitive, e.g. '@daily', '@DAILY')."""
    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        """Alias of the constructor (same behavior)."""

    # --- 公开只读属性（TS-01 直接断言这些集合） ---
    @property
    def expression(self) -> str:
        """Original expression text as given (macro preserved verbatim)."""
    @property
    def minutes(self) -> frozenset[int]: ...
    @property
    def hours(self) -> frozenset[int]: ...
    @property
    def days_of_month(self) -> frozenset[int]: ...
    @property
    def months(self) -> frozenset[int]: ...
    @property
    def days_of_week(self) -> frozenset[int]: ...   # 0=Sunday .. 6=Saturday
    @property
    def dom_restricted(self) -> bool: ...           # 原始 DOM 字段 != "*"
    @property
    def dow_restricted(self) -> bool: ...           # 原始 DOW 字段 != "*"

    def next_after(self, dt: "_dt.datetime") -> "_dt.datetime":
        """Return the next matching moment strictly after dt (second=0).
        dt's seconds/microseconds are ignored; search starts at the next minute.
        Raises NoMatchError if no match within 4 calendar years.
        私有辅助: _day_matches(day) -> bool (DOM/DOW 或与语义);
                  _cron_dow(date) -> int (python weekday -> cron 0=Sun)."""

    def matches(self, moment: "_dt.datetime") -> bool:
        """Minute-granularity membership test (debug/test aid).
        Uses the same _day_matches as next_after; NOT an independent
        implementation (A2 cross-check uses tests/support.py brute force)."""
```

字段解析规则（与 S1 / FR-01 逐条对应，编码照此实现）：

1. 顶层：`expression.strip()`；以 `@` 开头 → 宏路径：整串大写后查 `MACROS`，命中且**无其他 token** → 用等价表达式继续；未命中 → `CronParseError`；宏后跟其他段（`@daily extra`）→ `CronParseError`。
2. 非宏：`expression.split()`（任意空白序列分隔，容忍 Tab/连续空格）；段数 != 5 → `CronParseError`。段序：minute hour day-of-month month day-of-week。
3. 每段按 `,` 拆 part；空 part（`1,,2`、尾逗号）→ `CronParseError`。
4. 每 part：
   - `*` 独占整段时：`values = range(lo, hi+1)`（dow 为 `range(0,8)` 后归一化），`restricted=False`；
   - 含 `/`：`base/step`。`step` 必须为纯数字且 `int(step) > 0`（`*/0` 非法）；`base == "*"` → `range(lo, hi+1, step)`；`base` 为 `a-b` → 校验 `b >= a` 后 `range(a, b+1, step)`；其余（含 `a/n`，如 `5/15`）→ `CronParseError`（设计假设：S1 仅列出 `*/n` 与 `a-b/n` 两种步长形式，DEV-08）；
   - 含 `-`（且不含 `/`）：`a-b`，`b >= a` 否则 `CronParseError`（`5-1` 非法）；`values = range(a, b+1)`；
   - 单值：`_value(token, lo, hi, names)`；
   - 列表中含 `*`（如 `1,*`）→ `CronParseError`（设计假设：`*` 只能独占整段，DEV-08）。
5. `_value`：`names` 非 None 时先 `token.upper()` 查名称表（大小写不敏感，仅名称段）；命中返回值。否则 token 必须为纯数字串（`token.isdigit()`，允许前导零，不允许 `+`/`-`/小数）；`lo <= v <= hi` 否则 `CronParseError`（dow 段 `hi=7`）。
6. 名称段允许数字与名称混用（如 month 段 `JAN,3`）；数字段（minute/hour/dom）出现字母 → `CronParseError`；month/dow 段出现非名称字母 → `CronParseError`。
7. DOW 归一化：字段展开完成后 `7 -> 0`（`5-7` → `{5,6,7}` → `{5,6,0}`；`7` → `{0}`；`*` → `{0..6}`）。
8. 命名范围（附 B 附加要求）：`MON-FRI` → `{1,2,3,4,5}`，`JAN-MAR` → `{1,2,3}`——名称参与 `a-b` 分支，边界同为名称；逆序（`FRI-MON`）→ `CronParseError`。
9. `restricted` 是段级标志：整段 strip 后 `== "*"` → False；`*/2`、`1-5`、列表等 → True（设计假设：遵 S2 字面「均非 `*`」，与 Vixie「星号开头即 unrestricted」略有差异，DEV-09）。

`next_after` 算法见 5.1（伪代码级）；此处固定其可观测契约：

- 结果 `second == 0` 且 `result > dt`（即使 `dt` 秒为 0）。
- 4 年上界：`limit = dt 截断到分钟 + 1 分钟` 后推 4 个日历年的同一月/日/时/分；搜索中当前时刻 `> limit` 仍无命中 → `NoMatchError`。
- 与逐分钟蛮扫可观测行为完全一致（A2 对拍）。

### 4.2 `scheduler/triggers.py`

```python
class Trigger:
    """Base class (docstring-declared interface)."""
    def get_next_fire_time(self, after: dt.datetime) -> dt.datetime:
        """Return the next fire moment strictly after `after`.
        Raises NoMatchError if no such moment exists."""
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "Trigger": ...   # 仅子类实现

class CronTrigger(Trigger):
    def __init__(self, expression: str) -> None:
        """Parse immediately; CronParseError on malformed input."""
    @property
    def expression(self) -> str: ...                   # 原文（宏保留）
    @property
    def cron(self) -> CronExpression: ...
    def get_next_fire_time(self, after: dt.datetime) -> dt.datetime:
        """Delegate to CronExpression.next_after; NoMatchError if never matches."""
    def to_dict(self) -> dict:
        """{'type': 'cron', 'expression': '<original text>'}"""
    @classmethod
    def from_dict(cls, data: dict) -> "CronTrigger":
        """data['expression'] missing/not str -> ValueError; parse error -> CronParseError."""

class IntervalTrigger(Trigger):
    def __init__(self, seconds: float) -> None:
        """seconds must be a positive int/float (bool excluded); else ValueError."""
    @property
    def seconds(self) -> float: ...                    # 原值（int 保持 int）
    def get_next_fire_time(self, after: dt.datetime) -> dt.datetime:
        """Return after + timedelta(seconds=self.seconds)."""
    def to_dict(self) -> dict:
        """{'type': 'interval', 'seconds': <number>}"""
    @classmethod
    def from_dict(cls, data: dict) -> "IntervalTrigger":
        """data['seconds'] missing/not number/<= 0 -> ValueError."""

def trigger_from_dict(data: dict) -> Trigger:
    """Factory for the persisted trigger object.
    data not a dict / 'type' missing or unknown / inner field invalid
    -> ValueError. 'cron' -> CronTrigger.from_dict; 'interval' -> IntervalTrigger.from_dict."""
```

不变量：

- INV-T1：`CronTrigger` 构造即解析，绝不在运行期才暴露语法错误（FR-03.1 fail-fast）。
- INV-T2：`get_next_fire_time` 结果严格晚于 `after`。
- INV-T3：`IntervalTrigger(seconds)` 的 `seconds > 0`；`IntervalTrigger(0)` / `(-5)` → `ValueError`（TS-15）。
- INV-T4：`to_dict()` → `from_dict()` round-trip 保真（expression 原文含宏）。

### 4.3 `scheduler/registry.py`

```python
class FunctionRegistry:
    def __init__(self) -> None: ...
    def register(self, name: str, func: Callable) -> None:
        """Overwrite on duplicate name. name must be non-empty ASCII str
        (else ValueError); func must be callable (else ValueError)."""
    def unregister(self, name: str) -> None:
        """Remove mapping; unknown name -> FunctionNotFoundError."""
    def get(self, name: str) -> Callable:
        """Return the callable; unknown name -> FunctionNotFoundError."""
    def __contains__(self, name: object) -> bool: ...
    def names(self) -> list[str]:
        """Sorted list of registered names (determinism)."""
    def clear(self) -> None: ...
    # 私有: self._lock (threading.Lock) 保护 self._funcs dict
```

不变量：

- INV-R1：注册表是纯内存映射，**不序列化 callable**；落盘与 `list` 只出现名称字符串（S8 / 4.4）。
- INV-R2：运行期 `get` 失败由 executor 捕获降级为 error 运行记录，绝不冒泡到调度主循环（第 8 章）。
- INV-R3：`register/unregister/get/__contains__/names/clear` 全部持锁，允许多线程（工作线程 get + 主线程 register）。

### 4.4 `scheduler/jobs.py`

```python
MISFIRE_POLICIES: frozenset[str] = frozenset({"discard", "coalesce", "fire_once"})
DEFAULT_MISFIRE_POLICY   = "discard"
DEFAULT_MISFIRE_GRACE    = 60.0
DEFAULT_MAX_RETRIES      = 0
DEFAULT_RETRY_BACKOFF    = 1.0
DEFAULT_RETRY_BACKOFF_MAX = 300.0

def to_iso(moment: dt.datetime) -> str:
    """strftime('%Y-%m-%dT%H:%M:%S'); second must be integral (naive)."""
def from_iso(text: str) -> dt.datetime:
    """Parse '%Y-%m-%dT%H:%M:%S' via datetime.fromisoformat
    (accepts a superset, e.g. microseconds, for round-trip safety);
    invalid -> ValueError. (设计假设 DEV-07)"""

class Job:
    def __init__(self, name: str, func: str, trigger: Trigger, *,
                 args: list | None = None,
                 kwargs: dict | None = None,
                 misfire_policy: str = "discard",
                 misfire_grace: float = 60.0,
                 max_retries: int = 0,
                 retry_backoff: float = 1.0,
                 retry_backoff_max: float = 300.0,
                 timeout: float | None = None,
                 enabled: bool = True,
                 next_run_time: dt.datetime | None = None) -> None:
        """Validation (all violations -> ValueError, ASCII message):
        name/func: non-empty ASCII str;
        trigger: Trigger instance;
        args: list (None -> []); kwargs: dict (None -> {});
        misfire_policy in MISFIRE_POLICIES;
        misfire_grace: real number >= 0 (bool excluded);
        max_retries: int >= 0 (bool excluded, float rejected);
        retry_backoff / retry_backoff_max: real number > 0;
        timeout: None or real number > 0;
        enabled: bool;
        next_run_time: None or datetime.
        misfire_grace/retry_backoff/retry_backoff_max/timeout are normalized
        to float at construction (int 30 -> 30.0)."""

    # 公开属性（全部 property；next_run_time 与 enabled 带 setter）
    name: str
    func: str
    trigger: Trigger
    args: list                # 返回内部列表的副本
    kwargs: dict              # 返回内部 dict 的副本
    misfire_policy: str
    misfire_grace: float
    max_retries: int
    retry_backoff: float
    retry_backoff_max: float
    timeout: float | None
    enabled: bool             # setter 校验 bool
    next_run_time: dt.datetime | None   # setter 校验 None/datetime（调度器推进用）

    def to_dict(self) -> dict:
        """Key set exactly per 4.2 (13 keys):
        name, func, trigger, args, kwargs, misfire_policy, misfire_grace,
        max_retries, retry_backoff, retry_backoff_max, timeout, enabled,
        next_run_time (ISO string or None). trigger -> trigger.to_dict().
        不排序（排序由 store.save 统一 sort_keys=True 完成）。"""
    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        """Inverse of to_dict.
        data not dict / missing name|func|trigger / trigger object invalid
        -> ValueError (trigger_from_dict errors propagate).
        args default [], kwargs default {}; next_run_time None or ISO str
        (invalid -> ValueError); unknown extra keys are ignored
        (设计假设：宽容未知键，保证前向兼容；DEV-13)."""
```

不变量：

- INV-J1：`to_dict()` → `from_dict()` round-trip 后全部字段相等（TS-14），含 trigger 原文。
- INV-J2：`to_dict()` 的键集合与 4.2 完全一致（13 键，一个不多一个不少）。
- INV-J3：`func` 在 dict 中永远是字符串（不序列化 callable）。
- INV-J4：`next_run_time` 为 None 的唯一语义是「无未来触发」（cron 永不命中）；interval 的 None 只可能来自显式构造/加载。
- INV-J5：`args`/`kwargs` 仅限 JSON 可序列化值（构造期只校验容器类型；序列化期 `TypeError` 冒泡，CLI 转退出码 1，第 8 章）。

### 4.5 `scheduler/runlog.py`

```python
@dataclass(frozen=True)
class RunRecord:
    job: str
    scheduled_time: dt.datetime
    started_at: dt.datetime
    finished_at: dt.datetime
    duration: float                  # >= 0
    status: str                      # 'success' | 'error' | 'timeout'
    attempts: int                    # >= 1
    error: str | None                # None iff status == 'success'
    def __post_init__(self) -> None:
        """status not in {'success','error','timeout'} -> ValueError;
        attempts < 1 -> ValueError; duration < 0 -> ValueError;
        success with non-None error, or failure with None error -> ValueError."""
    def to_dict(self) -> dict:
        """8 keys in 4.3 order: job, scheduled_time, started_at,
        finished_at, duration, status, attempts, error.
        datetimes -> '%Y-%m-%dT%H:%M:%S'; error None -> null."""

class RunLogger:
    def __init__(self, path: str | None = None) -> None:
        """path None -> memory-only (records still queryable).
        path given -> each append opens the file in 'a' mode, writes one
        line, closes (crash-safe append semantics; file created on demand)."""
    @property
    def records(self) -> list[RunRecord]:
        """Copy of the in-memory record list (test assertions)."""
    def append(self, record: RunRecord) -> None:
        """Thread-safe (internal Lock): append to memory list; if path is
        set, append one JSON line (json.dumps(record.to_dict()) + '\\n',
        UTF-8, ASCII content). OSError propagates to caller."""
    def flush(self) -> None:
        """No-op placeholder (each append is already flushed by close)."""
    # 私有: self._lock (threading.Lock)
```

不变量：

- INV-L1：一次触发（含全部重试尝试）只写一条记录（FR-13.5 / FR-12.4）。
- INV-L2：追加语义——旧行字节不变；文件不存在则创建。
- INV-L3：`duration` 口径 = 该次触发各次尝试的 wall-clock 观测之和（executor 用 `time.perf_counter` 测，见 5.4；虚拟时钟 + 瞬时函数下可为 `0.0`，设计假设 DEV-04）。
- INV-L4：内联模式下 `append` 同步发生在 `tick()` 返回前（TS-16/TS-21 确定性）；池模式下发生在工作线程、`drain()` 返回前完成。

### 4.6 `scheduler/store.py`

```python
STORE_VERSION = 1

class JobStore:
    def __init__(self, path: str) -> None:
        """path: JSON file path (relative to cwd allowed)."""
    @property
    def path(self) -> str: ...
    def exists(self) -> bool: ...
    def load(self) -> list[Job]:
        """Missing file -> [] (empty store).
        Any structural problem -> StoreError (original bytes untouched).
        Returns jobs in file order (caller sorts by name when needed)."""
    def save(self, jobs: Iterable[Job]) -> None:
        """Atomic write: same-dir temp '<path>.tmp' + os.replace.
        No .tmp residue on success or failure (A6).
        OSError / serialization failure -> StoreError."""
```

持久化细节（JSON 结构、原子写步骤、加载校验清单）见第 7 章。

### 4.7 `scheduler/executor.py`

```python
@dataclass(frozen=True)
class SingleRunResult:
    """One physical attempt (internal; public for tests)."""
    status: str            # 'success' | 'error' | 'timeout'
    error: str | None
    started_at: dt.datetime
    finished_at: dt.datetime
    duration: float        # perf_counter observation of this attempt

class JobExecutor:
    def __init__(self, registry: FunctionRegistry, clock: Clock, *,
                 max_workers: int | None = 4,
                 sleep: Callable[[float], None] | None = None) -> None:
        """max_workers None -> INLINE mode (synchronous execution on the
        calling thread; timeout is ignored, see R6).
        max_workers <= 0 (and not None) -> ValueError.
        sleep defaults to time.sleep (retry backoff; injectable)."""
    def submit_trigger(self, job: Job, scheduled_time: dt.datetime,
                       on_record: Callable[[RunRecord], None]) -> "Future | None":
        """Run one full trigger for `job` (all retries + backoff) and call
        on_record(record) exactly once when finished.
        INLINE: executes synchronously, returns None.
        POOL: submits to the pool (fire-and-forget), returns the Future.
        After close() -> RuntimeError."""
    def drain(self, timeout: float | None = None) -> None:
        """Wait for all pending trigger futures (no-op in INLINE mode).
        Never re-raises future exceptions (they are collected in async_errors)."""
    def pending(self) -> int:
        """Number of unfinished pool futures (0 in INLINE mode)."""
    @property
    def async_errors(self) -> list[BaseException]:
        """Exceptions escaped from worker tasks (e.g. on_record OSError)."""
    def close(self, wait: bool = True) -> None:
        """Shutdown the pool (cancel_futures=True). wait=False returns
        immediately; running tasks continue (R1 known limitation)."""
    # 私有辅助:
    #   _run_trigger_task(job, scheduled_time, on_record) -> None
    #       完整触发：重试循环 + 退避 + 记录构造 + on_record 调用；
    #       自身异常兜底捕获后存入 async_errors（绝不死工作线程）。
    #   _run_once(job) -> SingleRunResult
    #       单次物理执行：解析函数（FunctionNotFoundError -> error 结果）、
    #       内联直调或 pool.submit + future.result(timeout)。
    #   _pending: set[Future]，_pending_lock: threading.Lock
```

不变量：

- INV-E1：总尝试次数 = `max_retries + 1`；`attempts` ∈ `[1, max_retries+1]`，成功即停。
- INV-E2：第 attempt 次失败后、第 attempt+1 次前 sleep `min(retry_backoff * 2**(attempt-1), retry_backoff_max)`（单次封顶，非累计；S7）。
- INV-E3：超时是一次尝试（status `timeout`，error 文本含 `"timeout"`），可触发重试（S6）。
- INV-E4：内联模式 timeout 不生效（FR-11.5）；超时测试必须用真实池（R6）。
- INV-E5：INLINE 模式下 `submit_trigger` 返回时记录已写入（确定性，A3/A5 的前提）。
- INV-E6：池模式 fire-and-forget——`submit_trigger` 不等待；`drain` 才等待。

### 4.8 `scheduler/scheduler.py`

```python
class CronScheduler:
    def __init__(self, *,
                 store: JobStore | None = None,
                 registry: FunctionRegistry | None = None,
                 executor: JobExecutor | None = None,
                 run_logger: RunLogger | None = None,
                 clock: Clock | None = None,
                 max_workers: int | None = 4,
                 sleep: Callable[[float], None] | None = None) -> None:
        """Dependency injection with sane defaults:
        store None -> in-memory (save() is a no-op);
        registry None -> new empty FunctionRegistry;
        executor None -> new JobExecutor(registry, clock, max_workers=...,
                                         sleep=...)  (max_workers None -> INLINE);
        run_logger None -> new memory-only RunLogger;
        clock None -> RealClock();
        sleep None -> time.sleep (used by run()'s tick loop only)."""

    # --- 任务生命周期（全部 save 由 auto_save 控制；持 _state_lock 改状态）---
    def add_job(self, job: Job) -> None:
        """Duplicate name -> JobAlreadyExistsError.
        If job.next_run_time is None: compute via
        trigger.get_next_fire_time(clock.now()); NoMatchError -> None
        (cron never-match => silent no-future, FR-03.3).
        Explicit next_run_time is preserved as-is (restore path, S4)."""
    def remove_job(self, name: str) -> None:
        """Unknown name -> JobNotFoundError."""
    def pause_job(self, name: str) -> None:
        """enabled=False. Unknown name -> JobNotFoundError.
        Does NOT touch next_run_time (S11)."""
    def resume_job(self, name: str) -> None:
        """enabled=True. Unknown name -> JobNotFoundError.
        Does NOT recompute next_run_time; backlog handled by misfire
        policy at the next tick (S11 / FR-08.5)."""
    def trigger_now(self, name: str) -> None:
        """Execute once immediately (works while paused, S12).
        Does NOT modify next_run_time. Record's scheduled_time = now.
        Unknown name -> JobNotFoundError. Submitted via executor
        (INLINE: completes before return)."""
    # --- 主循环 ---
    def tick(self) -> None:
        """One scheduling pass; see 5.6 for the exact order.
        Saves at end of tick iff any next_run_time advanced (FR-07.6)."""
    def run(self, duration: float | None = None,
            tick_interval: float = 1.0) -> None:
        """while not stopped: tick(); then if deadline reached break;
        else sleep(tick_interval). deadline = clock.now()+duration when
        duration is not None (clock-based, DEV-03). KeyboardInterrupt
        propagates out of run() (CLI maps it to exit 0)."""
    def stop(self) -> None: ...
    def close(self, wait: bool = True) -> None:
        """stop() + executor.close(wait)."""
    # --- 查询 ---
    def get_job(self, name: str) -> Job:
        """Unknown name -> JobNotFoundError."""
    def job_names(self) -> list[str]:
        """Sorted job names."""
    @property
    def jobs(self) -> dict[str, Job]:
        """Snapshot copy {name: Job} (lock-protected)."""
    def save(self) -> None:
        """Persist all jobs via store; no-op when store is None.
        StoreError propagates to caller."""
    # 私有辅助:
    #   _state_lock: threading.RLock  保护 self._jobs 与 job.next_run_time
    #   _due_jobs(now) -> list[Job]   快照 + name 排序
    #   _advance_one_period(job, scheduled) -> datetime|None
    #       cron: trigger.get_next_fire_time(scheduled) (NoMatchError -> None)
    #       interval: scheduled + timedelta(seconds=trigger.seconds)
    #   _next_after_now(job, now) -> datetime|None
    #       trigger.get_next_fire_time(now) (NoMatchError -> None)
    #   _on_record(record) -> None    run_logger.append
```

不变量：

- INV-S1：tick 内每个 Job 最多触发一次（FR-08.3）；推进后仍 ≤ now 留待后续 tick（coalesce 排水，C2）。
- INV-S2：tick 内多 Job 按 `name` 字典序处理（N3 确定性）。
- INV-S3：正常推进锚定 `scheduled_time`（cron `next_after(scheduled)` / interval `scheduled+interval`）；misfire 跳转分支用 `get_next_fire_time(now)`（FR-04.4 / FR-09.5）。
- INV-S4：`enabled=False` 的 Job 在 tick 中零运行、零推进（S11）。
- INV-S5：`trigger_now` 不改 `next_run_time`、不禁用也拦（S12）。
- INV-S6：tick 末（发生过推进时）`save()`（FR-07.6）；add/remove/pause/resume 后立即 save。
- INV-S7：加载路径（CLI run / 测试恢复）一律保留落盘 `next_run_time`，不重算（FR-08.8 / S4）。
- INV-S8：`next_run_time` 要么为 None，要么是秒=0 的 naive datetime；推进函数永不回退（单调不减，除 misfire 跳转可能跳到更远的未来）。

### 4.9 `scheduler/builtins.py`

```python
def noop(*args: object, **kwargs: object) -> None:
    """Do nothing, succeed."""
def fail(*args: object, **kwargs: object) -> None:
    """Always raise RuntimeError('fail')."""
def register_builtins(registry: FunctionRegistry) -> None:
    """Register 'noop' and 'fail' (overwrite allowed)."""
```

用途：CLI `run` 与 demo 的内置函数来源（FR-06.5 / R10）。

### 4.10 `scheduler/cli.py`

```python
def build_parser() -> argparse.ArgumentParser:
    """Constructs the CLI per 4.1. Sub-commands: add/list/remove/pause/
    resume/run. Global (before subcommand): --store PATH (default
    'cron_jobs.json'), --log PATH (default 'cron_runs.jsonl').
    Parser subclass overrides error() to print a single ASCII line
    'error: <message>' to stderr and exit 2."""

def main(argv: list[str] | None = None) -> int:
    """Returns the process exit code (0/1/2 per 4.1 table).
    Never prints tracebacks. --help exits 0 via argparse."""
```

子命令参数（与 4.1 逐字一致）：

| 子命令 | 参数 | 类型/默认 |
|---|---|---|
| add | `--name`（必填）、`--func`（必填）、`--cron` / `--interval`（互斥组，必填二选一） | str / str / str / float |
| add | `--misfire`（默认 discard）、`--grace`（60）、`--max-retries`（0）、`--backoff`（1）、`--backoff-max`（300）、`--timeout`（None）、`--disabled`（flag）、`--args`（JSON 数组）、`--kwargs`（JSON 对象） | 见 4.1.1 |
| list | 无 | — |
| remove/pause/resume | `--name`（必填） | str |
| run | `--duration`（None）、`--tick`（1.0） | float |

数值 type 校验函数（私有）：`_nonneg_float`（grace）、`_pos_float`（backoff/backoff-max/timeout/duration/tick）、`_nonneg_int`（max-retries）；非法文本或越界 → `argparse.ArgumentTypeError` → 统一 exit 2。

各子命令行为（私有 `_cmd_add(args) -> int` 等）：

- `_cmd_add`：`JobStore(store).load()`（StoreError → 1）→ 构造 trigger（CronParseError/ValueError → 2）→ 构造 Job（ValueError → 2）→ 重名检查（JobAlreadyExistsError → 2）→ `next = trigger.get_next_fire_time(datetime.now())`（NoMatchError → None）→ append + save → stdout `added <name> next_run=<ISO|null>`。
- `_cmd_list`：load → 按 name 排序 → 每行 `json.dumps(row)`，row 键序固定为 `name, func, trigger, next_run_time, enabled, misfire_policy, misfire_grace, max_retries, timeout`（4.1.2 子集，**不是** to_dict 全集；`trigger` 内 `type` 在前）；空库无输出。
- `_cmd_remove/_cmd_pause/_cmd_resume`：load → 按名查找（JobNotFoundError → 2）→ 改 → save → stdout `removed|paused|resumed <name>`。
- `_cmd_run`：load（StoreError → 1）→ 日志可写预探（`open(log,'a')`，OSError → StoreError → 1）→ 注册表 + builtins → `RunLogger(log)` → `RealClock` → `JobExecutor(max_workers=4)` → `CronScheduler(...)` → 逐 job `add_job`（保留落盘 next_run_time）→ `sched.run(duration, tick_interval)`（KeyboardInterrupt → 0）→ `finally: sched.close()` → 0。

退出码映射见第 8 章总表（与 4.1 / 4.5 一致）。

---

## 5. 关键算法设计（伪代码级）

### 5.1 `next_after`：字段递进跳跃算法

目标：与逐分钟蛮扫可观测行为完全一致；4 年上界有界终止；单次调用远快于蛮扫（N5：典型 < 10ms；`0 0 30 2 *` 类 < 5s）。

```
function next_after(expr, dt):
    # 起算点：忽略秒/微秒，取下一分钟（严格大于 dt）
    t     <- truncate_to_minute(dt) + 1 minute        # second = 0
    limit <- datetime(t.year + 4, t.month, t.day, t.hour, t.minute)

    loop:                                             # 每次迭代至少跳一个字段，单调严格递增
        # L1 月：不在匹配月集合 -> 跳到下月 1 日 00:00
        if t.month not in expr.months:
            t <- first_day_of_next_month(t)            # 12月 -> 次年1月
            if t > limit: raise NoMatchError
            continue

        # L2 日：DOM/DOW 或与语义（见 _day_matches）
        if not _day_matches(expr, t.date()):
            t <- midnight_of_next_day(t)               # 次日 00:00（低字段清零）
            if t > limit: raise NoMatchError
            continue

        # L3 时：不在匹配时集合 -> 跳到本小时内下一个匹配时 :00；
        #        今天已无匹配时 -> 次日 00:00（回到 L2）
        if t.hour not in expr.hours:
            t <- next_matching_hour_in_day(t, expr.hours)  # 无 -> 次日 00:00
            if t > limit: raise NoMatchError
            continue

        # L4 分：不在匹配分集合 -> 跳到本小时内下一个匹配分；
        #        本小时已无 -> 下一小时 :00（回到 L3）
        if t.minute not in expr.minutes:
            t <- next_matching_minute_in_hour(t, expr.minutes)  # 无 -> 下一小时 :00
            if t > limit: raise NoMatchError
            continue

        return t                                       # 四级全中，second=0

function _day_matches(expr, d):
    dom_ok <- d.day in expr.days_of_month
    dow_ok <- cron_dow(d) in expr.days_of_week         # cron_dow: 0=Sun..6=Sat
    if expr.dom_restricted and expr.dow_restricted:    # 均非字面 "*"（S2）
        return dom_ok or dow_ok                        # 「或」：先到者准
    return dom_ok and dow_ok                           # 「与」

function cron_dow(d):  return (d.weekday() + 1) % 7    # python Mon=0 -> cron Sun=0
```

设计要点：

1. **跳跃层级**：月跳（≤ 12 次/年）→ 日跳（≤ 31 次/月）→ 时跳（≤ 24 次/日）→ 分跳（≤ 60 次/小时）。每次 `continue` 把低位字段清零，保证单调严格递增，循环不可能打转。
2. **4 年上界落地**：`limit` = 起算点同月/日/时/分后推 4 个日历年（2/29 起算点时 `+4` 年必为闰年，`datetime(...)` 构造不会失败；仍以 try/except 兜底转 `NoMatchError`）。每次跳跃后比对，`t > limit` 即抛。`0 0 30 2 *` 最坏路径 ≈ 4 年 × (30 次日跳 + 11 次月跳) ≈ 164 次迭代，墙钟 << 5s（TS-11）。
3. **DOM/DOW「或」语义**：仅在 `_day_matches` 一处实现，`restricted` 标志来自段原文（`*/2` 视为受限，DEV-09）。`0 0 13 * 5` @ 2026-10-14（周三）：10-16（周五）先于 11-13，命中 10-16（TS-07/TS-08）。
4. **与蛮扫一致性论证**：蛮扫从同一起算点逐分钟检查同一 `_day_matches` 谓词；跳跃算法的每个 `continue` 只会跳过必然不匹配的区域（该级字段不匹配 ⇒ 整分钟不匹配），返回的第一个 t 即最早匹配点。两侧谓词相同（tests/support.py 的参考实现用同一文档化规则独立编写，DEV 级对拍见 9.2）。
5. 抽样验证：`*/15 * * * *` @ 10:07 → 10:15；`0 0 1 * *` @ 2026-10-14T10:00 → 2026-11-01T00:00；`0 0 29 2 *` @ 2027-03-01 → 2028-02-29T00:00；`59 23 31 12 *` @ 2026-12-31T23:59 → 2027-12-31T23:59。

### 5.2 misfire 三策略状态机

输入：`now`、`scheduled_time`（触发前 `next_run_time`）、`grace`（`misfire_grace`）、`policy`。
输出：`run`（是否执行一次）、`new_next`（推进后的 `next_run_time`，可为 None）。

```
lateness <- (now - scheduled_time).total_seconds()

if lateness <= grace:                       # 正常迟到分支（含 lateness == grace，TS-25）
    state <- NORMAL
else:
    state <- MISFIRE

NORMAL:      run = True ; new_next = advance(scheduled)          # 锚定推进一个周期
MISFIRE + discard:    run = False; new_next = jump(now)          # 直接跳到未来，无记录
MISFIRE + fire_once:  run = True ; new_next = jump(now)          # 补最早一次，然后跳到未来
MISFIRE + coalesce:   run = True ; new_next = advance(scheduled) # 排水一次，追平留待后续 tick

advance(s):  cron: next_after(s)（NoMatchError -> None） ; interval: s + interval
jump(n):     trigger.get_next_fire_time(n)（NoMatchError -> None）
             # cron: next_after(n)；interval: n + interval（设计假设 DEV-02）
```

状态机转移图：

```
                    lateness <= grace
        (due) ─────────────────────────────► NORMAL ──► run=1, advance(scheduled)
          │                                        ▲
          │ lateness > grace                       │
          ▼                                        │
     ┌─────────┐   discard    │ run=0, jump(now)   │
     │ MISFIRE ├──────────────┼────────────────────┘
     │         │   fire_once  │ run=1, jump(now)
     │         ├──────────────┼────────────────────► (结束，无记录或 1 条记录)
     │         │   coalesce   │ run=1, advance(scheduled)
     └─────────┘──────────────┴────────────────────► (结束；若 new_next <= now，
                                                      下一 tick 再次进入 MISFIRE)
```

A4 口径落地（连续错过 10 个周期、恢复时刻 R、grace=0）：

| 策略 | 运行次数（只计 scheduled_time < R 的执行，FR-09.6） | 记录中 scheduled_time | 结束后 next_run_time |
|---|---|---|---|
| discard | 0 | 无记录 | `jump(R)` > R |
| coalesce | 10（每 tick 1 次，需 ≥10 次 tick） | 10:06 → 10:15 递增 | 追平后 > R |
| fire_once | 1 | 最早错过周期 | `jump(R)` > R |

`jump(R)` 对 cron `* * * * *` = `next_after(R)`：R=10:16:00 → 10:17:00（10:16 周期在跳过之列——附 B G1 修正口径，三场景统一）。

### 5.3 重试循环与指数退避

```
function _run_trigger_task(job, scheduled_time, on_record):
    total   <- job.max_retries + 1
    first_started <- None ; last_finished <- None
    observed <- 0.0 ; status <- None ; error <- None ; attempts <- 0
    for attempt in 1 .. total:
        attempts <- attempt
        single <- _run_once(job)                  # 见 5.4（含超时/解析失败降级）
        if first_started is None: first_started <- single.started_at
        last_finished <- single.finished_at
        observed <- observed + single.duration    # perf_counter 观测
        status, error <- single.status, single.error
        if status == "success": break
        if attempt < total:                       # 失败且还有尝试机会
            backoff <- min(job.retry_backoff * 2 ** (attempt - 1),
                           job.retry_backoff_max)  # 单次封顶（S7 / FR-12.2）
            sleep(backoff)                         # 注入的 sleep（虚拟时钟测试用假 sleep）
    record <- RunRecord(job=job.name, scheduled_time=scheduled_time,
                        started_at=first_started, finished_at=last_finished,
                        duration=observed, status=status, attempts=attempts,
                        error=error)
    on_record(record)                              # 一次触发一条记录（INV-L1）
```

- 退避序列：第 attempt 次失败后 sleep `min(b·2^(attempt-1), bmax)`；TS-26(b) `b=100, bmax=300, retries=5` 全失败 → `[100,200,300,300,300]`、attempts=6。
- 注入 sleep：`JobExecutor.__init__(sleep=...)`；测试注入 `RecordingSleep`（只记录不推进时钟，TS-26）；真实模式 `time.sleep`。
- 上限截断：`min(...)` 作用于**单次**退避时长（FR-12.2 假设），非累计。
- 函数解析失败（`FunctionNotFoundError`）在 `_run_once` 内转为 `status="error"` 结果（error 文本含函数名），与其他失败同口径计入 attempts、可重试（FR-06.4）。

### 5.4 单次执行与超时

```
function _run_once(job):
    started <- clock.now() ; t0 <- perf_counter()
    try:
        func <- registry.get(job.func)            # 持注册表锁
    except FunctionNotFoundError:
        return SingleRunResult("error", "function not found: " + job.func,
                               started, clock.now(), perf_counter() - t0)

    if executor is INLINE:                        # 池 = None：timeout 不生效（R6）
        try:
            func(*job.args, **job.kwargs)
            return SingleRunResult("success", None, started, clock.now(),
                                   perf_counter() - t0)
        except Exception as e:                    # 不捕 BaseException
            return SingleRunResult("error", type(e).__name__ + ": " + str(e),
                                   started, clock.now(), perf_counter() - t0)
    else:                                         # 真实池
        future <- pool.submit(func, *job.args, **job.kwargs)
        try:
            future.result(timeout = job.timeout)  # None -> 无限等待
            return SingleRunResult("success", None, started, clock.now(),
                                   perf_counter() - t0)
        except FuturesTimeoutError:               # concurrent.futures.TimeoutError
            # 放弃等待口径：不 cancel 底层任务（对已开始任务无效，语义等价放弃）；
            # 不 join、不中断；底层线程继续执行（S6 已知限制 R1）
            return SingleRunResult("timeout",
                                   "timeout after %ss" % job.timeout,
                                   started, clock.now(), perf_counter() - t0)
        except Exception as e:
            return SingleRunResult("error", type(e).__name__ + ": " + str(e),
                                   started, clock.now(), perf_counter() - t0)
```

超时与 daemon 的口径（必须写入 README 与设计文档，S6 要求）：

- 实现采用标准库 `ThreadPoolExecutor`（S9）。`future.result(timeout)` 到点抛 `TimeoutError`，立即放弃等待并记录 `status="timeout"`；**不** `cancel`、**不**中断底层线程——该线程继续执行直到函数自行结束。
- 已知限制（设计假设 DEV-01）：标准库池的工作线程**无法**设为 daemon，且解释器退出时 `concurrent.futures` 的 atexit 会 join 在途任务；因此挂起函数（如 `sleep(10)`）会使进程退出最多延迟约该函数时长。验收与测试**不得**断言「进程可立即退出」（R1）；`close(wait=False, cancel_futures=True)` 可取消尚未开始的挂起任务。
- 超时计入一次尝试（attempts 含它），可触发重试（TS-27：timeout=0.2、max_retries=1 → attempts=2、调用方 ≤1s 返回、重试继续）。
- error 文本格式：异常 `"<ExceptionName>: <message>"`（示例 `RuntimeError: boom`）；超时 `"timeout after <N>s"`（含 `"timeout"`，TS-27）。

### 5.5 `tick` 主处理流程（完整顺序）

```
function tick():
    now     <- clock.now()
    changed <- False

    # S1 快照：持锁取到期列表并按 name 字典序排序（INV-S2）
    with _state_lock:
        due <- [j for j in _jobs.values()
                if j.enabled and j.next_run_time is not None
                   and j.next_run_time <= now]
        due.sort(by name)

    for job in due:                                # 每 job 本 tick 最多一次（INV-S1）
        scheduled <- job.next_run_time             # 读取（锁外读，值在快照时已固定）
        lateness  <- (now - scheduled).total_seconds()

        # S2 策略分支（5.2 状态机）
        if lateness <= job.misfire_grace:
            run <- True ;  new_next <- _advance_one_period(job, scheduled)
        else:
            case job.misfire_policy of
              "discard":   run <- False; new_next <- _next_after_now(job, now)
              "fire_once": run <- True ; new_next <- _next_after_now(job, now)
              "coalesce":  run <- True ; new_next <- _advance_one_period(job, scheduled)

        # S3 触发（INLINE 同步执行完并已写记录；POOL fire-and-forget）
        if run:
            executor.submit_trigger(job, scheduled, on_record = _on_record)

        # S4 推进（持锁写；None = 无未来触发；new_next 可能仍 <= now，留待后续 tick）
        with _state_lock:
            job.next_run_time <- new_next
        changed <- True

    # S5 落库：本 tick 推进过才保存（FR-07.6）；StoreError 冒泡给调用方（第 8 章）
    if changed and auto_save:
        save()
```

顺序要点（测试断言依赖此顺序）：

1. 排序在触发之前（tick 内 Job 按 name 处理）。
2. 触发在推进之前（`scheduled_time` 取推进前的值；INLINE 模式下记录先于 store 保存写入）。
3. 推进逐 job 立即生效（不等执行完成——池模式 fire-and-forget）。
4. 保存一次（tick 末），非每 job 一次。
5. `changed` 仅由「推进」置位；discard 也是推进（next 跳到未来）→ 也保存。

### 5.6 `run` 主循环

```
function run(duration, tick_interval):
    stopped <- False
    deadline <- None if duration is None else clock.now() + duration 秒
    while not stopped:
        tick()
        if deadline is not None and clock.now() >= deadline: break
        sleep(tick_interval)                       # 注入；KeyboardInterrupt 在此抛出
```

- duration 判定用**注入时钟**（DEV-03）：虚拟时钟 + 假 sleep 也可驱动 `run`（测试便利）；CLI 真实模式天然正确。
- `--duration 2` + interval=1s 任务：tick 在约 1s、2s 时到期 → ≥1 条记录（TS-29(h)）。

---

## 6. 线程与并发模型

### 6.1 跨线程共享对象与锁

| 对象 | 访问线程 | 保护 | 说明 |
|---|---|---|---|
| `CronScheduler._jobs`（dict） | 主循环/测试线程；外部 API 线程（trigger_now） | `_state_lock`（RLock） | 增删改与快照均持锁；tick 处理中不持锁（避免长时持锁阻塞 API） |
| `Job.next_run_time` / `Job.enabled` | 主循环写；`jobs` 快照读 | 同 `_state_lock` | setter 只做类型校验，不加锁（锁由调用方持有） |
| `FunctionRegistry._funcs` | 工作线程 `get`；主线程 `register/unregister` | `registry._lock`（Lock） | 读多写少，简单互斥 |
| `RunLogger`（内存 list + 文件） | 多线程 `append` | `logger._lock`（Lock） | 追加持锁；文件以 `"a"` 模式逐次开闭（崩溃安全） |
| `JobStore` | 仅主循环/CLI 线程 save | 无锁 | 单写者假设（R8，FR-07.7） |
| `ThreadPoolExecutor` | 多线程 submit / future | 池内部线程安全 | 标准库保证 |
| `executor._pending`（set） | 主线程 submit；工作线程 done_callback | `_pending_lock` | `drain` 取快照后 `futures.wait` |
| `VirtualClock._now` | 工作线程只读；主线程 advance/set | 无锁（GIL 原子赋值） | 禁止双写；设计假设 DEV-14 |
| `executor._async_errors` | 工作线程追加；任意线程读 | `_pending_lock` | 有界关注：仅诊断用途 |

### 6.2 提交与等待策略

- **tick 不阻塞**：池模式下 `submit_trigger` 即 fire-and-forget（FR-10.3）；INLINE 模式下同步执行完毕（确定性测试模式，S9）。
- **drain**：测试/关停前 `executor.drain(timeout)` 等待全部挂起 future；记录写入发生在 future 完成前（`_run_trigger_task` 末尾调 `on_record`），故 drain 返回 ⇒ 记录已落。
- **A7 并发口径**：20 job 同时到期、池 4、单任务 50ms → tick 一次性提交 20 个 future，池内 5 批并行 ≈ 250–300ms < 2s（TS-28）。

### 6.3 shutdown 语义

```
close(wait=True):
    stop()                                    # run 循环退出
    executor.shutdown(wait=wait, cancel_futures=True)
```

- `wait=True`：等待在途触发完成（含其退避 sleep；虚拟时钟+假 sleep 下立即完成）。
- `wait=False`：立即返回；运行中的任务继续（R1：挂起线程不被打断），未开始的被取消。
- `close()` 之后 `submit_trigger` 抛 `RuntimeError`（池已关）——文档明示，调用方负责关闭顺序。
- CLI `run` 收到 KeyboardInterrupt：`finally` 中 `close(wait=False)`（不挂住退出），退出码 0（S10）。

### 6.4 虚拟时钟下的确定性保证

确定性四要素（N3，缺一不可）：

1. **时钟注入**：核心只认 `clock.now()`；虚拟时钟由测试完全控制（`advance`）。
2. **内联执行**：`max_workers=None` ⇒ 无工作线程，tick 内同步执行+写记录，无竞态。
3. **假 sleep**：退避 sleep 注入 `RecordingSleep`（不推进时钟、不耗时）；`run` 循环 sleep 同样可注入。
4. **无随机源 + 固定序**：tick 内按 name 排序；落盘 `sort_keys=True`；JSONL 固定键序。

结论：同一场景重复执行 ⇒ 运行记录逐条一致（A3/A6 恢复一致性的前提）。池模式仅用于 A5/A7/TS-27/TS-28 等真实并发场景，其断言只涉及计数、状态与墙钟上界，不涉及记录顺序细节（记录顺序由完成顺序决定，不断言）。

---

## 7. 持久化设计

### 7.1 JSON 结构（与需求 4.2 逐字一致）

```json
{
  "version": 1,
  "jobs": [
    {
      "name": "sync",
      "func": "noop",
      "trigger": {"type": "cron", "expression": "0 3 * * *"},
      "args": [],
      "kwargs": {},
      "misfire_policy": "discard",
      "misfire_grace": 60.0,
      "max_retries": 0,
      "retry_backoff": 1.0,
      "retry_backoff_max": 300.0,
      "timeout": null,
      "enabled": true,
      "next_run_time": "2026-10-15T03:00:00"
    }
  ]
}
```

- 顶层：`version`（必须为 int 1）、`jobs`（array）。
- Job 对象：13 键，与 `Job.to_dict()` 完全一致（4.2 表；可选键缺省时 `from_dict` 补默认值，但 `to_dict` 总是写全 13 键）。
- `trigger`：`{"type":"cron","expression":"<原文，宏保留>"}` 或 `{"type":"interval","seconds":N}`。
- `next_run_time`：ISO `YYYY-MM-DDTHH:MM:SS` 或 null；缺失按 null（4.2）。
- 序列化：`json.dumps(data, sort_keys=True, indent=2)` + 末尾 `"\n"`（设计假设 DEV-06：4.2 未定缩进；sort_keys 保证字节确定，N3）。

### 7.2 原子写步骤（`JobStore.save`）

```
1. data  <- {"version": 1, "jobs": [job.to_dict() for job in jobs]}
2. text  <- json.dumps(data, sort_keys=True, indent=2) + "\n"
           （序列化异常 TypeError/ValueError -> StoreError）
3. tmp   <- path + ".tmp"（同目录，保证 os.replace 原子性）
4. 以 "w"、encoding="utf-8"、newline="\n" 打开 tmp：
   write(text) -> flush() -> os.fsync(fileno())    # 落盘耐久性
5. os.replace(tmp, path)                            # 原子替换
6. finally: 若 tmp 仍存在（replace 失败路径）-> os.remove(tmp) 清理
   （OSError -> StoreError；成功后 tmp 不存在，A6 无残留）
```

### 7.3 加载校验清单（`JobStore.load`，任一命中 → `StoreError`，原文件字节不动）

| # | 检查项 | 失败情形 |
|---|---|---|
| L1 | 文件不存在 | → 返回 `[]`（空库，不算错误） |
| L2 | 读取失败（权限/IO） | `OSError` → StoreError |
| L3 | JSON 语法错误 | `JSONDecodeError` → StoreError |
| L4 | 顶层非 object | → StoreError |
| L5 | `version` 缺失或 `!= 1`（含 `"2"`、字符串 `"1"`） | → StoreError |
| L6 | `jobs` 缺失或非 list | → StoreError |
| L7 | job 项非 object | → StoreError |
| L8 | 缺 `name` / `func` / `trigger` | `ValueError` → StoreError |
| L9 | `name`/`func` 非非空 ASCII 字符串 | → StoreError |
| L10 | `trigger` 非 object / `type` 未知 | `ValueError` → StoreError |
| L11 | cron `expression` 无法重新解析（含宏原文） | `CronParseError` → StoreError（FR-07.4 假设口径） |
| L12 | interval `seconds` 非正数 | `ValueError` → StoreError |
| L13 | 值域非法（grace<0、max_retries<0、backoff≤0、timeout≤0、policy 非法） | `ValueError` → StoreError |
| L14 | `next_run_time` 非 null 且非合法 ISO 字符串 | `ValueError` → StoreError |
| L15 | `name` 在文件内重复 | → StoreError（库内唯一约束，设计假设 DEV-15） |
| L16 | `args` 非 list / `kwargs` 非 dict | `ValueError` → StoreError |

加载成功后：`next_run_time` 一律取落盘值，**不重算**（INV-S7 / S4）。

### 7.4 保存时机

| 时机 | 触发点 | 说明 |
|---|---|---|
| 变更即存 | `add_job` / `remove_job` / `pause_job` / `resume_job` 成功返回前 | CLI 子命令退出前完成保存（FR-07.6 假设） |
| tick 末 | `tick()` 内发生过 next_run_time 推进时 | 一次 tick 最多一次 save |
| 手动 | `CronScheduler.save()` | store 为 None 时 no-op |

---

## 8. 错误分类与冒泡策略

### 8.1 异常清单与映射总表

| 异常 | 抛出层 | CLI 退出码 | 允许冒泡到调度主循环？ | 降级方式 |
|---|---|---|---|---|
| `CronParseError(SchedulerError, ValueError)` | cron_parser / triggers / cli | 2 | 否（构造期 fail-fast，运行期不会产生） | CLI 捕 `ValueError` 系 → stderr `error: <msg>` |
| `NoMatchError(SchedulerError)` | cron_parser.next_after | 不冒泡到 CLI | **否** | `_advance_one_period`/`_next_after_now` 捕获 → `next_run_time=None`；`add_job` 捕获 → None；CLI add 捕获 → stdout `next_run=null`、退出码 0 |
| `StoreError(SchedulerError)` | store.load / store.save / CLI 日志预探 | 1 | **否**（tick 末 save 失败冒泡出 `tick()`→`run()`，CLI 捕 → 退出码 1 并停止循环） | 加载失败：命令直接失败；保存失败：停止主循环，退出码 1 |
| `FunctionNotFoundError(SchedulerError, LookupError)` | registry.get | 不冒泡 | **否** | executor `_run_once` 捕获 → `status="error"`、error 含函数名；计入 attempts、可重试（FR-06.3/4） |
| `JobNotFoundError(SchedulerError, LookupError)` | scheduler.remove/pause/resume/trigger_now | 2 | 否 | CLI 直接转退出码 2 |
| `JobAlreadyExistsError(SchedulerError, ValueError)` | scheduler.add_job / CLI add | 2 | 否 | 同上（不静默覆盖，C6） |
| `ValueError`（内置，含上述两个子类） | jobs/triggers/registry/cli 参数校验 | 2 | 否 | CLI 统一捕 |
| `concurrent.futures.TimeoutError` | executor `_run_once` | 不冒泡 | **否** | 转 `status="timeout"` 记录，计入尝试 |
| func 业务异常（任意 `Exception`） | 用户函数体内 | 不冒泡 | **否** | executor 捕获 → error 记录 → 重试 → 最终记录 |
| `OSError`（RunLogger 同步路径） | runlog.append | 1（INLINE 模式经 tick→run 冒泡到 CLI） | 否 | 池模式：收集进 `async_errors` 并丢弃该次记录（已知限制，DEV-16）；CLI run 启动时预探日志可写，不可写 → 退出码 1 |
| `KeyboardInterrupt` | `run()` 的 sleep / 任意处 | 0 | — | CLI run 捕获 → `close(wait=False)` → 退出码 0（S10） |
| 未分类异常（`Exception` 兜底） | 任意 | 1 | 否 | CLI 顶层兜底 → `error: <msg>`，不打印 traceback |

### 8.2 冒泡原则

1. **调度主循环永不被单任务故障拖死**：一切「任务级」异常（函数异常、未注册、超时）在执行器内转记录；一切「配置级」异常（解析、校验）在构造期 fail-fast；只有「存储级」异常（StoreError）允许穿透 tick→run，且 CLI 将其转为退出码 1 并停循环。
2. **错误信息单行 ASCII**：所有冒泡到 CLI 的异常消息经 `print("error: %s" % msg, file=sys.stderr)` 输出，不带 traceback（4.1 假设）。
3. **`NoMatchError` 是控制流不是故障**：永不命中表达式 ⇒ Job 静默停摆（`next_run_time=null`），不产生运行记录、不崩溃（FR-03.3 假设）。

---

## 9. 测试策略（测试 agent 施工图）

### 9.1 TS-01 ~ TS-30 → 测试文件/类映射表

| TS | 测试文件 | 测试类 | 预计用例数 | 关键断言 |
|---|---|---|---|---|
| TS-01 | test_cron_parser.py | TestBasicSyntax | 4 | 各字段展开集合 |
| TS-02 | test_cron_parser.py | TestRangesAndSteps | 4 | 时/分集合 |
| TS-03 | test_cron_parser.py | TestNamesAndMacros | 8 | 命名月/周、5 宏、`@DAILY` |
| TS-04 | test_cron_parser.py | TestDowNormalization | 3 | 0/7 等价、`5-7`→{5,6,0} |
| TS-05 | test_cron_parser.py | TestInvalidRejected | 12 | 12 个非法输入逐一抛 `CronParseError` |
| TS-06 | test_cron_parser.py | TestBoundaryValues | 4 | 边界合法值 |
| 附B | test_cron_parser.py | TestNamedRanges | 3 | `MON-FRI`/`JAN-MAR`/逆序非法（编排者附加要求） |
| TS-07 | test_next_after.py | TestDomDowOr | 1 | 5 次 next_after 序列 |
| TS-08 | test_next_after.py | TestDomDowAnd | 3 | 与语义 2 组 + 或语义 1 组 |
| TS-09 | test_next_after.py | TestNextAfterSemantics | 5 | 秒截断/严格大于/跨周期/闰年 |
| TS-10 | test_next_after.py | TestBruteForceCrossCheck | 3（每方法 ≥20 组随机，固定 seed） | 与蛮扫逐组一致 |
| TS-11 | test_next_after.py | TestNeverMatch | 2 | `NoMatchError` 且 < 5s |
| TS-12 | test_triggers.py | TestCronTrigger | 3 | 接口/构造即抛/NoMatch |
| TS-13 | test_triggers.py | TestIntervalTrigger | 3 | 首次=加入+interval、锚定推进、恢复不重算 |
| TS-14 | test_jobs.py | TestJobRoundTrip | 3 | 全字段 round-trip、键集合、trigger 原文 |
| TS-15 | test_jobs.py | TestJobValidation | 10 | 缺字段/6 种值域违规/IntervalTrigger(0)/(-5) |
| TS-16 | test_registry.py | TestRegistryDegrade | 4 | success 记录、missing 降级、func 字符串、注册表 API |
| TS-17 | test_store.py | TestAtomicSaveRestore | 3 | 无 .tmp、恢复一致（记录逐条相同） |
| TS-18 | test_store.py | TestCorruptAndMissing | 4 | 损坏 JSON、version=2、字节不变、缺失空库 |
| TS-19 | test_scheduler_core.py | Test24hVirtualClock | 4 | 5 混合 job 触发时刻零偏差、paused 零触发、fail attempts=3 |
| TS-20 | test_scheduler_core.py | TestPauseResume | 3 | pause 期零记录、next 不变、resume 后 discard 跳到 10:17 |
| TS-21 | test_scheduler_core.py | TestTriggerNow | 3 | enabled/paused 各 1 条且 next 不变、ghost 抛错 |
| TS-22 | test_misfire.py | TestDiscard | 2 | 0 次错过运行、next=10:17、10:17 正常触发 |
| TS-23 | test_misfire.py | TestCoalesce | 3 | 10 次排水、scheduled 递增、追平后第 11 次 tick |
| TS-24 | test_misfire.py | TestFireOnce | 2 | 1 次且 scheduled=10:06、next=10:17 |
| TS-25 | test_misfire.py | TestGraceBoundary | 2 | lateness<grace 与 ==grace 均走正常分支 |
| TS-26 | test_executor.py | TestRetryBackoff | 4 | k=3 序列 [2,4,8]、封顶 [100,200,300,300,300]、success/error 状态 |
| TS-27 | test_executor.py | TestTimeout | 2 | ≤1s 返回、timeout 记录 attempts=2 且重试继续 |
| TS-28 | test_executor.py | TestConcurrency | 1 | 20 记录 success、墙钟 < 2s |
| TS-29 | test_cli.py | TestCliSubprocess | 8 | 退出码 0/2/0/2/0,0/1/2/0、JSONL 合法 |
| TS-30 | test_gates.py | TestGates | 4 | demo 子进程、acceptance 子进程、零依赖 AST、discover 统计 |
| **合计** | 12 个文件 | 21 个类 | **120** | ≥ 40（A10）；解析类单独 38 ≥ 30（A1） |

### 9.2 可复用夹具设计（`tests/support.py`，非测试文件）

```python
# 1) 虚拟时钟：直接复用产品类（基础设施复用，非被测逻辑）
from scheduler.clocks import VirtualClock

# 2) 记录型假 sleep：只记录请求时长，不耗时、不推进时钟（TS-26/TS-19）
class RecordingSleep:
    def __init__(self) -> None: self.calls: list[float] = []
    def __call__(self, seconds: float) -> None: self.calls.append(seconds)

# 3) 暴力扫描参考实现：独立解析 + 独立逐分钟匹配（不 import cron_parser 的匹配逻辑）
def brute_force_next(expression: str, dt: datetime) -> datetime:
    """Independent field parser (supports *, a, a-b, */n, a-b/n, lists,
    names, macros) + minute-by-minute scan from dt+1min, 4-year window.
    Raises scheduler.errors.NoMatchError when the window has no match.
    DOM/DOW restricted 口径与 4.1 第 9 条相同（段原文 != '*'）。"""

# 4) 随机表达式采样（保证参考实现可快速命中：频率 <= 每天数次，避开永不命中）
def random_expression(rng: random.Random) -> str:
    """Template pool: minute/hour/day/week/month/step/named/DOM-DOW combos."""

# 5) 临时目录管理
@contextmanager
def tmp_paths() -> Iterator[tuple[str, str]]:
    """TemporaryDirectory yielding (store_path, log_path); cleanup after."""

# 6) 构造辅助
def make_job(name: str, trigger: Trigger, **overrides) -> Job:
    """Job factory with explicit defaults (never rely on constructor defaults
    in assertions - R5: tests always pass parameters explicitly)."""

def build_scheduler(*, clock=None, store_path=None, log_path=None,
                    max_workers=None, sleep=None, jobs=()) -> CronScheduler:
    """INLINE-mode scheduler: VirtualClock(start=2026-10-14T00:00:00),
    memory/file RunLogger, RecordingSleep by default."""

# 7) ground truth（TS-19 独立期望值）
def expected_cron_times(expression, start, end) -> list[datetime]:
    """brute force enumeration within [start, end]."""
```

夹具要点：

- **VirtualClock now/sleep 一体化**：`vclock = VirtualClock(datetime(2026,10,14))`；模拟推进 `vclock.advance(60)`；需要「sleep 即推进」的场景注入 `sleep=vclock.advance`（如驱动 `run()`）。
- **内联执行模式**：`build_scheduler()` 默认 `max_workers=None` ⇒ tick 返回后记录已落（INV-E5）。
- **暴力参考独立**：对拍（TS-10）不共享 `cron_parser` 的任何匹配代码，只共享异常类型与「restricted 口径」这一文档化规则。
- **确定性**：随机用例一律 `random.Random(<fixed seed>)`；无时间戳断言（除墙钟上界）。

### 9.3 用例数估算汇总

| 文件 | 用例数 | 累计 |
|---|---|---|
| test_cron_parser.py | 38 | 38 |
| test_next_after.py | 14 | 52 |
| test_triggers.py | 6 | 58 |
| test_jobs.py | 13 | 71 |
| test_registry.py | 4 | 75 |
| test_store.py | 7 | 82 |
| test_scheduler_core.py | 10 | 92 |
| test_misfire.py | 9 | 101 |
| test_executor.py | 7 | 108 |
| test_cli.py | 8 | 116 |
| test_gates.py | 4 | 120 |

**合计 120 个用例 ≥ 40（A10）**；其中 A1 解析用例 38 ≥ 30；A2 随机对拍 3 方法 × ≥20 组 = ≥60 组 ≥ 20。

### 9.4 真实时间测试的边界（白名单制）

| 用例 | 真实资源 | 墙钟上界 | 理由 |
|---|---|---|---|
| TS-27（test_executor.TestTimeout，2 例） | 真实线程池 + `time.sleep` | 每例 ≤ 1.5s | `future.result(timeout)` 必须真实等待（R6） |
| TS-28（test_executor.TestConcurrency，1 例） | 真实线程池 + `sleep(0.05)` | < 2s | A7 硬门禁 |
| TS-29（test_cli.TestCliSubprocess，8 例） | 子进程；`run --duration 2` 1 例 | 总计 ≤ 10s | A8；其余子命令 < 1s |
| TS-30（test_gates，4 例） | 子进程（demo/acceptance/discover） | 总计 ≤ 300s | A9/A10 门禁；acceptance 内含全量 discover |

规则：白名单外一律虚拟时钟 + 假 sleep + 内联池；任何真实 sleep 单次 ≤ 0.05s（除 timeout 语义必须的 0.2s）；`time.sleep` 不得出现在断言路径。

### 9.5 gate 递归防护（TS-30 专用机制）

问题：`acceptance.py` 运行 `unittest discover`，而 discover 套件含 TS-30 的「跑 acceptance.py 子进程」用例 ⇒ 无限递归。

机制（设计假设 DEV-11）：

- `test_gates.py` 的所有子进程类用例标注 `@unittest.skipIf(os.environ.get("CRON_GATE_DEPTH"), "gate child run")`。
- `acceptance.py` 以 `env["CRON_GATE_DEPTH"] = "1"` 启动 discover 子进程 ⇒ 子套件跳过 gate 子进程用例，其余 116 用例正常执行。
- 顶层直接 `discover`（无该变量）⇒ gate 用例执行，其子进程自动带上 `CRON_GATE_DEPTH=1`，不再递归。

---

## 10. demo 与 acceptance 设计

### 10.1 `demo.py`（F9，TS-30(a)）

- 场景：`VirtualClock(2026-10-14T00:00:00)`，INLINE 执行（`max_workers=None`），`RecordingSleep`，内存 `RunLogger`，无 store。
- 注册计数器函数（闭包计数），6 个 Job：`0 * * * *`（期望 24）、`*/15 * * * *`（96）、`0 3 * * *`（1）、`30 2 * * 3`（1，2026-10-14 即周三）、interval=900s（96）、外加 1 个 paused Job（0）与 1 个 `func=fail`、`max_retries=2` 的 `0 * * * *` Job（24 次触发、每次 attempts=3、status=error）。
- 驱动：`for _ in range(1440): vclock.advance(60); scheduler.tick()`。
- 断言（自带期望值）：各计数相等；paused 零记录；fail 记录 24 条且 attempts=3；任一失败 → 打印 `FAIL: ...` 并退出码 1；全过 → 打印摘要行、退出码 0。

### 10.2 `acceptance.py`（F10，TS-30(b)，退出码即门禁）

按序执行 4 项，逐项打印 `PASS`/`FAIL`，全部 PASS ⇒ 退出码 0：

| # | 检查 | 命令/方式 | 通过条件 |
|---|---|---|---|
| G-1 | 全量测试 | `sys.executable -m unittest discover -s tests -v`（env `CRON_GATE_DEPTH=1`，cwd=任务根） | 退出码 0 且输出 `Ran N tests` 中 N ≥ 40 |
| G-2 | demo 自检 | `sys.executable demo.py` | 退出码 0 |
| G-3 | 零依赖（A9） | AST 遍历 `scheduler/` 与 `tests/`（跳过 `__pycache__`）：收集 `import x` / `from x import ...` / `from . import ...` 的顶层名；每个名 ∈ `sys.stdlib_module_names` ∪ `{"scheduler", "tests"}` | 无越界名 |
| G-4 | CLI 冒烟（A8 关键子集） | 子进程：`add`（0）、重复 `add`（2）、`list`（0 且含合法 JSON 行）、`remove ghost`（2）、`run --duration 2`（0 且 JSONL ≥1 行） | 退出码与产物符合 |

---

## 11. 风险与缓解

### 11.1 承接需求 R1~R10（需求 7.1，设计层落实点）

| ID | 设计层落实 |
|---|---|
| R1 | 超时放弃等待不 cancel（5.4）；README 与 5.4 明示；测试只验证返回时长/记录/重试；`close(wait=False)` 支持不停留退出 |
| R2 | 全程 naive datetime；ISO 序列化不带时区后缀 |
| R3 | DOW 归一化单点实现（`parse_field` 第 7 条）；TS-04 专项 + TS-10 对拍 |
| R4 | coalesce = 每 tick 一次排水（5.5 S1/S4 + INV-S1）；TS-23 按多次 tick 编写 |
| R5 | 全部默认值集中在 4.4 常量与 4.10 参数表；测试夹具 `make_job` 显式传参 |
| R6 | 内联模式 timeout 不生效（INV-E4）；TS-27 强制真实池；文档与注释明示 |
| R7 | 核心只认注入 `clock.now()`；真实模式由默认 grace=60s 吸收抖动 |
| R8 | 单写者假设写入 6.1 表与 README；不做文件锁 |
| R9 | 字段递进（5.1）；蛮扫仅在 tests/support.py；`NoMatchError` < 5s（TS-11） |
| R10 | 内置 `noop`/`fail`（4.9）；CLI add 不校验 func 已注册（运行期降级）；README 说明 |

### 11.2 设计层新增风险（D1~D6）

| ID | 风险 | 影响 | 缓解 |
|---|---|---|---|
| D1 | S6「daemon 线程」与 S9「ThreadPoolExecutor」存在实现张力：标准库池线程非 daemon 且退出时 atexit join，挂起函数会延迟进程退出（最长约函数时长） | 若验收脚本断言「run 后进程立即退出」会误判 | 采用 S9 优先口径（DEV-01）；README/5.4 明示；`close(wait=False, cancel_futures=True)`；测试与验收不断言即时退出（R1） |
| D2 | `duration` 口径（各次尝试 perf_counter 观测和）与「时钟差」直觉不一致；虚拟时钟下瞬时函数 duration=0.0 | 测试若断言 duration>0 会失败 | INV-L3 + 9.4：测试只断 `duration >= 0` 与有限性，不断言具体值 |
| D3 | gate 递归：acceptance 跑 discover、discover 又跑 acceptance | 无限递归、验收卡死 | `CRON_GATE_DEPTH` 环境变量 + skipIf（9.5）；acceptance 子进程注入该变量 |
| D4 | `list` 输出是 to_dict 的**子集**（9 键 vs 13 键），两处键集不同易混淆 | 编码/测试取错键集 | 4.10 `_cmd_list` 明确固定键序；TS-29 只断言 `"name": "j1"` 子串 |
| D5 | misfire 跳转分支对 interval 的口径（next = now + interval）无 TS 直接覆盖 | interval + discard/fire_once 行为可能与预期不符 | DEV-02 明示；TS-13 覆盖恢复路径；测试 agent 可补 1 例 interval+discard（计入 120 之外的余量） |
| D6 | 池模式下记录写入失败被吞（`async_errors`），CLI 退出码 1 覆盖不到 | 审计丢记录且无感知 | CLI run 启动预探日志可写（同步检测 → 1）；文档明示运行期降级；`async_errors` 供诊断 |

---

## 12. 偏差声明与上游问题

### 12.1 对章程/需求的细化与偏离（逐条）

| # | 条目 | 类型 | 内容与理由 |
|---|---|---|---|
| DEV-01 | S6 vs S9 | 偏离（实现口径） | S6 要求超时后「底层线程作为 daemon 继续执行」，但标准库 `ThreadPoolExecutor`（S9 明确）的线程无法设为 daemon，且退出时 atexit 会 join。设计选 S9 优先：`future.result(timeout)` 到点放弃等待、不 cancel、不中断，线程继续执行（行为本质不变），进程退出可能短暂延迟。已在 5.4/11.2-D1 明示，README 将同步。请编排者确认该口径可接受。 |
| DEV-02 | FR-09 规则 2/4 | 细化 | 「跳到未来下一个匹配时刻」对 interval 的落地：`get_next_fire_time(now) = now + interval`（与 cron 的 `next_after(now)` 同构，丢弃全部积压从当前起跳）。FR-09.5 只定义了「一个周期」口径，未定义跳转口径。 |
| DEV-03 | FR-08.9 | 细化 | `run(duration)` 的到期判定用注入时钟（`deadline = clock.now()+duration`）而非 `perf_counter`，使虚拟时钟可驱动 `run`（N4）。 |
| DEV-04 | 4.3 `duration` | 细化 | 多次尝试时 `duration` = 各次尝试 perf_counter 观测之和（不含退避 sleep）；4.3 未定义聚合口径。与 4.3 timeout 示例（duration≈0.2）相容。 |
| DEV-05 | 4.5 异常表 | 新增（兼容） | 新增 `JobAlreadyExistsError(SchedulerError, ValueError)`；4.5 表未列该名，但其为 `ValueError` 子类，CLI 映射（退出码 2）不变。 |
| DEV-06 | 4.2 序列化 | 细化 | 落盘格式 `json.dumps(..., sort_keys=True, indent=2)` + 末尾换行；4.2 只要求 sort_keys，未定缩进。 |
| DEV-07 | 4.2 `next_run_time` | 细化 | 加载用 `datetime.fromisoformat`（接受含微秒的超集），序列化固定 `%Y-%m-%dT%H:%M:%S`；保证 round-trip 无损。 |
| DEV-08 | S1 步长/列表形式 | 收紧 | `a/n`（如 `5/15`）与列表中含 `*`（如 `1,*`）判非法；S1 仅列出 `*`、`a`、`a-b`、`*/n`、`a-b/n`、列表形式。 |
| DEV-09 | S2 restricted 判定 | 细化 | DOM/DOW「受限」= 段原文 strip 后 `!= "*"`（故 `*/2` 视为受限，参与「或」语义）；遵 S2 字面，与 Vixie「星号开头即 unrestricted」略有差异。对拍参考实现采用同一文档化规则。 |
| DEV-10 | TS-29 解释器 | 细化 | CLI 子进程测试用 `sys.executable`；验收场景下 `sys.executable` 即章程指定解释器，等价且更可移植。 |
| DEV-11 | TS-30 递归 | 新增机制 | `CRON_GATE_DEPTH` 环境变量 + `skipIf` 防止 acceptance↔discover 递归（9.5）。 |
| DEV-12 | tests 布局 | 新增约定 | `tests/__init__.py` 空文件 + `from tests.support import ...` 绝对包导入，保证 discover 兼容。 |
| DEV-13 | Job.from_dict | 放宽 | 未知额外键忽略（前向兼容）；`to_dict` 仍严格输出 13 键。 |
| DEV-14 | VirtualClock 并发 | 口径 | 仅保证「多线程读 + 单线程写」安全（GIL 原子赋值）；不提供跨线程写保护。 |
| DEV-15 | 4.2 唯一性 | 收紧 | 加载期发现重名 `name` → `StoreError`（库内唯一约束的加载期检查）。 |
| DEV-16 | 记录写失败 | 降级口径 | 池模式下 `on_record` 异常收集进 `async_errors` 并丢弃该记录（不冒泡、不重试）；CLI 以启动预探覆盖「日志不可写 → 退出码 1」。 |

### 12.2 发现的上游问题（不自行修改上游文件，交编排者裁决）

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| UP-01 | 章程 S6 × S9 | 「底层线程作为 daemon 继续执行」与「`ThreadPoolExecutor`」在标准库内无法同时严格成立（DEV-01）。 | 请编排者确认 S9 优先 + 明示限制的口径；若要求严格 daemon，需走章程变更改 S9（自建 daemon 线程池），影响 S9/A7 验收表述。 |
| UP-02 | 需求 4.3 示例 | timeout 示例行 `finished_at == started_at` 而 `duration = 0.2`；若 duration 定义为 finished-started 则内部不自洽。 | 设计采用「duration = 各次尝试观测和」（DEV-04）与示例相容；请确认该聚合口径。 |
| UP-03 | 需求附 B | 附加要求「cron 字段名称须同时支持命名范围（MON-FRI、JAN-MAR）」无 TS 覆盖。 | 测试策略已在 test_cron_parser.py 补 `TestNamedRanges` 3 例（计入 120 用例）；无需改需求。 |

### 12.3 无偏差声明

除 12.1/12.2 所列外，本设计对 S1–S12、FR-01~14、4.1~4.5 契约、TS-01~30 场景**无其他偏差**；未发现其他上游错误。

---

## 附录 A：常量与默认值汇总（单一事实来源）

| 常量 | 值 | 出处 |
|---|---|---|
| `DEFAULT_MISFIRE_POLICY` | `"discard"` | C1 / 4.2 |
| `DEFAULT_MISFIRE_GRACE` | `60.0` | C1 / 4.2 |
| `DEFAULT_MAX_RETRIES` | `0` | C1 / 4.2 |
| `DEFAULT_RETRY_BACKOFF` | `1.0` | C1 / 4.2 |
| `DEFAULT_RETRY_BACKOFF_MAX` | `300.0` | S7 / 4.2 |
| 默认 timeout | `None` | C1 / 4.2 |
| 默认 max_workers | `4` | S9 |
| CLI `--store` | `cron_jobs.json` | C1 / 4.1 |
| CLI `--log` | `cron_runs.jsonl` | C1 / 4.1 |
| CLI `--tick` | `1.0` | C1 / 4.1 |
| `STORE_VERSION` | `1` | 4.2 |
| next_after 上界 | 4 个日历年 | S3 / FR-02.4 |
| ISO 格式 | `%Y-%m-%dT%H:%M:%S` | 4.2 / 4.3 |
| 退出码 | 0 成功 / 2 用法校验 / 1 运行时 | S10 / 4.1 |

## 附录 B：不变量速查（实现自检清单）

| ID | 不变量 |
|---|---|
| INV-C1/2 | 虚拟时钟纯读不变；advance ≥ 0 |
| INV-T1~4 | 触发器 fail-fast；结果严格晚于 after；interval>0；round-trip 保真 |
| INV-R1~3 | 注册表不序列化；get 失败降级；全方法持锁 |
| INV-J1~5 | Job round-trip；13 键；func 为字符串；None=无未来触发；容器类型校验 |
| INV-L1~4 | 一触发一记录；追加语义；duration 口径；INLINE 同步落记录 |
| INV-E1~6 | attempts∈[1,max_retries+1]；退避公式与单次封顶；超时计尝试；内联无 timeout；INLINE 同步；池 fire-and-forget |
| INV-S1~8 | tick 内一次/name 序/锚定推进/pause 零动作/trigger_now 不改 next/tick 末存/加载不重算/next 形态 |
| 算法 | next_after 严格大于且秒=0；4 年上界；DOM/DOW 或与；跳跃单调 |

（完）
