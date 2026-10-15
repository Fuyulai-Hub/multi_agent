# 待办清单 Web 应用（todoweb）需求文档

## 1. 背景与目标

提供一个纯 Python 标准库实现的待办清单 Web 应用：浏览器访问即可添加、完成、删除待办任务，页面为服务端渲染（SSR）HTML，数据 JSON 持久化。本需求用于验证流水线对「前后端一起交付」形态的端到端闭环能力（docs/15 方案 A）：后端 HTTP 服务与前端页面在同一交付物内，全部功能点（含页面）必须能被自动化测试以真实 HTTP 请求断言。

## 2. 技术约束

- 语言：Python 3.9+，纯标准库实现，零第三方依赖
- **delivery_shape: full_stack_ssr**（后端 HTTP 服务 + 服务端渲染 HTML 页面一起交付，即 docs/15 的 A 档）
- 交付形态：可通过 `import todoweb` 使用的包，且必须公开 `build_handler()` 工厂函数，返回 `http.server.BaseHTTPRequestHandler` 子类；服务由调用方以 `ThreadingHTTPServer(("127.0.0.1", 0), build_handler())` 方式启动（测试同进程起停，端口 0 由 OS 分配）
- 页面一律服务端渲染：HTML 由 Python 字符串模板生成，禁止要求浏览器执行 JS 才能呈现核心内容
- HTTP 层仅用标准库 `http.server` / `http.client`；表单用 `application/x-www-form-urlencoded`
- 领域异常统一抛 ValueError 或其子类；HTTP 层把领域异常映射为 4xx 状态码 + 可读错误页，禁止裸 500
- 持久化用 JSON 原子写（同目录临时文件 + os.replace），数据文件路径可通过 `build_handler(data_path=...)` 注入

## 3. 功能点

### FP-01 任务存储内核 TodoStore
- 描述：任务的增查改删，内存态唯一事实源
- 输入：add_task(title) / list_tasks() / complete_task(task_id) / delete_task(task_id)
- 输出：任务 dict（id/title/done/created_at）或列表
- 规则：title 去首尾空白后非空且 ≤ 100 字符（否则抛 ValueError）；id 形如 "T-XXXXXXXX"（8 位十六进制，进程内唯一）；list_tasks 按创建顺序返回；complete_task 幂等（已完成再调用不报错，done 保持 True）；complete/delete 不存在的 id 抛 KeyError
- 样例：add_task("买牛奶") 返回 id 以 "T-" 开头；add_task("   ") 抛 ValueError；add_task("x"*101) 抛 ValueError；complete 后 list_tasks 中该任务 done == True
- 类别：功能

### FP-02 路由与处理器工厂 build_handler
- 描述：把 URL 路由到处理逻辑，返回可供 ThreadingHTTPServer 使用的 Handler 类
- 输入：build_handler(data_path=None) -> Handler 类；路由：GET / 、GET /tasks 、POST /tasks 、POST /tasks/{id}/done 、POST /tasks/{id}/delete
- 输出：Handler 类（BaseHTTPRequestHandler 子类）
- 规则：未匹配路径返回 404；GET 请求发到 POST-only 路径返回 405；响应头必须含 Content-Type（HTML 为 text/html; charset=utf-8）与 Content-Length
- 样例：GET /nope → 404；GET /tasks → 405；POST /tasks → 302（重定向回 /）
- 类别：功能

### FP-03 任务列表页（SSR）
- 描述：GET / 返回服务端渲染的完整 HTML 页面
- 输入：GET /
- 输出：200 + text/html 页面
- 规则：页面必须含标题文本「待办清单」；每条任务渲染为一个 `<li>`，含任务标题文本；已完成任务带 `class="done"` 标记；空列表时页面含「暂无任务」；新增任务后再次 GET / 能看到该任务标题（数据经 FP-07 持久化，跨请求可见）
- 样例：GET / → 200 且 body 含 "待办清单"；add_task("买牛奶") 后 GET / → body 含 "<li" 且含 "买牛奶"；空库 GET / → body 含 "暂无任务"
- 类别：功能

### FP-04 添加任务接口
- 描述：POST /tasks 以表单提交新任务
- 输入：POST /tasks，body 为 title=...（urlencoded）
- 输出：302 重定向到 /（Location: /）
- 规则：title 校验复用 FP-01（非法 title 返回 400 + 错误页，body 含「标题不能为空」或「标题过长」）；成功添加后 GET / 页面含该标题；重复提交同 title 生成两条任务（不去重）
- 样例：POST title=写周报 → 302；随后 GET / → body 含 "写周报"；POST title=（空）→ 400 且 body 含 "标题不能为空"
- 类别：功能

### FP-05 完成与删除任务接口
- 描述：POST /tasks/{id}/done 标记完成；POST /tasks/{id}/delete 删除
- 输入：POST /tasks/T-XXXXXXXX/done 、POST /tasks/T-XXXXXXXX/delete
- 输出：302 重定向到 /
- 规则：done 后列表页该任务 `<li>` 带 class="done"；delete 后列表页不再含该标题；不存在的 id 返回 404 + 错误页（body 含「任务不存在」）
- 样例：done 后 GET / → body 含 'class="done"'；delete 后 GET / → body 不含该标题；POST /tasks/T-00000000/done → 404 且 body 含 "任务不存在"
- 类别：功能

### FP-06 错误处理与状态码
- 描述：异常路径统一映射为 4xx + 可读 HTML 错误页
- 输入：各类非法请求
- 输出：400 / 404 / 405 状态码 + 含中文原因的错误页
- 规则：任何领域异常不得导致连接中断或 500；错误页含状态码数字与原因文本；Content-Type 仍为 text/html
- 样例：GET /nope → 404 且 body 含 "404"；GET /tasks → 405 且 body 含 "405"；POST title 超 100 字符 → 400 且 body 含 "标题过长"
- 类别：异常

### FP-07 JSON 持久化
- 描述：任务状态落盘，进程重启后恢复
- 输入：build_handler(data_path="todos.json")；每次写操作后自动落盘
- 输出：data_path 处的 JSON 文件
- 规则：原子写不残留 .tmp；文件缺失视为空库不报错；JSON 损坏抛 ValueError 且原文件不动；恢复后任务 id/done/顺序与内存态一致
- 样例：add 2 条 + done 1 条后重建 Handler（同 data_path）→ list 仍 2 条且 done 状态保持；写坏 JSON 后启动 → ValueError
- 类别：功能

## 4. 异常与边界要求（重要）

- 边界：title 长度 0/1/100/101 字符、首尾空白、含 HTML 特殊字符（`<script>` 等必须转义输出，页面不得出现原样 `<script>` 注入）、任务数 0/1/50、重复 done、删除后再 done（404）
- 异常：全部非法请求返回对应 4xx，禁止裸 500 与连接中断
- XSS：任务标题渲染进 HTML 前必须经 html.escape
- 并发：单测串行即可，不要求并发正确性，但 Handler 必须可在 ThreadingHTTPServer 下工作（不得依赖单线程假设）

## 5. 验收标准

- 全部功能点实现且 `import todoweb`、`todoweb.build_handler()` 可用
- 功能 / 异常 / 边界三类用例全部通过（每个功能点每类至少 1 条，覆盖率 100%）
- 页面类用例（FP-03/04/05）必须以真实 HTTP 请求 + 响应体断言验证，不得只测内核函数
- 无阻塞级代码审查问题，全部缺陷闭环；待澄清问题协商闭环

## 6. 待澄清问题

- Q1：done 任务是否需要支持「取消完成」（toggle）未明确，请研发与测试协商统一（建议：本期不做，done 为终态，delete 是唯一移除手段）
- Q2：列表页是否需要过滤参数（只看未完成）未明确，请协商统一（建议：本期不做）
