# 短链接服务（前后端一起交付）需求文档

## 1. 背景与目标

slink 是一个内网短链接服务：用户把长 URL 提交给服务，服务分配一个短码，浏览器访问短码地址自动跳转到原始长 URL；同时提供一个管理页面，前后端都要在这个交付里完成，页面由服务端渲染（SSR），数据以 JSON 文件持久化。

本项目用于验证多 Agent 流水线能否端到端交付「后端接口 + 前端页面」同时存在的需求，因此**前端页面与后端接口都是必需交付物**，不接受只交付纯库或只交付接口的形态。

## 2. 硬性约束

- 仅使用 Python 标准库，禁止任何第三方依赖（Flask/FastAPI/requests 等一律不允许）。
- 不提供构建步骤，不使用子进程调用外部编译器；运行方式必须是 `python -m slink` 或调用方直接构造 handler。
- HTTP 服务基于 `http.server.ThreadingHTTPServer`，监听地址由调用方传入，端口支持 0（自动分配）。
- 所有写操作需要持久化到单个 JSON 文件，进程重启后数据不丢。

## 3. 功能点

| 编号 | 功能点 | 说明 |
|---|---|---|
| FP-01 | 短码存储内核 | `Store` 类：`create(long_url, ttl=None) -> Link`、`lookup(code) -> Link`、`list() -> list[Link]`，短码 6 位、字符集 `a-z0-9`、冲突自动重试、同一长 URL 重复提交返回已存在短码 |
| FP-02 | 有效期判定 | Link 有 `created_at`、可选 `expires_at`；`lookup` 对过期链接返回未命中 |
| FP-03 | 创建接口 | `POST /api/links`，接受 JSON `{"url": "..."}` 也接受表单 `url=...`，返回 201 + JSON `{"code": "...", "short_url": "..."}` |
| FP-04 | 跳转 | `GET /<code>` 命中返回 302 + `Location` 指向长 URL；未命中返回 404 页面 |
| FP-05 | 列表 API | `GET /api/links` 返回 JSON 数组，含 code/url/created_at/hits，支持 `?q=` 按 url 子串过滤 |
| FP-06 | 管理页面（前端） | `GET /` 返回 HTML 页面：包含提交表单（input name=url + 提交按钮）、已生成短链接列表（每行显示短码、原始 URL、访问次数）、`<a>` 链接可点击跳转到短码地址；页面必须对用户输入做 HTML 转义，防 XSS |
| FP-07 | 删除 | `POST /api/links/<code>/delete` 删除后 `lookup` 未命中，页面列表不再出现该条 |
| FP-08 | 非法输入处理 | 非 http/https 协议、超长 URL（>2048 字符）、空 url、未知路径 404、不支持的方法 405，全部返回明确的 4xx，禁止裸 500 |

## 4. 验收标准

- AC-01 FP-01~FP-08 每个功能点都有代码实现位置（fp_mapping 完整）。
- AC-02 每条用例真实执行，通过率 1.0，未闭环缺陷为 0。
- AC-03 **页面类用例必须通过真实 HTTP 请求断言响应体**：至少有一条用例请求 `GET /` 并断言返回 200、`Content-Type` 含 `text/html`、正文含 `<form` 与提交字段名 `url`；至少有一条用例断言 `POST /api/links` 后再次 `GET /` 能看到新建短码。
- AC-04 XSS 用例：提交含 `<script>alert(1)</script>` 的 url 后，`GET /` 响应体中不得出现未转义的 `<script>alert(1)</script>`。
- AC-05 边界清单覆盖：重复 URL、过期链接、非法协议、超长 URL、空 body、并发创建。
- AC-06 任意异常路径不返回 500（用例外全部 4xx/2xx/3xx）。
- AC-07 持久化往返：写入后重新从 JSON 文件加载，短码与访问次数保持一致。

## 5. 交付形态声明

本需求交付形态为 `full_stack_ssr`：后端 HTTP 接口与前端页面同包交付，页面由服务端渲染。
