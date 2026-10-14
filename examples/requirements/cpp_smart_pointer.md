# C++ 智能指针库（smartptr）需求文档 —— 能力边界探针

## 1. 背景与目标

用 C++17 实现一个教学级的智能指针库：独占所有权的 `UniquePtr<T>` 与引用计数共享所有权的 `SharedPtr<T>`，并提供 `MakeUnique` / `MakeShared` 工厂函数。目标是验证「非 Python 交付物」在本流水线中的可完成性：全部逻辑在头文件里，用 `assert` 编写的独立测试程序需在真实编译器下编译并运行。

## 2. 技术约束

- 语言：C++17，交付形态为 header-only 库 `include/smartptr.hpp`
- 构建：`g++ -std=c++17 -Wall -Wextra -pedantic` 编译，零第三方依赖
- 测试：`tests/test_smartptr.cpp` 提供 `main()`，用 `<cassert>` 断言，全部通过时打印 `ALL PASS` 并以退出码 0 结束；任一断言失败以非 0 退出
- 语义要求：禁止内存泄漏（可用 ASan 验证），禁止 double free，移动后源对象必须处于空状态

## 3. 功能点

### FP-01 UniquePtr 构造与解引用
- 描述：独占堆对象所有权，行为接近裸指针的 RAII 包装
- 输入：`UniquePtr<T>(T* p = nullptr)`，显式构造禁止隐式转换
- 输出：`operator*` / `operator->` / `get()` / `explicit operator bool()`
- 规则：析构时 `delete` 持有指针并置空；空对象解引用为未定义行为但 `get()` 必须返回 nullptr；`static_assert` 保证 `UniquePtr` 不可拷贝构造、不可拷贝赋值
- 样例：`UniquePtr<int> q(new int(7)); assert(*q == 7 && q.get() == ...); assert(bool(q));`
- 类别：功能

### FP-02 UniquePtr 移动语义
- 描述：所有权只能转移，不能共享
- 输入：移动构造 `UniquePtr(UniquePtr&&)`，移动赋值 `operator=(UniquePtr&&)`
- 输出：转移后源对象 `get() == nullptr`
- 规则：自移动赋值（`a = std::move(a)`）不得释放对象也不得泄漏；移动赋值前必须先释放自身旧资源；移动后源对象析构不得二次释放
- 样例：`UniquePtr<int> b = std::move(a); assert(a.get() == nullptr && *b == 7);`
- 类别：功能

### FP-03 UniquePtr 的 reset / release
- 描述：显式换出与放弃所有权
- 输入：`reset(T* p = nullptr)` / `release()`
- 输出：release 返回原裸指针并放弃所有权
- 规则：`reset(自身 get())` 不得先 delete 再用已释放内存（自别名安全）；release 后析构不 delete；`reset(nullptr)` 等价清空
- 样例：`int* raw = q.release(); assert(q.get() == nullptr); delete raw;`
- 类别：功能

### FP-04 SharedPtr 引用计数
- 描述：共享所有权，最后一个持有者销毁对象
- 输入：`SharedPtr<T>(T* p = nullptr)`，计数控制块用 `unsigned long`
- 输出：`use_count()` / `operator*` / `operator->` / `get()` / `explicit operator bool()`
- 规则：拷贝构造/拷贝赋值计数 +1；析构计数 -1，计数归零时 delete 对象并释放控制块；拷贝赋值需先递增新计数再递减旧计数（自赋值 `p = p` 安全）；空 SharedPtr 的 `use_count()` 返回 0
- 样例：`SharedPtr<int> q(p); assert(p.use_count()==2); { SharedPtr<int> r(p); assert(r.use_count()==3); } assert(p.use_count()==2);`
- 类别：功能

### FP-05 SharedPtr 移动与 make_unique / make_shared
- 描述：移动不改变计数；工厂函数一次分配
- 输入：`SharedPtr(SharedPtr&&)`、`MakeUnique<T>(Args&&...)`、`MakeShared<T>(Args&&...)`
- 输出：构造好的智能指针
- 规则：移动后源对象 `use_count()==0` 且 `get()==nullptr`，目标对象计数保持 1；`MakeShared` 对同一对象重复调用得到彼此独立计数（不做去重）；工厂函数需完美转发构造参数
- 样例：`auto s = MakeShared<int>(42); assert(*s == 42 && s.use_count() == 1);`
- 类别：功能

### FP-06 循环引用与 WeakPtr（异常/边界要求）
- 描述：提供 `WeakPtr<T>` 打破循环引用
- 输入：`WeakPtr<T>(const SharedPtr<T>&)`，`lock()` / `expired()`
- 输出：`lock()` 返回 SharedPtr（对象已销毁时返回空 SharedPtr）
- 规则：WeakPtr 不增加强引用计数；对象析构后 `expired()` 为 true 且 `lock()` 返回空对象而非崩溃；控制块需在强引用归零但弱引用仍存在时保留，弱引用也归零后释放控制块
- 样例：`{ WeakPtr<int> w = p; assert(!w.expired()); auto s = w.lock(); assert(s && *s == 42); } assert(w.expired());`
- 类别：边界

## 4. 异常与边界要求（重要）

- 空指针构造、`reset(自身)`、自拷贝赋值、自移动赋值四类自别名场景不得 double free 或泄漏
- 强引用归零而弱引用仍在时，`lock()` 必须返回空且不解引用已释放内存
- 编译必须无 warning（`-Wall -Wextra -pedantic` 之下）

## 5. 验收标准

- `g++ -std=c++17 -Wall -Wextra -pedantic tests/test_smartptr.cpp -o test_smartptr` 编译成功、退出码 0、无 warning
- 运行 `./test_smartptr` 输出 `ALL PASS`、退出码 0
- 功能 / 异常 / 边界三类断言各覆盖全部 6 个功能点
- 无阻塞级代码审查问题，全部缺陷闭环
