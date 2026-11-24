# 52 · SPA 全栈执行协议：把「起服务」从测试文件搬到框架

> 承接：`docs/45`（task12 三次真实运行 0/10 验收）、`docs/46`（补齐环境后仍 6/33）、`docs/50/51`（编排层）
> 结论：**前后端分离（`full_stack_spa`）从「结构上不可能通过」变成「可被验收」**——冻结套件不再 import 交付包、也不再自己 spawn 服务，而是由框架托管的 `mas_testkit` 起停真实服务、打真实 HTTP。本机用真 Node 服务端到端跑通 3/3 冻结用例。

---

## 0. 三句话

1. 阻断点从来不是「模型写不出 NestJS/Vue」，而是**证据通道只认 Python 同进程**：契约要求 `from <pkg> import build_handler`，Node/TS 交付里没有这个包（`tester.py` 的 `_WEB_PROTOCOL`）。
2. 解法不是换测试语言，而是**换「谁起服务」**：套件仍是 Python unittest（冻结/签名/逐用例证据链全部复用），服务由框架安装的 `mas_testkit` 启动、就绪判定、回收。
3. 同一条命令服务三处证据：可见套件、隐藏套件、浏览器 UI 检查——三者验的必须是同一个进程，避免「测试通过但页面跑不起来」。

---

## 1. 旧链路的三个结构性阻断

| 阻断 | 实测证据 | 根因 |
|---|---|---|
| 套件 `import` 交付包 | task12 iter01–03：39 条用例 33 条失败 | A 档协议强制 `from <pkg> import build_handler`，Node/TS 交付没有该包 |
| 套件自己 `subprocess` 起服务 | 裸名 `npm` → `FileNotFoundError`；`stdout=PIPE` 不读 → 8656 字节启动日志后永久阻塞（60s 不监听，改指文件 3.0s 就绪） | Windows CreateProcess 不查 PATHEXT；匿名管道写满即阻塞 |
| 冻结后无法修 | 同一份坏套件 iter04/05/06 原样重跑，两轮零增益 | `restore_if_changed()` 每轮恢复冻结副本，修复窗口在冻结前 |

`docs/46` 已经补了静态规则（`windows_spawn_extension` / `spawn_unread_pipe`）让这类写法在**冻结前**被退回重写，但**没解决「套件本来就不该自己起服务」**——规则只能逼模型把 spawn 写对，不能给它一个不会写错的东西。

---

## 2. 新链路

| 环节 | 旧 | 新 |
|---|---|---|
| 服务命令来源 | 测试文件里硬编码（`["npm","run","start:dev"]`） | 契约字段 `ui_service_cmd` → 框架注入 `MAS_SERVICE_CMD` |
| 进程管理 | 测试文件自己 `Popen` | `mas_testkit.start_service()`（框架安装，模型改写无效） |
| 日志 | `PIPE` 或工作区文件 | 框架分配的 `run_dir/verify_io/iterNN/service.log`（模型工作区之外，事后可查） |
| 就绪判定 | 测试自己轮询端口/睡几秒 | 服务打印 `MAS_UI_PORT=<port>`，testkit 回读 |
| 端口 | 写死 3000（并行必冲突） | 服务绑 0，OS 分配 |
| 回收 | `proc.terminate()` 只杀父进程 | 进程组整树回收（Windows `taskkill /T /F`，POSIX `killpg`） |
| 失败信息 | `spawn failed` / 空 stderr | `ServiceError` 带退出码 + 日志尾部 |

三条硬约束写进协议（`_SPA_PROTOCOL`）：**禁止 import 交付包**、**禁止自建进程**、**端口不得写死**。

---

## 3. 代码改动

| 文件 | 动作 | 量级 |
|---|---|---|
| `mas/verify/testkit.py` | 新增：`start_service()` / `Service`（HTTP 客户端、日志、回收）/ `ServiceError` | +305 |
| `mas/verify/exec.py` | 安装 testkit、PYTHONPATH 加 `.mas_verify`、注入 `MAS_SERVICE_CMD` / `MAS_SERVICE_LOG` / `MAS_SERVICE_ALLOWED`，可见与隐藏套件都支持 `service_cmd` | +54 / −7 |
| `mas/agents/tester.py` | SPA 走新协议（`_SPA_PROTOCOL`）、SSR 保持原协议；`_resolve_shape()` 提前定型供冻结前体检使用；`test_report.service` 记录契约；冻结轮次沿用上一轮命令 | +125 / −22 |
| `mas/sandbox.py` | 新增静态规则 `spa_service_self_managed`：SPA 套件出现 `subprocess`/`os.system`/`os.popen`/`multiprocessing` 即退回重写 | +52 |
| `mas/hidden.py` | 规则 6（起服务方式）按形态分叉，SPA 隐藏套件同样用 testkit | +22 / −3 |
| `mas/leader.py` | 门禁执行隐藏套件时从 `test_report.service.command` 取同一条命令 | +6（其余为 docs/50 的未提交改动） |
| `mas/uiprober.py` | 就绪后启动守护线程持续排空 stdout（否则 dev server 写满管道即阻塞，表现为「UI 检查超时」） | +18 |
| `tests/test_service_protocol.py` | 新增 18 个用例 | +430 |
| `tests/test_web_delivery.py` | 协议注入断言按新契约拆分（SSR 进程内 / SPA 托管） | +27 / −11 |

门禁、`expr.py`、`verifier.py`、经验库、编排层一行未动：**执行权与证据权仍在框架**，套件只定义用例。

---

## 4. 验证

### 4.1 新增用例（18 项，全绿）

| 组 | 覆盖 |
|---|---|
| testkit 本体（9） | 就绪与 HTTP 取证；**680KB 启动日志不死锁**（docs/46 的死锁复现）；超时带日志尾部；提前退出带退出码；**进程树回收**（孙进程心跳停写）；缺命令明确报错；白名单拒绝越权命令；`npm` 经 PATHEXT 解析成 `npm.CMD`；不存在的可执行文件报错 |
| 框架注入（3） | **真 Node 服务端到端**：冻结套件经 `execute_frozen_suite(service_cmd=["node","server.js"])` 跑出 3/3 通过，`.mas_verify/mas_testkit.py` 由框架安装、`verify_io/*/service.log` 留证；**隐藏套件同命令 2/2 通过**；不给命令时全部失败且信息指向 `MAS_SERVICE_CMD`（不静默跳过） |
| 协议提示（3） | SPA 协议含 `start_service` 且**不含** `build_handler`；SSR 协议保持不变；隐藏套件协议按形态分叉 |
| 静态规则（3） | SPA 套件 `import subprocess` → 命中 `spa_service_self_managed`；干净套件与非 `tests/` 文件不误报；`_preexec_diagnosis` 只在 SPA 形态挂该规则 |

### 4.2 全量回归

`python -m pytest tests/ -q` → **787 passed / 16 skipped**（改动前基线 769 / 16；新增 18 个用例）。
协议用例连跑 4 次稳定通过（无时序抖动）。

### 4.3 实施中发现的真实缺陷（已修）

隐藏套件的 runner 以**冻结目录**为工作目录（用例从那里加载），服务若按相对路径启动会解析到
`frozen_tests/server.js`：首版实现就报 `Cannot find module .../frozen_tests/server.js`，
两条隐藏用例整组 error。修法：框架注入 `MAS_SERVICE_CWD`（交付根目录），testkit 用它作为
默认工作目录——可见套件、隐藏套件、UI 检查三处的服务都从交付根启动。

---

## 5. 现在能走到哪一步（如实）

**已具备**：SPA 形态的「后端 REST 契约」可以被冻结套件黑盒验收；「前端页面」由既有的 UiProber + Playwright 验收，两者共用同一条启动命令。

**仍需满足的前置**（不是本次改动能消除的）：

1. **交付物必须打印 `MAS_UI_PORT=<port>`**：这是要求的启动契约，需求文档里必须写明（`_BROWSER_PROTOCOL` 已要求 `ui_service_cmd`）。
2. **一条命令起整套服务**：后端 + 前端（或后端托管构建产物）。`npm run start:dev` 这种「只起后端 + 另开 vite」的两进程形态无法被单命令托管。
3. **冷启动计入沙箱超时**（默认 180s）：NestJS + Vite 首次冷启动可能接近上限，测试类必须共享一个服务实例。
4. **容器沙箱需自带 node**：`sandbox_backend=container` 时镜像默认 `python:3.12-slim` 没有 node，属既有边界。
5. **前端质量仍是弱证据**：`sandbox.analyze_web_assets` 只做标签/括号配平、资源存在性、占位符检查；没有 eslint/tsc。

---

## 6. 写需求时的最小契约

```markdown
## 技术栈
NestJS + Prisma(SQLite) 后端，Vue3 + Vite 前端；后端托管前端构建产物（同一端口）。

## 启动契约（框架依赖，必须遵守）
- 一条命令起整套服务：`node serve.js`
- 服务绑定 0 端口，监听成功后 stdout 打印一行 `MAS_UI_PORT=<实际端口>`
- 数据库初始化在启动命令内完成（迁移 + 种子），测试不允许另外起进程

## 接口清单
GET  /api/products?page=&q=  → 200 {items,total}
POST /api/orders {items:[{id,qty}]} → 201 {order_id,total} | 409 库存不足 | 422 参数错误
```

tester 会把 `ui_service_cmd` 原样作为 `MAS_SERVICE_CMD` 交给可见套件、隐藏套件与 UI 检查。
