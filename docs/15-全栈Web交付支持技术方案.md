# 15. 前后端全栈交付支持技术方案

> 状态：方案（未实施）。所有"现状"均已在 2026-10-15 对照代码核实，标注 文件:行号。
> 原则沿用本项目一贯约束：最小改动、不放宽 gate 语义、每一步有可复跑的验收证据。

## 0. 结论摘要

| 阶段 | 交付能力 | 改动量 | 周期 |
|---|---|---|---|
| 方案 A（P0） | 同构全栈：Python 标准库 HTTP 服务 + 服务端渲染 HTML，前端功能点被真实验收 | 约 40 行代码 + 3 处配置/契约文本 + 1 个示例需求 | 1~2 周 |
| 方案 B（P1） | 真 SPA/JS 前端：Playwright(MCP) 采集 UI 证据，node 进白名单，gate 增加条件化 UI 断言 | 新增 1 个证据采集器 + gate/profile/契约扩展 | 3~6 周 |

两阶段完成后，流水线按**用户意图**支持三种交付形态：`library`（现状，纯逻辑库）/ `web_api`（单独后端）/ `web_fullstack`（前后端一起）。

## 1. 目标与非目标

**目标**
1. 用户输入"做一个带 Web 界面/带 HTTP 接口的系统"时，流水线能端到端交付并**用真实执行证据**通过验收，而不是 forced_fail 或需求缩水假通过。
2. 用户输入纯库需求时，行为与现状完全一致（豁免不外溢）。
3. 不破坏既有安全模型：shell 解释器仍硬拒（sandbox.py:287-288）、敏感环境变量仍剔除（sandbox.py:38-39）、F23 语言闸仍生效（tester.py:417-433）。

**非目标**
- 不做真实部署（docker/多服务/数据库运维）——那是独立的 C 档方案。
- 不引入第三方 Python 依赖（框架唯一硬依赖仍是 PyYAML）。
- 不改 8 条 acceptance criteria 的既有语义，只做**条件化追加**。

## 2. 现状阻力（已核实的代码事实）

| # | 阻力 | 位置 |
|---|---|---|
| R1 | 交付代码 import `http/socket/asyncio` 等即产出 `network_import` finding，severity 默认 `major` | sandbox.py:29-32, 542-555 |
| R2 | `major` ∈ `blocking_severities` → `review_report.blocking > 0` → gate 第 5 条不过 | config/roles/reviewer.yaml:61,65；offline.py:485-520；flow.yaml:92 |
| R3 | tester 执行契约写死"仅用标准库 + `[python, test_file]` 单命令"，无服务生命周期模板 | tester.py:118-143, 214-218 |
| R4 | 无浏览器/JS 证据通道：沙箱白名单只有 python 系（system.yaml:69-71），MCP 通道存在但双开关关闭（`tools_enabled: false` system.yaml:47；`servers: []` mcp.yaml:9） | sandbox.py:34-36, 134-138 |
| R5 | 非 Python 选型语言直接 gap=1 → 节点 fail（F23 闸），JS 交付同样被拒 | tester.py:425-433, 303-304 |
| R6 | 单次沙箱执行超时 180s（够用但需知晓），超时 exit_code=124 | system.yaml:67；sandbox.py:132, 237, 243-246 |

关键有利事实：
- requirement_spec 契约**已有** `constraints`/`tags` 字段，且 contracts 是纯 Prompt 契约、无运行时 schema 校验，可直接加新键（product.py:17-43, 57-80）。
- gate 表达式求值器支持 `or`/三元/`in`/字段缺失安全（返回 False + "字段缺失"，不抛异常）（expr.py:39-50, 170-188, 212-213）→ 条件化 criterion 无需改求值器。
- 新增 criterion 只需改 flow.yaml（+ profiles 同步，因其整表覆盖 criteria）（flow.yaml:81-98；profiles/strict.yaml:17-35；profiles/lean.yaml:5-15）。
- 白名单是配置项，追加 `node` 即可放行，且静态绕过检测与 tester 环境诊断复用同一判定（sandbox.py:134-138, 276-302, 619-620；tester.py:352-365）。
- MCP 最小客户端已实现 initialize/tools/list/tools/call，stdio/SSE 双传输（mcp.py:448-480, 227, 316）；toolrunner 能把 tools/list 转 OpenAI function schema 并真实回传（toolrunner.py:37-69, 84-105）。

## 3. 总体设计：delivery_mode 意图路由

**唯一的新概念**：`requirement_spec.delivery_mode ∈ {library, web_api, web_fullstack}`，默认 `library`。

- **判定来源（按优先级）**：
  1. 需求文档 `## 2. 技术约束` 段显式声明（示例需求文档提供三种模板措辞）；
  2. product 节点按契约从需求文本推断（contracts 增加一个键 + 一行判定说明，product.py:17-43）；
  3. 都没有 → `library`（现状行为，零回归风险）。
- **消费方**：reviewer（豁免网络 finding，A1）、tester（执行契约分支，A2/B2）、gate（条件化 UI criterion，B3）。
- 该字段随 requirement_spec 发布到黑板（product.py:74-78），所有下游节点用 `perception.found["requirement_spec"]` 读取，无新管道。

## 4. 方案 A（P0）：同构全栈

### A1 需求级网络豁免（唯一必须改代码的点）

**改动**：`mas/agents/reviewer.py` 调 `sandbox.analyze()` 前（现 :43 传 `self.role.policies`），按 delivery_mode 动态覆盖 severity_map：

```python
policies = dict(self.role.policies or {})
spec = (self.perception().found.get("requirement_spec") or {}).data or {}
if spec.get("delivery_mode") in ("web_api", "web_fullstack"):
    smap = dict(policies.get("severity_map") or {})
    smap["network_import"] = "info"        # 查表点 sandbox.py:445-446，无需改 sandbox
    policies["severity_map"] = smap
```

- 约 6 行；`info` 不在 `blocking_severities: ["critical","major"]`（reviewer.yaml:65）→ blocking 不计数，LLM 路径与 offline 路径同时生效（两条路径都吃这份 policies：reviewer.py:43→sandbox.py:391；offline.py:485）。
- **豁免范围刻意收窄**：只降 `network_import` 一条规则；`eval_usage`(critical)、`sandbox_command_bypass`(major) 等不动。
- **不外溢保证**：library 任务 policies 原样，network_import 仍 major → 回归用例见 A4。

### A2 tester Web 执行契约（改契约文本，不改执行代码）

执行命令仍是 `[python, test_file]`（tester.py:214-218 不动），在 `execution_protocol`（tester.py:118-143）与 `_REFERENCE_RUNNER`（tester.py:20-101）中**追加一段服务生命周期模板**，delivery_mode 为 web_* 时注入：

```python
# 同进程起服务：端口 0 由 OS 分配，避免并行 run 冲突
import threading, http.client
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), app_handler)   # app_handler 来自被测包
t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
try:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    ...  # 断言
finally:
    srv.shutdown(); srv.server_close()
```

- 全部标准库，180s 超时（R6）内轻松完成；测试进程退出即服务销毁，无长驻残留。
- 测试文件自身也会 import http/socket 并被静态扫描——A1 的豁免按 run 生效，测试文件同被覆盖（analyze 对整个 code 目录跑，同一份 policies）。

### A3 SSR 前端：FP → 响应体断言

- 需求文档模板约定：UI 功能点（category=功能，tags 含 `ui`）的 `samples` 写成"请求 → 期望响应体片段"，例：`GET /cart → 200 且 body 含 "购物车: 2 件"`。
- tester 契约追加一句：`ui` 类 FP 的用例 = 对 HTTP 响应体做字符串/DOM-lite 断言（`assert "购物车: 2 件" in resp`）。
- 这样前端 FP 进入既有 coverage 统计（functional/exception/boundary rate），**8 条 gate 零改动**即真实可满足——这是方案 A 的核心取巧点：不是绕过覆盖率门禁，而是把 UI 行为变成可断言的字节。
- 产物含 `.html` 模板文件：code_manifest 已统计非 Python 文本文件（sandbox.py:182-211，task04 修复过），无需改。

### A4 回归与验收（方案 A 的"完成"定义）

1. **单元回归**：`python -m unittest discover -s tests`（现 141 用例）全过；为 A1 新增 2 个用例：web_fullstack 标签下 network_import severity==info；library 标签下仍 ==major。
2. **端到端正例**：新增 `examples/requirements/web_todo_app.md`（骨架照抄 eshop_service.md:1-22，技术约束段声明 `delivery_mode: web_fullstack`、SSR、仅标准库），`python -m mas run -r examples/requirements/web_todo_app.md --profile lean` → 期望 verdict=pass，且 `test_runs/iterNN/stdout.txt` 里有真实 HTTP 请求断言输出。
3. **端到端反例（防豁免外溢）**：重跑 `examples/requirements/eshop_service.md` 一次，确认 network_import 仍按 major 拦截（可在 developer 代码里故意 import requests 验证被 blocking）。
4. 证据归档：run 目录 + `python -m mas report show <run-id>` 输出存入 docs/。

**工作量**：A1 半天；A2+A3 契约文本与示例需求 2~3 天；A4 验证受 LLM 延迟影响预留 2~3 天（历史经验：单节点可达数十分钟，勿以时长判断死活，看 events.jsonl 增长）。

## 5. 方案 B（P1）：真 SPA/JS 前端

### B1 执行通道

- `config/system.yaml:71` allowed 追加 `"node"`（零代码，R4 解除一半）；`g++` 等仍不加，维持 F23 边界。
- **环境探测（新增，先于一切 UI 验证）**：run 启动时探测 `node --version`、Playwright MCP server 可启动性，写入 artifact `env_probe`；缺失 → 直接归"环境或构建问题"并 halt，**不允许 developer 在回滚压力下改写需求刷绿**（F23 教训的机制化，docs/11）。

### B2 UI 证据采集器（新组件，不走 LLM function-calling）

- 明确取舍：**不用** `tools_enabled` 通道（system.yaml:47 默认关，且与 `response_format(json_object)` 互斥、`tool_max_rounds: 2`，不可控）——UI 验证必须是确定性步骤，不是 LLM 的自由工具。
- 新增 `mas/uiprober.py`：tester 节点在沙箱测试通过后追加一步（仅 delivery_mode==web_fullstack）：
  1. 子进程启动被测服务（白名单内 `python app.py`，端口 0 → 从 stdout 约定行读端口）；
  2. 经 `mas/mcp.py` 的 MCP 客户端（stdio JSON-RPC，mcp.py:227, 448-480）连接 Playwright MCP server（`config/mcp.yaml` 注册，`servers: []` → 加一条）；
  3. 执行需求声明的 UI 检查清单（navigate/click/screenshot/console_errors/DOM 文本断言），每项产出 `{name, passed, evidence}`；
  4. 关停服务与浏览器，写 artifact `ui_evidence = {failed_asserts: N, checks: [...], screenshots: [...]}`；
  5. 超时独立配置（新增 `runtime.ui_probe_timeout`，默认 300s），失败不静默——采集器异常 = 节点 fail，绝不产出"空证据的 pass"。

### B3 gate 条件化扩展

- flow.yaml acceptance criteria 追加第 9 条（同步 strict.yaml/lean.yaml，因整表覆盖）：
  `expr: "requirement_spec.delivery_mode != 'web_fullstack' or ui_evidence.failed_asserts == 0"`
- 依赖 expr.py 已支持的 `or` 与字段缺失安全（expr.py:170-174, 212-213）：library/web_api 任务左支为 True 直接过，**无 UI 证据字段也不会误伤**。
- 既有 8 条一字不改。

### B4 语言闸放宽（精确、不拆 F23）

- tester.py:425-433 的 `executable_languages` 判定扩展为：`tech_choice.language` 为 `python`、`python+javascript`（枚举白名单）时可执行；出现 `c++` 等白名单外语种维持 gap=1 → fail。JS 的可执行性以 B1 的 env_probe（node 存在）为前提，probe 缺失时 `python+javascript` 同样判 gap。

### B5 验收（方案 B 的"完成"定义）

1. 示例需求 `examples/requirements/spa_todo.md`：vanilla JS 单页 + Python 标准库 JSON API（不引入 npm 构建链，静态 JS 由服务端托管——把 B 档的不确定性压到最小）；
2. lean profile 跑通 verdict=pass，`ui_evidence.checks` 里每条 DOM 断言有截图文件落盘（`workspace/runs/<id>/evidence/`）；
3. 拔掉 node（改 PATH 模拟）重跑 → 期望 halt 于 env_probe，归类"环境或构建问题"，**不允许**降级成 library 刷绿；
4. 回归：A4 的三个端到端用例 + 141 单测全过。

**工作量**：B1+B3+B4 约 3 天；B2 采集器 1~2 周（MCP 客户端联调是主要不确定性）；B5 验证 1 周。合计 3~6 周（单人）。

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| A1 豁免被滥用到 library 任务 | 豁免只在 delivery_mode 命中时生效；A4 反例回归固化 |
| reviewer LLM 路径不受 policies 约束、自行数 blocking | reviewer 契约（reviewer.py:26）已声明 severity 语义；在契约文本中同步"info 不计 blocking"；用 offline provider 先验证确定性路径 |
| Playwright MCP server 与最小客户端（只实现 3 个方法）不兼容 | **未实测**，列为 B 阶段第一个 spike：先用 `mcp check`（cli 已有 `mas mcp add/check`）验证握手，失败则退化为直接子进程驱动 playwright 的 CLI（仍在白名单机制内加一条） |
| 180s 沙箱超时不够 UI 检查 | UI 探测走独立 `ui_probe_timeout`，不占用沙箱执行预算 |
| 每步可独立回滚 | A1/A2/A3/B3/B4 各自是独立小 diff；gate 表达式在 config 加载期校验（config.py:696-703），写错启动即报错，不会带病运行 |

## 7. 实施顺序（里程碑）

1. **M1（A 档全量）**：A1 → A2/A3 → A4 三例验证 → docs 归档证据。产出：web_todo_app pass + eshop 回归 pass + 豁免反例拦截。
2. **M2（B 档 spike）**：Playwright MCP 握手验证 + env_probe 落地。spike 失败则按风险表退化路线重排。
3. **M3（B 档全量）**：B2 → B3/B4 → B5 四例验证。

## 8. 明确未验证的假设（诚实清单）

- Playwright MCP server 能被 mas/mcp.py 的最小客户端驱动（只做了 initialize/tools/list/tools/call 三方法）——未实测，M2 首项验证。
- 本机 node 可用性：环境显示 Node v24.18.0 存在，但未在沙箱白名单语境下实测 `node --version` 通过 `_check_command`（匹配规则 sandbox.py:276-302 推演可行）。
- reviewer LLM 路径对"info 不计 blocking"的服从度依赖提示词，需用真实 run 验证（offline 路径是确定性的，可先行）。
- 校园网 LLM 端点延迟（历史单节点最长 68 分钟）会拉长所有端到端验证的日历时间，不影响方案正确性。
