# 45 · task12 FoodLink 前后端分离真实验收记录与能力边界

## 0. 结论（先看这段）

本轮把「FoodLink 社区余量食物捐赠匹配平台」（Vue3 + NestJS + Prisma + PostgreSQL + Docker 的前后端分离 MVP）
完整喂给多 Agent 流水线，真实跑了 3 次，最终结果是：

- **流水线 verdict = fail，acceptance forced_fail = true；10 条验收标准 0 条通过、10 条未验证；33 个缺陷全部 open、修复 0 个。**
- **没有交付一个可运行、可验证的 FoodLink 项目。** 验收报告明确拒绝给出「代码可运行」「接口正确」的结论。

但这不是「模型没干活」：

| 环节 | 真实产出 |
|---|---|
| 需求冻结 | 13 个功能点、117 条契约条款（116 条映射 + 1 条显式 out_of_scope，uncovered=[]）、10 条验收标准 |
| 隐藏用例 | 34 条 Python 用例，1 个测试文件（21,267 字节），签名（64 位十六进制）后冻结 |
| 方案 | 按需求选中 NestJS + Prisma + PostgreSQL + Vue3 SPA，20 个模块、8 项风险、11 个待澄清全部消解、覆盖 13/13 |
| 实现 | 57 个文件、2,747 行；功能点映射 13/13 |
| 测试 | 39 条用例（功能/异常/边界各 13），6 通过、33 失败，通过率 0.1538 |
| 验收 | 门禁 fail（forced），原因：环境/证据通道缺口；总耗时 4,672.21 秒 |

**卡点不在需求理解，而在「测试与验证通道」**：本机沙箱没有 npm / npx / Docker / PostgreSQL / TypeScript 工具链，
框架的隐藏用例协议只支持 Python 包（`from foodlink import create_app`），full_stack_spa 所需的浏览器证据通道也未接入。
用现有框架验收这种需求，结构上不可能通过（详见 §6）。

本轮同时定位并最小修复了 **2 个会制造「假失败」的框架缺陷 + 1 个超时预算配置**，并用回归测试和前后对照证据固定：

1. 隐藏用例方法名「只差后缀」被误判为缺失/多余（`mas/hidden.py`）；
2. 感知 Prompt 对超长需求规格硬截断，整段丢失 FP-12/FP-13（`mas/blackboard.py`）；
3. 隐藏用例单次生成墙钟上限 900 秒不够（`config/system.yaml`，调到 1800 秒 / 总预算 3600 秒）。

修复后全量回归：**705 个测试全部通过（178.967 秒，exit 0）**。
未为了让用例变绿修改任何业务代码，也没有把「环境没跑起来」包装成「功能通过」。

---

## 1. 需求与输入

- 需求文件：`examples/requirements/foodlink_spa_fullstack.md`（7,501 字符）
- 交付形态：`full_stack_spa`（声明的技术栈即验收对象）
- 技术栈：Vue3 + Vite + TS + Pinia + Vue Router + Element Plus + ECharts + Axios（5173）；
  NestJS + Prisma + PostgreSQL + JWT + RBAC + Swagger（3000，`/api`，`/api/docs`）
- 业务：DONOR / RECEIVER / ADMIN 三角色；Listing 七态状态机；Claim 四态；6 位核销码；
  地址脱敏；保质期 ≥ pickupEnd+2h；通知与审计日志；管理员审核/用户管理/统计看板
- 交付要求：Prisma schema + migrations + seed；Jest+Supertest / Vitest；README 与 `.env.example`；
  验收命令 `docker compose up -d` → `npm install && npx prisma migrate dev && npm run seed && npm run start:dev` → 前端 `npm run dev`

需求契约（`workspace/runs/task12-foodlink-spa-03/contract/requirement_contract.json`）：

```
contract_hash = 6bb7a767bc4c0c07ee649ba7b86ac840a82f5f0fbfbbdff42b9e09b1d4f13385
clauses       = 117（mapped 116，out_of_scope 1，uncovered []）
function_points = 13
acceptance_criteria = 10
```

---

## 2. 三次运行时间线

| # | run_id | 触发原因 | 关键事实 | 结果 | 耗时 |
|---|---|---|---|---|---|
| 01 | `task12-foodlink-spa-01` | 首次真实运行 | 隐藏用例生成 3 次尝试，最后一次只剩 3 组「方法名只差后缀」的误判 | intake fail | 3,160.98s |
| 02 | `task12-foodlink-spa-02` | 加方法名归一化后重跑 | 隐藏用例生成在 900 秒被掐断，0 token；重试被 call_budget 拒绝 | intake fail | 1,211.63s |
| 03 | `task12-foodlink-spa-03` | 调高超时预算后重跑 | 隐藏用例 1,132 秒生成 34 条；方案先因规格截断失败，修复后断点续跑通过；实现/测试/验收均 fail | **verdict=fail（forced）** | 4,672.21s |

### 2.1 run 01：方法名误判烧掉 52 分钟

`workspace/runs/task12-foodlink-spa-01/events.jsonl` 的最终错误（节选，原样）：

```
hidden_case_method_missing:test_boundary_claim_cancel_restores_listing_success;
hidden_case_method_missing:test_boundary_listing_expired_status_expired;
hidden_case_method_missing:test_functional_frontend_dependencies_present_present;
hidden_method_without_case:test_boundary_claim_cancel_restores_listing;
hidden_method_without_case:test_boundary_listing_expired_status;
hidden_method_without_case:test_functional_frontend_dependencies_present
```

6 条报错其实是 3 组同一个方法：模型定义的方法名缺少或多了一个语义后缀，校验器按「精确字符串」比对，
既判「用例缺失」又判「方法多余」，3 次重试全部消耗在同一个机械差异上。

### 2.2 run 02：900 秒墙钟、0 token

- `llm_traces.jsonl`（`task_kind=hidden_test_suite`）：`latency_ms=903010`、`tokens=0`，错误文本为超时
- 重试被 `call_budget` 拦截，整轮 1,211.63 秒后失败
- 结论：不是模型不会写，而是单次流式生成被 900 秒硬上限掐断；本轮模型真实生成需要 19 分钟左右

### 2.3 run 03：跑通到验收的完整时间线

| 时间（2026-10-31） | 事件 | 关键事实 |
|---|---|---|
| 19:23:44 | run 开始 | 输入 7,501 字符需求 |
| 19:29:36 | 契约冻结 | 13 FP / 117 clauses / 10 AC |
| 19:48:28 | 隐藏用例冻结 | 34 条用例、1 个文件、34 个方法名归一化 |
| 19:55:47 | solution 第一次 fail | 「方案未覆盖功能点: FP-12, FP-13」——Prompt 规格被截断 |
| 20:01:46 | 断点续跑 solution | 使用修复后的黑板压缩 |
| 20:05:50 | solution pass | NestJS+Prisma+PostgreSQL+Vue3，覆盖 13/13 |
| 20:29:11 | implement 结束（fail） | 57 文件 / 2,747 行；46 个 TS 文件无工具链，自检 fail closed |
| 20:38:19 | test 结束（fail） | 39 用例，6 通过 / 33 失败，通过率 0.1538 |
| 20:38:19 | 验收门禁 | passed=false，forced=true，原因「环境/证据通道缺口」 |
| 20:41:36 | 验收报告产出 | `reports/acceptance_report.md` |

run 摘要（`workspace/runs/task12-foodlink-spa-03/run_summary.json`）：

```
verdict=fail  steps=6  forced_fail=true  bugs_total=33  bugs_fixed=0
duration_s=4672.21  injected_experiences=17  skills_injected=14
experience_learned=0  promoted=0
```

---

## 3. 框架缺陷与最小修复

> 边界声明：工作树里还存在 docs/42~44、task11 相关等历史未提交改动，**不属于本轮**。
> 本节只列本轮为 FoodLink 运行实际改动的 5 个代码/配置文件和 1 个一次性续跑脚本。

### 3.1 隐藏用例方法名「只差后缀」误判（`mas/hidden.py:169`）

**症状**：期望方法名 `test_xxx_success` 与模型定义 `test_xxx` 只差一个语义后缀，
校验器同时报 `hidden_case_method_missing` 和 `hidden_method_without_case`，且不会自动修正。

**最小修复**：

- 新增 `_reconcile_method_names(expected, defined, files)`（`mas/hidden.py:169`）；
- 只在「缺失名与多余名互为前缀/后缀」且候选唯一时归一化；
- 只改 `def` 那一行（正则 `^(\s*def\s+)name(\s*\()`），要求全局恰好命中 1 次；
- 重命名后重新做 AST 解析与方法收集，失败则不加分、照常报错；
- 归一化结果写入 `hidden_meta.json` 的 `reconciled_methods`，可审计。

**修复前后对照**（`workspace/logs/revalidate-task12-attempt03.txt`，对 run 01 的拒收文件重放）：

```
old_error: 隐藏套件结构校验失败: hidden_case_method_missing:... 共 6 条
new_errors: []
reconciled: 3 组（..._listing → ..._listing_success；..._status → ..._status_expired；
            ..._present → ..._present_present）
file_sizes: [('tests/test_hidden_foodlink.py', 25121)]
```

**回归测试**：

- `tests/test_hidden_repair.py:233` 同义后缀自动归一化；
- `tests/test_hidden_repair.py:261` 无关方法名不得被误改；
- `tests.test_hidden_repair` 14/14 通过。

run 03 实际生成时归一化了 **34 个**方法名（`workspace/hidden/task12-foodlink-spa-03/hidden_meta.json`），
说明这是模型的高频行为，不是偶发。

### 3.2 感知 Prompt 规格硬截断（`mas/blackboard.py:206` / `_compact_spec`）

**症状**：需求规格 38,194 字符，`brief()` 按 `prompt_artifact_max_chars=30000` 直接切字符串，
FP-12 / FP-13 整段消失。方案节点只看到 FP-01~FP-11，却被覆盖率校验按 13 个 FP 要求，必然 fail。

**最小修复**：

- `brief()` 超预算时先调用 `_compact_spec()`，复用 `mas/compress.py` 的字段保留策略：
  先丢 `samples` / `parameters` / `source_spans`，再丢 `description` / `inputs` / `outputs`；
- **每个 FP 的 `id` / `title` 永不丢**；压缩后仍超预算才回退到原有硬截断；
- 只改「进 Prompt 的文本」，黑板上的 artifact 数据不变（门禁求值仍走 `snapshot()`）；
- 截断提示改为显示**原始长度**（`共 38194 字符`），不再显示截断后的长度。

**真实前后对照**（同一 run 的两次 solution Prompt）：

| Prompt | 字符数 | 是否含「截断」 | FP-12/FP-13 出现次数 | 结果 |
|---|---|---|---|---|
| `solution#1-developer-194828.md` | 48,149 | 是（`截断, 共 38194 字符`） | 0 / 0 | fail：方案未覆盖 FP-12、FP-13 |
| `solution#2-developer-200146.md` | 33,217 | 否 | 3 / 3 | pass：覆盖 13/13 |

规格压缩实测：38,194 字符 → 15,325 字符（brief 中该产物块约 15.4k），13 个 FP 全部保留，
`source_spans` 按策略丢弃，黑板原始数据未变。

**回归测试**：`tests/test_hardening.py:635`
（13 个 FP + 大字段，断言无「截断」、长度 ≤30000、13 个 id 全在、黑板数据保持原样）。

### 3.3 隐藏用例生成超时预算（`config/system.yaml:192`）

| 配置 | 修改前 | 修改后 | 依据 |
|---|---|---|---|
| `hidden_suite_stream_timeout_s` | 900 | 1800 | run 02 实测 903,010ms / 0 token 被掐断 |
| `hidden_suite_total_budget_s` | 2700 | 3600 | 单次 1800s 后仍需给重试留余量 |

run 03 实测：`latency_ms=1,132,403`、`tokens=55,264`、`finish_reason=stop`，证明 900 秒确实不够，
1800 秒能容纳一次完整生成。**注意：这是放宽预算，不是加速**；19 分钟的单节点生成延迟仍是现实（见 `docs/44`）。

---

## 4. 真实跑测结果（run 03）

### 4.1 intake：契约与隐藏用例

- 契约：13 FP、117 clauses（116 mapped + 1 out_of_scope）、10 AC、`uncovered=[]`；
- 隐藏用例：`workspace/hidden/task12-foodlink-spa-03/tests/test_hidden_foodlink.py`（21,267 字节）；
- 用例数：34；覆盖 13 个 FP 的 functional / exception / boundary；
- 归一化：34 个方法名（例如 `test_exception_auth_wrong_credentials` → `..._wrong_credentials`）；
- 签名与摘要：`hidden_meta.json` 中 `signature` / `manifest_digest` 齐全。

隐藏用例的集成假设（原文节选）：

```python
from foodlink import create_app
...
app = create_app()
cls._server = make_server("127.0.0.1", 0, app)
```

这决定了它只能测 Python 包，测不了 NestJS 服务——这是下一节失败的根。

### 4.2 solution：先失败，修复后 13/13

- 第一次 fail（19:55:47）：模型在回复中明确说只看到了 FP-01~FP-11；
- 断点续跑（20:01:46）使用压缩后的规格，20:05:50 通过；
- 选型：NestJS + Prisma + PostgreSQL + Vue3 SPA；
- 方案内容：20 个模块、8 项风险、11 个待澄清问题全部给出消解、`requirement_coverage=13`。

### 4.3 implement：57 文件 / 2,747 行，但自检 fail closed

`code_manifest.self_check`（原样要点）：

```
ok=false  exit_code=1  unsupported=true
note: 非 Python 交付工具链校验：46 个文件无工具链可验证，fail closed
```

46 个 TypeScript 文件逐一标注「没有 typescript 的编译/校验工具映射」。
`node --check` 不能解析 TypeScript（本轮实测，含 `--experimental-strip-types` / `--experimental-transform-types`，
三种方式均报 `SyntaxError: Missing initializer in const declaration`，exit=1；
证据：`workspace/logs/node-check-ts-probe-20261031.txt`），所以框架无从给出「语法通过」的真结论，只能 fail closed。

更关键的是 `code_manifest.notes` **自己承认是部分交付**（原文摘录）：

> 首批交付核心闭环（FP-01~FP-06、FP-11~FP-13 + FP-09 最小可验收路径 + FP-10 脚手架/种子/文档），
> FP-07 统计与 FP-08 通知已实现接口与聚合逻辑。……未覆盖项：完整 Jest+Supertest e2e
> （需 PostgreSQL 与 prisma generate）与前端剩余业务页面（商家管理/管理员审核/看板/通知）留待后续批次。

但 `fp_mapping` 仍是 13/13——**符号存在即可映射，映射完整性不能替代功能完成度**。
这也解释了为什么静态检查能过而业务闭环不能过。证据：`workspace/logs/code_manifest_notes.txt`。

**静态检查生成代码，交付物本身也不完整**（这属于实现质量问题，不只是环境问题）：

| 需求 | 生成结果 | 差距 |
|---|---|---|
| 前端页面：登录/注册/个人中心、列表/详情/认领、商家发布/编辑/提交/我的、我的认领/核销码、管理员审核/用户/统计、通知、401/403/404/空/加载/错误态 | `frontend/src/views/` 只有 `HomeView.vue`、`LoginView.vue` 两个视图 | 大部分页面缺失 |
| Prisma migrations | `backend/prisma/` 只有 `schema.prisma` 与 `seed.ts`，无 `migrations/` | 迁移缺失 |
| 后端 API 面 | 5 个 controller（auth / listings / claims / admin / notifications） | 与需求 API 清单不一一对应 |
| Jest + Supertest / Vitest | 4 个后端 spec、1 个前端 spec，未真实执行 | 无执行证据 |

### 4.4 test：39 条用例，33 条死在「拉不起后端」

`test_report.summary`：

```
total=39  passed=6  failed=33  pass_rate=0.1538  duration_ms=57
root_cause=environment_or_build
verification_gap=true  gap=1
```

33 条失败是同一个症状（不是 33 个不同业务 bug）：

```
AssertionError: backend service unavailable:
spawn failed: FileNotFoundError(2, '系统找不到指定的文件。', None, 2, None)
```

测试夹具硬编码了 npm 启动（`workspace/runs/task12-foodlink-spa-03/code/tests/test_all.py:58-60`）：

```python
self.proc = subprocess.Popen(
    ["npm", "run", "start:dev"],
    cwd=BACKEND, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
```

沙箱命令白名单只有 `python/python3/py/node`；且 Windows 上 `npm` 实际是 `npm.cmd`，直接 spawn 无扩展名会解析失败。
本轮在 Git Bash + Python 3.14 下实测（真实输出）：

```
['npm', '--version']    -> FileNotFoundError(2, '系统找不到指定的文件。', None, 2, None)
['npm.cmd', '--version'] -> rc=0  stdout=11.12.1
```

（证据：`workspace/logs/npm-spawn-probe-20261031.txt`）

验收报告另记 `preexec_findings: sandbox_command_bypass（tests/test_all.py:58 subprocess.Popen 调用白名单外的 npm）`。

通过 6 条（FP-09 前端静态/渲染 3 条、FP-12 Prisma Schema 静态 3 条）都是静态检查，
**不构成任何后端业务闭环的验证**；浏览器级 AC-10 仍因证据通道未接入判「未验证」。

### 4.5 acceptance：0/10 通过，拒绝给出可用结论

`workspace/runs/task12-foodlink-spa-03/reports/acceptance_report.md` 的关键结论（原样要点）：

- 门禁：`passed=false, forced=true`，签署意见「环境/证据通道缺口，验收不通过」；
- 测试通过率 6/39 = 0.1538；缺陷 33 个全部 open、闭环 0；verification_gap=1；
- AC-01 ~ AC-10 **0 条通过 / 10 条未验证**；
- 审查段 `review_report` 缺失、修复段 `fix_summary` / `fix_test_report` 缺失；
- 明确拒绝给出：代码是否可运行、接口行为是否正确、安全基线是否生效、性能是否达标。

报告里的一句话可以作为本轮总结：

> 本轮交付物（57 文件 / 2747 行）在结构上完成了需求→方案→实现的映射，但没有任何一条业务用例真正执行到被测代码。

---

## 5. 根因四分类

| 类别 | 本轮事实与证据 | 判定 |
|---|---|---|
| 需求理解偏差 | 117 条契约 116 条映射、1 条显式 out_of_scope、uncovered=[]；方案覆盖 13/13；没有发现模型误读需求 | **不是主因** |
| 设计方案缺陷 | 隐藏用例协议固定为 Python `unittest` + `from <pkg> import ...`，与声明的 NestJS 交付形态结构性冲突；无服务生命周期（启动/健康检查/清理）；无语言对齐的非 Python 用例通道；浏览器证据通道未接入；框架把 `environment_or_build` 判为 unfixable 后主动 forced fail（`mas/leader.py:745`），review/optimize 不运行 | **主因（框架能力边界）** |
| 代码实现问题 | 前端仅 2 个视图，需求页面大面积缺失；无 Prisma migrations；46 个 TS 文件无编译校验；测试夹具硬编码 `npm run start:dev` | **次因（交付物不完整/不可验证）** |
| 环境或构建问题 | 沙箱无 npm/npx/Docker/PostgreSQL/tsc/vitest；Windows 上 npm 为 `npm.cmd` 导致 spawn FileNotFoundError；浏览器 MCP 未就绪 | **直接卡点** |

一句话：**需求没有理解错，方案也没有选错；错在「验证通道」和「实现完整度」两头都没到位，而框架目前只能对 Python 包做闭环验证。**

---

## 6. 为什么没有继续修：能力边界

用户要求「涉及的问题如果需要改 agent 代码，最小改动修复」。本轮按这个口径停了，因为剩下的缺口不是最小改动能解决的：

1. **隐藏用例语言协议是框架级约束**
   `intake_hidden_iter01.md` 明确要求：
   「只写 `tests/` 下的 `.py` 文件」「只能使用 Python 标准库与契约声明的交付包（`from foodlink import ...`）」
   「若契约声明的是 HTTP 服务，在测试进程内用端口 0 启动服务并在 finally 中关闭」。
   对 NestJS 交付，这等于要求「在 Python 进程里 import 一个 TypeScript 服务」，结构上不可满足。
   要修就得改成「按交付语言生成用例」或「统一黑盒 HTTP 用例 + 服务生命周期」，涉及隐藏用例协议、执行器、
   门禁证据链三处改造，不属于最小改动。

2. **沙箱工具链与需求不匹配**
   `sandbox_command_policy.allowed = ["python","python3","py","node"]`；没有 npm/npx/docker/PostgreSQL/tsc。
   即使把 npm 加进白名单，还需要 PostgreSQL 实例、迁移、seed、前后端进程编排与清理。

3. **浏览器证据通道未接入**
   `full_stack_spa` 的 AC-10 要求浏览器级验收；本轮 `ui_probe` 配置为启用但运行时 MCP 未就绪，
   `verification_gap=1`，门禁按规则不放行。

4. **框架的 unfixable 短路是「正确处置」，但也让修复闭环无从发生**
   框架把 `environment_or_build` 判为 unfixable（`mas/leader.py:745`、docs/15 B 档），
   直接 forced fail 进报告，不再消耗回退预算——本轮 npm 缺失确实不是改代码能解决的，这个处置是对的。
   但它意味着 33 个缺陷不会进入 review/optimize 修复闭环；要让这轮变绿，必须先补环境，
   再让 optimize 真正改代码并重跑，这已经超出「验证一次 FoodLink」的范围。

**能通过的对照**：`docs/43` 记录的 task11 短链接需求是纯 Python SSR，同一套流水线拿到 18/18 门禁、49/49 测试；
本轮 SPA 的失败不是流水线「突然不会干活」，而是**交付语言/运行环境超出了当前验证能力**。

---

## 7. 证据索引与复现

| 用途 | 路径 |
|---|---|
| 需求输入 | `examples/requirements/foodlink_spa_fullstack.md` |
| run 01（方法名误判） | `workspace/runs/task12-foodlink-spa-01/events.jsonl` |
| run 02（900 秒超时） | `workspace/runs/task12-foodlink-spa-02/`；`workspace/hidden/` 无冻结产物 |
| run 03（完整链路） | `workspace/runs/task12-foodlink-spa-03/` |
| 运行摘要 | `workspace/runs/task12-foodlink-spa-03/run_summary.json` |
| 冻结契约 | `workspace/runs/task12-foodlink-spa-03/contract/requirement_contract.json` |
| 隐藏用例与元数据 | `workspace/hidden/task12-foodlink-spa-03/tests/test_hidden_foodlink.py`、`hidden_meta.json`、`test_manifest.json` |
| 方案两次 Prompt | `workspace/runs/task12-foodlink-spa-03/prompts/solution#1-developer-194828.md`、`solution#2-developer-200146.md` |
| 验收报告 | `workspace/runs/task12-foodlink-spa-03/reports/acceptance_report.md` |
| 生成代码 | `workspace/runs/task12-foodlink-spa-03/code/` |
| 实现自述（部分交付） | `workspace/runs/task12-foodlink-spa-03/artifacts/code_manifest/latest.json` → `notes`；摘录 `workspace/logs/code_manifest_notes.txt` |
| 方法名修复复放 | `workspace/logs/revalidate-task12-attempt03.txt` |
| 断点续跑脚本 | `workspace/logs/resume_task12_03.py` |
| 修复后全量回归 | `workspace/logs/unittest-full-after-fixes-20261031.log` |
| npm 启动探针 | `workspace/logs/npm-spawn-probe-20261031.txt` |
| TypeScript 语法检查探针 | `workspace/logs/node-check-ts-probe-20261031.txt`、`workspace/logs/ts_check_probe.ts` |

全量回归执行记录（真实输出）：

```
$ PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -q
Ran 705 tests in 178.967s

OK
```

聚焦回归：

```
$ python -m unittest tests.test_hidden_repair -q
Ran 14 tests in 0.350s
OK

$ python -m unittest tests.test_hardening.PermissionAndRetryTests.test_perception_brief_keeps_all_function_points_when_over_budget -q
Ran 1 test in 0.002s
OK
```

日志中的 `ResourceWarning` / `ConnectionAbortedError` 是测试内嵌 HTTP 夹具关闭时的噪声，不是用例失败；
最终结果以 `Ran 705 tests ... OK` 与 exit code 0 为准。

---

## 8. 建议（按优先级）

1. **短期验收用「语言匹配」的需求**：要验证流水线业务闭环，优先用 Python `backend_only` 或 `full_stack_ssr` 需求；
   SPA + NestJS + PostgreSQL 这套组合在现框架下无法给出绿结论。
2. **隐藏用例改为黑盒协议**：对被测服务生成「进程外 HTTP 用例」，由框架负责启动命令、健康检查、端口分配与清理；
   非 Python 交付不再要求 `from <pkg> import ...`。
3. **补齐工具链**：沙箱允许 `npm.cmd` / `npx` / `tsc` / `docker`，或提供 Node + PostgreSQL 容器；
   `self_check` 增加 TypeScript（`tsc --noEmit` / `vue-tsc`）映射。
4. **浏览器证据**：启用 Playwright MCP 或 headless Chrome，AC-10 才能留证。
5. **流程闭环**：环境补齐后，让可修复的 test 失败进入 optimize/fix 轮次，产出 `fix_summary` / `fix_test_report` 再验收；
   当前 33 个缺陷被 unfixable 短路，0 修复直接 fail，信息价值有限（短路本身对「环境缺口」是合理的）。
6. **超长生成治理**：隐藏用例 19 分钟属正常模型延迟；建议异步生成 + 断点续跑，而不是继续加墙钟上限。

---

## 9. 本轮改动清单（仅列本轮）

| 文件 | 改动 | 目的 |
|---|---|---|
| `mas/hidden.py` | 新增 `_reconcile_method_names` 并接入校验；元数据记录 `reconciled_methods` | 修复方法名「只差后缀」误判 |
| `mas/blackboard.py` | `brief()` 超预算先结构化压缩；新增 `_compact_spec` | 防止 Prompt 硬截断丢功能点 |
| `config/system.yaml` | `hidden_suite_stream_timeout_s: 900 → 1800`；`hidden_suite_total_budget_s: 2700 → 3600` | 容纳一次完整隐藏用例生成 |
| `tests/test_hidden_repair.py` | 新增 2 条回归 | 归一化生效 / 不误伤无关方法 |
| `tests/test_hardening.py` | 新增 1 条回归 | 13 个 FP 在预算内不丢失 |
| `workspace/logs/resume_task12_03.py` | 一次性断点续跑脚本 | 复用 run 03 检查点，避免重跑 19 分钟隐藏生成 |
| `docs/45-…（本文）` | 验收记录 | 如实留档 |

没有修改任何 FoodLink 业务代码；没有为了让门禁变绿改测试期望；工作树中其他未提交改动不属于本轮。

---

## 10. 最终判定

**FoodLink 本轮没有交付成功。** 流水线在「需求 → 方案」段已经证明能按真实需求工作（修复截断后 13/13），
但「实现完整度 + 测试通道 + 浏览器证据」三处缺口叠加，导致 0/10 验收标准可验证。
这不是一次可以包装成「基本完成」的运行；正确的下一步是补验证基础设施，而不是继续在同一环境下重跑。
