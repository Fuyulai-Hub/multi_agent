# 立即终止（LLM 层协作式取消）最小改动实现与验证

日期：2026-10-28。关联：docs/23（协作式取消初版）、docs/34（ReAct）、docs/35（上下文管理）。

## 1. 问题

面板点击「终止任务」后，运行长时间停留在「终止中…（当前节点执行完毕后停止）」，
任务卡片仍为「进行中」。实测场景（run 20261028-211058-bf82）：intake 节点的 LLM
流式调用进行中，取消信号已置位但迟迟不生效。

## 2. 根因

取消信号只在 **Leader 主循环的节点边界**检查（leader.py 的 while 循环开头）：

```
点击终止 → frontend/server.py cancel_run → RUNNING_TASKS[run_id].set()
        → Leader 等当前节点跑完 → 下个节点边界才退出
```

本端点（api.chat.csu.edu.cn/v1）单节点 LLM 流式调用实测可达 300~400s（implement/
optimize 更长，含降级链与重试时数倍于此），节点内不打断 ⇒ 用户体验等同"终止无效"。

## 3. 方案（最小改动）

把取消信号下沉到 LLM 层，节点内发现信号立即抛 `RunCanceledError` 穿透到 Leader，
**复用既有取消收尾链**（canceled=true / verdict=fail / 跳过经验与指标 / run_summary
与 timeline 照常落盘），不新写任何收尾逻辑。

### 3.1 异常类型（mas/errors.py）

`RunCanceledError(BaseException)`——**特意不继承 Exception**（同 asyncio.CancelledError
3.8 起的设计理由）：Agent 各层遍布 `except Exception` 降级兜底（实测吞噬点：
`product.py:_generate_hidden_suite` 把取消异常吞成"隐藏用例生成失败"的普通节点失败），
Exception 子类会被就近吞掉；BaseException 让所有通用 except 自然拦不住它。
全仓 `except BaseException` 仅存在于 `verify/runner.py` 测试执行器（try 块内无 LLM
调用），不会反向吞掉本异常。

### 3.2 探测点（mas/llm.py）

- `LLMClient.should_stop: Optional[Callable[[], bool]]`——由 Leader.run() 注入，
  默认 None 时 `_canceled()` 恒 False，**未注入路径行为与改造前逐字节一致**。
- 三处检查点：
  1. 每次请求/重试迭代入口（`_attempt`、流式与非流式 for-attempt 开头，`force=True`
     真实探测）；
  2. **流式 SSE 逐行**（节流 `cancel_poll_interval` 秒至多一次真实探测，默认 0.5s；
     抛出点在 `with urlopen` 内部 → 连接随退出关闭，服务端流被真实中断）；
  3. 重试退避分段睡眠（`_sleep_cancelable`，0.25s 粒度）。
- 探测通道自身异常（如 Redis 抖动）按"未取消"处理，绝不把取消通道故障变成生成失败。
- offline provider 不构造请求体，不经过上述路径——离线行为完全不变（既有
  `_NthCheckEvent` 边界测试的 is_set 计数不受影响）。

### 3.3 穿透与收尾（mas/agents/base.py、mas/leader.py）

- `base.py:act()`：`except RunCanceledError: raise` 置于兜底 `except Exception` 之前
  （BaseException 语义下为冗余双保险，防有人日后把基类改回 Exception）。
- `leader.run()`：`llm.should_stop = _llm_should_stop` 闭包同时探 cancel_event 与
  registry（worker 模式只有 registry，无 event）。
- 状态机 `try` 增加 `except RunCanceledError`：置 canceled=True、发
  `node_interrupted` 事件（含被中断节点 id）、node_id="halt"，随后 finally 与既有
  取消收尾照常执行。`_act_with_retry`、worker、CLI 均不感知该异常（不逃逸出 run()）。

### 3.4 前端文案（frontend/app.js、server.py）

按钮提示/确认框/状态行从「当前节点执行完毕后停止」改为「立即中断当前模型调用」，
注释同步。机制零改动：`/api/run/cancel` 与轮询呈现逻辑不变。

## 4. 验证

- `tests/test_cancel.py` 新增 8 用例：
  - Leader 级：`test_cancel_interrupts_llm_call_mid_node`——第 2 次 LLM 调用中抛
    RunCanceledError，钉死 ①无 node_error（不被吞成节点失败）②node_interrupted +
    run_canceled 事件、run_summary.canceled=true、不落知识库指标 ③不再有第 3 次调用。
  - LLM 级：请求入口 force 探测、节流与 force 语义、探测故障隔离、**假 SSE 桩流式
    逐行中断**（第 2 行即抛）、退避睡眠正常返回/立即中断。
- 回归：`tests.test_cancel` 21/21 通过；**全量 `unittest discover` 669 用例 OK**
  （含既有边界取消三用例，行为不变）。
- 真机端到端（起服务 → 流式生成中途 POST /api/run/cancel → 面板秒级转「已终止」）
  待前端服务重启后复核（长驻服务不重启则运行期模块与源码不一致，见 MEMORY）。

## 5. 边界（如实说明）

| 场景 | 终止生效时机 |
|---|---|
| LLM 流式读取中（chunk 持续到达） | ≤ 0.5s（节流间隔）+ 收尾落盘，秒级 |
| LLM 重试退避等待中 | ≤ 0.5s，立即抛 |
| 流静默期（模型长时间不吐 chunk） | 最坏等一次 socket 读超时（llm.timeout，120s），由重试入口 force 探测兜住 |
| 节点内无 LLM 调用的执行段（沙箱跑测试、MCP 工具调用） | 该段跑完，下个节点边界生效（与改造前一致） |
| 排队中的运行（worker 未领取） | 既有 worker 领取前检查，不变 |
