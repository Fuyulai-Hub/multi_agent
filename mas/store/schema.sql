-- =============================================================================
-- multi_agent 存储 Schema（MySQL 8.0，InnoDB / utf8mb4）
-- 执行：python -m mas store init   （幂等，全部 IF NOT EXISTS）
--
-- 分层原则（docs/25）：文件系统仍是证据层 SSOT（events.jsonl / artifacts/v*.json
-- 不动），本 schema 只建「可查询的索引/结构层」：
--   experiences      经验库主存储（UPSERT 替代全文件读-改-写 + 全局文件锁）
--   runs             运行索引（替代前端 O(n) 目录扫描）
--   run_metrics      每轮效果指标
--   artifacts_index  黑板产物版本索引（文件本体仍在磁盘）
--   run_events       事件日志索引（双写，SSOT 仍是 events.jsonl）
--   llm_traces       LLM 调用留痕索引（双写，SSOT 仍是 llm_traces.jsonl）
-- =============================================================================

CREATE TABLE IF NOT EXISTS experiences (
    id             VARCHAR(64)  NOT NULL COMMENT '经验 ID（exp-...）',
    key_hash       VARCHAR(64)  NOT NULL COMMENT '去重键 sha1(scope||action_key||dedupe)',
    tier           VARCHAR(8)   NOT NULL DEFAULT 'L1' COMMENT '层级 L1/L2/L3',
    scope          VARCHAR(16)  NOT NULL DEFAULT 'run' COMMENT 'run/project/global',
    roles          JSON         NULL COMMENT '适用角色列表',
    tags           JSON         NULL COMMENT '标签列表',
    `trigger`      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '触发事件类型（MySQL 保留字，须反引号）',
    lesson         MEDIUMTEXT   NULL COMMENT '经验内容',
    action         MEDIUMTEXT   NULL COMMENT '建议动作',
    action_key     VARCHAR(128) NOT NULL DEFAULT '' COMMENT '离线 Provider 消费的结构化动作键',
    dedupe         VARCHAR(255) NOT NULL DEFAULT '' COMMENT '去重细分维度',
    confidence     DOUBLE       NOT NULL DEFAULT 0.5,
    hits           INT          NOT NULL DEFAULT 0,
    hits_success   INT          NOT NULL DEFAULT 0,
    hits_failed    INT          NOT NULL DEFAULT 0,
    evidence_count INT          NOT NULL DEFAULT 1,
    source_run     VARCHAR(64)  NOT NULL DEFAULT '',
    source_refs    JSON         NULL COMMENT '证据引用（事件 ID 等）',
    created_at     VARCHAR(40)  NOT NULL DEFAULT '',
    last_used_at   VARCHAR(40)  NULL,
    status         VARCHAR(16)  NOT NULL DEFAULT 'active',
    PRIMARY KEY (id),
    UNIQUE KEY uk_key_hash (key_hash),
    KEY idx_tier_scope (tier, scope),
    KEY idx_action_key (action_key),
    KEY idx_source_run (source_run)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='分级经验库（L1/L2/L3）';

CREATE TABLE IF NOT EXISTS runs (
    run_id              VARCHAR(64)  NOT NULL,
    requirement_title   MEDIUMTEXT   NULL,
    verdict             VARCHAR(32)  NULL COMMENT 'pass/fail',
    steps               INT          NOT NULL DEFAULT 0,
    rollbacks           INT          NOT NULL DEFAULT 0,
    review_rejections   INT          NOT NULL DEFAULT 0,
    bugs_total          INT          NOT NULL DEFAULT 0,
    bugs_fixed          INT          NOT NULL DEFAULT 0,
    duration_s          DOUBLE       NULL,
    injected_experiences INT         NOT NULL DEFAULT 0,
    profile             VARCHAR(64)  NULL,
    ts                  VARCHAR(40)  NULL COMMENT '运行开始时间（ISO 8601）',
    finished            TINYINT      NOT NULL DEFAULT 0,
    canceled            TINYINT      NOT NULL DEFAULT 0,
    updated_at          TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id),
    KEY idx_verdict (verdict),
    KEY idx_profile (profile),
    KEY idx_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运行索引（run_summary 的可查询投影）';

CREATE TABLE IF NOT EXISTS run_metrics (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id     VARCHAR(64) NOT NULL,
    metrics    JSON        NOT NULL COMMENT 'metrics.jsonl 单行的结构化投影',
    created_at TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_run (run_id),
    KEY idx_acceptance ((CAST(metrics->>'$.acceptance' AS CHAR(8))))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每轮效果指标';

CREATE TABLE IF NOT EXISTS artifacts_index (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id     VARCHAR(64)  NOT NULL,
    name       VARCHAR(128) NOT NULL COMMENT '产物名（黑板键）',
    version    INT          NOT NULL,
    producer   VARCHAR(64)  NULL,
    node       VARCHAR(64)  NULL,
    iteration  INT          NULL,
    digest     VARCHAR(32)  NULL COMMENT '内容摘要（与磁盘 v*.json 对应）',
    path       MEDIUMTEXT   NULL COMMENT '磁盘路径（文件本体不动）',
    created_at VARCHAR(40)  NULL,
    UNIQUE KEY uk_run_artifact (run_id, name, version),
    KEY idx_run (run_id),
    KEY idx_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='黑板产物版本索引';

CREATE TABLE IF NOT EXISTS run_events (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id     VARCHAR(64) NOT NULL,
    seq        INT         NOT NULL COMMENT '运行内序号（与 events.jsonl 一致）',
    type       VARCHAR(64) NOT NULL,
    agent      VARCHAR(64) NULL,
    node       VARCHAR(64) NULL,
    iteration  INT         NULL,
    severity   VARCHAR(16) NULL,
    payload    JSON        NULL,
    ts         VARCHAR(40) NULL,
    UNIQUE KEY uk_run_seq (run_id, seq),
    KEY idx_type (type),
    KEY idx_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='事件日志索引（双写，SSOT 仍是 events.jsonl）';

CREATE TABLE IF NOT EXISTS llm_traces (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id           VARCHAR(64)  NOT NULL,
    agent            VARCHAR(64)  NULL,
    node             VARCHAR(64)  NULL,
    iteration        INT          NULL,
    provider         VARCHAR(64)  NULL,
    model            VARCHAR(128) NULL,
    latency_ms       INT          NOT NULL DEFAULT 0,
    tokens           INT          NOT NULL DEFAULT 0,
    finish_reason    VARCHAR(32)  NULL,
    prompt_tokens    INT          NOT NULL DEFAULT 0,
    completion_tokens INT         NOT NULL DEFAULT 0,
    reasoning_tokens INT          NOT NULL DEFAULT 0,
    degraded         TINYINT      NOT NULL DEFAULT 0,
    error            MEDIUMTEXT   NULL,
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_run (run_id),
    KEY idx_model (model)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='LLM 调用留痕索引（双写，SSOT 仍是 llm_traces.jsonl）';
