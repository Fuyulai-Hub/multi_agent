# 多账户记账库（miniledger）需求文档

## 1. 背景与目标

为对账系统提供一个可嵌入的多账户记账库：支持开户、存取、转账、流水查询与 JSON 持久化。
记账必须精确到分、转账必须原子、重复请求必须幂等（支付网关会重投同一笔请求），
余额与流水必须能在进程重启后完整恢复。

## 2. 技术约束

- 语言：Python 3.9+，交付形态为可通过 `import miniledger` 使用的库（backend_only，无前端、无 HTTP 服务）
- 纯标准库，零第三方依赖；金额使用 `decimal.Decimal`，统一 `ROUND_HALF_UP` 量化到两位小数
- 交付包名必须是 `miniledger`

## 3. 公共接口（必须严格按此命名与签名）

```python
from decimal import Decimal
from miniledger import (
    AccountBook, Account, Transaction,
    LedgerError, AccountNotFoundError, AccountExistsError,
    InsufficientFundsError, AccountFrozenError, IdempotencyConflictError,
)
```

- `AccountBook()`：创建空账本
- `book.create_account(account_id: str, owner: str, currency: str = "CNY") -> Account`
- `book.freeze(account_id: str, frozen: bool = True) -> Account`
- `book.deposit(account_id: str, amount, txn_id: str | None = None, when: str | None = None) -> Transaction`
- `book.withdraw(account_id: str, amount, txn_id: str | None = None, when: str | None = None) -> Transaction`
- `book.transfer(src: str, dst: str, amount, txn_id: str | None = None, when: str | None = None) -> Transaction`
- `book.balance(account_id: str) -> Decimal`
- `book.statement(account_id: str, start: str | None = None, end: str | None = None, limit: int | None = None) -> list[dict]`
- `book.summary() -> dict`
- `book.save(path: str) -> None`；`AccountBook.load(path: str) -> AccountBook`

`Account` 至少包含属性：`account_id, owner, currency, balance: Decimal, frozen: bool`。
`Transaction` 至少包含属性：`txn_id: str, kind: str, when: str, amount: Decimal, src: str | None, dst: str | None`，
其中 `kind` 取值 `"deposit" | "withdraw" | "transfer"`。
异常层级：除 `LedgerError` 外其余五个异常都必须是 `LedgerError` 的子类。

## 4. 功能点

### FP-01 开户 create_account
- 描述：创建账户并返回 Account
- 规则：`account_id` 必须匹配 `^[A-Za-z][A-Za-z0-9_-]{2,31}$`（3~32 字符，首字符为字母），否则抛 `LedgerError`；
  `owner` 必须是非空字符串且去空白后非空，否则抛 `LedgerError`；
  `currency` 只允许 `CNY`/`USD`/`EUR`（大小写不敏感，内部统一存为大写），否则抛 `LedgerError`；
  重复开户抛 `AccountExistsError`；新账户余额为 `Decimal("0.00")`，`frozen=False`
- 样例：`create_account("acc_001", "Alice")` → `balance == Decimal("0.00")`，`currency == "CNY"`
- 类别：功能

### FP-02 存款 deposit
- 描述：向账户存入金额，返回 Transaction
- 规则：金额必须为正（>0），量化到两位小数后再校验；`when` 缺省为当前 UTC 时间（ISO 8601 字符串）；
  账户不存在抛 `AccountNotFoundError`；冻结账户**允许**存款；成功后余额增加
- 样例：`deposit("acc_001", "10.005")` → 余额 `10.01`（HALF_UP）
- 类别：功能

### FP-03 取款 withdraw
- 描述：从账户取出金额，返回 Transaction
- 规则：余额不足抛 `InsufficientFundsError` 且余额保持不变；冻结账户抛 `AccountFrozenError`；
  金额规则同 FP-02
- 样例：余额 10.00 时 `withdraw(..., "10.01")` → 抛 `InsufficientFundsError`，余额仍 10.00
- 类别：功能

### FP-04 转账 transfer
- 描述：账户间转账，必须原子
- 规则：任一账户不存在抛对应 `AccountNotFoundError`；源账户冻结抛 `AccountFrozenError`；
  币种不同抛 `LedgerError`；余额不足抛 `InsufficientFundsError`；
  **任一校验失败时源/目标余额都不得变化**；成功后 `kind == "transfer"`，`src`/`dst` 为账户号
- 样例：A 100.00 → B 40.00 后，A=60.00，B=40.00
- 类别：功能

### FP-05 幂等 txn_id
- 描述：同一 `txn_id` 重复提交时不得重复记账
- 规则：`txn_id` 缺省时由库生成（形如 `txn_` 前缀，全局唯一）；
  已存在且业务参数（kind、规范化金额、src、dst）完全一致时，返回**首次**的 Transaction 且不新增流水；
  业务参数不一致时抛 `IdempotencyConflictError`；金额按量化后的 Decimal 比较，`"10"` 与 `"10.00"` 视为相同
- 样例：同一 txn_id 调用两次 deposit("10") → 余额只增加一次，两次返回对象 txn_id 相同
- 类别：功能

### FP-06 流水 statement
- 描述：按时间返回账户流水，含逐笔余额
- 规则：按 `(when, 记账序号)` 升序；`start`/`end` 为 `YYYY-MM-DD`，按日期**闭区间**过滤（含首尾）；非法日期格式抛 `LedgerError`；
  `limit` 必须为正整数，含义是"过滤后按时间倒序取最近 N 笔，再按时间升序返回"；`limit=None` 返回全部；
  每条 dict 至少包含键：`seq`(int)、`txn_id`、`kind`、`when`、`amount`(带符号 Decimal，存入为正、取出为负)、
  `balance_after`(Decimal)、`counterparty`(转账时对端账户号，否则为空字符串)
- 样例：三笔流水后 `statement(limit=2)` 返回最后两笔，且仍为时间升序
- 类别：功能

### FP-07 汇总 summary
- 描述：返回账本汇总
- 规则：dict 至少包含 `accounts`(账户数,int)、`transactions`(去重后的交易数,int)、
  `totals`(dict[str, Decimal]，按币种聚合所有账户余额，无账户的币种不出现)；金额量化为两位小数
- 样例：两个 CNY 账户余额 60.00 与 40.00 → `totals["CNY"] == Decimal("100.00")`
- 类别：功能

### FP-08 持久化 save/load
- 描述：JSON 持久化与恢复
- 规则：`save` 后 `AccountBook.load` 必须恢复账户、余额、冻结状态、流水、幂等索引与序号计数；
  恢复后继续记账，`statement` 的 `seq` 不得与已恢复的序号冲突；加载不存在路径抛 `FileNotFoundError`；
  加载非法 JSON 或结构缺字段抛 `LedgerError`
- 样例：save → load → balance 相同，且同 txn_id 再提交仍幂等
- 类别：功能

### FP-09 输入校验
- 描述：非法输入统一失败
- 规则：金额接受 `str`/`int`/`Decimal`，**拒绝 `float` 与 `bool`**（抛 `LedgerError`）；
  无法解析的字符串、NaN/Infinity 抛 `LedgerError`；金额先按 HALF_UP 量化到两位小数，量化后 `<= 0` 的一律抛 `LedgerError`；
  未知账户的任何读写操作抛 `AccountNotFoundError`；未知币种、非法账户号、空 owner 抛 `LedgerError`
- 类别：异常

### FP-10 边界条件
- 描述：临界值行为
- 规则：金额恰好 `0.005` 量化为 `0.01`，`0.004` 量化为 `0.00` 并因非正被拒；
  `account_id` 长度 3 与 32 合法、2 与 33 非法；`limit=1` 与 `limit=None` 行为正确，`limit=0` 或负数抛 `LedgerError`；
  `start == end` 时返回当天全部流水；无流水账户 `statement() == []`；余额恰好等于取款金额时允许取到 0.00
- 类别：边界

## 5. 异常与边界要求

- 所有对外方法对非法输入必须抛出上述异常体系内的异常，禁止静默返回或吞错
- 转账失败不得留下半笔流水（原子性）
- 时间只作为字符串存储与比较，不做时区换算；调用方传入的 `when` 原样保存

## 6. 验收标准

- 全部功能点实现且 `import miniledger` 可用，公共接口命名与签名一致
- 功能 / 异常 / 边界三类用例全部通过（覆盖率 100%）
- 无阻塞级代码审查问题，全部缺陷闭环
- 待澄清问题在交付前完成协商闭环并写入技术方案
