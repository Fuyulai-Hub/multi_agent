# 五项能力扩展：最小改动技术方案（总览）

> 本文所有结论均以 2026-10-15 逐行核实的代码为准，行号可直接定位。
> 未核实的内容一律标注「待验证」，不作为方案依据。
> 五个能力的分方案见同目录 01~05 文档；本文只做决策视图。

---

## 0. 结论先行

**五个能力全部可以在「不改流程拓扑、不新增节点、不新增门禁表达式、不新增角色」的前提下落地。**
总改动估算：5 个新文件（`mas/sources.py`、`mas/compress.py`、`mas/websearch.py`、`config/inputs.yaml`、对应测试）
+ 6 个既有文件的局部函数改动，合计约 700 行（不含测试）。
**所有新行为默认关闭，默认路径与现状逐字节一致**——这是本方案的第一约束。

| 能力 | 核心接入位置（文件:行） | 新代码 | 改既有代码 | 默认行为 | 主要风险 | 阶段 |
|---|---|---|---|---|---|---|
| 图片输入 | `mas/llm.py:250` `_build_payload` + `mas/leader.py:142` `requirement_source` + `mas/agents/product.py:55` | ~80 行 | llm / leader / product / cli / prompts | 不变（`llm.vision.enabled=false`） | 端点 multimodal 兼容性**待验证** | P3 |
| 上下文压缩 | `mas/agents/base.py:226` `perceive` + `base.py:140` tool 结果回传处 | `compress.py` ~120 行 | base.py ~12 行 | 不变（`enabled=false`） | 压缩丢信息导致下游节点失败 | P4 |
| 飞书文档解析 | `mas/leader.py:131` 输入段 + `mas/config.py:738` `load_requirement` | `sources.py` 飞书部分 ~150 行 | leader ~12 行 | 不变（URL 不匹配走原路） | raw_content 丢表格结构 / 403 / 网络依赖 | P2 |
| 文档上传 | `frontend/server.py:413` `do_POST` + `server.py:335` `start_run` | `sources.py` 抽取 + MCP 解析 ~115 行 | server / app.js / index.html / leader / 配置 ~150 行 | 不变（无上传时前端行为不变） | docx 丢结构；pdf 需配 MCP；解析在沙箱外 | P1（+P1.5 MCP 解析层） |
| 联网搜索 | `mas/leader.py:516` `_build_tool_runner` + `mas/agents/base.py:96` `_tool_schemas` | `websearch.py` ~110 行 | toolrunner / base / leader ~45 行 | 不变（`websearch.enabled=false`） | 端点 function calling **未实测**；prompt 注入 | P5 |

排序理由：**零外部依赖的先落地**（上传），**需外部凭据的其次**（飞书），**需端点能力探测的再次**（图片），
**内部质量改进需对比实验**（压缩），**风险最高的收尾**（搜索）。

---

## 1. 现有架构与五个能力的落点（已核实代码事实）

### 1.1 输入主干线——五个能力中有四个最终都汇入这条线

```text
CLI: python -m mas run -r <path> [--text]          前端: POST /api/run {requirement}
        │                                                   │
        ▼                                                   ▼
Leader.run()                          mas/leader.py:89
        │
        ▼
load_requirement()                    mas/config.py:738      ← 文件 or 文字，二选一，无第三种
        │
        ▼
run_dir/input/requirement.md 落盘     mas/leader.py:141
        ▼
blackboard.publish("requirement_source",
    {title, text, source, chars, run_id})   mas/leader.py:142-147   ← 只有 text，无附件字段
        ▼
intake 节点: ProductAgent.build_context() 取 source["text"]   mas/agents/product.py:55-65
        ▼
PromptBuilder.build()                 mas/prompts.py:63 → PromptBundle{system, user}
        ▼
LLMClient.generate()                  mas/llm.py:139
        → _build_payload()             mas/llm.py:250
        messages = [system, user] 两条纯文本（llm.py:258-261）  ← 无 multimodal parts
        ▼
requirement_spec 发布 → solution/implement/review/test/acceptance/report
```

**结论：需求输入的「单一收窄点」是 `requirement_source` artifact 与 `load_requirement()`。**
图片、飞书文档、上传文档三个能力都只需把数据在收窄点之前/之中汇入 `text`（或扩展该 artifact 的字段），
下游全部零改动。这是四个输入类能力可以小步快跑的根本原因。

### 1.2 五能力现状盘点

| 能力 | 现状 | 关键证据（已逐行核实） |
|---|---|---|
| 图片输入 | 完全没有。`requirement_source` 只有 `text` 字段；payload 只有纯文本两条 message；`generate()` 的 `context` 参数没有传进 `_build_payload` | `leader.py:142-147`；`llm.py:258-261`；`llm.py:152` |
| 上下文压缩 | 只有「无结构硬截断」：`blackboard.brief()` 超长直接切字符串加省略号，JSON 被切坏；经验注入反而有完整的 token 预算机制可作参照 | `blackboard.py:189-190`；`base.py:231-232`；`experience.py:340,375` |
| 飞书文档 | 零代码（全仓 grep `feishu/lark` 零命中）。输入通道只有文件路径和 `--text` 两种。WorkBuddy 宿主侧的飞书 connector 是插件进程，本项目 Python 进程调不到，**必须项目内自发自收** | `config.py:738-748` |
| 文档上传 | CLI 有文件通道；前端 `POST /api/run` 只解析 JSON body，`start_run()` 固定 `as_text=True`，用户只能粘贴文本 | `server.py:418-424,335`；`app.js:566-575` |
| 联网搜索 | 只有 function calling 框架（默认关）：`llm.tools_enabled` → `ToolRunner` 拉 MCP 工具全节点下发；静态声明的工具**不可执行**；无任何搜索实现 | `leader.py:516-550`；`base.py:96-102`；`toolrunner.py:120-121` |

### 1.3 已有可复用机制（五个能力都蹭得上，不要重造）

| 机制 | 位置 | 复用方式 |
|---|---|---|
| 凭据三通道外注（文件 < 环境变量 < 运行时注入） | `config.py:141-159` `resolve_credentials` | 飞书 app_secret、搜索 API key 走同一套优先级 |
| `${VAR}` 环境变量展开（变量未设置即报错，不静默拿空串） | `mcp.py:75-82` `expand_env` | **直接 import 复用**，不重写 |
| 配置文件按需加载（不进 `load_config` 的 bundle 白名单） | `mcp.py:91-99` `load_servers` 读 `config/mcp.yaml` | 新 `config/inputs.yaml` 照抄此模式，**零改 `config.py`** |
| L1 规则 → L2 LLM → L3 框架兜底三层识别 | `mas/intent.py`（`classify_delivery_shape`） | 新输入源的识别/分类按此范本做，判定权不全交 LLM |
| 结果不可用即降级重发 | `llm.py:203-219` 降级链 + `_unusable` | 图片 400 降级、压缩摘要失败的兜底都挂在这骨架上 |
| 事件留痕 | `events.emit` 全链路（范本：`intent_classified`、`tool_calls_requested`） | 每个新能力必须发事件，保证可审计 |

---

## 2. 三条不可动的框架约束（实现时必须规避）

| 约束 | 位置（已核实） | 对本方案的要求 |
|---|---|---|
| 门禁 expr 的 BoolOp **不短路**求值：`A or B` 中 B 的字段缺失会让整条判失败 | `expr.py:170-174` | 本方案**不新增任何门禁 expr**，天然规避；未来若要加，被引用的 artifact 必须恒存在（框架注入默认值 + 无条件发布） |
| profile 在启动期决定；`flow.transitions` 只有 pass/fail 两条边，无条件路由 | `config.py:613-626`；`flow.yaml` 各节点 transitions | 不做「按输入类型分流」的拓扑改造；五个能力全部做成**节点内行为分化 + 输入通道扩展** |
| 端点网关 300s 上限 + 必须流式 + 思考链计入 max_tokens | `llm.py:108-112`；`system.yaml:31-32` | 任何**新增的 LLM 调用**（如 LLM 摘要压缩）默认关闭；开启时必须计入超时预算，不能叠加在 implement 这类长任务后面 |

---

## 3. 改造顺序（P0→P5，每期独立可回滚）

| 阶段 | 内容 | 退出标准 | 顺序理由 |
|---|---|---|---|
| P0 | `mas/sources.py` 地基：source 分类（file/text/feishu/url）+ docx 抽取 + `config/inputs.yaml` 加载 | 单测绿；此阶段不接任何调用方，行为零变化 | 飞书与上传共用地基；纯增量函数 |
| P1 | 文档上传：`POST /api/upload` + `start_run` 文件通道 + `sources.extract_text` 三级路由（stdlib→MCP→明确失败） | 前端传 .md/.docx 各跑通一次 intake；MCP 解析层冒烟通过 | 零外部依赖，当天可验证，用户可感知 |
| P1.5 | MCP 文档解析层（PDF/pptx/xlsx）：`docparse` 服务条目 + `MCPDocumentParser` + 选型冒烟 | `mcp check docparse --call convert_file --params ...` 通过后，真实 PDF 跑通 intake | 接在 P1 之上；依赖隔离是核心收益（主项目保持 stdlib-only） |
| P2 | 飞书 URL 输入：Leader 入口 URL 识别 + `fetch_feishu_document` | 真实飞书文档跑通（需用户提供 app_id/app_secret）；HTTP 桩单测绿 | 纯增量输入通道；依赖用户先去开放平台建应用 |
| P3 | 图片输入：CLI `--image` + multimodal payload + 400 降级 | 端点 vision 兼容性探测通过；降级路径单测绿；真实图片跑一次 intake | 先探测端点兼容性，再决定是否默认开 |
| P4 | 上下文压缩：`compress.py` 结构化压缩 + tool 结果截断 | 12 功能点需求前后对比实验；全量 unittest + offline demo 绿 | 内部质量改进，需对比实验定量收益 |
| P5 | 联网搜索：`websearch.py` + ToolRunner 本地执行器 + `tools_nodes` 节点隔离 | 桩测试 + tools_nodes 过滤单测 + 真实端点一次 intake | 风险最高（function calling 未实测 + prompt 注入），收尾 |

**每期通用回归门**（不通过不算完成）：
```bash
python -m unittest discover -s tests        # 全绿
python -m mas demo                          # offline provider 三轮验收通过
```

---

## 4. 验证矩阵

| 能力 | 单元测试 | 端到端 | 真实端点探测 | 攻击面/健壮性用例 |
|---|---|---|---|---|
| 图片输入 | 400 降级单测（桩 HTTP 400 + "image" detail）；payload parts 结构断言 | 真实图片跑一次 intake，spec 质量对比 | 最小 multimodal 请求探测端点是否接受 image_url（命令见 03 文档） | 超大图/非图片后缀/文件不存在 → 明确报错不拖垮启动 |
| 上下文压缩 | 压缩函数保结构断言（FP id/title 必须保留） | 12 FP 需求开启前后各跑一次，diff prompt 文件 | 不需要端点 | 压缩预算为 0/负数；artifact 为 null/标量 |
| 飞书文档 | 本地 HTTP 桩模拟 token + raw_content 两端点（范本 `tests/fake_mcp_server.py`） | 真实 docx 链接跑通 intake | 真实 app 凭据拉一次 raw_content | 403/404/超时/非 docx 类型（sheet/幻灯片）→ 如实报错；wiki 链接解析分支 |
| 文档上传 | 路由表 + MCP 桩（正常/isError/超时）+ docx 最小 zip 抽取 + save_upload 五条用例 | 前端传 .md/.docx 跑通；pdf 未配置→报错、配置后跑通 | 不需要端点（MCP 冒烟：`mcp check docparse --call`） | 后缀白名单外拒绝；>10MB 拒绝；文件名路径穿越（`../../x`）净化 |
| 联网搜索 | ToolRunner 本地执行器单测；tools_nodes 过滤单测 | 真实端点一次 intake（模型自主发起搜索） | 搜索 API 连通性 + 端点 tool_calls 可靠性 | 搜索 API 超时/空结果 → 不阻塞闭环；prompt 注入样本（搜索结果含指令性文本） |

---

## 5. 未验证项清单（待验证，不作为方案依据）

1. **CSU 端点（DeepSeek/Qwen/GLM）是否接受 `image_url` content parts**——P3 用最小请求探测；GLM 系通常有独立视觉型号，通用模型名不一定支持。降级路径必须默认存在。
2. **CSU 端点 function calling 可靠性**——现有 `tests/test_function_calling_e2e.py` 只覆盖测试桩，未对真实端点验证；P5 必须先实测再默认开。
3. **飞书 raw_content 对表格/图片/代码块的丢失程度**——API 只返回纯文本；复杂文档需 blocks 递归（列为第二步，不阻塞 P2 落地）。
4. **docx 抽取对复杂文档（表格、文本框、页眉页脚）的保真度**——stdlib 只能取 `word/document.xml` 的 `w:t` 文本；P1 用真实 docx 验证后决定是否够用。
5. **MCP 文档解析 server 的工具名/入参名/输出契约**——`markitdown-mcp` 有多个社区发行版，`convert_file`/`file_path` 等名字与「直接返回 markdown content」的行为需按 `06-MCP文档解析接入评估.md` §7 的冒烟命令实测后写入 `config/inputs.yaml`。
6. **搜索 API 选型**（Bing / Tavily / SerpAPI）需要用户确认可用 key；方案按 provider 可插拔设计，不绑定具体厂商。

---

## 6. 分方案索引

| 文档 | 内容 |
|---|---|
| `01-图片输入.md` | multimodal payload 改造、requirement_source 扩展、400 降级、端点兼容性探测 |
| `02-上下文压缩.md` | compress.py 结构化压缩、tool 结果截断、可选 LLM 摘要、12FP 对比实验设计 |
| `03-飞书在线文档解析.md` | sources.py 适配器、inputs.yaml 凭据、token/wiki/raw_content 三端点、桩测试 |
| `04-文档上传.md` | /api/upload base64 通道、docx 抽取、两段式上传-启动时序、安全面 |
| `06-MCP文档解析接入评估.md` | 文档解析层选型评估：stdlib/MCP 本地/MCP 云端/pip 四方案对比、三级路由、框架直调的四个理由、docparse 配置示例、沙箱外解析风险 |
| `05-联网搜索.md` | 三路径对比（MCP 零代码/本地工具+节点隔离/确定性预检索）、prompt 注入风险 |
