# 接口限流与熔断库（ratelimit）需求文档

## 1. 背景与目标

为一个纯 Python 后端服务提供零第三方依赖的流量治理内核：四种主流限流算法（令牌桶、漏桶、滑动窗口日志、滑动窗口计数器）统一接口，多 key（多租户/多接口）各自独立计量并按 LRU 淘汰；配套的熔断器在依赖故障时快速失败并自动试探恢复；限流判定状态可 JSON 快照与恢复；线程安全，时钟与随机性完全可注入以保证测试确定性。

## 2. 技术约束

- 语言：Python 3.9+，交付形态为可通过 `import ratelimit` 使用的库
- 纯标准库实现，零第三方依赖（禁止 import 任何第三方包）
- 时间：一律使用可注入的单调时钟（默认 `time.monotonic`），单元测试必须能注入假时钟；时间参数单位统一为秒（float）
- 领域异常统一继承 `RateLimitError`（其本身继承 `Exception`），配置类错误继承 `ValueError`；禁止静默吞错
- 线程安全：所有对外写读路径用 `threading` 标准锁保护；判定路径不得在持锁期间调用用户回调
- 持久化用 JSON 原子写（同目录临时文件 + os.replace），序列化 `sort_keys=True` 保证确定性

## 3. 功能点

### FP-01 限流器统一抽象 RateLimitAlgorithm
- 描述：定义四种算法共同的判定接口，便于门面与装饰器无差别调用
- 输入：`acquire(tokens=1, now=None) -> bool` / `available(now=None) -> float` / `to_dict() -> dict` / `from_dict(data) -> 实例`（类方法）
- 输出：布尔判定与当前余量
- 规则：`acquire` 的 tokens 必须为正数（<=0 或非数值抛 ValueError），`now` 为 None 时取注入时钟当前值；`to_dict()` 必须含 `"algorithm"` 键用于反序列化分派；`from_dict` 遇到未知 algorithm 值抛 `ConfigError`（ValueError 子类）
- 样例：`bucket.acquire(2)` 返回 True 后 `bucket.available()` 减少 2；`to_dict()["algorithm"] == "token_bucket"`
- 类别：功能

### FP-02 令牌桶 TokenBucket
- 描述：以固定速率补充令牌，允许一定程度的突发流量
- 输入：TokenBucket(capacity, refill_rate, clock=None)；capacity 正整数、refill_rate 每秒令牌数（正浮点）
- 输出：acquire 布尔；available 当前令牌数（float）
- 规则：惰性补充——每次判定前按 `elapsed * refill_rate` 补充并封顶 capacity；初始满桶；tokens 超过当前令牌数时返回 False 且**不产生部分扣减**、令牌数不得为负；elapsed 为负（时钟回拨）时按 0 处理不报错；capacity<=0 或 refill_rate<=0 抛 ValueError
- 样例：capacity=5、refill_rate=1，假时钟 t=0 连续 acquire 5 次全 True、第 6 次 False；t=2.0 时 available 恰好 2.0、acquire(2) True 后 available 0.0
- 类别：功能

### FP-03 漏桶 LeakyBucket
- 描述：请求排队按固定速率流出，流出速率恒定、无突发
- 输入：LeakyBucket(capacity, leak_rate, clock=None)；capacity 正整数（队列长度）、leak_rate 每秒流出数（正浮点）
- 输出：acquire 布尔；available 剩余可容纳请求数；pending 当前排队量
- 规则：惰性排水——判定前按 `elapsed * leak_rate` 排空并下限 0；请求入队即占用 1 容量，超出 capacity 返回 False 且不入队；同一时刻重复判定不得重复排水（水位单调不增）；capacity 或 leak_rate 非正抛 ValueError
- 样例：capacity=3、leak_rate=1，t=0 连续 acquire 三次 True、第四次 False；t=1.0 后 pending 从 3 变 2、再 acquire 一次 True
- 类别：功能

### FP-04 滑动窗口日志 SlidingWindowLog
- 描述：记录每个请求的时间戳，统计窗口内请求数，精确无误差
- 输入：SlidingWindowLog(max_requests, window_seconds, clock=None)
- 输出：acquire 布尔；count(now=None) 窗口内请求数
- 规则：判定前先丢弃 `ts <= now - window` 的记录（严格大于窗口才保留，即恰好等于窗口边界的记录视为过期）；未超上限则追加时间戳并 True，超限 False 且不追加；窗口内计数不得包含未来时间戳；max_requests 或 window_seconds 非正抛 ValueError
- 样例：max=2、window=10：t=0、t=5 各一次 True，t=6 False；t=10.0 时 t=0 那条恰好过期 → acquire True、count==2
- 类别：功能

### FP-05 滑动窗口计数器 SlidingWindowCounter
- 描述：只保留当前与上一窗口的计数，按时间比例加权近似，内存 O(1)
- 输入：SlidingWindowCounter(max_requests, window_seconds, clock=None)
- 输出：acquire 布尔；estimated(now=None) 加权估计值（float）
- 规则：`estimated = prev * (1 - ratio) + cur`，ratio 为当前窗口已过比例；超过 max_requests 时 False 且不增加 cur；时钟跨窗口边界时把 cur 滚动为 prev 并清零 cur；恰好落在窗口起点时 ratio 为 0、prev 全额计入；非正参数抛 ValueError
- 样例：max=10、window=60，t=0（ratio=0）cur 计 6 → estimated 6；t=30（ratio=0.5）时上一窗口 prev=6 → estimated 3.0；t=60 进入新窗口后 prev 滚动为 6、cur 为 0
- 类别：功能

### FP-06 熔断器 CircuitBreaker
- 描述：连续失败达阈值则打开熔断快速失败，冷却后半开试探，成功即恢复
- 输入：CircuitBreaker(failure_threshold, recovery_timeout, half_open_max_calls=1, clock=None, counted_exceptions=(Exception,))
- 输出：state 属性（"closed"/"open"/"half_open"）；`call(func, *args, **kwargs)` 透传返回值或抛异常
- 规则：调用失败（抛 counted_exceptions 中的异常）计数 +1，成功时 closed 态计数清零；`failure_count >= failure_threshold` 转 open 并记录打开时刻；open 态调用立即抛 `CircuitOpenError` 且**不调用**被保护函数；`now - opened_at >= recovery_timeout` 后首次调用转 half_open 并放行最多 half_open_max_calls 个试探；试探全部成功转 closed 并清零计数，任一失败立即回到 open 且重新计时；非 counted_exceptions 异常直接透传且不计失败不改变状态；failure_threshold<=0、recovery_timeout<=0、half_open_max_calls<=0 抛 ValueError
- 样例：threshold=2、recovery=5：两次失败 → state open；第 3 次调用抛 CircuitOpenError；假时钟推进 5.0 → 下次调用进入 half_open，成功 → closed；half_open 中失败 → 立即 open 且再次拒绝
- 类别：功能

### FP-07 多 key 门面与 LRU 淘汰 MultiKeyLimiter
- 描述：按 key（租户 id / 接口名）各自持有独立桶，总数量受控不无限增长
- 输入：MultiKeyLimiter(factory, max_keys=1024)；`check(key, tokens=1, now=None)` / `remaining(key)` / `keys()`
- 输出：判定布尔与余量；桶实例按需惰性创建
- 规则：命中已有 key 时更新其 LRU 顺序（最近使用在后）；新建导致数量超过 max_keys 时淘汰最久未使用的一个；`check` 的 tokens 非正抛 ValueError；key 必须是非空 str，空串/None/非字符串抛 ValueError；`keys()` 按 LRU 从旧到新返回；max_keys<=0 抛 ValueError
- 样例：max_keys=2，依次 check("a")、check("b")、check("a")、check("c") → keys()==["b","c"]（"a" 因最近被用过而幸存）；check("") 抛 ValueError
- 类别：功能

### FP-08 JSON 快照与恢复
- 描述：把限流状态落盘，重启后不丢窗口与令牌，避免流量突增
- 输入：`save(path)` / `load(path)`（MultiKeyLimiter 方法）
- 输出：文件为 JSON，结构 `{"version": 1, "buckets": [to_dict()...]}`；load 返回新实例所需状态
- 规则：文件不存在时 load 视为空（返回空 limiter，不报错）；原子写，正常与失败路径均不残留 .tmp；JSON 语法损坏、version != 1、桶记录缺 `algorithm` 键均抛 `StoreError`（RateLimitError 子类）且原文件字节不变；round-trip 后判定行为与保存前一致（含剩余令牌/窗口计数）
- 样例：check 若干次后 save、load 得新实例，再 check 的剩余量与原实例一致；手工截断 JSON → load 抛 StoreError 且文件字节前后一致
- 类别：功能

## 4. 异常与边界要求（重要）

- 全部构造参数非法（None/0/负数/字符串混入）必须抛 ValueError 子类，不得静默容错
- 边界：acquire 恰好耗尽全部令牌、elapsed 恰好等于一个完整窗口、时钟回拨（now 变小）、tokens 恰好等于当前 available、max_keys 恰好等于当前 key 数、熔断恰好在 recovery_timeout 边界恢复、half_open_max_calls 恰好等于并发试探数
- 并发：20 线程同时对同一 TokenBucket 各 acquire(1)，成功次数必须恰好等于初始令牌数（不超发）；10 线程同时 check 不同 key 时 keys() 数量不得超过 max_keys
- 确定性：注入假时钟时，同一场景重复执行产生逐条一致的判定序列

## 5. 验收标准

- 全部功能点实现且 `import ratelimit` 可用，包内无第三方依赖
- 功能 / 异常 / 边界三类用例全部通过（每个功能点每类至少 1 条，覆盖率 100%）
- 无阻塞级代码审查问题，全部缺陷闭环
- 待澄清问题在交付前完成协商闭环并写入技术方案

## 6. 待澄清问题

- Q1：half_open 状态下若多个试探并发返回，"任一失败立即回到 open"是否要把已经成功的试探计入恢复票数（建议：一旦失败立即 open 并作废其它在途试探结果）
- Q2：MultiKeyLimiter 的 LRU "使用"定义是"每次 check 都算"还是"仅发生扣减时才算"（建议：任何 check 都刷新 LRU，含被拒绝的请求）
