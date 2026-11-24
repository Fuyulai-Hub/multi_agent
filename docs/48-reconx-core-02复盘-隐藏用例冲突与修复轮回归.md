# reconx-core-02 未通过复盘：隐藏用例与契约冲突 + 修复轮盲改回归

- 运行：`reconx-core-02`（需求 `reconx_core_backend`，profile=base）
- 结论：未通过｜22 步｜回退 7 次｜缺陷 10（闭环 1）｜5338.34s｜forced_fail
- 复盘时间：2026-11-07
- 关联代码：`mas/agents/developer.py`、`mas/agents/base.py`、`mas/leader.py`、`mas/hidden.py`、
  `mas/providers/offline.py`、`config/roles/developer.yaml`、`config/system.yaml`

---

## 1. 结论先行

这次失败不是「研发不够努力」，而是三件事叠加：

| # | 事实 | 影响 |
|---|------|------|
| 1 | **隐藏套件里有 3 条用例与冻结契约直接冲突**，任何符合契约的实现都过不了 | 验收门禁不可满足，回退预算必然耗尽 |
| 2 | **研发的定向回归对隐藏缺陷是无效动作**：拿隐藏用例 id 去过滤可见冻结清单，必然 `no_test_id_matched`（runner 退出码 2） | 「没跑」被记成「回归失败」，节点整轮判 fail，报告据此误判根因 |
| 3 | **修复轮看不到当前源码**，只能凭需求 + 缺陷症状整份重写 | 把已通过的用例改坏（新增 3 个缺陷）、引入 `function_too_long` 被审查驳回，回退次数被放大 |

第 1 条让这个任务在现有隐藏套件下**不可能通过**；第 2、3 条把「本来 1 轮能收敛」变成「7 轮回退」。
第 2、3 条已修复；第 1 条的成因（隐藏用例生成缺一致性约束）已加提示约束，并把「连续两轮同用例失败」
变成门禁里的显式诊断，不再把责任默记到研发。

---

## 2. 证据链（全部来自 `workspace/runs/reconx-core-02/`）

### 2.1 主线时间线

| 时间 | 事件 |
|------|------|
| 07:57:34 | 可见套件冻结并通过：80/80，缺陷池 open=0，审查 pass |
| 07:58:43 | 隐藏门禁首跑：67 条，62 通过 / **5 失败**；5 条失败回灌缺陷池（`source=hidden`） |
| 08:03:48 | optimize：修复 5 条隐藏缺陷，「定向回归 **0/5 通过**」，节点 fail |
| 08:07:54 | review：「框架复核否决」，阻塞项 `function_too_long`（`reconx/matcher.py::reconcile` 73 行 > 60） |
| 08:11:58 | implement（fix）：**整份重写 6 个文件 618 行** |
| 08:11:59 | 可见套件 78/80，新增 2 个缺陷（crit `parse_internal_csv` 合法输入返回 0 条） |
| 08:17:43 | optimize：修复 7 个缺陷，定向回归 2/7；可见套件回到 80/80 |
| 08:20:01 | 审查 pass；隐藏门禁：64/67，**仍 3 条失败** |
| 08:24:59 | optimize：修复 5 条隐藏缺陷，定向回归 0/5；可见套件掉到 76/80，新增 3 个缺陷 |
| 08:25:00 | 回退预算耗尽，强制失败 |

### 2.2 「定向回归 0/N」的真实原因

`fix_summary` v4 里 5 条回归证据长这样：

```json
{"cmd": "python .mas_verify/runner.py --manifest ...\\verify_io\\iter05-developer\\test_manifest.json
         --root ...\\code --out ... --only HiddenFp03Tests.test_boundary_reconcile_config_error_catch_as_value_error",
 "exit_code": 2, "failed": 0, "stdout_tail": "", "stderr_tail": ""}
```

- 这个 manifest 是**可见**冻结清单（80 条业务用例），里面没有 `HiddenFp03Tests.*`；
- `runner.run_manifest()` 过滤后 `selected` 为空 → `result["error"]="no_test_id_matched"` → 退出码 2、用例数 0；
- `developer._apply_fix` 把 `bool(exit_code==0) and total>0` 记为 false，于是「一行都没跑」变成「回归失败」。

验收报告 §3.3 写的是「runner 在 `--only` 模式下执行异常（runner 配置或路径问题）」——**这是模型猜测，不是实测结论**；
真实原因是筛选集合为空，runner 行为本身是对的。

### 2.3 3 条隐藏用例为什么不可满足

| 用例 | 断言 | 契约原文 | 判定 |
|------|------|----------|------|
| `HiddenFp03Tests.test_functional_reconcile_time_gap_values` | `paid_at=00:01:00` 时 `time_gap_seconds == 3600` | FP-03：「`time_gap_seconds` 为两侧 `paid_at` 的**绝对秒差**」 | 测试算错：00:00:00 → 00:01:00 是 60 秒。可见套件 `tests/test_all.py` 同一场景断言 60，两套互相矛盾 |
| `HiddenFp07Tests.test_functional_reconcile_primary_selection_shuffle` | 渠道 row_no=2/4/5 同 `paid_at`，`matched[0].channel.row_no == 4` | FP-04：「组内按 `(paid_at, row_no)` 升序，**第一条为主记录**」 | 测试与契约冲突：主记录是 row_no=2（实测实现返回 2，被判失败） |
| `HiddenFp07Tests.test_exception_parse_channel_csv_quote_extra_char` | `'trade_no,amount\nT1,"10.00"x,1\n'` 期望 `ParseError(line=2, column=11)` | FP-01：「必需列缺失抛 `ParseError(line=1, column=0)`」 | 用例前提非法：表头只有 2 列（缺 currency/paid_at/merchant_id），错误在表头校验处触发，断言永远到不了 |

用本次新增的诊断函数对两轮真实隐藏报告求交集，恰好命中这 3 条：

```
第1轮失败: 5 第2轮失败: 3
  连续失败 -> HiddenFp03Tests.test_functional_reconcile_time_gap_values
  连续失败 -> HiddenFp07Tests.test_exception_parse_channel_csv_quote_extra_char
  连续失败 -> HiddenFp07Tests.test_functional_reconcile_primary_selection_shuffle
```

### 2.4 隐藏用例信息泄露造成的「按名字改代码」

隐藏缺陷回灌时把**用例方法名**和**原始断言差值**一起写进 `bug_reports`，研发在 optimize#5 的推理里逐条引用：

- 看到 `..._config_error_catch_as_value_error` → 把 `ReconError` 改成继承 `ValueError`，
  而契约写的是 `ReconError(Exception)`、`ConfigError(ReconError, ValueError)`——正确修法只改 `ConfigError`；
- 看到 `60 != 3600` → 试图让 60 秒的时间差返回 3600。

即：隐藏门禁不仅不可满足，还在**主动诱导**违反契约的实现。

---

## 3. 本次改动（最小改动，5 处）

| # | 文件 | 改动 | 目的 |
|---|------|------|------|
| 1 | `mas/agents/developer.py::_apply_fix` | `source=hidden` 的缺陷不再执行可见清单定向回归：标 `regression_ok=None` + `deferred="hidden_gate"`，不计入本轮回归结论，发 `bug_fix_deferred` 事件 | 消除「0/5 假证据」与由此产生的节点 fail；隐藏缺陷仍由门禁复跑闭环（语义未变） |
| 2 | `mas/agents/base.py::prompt_extra_directive`（默认空串）+ `developer.py` 覆写 | 返工/修复轮把沙箱里**现存交付源码**注入 directive（跳过 `tests/`，单文件 24000 / 合计 60000 字符上限，可配置置 0 关闭） | 让修复变成增量改动，而不是整份重写 |
| 3 | `mas/agents/developer.py::_bug_view` | `source=hidden` 的缺陷在研发视图里隐去用例方法名，换成 `hidden-<hash8>`；可见缺陷保持原样 | 切断「按用例名反推实现」的诱导路径 |
| 4 | `mas/leader.py::_suspect_hidden_cases` + 门禁 criteria | 同一隐藏用例连续 ≥2 轮失败 → 发 `hidden_cases_suspect` 事件，并把「疑与契约冲突，需复核用例」写进该判定项的 reason | 把「实现修不好」与「用例写错了」在报告里分开，不再默认为研发责任 |
| 5 | `mas/hidden.py::_PROTOCOL` + `config/roles/developer.yaml` | 隐藏生成协议新增 3 条硬规则（期望值须能由用例自身输入机械推导、不得与契约样例冲突、反例用例的输入仍须满足格式前提）；研发角色提示词新增「契约优先、禁止按用例名/断言数值反推」「返工只改必要文件」 | 从源头降低冲突用例概率，并约束返工行为 |

配置项（`config/system.yaml::runtime`）：`fix_source_per_file_chars: 24000`、`fix_source_total_chars: 60000`。

---

## 4. 验证

| 验证 | 结果 |
|------|------|
| 全量单元/集成测试（`python -m unittest discover -s tests -t .`） | **Ran 774 tests, OK**（209s），含离线端到端流水线（12 步、缺陷 5→闭环 5、验收通过） |
| 新增回归 `tests/test_fix_loop.py`（9 例） | 通过：隐藏缺陷零次调用 runner、可见缺陷仍走回归、源码注入含真实文件且不含 tests/、预算置 0 关闭、其他角色 Prompt 不变 |
| 新增回归 `tests/test_gate_reconciliation.py::SuspectHiddenCaseTests`（3 例） | 通过：两轮交集识别、无历史不误报、空 test 字段忽略 |
| 真实产物重建（`.workbuddy/verify_fix_prompt.py`） | 用 reconx-core-02 的 `code_manifest` + 真实沙箱代码重建 optimize 轮注入段：**22114 字符**，含 6 个交付文件真实内容，`### tests/` 0 处；隐藏缺陷视图无 `test` 字段 |
| 真实隐藏报告交集（本次新增诊断跑真实 v1/v2） | 精确命中 2.3 节的 3 条冲突用例 |

**未完成的验证**：真实 LLM 全链路复跑（reconx-core-03）。本机没有模型凭据
（`config/credentials.yaml` 的 `llm` 段为空，`MAS_LLM_API_KEY` 未设置），无法发起真实调用。复跑命令：

```powershell
$env:MAS_LLM_API_KEY = "<你的 Key>"
C:\Python314\python.exe -m mas.cli run -r examples/requirements/reconx_core_backend.md --run-id reconx-core-03
```

预期：优化轮不再出现 `0/N 定向回归`；返工只改必要文件；若仍有隐藏用例连续两轮失败，门禁会显式标注
`hidden_cases_suspect` 而不是把它记成研发缺陷。**该任务的通过与否仍取决于重新生成的隐藏套件是否还有冲突用例**——
这一点不是流水线能自行修复的，见第 6 节。

---

## 5. 其他未通过案例归类（89 个历史运行：28 通过 / 61 未通过）

其中 **29 个在 5 步内即失败**（多为 intake 阶段），**32 个跑进多轮回退后耗尽预算**。

### 5.1 单步即失败（29 个，0~2 步）

| 类别 | 数量 | 典型 run | 真实原因 | 处理建议 |
|------|------|----------|----------|----------|
| 隐藏套件生成/冻结失败 | 12 | `reconx-e2e-01`、`verify-comp-01/02`、`task04-cpp-probe-05/06/07`、`task06-tally-lean-02/03`、`task09-miniledger-03`、`task12-foodlink-spa-01`、`20261024-192940-e1f1` | 结构协议未对齐：`hidden_file_not_object`、`hidden_cases_missing`、`hidden_case_missing:FP-xx`、`hidden_case_duplicate_method`、`hidden_case_method_missing`、无可枚举用例；重试 2~3 次仍不过就整轮作废 | ① `files` 元素为裸字符串/裸代码时做容错归一（当前直接丢弃整份输出）；② 复用已落盘的 `rejected/hidden_suite_iterXX_attemptYY.json` 做续修；③ 现在 `hidden_suite_max_attempts=3`，可对「只差命名对齐」的错误放宽（已有 `_reconcile_method_names` 先例） |
| 需求规格解析不出功能点 | 4 | `20261009-102140-cd70`、`20261011-*`、`mq-smoke-161603`、`20261105-160411-8ad9` | 短文本/探针需求走离线 Provider 或模型输出空壳 | 对 `inline-requirement` 短需求加「功能点≥1」的修复重生成 |
| 契约校验失败 | 1 | `task09-miniledger-01` | `source_span_quote_mismatch:FP-08:C-13`（条款引用与原文不一致） | product 侧对 clause 引用做逐字回查后重生成 |
| 网关/预算 | 2 | `task12-foodlink-spa-02`、`20261028-222532-7675` | 调用预算耗尽 / 连接被拒 | 属环境与预算参数，已有降级链路 |

### 5.2 多步后回退耗尽（32 个，下表按主因归类）

| 类别 | 典型 run | 真实原因 | 处理建议 |
|------|----------|----------|----------|
| 修复轮盲改导致回归 | `reconx-core-02`、`verify-comp-04/05`、`task04-cpp-probe-03/04`、`20261019-013517-b877`（29 缺陷） | 与本次同因：修复无源码基线，整份重写 | **本次已修**（源码注入 + 隐藏缺陷回归不再假失败） |
| 隐藏用例与契约冲突 / 隐藏套件整体不可执行 | `20261025-063338-26c1`（13 条 `account_id 格式非法`）、`task08-orderpricing-gate-01`（143 条全部 `test module import failed`）、`reconx-core-02` | 用例本身错误或套件导入失败，被当成逐条缺陷塞给研发 | 建议两步：① 套件级失败（import/collection 全灭）单独立单，不进逐条缺陷池；② `hidden_cases_suspect` 已达 2 轮即提示复核用例（本次已加诊断，尚未阻断回退） |
| 覆盖率门禁不达标 | `20261015-015327-b0c2`、`skillcheck-real-01`、`20261024-233917-897d` | 三类用例（functional/exception/boundary）覆盖 rate < 1.0，多为修复轮重写 `tests/` 导致 `case_meta` 绑定丢失 | 与源码注入同源；另可在 tester 冻结前对「重写测试文件」做一次元数据重建 |
| 非 Python 工具链缺失 | `task04-cpp-probe-03/04`、`task04-cpp-probe-lean` | 本机无 g++/clang，`unsupported` fail closed | 设计如此；要过就跑容器镜像或装 g++ |
| SPA 无浏览器证据 | `task12-foodlink-spa-03`（221143s）、`task07-spa-probe-01` | 本机无可用浏览器 MCP 通道 | 已由 docs/36 的 `delivery_shape` 声明通道规避；或接入 playwright |
| 验收被框架复核否决 | `skillcheck-real-01`、`task08-*` | 阻塞项为确定性静态规则（函数过长、docstring 缺失等） | 已由「返工可见源码 + 只改必要文件」降低复发 |

### 5.3 优先级建议

1. **P0（已做）**：修复轮源码基线 + 隐藏缺陷回归语义 + 隐藏用例名脱敏 + 连续失败诊断。
2. **P1**：隐藏套件生成的两类容错（裸字符串 `files` 归一、rejected 快照续修）——直接消掉 12 个「0 步死」。
3. **P1**：隐藏门禁的失败分级——套件级（import/collection/无可枚举用例）与用例级分离，前者不进研发缺陷池。
4. **P2**：隐藏用例一致性自检（生成后、冻结前，由 product 用契约逐条复核期望值；冲突用例走重生成而不是交给研发）。
5. **P2**：覆盖率绑定在测试文件被重写后自动重建 `case_meta`。

---

## 6. 诚实边界

- 本次没有做真实 LLM 全链路复跑（无凭据），`reconx-core-02` 的隐藏套件是**已冻结**的产物，
  本次改动不会改变它的内容；要让该任务真正通过，需要重新生成隐藏套件（新 run_id）或人工复核那 3 条用例。
- 隐藏用例生成的一致性目前只靠提示词约束（第 3 节第 5 项），没有确定性校验兜底；
  P1/P2 的容错与自检尚未实现。
- 第 3 节第 1 项把隐藏缺陷的「回归结论」改为 `None`，`fix_test_report.all_pass` 因此只覆盖研发侧可验证的缺陷；
  隐藏缺陷的真实结论仍由验收门禁给出（`verified_by=hidden_gate`），不是放宽验收。
