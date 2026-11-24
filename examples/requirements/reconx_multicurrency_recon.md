# 多币种支付对账引擎（reconx）需求文档

## 1. 背景与目标

支付平台每日需要把渠道账单与内部交易流水做对账，识别金额不符、币种不符、重复入账与单边缺失，
输出可审计的差异清单与汇总报表，供资金运营定位差异并驱动修复。
当前流程依赖人工 Excel 比对（日均 20 万笔、CNY/USD/EUR 三种币种），需要交付一个可嵌入对账服务的纯 Python 库。

## 2. 技术约束

- 语言 Python 3.9+，纯标准库，零第三方依赖（禁止 import 任何第三方包）
- 交付形态为可 `import reconx` 的库（backend_only：无前端、无 HTTP 服务、无命令行入口）
- 交付包名必须是 `reconx`
- 金额一律 `decimal.Decimal` + `ROUND_HALF_UP` 量化两位小数，禁止用 float 参与金额运算
- 时间一律 UTC 的 `YYYY-MM-DDTHH:MM:SSZ` 字符串，字典序比较等价于时间序
- 同一输入任意次运行，结果的字段、顺序与数值完全一致（确定性）

## 3. 公共接口

必须导出以下名字，函数签名与属性名严格一致：

```python
from decimal import Decimal
from reconx import (
    Record, ParseIssue, MatchPair, DiffItem, ReconResult,
    ReconError, ParseError, ConfigError,
    parse_channel_csv, parse_internal_csv, reconcile, summarize,
)

parse_channel_csv(text: str) -> tuple[list[Record], list[ParseIssue]]
parse_internal_csv(text: str) -> tuple[list[Record], list[ParseIssue]]
reconcile(channel: list[Record], internal: list[Record], *, window_seconds: int = 300) -> ReconResult
summarize(result: ReconResult) -> dict
```

- `Record` 属性：`trade_no: str`、`amount: Decimal`、`currency: str`、`paid_at: str`、`merchant_id: str`、`source: str`、`row_no: int`
- `ParseIssue` 属性：`row_no: int`、`reason: str`
- `MatchPair` 属性：`trade_no: str`、`channel: Record`、`internal: Record`、`phase: str`、`time_gap_seconds: int`
- `DiffItem` 属性：`kind: str`、`trade_no: str`、`currency: str`、`channel: Record | None`、`internal: Record | None`、`delta: Decimal`
- `ReconResult` 属性：`matched: list[MatchPair]`、`diffs: list[DiffItem]`、`issues: list[ParseIssue]`
- 异常层级：`ReconError(Exception)`；`ParseError(ReconError)`；`ConfigError(ReconError, ValueError)`

## 4. 输入格式

- 渠道 CSV 必需表头为 `trade_no`、`amount`、`currency`、`paid_at`、`merchant_id`，列顺序任意
- 内部 CSV 必需表头为 `txn_id`、`amt`、`ccy`、`created_at`、`merchant`，列顺序任意，依次映射为 `trade_no`、`amount`、`currency`、`paid_at`、`merchant_id`
- 字段用逗号分隔；字段被双引号包裹时，内部逗号与 `""` 转义的双引号是字面量；引号内出现换行视为非法
- 输入按 UTF-8 解析，开头的 BOM 必须剥离；行尾支持 `\n` 与 `\r\n`
- 空白行（strip 后为空）直接跳过，不计入 issues

## 5. 功能点

### FP-01 渠道账单解析 parse_channel_csv
- 描述：把渠道 CSV 文本解析为 Record 列表与坏行清单
- 规则：表头必须是第一条记录；必需列缺失抛 `ParseError`（line=1、column=0，message 含缺失列名）
- 规则：表头出现重复列名抛 `ParseError`（line=1，column 为第二次出现该列的 1-based 列位置，message 含列名）
- 规则：每条数据记录的 `row_no` 为物理行号（表头为第 1 行，首条数据为第 2 行），`source` 固定为 `"channel"`
- 规则：字段数与表头不一致的行不中断解析，记入 issues，`reason` 固定为 `"field_count"`
- 规则：空文本或只有表头时返回 `([], [])`
- 样例：`"trade_no,amount,currency,paid_at,merchant_id\nT1,10.00,CNY,2026-01-01T00:00:00Z,M1\n"` → 1 条 Record、0 条 issue，`amount == Decimal("10.00")`
- 类别：功能

### FP-02 内部流水解析 parse_internal_csv
- 描述：把内部流水 CSV 文本解析为 Record 列表与坏行清单
- 规则：按第 4 节的字段映射执行，输出的 `Record.source` 固定为 `"internal"`
- 规则：`trade_no` 去首尾空白后必须非空，否则记 issue `reason="empty_trade_no"`，不产生 Record
- 规则：`merchant_id` 去首尾空白后允许为空字符串
- 规则：表头校验与坏行处理与 FP-01 完全一致（缺列、重复列、字段数不一致）
- 样例：`"txn_id,amt,ccy,created_at,merchant\nT2,20.5,usd,2026-01-01T00:00:01Z,\n"` → `currency == "USD"`、`amount == Decimal("20.50")`、`merchant_id == ""`
- 类别：功能

### FP-03 记录规范化与坏值隔离
- 描述：解析产出的每条记录都必须是已校验、已规范化的值，坏值只进 issues
- 规则：金额去空白后必须匹配 `^-?\d+(\.\d+)?$`，否则记 issue `reason="invalid_amount"`
- 规则：金额按 `ROUND_HALF_UP` 量化两位后必须大于 0，否则记 issue `reason="invalid_amount"`（`0.005` 量化为 `0.01` 合法，`0.004` 量化为 `0.00` 非法）
- 规则：币种去空白并转大写后必须属于 `{"CNY","USD","EUR"}`，否则记 issue `reason="invalid_currency"`
- 规则：`paid_at` 必须严格匹配 `YYYY-MM-DDTHH:MM:SSZ` 且是真实存在的 UTC 时刻，否则记 issue `reason="invalid_time"`
- 规则：同一行有多个坏值时只记第一条原因，检查顺序固定为金额、币种、时间、交易号
- 类别：异常

### FP-04 精确匹配
- 描述：优先按交易号做一对一精确匹配
- 规则：同一 `trade_no` 在渠道与内部各恰好 1 条时，若币种相同且量化后金额相等，产生 `MatchPair(phase="exact")`
- 规则：`time_gap_seconds` 为两侧 `paid_at` 的绝对秒差（非负整数）；精确匹配不受 `window_seconds` 限制
- 规则：金额比较使用量化后的 Decimal，`"10"` 与 `"10.00"` 视为相等；匹配结果按 `(trade_no, phase)` 升序排列
- 规则：`reconcile` 的 channel 与 internal 必须是 list 且每个元素都是 Record，否则抛 `ConfigError`
- 样例：渠道 T1 10.00 CNY 与内部 T1 10.00 CNY → 1 条 exact，`time_gap_seconds` 等于两侧时间差
- 类别：功能

### FP-05 差异归因（金额、币种、重复）
- 描述：同交易号但关键字段不一致时判定为差异，同路重复入账单独归因
- 规则：同 `trade_no` 两侧各 1 条、币种相同但金额不同 → `kind="amount_mismatch"`，`delta = 渠道金额 - 内部金额`
- 规则：同 `trade_no` 两侧各 1 条但币种不同 → `kind="currency_mismatch"`，`delta = Decimal("0.00")`，`currency` 取渠道侧
- 规则：同一路内同 `trade_no` 出现多次时，组内按 `(paid_at, row_no)` 升序取第一条为主记录，其余每条产生 `kind="duplicate_in_channel"` 或 `kind="duplicate_in_internal"`
- 规则：重复差异的 `delta` 在渠道重复时取正的该条金额、在内部重复时取负的该条金额，缺失侧字段为 `None`；重复记录不参与后续匹配
- 规则：两侧都重复时先各自取主记录，再按精确匹配规则处理主记录
- 样例：渠道 T1 出现两次（10.00 与 10.00）、内部 T1 一次 → 1 条 duplicate_in_channel，`delta == Decimal("10.00")`
- 类别：功能

### FP-06 模糊匹配
- 描述：交易号对不上的记录，按币种、金额与时间窗做一对一配对
- 规则：只有未匹配、非重复的剩余记录参与模糊匹配；候选条件为币种相同、量化后金额相等、时间差小于等于 `window_seconds`
- 规则：候选按 `(time_gap_seconds, 渠道 trade_no, 内部 trade_no)` 升序贪心配对，已配对的记录不再参与
- 规则：`window_seconds` 必须是 `int`（拒绝 `bool`）且大于 0，否则 `reconcile` 抛 `ConfigError`
- 规则：时间差恰好等于 `window_seconds` 时算匹配，超出 1 秒不匹配；模糊匹配的 `phase="fuzzy"`，`trade_no` 取渠道侧交易号
- 样例：渠道 T1 与内部 T9 同币种同金额、时间差 300 秒、`window_seconds=300` → 匹配成功
- 类别：功能

### FP-07 单边差异
- 描述：无法配对的剩余记录判定为单边缺失
- 规则：剩余渠道记录 → `kind="missing_in_internal"`，`internal=None`，`delta` 为正的渠道金额
- 规则：剩余内部记录 → `kind="missing_in_channel"`，`channel=None`，`delta` 为负的内部金额
- 规则：单边差异的 `currency` 取存在一侧的币种
- 规则：`diffs` 按 `(kind 固定枚举序, trade_no, currency)` 升序排列；固定枚举序为 `amount_mismatch`、`currency_mismatch`、`duplicate_in_channel`、`duplicate_in_internal`、`missing_in_internal`、`missing_in_channel`
- 规则：两侧都为空时返回空的 `matched` 与 `diffs`，不得抛异常
- 类别：功能

### FP-08 汇总报表 summarize
- 描述：把一次对账结果汇总为统计字典，金额字段保持 Decimal 类型
- 规则：返回键固定为 `matched_count`、`exact_count`、`fuzzy_count`、`diff_count`、`by_kind`、`by_currency`、`issues_count`
- 规则：`by_kind` 的键为本次出现的差异类型，值为 `{"count": int, "amount": Decimal}`，`amount` 为该类型所有 `delta` 绝对值之和（量化两位）
- 规则：`by_currency` 的键按币种升序，值为 `{"matched": int, "diff": int}`；`currency_mismatch` 计入渠道侧币种
- 规则：空结果返回全 0 计数与空字典，不得抛异常
- 规则：`summarize` 必须接受 `ReconResult`，否则抛 `ConfigError`；调用不得修改传入对象
- 类别：功能

### FP-09 确定性与错误定位
- 描述：同样的输入必须得到逐字段一致的结果，解析错误必须可定位
- 规则：连续两次 `reconcile` 同一输入，`matched` 与 `diffs` 的元素顺序与字段值完全相等
- 规则：打乱输入列表顺序后，结果排序仍符合 FP-04 与 FP-07 的规则，与输入顺序无关
- 规则：`ParseError` 必须携带 `line`（1-based）与 `column`（1-based，表头级错误为 0）属性
- 规则：`ParseError` 的 `str()` 固定为 `ParseError(line=<n>, column=<m>): <message>`
- 规则：未闭合引号抛 `ParseError`，`line` 为该行物理行号，`column` 为引号所在 1-based 列位置；引号闭合后紧跟非逗号字符抛 `ParseError`，定位到该多余字符
- 类别：边界

## 6. 验收标准

- 全部功能点实现，`import reconx` 可用，公共接口命名与签名一致
- 每类差异都用真实输入构造并断言，不允许只校验计数
- 金额与时间边界（0.005、0.004、window 300 与 301）全部通过
- 无阻塞级代码审查问题，全部缺陷闭环
