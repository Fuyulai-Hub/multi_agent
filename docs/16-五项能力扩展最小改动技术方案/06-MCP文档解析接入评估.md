# 06 · 文档上传的解析层选型：MCP 工具接入评估

> 行号基准：2026-10-15 逐行核实。未核实项标注「待验证」。
> 本文是对 `04-文档上传.md` 解析层的选型评估与修订依据；04 文档已按本文结论修订（见 §9）。

## 1. 结论

**可行，推荐采纳——但调用方式必须是「框架直调 MCP」（Leader 输入段直接 `tools/call`），
不能走「模型 function calling」（ToolRunner 通道）。**

MCP 方案对这个项目的核心价值**不是解析质量，而是依赖隔离**：
主项目继续保持 stdlib-only（PyYAML 是唯一依赖，离线 demo 与 HPC 部署不受影响），
把 pypdf / markitdown 这类重依赖关进一个**可替换的独立进程**。这与 README 既有定位一致——
「能力扩展是独立于交付流水线的能力层，由 `mas/skills.py` / `mas/mcp.py` 两个确定性模块提供」。
PDF 从此从「明确不支持」变为「配置了 MCP 解析服务就支持，没配就如实报错」。

最终形态是**三级路由**：stdlib 默认 → MCP 增强 → 明确失败（不静默、不伪造）。

## 2. 现状核实：现有 MCP 客户端能支撑到哪一步

| 能力 | 事实 | 位置 |
|---|---|---|
| `tools/call` **完整实现**，不是残缺的健康检查桩 | `call_tool(name, arguments)` → 返回 `result` dict（含 `content` / `isError`） | `mas/mcp.py:478-480` |
| 进程模型 | stdio = 直起子进程（Popen + 换行分隔 JSON-RPC），stdio/sse 双传输 | `mas/mcp.py:227-258` |
| 生命周期 | `start / initialize / call_tool / close` 齐全；`check_server` 的 try/finally close 是范本 | `mas/mcp.py:441-480, 522-554` |
| 配置与冒烟 | `config/mcp.yaml` + `validate_server` + **`mcp check <name> --call <tool> --params '{...}'` 带参冒烟**——选型验证通道现成 | `mas/mcp.py:484-558`；`mas/cli.py:353-361` |
| 凭据外注 | `${VAR}` 展开，变量未设置即报错（不静默拿空串） | `mas/mcp.py:75-83` |
| **超时坑** | stdio 的 `receive()` 是按行等待 `queue.get(timeout=self.timeout)`，默认 `timeout_s=10`——**不够 PDF 解析**，parse 服务条目必须显式配 60~120s | `mas/mcp.py:277`；`DEFAULT_TIMEOUT_S=10`（`mcp.py:68`） |
| 模型调用通道已存在但**不适用于解析** | `ToolRunner.register_from_mcp` → function calling 闭环（`leader.py:526-528` → `base.py:104-142`），与「框架直调」是两条独立路径，互不冲突 | `mas/toolrunner.py:37-69` |

**「只实现健康检查所需三个方法」的真实含义**：不实现 resources/prompts/分页/通知等规范外围，
`tools/call` 本身是完整的。文档解析只需要 `initialize` + `tools/call`，**客户端零改动**。

## 3. 方案对比：四种文档解析后端

| 维度 | L1 stdlib 内置 | L2a MCP 本地 stdio（推荐） | L2b MCP 远端 SSE | pip 直引依赖 |
|---|---|---|---|---|
| PDF | 不可能 | 可行（markitdown 系） | 可行 | 可行 |
| docx | 可行但 lossy（表格/图片丢） | 可行且保真高 | 同左 | 同左 |
| pptx/xlsx/html/epub | 部分不可能 | 可行（29+ 格式） | 同左 | 同左 |
| 主项目依赖 | 零 | **零**（依赖在独立进程） | 零 | **破坏零依赖** |
| 离线 / HPC 集群 | 完全可用 | 可用（pipx/pip 装本地 server；npx 未必可用） | 不可用 | 可用 |
| 失败隔离 | 解析库崩在主进程 | 子进程崩 → `ConfigError`，主流程可控 | 同左 | 崩在主进程 |
| 数据外流 | 无 | 无 | **有**（文档出本机） | 无 |
| 可测试性 | 单测直测 | fake server 桩（范本 `tests/fake_mcp_server.py`） | 同左 | 单测 |
| 运维成本 | 零 | 装 server + 配两条 YAML | 买 token | pip install |

候选 server（选型时需实测，工具名以 `tools/list` 为准）：
- **本地 stdio（推荐）**：Microsoft MarkItDown 的 MCP 封装（`markitdown-mcp`，社区有多个发行版，
  典型工具 `convert_file` / 入参 `file_path` / 返回 markdown 文本 content）。**待验证**：
  具体发行版的工具名、入参名、是否直接返回文本 content（有的 server 返回「已转换到 /tmp/x.md」的路径，
  那种不适配——见 §6）。
- **远端 SSE（不推荐默认）**：Apify 等托管的 markitdown MCP（按次付费）。上传的文档会流出本机，
  只适合用户显式知情并选择的场景；本机安全语境下不作为默认。

**为什么不用 Skill 机制**：`mas/skills.py` 是 prompt 级指令注册表，不做框架侧执行；
文档解析是确定性框架动作，MCP（或 stdlib）才是正确载体。

## 4. 推荐架构：三级路由

### 4.1 数据流

```text
前端 /api/upload 或 CLI 文件输入
        │
        ▼
Leader.run 输入段                       mas/leader.py:131-141
        │  sources.extract_text(path)   ← 路由（确定性）
        ▼
mas/sources.py:
    L1  stdlib：.md/.txt/.csv/.json 直读；.docx 用 zipfile+ElementTree 抽取（P0 已规划）
    L2  MCP  ：后缀命中 inputs.yaml 的 document_parse.formats 时：
                 server = get_server(config, "docparse")     # mcp.yaml 条目，复用 validate/check
                 MCPClient(server).start() → initialize()
                 → call_tool(tool, {path_arg: 绝对路径})
                 → 抽 content[].text；isError=true 或空文本 → ConfigError
                 → close()（try/finally，范本 check_server:553-554）
    L3  失败  ：都不支持 → ConfigError「格式 X 需要配置文档解析 MCP 服务」（如实失败）
        │
        ▼
run_dir/input/requirement.md（UTF-8 文本）+ requirement_source(source="file") → intake（零改动）
```

### 4.2 四个关键设计决定（为什么是框架直调）

1. **确定性**：解析是确定性动作。「让模型决定调不调解析工具」把确定性步骤变成概率性步骤，
   且真实端点的 tool_calls 可靠性**未实测**（`tests/test_function_calling_e2e.py` 的 LLM 是 localhost 桩）。
2. **json_mode 冲突**：tools 与 `response_format=json_object` 互斥（`llm.py:271-276`）。
   走 function calling 会让 intake 失去 JSON 端点强制约束——用一个输入能力换契约稳定性，不划算。
3. **provider 无关**：框架侧解析让 `offline` provider 也能吃文档上传，离线 demo 回归不受影响。
4. **可回放**：同一文件 + 同一 server → 同一文本，测试可断言。

## 5. 文件级改动清单（对 04 文档的修订）

| 文件 | 位置 | 改动 | 估算行数 |
|---|---|---|---|
| `mas/sources.py` | 新（P0 骨架） | `extract_text(path)` 路由 + `MCPDocumentParser`（config 读取、client 生命周期、content 抽取、错误翻译） | ~70 |
| `mas/leader.py` | `run`（131-141） | 文件后缀路由 stdlib → MCP → 失败；发 `document_parsed` 事件（path/format/chars/backend） | ~12 |
| `config/mcp.yaml` | servers | 加 docparse 服务条目（见 §5.1），**`timeout_s: 120` 必须显式写** | ~8 |
| `config/inputs.yaml` | 新 | `document_parse` 段：server 名 / tool 名 / path 参数名 / formats 后缀表 | ~8 |
| `frontend/server.py` | `save_upload` | 后缀白名单扩到 pdf/pptx/xlsx（能否解析由后端路由决定，前端只做大小与净化） | ~4 |
| `frontend/index.html` | 上传提示 | 文案注明「PDF 等格式需要配置文档解析 MCP 服务」 | ~2 |
| `tests/test_sources.py` | 新 | fake parse server 桩 + 路由/isError/超时三路径 | ~50 |

### 5.1 配置示例（工具名/入参名以 `mcp check` 实测为准）

```yaml
# config/mcp.yaml
servers:
  - name: docparse
    transport: stdio
    enabled: true
    command: markitdown-mcp            # pipx install git+https://github.com/trsdn/markitdown-mcp.git
    args: []
    timeout_s: 120                     # 必须显式调大：默认 10s 不够 PDF 解析（mcp.py:277 按行等待）
```

```yaml
# config/inputs.yaml
document_parse:
  server: docparse          # mcp.yaml 中的服务名
  tool: convert_file        # 工具名（以 tools/list 实测为准）
  path_arg: file_path       # 文件路径参数名（以实测为准）
  formats: [".pdf", ".pptx", ".xlsx", ".html", ".epub", ".zip"]   # 走 MCP 的后缀
```

## 6. 风险与必须接受的取舍

| 风险 | 等级 | 评估与缓解 |
|---|---|---|
| **解析在沙箱之外**：`_StdioTransport` 直起子进程，不经 sandbox 白名单、Windows 无 rlimit；上传的 PDF 是不可信输入，解析器（pdfminer 系）历史上有内存安全问题 → 最坏可在 MCP server 进程内 RCE | **高** | 缓解：格式白名单 + 10MB 大小上限（04 方案已有）+ 文档如实告知「解析发生在沙箱子进程之外」。彻底方案（parse server 也走 sandbox 执行）需改 `_StdioTransport` 的启动路径，改动面大，**明确不做**，列为后续项 |
| server 生态参差：工具名/入参/输出契约不统一；有的 server 返回输出**文件路径**而非文本 content | 高 | 选型必须过 `mcp check --call` 冒烟；适配层只契约化「返回文本 content」的 server，返回路径的需加读回逻辑（文件落入 workspace 边界，与沙箱交付物混杂），**列为不适配** |
| 数据外流（仅 SSE/云 server） | 高（若选云端） | 默认只推荐本地 stdio；云端选项必须用户显式配置并知情 |
| `timeout_s` 默认 10s 坑 | 中 | docparse 条目显式 120s；实现里对 parse 调用的超时单独给默认值，不依赖服务条目配全 |
| `tools_enabled=true` 时 parse 工具也会进 LLM 工具池（`register_from_mcp` 拉全部 enabled 服务） | 低 | 无害（模型多用一个工具）；若不愿暴露，可把 docparse 的 `enabled` 设 false 并由 `sources.py` 用 `get_server` 直取——但这绕过 enabled 语义，需在配置注释里写明，避免误导 |
| HPC 可移植性：npx 未必可用 | 中 | 推荐 pipx/pip 安装的本地 stdio server（集群有 Python 即可）；不依赖 npx |

## 7. 验证方式

1. **选型冒烟（接入前置条件）**：
   ```bash
   python -m mas mcp check docparse --call convert_file --params '{"file_path": "C:/path/real.pdf"}'
   ```
   通过（tools/list 见到工具、tools/call 返回 markdown content）才允许接入；失败则维持 L1-only。
2. **单测**（tests/test_sources.py）：fake parse server 桩（范本 `tests/fake_mcp_server.py`）覆盖
   正常 markdown / `isError=true` / 超时三条路径 + 路由表断言（pdf→MCP、md→stdlib、.psd→明确失败）。
3. **端到端**：① 未配置 docparse 时传 pdf → 明确报错且不影响 md/docx；② 配置后传真实 pdf →
   `input/requirement.md` 为抽取文本 → intake 产出正常 spec。
4. **回归**：`python -m unittest discover -s tests` 全绿 + offline demo 通过
   （证明解析在框架侧完成，与 LLM provider 无关）。

## 8. 验收标准

1. 路由表三条路径（stdlib / MCP / 明确失败）各有单测，`document_parsed` 事件含 backend 字段；
2. 真实 PDF 端到端跑通一次 intake，抽取文本字符数记入本文「实验记录」节；
3. 未配置 MCP 时 PDF 上传的错误信息可读、不拖垮其他格式；
4. 默认配置（无 docparse）下全量 unittest + offline demo 与现状一致。

## 9. 对 04-文档上传.md 的修订点（已同步应用）

1. §1 结论：PDF 从「明确不支持」改为「MCP 可选支持（本文档），未配置则如实报错」；
2. §3.1 数据流：`extract_docx_text` 改为 `sources.extract_text` 三级路由；
3. §3.3 改动清单：`extract_docx_text` 一行替换为 `extract_text` + `MCPDocumentParser` + 两处配置；
4. §3.5 风险表：pdf 行改写，新增「解析在沙箱之外」与「server 契约不统一」两条；
5. §3.7 验收标准：新增 MCP 路由两条；
6. 阶段：文档上传（P1）与 sources.py 地基（P0）合并实施，MCP 解析层作为 P1.5 紧随其后。

---

## 实验记录（2026-10-15 本机实测）

| 项 | 结果 |
|---|---|
| 路由表三条路径 | `tests/test_sources.py`：`.md`→stdlib、`.docx`→stdlib 抽取、`.pdf`（formats 命中）→MCP、
  `.psd`→明确失败，全部断言通过 |
| MCP 解析层 | 用 `tests/fake_parse_server.py` 桩覆盖 正常 markdown / `isError=true` / 超时 三条路径，
  `MCPDocumentParser` 契约化「返回文本 content」并 try/finally close |
| **选型冒烟（§7 前置条件）** | **未执行**：`markitdown-mcp` 未安装（本机无该命令），故 `config/mcp.yaml` 里 docparse
  条目以「注释示例」形式给出（含 `timeout_s: 120`），`mcp list` 显示未配置 —— 默认维持 L1-only |
| 未配置 docparse 时传 pdf | 明确 `ConfigError`，不影响 md/docx（实测通过） |
| 配置后真实 pdf 跑通 | **未执行**（阻塞于上一条：需先装 server 并过冒烟；见 §8 第 2 条的对应说明） |
| 默认配置回归 | 无 docparse 时全量 260 条 unittest + offline demo 全绿，与现状一致 |
| 沙箱外解析风险 | 保持如实告知：解析发生在 MCP 子进程（沙箱之外），彻底方案明确不做，列为后续项 |
