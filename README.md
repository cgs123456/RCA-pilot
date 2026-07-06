# RCA Pilot

受 GxP 监管的 Agentic AI 偏差调查平台，面向生物制药企业提供端到端的根因分析能力。系统融合多 Agent 编排、HITL 人工决策门控、不可篡改审计链与 21 CFR Part 11 电子签名，支持从偏差告警接入到 ICH Q9 报告签发的完整闭环。

> **Phase 2 更新（2026-07）**：6 项改进全部落地，Agent 流水线全面 LLM 化（Reasoner / Skeptic / Drafter 三大 Agent 接入 LLM Chain-of-Thought），新增多 Agent 博弈验证（双 Reasoner 并行 + Skeptic 交叉质证 + Meta-Controller 综合置信度），Evidence Hunter 升级语义检索 + 跨案例关联，Connector 扩展至 4 个（LMS + BMS + EMS + MES）。测试总数从 53 增长至 **202**（0 回归）。详见 [§ Phase 2 LLM 化改造](#phase-2-llm-化改造)。

## 1. 项目概述

RCA Pilot 围绕"偏差调查 4 小时交付可审计结构化草稿"的核心目标设计，主要特性包括：

- 多 Agent 串行编排流水线：Meta-Controller → Evidence Hunter → Reasoner → Skeptic → Drafter → Guardian，每步输出计算 hash 并写入审计日志。
- **多 Agent 博弈验证**（Phase 2 新增）：并行启动 2 个 Reasoner（primary + adversary，不同 LLM/model），Skeptic 交叉质证两份推理，Meta-Controller 输出置信度加权综合结论（0.0-1.0）。
- **统一 LLM 客户端**（Phase 2 新增）：支持 GLM / Qwen / OpenAI / Anthropic / Azure 五大 Provider，OpenAI 兼容协议，JSON 输出 + Pydantic schema 二次校验，指数退避重试 + 三级降级保护。
- **语义检索 + 跨案例关联**（Phase 2 新增）：Evidence Hunter 接入 LLM Embedding API（GLM embedding-3 / Qwen text-embedding-v3 / OpenAI text-embedding-3-small），证据向量存储 JSONB + 纯 Python 余弦相似度计算，关键词搜索自动降级。
- 5 道 HITL 人工决策关卡，覆盖证据完整性、5 Whys 推理、Skeptic 反例、CAPA 评审、报告签发，不可跳过。
- GxP 合规内建：ALCOA+ 9 项原则、21 CFR Part 11 双人电子签名、SHA-256 hash 链审计日志、DB 层 RULE 阻止 UPDATE/DELETE、LLM 调用全量审计（`ai_model` / `ai_prompt_hash` / `ai_output_hash`）。
- 4 个源系统只读 Connector（LMS 培训管理 + BMS 楼宇管理 + EMS 环境监测 + MES 生产执行），证据数据不写入源系统，`fetch_from_source` 端点统一编排。
- 微服务架构：Auth/Case/Evidence/Agent/Audit 5 个业务服务 + 4 个 Connector，共享 backend/shared 层。
- 大厂级工程基线：pytest 测试套件（202 条测试，含 LLM 路径 + 博弈 + 语义检索 + Connector）、Alembic 版本化迁移、CI/CD 流水线、Prometheus 监控、slowapi 限流、Sentry 异常捕捉、Feature Flag 灰度框架（6 个 flag 独立灰度）。

项目同时包含两套前端：

- `demo/`：基于 Alpine.js + Tailwind CDN 的原型版，含完整 mock 数据，开箱即用，用于产品演示。
- `frontend/`：基于 React 18 + TypeScript + Vite 的生产版，已实现登录、仪表盘、收件箱、调查工作台、审计追踪 5 个核心页面，其余页面为占位。

## 2. 技术栈

### 前端

- React 18 + TypeScript + Vite
- Tailwind CSS + lucide-react 图标
- Zustand 状态管理 + TanStack Query 数据请求
- React Router v6 路由 + Axios HTTP 客户端
- d3 数据可视化
- demo 版：Alpine.js + Tailwind CDN

### 后端

- Python 3.11 + FastAPI
- SQLAlchemy 2.0（async）+ asyncpg
- Pydantic v2 + pydantic-settings
- PyJWT + bcrypt（直接调用，未使用 passlib）
- slowapi 限流 + prometheus-client 指标 + sentry-sdk 异常捕捉
- Unleash Client（Feature Flag）
- Alembic 数据库迁移
- **httpx**（LLM API 异步调用 + Connector 编排）
- **统一 LLM 客户端**（`shared/llm_client.py`，支持 GLM / Qwen / OpenAI / Anthropic / Azure 五大 Provider，OpenAI 兼容协议，JSON 模式 + Pydantic schema 校验 + Embedding API）
- **纯 Python 向量检索**（`shared/vector_store.py`，无 numpy/Chroma/faiss 依赖，余弦相似度 + Jaccard 关键词降级）
- pytest + pytest-asyncio + ruff + mypy

### 数据库与中间件

- PostgreSQL 15（JSONB 存储证据/hitl_gates 等半结构化数据）
- Redis 7（会话/缓存，预留）

### 开发与运维工具

- Docker Compose 单机编排 + Nginx 反向代理
- GitHub Actions（CI：lint/typecheck/test/alembic check；CD：staging/prod 部署）
- pre-commit hooks（ruff + mypy + eslint）
- Dependabot 依赖更新
- k6 负载测试脚本
- Grafana dashboard + Prometheus 抓取配置

## 3. 项目架构

### 整体架构

```
                    Frontend (React + TS)
                         │  Nginx :8080
        ┌────────────────┼────────────────┐
        │                │                │
   Auth Service    Case Service    Evidence Service
     :8001           :8002            :8003
        │                │                │
        │           Agent Service    Audit Service
        │              :8004            :8005
        │                │                │
        └────────────────┼────────────────┘
                         │
              ┌──────────┴──────────┐
              │  PostgreSQL 15      │
              │  Redis 7            │
              │                     │
              │  Connectors:         │
              │   LMS  :8011        │
              │   BMS  :8012        │
              │   EMS  :8013        │
              │   MES  :8014        │
              │                     │
              │  LLM Providers:      │
              │   GLM / Qwen /       │
              │   OpenAI / Anthropic │
              │   / Azure            │
              └─────────────────────┘
```

### 模块关系

- `backend/shared/`：所有微服务共用的基础设施层，包括应用工厂（`service_base.create_app`）、ORM 模型、Pydantic schemas、认证中间件、审计 hash 链、错误处理器、限流、metrics、feature flag、配置管理等。Phase 2 新增 `llm_client.py`（统一 LLM 客户端，支持 5 大 Provider）和 `vector_store.py`（纯 Python 向量检索）。
- 5 个业务服务各自包含 `main.py`（入口，调用 `create_app` 注册路由）和 `routes.py`（业务端点实现），通过 `shared` 层复用基础能力。
- `backend/connectors/`：4 个独立的 Mock Connector 服务，模拟 LMS 培训管理 / BMS 楼宇管理 / EMS 环境监测 / MES 生产执行系统的 API，供 Evidence Service 拉取证据。每个 Connector 仅 `main.py` + `routes.py` ~30+150 行代码。
- 前端通过 `/api/v1/*` 前缀访问后端，由 Nginx 反向代理转发到对应微服务。

### 数据流向

1. 用户登录 → Auth Service 校验密码 → 签发 JWT（access 15 分钟 + refresh 7 天）。
2. 用户创建偏差案例 → Case Service 写入 `deviations` 表 + 初始化 5 道 HITL 关卡 + 审计日志。
3. 用户启动 Agent 流水线 → Agent Service 串行执行 6 个 Agent，每步输出 hash 存入 `agent_runs` 表 + 审计日志。
4. HITL 关卡处理 → Case Service 更新 `hitl_gates` JSONB 字段，5 关全通过后状态自动转为 `reviewing`。
5. 报告签发 → Case Service `/signatures` 端点接收双人电子签名 → 状态转为 `closed`。
6. Evidence Service 跨 6 源系统聚合证据，时间线组织。
7. Audit Service 提供 hash 链验证、CSV/JSON 导出、Feature Flag 管理端点。

## 4. 目录结构

```
RCA-pilot/
├── backend/                          # Python FastAPI 后端
│   ├── shared/                       # 共享基础设施层
│   │   ├── service_base.py           # FastAPI 应用工厂（TraceId/异常/metrics/限流）
│   │   ├── models.py                 # SQLAlchemy ORM 模型（User/Deviation/Evidence 等）
│   │   ├── schemas.py                # Pydantic 请求/响应模型（含泛型 Page[T]）
│   │   ├── auth_middleware.py        # JWT 认证 + RBAC 权限 + AuthContext 依赖注入
│   │   ├── security.py               # JWT 签发/校验 + bcrypt 密码哈希
│   │   ├── audit.py                  # 审计日志 hash 链（SHA-256 链式）
│   │   ├── error_handler.py          # 全局异常处理器 + traceId 透传中间件
│   │   ├── error_codes.py            # 错误码枚举 + HTTP 状态码映射
│   │   ├── exceptions.py             # 业务异常基类（RCAError 体系）
│   │   ├── rate_limit.py             # slowapi 限流装饰器（login/api/write）
│   │   ├── metrics.py                # Prometheus 4 黄金信号中间件
│   │   ├── feature_flag.py           # Feature Flag 框架（本地 + Unleash，6 个 flag 独立灰度）
│   │   ├── database.py               # AsyncEngine + AsyncSessionLocal
│   │   ├── config.py                 # Pydantic Settings 环境变量配置
│   │   ├── llm_client.py             # 统一 LLM 客户端（GLM/Qwen/OpenAI/Anthropic/Azure）
│   │   ├── vector_store.py           # 纯 Python 向量检索（cosine + Jaccard 关键词降级）
│   │   └── logging.py                # 结构化 JSON 日志
│   ├── auth_service/                 # 认证服务（登录/刷新/当前用户）
│   ├── case_service/                 # 案例服务（CRUD + HITL + 5Whys + 签名 + Dashboard KPI）
│   ├── evidence_service/             # 证据服务（列表/详情/时间线/Connector 编排/语义检索/跨案例关联）
│   ├── agent_service/                # Agent 服务（6 Agent 流水线 + 多 Agent 博弈 + LLM 路径）
│   ├── audit_service/                # 审计服务（hash 链验证/导出/Feature Flag 管理）
│   ├── connectors/
│   │   ├── lms/                      # LMS Mock Connector（培训管理，:8011）
│   │   ├── bms/                      # BMS Mock Connector（楼宇管理 HVAC，:8012）
│   │   ├── ems/                      # EMS Mock Connector（环境监测沉降菌，:8013）
│   │   └── mes/                      # MES Mock Connector（生产执行批次/清洁/CCTV，:8014）
│   ├── alembic/                      # 版本化迁移
│   │   ├── env.py                    # 异步迁移环境
│   │   └── versions/                 # 迁移脚本
│   ├── tests/                        # pytest 测试套件（202 条测试）
│   │   ├── conftest.py               # 异步 fixture + SQLite 测试 DB
│   │   ├── test_auth.py              # 登录/刷新/锁定
│   │   ├── test_case_crud.py         # 案例 CRUD + 状态机校验
│   │   ├── test_hitl.py              # 5 关 accept/reject
│   │   ├── test_agent_pipeline.py    # 6 Agent 流水线 + hash 输出
│   │   ├── test_audit_chain.py       # hash 链完整性
│   │   ├── test_reasoner_llm.py      # Reasoner LLM 路径 + 客户端配置（38 测试）
│   │   ├── test_skeptic_llm.py       # Skeptic LLM 动态对抗反例（16 测试）
│   │   ├── test_evidence_semantic.py # 语义检索 + 向量相似度（29 测试）
│   │   ├── test_drafter_llm.py       # Drafter LLM ICH Q9 报告生成（13 测试）
│   │   ├── test_multi_agent_debate.py# 多 Agent 博弈 + Meta-Controller（23 测试）
│   │   └── test_connectors.py        # BMS/EMS/MES Connector + fetch_from_source（30 测试）
│   ├── db/01_schema.sql              # 数据库初始化 schema（含 RULE 防篡改）
│   ├── seed.py                       # 种子数据（3 用户 + 4 案例 + 24 证据）
│   ├── requirements.txt
│   ├── pyproject.toml                # ruff/mypy/pytest 配置
│   └── alembic.ini
├── frontend/                         # React + TS 生产版前端
│   ├── src/
│   │   ├── App.tsx                   # 路由 + 认证守卫
│   │   ├── main.tsx                  # 应用入口
│   │   ├── components/
│   │   │   ├── AppShell.tsx          # 左侧导航 + 顶栏 + 主内容区
│   │   │   └── ui/                   # Badge/Button/Card 基础组件
│   │   ├── pages/                    # 登录/仪表盘/收件箱/调查/审计 + 占位页
│   │   ├── services/
│   │   │   ├── api-client.ts         # Axios 封装 + JWT 拦截器 + 401 自动刷新
│   │   │   ├── auth.service.ts
│   │   │   ├── case.service.ts
│   │   │   ├── agent.service.ts
│   │   │   └── audit.service.ts
│   │   ├── stores/auth.store.ts      # Zustand 认证状态
│   │   ├── types/index.ts            # TypeScript 类型定义
│   │   └── lib/utils.ts              # 工具函数
│   ├── vite.config.ts                # Vite 配置 + /api 代理
│   ├── tailwind.config.js
│   └── package.json
├── demo/                             # Alpine.js 原型版（完整 mock 数据）
│   ├── index.html
│   ├── css/styles.css
│   └── js/
│       ├── app.js                    # 主应用 + 状态管理 + 路由
│       ├── agents.js                 # Agent 流水线模拟
│       ├── charts.js                 # 图表渲染
│       └── mockData.js               # 完整演示数据
├── deploy/                           # 部署文件
│   ├── docker-compose.yml            # 9 服务编排（5 后端 + 1 前端 + PG + Redis + LMS + seed）
│   ├── Dockerfile.backend            # 多阶段构建（Python 3.11-slim）
│   ├── Dockerfile.frontend           # 多阶段构建（Node 20 + Nginx）
│   ├── nginx.conf                    # 反向代理 + 静态资源
│   ├── prometheus.yml                # 抓取 /metrics 端点
│   ├── grafana-dashboard.json        # 4 黄金信号面板
│   ├── k6/load-test.js               # 负载测试脚本
│   └── .env.example                  # 环境变量模板
├── docs/adr/                         # 架构决策记录
│   └── 001-fastapi-choice.md         # 为何选择 FastAPI
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                    # lint → typecheck → test → alembic check
│   │   └── cd.yml                    # 部署 staging/prod
│   └── dependabot.yml                # Python + npm 依赖自动更新
└── .pre-commit-config.yaml           # ruff + mypy + eslint
```

## 5. 核心文件说明

### 项目入口与配置

- `backend/shared/service_base.py`：FastAPI 应用工厂，所有微服务通过 `create_app(service_name)` 创建实例，统一集成 TraceId 中间件、全局异常处理器、Prometheus metrics、slowapi 限流、`/health` 与 `/ready` 端点。支持 `enable_metrics` / `enable_rate_limit` 开关用于测试环境禁用。
- `backend/shared/config.py`：基于 pydantic-settings 的统一配置，从环境变量读取数据库、Redis、JWT、CORS、服务端口、Sentry DSN、Unleash URL 等配置。`@lru_cache` 单例模式，所有服务共用。
- `frontend/vite.config.ts`：Vite 配置，包含 `/api` 代理到后端服务和路径别名 `@` → `src/`。
- `deploy/docker-compose.yml`：9 服务编排，PostgreSQL 健康检查后启动后端微服务，前端依赖后端就绪后启动，`seed` 服务通过 `profiles: ["seed"]` 按需运行。
- `backend/alembic/env.py`：异步迁移环境，自动从 `shared.models` 导入全部 ORM 模型让 `Base.metadata` 识别所有表。
- `backend/pyproject.toml`：ruff/mypy/pytest 统一配置，CI 流水线引用。

### 核心业务逻辑

- `backend/agent_service/routes.py`：Agent 流水线编排引擎。定义 `AGENT_PIPELINE` 顺序数组，实现 `_run_evidence_hunter` / `_run_reasoner` / `_run_skeptic` / `_run_drafter` / `_run_guardian` 6 个 Agent 函数。每步输出通过 `_hash_output()` 计算 SHA-256 hash 并写入审计日志。`start_pipeline` 端点使用 `BackgroundTasks` 异步执行，避免阻塞请求。Phase 2 改造后：
  - **Reasoner**：`_run_reasoner` 支持 LLM 路径（Pydantic schema `_LLMReasonerOutput` 校验 5 Whys + 6M 鱼骨图），三级降级（flag off → rule-based；LLM 未配置 → rule-template-v1；LLM 异常 → fallback）。
  - **Skeptic**：`_run_skeptic` 支持 LLM 动态生成 `alternative_explanation` / `independent_cause` / `evidence_gap` 三类对抗反例，schema `_LLMSkepticOutput` 校验。
  - **Drafter**：`_run_drafter` 支持 LLM 生成 ICH Q9 十节格式 Markdown 报告，自动剥离 ` ```markdown ` 代码围栏，使用 `chat_completion`（非 `chat_json`，因输出是 Markdown 文本）。
  - **多 Agent 博弈**（改进5）：`_run_reasoner_debate` 用 `asyncio.gather` 并行启动 primary + adversary 两个 Reasoner，`_run_skeptic_cross_examine_with_llm` 扩展质证维度（新增 `contradiction` / `evidence_conflict`），`_run_meta_controller_synthesis_with_llm` 综合置信度加权结论（0.0-1.0 + agreement/disagreement points），`_run_drafter_debate` 用 Meta-Controller 结论生成 ICH Q9 报告。
  - 所有 LLM 路径写入审计日志：`ai_model` / `ai_prompt_hash` / `ai_output_hash` 三字段，满足 GxP 可追溯性。
- `backend/case_service/routes.py`：案例 CRUD + HITL 关卡管理。`handle_hitl_gate` 端点处理 accept/reject/edit，gate3 reject 时若 `hitl_gate3_auto_retry` flag 启用则触发 Reasoner 重试。`update_case` 端点实现状态机校验，禁止通过 PATCH 直接设 `status=closed`（必须走 `/signatures` 双签流程）或 `status=reviewing`（系统态）。`create_case` 捕获 `IntegrityError` 处理重复 ID，使用 `selectinload` 避免 async 懒加载报错。`sign_case` 端点实现 21 CFR Part 11 双人签名（investigator 提交 + qa_reviewer 批准）。
- `backend/auth_service/routes.py`：登录/刷新/当前用户。`login` 端点校验密码后签发 access + refresh token，失败 5 次锁定账号。关键修复：失败计数必须 `await db.commit()` 后再 `raise AuthError`，否则事务回滚丢失计数。`refresh` 端点使用 `@refresh_limit` 装饰器，需声明 `request: Request` 参数供 slowapi 读取客户端 IP。
- `backend/evidence_service/routes.py`：证据查询 + 时间线 + Connector 编排 + 语义检索。`fetch_from_source` 端点支持 LMS/BMS/EMS/MES 四个 Connector，每个 Connector 路由到其默认拉取端点（如 `/bms/ahus` / `/ems/exceedances` / `/mes/cctv/anomalies`）。Phase 2 新增 4 个语义检索端点：`POST /evidence/{id}/embed`（单条向量化）、`POST /evidence/embed-batch`（批量嵌入）、`GET /evidence/search`（语义搜索，向量降级到关键词）、`GET /evidence/related-cases`（跨案例关联聚合）。

### 数据模型与 API

- `backend/shared/models.py`：SQLAlchemy ORM 模型，核心表包括：
  - `User`：用户表，role 字段受 DB 层 CHECK 约束（admin/investigator/qa_reviewer/ops_manager/auditor/operator）。
  - `Deviation`：偏差案例表，`hitl_gates` 字段为 JSONB 存储 `{gate1: pending|done|rejected, ...}`。
  - `Evidence`：证据表，关联案例与源系统，含 `ai_output_hash` 字段。Phase 2 新增 `embedding`（JSONB 向量数组）/ `embedding_model` / `embedded_at` 字段，支持语义检索（向量存储 JSONB 避免 sqlite-vec/Chroma 依赖）。
  - `AuditLog`：审计日志表，`prev_hash` + `current_hash` 形成链式结构，DB 层 CREATE RULE 阻止 UPDATE/DELETE。Phase 2 新增 `ai_model` / `ai_prompt_hash` / `ai_output_hash` 三字段，记录 LLM 调用元信息满足 GxP 可追溯性。
  - `SignatureRecord`：电子签名记录表，关联案例与签字人。
  - `FiveWhy` / `SkepticChallenge` / `TimelineEvent` / `AgentRun` / `SourceSystem` 等。
- `backend/shared/schemas.py`：Pydantic v2 请求/响应模型。`Page(BaseModel, Generic[T])` 支持泛型参数化（如 `Page[DeviationOut]`），供 FastAPI 自动生成 OpenAPI schema。
- `backend/shared/audit.py`：审计日志 hash 链实现。`_hash_payload()` 将 prev_hash + seq + operator + action + target + detail + timestamp + evidence_refs 拼接后计算 SHA-256。`append_audit_log()` 使用独立事务确保审计日志不被业务回滚。`verify_hash_chain()` 提供完整性校验。
- `backend/shared/auth_middleware.py`：`AuthContext` 数据类封装请求认证上下文（user/session_id/ip/permissions/db），通过 `Depends(get_auth_context)` 注入到受保护端点。`get_db_from_auth` 依赖复用 AuthContext 已建立的 DB session，避免连接池耗尽（Critical 修复）。
- `backend/shared/security.py`：直接调用 `bcrypt.hashpw` / `bcrypt.checkpw` 实现密码哈希，手动 72 字节截断。替代 passlib 1.7.4（与 bcrypt 4.x 不兼容）。
- `backend/shared/rate_limit.py`：slowapi 限流装饰器。`login_limit`（10/min/IP）、`refresh_limit`（20/min/IP）、`api_limit`（100/s/IP）、`write_limit`（30/min/IP）。`_client_ip()` 穿透 `X-Forwarded-For` / `X-Real-IP` 代理头。
- `backend/shared/error_handler.py`：全局异常处理器，将 `RCAError` 子类映射到 `ErrorCode` 枚举，统一返回 `{code, message, detail, trace_id}` 格式。`ValidationError`（业务校验）映射到 `BUSINESS_VALIDATION_ERROR`（HTTP 400），区别于 `RequestValidationError`（pydantic 格式错误，HTTP 422）。5xx 错误自动上报 Sentry。
- `backend/shared/feature_flag.py`：Feature Flag 框架。支持本地 JSON 配置（默认）和 Unleash 远程模式（生产）。`is_enabled(flag, user_id)` 按 user_id hash 分桶实现百分比灰度。环境变量 `RCA_FEATURE_FLAGS` 支持热覆盖。已接入 6 个 flag：`skeptic_v2_enhanced` / `reasoner_llm_enabled` / `hitl_gate3_auto_retry` / `evidence_hunter_semantic` / `drafter_llm_enabled` / `multi_agent_debate_enabled`，每个 Agent 独立灰度。
- `backend/shared/llm_client.py`（Phase 2 新增）：统一 LLM 客户端，单例 `llm_client`。支持 GLM / Qwen / OpenAI / Anthropic / Azure 五大 Provider（OpenAI 兼容协议）。核心方法 `chat_completion`（文本）/ `chat_json`（JSON+Pydantic 校验）/ `embed_texts`（批量向量化）。`LLMClient.from_override()` classmethod 用于多 Agent 博弈构造独立实例。异常体系 `LLMError` / `LLMConfigError` / `LLMResponseError`。指数退避重试（4xx 不重试，5xx 重试），30s 超时。
- `backend/shared/vector_store.py`（Phase 2 新增）：纯 Python 向量检索（无 numpy/Chroma 依赖）。`_cosine_similarity()` 余弦相似度计算，`vector_search()` 语义搜索，`keyword_search()` Jaccard 相似度关键词降级，`search_evidence()` 统一入口（自动降级），`find_related_cases()` 跨案例关联聚合。

### 关键组件

- `frontend/src/services/api-client.ts`：Axios 实例封装，`baseURL: '/api/v1'` 统一 API 版本前缀。请求拦截器注入 JWT，响应拦截器处理 401 自动刷新 token + 重试，统一错误格式 `{code, message, details, status}`。
- `frontend/src/components/AppShell.tsx`：应用外壳，左侧导航按"工作台/调查/合规"分组，顶栏含搜索框、通知铃铛、用户菜单。导航项支持折叠。
- `frontend/src/stores/auth.store.ts`：Zustand 认证状态管理，持久化 access/refresh token，提供 `login` / `refresh` / `logout` action。
- `demo/js/app.js`：Alpine.js 主应用，含路由、状态管理、命令栏 ⌘K、Skeptic 反例弹窗、通知面板等交互。修复过的 `statusBadgeClass()` 和 `statusLabel()` 辅助方法确保 inbox 卡片状态徽章正确显示。
- `demo/js/agents.js`：Agent 流水线模拟，含 6 个 Agent 的执行状态机与延时动画。
- `deploy/nginx.conf`：Nginx 反向代理配置，`/api/v1/*` 转发到后端服务（按路径前缀路由到对应微服务端口），`/` 返回 React 静态资源。

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
cd deploy
cp .env.example .env

# 启动所有服务
docker compose up -d

# 首次初始化数据库种子数据
docker compose --profile seed up seed

# 访问 http://localhost:8080
```

### 方式二：本地开发

后端：

```bash
cd backend
pip install -r requirements.txt

# 初始化数据库
python -m backend.seed

# 各服务分终端启动
uvicorn auth_service.main:app --port 8001 --reload
uvicorn case_service.main:app --port 8002 --reload
uvicorn evidence_service.main:app --port 8003 --reload
uvicorn agent_service.main:app --port 8004 --reload
uvicorn audit_service.main:app --port 8005 --reload
uvicorn connectors.lms.main:app --port 8011 --reload
```

前端：

```bash
cd frontend
npm install
npm run dev
# 访问 http://localhost:5173
```

Demo 原型版（无需后端）：

```bash
cd demo
python -m http.server 8080
# 访问 http://localhost:8080
```

### 运行测试

```bash
cd backend
$env:PYTHONPATH="."
python -m pytest tests/ --tb=short --no-cov -q
```

## 演示账号

| 角色 | 邮箱 | 密码 |
|---|---|---|
| 调查员 | lisa.chen@biopharma.com | rca-demo-2025 |
| QA 审核员 | james.wilson@biopharma.com | rca-demo-2025 |
| 管理员 | admin@biopharma.com | rca-admin-2025 |

## API 文档

各服务启动后访问 Swagger UI：

- Auth: http://localhost:8001/docs
- Case: http://localhost:8002/docs
- Evidence: http://localhost:8003/docs
- Agent: http://localhost:8004/docs
- Audit: http://localhost:8005/docs
- LMS Connector: http://localhost:8011/docs

所有业务 API 统一使用 `/api/v1/*` 前缀。

## GxP 合规要点

1. ALCOA+ 9 项：Attributable / Legible / Contemporaneous / Original / Accurate / Complete / Consistent / Enduring / Available。
2. 21 CFR Part 11：电子签名双人确认（investigator 提交 + qa_reviewer 批准）+ 法律声明文本 + 二次密码校验。
3. 审计日志：DB 层 CREATE RULE 阻止 UPDATE/DELETE，SHA-256 hash 链，任何篡改破坏链条。
4. 会话超时：15 分钟无操作自动登出（可配置）。
5. 数据保留：10 年（可配置）。
6. 案例结案：仅能通过 `/signatures` 端点完成双人签名后由系统自动结案，禁止 PATCH 直接设 `status=closed`。

## Phase 2 LLM 化改造

2026 年 7 月完成 6 项改进，将 Agent 流水线从规则模板升级为真实 LLM 推理 + 多 Agent 博弈。所有改造均通过 Feature Flag 独立灰度，LLM 失败永不阻塞流水线（三级降级保护）。

### 改进总览

| # | 改进 | Feature Flag | 核心价值 | 测试 |
|---|---|---|---|---|
| 1 | Reasoner LLM 化 | `reasoner_llm_enabled` | 真实 LLM Chain-of-Thought 推理替代规则版 5 Whys，Pydantic schema 校验输出 | 38 |
| 2 | Skeptic LLM 化 | `skeptic_v2_enhanced` | LLM 动态生成 `alternative_explanation` / `independent_cause` / `evidence_gap` 三类对抗反例 | 16 |
| 3 | Evidence Hunter 语义检索 | `evidence_hunter_semantic` | Embedding API + 余弦相似度 + 跨案例关联（`related_historical_cases`） | 29 |
| 4 | Drafter LLM 化 | `drafter_llm_enabled` | LLM 生成 ICH Q9 十节格式 Markdown 报告（替代模板拼接） | 13 |
| 5 | 多 Agent 博弈验证 | `multi_agent_debate_enabled` | 双 Reasoner 并行 + Skeptic 交叉质证 + Meta-Controller 置信度综合（0.0-1.0） | 23 |
| 6 | Connector 扩展 | — | 新增 BMS/EMS/MES Connector（共 4 个），`fetch_from_source` 统一路由 | 30 |

### 统一 LLM 客户端架构

`shared/llm_client.py` 实现 OpenAI 兼容协议，5 大 Provider 复用同一请求结构：

```
┌──────────────────────────────────────────────────────┐
│              LLMClient（单例 llm_client）            │
├──────────────────────────────────────────────────────┤
│  Provider:    glm │ qwen │ openai │ anthropic │ azure │
│  Methods:    chat_completion / chat_json / embed_texts│
│  Override:   LLMClient.from_override() ← 博弈专用    │
│  Factory:    get_debate_client() ← secondary 客户端  │
├──────────────────────────────────────────────────────┤
│  Retry:      指数退避（0.5s → 1s → 2s），4xx 不重试  │
│  Timeout:    30s                                     │
│  JSON:       response_format=json_object + Pydantic  │
│  Audit:      hash_prompt() → sha256:xxx              │
└──────────────────────────────────────────────────────┘
```

### 多 Agent 博弈架构

启用 `multi_agent_debate_enabled` 后，pipeline 在 Evidence Hunter 之后切换到博弈路径：

```
Evidence Hunter
       ↓
┌──────────────────────────────────────┐
│  asyncio.gather                      │
│  ├── Primary Reasoner (LLM A)        │
│  └── Adversary Reasoner (LLM B)      │
└──────────────────────────────────────┘
       ↓
Skeptic Cross-Examine（contradiction / evidence_conflict 新维度）
       ↓
Meta-Controller Synthesis（置信度 0.0-1.0 + agreement/disagreement points）
       ↓
Drafter（使用 Meta-Controller 综合结论生成 ICH Q9 报告）
       ↓
Guardian
```

降级模式：
- `parallel`：primary + adversary 双成功（理想状态）
- `primary_only`：adversary 失败/未配置，仅 primary（confidence ≤ 0.7）
- `rule_fallback`：primary 也失败，完全降级到规则版

### 配置 LLM

在 `.env` 中配置 LLM provider 即可启用（feature flag 默认 off，需手动开启）：

```bash
# 主 LLM（primary Reasoner / Skeptic / Drafter / Meta-Controller 共用）
LLM_PROVIDER=glm              # glm | qwen | openai | anthropic | azure
LLM_API_KEY=your-glm-key
LLM_MODEL=glm-4-plus          # 可选，留空用 provider 默认

# 对抗 LLM（adversary Reasoner，仅多 Agent 博弈用）
LLM_DEBATE_PROVIDER=qwen
LLM_DEBATE_API_KEY=your-qwen-key
LLM_DEBATE_MODEL=qwen-plus    # 可选
LLM_DEBATE_TEMPERATURE=0.5    # 鼓励多样性

# 启用 feature flag（按用户 hash 分桶灰度，0=off, 100=全量）
RCA_FEATURE_FLAGS=reasoner_llm_enabled:100,skeptic_v2_enhanced:100,drafter_llm_enabled:100,evidence_hunter_semantic:100,multi_agent_debate_enabled:50
```

## 工程质量基线

- pytest 测试套件：**202 条测试**覆盖认证、案例 CRUD、HITL、Agent 流水线、审计 hash 链、Reasoner/Skeptic/Drafter LLM 路径、多 Agent 博弈、语义检索、Connector（LMS/BMS/EMS/MES）。测试总数从 Phase 1 的 53 条增长到 202 条（0 回归）。
- CI 流水线（`.github/workflows/ci.yml`）：ruff lint → mypy 类型检查 → pytest（80% 覆盖率门槛）→ tsc --noEmit → vite build → alembic check（PostgreSQL service container 验证迁移一致性）。
- Alembic 版本化迁移：`backend/alembic/versions/` 含 initial_schema 迁移，支持 upgrade/downgrade。
- Pre-commit hooks：ruff + mypy + eslint 本地提交前拦截。
- 监控告警：Prometheus 4 黄金信号（延迟/流量/错误/饱和度）+ Grafana dashboard + Sentry 异常自动上报。
- 限流：`/auth/login` 10/min/IP，`/auth/refresh` 20/min/IP，读 API 100/s/IP，写 API 30/min/IP。
- Feature Flag：6 个 flag 已接入业务代码路径（`skeptic_v2_enhanced` / `reasoner_llm_enabled` / `hitl_gate3_auto_retry` / `evidence_hunter_semantic` / `drafter_llm_enabled` / `multi_agent_debate_enabled`），支持百分比灰度与环境变量热覆盖。
- LLM 降级保护：所有 LLM 路径三级降级（flag off → rule-based；LLM 未配置 → rule-template-v1；LLM 异常 → fallback），LLM 失败永不阻塞 Agent 流水线。
- GxP 审计：每个 LLM 调用写入 `ai_model` / `ai_prompt_hash` / `ai_output_hash` 到审计日志，满足可追溯性。

## 许可

Internal use only — BioPharma RCA Pilot
