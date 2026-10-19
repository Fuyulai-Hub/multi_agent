# 25 - MySQL 与 Redis 接入评估与最小改动方案

> 评估范围：`G:/mycv/cragent/multi_agent`（master 分支，2026-10-19）
> 原则：**只做架构层接入建议，不改任何代码**；所有结论均给出代码级依据（文件:行）。
> 现状规模（实测）：workspace 48MB / 55 次运行 / events.jsonl 合计 3.5MB / 807 个产物版本 / 经验库 156 条 / 指标 45 行。

---

## 0. 结论摘要

| 组件 | 应该承担的职责 | 不应该承担的职责 | 优先级 |
| --- | --- | --- | --- |
| **MySQL** | ① 经验库主存储（替换全文件读-改-写）；② 运行索引 + 指标（替代前端 O(n) 目录扫描）；③ 产物/事件/LLM 留痕的**可查询索引**（双写，文件仍为 SSOT） | 事件日志本体、黑板产物文件本体、运行内实时状态 | 中（Phase C） |
| **Redis** | ① 跨进程运行注册表 + 心跳 + 取消信号（替代进程内 `RUNNING_TASKS`）；② 前端 API 读缓存；③ 分布式锁（替代 msvcrt 文件锁的 Windows 权限坑） | 持久化存储、消息队列、LLM 响应缓存 | 高（Phase B，先行） |
| **都不做** | 消息队列（当前总线是运行内同步请求-应答 + jsonl 留痕，引入 MQ 等于重写架构）；会话缓存（单运行内黑板热点已被进程内 `_latest` 缓存覆盖，收益低） | — | — |

**一句话架构建议**：文件系统继续当「证据层 SSOT」不动，MySQL 当「可查询的索引层/结构层」，Redis 当「跨进程协调层」。三层职责不重叠，任何一层宕机都可降级回纯文件模式（配置门控，默认关，行为与现状逐字节一致）。

---

## 1. 现状数据平面盘点（代码级事实）

| 数据 | 当前位置 | 写入方式 | 读取方式 | 并发控制 |
| --- | --- | --- | --- | --- |
| 事件日志（SSOT） | `runs/<id>/events.jsonl`（`mas/events.py:84` append） | 每事件一行 append | `read()`/`load()` 全文件读（`events.py:99-132`）；`_extract_inline` 每步全量扫 `ctx.events.events`（`leader.py:928`） | 无锁（append-only） |
| Agent 消息 | `runs/<id>/messages.jsonl`（`bus.py:87` append） | append + 内存 list | 进程内 `inbox()/digest()` | 无锁 |
| 黑板产物 | `runs/<id>/artifacts/<name>/v*.json` + `latest.json`（`blackboard.py:86-90`） | 锁内「版本号计算 + 落盘」 | 进程内 `_latest` 缓存，未命中回落磁盘 | `file_lock`（`filelock.py`） |
| 经验库 | `workspace/knowledge/experience.jsonl`（156 条） | **全文件 load→merge→save**（`experience.py:266-312`） | `load()` 全文件；`retrieve()` 还因 `hits += 1` **再全量重写一次**（`experience.py:394-398`） | **全局文件锁**，`lock_timeout_s=10` |
| 运行指标 | `workspace/knowledge/metrics.jsonl`（45 行） | append（`experience.py:733-735`） | `metrics_history()` 全文件 | 文件锁 |
| 运行摘要 | `runs/<id>/run_summary.json`（`leader.py:435`） | 一次性 write | 前端每次 `/api/runs` **遍历全部 55 个目录**（`server.py:141-179`） | 无 |
| LLM 留痕 | `runs/<id>/llm_traces.jsonl`（`base.py:235`） | append | 运行详情整文件读（`server.py:201`） | 无 |
| 运行注册表 | **仅进程内存** `RUNNING_TASKS`（`server.py:77`） | dict 登记/注销 | 取消接口查表（`server.py:481`） | 无跨进程语义 |
| 进行中判定 | mtime 启发式（`server.py:88-92`，600s 无新事件视为中断） | — | `/api/runs` 每次重算 | — |

### 已暴露的三个结构性问题（代码可复现）

1. **经验库是全系统唯一的全局争抢点**：`add()`/`reinforce()`/`promote()`/`retrieve()` 四处都在 `file_lock` 内做「全量 load → 全量 save」（`experience.py:266/511/557/394`）。两个运行并发（前端 + CLI，或前端连点两次）时，后到者要等前者的全文件写完；超时即 `FileLockTimeout` 上抛。且每次写入都是 O(N) 全量重写——156 条时无感，1560 条时每次注入经验都要重写几 MB。
2. **前端读路径随运行数线性劣化**：`/api/runs` 每请求 `iterdir()` 全量 + 读 55 份 summary；`/api/run` 详情整文件读 events.jsonl（单次运行事件日志实测可达数百 KB）。没有任何缓存层。
3. **取消语义被进程边界锁死**：`RUNNING_TASKS` 只登记本进程 POST 发起的运行（`server.py:75-76` 注释已自认），CLI 运行不可取消；「是否还在跑」靠 600s mtime 猜（`server.py:71`），不是真实心跳。

---

## 2. 评估：MySQL 适合什么

### 2.1 适合，且价值最高

**① 经验库主存储（`experiences` 表）——最高优先级**

现状痛点是「全文件读-改-写 + 全局锁」，而这在关系模型里天然是单行 UPSERT：

```sql
INSERT INTO experiences (id, tier, scope, confidence, hits, hits_success, hits_failed, ...)
VALUES (...)
ON DUPLICATE KEY UPDATE confidence = VALUES(confidence), hits = hits + 1, ...;
```

- `add()` 的合并语义（同 key → 置信度 +step、证据计数 +1，`experience.py:274-281`）= `ON DUPLICATE KEY UPDATE`，一次往返、原子、无锁。
- `retrieve()` 的 `hits += 1`（`experience.py:396`）= 批量单行 UPDATE，不再全量重写整本库。
- `reinforce()` 的正负反馈（`experience.py:511-545`）= 按 id 的 WHERE 更新，多运行并发互不阻塞。
- 附带收益：经验检索从「内存全量 + Python 打分」可渐进改为「SQL 预筛 tier/scope/min_confidence + Python 精排」，经验量上千后检索耗时不再线性涨。

**② 运行索引与指标（`runs` / `run_metrics` 表）**

`run_summary.json` + `metrics.jsonl` 是全息的运行事实，但**只能全表扫描**。MySQL 化后新增能力：

- `WHERE verdict='fail' AND profile='strict' ORDER BY ts DESC LIMIT 20` 之类的真实查询（现在是读 55 份 JSON 在内存里过滤）；
- 前端 `/api/runs` 从「遍历目录」变成「一条 SQL」，且可加 limit/since 参数分页；
- 为后续「按维度对比自学习收益」提供聚合基础（`AVG(duration_s) GROUP BY profile` 等）。

**③ 产物索引表（`artifacts_index`）——文件不动，只建索引**

黑板产物文件（版本化 JSON + digest）继续留在文件系统——**版本化 + 可 diff + digest 校验正是它作为「证据」的价值**（`blackboard.py:96-101`）。MySQL 只存索引：`(run_id, name, version, producer, node, iteration, digest, path, created_at)`，让「某产物第几版被谁在哪轮改了什么」可 SQL 查询，同时补上跨运行检索能力。

**④ 事件/消息/LLM 留痕的可查询索引（双写，SSOT 仍在文件）**

`events.jsonl` / `messages.jsonl` / `llm_traces.jsonl` **本体不搬**（append-only 审计语义 + 证据链 + 可 diff，`events.py:1-5` 的设计声明）。但可双写一份到 `run_events` / `messages` / `llm_traces` 表（seq 唯一键防重），解决「事件只能按 run 全文件读、不能跨运行按类型/严重级/节点检索」的问题。这是可选增强，不是必需。

### 2.2 不适合（明确不做）

| 候选 | 不做的理由 |
| --- | --- |
| 事件日志本体 | 审计 SSOT，全链路依赖「读文件即可复盘」；搬库后 offline 复盘、git diff、跨机器拷目录全部失效。双写索引是上限。 |
| 黑板产物文件本体 | 版本化 + digest + 目录结构是特性（见上）。 |
| 运行内实时状态（run_state、节点迭代计数） | 进程内状态模型，见 `concurrency.py:7-8` 的明确注释：多进程会破坏 Blackboard/Bus 的进程内状态模型。运行生命周期内的状态不应外迁。 |
| 消息队列（MySQL outbox） | 当前 `bus.py` 是运行内同步请求-应答（`leader.py:626-649` 的 consult 是同步闭环），没有任何跨运行异步解耦需求。为想象中的「Agent 间异步消息」引入 MQ 是过度设计。 |

---

## 3. 评估：Redis 适合什么

### 3.1 适合，且应立即做

**① 跨进程运行注册表 + 心跳 + 取消信号（替代 `RUNNING_TASKS`）——最高优先级**

- 数据结构：`SET mas:run:<run_id> '{"status":"running","node":"implement"}' EX 90`，Leader 主循环每个节点边界 `EXPIRE` 续期（心跳）；取消键 `mas:run:<run_id>:cancel`（SETNX）。
- 直接修掉现状问题 3：CLI/前端/任何 worker 发起的运行统一可见、可取消（`cancel_run` 的 404 语义保留：不在表 = 已完成或不存在）。
- 心跳替代 mtime 启发式（`server.py:88-92`）：「90s 未续期」比「600s 文件没动」更真实，且不受事件密集度影响。
- TTL 自然清理，不需要 `finally: RUNNING_TASKS.pop()` 也能兜底（进程被杀后 key 到期自动消失）。

**② 前端 API 读缓存**

- `GET /api/runs`（当前每请求全目录扫描）、`GET /api/experience` 的列表结果缓存 5~30s，写事件（run_started/run_finished/经验变更）时精准失效。
- 缓存击穿保护：单飞（single-flight）或短 TTL + 旧值兜底，避免并发首刷。

**③ 分布式锁（替代/补充 `filelock.py`）**

- `SET lock:<path> <token> NX PX <timeout>` 的语义比 msvcrt LF 锁更干净，且天然解决 working memory 记录的 Windows 并发 open PermissionError 坑（`filelock.py:47-51` 的注释自认约 10~20% 概率触发）。
- 建议作为**可选后端**而非替换：`file_lock()` 保持默认文件锁（零依赖路径逐字节不变），Redis 启用时走 Redis 锁；`lock_timeout_s` 语义（超时抛 `FileLockTimeout`）保留。

**④ Pub/Sub 实时事件推送（可选，中低优先级）**

前端目前靠轮询 `/api/run`。Redis pub/sub 可把 `EventLog.emit()` 的新事件推给 SSE 长连接，前端从轮询改推送。属于体验优化，不影响正确性，可最后做。

### 3.2 不适合（明确不做）

| 候选 | 不做的理由 |
| --- | --- |
| 持久化存储 | Redis 是内存态，AOF/RDB 与「证据必须可追溯」的定位冲突；且重启丢 key 会破坏审计叙事。 |
| LLM 响应缓存 | `temperature=0.2` 但 prompt 每轮因经验注入/黑板内容变化而不同（`prompts.py` 每次组装），命中率极低；且缓存会掩盖端点行为观测（working memory 大量实测依赖「每次真实调用」）。 |
| 会话缓存（黑板快照） | 单运行内热点已被 `blackboard._latest` 进程内缓存覆盖（`blackboard.py:127-140`）；只有多 worker 同时读同一 run 才有收益，当前无此部署形态。优先级最低，列为远期。 |

---

## 4. 目标数据平面架构

```
                    ┌────────────────────────────────────────────┐
                    │              协调层（Redis）                 │
                    │  运行注册表+心跳+取消键 · API 读缓存 · 分布式锁  │
                    └───────┬───────────────────────▲────────────┘
                            │ 心跳/取消              │ 读缓存
  ┌──────────┐       ┌──────▼───────┐        ┌──────┴───────┐
  │ 前端面板  │◄─────►│ Leader/Agent │        │  (可选)SSE   │
  │ server.py │  SQL  │  流水线进程   │        │  pub/sub     │
  └────┬─────┘       └──────┬───────┘        └──────────────┘
       │                    │ 写
       │            ┌───────▼────────────────────────┐
       │            │      索引/结构层（MySQL）        │
       │            │  experiences · runs · metrics   │
       │            │  artifacts_index · (双写索引)    │
       │            └───────┬────────────────────────┘
       │ 查索引/分析         │ 双写（SSOT 不动）
       │            ┌───────▼────────────────────────┐
       └───────────►│      证据层（文件系统，不变）      │
         读原文      │  events.jsonl · artifacts/v*.json│
                    │  run_summary · llm_traces ...    │
                    └──────────────────────────────────┘
```

**降级契约**：`runtime.store.enabled=false`（默认）时，三条数据路径与现状逐字节一致——Redis 全部走 `MemoryRegistry`（= 现有 `RUNNING_TASKS` 行为），MySQL 全部走 jsonl（= 现有 `ExperienceLibrary` 行为），前端读路径不变。

---

## 5. 最小代码改动方案（分三层递进，每层独立可回滚）

### Phase A：数据面基础设施（零行为变化）

| 改动点 | 文件 | 内容 |
| --- | --- | --- |
| A1 | `mas/store/__init__.py`（新） | 后端工厂：按配置返回 `None` / `RedisBackend` / `MySQLBackend`；import 失败只发 warning 事件并降级，不抛异常 |
| A2 | `mas/store/redis_backend.py`（新） | **零依赖** RESP2 over socket 客户端（~150 行：`get/set/setnx/expire/eval(lock)/publish`）——与项目「除 PyYAML 外零依赖」气质一致；也允许环境里已有 `redis` 包时优先用它 |
| A3 | `mas/store/mysql_backend.py`（新） | PyMySQL 薄封装（线程本地连接 + 简单重连）；PyMySQL 为**可选软依赖**，未安装即禁用后端并留痕 |
| A4 | `mas/store/schema.sql`（新） | 建表 DDL：`experiences`（主键 `key_hash`，UNIQUE `id`）、`runs`、`run_metrics`、`artifacts_index`、`run_events`（UNIQUE `(run_id, seq)`）、`llm_traces` |
| A5 | `config/system.yaml` | `runtime.store` 段：`enabled: false` + `mysql: {host,port,user,password_env,db}` + `redis: {url_env}`；连接串走环境变量外注（`MAS_MYSQL_DSN` / `MAS_REDIS_URL`），与 `llm.api_key_env` 同款外注约定 |
| A6 | `mas/config.py` | `SystemConfig` 增加 `store` 属性（与 `runtime.skills` 同模式，`config.py:453-499` 附近）；**不改任何默认值**，老配置缺省 `enabled=false` |
| A7 | `mas/cli.py` | `python -m mas store migrate` 子命令：jsonl → MySQL 一次性迁移（经验库 156 条 + 指标 45 行 + runs 摘要），幂等（按 key/run_id 去重） |

### Phase B：Redis 协调层（先行，风险最低、收益最直接）

| 改动点 | 文件 | 内容 | 作用 | 预期效果 / 验收标准 |
| --- | --- | --- | --- | --- |
| B1 | `mas/runhub.py`（新） | `RunRegistry` 抽象：`register(run_id)` / `heartbeat(run_id, status)` / `request_cancel(run_id)` / `is_canceled(run_id)` / `list_running()`；实现 `MemoryRegistry`（语义 = 现有 `RUNNING_TASKS`，默认）+ `RedisRegistry`（hash + TTL + cancel 键） | 把「运行注册表」从进程内 dict 抽象成可插拔后端 | 默认路径下 `tests/` 全绿且无行为变化；Redis 模式下 CLI 运行可被面板取消（现有 404 语义保留） |
| B2 | `frontend/server.py` | `RUNNING_TASKS` 替换为 registry 实例；`start_run`/`cancel_run`/`run_finished` 改走 registry；Redis 启用时 `run_finished` 用心跳（90s 未续期 = 中断）替代 mtime 600s 启发式（`server.py:69-92`） | 跨进程取消 + 真实判活 | 关 Redis 时逐字节兼容；开 Redis 后 `POST /api/run/cancel` 对 CLI 运行返回 200 且 Leader 下个节点边界退出（`run_summary.canceled=true`） |
| B3 | `mas/leader.py` | `run()` 增加 `registry=None` 参数；主循环节点边界（`leader.py:244-258` 已有的 cancel 检查处）heartbeat + status；Redis 模式下额外轮询取消键置位本地 `cancel_event` | 心跳上报 + 跨进程取消信号注入 | 运行中 `list_running()` 可见当前节点；取消后 summary.canceled=True、不写 metrics（现有契约不变） |
| B4 | `frontend/server.py` | `/api/runs`、`/api/experience` 加 Redis 缓存（TTL 5~30s），`run_started`/`run_finished`/经验写入时失效 | 消除每请求全目录扫描 | 55 runs 下 `/api/runs` 命中缓存 < 5ms；并发 20 请求无重复扫描（单飞） |
| B5 | `mas/filelock.py` | `file_lock()` 增加 `backend` 参数：默认 `file`（现状）；Redis 模式走 `SET NX PX` 锁（token 校验防误删他人锁），超时仍抛 `FileLockTimeout` | 消除 Windows msvcrt 权限坑 + 跨机锁 | 关 Redis 时逐字节兼容；`tests/test_filelock*` 与并发回归全绿 |

### Phase C：MySQL 结构层（经验库 + 运行索引）

| 改动点 | 文件 | 内容 | 作用 | 预期效果 / 验收标准 |
| --- | --- | --- | --- | --- |
| C1 | `mas/experience.py` | `ExperienceLibrary.__init__` 增加 `backend="jsonl"｜"mysql"`；mysql 后端把 `add()` 的 merge 改 UPSERT（`experience.py:266-312`）、`retrieve()` 的 hits 改批量单行 UPDATE（`experience.py:394-398`）、`reinforce()` 按 id 更新（`experience.py:511-545`）、`promote()` 单行 UPDATE；`_locked()` 在 mysql 后端退化为 no-op（行锁即并发控制） | **消除全局文件锁争抢 + O(N) 全量重写** | 两个运行并发 `add`/`reinforce` 零等待、零覆盖丢经验；`tests/test_experience*` 全绿；156 条迁移后检索结果与 jsonl 版一致（内容比对） |
| C2 | `mas/leader.py` | 收尾处（`leader.py:435` run_summary 落盘旁）双写 `runs` 表；`experience.record_metrics` 双写 `run_metrics` 表 | 运行索引 + 指标入库 | 每次运行结束 `SELECT * FROM runs WHERE run_id=?` 有行；jsonl 继续为 SSOT |
| C3 | `mas/agents/base.py` | LLM 留痕（`base.py:234-238`）双写 `llm_traces` 表（失败不影响主流程，保留现有 try/except 语义） | 留痕可跨运行检索（按模型/节点/finish_reason 统计） | 留痕失败不崩主流程（现有 `except OSError: pass` 语义保留） |
| C4 | `frontend/server.py` | `load_runs`/`load_run_detail`/`/api/metrics`/`/api/experience` 在 MySQL 启用时走 SQL（jsonl 兜底）；`/api/runs` 增加 `verdict/profile/limit/since` 查询参数 | 查询能力 + 分页 | `GET /api/runs?verdict=fail&limit=10` 返回正确子集；MySQL 不可用时自动回落 jsonl，面板不白屏 |
| C5 | `mas/blackboard.py` | publish 时（`blackboard.py:102-112` 已有 ref 构造处）顺带写 `artifacts_index` 一行 | 产物版本可 SQL 检索 | `WHERE run_id=? ORDER BY name, version` 与目录扫描结果一致 |

### 不做（负面清单，防止范围蔓延）

1. **不搬 `events.jsonl` 本体**——审计 SSOT，双写索引（C 阶段可选）是上限。
2. **不搬黑板产物文件**——版本化 + digest 是证据特性。
3. **不引入任何消息队列**（Redis Stream / RabbitbitMQ / MySQL outbox）——当前总线是运行内同步闭环，无跨运行异步需求。
4. **不做 LLM 响应缓存**——命中率低且会污染端点行为观测。
5. **不改 `flow.yaml` / 节点拓扑**——接入是数据面改造，与流程编排解耦。
6. **不改测试夹具语义**——`config/system.yaml` 同时是测试夹具（`tests/test_skill_injection.py` 断言 skills.enabled=false），新增 `runtime.store` 段必须默认关闭且不触碰既有键。

---

## 6. 配置设计（与现有约定对齐）

```yaml
runtime:
  store:                        # 新增段，默认全关 → 行为与现状逐字节一致
    enabled: false              # 总闸；false 时 B/C 所有后端走内存/jsonl 路径
    mysql:
      enabled: false
      host: "127.0.0.1"
      port: 3306
      database: "multi_agent"
      user: "mas"
      password_env: "MAS_MYSQL_PASSWORD"     # 凭据外注，与 llm.api_key_env 同款
    redis:
      enabled: false
      url_env: "MAS_REDIS_URL"               # 例：redis://127.0.0.1:6379/0
      heartbeat_ttl_s: 90                    # 运行心跳 TTL（替代 600s mtime 猜测）
```

对齐点：① 默认关闭 + 「关闭时行为逐字节一致」（全仓既有约定，见 `runtime.ui_probe` / `context_compression` 等）；② 凭据环境变量外注（`config.py:68-77` 的三通道模式）；③ 生效方式为下一次运行 `load_config` 热生效（`server.py:307-311` 已建立的模式，无需重启面板）；④ 前端开关若要接入，按 working memory 的教训——**主界面必须有可见入口**并与弹窗双向 sync，且开关写本地覆盖文件而非直接改 system.yaml（避免测试夹具断言被打脸）。

---

## 7. 部署与依赖说明

- **MySQL**：PyMySQL（纯 Python，单包）作为可选软依赖；`requirements.txt` 增加注释行说明「仅 `runtime.store.mysql.enabled=true` 时需要」。开发机可用 Docker Desktop 起 `mysql:8`；HPC/服务器侧由运维提供实例。
- **Redis**：自实现 RESP2 客户端（零新依赖）；`redis` 包存在时优先使用（行为等价）。
- **迁移**：`python -m mas store migrate` 一次性导入；建议双读过渡期（MySQL 写 + jsonl 继续写 + 读走 MySQL），确认一周后再考虑 jsonl 降级为纯备份。
- **降级演练**（验收必做）：`runtime.store.enabled=false` 跑一轮 `python -m mas demo` 与一次面板运行，diff 产物与开启前一致（git status 干净）。

---

## 8. 量化收益预估

| 指标 | 现状 | 目标 | 度量方式 |
| --- | --- | --- | --- |
| `/api/runs` 延迟 | 随运行数线性增长（55 runs 全目录扫描 + 55 份 JSON 解析） | 命中缓存 < 5ms；未命中单条 SQL < 20ms | 压测 50 并发取 P50/P95 |
| 经验库写入 | 全局文件锁 + 全文件重写（O(N)），并发即争抢，超时 10s 抛错 | 单行 UPSERT，并发零等待 | 两进程并发 `add` × 100 次，统计耗时与丢条数 |
| 运行取消覆盖面 | 仅本进程 POST 发起的运行 | 任意进程发起均可取消（心跳可见） | CLI 起运行 → 面板取消 → summary.canceled=true |
| 进行中判定 | 600s mtime 猜测（可能把慢节点误判为中断） | 90s 心跳 TTL，节点边界续期 | 运行中 kill -9，观察列表状态切换时间 |
| 跨运行分析 | 不可能（只能全文件扫） | SQL（verdict/profile/时段聚合） | 新增查询端点用例 |

---

## 9、风险与保障

1. **软依赖失败路径**：PyMySQL 未装 / MySQL 不可达 → 后端禁用 + `store_backend_unavailable` 事件 + 回落 jsonl（不允许因数据库挂了让流水线失败）。
2. **双写一致性**：双写期以 jsonl 为 SSOT；MySQL 写失败只留痕不回滚主流程（与 `llm_traces` 现有 `except OSError: pass` 同款语义）。
3. **Windows 行尾**：任何写 `system.yaml` 的行级替换必须 `newline="\n"`（working memory 2026-10-17 教训）。
4. **测试夹具**：`tests/` 对 `system.yaml` 有断言（如 `test_skill_injection.py:198`），新增段需先跑全量回归确认无破坏。
5. **文件锁回归**：B5 改 `filelock.py` 后必须保持 working memory 记录的修复结构（open 在重试循环内）并跑并发回归（10/10 轮）。
