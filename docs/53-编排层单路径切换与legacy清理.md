# 53 · 编排层单路径切换与 legacy 清理

> 对象：`G:/mycv/cragent/multi_agent`
> 依据：`docs/50-LangGraph重构技术方案.md`、`docs/51-LangGraph编排层落地与验证记录.md`
> 结论：**2026-11-25 起编排层固定为 LangGraph 单路径；`runtime.orchestrator` 开关与 leader 里的 180 行 legacy 循环已从代码删除。全量回归 788 passed / 16 skipped（基线 787 / 16），真实 CLI 双跑事件脚本与代码树 hash 一致。**

---

## 0. 三句话摘要

1. 删除的是「驱动下一轮」这段代码，不是编排能力：`Leader._execute_node`（节点执行）、`orchestration.decision.decide`（分支/回退/权限/escalation）、`Leader._cancel_requested`（节点边界取消）全部保留，图节点每轮调用它们。
2. langgraph 从可选依赖变为运行必需：缺失时 `Leader.run` 抛 `ConfigError` 并提示安装，不再静默跑另一条路径。
3. 原 A/B 对照改为**单路径双跑对照**（同配置、独立 workspace），继续锁死「事件脚本、回退记录、门禁裁决、产物版本、code 树 hash」逐项一致。

---

## 1. 为什么现在切

- `docs/51 §3` 第 6 条记录的 P4（删 legacy 循环）当时未做，前提是「A/B 连续 5 次一致 + 线上两周」。本次由需求方明确取消该前提，要求「默认使用重构后的链路，并清理没有用的代码」。
- 已有的等价性证据（docs/51 §2）：进程内 A/B 与 CLI 层 A/B 中，两条路径的节点序列、事件脚本（类型+节点+轮次）、回退记录、19 个产物版本集合、`code/` 树 hash 全部相同，仅 `duration_ms` 不同。
- 因此本次不再新增语义改动，只做「删分支 + 换默认 + 清残留」，并用重复性对照覆盖删除后的唯一路径。

---

## 2. 删改清单（逐文件）

| 文件 | 动作 | 说明 |
|---|---|---|
| `mas/leader.py` | 删 `if orchestrator == "langgraph" … else: while …` 整段 | 固定调用 `orchestration.runner.run_graph`；删除模块级 `decide` 导入（唯一调用点在旧循环里） |
| `mas/leader.py` | 保留 `_execute_node` / `_cancel_requested` | 图节点（`orchestration/nodes.py`）仍调用；取消契约不变 |
| `mas/config.py` | `runtime.orchestrator` 从「枚举校验」改为「废弃键守卫」 | 键缺失或值为 `langgraph` 通过；其他值（含 `legacy`、拼错值）直接 `ConfigError`，不静默降级 |
| `config/system.yaml` | 删除 `orchestrator: "legacy"` 与双轨注释 | 改为单路径说明 + 依赖说明 |
| `requirements.txt` | `langgraph>=1.2,<2.0`、`langgraph-checkpoint-sqlite>=3.0` 从注释提为必需 | 与「缺失即 ConfigError」一致 |
| `mas/orchestration/*.py` | 仅改 docstring 中「两条路径/legacy」表述 | 6 个模块逻辑零改动（state/decision/nodes/graph/checkpoint/runner） |
| `tests/test_orchestration_ab.py` | 删除，替换为 `tests/test_orchestration_determinism.py` | 原对照对象（legacy）已不存在；保留同一套比对口径，改为同路径双跑 |
| `tests/test_orchestration_graph.py` | 调整 3 处 | ①`_config` 不再设开关；②`legacy` 残留值被拒 + 显式 `langgraph` 仍接受；③json-only 续跑改为「跑挂后删 `graph.sqlite*`」复现 |
| `.workbuddy/verify_orchestrator_ab.py` | 删除，替换为 `verify_orchestrator_cli.py` | 新脚本做真实 CLI 双跑对照 + 残留开关拒绝验证 |

**没有改动**：`mas/agents/`、`sandbox`、`verifier`、`expr`、`blackboard`、`experience`、`frontend/`、`flow.yaml`、`collaboration.yaml`。

---

## 3. 保留下来的契约

- **节点执行与分支决策只有一份实现**：`Leader._execute_node` + `decision.decide`。删掉的是它们的外层驱动器。
- **检查点双源不变**：`graph.sqlite` 是续跑权威源，`checkpoint.json` 仍是审计快照与降级入口。`graph.sqlite` 丢失时，START 条件边按 `checkpoint.json` 的 `next_node` 落位续跑（回归 `test_resume_from_json_only_checkpoint`）。
- **取消立即生效不变**：`RunCanceledError` 继承 `BaseException` 穿透图节点；节点边界检查由图的节点函数调用 `_cancel_requested`；取消仍跳过经验/指标写入并落 `node_interrupted`。
- **router 纯函数 + 上限收口不变**：`route_from_state` 只读 state；`steps >= max_steps → halt`，与 `recursion_limit=max_steps*2+10` 配合，避免多跑一个节点。
- **事件序列口径不变**：节点内副作用（rollback 事件、经验降权、escalation、检查点）仍在节点函数里，router 不产生副作用。

---

## 4. 验证（实测）

| 项 | 命令 | 结果 |
|---|---|---|
| 改动前基线 | `python -m pytest -q tests` | **787 passed / 16 skipped**，252.62s |
| 改动后全量 | `python -m pytest -q tests` | **788 passed / 16 skipped**，252.36s |
| 定向三组 | `pytest -q tests/test_orchestration_graph.py tests/test_orchestration_determinism.py tests/test_cancel.py` | **33 passed**，47.91s |
| 真实 CLI 双跑 | `python .workbuddy/verify_orchestrator_cli.py` | 两次均 `verdict=pass / steps=12 / rollbacks=2 / bugs=5/5`；`graph.sqlite=True`；`checkpoint=completed`；事件脚本一致 `True`；code 树 hash 一致 `True` |
| 残留开关 | 同上脚本第三段 | `runtime.orchestrator: legacy` 的配置启动即失败，输出含 `runtime.orchestrator`（`True`） |

计数解释（788 vs 787，净 +1）：删除 1 个 A/B 用例（−1），新增 1 个重复性用例（+1），新增 1 个「显式 langgraph 仍接受」用例（+1）。

节点序列（两次 CLI 运行相同）：
`intake → solution → implement → test → optimize → test → review → implement → test → review → acceptance → report`

---

## 5. 风险与回退

| 风险 | 说明 | 处置 |
|---|---|---|
| langgraph 成为硬依赖 | 未安装环境的 `mas run` 直接 `ConfigError`，不再有 legacy 兜底 | 已在 `requirements.txt` 声明；报错文案给出安装命令 |
| 失去运行时切换开关 | 线上若怀疑图驱动有问题，不能改一行 yaml 切回旧循环 | 回退走 git：`mas/leader.py`、`mas/config.py`、`config/system.yaml`、`requirements.txt` 四处 revert 即可恢复双轨 |
| 语义漂移风险 | 本次未改 `_execute_node`/`decide`/`nodes.py` 逻辑，只删外层循环 | 由 `test_orchestration_determinism.py` + 既有 787 个用例共同覆盖 |

**未做（与 docs/51 一致）**：`interrupt()` 人工审批。原因未变——没有前端审批入口，且 `review` 节点有落盘副作用、恢复会从函数顶部重跑。

---

## 6. 复现

```bash
# 全量回归
python -m pytest -q tests

# 真实 CLI：两次独立运行 + 残留开关拒绝
python .workbuddy/verify_orchestrator_cli.py

# 手工单跑
python -m mas run -r examples/requirements/order_pricing_service.md --profile offline --workspace /tmp/ws
ls /tmp/ws/runs/*/graph.sqlite
```
