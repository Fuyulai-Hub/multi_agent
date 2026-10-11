# Skill 与 MCP 扩展能力方案（设计稿，未实现）

> 本文只定方案，不改代码。所有结论均基于当前源码核对（见文末「事实核对表」）。

---

## 0. 结论先行

| 问题 | 结论 |
| --- | --- |
| 交给哪个 Agent？ | **手动扩展与自动装载不交给任何 Agent**；只有「流程沉淀」交给 Agent，且**复用现有的 `reporter`**，不新建角色 |
| 要不要新建第 6 个 Agent？ | **不要**。新建角色要动 `flow.yaml` + `collaboration.yaml` + `roles/` + `tests/`，而三件事里只有沉淀需要 LLM |
| 整体形态 | 三层：**L0 能力运行时**（纯代码）→ **L1 Agent 承载**（仅沉淀）→ **L2 对话/CLI 入口** |
| 主流程影响 | `flow.yaml` / `collaboration.yaml` / `roles/*.yaml` **一行不动**。沉淀走 off-flow 单次调用 |

---

## 1. 为什么不放进流水线、不新建角色

### 1.1 一个本质区分

> **流水线节点 = 为本次需求产出交付物；能力层 = 改变系统自身能做什么。**

`flow.yaml` 里每个节点都产出黑板产物（`requirement_spec` / `code_manifest` / `review_report`…），终态是"验收通过"。而「装一个 MCP server」「沉淀一个 skill」是**元操作（meta）**——它不服务于本次需求，而是改变系统后续的能力集。把元操作塞进交付流水线，会让"跑一次需求"变成"顺便改了系统"，破坏幂等性。

### 1.2 塞进 flow.yaml 的具体代价

| 代价 | 说明 |
| --- | --- |
| 自学习基线失效 | README 与 `metrics.jsonl` 现存的收益基线是**离线首轮 11 步 → 次轮 7 步**（README 写 12→7，实测 11→7，两处不一致）。新增节点会让步数不可比，`learning_report.md` 的跨轮对比失去意义 |
| 改动面爆炸 | 新增角色必须同步改：`config/roles/<new>.yaml`、`flow.yaml`（节点 + transitions）、`collaboration.yaml`（perception / broadcast / rollback 三个矩阵）、`AGENT_REGISTRY`、`offline.py` handlers、`tests/` |
| 副作用污染 | 装载 MCP 会 `subprocess.Popen` 起常驻进程。放进流水线意味着每次 `run` 都可能留下僵尸进程 |
| 职责串味 | `developer` 的人设是"写代码交付"，让它产出 skill 文档会破坏 prompt 一致性；`product` 懂需求拆解但不懂执行细节 |

### 1.3 那沉淀为什么可以复用 reporter

`mas/agents/reporter.py` 的现状：

```python
class ReporterAgent(BaseAgent):
    task_kinds = {"default": "acceptance_report"}
    contracts = {...}
    def _build_facts(self, task, perception) -> Dict[str, Any]   # 汇总全流程证据
    def _evidence_index(self) -> Dict[str, Any]                  # 证据索引
```

它的职责已经是 **「读全量证据 → 产出一份 Markdown 文档」**（`acceptance_report.md`）。沉淀 skill 本质是同一件事，只是**换了一份输出契约**：输入仍是历史事件 + 黑板产物，输出变成 `config/skills/<id>/SKILL.md`。

所以只需：

```python
task_kinds = {"default": "acceptance_report", "skill_distill": "skill_distill"}
contracts  = {"default": {...}, "skill_distill": {...}}
```

改动约 40 行，且**不进 `flow.yaml`**——由 Leader 暴露一个 off-flow 方法单独调用。

---

## 2. 三层架构

```
┌──────────────────────────────────────────────────────────────┐
│ L2  入口层（对话 / CLI / HTTP API）                            │
│     mas/cli.py       skill add|list|show|edit|remove|check    │
│                      mcp   add|list|show|remove|check|call    │
│     frontend/server.py   POST /api/skills  POST /api/mcp      │
│     意图路由：规则优先（/skill、/mcp、关键词）+ LLM 兜底         │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│ L1  Agent 承载层（唯一需要 LLM 的环节）                         │
│     reporter.act(task_kind="skill_distill")   ← off-flow 调用  │
│     输入：run_dir/events.jsonl + artifacts/*.json              │
│     输出：config/skills/<id>/SKILL.md                          │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│ L0  能力运行时（纯代码，确定性，零 LLM）                        │
│     mas/skills.py   注册表 / 目录规范 / 校验 / 装载 / 冒烟       │
│     mas/mcp.py      配置 / stdio+SSE 客户端 / 握手 / 调用       │
│     mas/sandbox.py  复用现成的 run() 做 skill 冒烟执行          │
└──────────────────────────────────────────────────────────────┘
```

**分层依据**：三件事里只有「沉淀」需要自然语言抽象（剔除环境绑定细节），其余两件都是**确定性**的——YAML 读写、参数校验、进程握手、退出码断言。用确定性代码实现，离线 provider 下也能跑，符合本项目"无 Key 也能端到端"的调性。

---

## 3. 规范定义

### 3.1 Skill 目录规范

```
config/skills/<skill-id>/
├── SKILL.md          # 必需：YAML front-matter + 正文步骤
├── scripts/          # 可选：可执行脚本（冒烟脚本放这里）
├── references/       # 可选：参考资料
└── assets/           # 可选：模板文件
```

`SKILL.md` 元信息（覆盖需求中的 6 项）：

```yaml
---
name: order-pricing-delivery        # 名称（= 目录名，小写连字符）
description: 一句话说明这个技能做什么
version: 0.1.0
source: manual | distilled          # 手写 or 沉淀
source_run: 20261011-041500-a3f2    # 沉淀来源（manual 时为 null）

when_to_use:                        # ① 适用场景
  - 需要从零交付一个带金额计算的 Python 库
  - 需要同时产出实现代码 + 边界测试

trigger:                            # ② 触发条件
  keywords: [订单计价, 金额计算, 优惠]
  intents: [deliver_library]
  requires_artifacts: [requirement_spec]   # 前置黑板产物

inputs:                             # ③ 入参说明
  - name: spec_path
    type: string
    required: true
    default: "{{workspace}}/input/requirement.md"
    description: 需求文档路径，支持占位符

steps:                              # ④ 执行步骤（正文用有序列表展开）

error_handling:                     # 异常处理
  - when: 门禁未通过
    then: 按 collaboration.rollback 回退到 optimize，最多 N 次

checkpoints:                        # 结果校验点
  - id: boundary_coverage
    expr: test_report.coverage.boundary.rate >= 1.0

smoke:                              # 可选：冒烟用例（声明后才做动态冒烟）
  command: [python, -c, "print('ok')"]
  expect_exit: 0
tags: [delivery, python]
---
```

`{{...}}` 为占位符，装载时按需展开（`workspace` / `input` / `run_id`）。

### 3.2 MCP 配置规范

`config/mcp.yaml`：

```yaml
servers:
  - name: filesystem                 # 服务名称
    transport: stdio                 # stdio | sse
    enabled: true
    command: npx                     # stdio 必填
    args: ["-y", "@modelcontextprotocol/server-filesystem", "{{workspace}}"]
    env:
      LOG_LEVEL: info
      API_TOKEN: "${MCP_FS_TOKEN}"   # ${VAR} 运行时从环境变量展开，不落明文
    timeout_s: 10

  - name: remote-search
    transport: sse
    enabled: false
    url: https://mcp.example.com/sse   # sse 必填
    headers:
      Authorization: "Bearer ${MCP_SEARCH_TOKEN}"
    timeout_s: 15
```

| 字段 | 校验规则 |
| --- | --- |
| `name` | 必填、唯一、 `[a-z0-9-]+` |
| `transport` | 必填，`stdio` \| `sse` |
| `command` | `stdio` 必填；用 `shutil.which` / `Path.exists` 检查可执行性 |
| `args` | 必须是字符串列表 |
| `env` | 必须是 `dict[str,str]`；值含 `${VAR}` 时运行时展开，缺失即报错并指出变量名 |
| `url` | `sse` 必填，必须是 `http(s)://` |
| `timeout_s` | 正整数，默认 10 |

**密钥处理**：沿用现有 `config.py` 的三通道思路 + `mask_secret()` 脱敏——`list` / `show` 一律脱敏展示，落盘只存 `${VAR}` 引用。

---

## 4. 能力一：手动扩展

### 4.1 三个入口，同一套后端

| 入口 | 形式 | 说明 |
| --- | --- | --- |
| 对话 | `/skill add ...`、`/mcp add ...`，或自然语言 | 经意图路由 → 同一批函数 |
| CLI | `python -m mas skill add --from-yaml x.yaml` | 脚本化、可 CI |
| 配置 | 直接编辑 `config/skills/<id>/SKILL.md`、`config/mcp.yaml` | 纯文本，可版本控制 |

三者都收敛到 `mas/skills.py` / `mas/mcp.py` 的 CRUD，保证行为一致。

### 4.2 命令矩阵

```bash
# Skill
python -m mas skill list [--source manual|distilled]
python -m mas skill show <id>
python -m mas skill add --id <id> --yaml <file>      # 也可 --from-run <run_id>（见能力二）
python -m mas skill edit <id> --set description="..."
python -m mas skill remove <id>
python -m mas skill check <id>                        # 能力三：校验 + 冒烟

# MCP
python -m mas mcp list
python -m mas mcp show <name>
python -m mas mcp add --name <n> --transport stdio --command npx --args ... [--env K=V]
python -m mas mcp remove <name>
python -m mas mcp check <name> [--call tool_name --params '{...}']
```

### 4.3 关于「对话」的现状与缺口（需明确）

**当前系统没有真正的对话式入口**。前端所谓"对话区"（`frontend/app.js` 第 233 行注释）实际是「输入需求文本 → `POST /api/run` → 轮询结果」，不是自由对话。

因此要支持"通过对话添加 skill"，**必须新增一小块意图路由**：

1. **规则优先**（确定性）：消息以 `/skill`、`/mcp` 开头，或命中关键词（`沉淀`、`装载`、`冒烟`）→ 直接路由到对应命令。离线 provider 下也能工作。
2. **LLM 兜底**：规则未命中时，用一次轻量 LLM 调用做意图分类（`run` / `skill` / `mcp` / `chat`），低置信度一律回落为普通需求 `run`（**保守策略，避免误触发副作用**）。

后端落点：`frontend/server.py` 的 `do_POST` 白名单当前只有 `("/api/run", "/api/llm", "/api/llm/test")`，需追加 `/api/skills`、`/api/mcp`、`/api/chat`。

---

## 5. 能力二：流程沉淀（严格非自动）

### 5.1 触发机制

```
run 完成（verdict=pass）
      │
      ├── 不写任何 skill 文件  ← 硬性要求
      │
      ▼
用户在后续对话中明确要求（"把上次跑通的沉淀成技能"）
      │
      ▼
Leader.distill_skill(run_id) → reporter.act(task_kind="skill_distill")
      │
      ▼
产出 config/skills/<id>/SKILL.md（source: distilled, source_run: <run_id>）
```

**代码层面如何保证"不自动"**：`Leader.run()` 收尾处（`_extract_run_end` 之后）**不新增任何调用**。沉淀只能由 `Leader.distill_skill()` 显式触发，该方法的唯一调用方是 CLI / API。可用一条测试锁死：跑完 `run` 后 `config/skills/` 下不应新增文件。

### 5.2 提炼规则：保留什么、剔除什么

输入源：`workspace/runs/<run_id>/events.jsonl`（唯一事实来源）+ `artifacts/<产物>/v*.json`（版本化黑板）。

| 类别 | 处理 | 理由 |
| --- | --- | --- |
| 最终 pass 路径的节点序列 | **保留** → `steps` | 这才是"成功流程" |
| 被回退的失败迭代（`rollback` 事件之前的 iteration） | **剔除** | 临时试错 |
| `bug_reported` / `bug_fixed` 的失败细节 | **剔除** | 属于失败经验，已进 `experience.jsonl`（L1/L2/L3），不该污染 skill |
| 缺陷类型对应的防御措施 | **保留** → `error_handling` | 是可复用的异常处理 |
| 门禁 `criteria` 表达式 | **保留** → `checkpoints` | 天然就是结果校验点 |
| `requirement_spec` 的功能点 | **保留并参数化** → `inputs` | 需求会变，参数化才复用 |
| 绝对路径、`run_id`、时间戳、模型名、token 数 | **剔除/占位符化** | 环境绑定细节 |
| 沙箱内临时文件、`optimize` 的中间补丁 | **剔除** | 临时产物 |

参数化的具体做法：把 `G:\...\workspace\...` → `{{workspace}}`，把具体需求标题 → `{{input}}`。

### 5.3 offline provider 兜底（诚实标注）

`OfflineProvider.provide()` 按 `task_kind` 分发，handlers 字典目前有 9 项。需新增：

```python
"skill_distill": self._skill_distill,
```

用规则化方式从 events 抽取节点序列与门禁表达式，生成结构完整但文字朴素的 SKILL.md。**质量上限明确低于真 LLM**，这是离线模式的既定取舍（README 已声明"自然语言理解部分被规则化替代"），不假装等价。

### 5.4 需确认的实现细节

`reporter.act()` 走 `BaseAgent` 骨架 → `perceive()` → `blackboard.require(names)`。沉淀要读的是**历史 run** 的黑板，需构造一个指向历史 `run_dir/artifacts` 的 Blackboard。实现时要确认其不会在只读场景递增版本号（如有副作用，加一个 `read_only` 开关）。

---

## 6. 能力三：自动装载与冒烟

### 6.1 五阶段流水线，失败可定位

```
config_parse  →  param_validate  →  transport_connect  →  handshake  →  smoke
   YAML解析        必填/类型/取值     起进程/连SSE       initialize    tools/list
```

统一返回结构：

```json
{
  "ok": false,
  "stage": "transport_connect",
  "target": {"kind": "mcp", "name": "filesystem"},
  "reason": "启动命令不存在: npx（PATH 中未找到）",
  "hint": "请安装 Node.js，或把 command 改为绝对路径，例如 C:/path/to/npx.cmd"
}
```

| stage | 典型失败 | 修复建议（自动给出） |
| --- | --- | --- |
| `config_parse` | YAML 语法错误 | 指出行号 |
| `param_validate` | 缺 `command` / `transport` 取值非法 | 列出缺失字段 + 合法取值 |
| `transport_connect` | 命令不存在 / 端口拒绝 | `shutil.which` 结果 + 建议绝对路径 |
| `handshake` | `initialize` 超时 / 协议版本不匹配 | 建议调大 `timeout_s` 或检查 server 版本 |
| `smoke` | `tools/list` 返回空 / 冒烟退出码非 0 | 贴出 stderr 尾部 |

### 6.2 MCP 客户端：零依赖自研

项目唯一依赖是 PyYAML，因此**不引入 `mcp` SDK**，自研最小客户端（约 150 行）：

- **stdio**：`subprocess.Popen(cmd, args, env=..., stdin=PIPE, stdout=PIPE, stderr=PIPE)`，按 newline-delimited JSON 收发 JSON-RPC 2.0。
- **sse**：发送用 `urllib.request` POST；接收用 `http.client` 建连接后 `resp.readline()` 逐行解析 `data:` 事件。

**诚实边界**：MCP 规范（Streamable HTTP / SSE）细节较多，P0 只实现 `initialize` + `tools/list` + `tools/call` 三个方法——这已足够完成健康检查，不承诺完整规范兼容。

### 6.3 冒烟策略

| 目标 | 静态校验 | 动态冒烟 |
| --- | --- | --- |
| MCP | 配置结构 + 可执行性 | `initialize` → `tools/list`；`--call` 时再执行一个只读 `tools/call` |
| Skill | front-matter 必填字段、目录结构、steps 非空 | **仅当** SKILL.md 声明了 `smoke.command` 才执行（复用 `Sandbox.run()`，带 timeout） |

**诚实标注**：未声明 `smoke` 的 skill 只做静态校验，报告须明确写「无冒烟用例，未做动态验证」，不得含糊地报"通过"。

---

## 7. 改动清单

| 文件 | 改动 | 估算 |
| --- | --- | --- |
| `mas/skills.py` | **新增**：注册表 / 目录规范 / CRUD / 校验 / 冒烟 | ~200 行 |
| `mas/mcp.py` | **新增**：配置 / stdio+SSE 客户端 / 握手 / 调用 | ~250 行 |
| `mas/cli.py` | 新增 `skill` / `mcp` 两个子命令组 | ~120 行 |
| `mas/config.py` | `SystemConfig` 加 `skills_dir` 属性 + 读取 `mcp.yaml` | ~20 行 |
| `mas/agents/reporter.py` | 加 `task_kind="skill_distill"` + 对应 contract | ~40 行 |
| `mas/providers/offline.py` | handlers 加 `skill_distill` 算子 | ~60 行 |
| `mas/leader.py` | 加 off-flow 方法 `distill_skill(run_id)` | ~30 行 |
| `frontend/server.py` | `/api/skills`、`/api/mcp`、`/api/chat` 路由 | ~80 行 |
| `frontend/app.js` + `index.html` | 技能/MCP 管理面板 | ~150 行 |
| `config/skills/` | 目录 + 1 个内置示例 | — |
| `config/mcp.yaml` | 新增（含注释示例，默认全 `enabled: false`） | — |
| `tests/test_skills.py`、`tests/test_mcp.py` | 新增 | ~120 行 |

**明确不动**：`flow.yaml`、`collaboration.yaml`、`config/roles/*.yaml`、`config/profiles/*`、`AGENT_REGISTRY`。

---

## 8. 分期与量化验收

### P0（核心闭环，必须先跑通）

1. `mas/skills.py` + `mas/mcp.py` 两个运行时模块
2. CLI `skill` / `mcp` 全套子命令
3. MCP stdio 真实握手（用自带的假 MCP server 脚本）
4. `skill check` / `mcp check` 五阶段失败定位

**验收标准**：
- `python -m mas mcp add --name demo --transport stdio --command <python> --args [tests/fake_mcp_server.py]` → `mcp check demo` 返回 `ok: true`，且打印出 `tools/list` 的工具名
- 故意把 `command` 改成不存在的名字 → 返回 `stage=transport_connect` 且 `hint` 非空
- `python -m mas skill check <示例>` → 静态校验通过，动态冒烟执行并打印退出码

### P1（沉淀）

5. reporter 的 `skill_distill` task_kind + offline 算子
6. `Leader.distill_skill(run_id)` + CLI `skill add --from-run <run_id>`
7. 反自动化测试锁

**验收标准**：
- 跑一次 `run`（pass）后，`config/skills/` 下**无新增文件**（测试断言）
- 执行 `skill add --from-run <run_id>` 后生成 `SKILL.md`，其 front-matter 六项齐全
- 生成的 SKILL.md 中**不含**该 run 的绝对路径与 run_id（grep 断言）

### P2（对话入口）

8. 意图路由（规则优先 + LLM 兜底）
9. 前端 `/api/skills`、`/api/mcp` + 管理面板
10. SSE transport

**验收标准**：
- 对话输入 `/mcp list` 等价于 CLI `mcp list`
- 未命中关键词的自然语言需求仍正常触发 `run`（无误触发回归）

### 回归底线

`python -m unittest discover -s tests -t .` 现有 22 项全绿；`python -m mas config validate` 通过；离线 `run` 步数仍为 7（证明主流程未受影响）。

---

## 9. 待确认问题

1. **`config/skills/` 是否进 Git？** 建议进（skill 是可复用资产），但 `config/mcp.yaml` 因含 `${VAR}` 引用建议进、含明文 token 的本地覆盖文件不进。
2. **MCP env 明文是否允许落盘？** 建议只允许 `${VAR}`，与现有 `credentials.yaml` 的处理保持一致。
3. **对话入口优先级**：P2 还是提前？当前系统没有真对话，做完整对话入口的改动量大于 P0+P1 之和。
4. **skill 是否需要版本化目录**（`v1/` `v2/`）？建议先不做，用 front-matter 的 `version` 字段 + Git 管版本。

---

## 附：事实核对表

以下均为源码实测，非推断：

| 事实 | 来源 |
| --- | --- |
| 5 个角色 Agent + Leader，`AGENT_REGISTRY` 硬编码 5 项 | `mas/agents/__init__.py` |
| `reporter` 的 `task_kinds = {"default": "acceptance_report"}` | `mas/agents/reporter.py:19` |
| `OfflineProvider` 按 `task_kind` 分发，handlers 共 9 项 | `mas/providers/offline.py:72-85` |
| `Sandbox.run(cmd, cwd, timeout, env_extra, result_marker)` 可跑任意命令 | `mas/sandbox.py:159` |
| `do_POST` 路由白名单仅 3 项 | `frontend/server.py:344` |
| 前端"对话区"实为发起 run + 轮询，非自由对话 | `frontend/app.js:233` 注释 + `server.py` run handler |
| 现有凭据三通道 + `mask_secret()` | `mas/config.py:86-159` |
| 实测离线步数 11→7（README 写 12→7，两处不一致） | `workspace/knowledge/metrics.jsonl` / README |
| 唯一依赖 PyYAML | `requirements.txt` |
