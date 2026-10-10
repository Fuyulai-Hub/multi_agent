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

# 查看报告与经验库
python -m mas report show latest
python -m mas experience list
python -m mas config show --profile lean

# 框架自测
python -m unittest discover -s tests -v
```

## 流程与状态机

```text
intake(产品) ─▶ solution(研发) ─▶ implement(研发) ─▶ review(评审) ─▶ test(测试) ─▶ acceptance(门禁) ─▶ report(报告)
                    ▲                │    ▲            │                 │              │
                    └── 审查驳回 ────┘    └── 测试失败/验收驳回 ──▶ optimize(研发: 修复+回归自测) ─┘
                                             修复后重新测试，直至门禁通过
```

- **审查不通过** → 退回研发修改（阻塞问题清零才能放行）
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
| `offline`（默认） | 内置确定性 Provider：解析需求 → 匹配算子 → 生成**真实可运行代码**与**真实可执行测试**，并按配置注入首轮缺陷以演示缺陷闭环。无 API Key 即可端到端跑通 |
| `openai_compatible` | 标准 `/chat/completions` 接口，处理任意需求（`config/system.yaml` 的 `llm.provider` 设为它，模型名填端点上真实存在的模型） |

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

前端面板（`python frontend/server.py`）顶栏有「⚙ 模型配置」入口，弹窗里填写供应商、接口地址、
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
├── blackboard.py   # 版本化共享黑板
├── bus.py          # 消息总线（direct/broadcast/request-response）
├── experience.py   # 分级经验库：提炼/检索/注入/晋级/反馈
├── prompts.py      # Prompt 组装（感知段 + 分层经验段 + 交付契约）
├── llm.py          # LLM 客户端（offline / openai_compatible）
├── sandbox.py      # 沙箱执行 + AST 静态分析
├── leader.py       # 调度器：状态机/门禁/回退/协商/经验闭环
├── schemas.py      # 数据契约
├── agents/         # 5 个角色 Agent（基类 + 实现）
├── providers/      # 离线确定性 Provider + 算子库
└── cli.py          # CLI（run/demo/experience/config/report）
```
