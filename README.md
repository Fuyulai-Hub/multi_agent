# 配置驱动的多 Agent 任务协作系统

Leader 统一调度的流水线架构，实现 **需求输入 → 技术方案设计 → 编码实现 → 代码审查 → 测试验收 → 验收报告** 的全流程自动化协作，并通过 **事件日志 + 分级经验库注入 Prompt** 构建自学习闭环。

## 角色

| 角色 | 职责 | 关键产物 |
| --- | --- | --- |
| **产品 Agent** | 需求评估：拆解功能点、验收标准、异常/边界要求、待澄清问题 | `requirement_spec` |
| **研发 Agent** | 技术选型 + 方案评估 + 编码实现 + 缺陷修复（含回归自测报告） | `solution_design` / `code_manifest` / `fix_test_report` |
| **Reviewer Agent** | 代码审查（真实 AST 静态分析 + 需求一致性）与验收签署 | `review_report` / `acceptance_opinion` |
| **测试 Agent** | 依据需求契约独立生成功能/异常/边界用例，沙箱真实执行，提缺陷单 | `test_report` / `bug_reports` |
| **报告 Agent** | 证据统计与验收报告（含自学习效果报告） | `acceptance_report` / `evidence_index` |
| **Leader** | 状态流转、门禁判定、回退/重试、Agent 间协商中介、经验闭环 | `acceptance_gate` / `run_summary` |

所有 Agent 通过 **共享黑板**（版本化产物）与 **消息总线**（广播/请求-应答）相互通信、感知彼此已完成的工作。

## 快速开始

```bash
pip install -r requirements.txt        # 仅需 PyYAML

# 方式一：一键演示（三轮：文档输入 → 经验复用 → 文字需求输入）
python -m mas demo

# 方式二：单次运行
python -m mas run --requirement examples/requirements/order_pricing_service.md

# 文字需求描述输入
python -m mas run --requirement "实现一个订单金额计算与校验服务库……" --text

# 切换流程 Profile（配置驱动：新增方案评审节点 / 裁剪审查节点）
python -m mas run --requirement examples/requirements/order_pricing_service.md --profile strict
python -m mas run --requirement examples/requirements/order_pricing_service.md --profile lean

# 需求附图输入（可重复；后缀限 png/jpg/jpeg/webp/gif，单张 ≤5MB）
python -m mas run --requirement examples/requirements/order_pricing_service.md --image docs/flow.png

# 只入队不执行（需 Redis；由独立 worker 进程消费，API 与运行生命周期解耦）
python -m mas run --requirement examples/requirements/order_pricing_service.md --enqueue
python -m mas worker                 # 另起终端：消费任务（心跳/ACK/PEL 认领/重试/死信）
python -m mas mq status              # 消息队列健康：运行流积压 / PEL / DLQ / worker
python -m mas store status           # MySQL/Redis 存储后端健康报告（未装 PyMySQL 时自动降级）

# 查看报告与经验库
python -m mas report show latest
python -m mas experience list
python -m mas config show --profile lean

# 技能（Skill）与 MCP 工具
python -m mas skill list
python -m mas skill check demo-delivery
python -m mas skill add --from-run <run_id>      # 把一次已通过验收的运行沉淀为技能
python -m mas mcp add demo --transport stdio --command npx --args "-y @x/server"
python -m mas mcp check demo

# 框架自测
python -m unittest discover -s tests -v

# 启动前端可视化面板（默认 http://127.0.0.1:8000）
python frontend/server.py

# 指定端口
python frontend/server.py --port 8080
```

> 前端服务需要在**项目根目录**下执行，且依赖 `workspace/` 已存在（先用 `python -m mas demo` 或
> `python -m mas run` 生成一次运行数据），否则会打印提示并以退出码 2 结束。
> 更多启动方式见下文 [前端面板](#前端面板)。

## 前端面板

内置可视化面板（仅 Python 标准库，无额外依赖）：运行列表与摘要指标、单次运行的事件流/验收报告/时间线、
分级经验库、跨轮效果指标，以及顶栏「⚙ 模型配置」入口。

### 启动命令

在**项目根目录**下执行：

```bash
# ① 默认端口 8000，浏览器打开 http://127.0.0.1:8000
python frontend/server.py

# ② 指定端口（端口被占用时换一个即可）
python frontend/server.py --port 8080

# ③ 指定端口 + 监听地址（0.0.0.0 开放局域网访问，默认仅本机）
#    非回环地址必须同时设置访问令牌，否则服务拒绝启动（失败关闭）
MAS_PANEL_TOKEN="换成一个随机长令牌" python frontend/server.py --host 0.0.0.0 --port 8080
#    浏览器首次打开 http://<主机>:8080/?token=<令牌>，换取 HttpOnly Cookie 后即可正常使用

# ④ 同时指定数据目录（默认 <项目根>/workspace）
python frontend/server.py --port 8080 --workspace workspace

# ⑤ 前后端分离：后端只提供 /api/*，页面由独立宿主提供
MAS_PANEL_TOKEN="换成一个随机长令牌" MAS_PANEL_ORIGINS="http://127.0.0.1:8001" \
  python frontend/server.py --port 8000 --no-static
python -m http.server 8001 --bind 127.0.0.1 --directory frontend
#    浏览器打开 http://127.0.0.1:8001/index.html?api=http://127.0.0.1:8000&token=<令牌>
```

> 分离模式下 `MAS_PANEL_ORIGINS` 必须显式列出前端宿主地址，否则跨源请求被 403 拒绝
> （失败关闭：默认只允许同源）。前端地址里的 `api=` 参数指定后端，不填则视为同源。

启动成功后终端会打印实际访问地址：

```text
前端面板：http://127.0.0.1:8080  （数据目录：G:\...\multi_agent\workspace）
```

浏览器打开该地址即可，停止服务按 `Ctrl + C`。

### 启动参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--port` | `8000` | HTTP 监听端口；被占用时改用其他空闲端口（如 `8080`、`9000`） |
| `--host` | `127.0.0.1` | 监听地址；`127.0.0.1` 仅本机可访问，改成 `0.0.0.0` 开放局域网访问（必须配令牌） |
| `--workspace` | `<项目根>/workspace` | 面板读取的运行数据目录，应与 `python -m mas run/demo` 的产出目录一致 |
| `--dist` | `frontend/` | 静态资源目录；页面迁出时指向新目录即可，接口路径不变 |
| `--no-static` | 关 | 只提供 `/api/*`，不托管页面（前后端分离部署） |
| `MAS_PANEL_TOKEN` | 空 | 面板访问令牌；非回环 `--host` 下为必填，空值会拒绝启动 |
| `MAS_PANEL_ORIGINS` | 空 | 额外允许的跨域来源，逗号分隔（默认只允许同源；分离部署必须配置） |

> 端口、监听地址与数据目录**只能通过命令行参数指定**；只有访问令牌与跨域白名单走环境变量。
> 面板没有对应的 `mas` CLI 子命令，必须直接运行 `frontend/server.py`。
> 若 `--workspace` 指向的目录不存在，服务打印「工作区不存在」提示并以退出码 2 退出，
> 先跑一次 `python -m mas demo` 生成数据即可。

### 访问控制与输入边界

- 默认绑定 `127.0.0.1`，`/api/*` 只接受同源请求；跨站 `POST` 会被 403 拒绝。
- 设置 `MAS_PANEL_TOKEN` 后，所有 `/api/*` 需要 `Authorization: Bearer <token>`、
  `X-Panel-Token: <token>`，或先访问 `/?token=<token>` 换取 `HttpOnly; SameSite=Strict` Cookie。
- 响应不再发送 `Access-Control-Allow-Origin: *`，只回显同源或 `MAS_PANEL_ORIGINS` 白名单来源。
- `GET /api/llm` 只返回 `api_key_set` 与掩码，**不回显明文 Key**；表单留空即沿用已保存值。
- `POST /api/run` 的 `requirement_path` 与 `images` 必须指向 `workspace/uploads/` 内已存在的文件；
  `profile` 只接受字母/数字/下划线/连字符；`run_id` 拒绝 `..` 与路径分隔符。

## 流程与状态机

```text
intake(产品) ─▶ solution(研发) ─▶ implement(研发) ─▶ test(测试) ─▶ review(评审) ─▶ acceptance(门禁) ─▶ report(报告)
                    ▲                │                  │    ▲            │              │
                    │                │                  │    └── 审查驳回 ┘              │
                    └── 测试失败/验收驳回 ──▶ optimize(研发: 修复+回归自测) ───────────────┘
                                             修复后重新测试 → 重新审查，直至门禁通过
```

- **先测试后审查**：审查必须拿到“冻结测试真实通过”的证据，才能核验功能点是否被测试 import/使用
- **审查不通过** → 退回研发修改（阻塞问题清零才能放行）；审查结论带 `code_hash`，门禁会与最终代码重算值比对，旧审查不能给新代码签字
- **测试失败** → 生成缺陷单（复现/期望/实际/堆栈）→ 退回研发修复 → 研发输出对应回归自测报告 → 测试复测闭环
- **验收不通过** → 退回上一步优化并重新执行（门禁项、次数、升级策略全部可配置）
- **回退次数耗尽** → 按升级策略 `fail_run_and_report` 出具“验收不通过”报告并标注原因

## 配置驱动（改配置即改流程）

```
config/
├── system.yaml            # 全局：LLM Provider、沙箱、运行时
├── flow.yaml              # 流程节点与流转（可增删节点、改门禁表达式）
├── collaboration.yaml     # 协作规则：感知矩阵、广播矩阵、协商、回退策略、升级策略
├── learning.yaml          # 自学习：三级经验库、打分、提炼触发、晋级、注入预算
├── roles/                 # 五个角色的职责/人设/准则/审查策略/经验层级
└── profiles/
    ├── strict.yaml        # 新增“技术方案评审”节点 + 更严门禁与回退上限
    └── lean.yaml          # 裁剪“代码审查”节点（skip: true 自动重接流转边）
```

- 门禁是**表达式驱动**的（AST 白名单安全求值），例如 `test_report.coverage.boundary.rate >= 1.0`；
- Profile 深合并覆盖任意片段，`skip: true` 的节点被裁剪后，指向它的流转边自动改接到其后继。

## 技能（Skill）与 MCP 扩展

能力扩展是**独立于交付流水线的能力层**：不占 `flow.yaml` 的节点，也不新增角色 Agent，
由 `mas/skills.py` / `mas/mcp.py` 两个确定性模块 + CLI 入口提供。

### 技能（Skill）

每个技能一个目录，`SKILL.md` 的 YAML front-matter 必填六项：

```
config/skills/<skill-id>/
├── SKILL.md        # 必需：name / description / when_to_use / trigger / inputs / steps
├── scripts/        # 可选：可执行脚本（冒烟命令以此为工作目录执行）
├── references/     # 可选
└── assets/         # 可选
```

| 命令 | 说明 |
| --- | --- |
| `skill list` / `skill show <id>` | 查看 |
| `skill add --yaml <file>` / `skill add --from-run <run_id>` | 手写添加 / 从成功运行沉淀 |
| `skill edit <id> --set k=v` / `skill remove <id>` | 修改 / 删除 |
| `skill check <id>` | 装载校验：解析 → 参数校验 → 冒烟 |

**沉淀不自动发生**：一次运行跑通后不会写入任何技能；只有在后续明确执行
`skill add --from-run <run_id>` 时才提炼落盘，且只保留最终通过路径，剔除被回退的迭代、
缺陷试错与绝对路径（替换为 `{{workspace}}` / `{{run_id}}` 等占位符）。

### MCP 工具

`config/mcp.yaml` 一个服务一条，支持 `stdio`（子进程 + 换行分隔 JSON-RPC）与
`sse`（远端事件流）两种传输，含服务名称、启动命令、参数、环境变量与超时。
`env` / `headers` 中的 `${VAR}` 在运行时从环境变量展开，避免明文落盘。

| 命令 | 说明 |
| --- | --- |
| `mcp add --name <n> --transport stdio\|sse ...` | 添加 |
| `mcp list` / `mcp show <name>` / `mcp remove <name>` | 查看 / 删除 |
| `mcp check <name> [--call <tool> --params '{...}']` | 参数校验 → 连接 → `initialize` 握手 → `tools/list` 冒烟 |

`check` 失败时按阶段给出原因与修复建议：`parse`（未配置）、`validate`（参数非法）、
`connect`（命令不存在 / 地址不可达）、`handshake`（握手失败）、`smoke`（工具调用失败）。
未声明 `smoke.command` 的技能只做静态校验，结果会明确标注「未做动态验证」。

## 自学习闭环

```text
事件日志(events.jsonl) ──规则化提炼──▶ 分级经验库 ──打分检索──▶ 分层注入 Prompt
        ▲                    L3 全局通用                       │
        │                    L2 项目/领域                       ▼
        └──────────── reinforce(hits/confidence) ◀── Provider/LLM 消费 action_key
```

- **L1**：本次运行即时经验（当轮审查驳回原因、缺陷模式），当轮即可注入后续节点；
- **L2 / L3**：按命中次数自动晋级（L1→L2→L3），跨运行复用；
- 注入全部留痕：`prompts/*.md` 记录每次注入的经验 ID、层级、token 预算与命中明细；
- 收益可测量：`workspace/reports/learning_report.md` 对比多轮运行的步数/回退/审查驳回/缺陷数。

离线 Provider 会消费结构化经验动作（`action_key`，如 `avoid_missing_function_docstring`、
`guard_calc_shipping__boundary`），使“经验注入”产生确定性的行为变化——第二轮运行
不再注入缺陷、不再缺文档字符串，步数从 12 降到 7。

## LLM 接入

| Provider | 说明 |
| --- | --- |
| `offline` | 内置确定性 Provider：解析需求 → 匹配算子 → 生成**真实可运行代码**与**真实可执行测试**，并按配置注入首轮缺陷以演示缺陷闭环。无 API Key 即可端到端跑通；未配置 `llm.provider` 时的代码默认值 |
| `openai_compatible`（当前 `config/system.yaml` 实配） | 标准 `/chat/completions` 接口，处理任意需求。**需要 API Key**：无 Key 直接报错，不会自动回退 `offline`；模型名填端点上真实存在的模型 |

### API Key 与 BASE_URL 的外注方式

凭据不写死在代码里，统一由三个通道注入，**后者覆盖前者**：

| 优先级 | 通道 | 用法 |
| --- | --- | --- |
| 1（低） | `config/credentials.yaml` | 前端面板填写后点「设为默认」自动写入；也可手工编辑。该文件已在 `.gitignore` 中，不会进版本库 |
| 2（中） | 环境变量 | 变量名由 `system.yaml` 的 `llm.api_key_env` / `llm.base_url_env` 指定，当前为 `MAS_LLM_API_KEY` / `MAS_LLM_BASE_URL`（未配置这两个键时回落 `OPENAI_API_KEY` / `OPENAI_BASE_URL`） |
| 3（高） | 运行时注入 | 前端表单填写后直接发起（仅本次生效，不落盘）；或 CLI `python -m mas run ... --api-key sk-xxx --base-url https://.../v1` |

```bash
# 例：不落盘，用环境变量注入
export MAS_LLM_API_KEY=sk-xxx
export MAS_LLM_BASE_URL=https://api.chat.csu.edu.cn/v1
python -m mas run -r "实现一个订单计价服务" --text
```

前端面板（启动命令见上文「前端面板」小节）顶栏有「⚙ 模型配置」入口，弹窗里填写供应商、接口地址、
API Key、默认模型与令牌备注名：可先点「测试连接」校验 Key 并拉取该 Key 支持的模型列表，
点「保存」写回配置并**立即生效**（无需重启）。
对话区的模型下拉框按当前 API Key 动态填充：Key 变更或所选模型不在可用列表时，
自动回退到第一个可用模型并在状态栏给出明确提示。
对话区左下角实时展示上下文用量（单次调用 token 峰值 / 模型上下文窗口 + 占比进度条），
达到 80% 转橙、95% 转红并提示精简需求。窗口大小（端点不返回该值，平台公告当前不超过 256K）
在「模型配置」弹窗的「上下文窗口」里填，写回 `system.yaml` 的 `llm.context_window`，改完进度条立即重算。

> 离线模式的“确定性”体现在：代码生成、测试执行、静态审查、门禁判定全部真实发生，
> 只有“自然语言理解”部分被规则化算子库替代，用于可重复回归。

## 交付物（workspace/）

```
workspace/
├── runs/<run_id>/            # 每次运行
│   ├── input/requirement.md   # 需求原文
│   ├── events.jsonl           # 事件日志（唯一事实来源）
│   ├── messages.jsonl         # Agent 间通讯
│   ├── prompts/               # 每次 Prompt（含经验注入证据）
│   ├── artifacts/<产物>/v*.json # 版本化黑板产物
│   ├── code/                  # 真实交付代码 + 测试（沙箱执行）
│   ├── test_runs/iter*/       # 每轮测试的 stdout/stderr/result.json
│   ├── reports/acceptance_report.md|.json
│   ├── evidence/index.jsonl   # 证据索引
│   ├── run_summary.json / timeline.md
├── knowledge/experience.jsonl # 跨运行经验库
├── knowledge/metrics.jsonl    # 每轮效果指标
└── reports/learning_report.md # 自学习效果报告
```

## 项目结构

```
mas/
├── config.py       # 配置加载/深合并/校验（Profile、节点裁剪、LLM 凭据外注）
├── expr.py         # 门禁表达式安全求值（AST 白名单）
├── events.py       # 事件日志
├── blackboard.py   # 版本化共享黑板（跨进程文件锁）
├── filelock.py     # 跨进程文件锁（msvcrt / flock）
├── bus.py          # 消息总线（direct/broadcast/request-response + 可选 MQ 投递）
├── experience.py   # 分级经验库：提炼/检索/注入/晋级/反馈
├── prompts.py      # Prompt 组装（感知段 + 分层经验段 + 交付契约）
├── llm.py          # LLM 客户端（offline / openai_compatible）
├── skills.py       # 技能注册表：目录规范、持久化、参数校验、冒烟、流程沉淀
├── mcp.py          # MCP 服务配置与最小客户端（stdio / sse，仅实现健康检查所需方法）
├── toolrunner.py   # MCP 工具 ↔ function calling 适配器（工具调用闭环）
├── sandbox.py      # 沙箱执行 + AST 静态分析
├── compress.py     # 上下文压缩（Prompt 感知段，默认关闭恒等返回）
├── concurrency.py  # 线程池并行原语（保序 map）
├── envprobe.py     # 环境探测（node / 浏览器 MCP 可用性，发布 env_probe 产物）
├── intent.py       # 交付形态意图识别（backend_only / full_stack_ssr / full_stack_spa）
├── sources.py      # 输入源适配（分类/文档抽取/飞书/上传暂存/附图）
├── uiprober.py     # UI 证据采集（浏览器 MCP 执行 checks）
├── websearch.py    # 联网搜索工具（bing / tavily / serpapi）
├── mq.py           # 消息队列（Redis Streams：任务队列 + Agent 消息流）
├── runhub.py       # 运行注册表（进程内 / Redis 跨进程，取消协调）
├── worker.py       # 运行 worker：消费任务、心跳、ACK、重试、死信
├── store/          # MySQL/Redis 后端（软依赖：连接失败自动降级不抛异常）
├── leader.py       # 调度器：状态机/门禁/回退/协商/经验闭环
├── schemas.py      # 数据契约
├── errors.py       # 异常层级
├── agents/         # 5 个角色 Agent（基类 + 实现；基类含可选 ReAct 推理-行动循环，见 docs/34）
├── providers/      # 离线确定性 Provider + 算子库（ops.py）
└── cli.py          # CLI（run/demo/experience/store/worker/mq/config/report/skill/mcp）
```
