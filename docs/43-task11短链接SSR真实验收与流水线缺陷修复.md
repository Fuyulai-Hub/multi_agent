# 43 · task11 短链接 SSR 真实验收记录与流水线缺陷修复

## 0. 结论

需求 `web_shortlink_fullstack`（服务端渲染的短链接服务，前后端同一交付）已由多 Agent 流水线
真实交付并**验收通过**：

- 流水线：`verdict=pass`，验收门禁 **18/18** 条全部通过，报告 `reports/acceptance_report.md`；
- 冻结测试：**49/49** 通过（`test_hash=3d439f30…`，断言 82 条，`exit_code=0`，attestation 有效）；
- 隐藏测试：**29/29** 通过（门禁现场 + 框架外独立复跑）；
- 框架外独立验收：真实启动 `python -m slink`，按需求 FP-01~FP-08 / AC-01~AC-07 打真实 HTTP，
  **40/40 全过**；
- 真实浏览器：SSR 页面渲染、表单 POST 创建短码、访问短码 302 跳转、列表回显均通过
  （无头 Chrome DOM + 截图，见 `.tmp-verify/browser-evidence/`）。

验收不是放水：本轮定位并最小修复了 **4 个会制造「假失败/错判」的框架缺陷**，
并用「同一冻结套件、同一 test_hash，修复前后 35/49 → 49/49」证明失败来自执行器而非交付物。
未为了让用例变绿改写业务逻辑；唯一的实现类修改是补全文档字符串（代码质量契约，见 §4.5）。

## 1. 需求与交付物

- 需求快照：`workspace/runs/task11-slink-fullstack-01/input/requirement.md`
- 交付目录：`workspace/runs/task11-slink-fullstack-01/code/`
- 形态：`full_stack_ssr`（`python -m slink` 启动，服务端渲染管理页 + JSON API + JSON 文件持久化）
- 依赖：纯标准库（无第三方运行时依赖）
- 实测文件：11 个 `slink/*.py`（770 行）+ `tests/__init__.py` + `tests/test_all.py`（437 行）

启动方式（与需求一致）：

```bash
cd workspace/runs/task11-slink-fullstack-01/code
python -m slink --host 127.0.0.1 --port 0 --data /tmp/slink-state.json
# 控制台打印真实监听地址，端口由 OS 分配
```

功能点映射（评审按 `file::symbol` 逐项 AST 核验，8/8 有效）：

| FP | 实现符号 | 需求能力 |
|---|---|---|
| FP-01 | `slink/store.py::Store.create` | 6 位短码、同 URL 幂等、持久化 |
| FP-02 | `slink/store.py::Store.lookup` | 查询/过期/TTL |
| FP-03 | `slink/api.py::handle_create_link` | JSON/表单创建 201 |
| FP-04 | `slink/api.py::handle_redirect` | 短码 302 跳转 + 计数 |
| FP-05 | `slink/api.py::handle_list_links` | 列表、字段、`?q=` 过滤 |
| FP-06 | `slink/render.py::render_index` | SSR 首页（表单 + 列表） |
| FP-07 | `slink/api.py::handle_delete_link` | 删除 200 / 不存在 404 |
| FP-08 | `slink/validation.py::validate_url` | URL 校验与 4xx 分类 |

## 2. 最终验收证据（真实输出）

### 2.1 运行摘要

```
run_id=task11-slink-fullstack-01  verdict=pass  steps=14  rollbacks=4
bugs_total=14  bugs_fixed=14  canceled=False
report=reports/acceptance_report.md
```

### 2.2 门禁（18/18，关键项）

```
隐藏用例通过（契约冻结后独立生成）        True
用例全部通过                              True
测试证据通过独立验证                      True
冻结测试 hash 非空                        True
功能点覆盖完整 / 异常用例覆盖 / 边界用例覆盖 True
阻塞性问题清零                            True
无未关闭缺陷                              True
待澄清问题均已闭环                        True
测试证据链完整（框架重验）                True
需求契约已冻结且未被篡改                  True
审查结论绑定最终代码                      True
```

### 2.3 冻结测试凭证（`verification_attestation.json`）

```
valid        = true
test_hash    = 3d439f30b193f84594b37125c0ed165038ad1857f8a907c16f0f3fd212c231d9
code_hash    = 0fdbdfa4db50937a9a000522c4941480bdd8c138a7310492cfba8adea00da295
req_hash     = ddfa8fd05d5c06d35ffe913ac6e7e3f014aa3096452ab21c34b97c2f14400670
total=49 passed=49 failed=0 assertions=82 exit_code=0
```

### 2.4 隐藏套件（框架外独立复跑）

```
cd workspace/hidden/task11-slink-fullstack-01/frozen_tests/tests
PYTHONPATH=<run>/code python -m unittest -v test_hidden_slink
----------------------------------------------------------------------
Ran 29 tests in 11.767s
OK
```

### 2.5 独立 HTTP 验收（不依赖框架产物）

```
python .tmp-verify/verify_slink.py workspace/runs/task11-slink-fullstack-01/code
SERVER BANNER: slink listening on http://127.0.0.1:53641
...
PASS  FP-05 跳转后 hits>=1   [1]
PASS  AC-04 用户输入被 HTML 转义
PASS  FP-08 ftp 协议 -> 4xx   [status=400]
PASS  FP-07 删除后跳转 404   [status=404]
PASS  AC-05 并发创建 10 条短码唯一   [10]
PASS  AC-07 HTTP 重启后短码仍可跳转   [status=302]
============================================================
TOTAL 40  PASS 40  FAIL 0
```

### 2.6 真实浏览器证据

内置浏览器（真实 Chrome）打开 `http://127.0.0.1:8137/`，读取 SSR 表单
（`method=post action=/`，字段 `name=url`），提交后页面回显新短码 `t43mwx`；
浏览器内访问 `/t43mwx` 后地址栏变为 `http://127.0.0.1:8137/?from=browser-form`（302 生效）。
无头 Chrome 另存 DOM 与截图：

- `.tmp-verify/browser-evidence/page.html`（`t43mwx` 出现在列表中，访问次数 1）
- `.tmp-verify/browser-evidence/page.png`

### 2.7 框架自身回归

```
python -m unittest discover -s tests -t .
Ran 693 tests in 167.352s
OK
EXIT=0
```

## 3. 运行时间线（含真实中断/续跑）

| 时间 | 节点 | 迭代 | 结果 | 说明 |
|---|---|---|---|---|
| 07:15 | intake | 1 | pass 08:55 | 需求拆解 + 契约冻结（LLM 单节点约 100 分钟） |
| 09:01 | solution | 1 | pass | 选型 + 设计 |
| 09:06 | implement | 1 | pass | 首版交付 |
| 09:06→09:20 | test | 1 | **fail** | 冻结前校验报 `case_without_method`（框架缺陷 R3，§4.3） |
| 09:29 | optimize | 1 | pass | 重新生成测试（直接继承 TestCase 的 41 条套件） |
| 09:30→09:36 | test | 2 | 中断 | 进程被人工终止（诊断中），09:36 续跑 |
| 09:43 | test | 2 | pass | 41/41，冻结 |
| 09:47 | review | 1 | **fail** | 模型自报 pass，框架重算 blocking=38（34 docstring + 4 覆盖误判 R4） |
| 10:00 | implement | 2 | pass | 补全 11 个模块 docstring；业务逻辑未改 |
| 10:02 | test | 3 | 中断 | 进程被人工终止（续跑诊断） |
| 10:10 | test | 3 | **fail** | `测试冻结清单签名无效`（框架缺陷 R1，§4.1） |
| 10:21 | test | 4 | 续跑 | 重新绑定 run 级密钥并重封装受影响签名后，人工把检查点回拨到 test |
| 10:28 | test | 4 | **fail** | 49 条中 14 条 `AttributeError: 'HttpServerTests' object has no attribute 'srv'`（框架缺陷 R2） |
| 10:28 | optimize | 2 | 中断 | 为避免被假失败驱动改坏正确代码，人工终止；检查点回拨 test |
| 10:35 | test | 5 | **pass** | 修复 runner 后同一冻结套件 49/49 |
| 10:49 | review | 2 | pass | blocking=0，19 项 minor/info |
| 10:49 | acceptance | 1 | pass | 隐藏 29/29、契约/证据/审查绑定全部通过 |
| 10:54 | report | 1 | pass | 生成验收报告，`verdict=pass` |

## 4. 失败分类、根因、修复与验证

### 4.1 R1（环境/构建类 → 框架实现）：dev 验签密钥随进程丢失

- 现象：进程重启后 test 节点报「测试冻结清单签名无效（文件被篡改或来自其他验证器实例）」，
  同轮 review/acceptance 也无法验签。
- 根因：dev 模式使用进程级随机密钥 `_PROCESS_SECRET = secrets.token_hex(32)`，重启即换钥；
  而契约、冻结测试清单、隐藏套件、attestation 都签在同一把键上。
- 修复（`mas/verifier.py`）：新增 `bind_run_secret()` 与 run 级持久密钥
  `<run_dir>/.verifier_secret`（0600，读不到则原子创建）；优先级
  **env 密钥 > run 密钥 > 进程密钥**；release 模式仍强制 `MAS_VERIFIER_SECRET`（≥32 字符，缺则拒绝启动）。
  `mas/leader.py` 在 run_dir 建好、任何签名发生之前调用 `bind_run_secret(run_dir, config)`。
- 验证：`tests/test_verifier.py::RunSecretPersistenceTests`（模拟重启后 manifest 仍可验签、密钥不轮换）。
- 恢复历史产物（仅 dev）：`.tmp-verify/reseal_task11.py` 在内部 hash/文件摘要/run_id 全部重算一致后
  才重签契约、隐藏清单与副本；过期可见清单/凭证归档到
  `evidence/stale-dev-key-20261031T102052/`，不删除、不伪造。

### 4.2 R2（代码实现类 → 框架执行器）：逐条 `test.run()` 绕过类级夹具

- 现象：iter04 的 49 条用例中 14 条 error，全部是
  `AttributeError: 'HttpServerTests' object has no attribute 'srv'`。
- 根因：`mas/verify/runner.py` 对清单内每条用例单独调用 `TestCase.run(result)`。
  `unittest` 的 `setUpClass/tearDownClass/setUpModule` 只由 `TestSuite.run()` 驱动，
  单独跑 `TestCase.run()` 不会执行类级夹具——`HttpServerTests.setUpClass` 创建真实 HTTP 服务
  的动作从未发生，14 条依赖 `cls.srv` 的用例全部假失败。
- 修复：改为把清单内用例装进一个 `unittest.TestSuite` 统一执行，新增 `_SuiteResult`
  逐用例记录状态/耗时/失败摘要；类/模块级夹具错误按
  `setUpClass (module.Class)` 归属回该组用例，执行完才暴露的 `tearDownClass/tearDownModule`
  错误归到该组最后一条用例，避免「49 条全 pass 但夹具失败被吞掉」。
- 验证：
  - 同一冻结清单、同一 `test_hash=3d439f30…`，修复前 35/49、修复后 **49/49**；
  - 框架外直接跑 runner：`total=49 passed=49 failed=0`（`.tmp-verify/runner_after_fix.json`）；
  - 新增回归 `tests/test_runner_authority.py::RunnerClassFixtureTests` 3 条
    （夹具正常执行 / setUpClass 失败归属 / tearDownClass 失败不静默）；
  - 全仓 693 测试 OK。

### 4.3 R3（代码实现类 → 框架 AST 识别）：间接继承的 TestCase 不被识别

- 现象：iter01 test 失败摘要 `case_without_method:test_functional_create_returns_six_char_code…`
  （冻结前覆盖率/绑定校验认为这些方法不存在）。
- 根因：`mas/verifier.py` 与 `mas/hidden.py` 各自维护一份 AST 判定，只认
  `class X(unittest.TestCase)` / `class X(*TestCase)` 这种**直接继承**；
  生成的套件用 `class BaseHttpTest(unittest.TestCase)` + `class HttpServerTests(BaseHttpTest)`
  的间接继承时，全部方法被判为不存在。
- 修复：`TestVerifier.iter_test_methods(tree)` 统一实现「基类名以 TestCase 结尾或传递闭包可达
  unittest.TestCase」的类识别；tester 与 hidden 改为复用同一实现，删除重复逻辑。
- 验证：冻结清单 49 条（含间接继承的 HTTP 测试类）正常冻结与执行；全仓测试通过。

### 4.4 R4（代码实现类 → 框架审查口径）：HTTP/SSR 功能点被误判「未覆盖」

- 现象：review iter01 `review_mapping_checked` 判 FP-03/04/05/07 为
  `fp_mapping_not_covered_by_passing_test`，框架 blocking=38 → 回退研发。
- 根因：审查只承认「冻结测试 import 并使用了该符号」。HTTP/SSR 交付的功能点经公开
  `create_server + 真实 HTTP 请求` 验证行为，测试不会 import `handle_*` 内部函数，
  41 条全过的套件仍被误判。
- 修复（`mas/agents/reviewer.py::_attested_fp_ids`）：在测试证据有效时，读取**已签名**的
  `test_manifest.json` 的 `case_meta`，若其用例集合与 `test_report.test_ids` 完全一致，
  则以 `fp_id` 绑定作为行为级覆盖证据；集合不一致时保守返回空集，维持原严格口径。
- 验证：review iter2 的 `mapping_audit` 8/8 有效、blocking=0；
  新增/更新 `tests/test_verifier.py`、`tests/test_tester_coverage_gate.py` 相关断言。

### 4.5 交付物侧真实问题（需求质量约定）：34 处函数缺文档字符串

- review iter01 的确定性规则命中 34 个公开/私有函数与方法缺少 docstring（框架 blocking 38 = 34 + 4 误判）。
- implement iter02 为 11 个模块逐一补齐 docstring，业务逻辑未改；review iter2 只剩 14 minor + 5 info。
- 这说明审查的确定性规则在「质量契约」上有效，并非只服务于测试通过。

### 4.6 需求理解 / 方案设计：未发现失败证据

intake 与 solution 均一次通过；方案要素（stdlib、SSR、JSON 文件持久化、端口 0、XSS 转义、
幂等短码、TTL、2048 字符阈值）与最终独立验收一致。本轮失败全部落在框架执行/取证链路上。

## 5. 修复清单（最小改动）

| 文件 | 改动 | 关键行 |
|---|---|---|
| `mas/verifier.py` | run 级持久密钥、`bind_run_secret`、密钥优先级与 release 校验 | 79/106/140 |
| `mas/leader.py` | 建 run_dir 后、任何签名前绑定密钥 | 433 |
| `mas/verify/runner.py` | `TestSuite` 统一执行 + `_SuiteResult` 逐用例记录 + 夹具错误归属 | 45/323/380 |
| `mas/verifier.py` | `iter_test_methods` 传递闭包识别 TestCase | 888 |
| `mas/agents/tester.py` / `mas/hidden.py` | 复用统一 AST 用例识别 | 900 / 164 |
| `mas/agents/reviewer.py` | `_attested_fp_ids` 行为级覆盖证据（签名清单 + 用例集合一致） | 525 |
| `tests/test_runner_authority.py` | 新增类级夹具回归 3 条 | 130+ |
| `tests/test_verifier.py` | 密钥持久化回归 2 条 + 覆盖口径回归 | — |

依赖：无新增。未改前端/面板代码（docs/42 的功能属于另一任务）。

## 6. 为什么可以判定「不是假绿」

1. **同 hash 对照**：iter04 与 iter05 使用同一冻结套件（`test_hash=3d439f30…`，文件摘要一致），
   唯一变量是 runner 的执行方式；结果 35/49 → 49/49，失败归因闭合。
2. **证据可重算**：门禁 `verify_evidence` 用当前工作区重算 `code_hash / requirement_hash /
   visible_suite_hash / hidden_suite_hash / env_hash`，并与签名凭证逐项比对；契约、审查
   `code_hash` 也绑定最终代码。
3. **框架外复验**：独立 harness 只依赖交付目录，真实启动服务并打 40 条 HTTP 断言；
   隐藏套件在框架外以全新解释器复跑 29/29。
4. **审查绑定**：review iter2 只对最终 `code_hash` 签字；验收门禁第 17 条「审查结论绑定最终代码」通过。
5. **缺陷闭环有证据**：14 条历史缺陷在 iter05 真实执行到同名用例并转为 fixed，
   `open=0`；不是「一条都没跑成」式的自动闭环。

## 7. 人工干预与能力边界（如实披露）

- **人工终止**：本轮两次只终止了已解析出的目标进程（PID 38784、30544），未按进程名批量杀进程。
  原因：测试/优化节点由框架缺陷驱动，继续运行会改写本已正确的交付代码。终止后检查点
  `pending_reason` 记录了框架原因，并回拨到 `test` 重测。
- **回退预算**：test 节点 3 次回退全部由框架缺陷触发（R3/R1/R2），已用满 `max_attempts=3`；
  若 iter05 仍失败将触发强制失败收尾。本轮通过依赖的是修复而非新增预算。
- **dev 密钥恢复**：仅 dev 模式适用。生产/release 模式缺 `MAS_VERIFIER_SECRET` 会直接拒绝启动，
  不存在「重签历史证据」的路径。
- **沙箱隔离**：本地后端下测试代码与 runner 同进程，理论可篡改内存/结果文件；本机制解决的是
  「执行集合与结果汇总权」，不是恶意代码隔离。容器后端才有只读挂载语义。
- **清单文件级存在性**：`code_manifest.files` 自报的 `tests/test_regression.py` 实际被冻结测试
  的「新增文件清理」移除；审查报告的 `reviewed_files` 原样列出了该自报文件。8 个 fp_mapping
  目标文件均真实存在，故不影响功能点证据；但框架目前没有做清单文件的全局存在性校验。
- **浏览器证据**：内置浏览器面板未挂载可见视口，指针 `click`/截图不可用；表单提交通过页面内
  `form.requestSubmit()` 完成（真实浏览器、真实 POST），并另用无头 Chrome 取 DOM 与截图。
  这不是「模拟 HTTP」，但也不是人工点击，特此说明。
- **SSR UI 证据**：`ui_evidence` 对 SSR 形态只做探针级检查（asserts=0）；门禁中的浏览器断言
  只对 `full_stack_spa` 生效。本任务的页面有效性由独立 harness + 浏览器实测补足。

## 8. 复现命令

```bash
# 1) 框架测试
python -m unittest discover -s tests -t .

# 2) 直接执行冻结套件（指向任意 manifest）
python mas/verify/runner.py \
  --manifest workspace/runs/task11-slink-fullstack-01/test_manifest.json \
  --root workspace/runs/task11-slink-fullstack-01/code \
  --out /tmp/runner_result.json

# 3) 框架外 HTTP 验收
python .tmp-verify/verify_slink.py workspace/runs/task11-slink-fullstack-01/code

# 4) 隐藏套件
cd workspace/hidden/task11-slink-fullstack-01/frozen_tests/tests
PYTHONPATH=../../../../runs/task11-slink-fullstack-01/code python -m unittest -v test_hidden_slink

# 5) 手动续跑（CLI 无 --resume，可用脚本）
python .tmp-verify/resume_task11.py
```

产物索引（均在 run 目录下）：`reports/acceptance_report.md`、
`artifacts/acceptance_gate/latest.json`、`artifacts/test_report/latest.json`、
`artifacts/hidden_test_report/latest.json`、`artifacts/bug_reports/latest.json`、
`verification_attestation.json`、`test_runs/iter05/result.json`、`verify_io/iter05-tester/`。
