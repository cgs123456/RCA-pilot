-- ============================================================
--  RCA Pilot — 数据库 Schema (PostgreSQL 15+)
--  GxP 合规要求：
--   1. audit_log 不可 UPDATE/DELETE（RULE 阻止 + 应用层不暴露接口）
--   2. 所有时间戳带时区 (TIMESTAMPTZ)
--   3. JSONB 字段不可空（默认 {} 或 []）
--   4. 所有关联外键强制
-- ============================================================

-- 扩展
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- 全文搜索辅助
-- CREATE EXTENSION IF NOT EXISTS "vector";  -- pgvector, Phase 2 启用

-- ============================================================
--  用户表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              VARCHAR(32) PRIMARY KEY,
    email           VARCHAR(200) UNIQUE NOT NULL,
    name            VARCHAR(100) NOT NULL,
    role            VARCHAR(30) NOT NULL CHECK (role IN ('admin','investigator','qa_reviewer','ops_manager','auditor','operator')),
    dept            VARCHAR(50),
    title           VARCHAR(100),
    avatar          VARCHAR(10),
    color           VARCHAR(20) DEFAULT 'ink',
    shift           VARCHAR(20),
    cert            TEXT,
    password_hash   VARCHAR(200),  -- 内置登录（生产对接 OIDC）
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until   TIMESTAMPTZ,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_users_role ON users(role);
CREATE INDEX IF NOT EXISTS ix_users_email ON users(email);

-- ============================================================
--  源系统注册表
-- ============================================================
CREATE TABLE IF NOT EXISTS source_systems (
    code            VARCHAR(10) PRIMARY KEY,  -- LIMS|MES|EMS|CMMS|LMS|BMS
    label           VARCHAR(100) NOT NULL,
    icon            VARCHAR(50) DEFAULT 'database',
    color           VARCHAR(30) DEFAULT 'src-lims',
    description     TEXT,
    connector_url   VARCHAR(200),
    health_status   VARCHAR(20) NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('healthy','degraded','down','unknown')),
    last_sync_at    TIMESTAMPTZ,
    is_readonly     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
--  偏差案例
-- ============================================================
CREATE TABLE IF NOT EXISTS deviations (
    id              VARCHAR(32) PRIMARY KEY,
    title           VARCHAR(500) NOT NULL,
    description     TEXT,
    severity        VARCHAR(20) NOT NULL CHECK (severity IN ('critical','major','minor','info')),
    status          VARCHAR(30) NOT NULL CHECK (status IN ('pending','investigating','reviewing','closed')),
    case_type       VARCHAR(100),
    complexity      INTEGER NOT NULL DEFAULT 1 CHECK (complexity BETWEEN 1 AND 3),
    location        VARCHAR(200),
    area            VARCHAR(200),
    shift           VARCHAR(50),
    investigator_id VARCHAR(32) REFERENCES users(id),
    qa_reviewer_id  VARCHAR(32) REFERENCES users(id),
    discovered_at   TIMESTAMPTZ NOT NULL,
    deadline        TIMESTAMPTZ,
    organism        VARCHAR(100),
    organism_source VARCHAR(200),
    product_impact  TEXT,
    immediate_action TEXT,
    progress        INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    closed_at       TIMESTAMPTZ,
    hitl_gates      JSONB NOT NULL DEFAULT '{}'::jsonb,
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- ICH Q9 风险矩阵（FMEA/RPN）
    risk_rpn        INTEGER CHECK (risk_rpn BETWEEN 1 AND 1000),
    risk_category   VARCHAR(20) CHECK (risk_category IN ('critical','high','medium','low')),
    risk_owner      VARCHAR(32) REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_deviations_status_severity ON deviations(status, severity);
CREATE INDEX IF NOT EXISTS ix_deviations_investigator ON deviations(investigator_id);
CREATE INDEX IF NOT EXISTS ix_deviations_discovered_at ON deviations(discovered_at DESC);

-- ============================================================
--  证据
-- ============================================================
CREATE TABLE IF NOT EXISTS evidence (
    id              VARCHAR(50) PRIMARY KEY,
    case_id         VARCHAR(32) REFERENCES deviations(id) ON DELETE CASCADE,
    source_system   VARCHAR(10) NOT NULL REFERENCES source_systems(code),
    collected_at    TIMESTAMPTZ NOT NULL,
    summary         TEXT,
    raw_data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash    VARCHAR(80) NOT NULL,
    confidence      SMALLINT NOT NULL DEFAULT 3 CHECK (confidence BETWEEN 1 AND 4),
    is_signed       BOOLEAN NOT NULL DEFAULT FALSE,
    embedding       JSONB,
    embedding_model VARCHAR(80),
    embedded_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_evidence_source_case ON evidence(source_system, case_id);
CREATE INDEX IF NOT EXISTS ix_evidence_case_id ON evidence(case_id);
CREATE INDEX IF NOT EXISTS ix_evidence_collected_at ON evidence(collected_at DESC);

-- ============================================================
--  时间线事件
-- ============================================================
CREATE TABLE IF NOT EXISTS timeline_events (
    id              VARCHAR(50) PRIMARY KEY,
    case_id         VARCHAR(32) NOT NULL REFERENCES deviations(id) ON DELETE CASCADE,
    occurred_at     TIMESTAMPTZ NOT NULL,
    source          VARCHAR(20) NOT NULL,
    title           VARCHAR(200) NOT NULL,
    detail          TEXT,
    severity        VARCHAR(20) NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','danger')),
    evidence_id     VARCHAR(50) REFERENCES evidence(id),
    is_anomaly      BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_timeline_case ON timeline_events(case_id);
CREATE INDEX IF NOT EXISTS ix_timeline_occurred ON timeline_events(occurred_at);

-- ============================================================
--  5 Whys
-- ============================================================
CREATE TABLE IF NOT EXISTS five_whys (
    id              VARCHAR(50) PRIMARY KEY,
    case_id         VARCHAR(32) NOT NULL REFERENCES deviations(id) ON DELETE CASCADE,
    n               INTEGER NOT NULL CHECK (n BETWEEN 1 AND 5),
    question        TEXT,
    answer          TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected','edited')),
    evidence_ids    TEXT[] NOT NULL DEFAULT '{}',
    is_root_cause   BOOLEAN NOT NULL DEFAULT FALSE,
    ai_model        VARCHAR(100),
    ai_output_hash  VARCHAR(80),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(case_id, n)
);
CREATE INDEX IF NOT EXISTS ix_fivewhys_case ON five_whys(case_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_five_whys_case_n ON five_whys(case_id, n);

-- ============================================================
--  Skeptic 反例挑战
-- ============================================================
CREATE TABLE IF NOT EXISTS skeptic_challenges (
    id              VARCHAR(50) PRIMARY KEY,
    case_id         VARCHAR(32) NOT NULL REFERENCES deviations(id) ON DELETE CASCADE,
    target          TEXT NOT NULL,
    challenge       TEXT NOT NULL,
    evidence_ids    TEXT[] NOT NULL DEFAULT '{}',
    status          VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    resolution      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_skeptic_case ON skeptic_challenges(case_id);

-- ============================================================
--  Agent 运行记录
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_runs (
    id              VARCHAR(50) PRIMARY KEY,
    case_id         VARCHAR(32) NOT NULL REFERENCES deviations(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','paused','completed','failed')),
    current_agent   VARCHAR(30),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    agent_states    JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message   TEXT,
    triggered_by    VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_agentruns_case ON agent_runs(case_id);
CREATE INDEX IF NOT EXISTS ix_agentruns_status ON agent_runs(status);

-- ============================================================
--  审计日志 — 不可篡改
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    log_id          VARCHAR(80) PRIMARY KEY,
    seq             BIGSERIAL NOT NULL UNIQUE,
    operator        VARCHAR(100) NOT NULL,
    action          VARCHAR(50) NOT NULL,
    target          VARCHAR(200),
    detail          TEXT,
    ip_address      VARCHAR(50),
    session_id      VARCHAR(80),
    evidence_refs   TEXT[] NOT NULL DEFAULT '{}',
    ai_model        VARCHAR(100),
    ai_prompt_hash  VARCHAR(80),
    ai_output_hash  VARCHAR(80),
    trace_id        VARCHAR(36),
    prev_hash       VARCHAR(80) NOT NULL,
    current_hash    VARCHAR(80) NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_log(target);
CREATE INDEX IF NOT EXISTS ix_audit_operator_created ON audit_log(operator, created_at DESC);

-- GxP 硬性约束：禁止 UPDATE / DELETE
-- 使用触发器抛异常而非 RULE DO INSTEAD NOTHING（后者静默吞掉操作，掩盖错误）
CREATE OR REPLACE FUNCTION audit_log_immutable_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log 表受 GxP 合规保护，禁止 % 操作 (log_id=%, seq=%)',
        TG_OP, COALESCE(OLD.log_id, NEW.log_id), COALESCE(OLD.seq, NEW.seq);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log;
CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_guard();

DROP TRIGGER IF EXISTS trg_audit_log_no_delete ON audit_log;
CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_guard();

-- TRUNCATE 不触发 DELETE 触发器 —— 必须单独拦截，否则一次 TRUNCATE 即可清空全部审计记录
CREATE OR REPLACE FUNCTION audit_log_no_truncate_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log 表受 GxP 合规保护，禁止 TRUNCATE 操作';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log;
CREATE TRIGGER trg_audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR STATEMENT EXECUTE FUNCTION audit_log_no_truncate_guard();

-- 触发器：自动更新 updated_at
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_deviations_updated BEFORE UPDATE ON deviations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_fivewhys_updated BEFORE UPDATE ON five_whys
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ============================================================
--  电子签名记录 — 21 CFR Part 11
-- ============================================================
CREATE TABLE IF NOT EXISTS signatures (
    id              VARCHAR(50) PRIMARY KEY,
    case_id         VARCHAR(32) NOT NULL REFERENCES deviations(id),
    signer_id       VARCHAR(32) NOT NULL REFERENCES users(id),
    signer_role     VARCHAR(30) NOT NULL,
    signature_meaning VARCHAR(100) NOT NULL,
    signed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    certificate_fingerprint VARCHAR(80),
    ip_address      VARCHAR(50),
    confirmation_text TEXT NOT NULL DEFAULT '我确认本电子签名等同于手写签名，所提交内容真实准确'
);
CREATE INDEX IF NOT EXISTS ix_signatures_case ON signatures(case_id);
CREATE INDEX IF NOT EXISTS ix_signatures_signer ON signatures(signer_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_signatures_case_meaning_signer ON signatures(case_id, signature_meaning, signer_id);

-- 电子签名记录同样是 GxP 关键记录 —— 禁止 UPDATE/DELETE/TRUNCATE
CREATE OR REPLACE FUNCTION signatures_immutable_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'signatures 表受 21 CFR Part 11 保护，禁止 % 操作', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_signatures_no_update ON signatures;
CREATE TRIGGER trg_signatures_no_update BEFORE UPDATE ON signatures
    FOR EACH ROW EXECUTE FUNCTION signatures_immutable_guard();

DROP TRIGGER IF EXISTS trg_signatures_no_delete ON signatures;
CREATE TRIGGER trg_signatures_no_delete BEFORE DELETE ON signatures
    FOR EACH ROW EXECUTE FUNCTION signatures_immutable_guard();

DROP TRIGGER IF EXISTS trg_signatures_no_truncate ON signatures;
CREATE TRIGGER trg_signatures_no_truncate BEFORE TRUNCATE ON signatures
    FOR STATEMENT EXECUTE FUNCTION signatures_immutable_guard();

-- ============================================================
--  updated_at 触发器（已建在上方）
-- ============================================================
COMMENT ON TABLE audit_log IS 'GxP 审计日志 — 不可篡改，DB 触发器阻止 UPDATE/DELETE/TRUNCATE';
COMMENT ON COLUMN audit_log.current_hash IS 'sha256(v2|prev_hash|seq|operator|action|target|detail|ts|evidence_refs|ip|session|ai_model|ai_prompt_hash|ai_output_hash|trace_id)';
COMMENT ON COLUMN evidence.content_hash IS 'sha256(raw_data_json_normalized)';
COMMENT ON TABLE deviations IS '偏差案例主表';

-- ============================================================
--  CAPA（纠正预防措施）— GxP 合规必备
-- ============================================================
CREATE TABLE IF NOT EXISTS capas (
    id                 VARCHAR(50) PRIMARY KEY,
    case_id            VARCHAR(32) NOT NULL REFERENCES deviations(id),
    title              VARCHAR(500) NOT NULL,
    description        TEXT,
    severity           VARCHAR(20) NOT NULL CHECK (severity IN ('critical','major','minor','info')),
    status             VARCHAR(30) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','under_review','implemented','verified','closed')),
    responsibility     VARCHAR(100) NOT NULL,
    due_date           TIMESTAMPTZ,
    verified_by        VARCHAR(32) REFERENCES users(id),
    verified_at        TIMESTAMPTZ,
    effectiveness_rate INTEGER CHECK (effectiveness_rate BETWEEN 0 AND 100),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_capas_case_id ON capas(case_id);
CREATE INDEX IF NOT EXISTS ix_capas_status ON capas(status);
CREATE INDEX IF NOT EXISTS ix_capas_due_date ON capas(due_date);

-- ============================================================
--  ICH Q9 十节格式报告（版本化管理，受 GxP 文件控制）
-- ============================================================
CREATE TABLE IF NOT EXISTS reports (
    id              VARCHAR(50) PRIMARY KEY,
    case_id         VARCHAR(32) NOT NULL REFERENCES deviations(id),
    version         INTEGER NOT NULL DEFAULT 1,
    title           VARCHAR(500) NOT NULL,
    content         TEXT NOT NULL,
    status          VARCHAR(30) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','under_review','approved','rejected')),
    author_id       VARCHAR(32) NOT NULL REFERENCES users(id),
    approved_by     VARCHAR(32) REFERENCES users(id),
    approved_at     TIMESTAMPTZ,
    change_reason   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reports_case_version_uniq UNIQUE (case_id, version)
);
CREATE INDEX IF NOT EXISTS ix_reports_case_id ON reports(case_id);
CREATE INDEX IF NOT EXISTS ix_reports_status ON reports(status);
