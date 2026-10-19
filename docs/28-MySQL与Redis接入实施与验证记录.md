# 28 - MySQL 与 Redis 接入：实施记录与验证报告

> 实施范围：`G:/mycv/cragent/multi_agent`（master 分支，2026-10-19）
> 上游设计文档：`docs/25-MySQL与Redis接入评估.md`（架构评估与最小改动方案）
> 本文记录**实际落地内容、真实服务验证结果、降级路径与偏差说明**。所有数字均为 2026-10-19 在本机实测，未修饰。

---

## 1. 一句话结论

MySQL 8.0.46 与 Redis 7 两个真实服务已通过 Docker 运行并连通；流水线在一次真实运行中完成五表双写（runs +1 / run_metrics +1 / experiences +4 / run_events +113 / artifacts_index +20），Redis 注册表完成注册→心跳→注销全生命周期；**全量回归 333 + 存储集成 16 共 349 个测试全绿**；任一后端不可用时系统自动降级回 jsonl/内存路径，不抛异常、不改变既有行为。

---

## 2. 真实服务状态（验证起点）

| 服务 | 容器 | 镜像 | 端口 | 数据卷 | 状态 |
| --- | --- | --- | --- | --- | --- |
| MySQL | `mas-mysql` | mysql:8.0（8.0.46） | 127.0.0.1:3306 | `mas-mysql-data` | Up，`mas` 用户对 `multi_agent` 库有 ALL PRIVILEGES |
| Redis | `mas-redis` | redis:7-alpine | 127.0.0.1:6379 | `mas-redis-data`（AOF） | Up |

### 2.1 服务启停命令（本机现状）

```bash
# 查看状态
docker ps --filter name=mas-

# 停止 / 启动 / 重启（不丢数据，卷持久化）
docker stop mas-mysql mas-redis
docker start mas-mysql mas-redis
docker restart mas-mysql mas-redis

# 新环境一键复现（推荐，含健康检查与 .env 密码外注）
cp deploy/.env.example deploy/.env   # 然后编辑 .env 填两个密码
docker compose -f deploy/docker-compose.store.yml up -d
```

> `deploy/docker-compose.store.yml` 的环境变量（root 密码 / mas 密码）**仅在数据卷首次初始化时生效**；现有 `mas-mysql-data` 卷已初始化过，密码以容器实际创建时为准（与 `config/credentials.yaml` 的 `store.mysql.password` 一致）。

### 2.2 账号与权限（已核实）

```sql
SHOW GRANTS FOR 'mas'@'%';
-- GRANT USAGE ON *.* TO `mas`@`%`
-- GRANT ALL PRIVILEGES ON `multi_agent`.* TO `mas`@`%`
```

`mas` 用户只能访问 `multi_agent` 一个库（最小权限）；密码只从 `config/credentials.yaml` 或环境变量注入，不进入任何提交文件。

---

## 3. 交付清单（文件地图）

### 3.1 新增文件

| 文件 | 作用 |
| --- | --- |
| `mas/store/__init__.py` | 后端工厂 + 三通道配置合并 + **工作区绑定守卫** + `store_status` 健康报告 |
| `mas/store/mysql_backend.py` | PyMySQL 薄封装：线程本地连接、`transaction()`（BEGIN/COMMIT/ROLLBACK）、`ensure_schema()`、`health()`；连接失败只抛给工厂，工厂降级为 None |
| `mas/store/redis_backend.py` | **零依赖** RESP2 over socket 客户端（标准库实现）+ `RedisBackend`（运行注册表 / 缓存 / 分布式锁） |
| `mas/store/schema.sql` | 六张表 DDL：`experiences` / `runs` / `run_metrics` / `artifacts_index` / `run_events` / `llm_traces` |
| `mas/runhub.py` | `RunRegistry` 抽象 + `MemoryRunRegistry`（= 原 `RUNNING_TASKS` 语义）+ `RedisRunRegistry` + `build_registry` |
| `tests/test_store_integration.py` | 16 个用例：真实后端 10 个（不可用自动 skip）+ 降级路径 6 个（全离线） |
| `examples/store_doublewrite_demo.py` | 基础调用示例：后端探测 → 行数基线 → 真实运行 → 双写对比 → 注册表/锁/缓存演示 |
| `deploy/docker-compose.store.yml`、`deploy/.env.example` | 新环境一键起服务的编排文件（密码走 .env 外注） |

### 3.2 修改文件（最小改动）

| 文件 | 改动 | 作用 |
| --- | --- | --- |
| `mas/leader.py` | `run(registry=None)` 参数；`_store_backends()` 实例缓存；`_make_mysql_event_sink`（事件双写 sink）；`upsert_run_index`（runs 表 UPSERT，migrate 共用）；`_persist_run_index`；节点边界 heartbeat + 跨进程取消判定；`RunSummary.canceled` | 运行级双写 + 跨进程协调；`registry=None` 时行为与改造前逐字节一致 |
| `mas/experience.py` | `ExperienceLibrary(mysql=None)` 后端参数：`load/save/_locked/record_metrics/metrics_history` 双实现；`migrate_jsonl_to_mysql` + `_merge_duplicate_key_rows` | 经验库/指标可切 MySQL；合并/打分/晋级/反馈逻辑不变 |
| `mas/cli.py` | `store status/init/migrate` 三子命令；`cmd_run/cmd_demo` 透传 registry | 运维入口 + CLI 运行也可被面板取消 |
| `mas/config.py` | `SystemConfig.store` 属性；`load_credentials_raw` | 配置接线（不改任何既有默认值） |
| `frontend/server.py` | `BACKENDS`/`REGISTRY` 模块级实例；`start_run(registry=None)`；`cancel_run` 增加 registry 快路径之外的跨进程查询；`GET /api/store/status` | 面板可见接入状态 + 跨进程取消；后端全挂时降级启动 |
| `config/system.yaml` | 新增 `runtime.store` 段（`enabled: true`）+ `runtime.skills` 段（两个开关 `false`，出厂默认态） | 配置门控；skills 段补记是为了前端开关的键在位（开关写回要求键存在） |
| `requirements.txt` | PyMySQL 注释为可选依赖 | 未安装时自动走 jsonl 路径 |
| `.gitignore` | `deploy/.env` | 密码文件不入库 |

---

## 4. 验证结果（2026-10-19 实测，可复现）

### 4.1 测试全绿

| 套件 | 解释器 | 结果 |
| --- | --- | --- |
| 全量回归 `tests/`（除 store 集成） | 系统 Python 3.14（pytest 9.0.3） | **333 passed** |
| 存储集成 `tests/test_store_integration.py` | 受管 venv（含 PyMySQL 1.2.3） | **16 passed** |

> venv 无 pytest，集成测试用 `python -m unittest` 跑；系统 Python 无 PyMySQL，正好天然覆盖「驱动缺失」降级路径。两个解释器路径都写进了测试文件头注释。

### 4.2 CLI 三件套

```bash
$ python -m mas store status
{"enabled": true, "mysql": {"ok": true, "version": "8.0.46", "latency_ms": 1.0},
 "redis": {"ok": true, "running_runs": 0}, "workspace_guard": true}

$ python -m mas store init
schema 已就绪：mas/store/schema.sql（执行 6 条 DDL，幂等可重复）

$ python -m mas store migrate
迁移完成（幂等，可重复执行；既有行不覆盖，只补缺口）：
经验 142 条（折叠历史漂移行 14 条）、指标 45 条、运行 42 轮
```

- 迁移的「折叠」：experience.jsonl 156 行里只有 142 个 distinct key_hash（历史漂移：同键不同 id）。按确定性规则合并（层级最高 + 证据最多者为基准，roles/tags 取并集，**计数不加总**——相加会发明不存在的观测数）。
- 重复迁移第二次输出与第一次逐字一致（幂等），且**不覆盖 MySQL 既有行**（见 §6 的 INSERT IGNORE 修复）。

### 4.3 真实运行双写（`examples/store_doublewrite_demo.py`）

一次 offline provider 的完整流水线（run `20261019-072742-645e`，verdict=pass，9 步）：

| 表 | 迁移后基线 | 运行后 | 增量 | 含义 |
| --- | --- | --- | --- | --- |
| `runs` | 41 | 42 | +1 | 运行索引（`finished=1, canceled=0`） |
| `run_metrics` | 45 | 46 | +1 | 指标双写 |
| `experiences` | 142 | 146 | +4 | 本轮新学经验（实时路径只写 MySQL） |
| `run_events` | 27 | 140 | +113 | 全量事件双写（`INSERT IGNORE`，(run_id,seq) 唯一键幂等） |
| `artifacts_index` | 4 | 24 | +20 | 产物发布索引 |

Redis 侧同轮验证：运行中 `list_running()` 可见（心跳续期），运行结束 `unregister` 后为空（TTL 另有兜底）；`acquire_lock` 二次获取返回 None（互斥生效）；`cache_set/cache_get` 往返正常；演示键全部自动清理（缓存 TTL 5s、锁已释放），无残留。

### 4.4 前端冒烟（单命令起服务→探测→停止）

| 探测 | 结果 |
| --- | --- |
| `GET /api/store/status` | `{"enabled":true,"mysql":{"ok":true,"version":"8.0.46",...},"redis":{"ok":true,...},"workspace_guard":true}` |
| `GET /api/skills` | `{"enabled": false, "tools_enabled": false}`（出厂默认） |
| `GET /api/runs` | 57 条运行列表（文件系统权威源，MySQL 不参与列表读取，见 §5） |
| `GET /` | 200 text/html |
| `POST /api/run/cancel`（不存在的 run） | 404（既有语义保留） |
| `GET /api/run?id=<run_id>` | 详情含 events/summary/canceled 字段 |

---

## 5. 与 docs/25 计划的偏差（如实记录）

| 计划项 | 实际 | 原因 |
| --- | --- | --- |
| B4 `/api/runs` Redis 读缓存 | **未做** | 前端列表读取保持「文件系统唯一权威源」：MySQL runs 表是至少一次投影（写失败静默），作列表源会漏行；缓存解决的是性能而非正确性，优先级让位于确定性 |
| B5 filelock 切 Redis 锁 | **未做** | `filelock.py` 保持文件锁（零依赖路径逐字节不变）；Redis 锁原语已在 `RedisBackend.acquire_lock/release_lock` 实现并测试，供后续按需接入 |
| C3 llm_traces 双写 | **未做** | 表已在 schema 就绪，留痕双写属可选增强；留待与「跨运行按模型/finish_reason 统计」需求一起做 |
| C4 前端 SQL 读取 | **未做** | 同 B4（保守决策） |
| C5 artifacts_index | **做了，落点不同** | 经 `EventLog.subscribe` 的事件 sink 统一双写（事件 + 产物索引一个钩子点），而非改 `blackboard.py` 的 publish——数据相同、侵入面更小 |
| A5 配置默认值 | **store.enabled=true** | 计划写「默认 false」；实际交付要求「真实可用」，故规范工作区下默认开启（测试/临时 workspace 被工作区守卫自动禁用，回归不受影响） |

**已知边界**（设计取舍，非缺陷）：

1. store 启用后，经验/指标的**实时写入只进 MySQL**，`experience.jsonl`/`metrics.jsonl` 成为启用前的陈旧快照（后端切换语义，见 `ExperienceLibrary` 文档字符串）。事件、产物、run_summary 仍全程落盘（证据层 SSOT 不动）。
2. 迁移是 `INSERT IGNORE`（只补缺口）。若曾发生「MySQL 挂→降级期 jsonl 写入→MySQL 恢复」，既有行的分歧需要人工裁决：两侧各读一份（MySQL `SELECT * FROM experiences` / jsonl 逐行），按 `key_hash` 对齐后比对 `hits/evidence_count/tier`，以更新的一侧为准手工同步。框架不自动合并——自动合并两个分歧源需要新鲜度信号，而现有 schema 没有可靠的 `updated_at`（不引入猜测性合并，避免发明数据）。

3. 只有**规范工作区**（`system.yaml` 的 `system.workspace`，默认 `workspace/`）连真实库；`--workspace` 指向别处或测试用临时目录时全禁用。

---

## 6. 实施中发现并修复的三个问题（均有回归用例）

1. **`learning_report_markdown` 的 `deltas` 未绑定**（`test_pipeline.test_learning_report_generated` 红）：上一会话一次误编辑把两行移出了 `if summary.get("available")` 块，历史指标恰好 1 条时 `UnboundLocalError` → reporter 节点 fail。恢复原缩进；`git diff` 逐行核对过该 hunk。
2. **`test_upload` 三处断言缺 `registry=None`**：`start_run` 现在总传 registry 给 `Leader.run`，补上新参数断言（顺带在 docstring 记录 registry=None 的降级语义）。
3. **migrate 的 UPSERT 会覆盖实时数据**（本次新发现，最值得一提）：MySQL 启用后 jsonl 不再跟进，migrate 若用 `ON DUPLICATE KEY UPDATE`，重复迁移会把运行中累积的 hits/晋级/证据**回滚成陈旧快照值**。用临时行实证了旧语义（hits 999→1 被 UPSERT 改写）。改为 `INSERT IGNORE`（只补缺口、永不覆盖既有行），并补回归用例 `test_migrate_never_clobbers_live_rows`（ sentinel +12345 后重复迁移，值必须原样）。

> 附带确认的一个 MySQL 行为：`INSERT ... ON DUPLICATE KEY UPDATE` 的更新列表不含 `id` 时，冲突行的 id 保持首次写入的值（first-writer-wins）。漂移合并后 migrate 的基准 id 与库中既有 id 可能不同，列值被改写而 id 不变——这也是迁移语义必须收紧的另一个理由。

---

## 7. 凭据与配置约定（三通道外注）

连接参数优先级（与 LLM 凭据完全同款）：

```
环境变量（键名由 *_env 声明） > config/credentials.yaml 的 store 段 > system.yaml 的 runtime.store 默认值
```

- `system.yaml` 只放非密项（host/port/user/database/url）；**密码只从环境变量或 credentials.yaml 来**。
- 环境变量名：`MAS_MYSQL_HOST/PORT/USER/PASSWORD/DB`、`MAS_REDIS_URL`。
- `config/credentials.yaml` 已在 `.gitignore`，不会提交。
- 前端「模型配置」弹窗的 skills 开关仍写 `system.yaml`（既有机制）；本次把它恢复为出厂默认 `false/false`，键保留在位（开关写回函数要求键存在，否则 ValueError）。

---

## 8. 复现清单（按顺序执行）

```bash
# 0) 服务（已运行可跳过）
docker ps --filter name=mas-            # 两个容器 Up

# 1) 解释器：全量回归用系统 Python（有 pytest）；集成/真实操作用 venv（有 PyMySQL）
PY_SYS=/c/Python314/python.exe
PY_VENV=/c/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe

# 2) 测试
$PY_SYS -m pytest tests/ -q --ignore=tests/test_store_integration.py   # 333 passed
$PY_VENV -m unittest tests.test_store_integration -v                   # 16 passed

# 3) CLI
$PY_VENV -m mas store status      # MySQL 8.0.46 + Redis ok + guard true
$PY_VENV -m mas store init        # 6 条 DDL（幂等）
$PY_VENV -m mas store migrate     # 142 经验/45 指标/42 轮（重复执行结果一致）

# 4) 双写 smoke（一次真实运行 + 行数对比 + 注册表/锁/缓存演示）
$PY_VENV examples/store_doublewrite_demo.py

# 5) 前端
$PY_VENV frontend/server.py --port 8000
curl -s localhost:8000/api/store/status
curl -s localhost:8000/api/runs | head -c 200
```

---

## 9. 降级路径与验证方法

| 场景 | 预期行为 | 验证方式 |
| --- | --- | --- |
| `runtime.store.enabled=false` | 双后端 None，全走 jsonl/内存，行为逐字节一致 | `test_store_disabled_gate` |
| PyMySQL 未安装 | MySQL 后端 None，Redis 照常；经验/指标走 jsonl | `test_mysql_driver_missing_returns_none`（mock `sys.modules`） |
| Redis 连不上 | registry 退化 `MemoryRunRegistry`（= 原 RUNNING_TASKS）；取消仅限本进程 | `test_redis_unreachable_returns_none`（`url_env` + `MAS_REDIS_URL` 指向假端口） |
| MySQL 连接失败/鉴权失败 | 只降级 + 留痕，**流水线不失败** | 工厂层 try/except 全覆盖；`store status` 的 `ok:false` 报告 |
| 非规范 workspace（测试/临时目录） | 自动全禁用，测试数据不写真实库 | `test_non_canonical_workspace_guard` |
| 进程被杀未走 finally | Redis 键 TTL 到期自动清除 | 注册表 `heartbeat_ttl_s=90` |

---

## 10. 遗留事项（下一步候选）

1. B4/C4（前端读缓存 + SQL 列表）：等一个明确的性能诉求，或先把 MySQL runs 表升级为「可靠投影」（写失败告警而非静默）再做列表源。
2. C3（llm_traces 双写）：schema 已就绪，接 `base.py` 留痕处一个 sink 即可。
3. B5（Redis 分布式锁接入 filelock）：原语已就绪，需要时给 `file_lock()` 加 backend 参数。
4. SSE 实时推送（docs/25 §3.1④）：体验优化，不影响正确性。
