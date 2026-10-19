# 29 · superpowers 技能适配（全文注入）与默认启用

> 目标：把 `github.com/obra/superpowers`（v7.0.0，commit `bb92a77`，克隆在本仓库 `tdd/`）
> 的 15 个技能适配进 multi_agent 的技能体系并**默认开启**。
> 本文记录加载机制选型、实际改动清单、预算测算与验证结果。

## 1. 加载机制：采用通用 Agent 宿主的全文注入

### 1.1 上游原生做法

superpowers 以插件形态分发（`.claude-plugin/plugin.json` v7.0.0 + `marketplace.json`，
另附 `.codex-plugin` / `.cursor-plugin` 等 12 套宿主适配目录），装好后有两条加载路径：

| 路径 | 机制 | 证据 |
| --- | --- | --- |
| SessionStart hook | 会话启动 / clear / compact 时执行 `hooks/session-start`，把 `using-superpowers/SKILL.md` **全文**包进 `<EXTREMELY_IMPORTANT>` 注入 additionalContext | `tdd/hooks/hooks.json`、`tdd/hooks/session-start` |
| Skill tool 按需加载 | `name` + `description` 常驻工具列表，**LLM 自行判断**调用哪个；调用即把该 SKILL.md 全文读进上下文 | 各 SKILL.md front-matter |

即：**front-matter 只是路由元数据，正文才是内容本体**，命中即整篇加载。
上游 15 篇正文合计 162712 字符，单篇 2974（requesting-code-review）~32577（subagent-driven-development）。

### 1.2 本项目的做法

`mas/skills.py` 走同一条路线，只是把「谁来决定命中」换成本项目已有的确定性机制：

```
节点执行前 match_skills（确定性打分，不依赖 LLM 自觉）
  → 命中集合
  → render_for_prompt：front-matter 条目作索引在前 + SKILL.md 正文全文在后
```

匹配规则不变（`score_skill`）：`requires_artifacts` 硬门禁 → `nodes`/`roles` 作用域 +1
→ `keywords` 命中需求文本 +2/项 → `intents` 命中节点标签 +3 → 三项全不命中则不注入。

### 1.3 为什么必须转换（与上游的三处硬差异）

| | superpowers | multi_agent |
| --- | --- | --- |
| front-matter | 只有 `name` + `description` | 必填 6 项：`name/description/when_to_use/trigger/inputs/steps`（`validate_skill`） |
| 触发依据 | LLM 读 description 做语义判断 | 机器按 `trigger` 打分，必须绑定到流水线节点才可能命中 |
| 动作语义 | 面向有人搭档的交互式编码宿主（提问、建 worktree、派 subagent） | 固定流水线、无人在环、无 git、无动态派发通道 |

需要转换的只有**元信息**；方法论**原样搬运**。

## 2. 适配产物（正文快照模式）

生成脚本：`tools/import_superpowers_skills.py`（只依赖 PyYAML + 标准库，幂等覆盖）

```
python tools/import_superpowers_skills.py            # 生成
python tools/import_superpowers_skills.py --dry-run  # 只看计划
```

输出 `config/skills/sp-<上游名>/SKILL.md` + `references/`（连同 `root-cause-tracing.md`
这类延伸资料一起复制）。本地 SKILL.md 的正文结构：

1. 来源与适配说明（上游路径、commit、注入范围、停用状态）；
2. **上游原文全文**——逐字保留，不改写、不压缩、不蒸馏。

`steps` 由上游正文的二级标题机械抽取（仅作 front-matter 必填项与目录用），
不再手工维护一份平行摘要，避免与上游原文漂移。脚本因此从 815 行缩到 604 行。

### 节点绑定（每个节点 ≤2 个，避免占满 `max_items=3`）

| 节点 / 角色 | 绑定的 superpowers 技能 |
| --- | --- |
| intake / product | `sp-brainstorming` |
| solution / developer | `sp-writing-plans` |
| implement / developer | `sp-test-driven-development`、`sp-executing-plans` |
| review / reviewer | `sp-requesting-code-review` |
| test / tester | `sp-verification-before-completion` |
| optimize / developer | `sp-systematic-debugging`、`sp-receiving-code-review` |
| report / reporter | `sp-finishing-a-development-branch`、`sp-writing-skills` |

### 只按关键词触发（不绑定节点）

`sp-dispatching-parallel-agents`、`sp-subagent-driven-development`、`sp-using-git-worktrees`

这三个依赖宿主「派发子 Agent / 建 git worktree」的能力，multi_agent 是固定流水线、
无动态派发通道，产物也已天然隔离在 `workspace/runs/<run_id>/` 下。因此不给 `nodes`，
只在需求文本真的出现「并行 / 独立任务 / 工作区隔离」等关键词时才参与竞争。

### 停用但保留资料（2 个）

`sp-using-superpowers`、`sp-diagnosing-superpowers`

- `using-superpowers` 是宿主层元指令（「任何响应前先检查适用技能」）。multi_agent 的等价机制是
  `mas/agents/base.py:match_skills`——每节点执行前框架确定性匹配，不靠模型自觉，再注入属重复且占名额。
- `diagnosing-superpowers` 诊断的是 superpowers 宿主会话，本流水线无该对象；运行复盘走
  `workspace/runs/<run_id>/events.jsonl` 回放。

停用方式：trigger 里写 `requires_artifacts: ["__disabled__"]`——`requires_artifacts` 是硬门禁，
声明一个永不就位的产物即永不命中。比给一个不会命中的关键词更确定、也更好解释。

## 3. 改动清单

### 3.1 全文注入改造（本次）

| 文件 | 改动 | 理由 |
| --- | --- | --- |
| `mas/skills.py` `SkillMatch` | 新增 `body` 字段承载 SKILL.md 正文全文 | 匹配结果必须带得上注入所需的内容；正文此前根本没进匹配对象 |
| `mas/skills.py` `score_skill` | 填充 `body=(skill.body or "").strip()` | 同上 |
| `mas/skills.py` `render_for_prompt` | 在结构化条目之后追加「技能文档正文（SKILL.md 全文，按此执行）」+ 全文；标题改为「技能注入（本次节点匹配到的技能全文）」；默认 `max_chars` 4000 → 88000 | 全文注入的标准做法是命中即加载整份文档；只注入摘要有丢方法论的风险 |
| `mas/skills.py` 模块 docstring | 「正文用于补充说明，不参与校验」→ 说明正文是方法本体、按全文注入 | 注释与实现保持一致 |
| `mas/agents/base.py` | `skill_matched` 事件补 `body_chars`（每篇）与 `truncated`（是否触预算）；`max_chars` 兜底值同步 | 全文注入后必须能从事件流核对「注入的是多少正文、有没有被切」 |
| `config/system.yaml` | `runtime.skills.max_chars: 4000` → `88000` | 见第 4 节测算 |
| `tools/import_superpowers_skills.py` | 正文改为上游逐字快照；新增 `read_upstream_body` / `extract_steps`；删除 ADAPTATIONS 里手写的 `steps`/`checkpoints`/`error_handling`（45 处） | 全文注入后不需要平行摘要；手写摘要会与上游原文漂移 |
| `tests/test_skill_injection.py` | 新增 `test_body_is_injected_verbatim` 与 `test_repo_skills_carry_body_and_render_verbatim` | 把「正文必须逐字进 Prompt」固化成回归断言，防止将来退回摘要注入 |

`runtime.skills.tools_enabled` 保持 `false`：superpowers 技能没有 `tool.script`（都是方法论，
不是可执行脚本），无需开工具调用通道。`max_items=3` / `min_score=1` 不动。

### 3.2 启用改动（首轮）

| 文件 | 改动 |
| --- | --- |
| `config/system.yaml` | `runtime.skills.enabled: false` → `true` |
| `tests/test_skill_injection.py` | ①「默认关闭」断言改为「默认开启」，方法名 `test_default_off_...` → `test_disabled_switch_...`（验证的仍是开关关闭分支） |
| 同上 | ②`test_prompt_file_header_records_injected_skills` 原来断言 `skills=['spec-scope-guard'`（首位）；开启后 intake 首位变成匹配度更高的 `sp-brainstorming`，改为断言命中列表**包含** `spec-scope-guard` |

## 4. 注入预算测算

全文注入后预算的职责从「防止索引过长」变成「保证文档完整到达」。取值规则是**满配不截断**：

- `max_items=3`，因此上界 = 正文最大的三篇之和：
  `sp-subagent-driven-development` 32958 + `sp-writing-skills` 27148 + `sp-executing-plans` 20789
  = **80895** 字符，留段头与索引开销后取 **88000**；
- 参照量级：模型上下文窗口 131072 token 起（DeepSeek），英文正文约 4 字符/token，
  满配注入约占窗口 1/4 以内；
- 截断行为不变（尾部切 + `…（技能段已截断）` 标记），但按上界取值后正常注入不应触发；
  真触预算时 `skill_matched` 事件的 `truncated=true` 会留痕。

## 5. 验证结果

- **校验**：`config/skills/` 下 20 个技能（5 自有 + 15 新增）全部通过 `validate_skill`，失败 0。
- **正文逐字性**：`sp-test-driven-development` 的本地正文以上游正文全文结尾，
  含原句 `NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST`、`Delete means delete`。
- **各节点注入体量**（`min_score=1, max_items=3`，脚本 `.workbuddy/verify_fulltext_injection.py`，
  预算读生产配置）：

| 节点 | 命中 | 注入字符 | 截断 |
| --- | --- | --- | --- |
| intake | sp-brainstorming、spec-scope-guard | 12750 | 否 |
| solution | sp-writing-plans | 11586 | 否 |
| implement | sp-test-driven-development、sp-executing-plans | 31999 | 否 |
| review | sp-requesting-code-review、evidence-cited-review | 5856 | 否 |
| test | sp-verification-before-completion | 4608 | 否 |
| optimize | sp-systematic-debugging、sp-receiving-code-review | 18132 | 否 |
| report | sp-finishing-a-development-branch、sp-writing-skills | 37148 | 否 |

  关键词竞争场景（不绑节点的技能被拉入）：implement 38912、optimize 25659，均未截断。
- **停用技能**：7 个节点全部未命中 `sp-using-superpowers` / `sp-diagnosing-superpowers`。
- **回归**：`python -m unittest discover -s tests -p "test_*.py"` 全通过。

## 6. 遗留与取舍（诚实记录）

- 上游正文面向可交互的编码宿主，其中涉及人类搭档的动作（提问、确认、选择方案）在流水线里
  无人应答。处理方式是**不删原文**：原文逐字保留，靠正文前的「适配说明」告知等价动作，
  由模型自行映射。这比改写正文更忠实，但也意味着模型仍会读到无法执行的指令。
- 注入体量从改造前的 886~2042 字符升到 4608~38912 字符。是否影响节点输出质量需要真实
  运行观察，离线只能验证「完整送达且未截断」。若发现注意力被稀释，第一顺位旋钮是
  `max_items`（减少同节点技能数），其次才是 `max_chars`（会切文档）。
- `distill_meta`（reporter 从运行沉淀技能）仍生成 `steps`/`checkpoints`/`error_handling`
  且**不写正文**——这类技能注入时正文为空，只有结构化条目。这是既有行为，未在本次改动范围内。