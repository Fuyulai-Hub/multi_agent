# 简易电商系统（eshop）需求文档

## 1. 背景与目标

为一个小型电商场景提供纯 Python 标准库实现的领域内核：管理商品目录与库存，支持购物车、优惠券、下单、支付、取消与退款的全链路，所有状态变更可持久化、可重放、可测试。重点保证：金额计算零精度误差、库存不超卖、支付幂等、订单状态机合法。

## 2. 技术约束

- 语言：Python 3.9+，交付形态为可通过 `import eshop` 使用的库
- 纯标准库实现，零第三方依赖；金额计算使用 Decimal（两位小数，ROUND_HALF_UP）
- 领域异常统一抛出 ValueError 或其子类（如 InsufficientStockError / PaymentError），禁止静默吞错
- 所有对外接口对非法输入显式报错；持久化用 JSON 原子写（同目录临时文件 + os.replace）

## 3. 功能点

### FP-01 商品目录 Catalog
- 描述：维护商品信息（id/名称/单价/分类），支持增查
- 输入：add_product(id, name, price, category="default") / get_product(id) / list_products()
- 输出：商品 dict 或列表
- 规则：id 非空 ASCII 且唯一（重复抛 ValueError）；price 必须 > 0（非法抛 ValueError）；name 非空；get_product 不存在抛 KeyError；list_products 按 id 升序
- 样例：add_product("p1", "笔记本", "5999.00") 后 get_product("p1")["price"] == Decimal("5999.00")；add_product("p1", ...) 重复抛 ValueError；add_product("p2", "x", "-1") 抛 ValueError
- 类别：功能

### FP-02 购物车 Cart
- 描述：按商品累计数量，实时计算金额小计
- 输入：add_item(product_id, quantity) / remove_item(product_id) / set_quantity(product_id, quantity) / subtotal()
- 输出：小计 Decimal（两位小数）
- 规则：quantity 必须为正整数（0/负数/非整数抛 ValueError）；商品不存在抛 KeyError；remove_item 不存在抛 KeyError；subtotal = Σ 单价×数量，不受折扣影响；同一商品多次 add 数量累加
- 样例：p1 单价 10.00，add 2 次 quantity=3 → subtotal 60.00；set_quantity(p1, 0) 抛 ValueError
- 类别：功能

### FP-03 库存 Inventory
- 描述：商品库存的预留与释放，防止超卖
- 输入：set_stock(product_id, quantity) / available(product_id) / reserve(product_id, quantity) / release(product_id, quantity)
- 输出：剩余可用数量 int
- 规则：set_stock 数量必须 ≥ 0 整数（非法抛 ValueError）；reserve 数量必须为正整数；库存不足抛 InsufficientStockError（ValueError 子类）且不产生任何部分预留；release 不能超过已预留量（抛 ValueError）；未设置库存的商品 available 返回 0
- 样例：set_stock("p1", 5) → available 5；reserve("p1", 3) → available 2；reserve("p1", 3) 抛 InsufficientStockError；release("p1", 10) 抛 ValueError
- 类别：功能

### FP-04 优惠券 Coupon
- 描述：校验优惠券并计算抵扣金额
- 输入：validate_coupon(code) / discount_amount(code, subtotal)
- 输出：抵扣金额 Decimal（两位小数，≥ 0）
- 规则：合法券码 = 两位大写字母 + 四位数字（如 "AB1234"），否则 validate 返回 False；抵扣规则：SAVE10 九折、SAVE20 八折、OFF50 满 200 减 50（不满 200 不减）；subtotal 为负抛 ValueError；非法券码 discount_amount 抛 ValueError；抵扣后不为负（折扣下限 0）
- 样例：validate_coupon("AB1234") → True；validate_coupon("ab1234") → False；discount_amount("SAVE10", "100.00") → 10.00；discount_amount("OFF50", "100.00") → 0.00；discount_amount("OFF50", "200.00") → 50.00
- 类别：功能

### FP-05 下单 Order
- 描述：从购物车生成订单，扣减库存，进入待支付状态
- 输入：create_order(cart, coupon_code=None) -> Order
- 输出：Order（id/items/subtotal/discount/total/status/coupon/created_at）
- 规则：购物车为空抛 ValueError；total = subtotal - discount，下限 0；下单成功后库存已预留（reserve）；订单状态初始 "pending"；订单 id 形如 "ORD-XXXXXXXX"（8 位十六进制，随机生成即可，但同一进程内唯一）；重复下单得到不同 id；coupon 非法抛 ValueError
- 样例：cart subtotal 100.00 + SAVE10 → discount 10.00、total 90.00；空 cart 抛 ValueError
- 类别：功能

### FP-06 支付 Payment
- 描述：订单支付，幂等控制
- 输入：pay(order_id, amount) -> Payment（order_id/amount/paid_at/status）
- 规则：订单不存在抛 KeyError；订单已支付过 → 同一订单再次 pay 且金额一致时返回首次支付结果（幂等，不重复扣款）；金额不一致抛 PaymentError（ValueError 子类）；金额为负或与订单 total 不一致抛 PaymentError；支付成功订单状态转 "paid"；已取消订单支付抛 PaymentError
- 样例：pay(oid, 90.00) 成功 → status "paid"；再 pay(oid, 90.00) 返回同一 Payment（幂等）；pay(oid, 80.00) 抛 PaymentError；pay(oid, -1) 抛 PaymentError
- 类别：功能

### FP-07 取消与退款 Cancel/Refund
- 描述：取消订单并释放库存；已支付订单取消即退款
- 输入：cancel_order(order_id) -> Order；refund(order_id) -> Refund（order_id/amount/refunded_at）
- 规则：只有 "pending"/"paid" 状态可取消（其他状态抛 ValueError，状态机非法流转）；取消后状态转 "cancelled" 并释放全部预留库存；已支付订单取消自动退款（refund 金额 = 订单 total）；重复取消抛 ValueError；refund 只能对 "paid" 订单且未退过（重复退抛 ValueError）；退款后状态转 "refunded" 且不可再支付
- 样例：pending 订单 cancel → "cancelled" 且库存恢复；paid 订单 cancel → "cancelled" 且可查到对应退款；refund 一个 pending 订单抛 ValueError；重复 refund 抛 ValueError
- 类别：功能

### FP-08 JSON 持久化 Store
- 描述：目录/库存/订单/支付/退款全量状态落盘，重启恢复
- 输入：save(path) / load(path) -> Shop
- 输出：恢复后的 Shop 实例
- 规则：文件缺失视为空状态不报错；原子写不残留 .tmp；JSON 损坏（语法错误）抛 StoreError 且原文件不动；金额序列化为字符串保留精度；恢复后订单状态机与内存态完全一致
- 样例：save 后同目录无 .tmp；写坏 JSON → load 抛 StoreError 且文件字节不变；删除文件 → load 得空 Shop
- 类别：功能

## 4. 异常与边界要求（重要）

- 全部非法输入显式抛错（ValueError / KeyError / 领域子类），禁止静默返回
- 边界：数量为 0/1/超大（1e9）、金额为 0.00/0.01/1e12、库存恰好够/差 1、折扣恰好到 0、券码边界（大小写、位数）、重复支付/重复退款/重复取消、取消已发货订单
- 幂等：同一订单重复支付（同金额）不产生第二笔扣款记录
- 精度：任何金额路径不得出现浮点误差（一律 Decimal）

## 5. 验收标准

- 全部功能点实现且 `import eshop` 可用
- 功能 / 异常 / 边界三类用例全部通过（每个功能点每类至少 1 条，覆盖率 100%）
- 无阻塞级代码审查问题，全部缺陷闭环
- 待澄清问题在交付前完成协商闭环并写入技术方案

## 6. 待澄清问题

- Q1：cancel 已支付订单时「先取消再退款」与「退款即取消」的状态口径未明确，请研发与测试协商统一（建议：取消动作触发自动退款，退款记录可单独查询）
- Q2：订单 id 的生成方式（随机/自增）未明确，请协商统一（建议：随机 8 位十六进制，进程内唯一）
