# RCA Pilot

受 GxP 监管的 Agentic AI 偏差调查平台，面向生物制药企业提供端到端的根因分析能力。系统融合多 Agent 编排、HITL 人工决策门控、不可篡改审计链与 21 CFR Part 11 电子签名，支持从偏差告警接入到 ICH Q9 报告签发的完整闭环。

## 1. 项目概述

RCA Pilot 围绕"偏差调查 4 小时交付可审计结构化草稿"的核心目标设计，主要特性包括：

- 多 Agent 串行编排流水线：Meta-Controller → Evidence Hunter → Reasoner → Skeptic → Drafter → Guardian，每步输出计算 hash 并写入审计日志。
- 5 道 HITL 人工决策关卡，覆盖证据完整性、5 Whys 推理、Skeptic 反例、CAPA 评审、报告签发，不可跳过。
- GxP 合规内建：ALCOA+ 9 项原则、21 CFR Part 11 双人电子签名、SHA-256 hash 链审计日志、DB 层 RULE 阻止 UPDATE/DELETE。
- 6 个源系统只读 Connector（LIMS/MES/EMS/CMMS/LMS/BMS），证据数据不写入源系统。
- 微服务架构：Auth/Case/Evidence/Agent/Audit 5 个业务服务 + LMS Connector，共享 backend/shared 层。
- 大厂级工程基线：pytest 测试套件、Alembic 版本化迁移、CI/CD 流水线、Prometheus 监控、slowapi 限流、Sentry 异常捕捉、Feature Flag 灰度框架。

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
              │  LMS Connector :8011│
              └─────────────────────┘
```

### 模块关系

- `backend/shared/`：所有微服务共用的基础设施层，包括应用工厂（`service_base.create_app`）、ORM 模型、Pydantic schemas、认证中间件、审计 hash 链、错误处理器、限流、metrics、feature flag、配置管理等。
- 5 个业务服务各自包含 `main.py`（入口，调用 `create_app` 注册路由）和 `routes.py`（业务端点实现），通过 `shared` 层复用基础能力。
- `connectors/lms/`：独立的 Mock Connector 服务，模拟 LMS 培训管理系统 API，供 Evidence Service 拉取证据。
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
│   │   ├── feature_flag.py           # Feature Flag 框架（本地 + Unleash）
│   │   ├── database.py               # AsyncEngine + AsyncSessionLocal
│   │   ├── config.py                 # Pydantic Settings 环境变量配置
│   │   └── logging.py                # 结构化 JSON 日志
│   ├── auth_service/                 # 认证服务（登录/刷新/当前用户）
│   ├── case_service/                 # 案例服务（CRUD + HITL + 5Whys + 签名 + Dashboard KPI）
│   ├── evidence_service/             # 证据服务（列表/详情/时间线/Connector 编排）
│   ├── agent_service/                # Agent 服务（6 Agent 流水线 + 运行状态追踪）
│   ├── audit_service/                # 审计服务（hash 链验证/导出/Feature Flag 管理）
│   ├── connectors/lms/               # LMS Mock Connector
│   ├── alembic/                      # 版本化迁移
│   │   ├── env.py                    # 异步迁移环境
│   │   └── versions/                 # 迁移脚本
│   ├── tests/                        # pytest 测试套件
│   │   ├── conftest.py               # 异步 fixture + SQLite 测试 DB
│   │   ├── test_auth.py              # 登录/刷新/锁定
│   │   ├── test_case_crud.py         # 案例 CRUD + 状态机校验
│   │   ├── test_hitl.py              # 5 关 accept/reject
│   │   ├── test_agent_pipeline.py    # 6 Agent 流水线 + hash 输出
│   │   └── test_audit_chain.py       # hash 链完整性
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

- `backend/agent_service/routes.py`：Agent 流水线编排引擎。定义 `AGENT_PIPELINE` 顺序数组，实现 `_run_evidence_hunter` / `_run_reasoner` / `_run_skeptic` / `_run_drafter` / `_run_guardian` 6 个规则版 Agent 函数。每步输出通过 `_hash_output()` 计算 SHA-256 hash 并写入审计日志。`start_pipeline` 端点使用 `BackgroundTasks` 异步执行，避免阻塞请求。Skeptic Agent 接入 `skeptic_v2_enhanced` feature flag，启用时追加 2 条增强挑战。
- `backend/case_service/routes.py`：案例 CRUD + HITL 关卡管理。`handle_hitl_gate` 端点处理 accept/reject/edit，gate3 reject 时若 `hitl_gate3_auto_retry` flag 启用则触发 Reasoner 重试。`update_case` 端点实现状态机校验，禁止通过 PATCH 直接设 `status=closed`（必须走 `/signatures` 双签流程）或 `status=reviewing`（系统态）。`create_case` 捕获 `IntegrityError` 处理重复 ID，使用 `selectinload` 避免 async 懒加载报错。`sign_case` 端点实现 21 CFR Part 11 双人签名（investigator 提交 + qa_reviewer 批准）。
- `backend/auth_service/routes.py`：登录/刷新/当前用户。`login` 端点校验密码后签发 access + refresh token，失败 5 次锁定账号。关键修复：失败计数必须 `await db.commit()` 后再 `raise AuthError`，否则事务回滚丢失计数。`refresh` 端点使用 `@refresh_limit` 装饰器，需声明 `request: Request` 参数供 slowapi 读取客户端 IP。
- `backend/evidence_service/routes.py`：证据查询 + 时间线 + Connector 编排。`fetch_from_source` 端点通过 httpx 调用 LMS Connector 拉取增量证据，超时降级返回缓存数据。

### 数据模型与 API

- `backend/shared/models.py`：SQLAlchemy ORM 模型，核心表包括：
  - `User`：用户表，role 字段受 DB 层 CHECK 约束（admin/investigator/qa_reviewer/ops_manager/auditor/operator）。
  - `Deviation`：偏差案例表，`hitl_gates` 字段为 JSONB 存储 `{gate1: pending|done|rejected, ...}`。
  - `Evidence`：证据表，关联案例与源系统，含 `ai_output_hash` 字段。
  - `AuditLog`：审计日志表，`prev_hash` + `current_hash` 形成链式结构，DB 层 CREATE RULE 阻止 UPDATE/DELETE。
  - `SignatureRecord`：电子签名记录表，关联案例与签字人。
  - `FiveWhy` / `SkepticChallenge` / `TimelineEvent` / `AgentRun` / `SourceSystem` 等。
- `backend/shared/schemas.py`：Pydantic v2 请求/响应模型。`Page(BaseModel, Generic[T])` 支持泛型参数化（如 `Page[DeviationOut]`），供 FastAPI 自动生成 OpenAPI schema。
- `backend/shared/audit.py`：审计日志 hash 链实现。`_hash_payload()` 将 prev_hash + seq + operator + action + target + detail + timestamp + evidence_refs 拼接后计算 SHA-256。`append_audit_log()` 使用独立事务确保审计日志不被业务回滚。`verify_hash_chain()` 提供完整性校验。
- `backend/shared/auth_middleware.py`：`AuthContext` 数据类封装请求认证上下文（user/session_id/ip/permissions/db），通过 `Depends(get_auth_context)` 注入到受保护端点。`get_db_from_auth` 依赖复用 AuthContext 已建立的 DB session，避免连接池耗尽（Critical 修复）。
- `backend/shared/security.py`：直接调用 `bcrypt.hashpw` / `bcrypt.checkpw` 实现密码哈希，手动 72 字节截断。替代 passlib 1.7.4（与 bcrypt 4.x 不兼容）。
- `backend/shared/rate_limit.py`：slowapi 限流装饰器。`login_limit`（10/min/IP）、`refresh_limit`（20/min/IP）、`api_limit`（100/s/IP）、`write_limit`（30/min/IP）。`_client_ip()` 穿透 `X-Forwarded-For` / `X-Real-IP` 代理头。
- `backend/shared/error_handler.py`：全局异常处理器，将 `RCAError` 子类映射到 `ErrorCode` 枚举，统一返回 `{code, message, detail, trace_id}` 格式。`ValidationError`（业务校验）映射到 `BUSINESS_VALIDATION_ERROR`（HTTP 400），区别于 `RequestValidationError`（pydantic 格式错误，HTTP 422）。5xx 错误自动上报 Sentry。
- `backend/shared/feature_flag.py`：Feature Flag 框架。支持本地 JSON 配置（默认）和 Unleash 远程模式（生产）。`is_enabled(flag, user_id)` 按 user_id hash 分桶实现百分比灰度。环境变量 `RCA_FEATURE_FLAGS` 支持热覆盖。已接入 `skeptic_v2_enhanced`、`reasoner_llm_enabled`、`hitl_gate3_auto_retry` 三个 flag。

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

## 工程质量基线

- pytest 测试套件：53 条测试覆盖认证、案例 CRUD、HITL、Agent 流水线、审计 hash 链。
- CI 流水线（`.github/workflows/ci.yml`）：ruff lint → mypy 类型检查 → pytest（80% 覆盖率门槛）→ tsc --noEmit → vite build → alembic check（PostgreSQL service container 验证迁移一致性）。
- Alembic 版本化迁移：`backend/alembic/versions/` 含 initial_schema 迁移，支持 upgrade/downgrade。
- Pre-commit hooks：ruff + mypy + eslint 本地提交前拦截。
- 监控告警：Prometheus 4 黄金信号（延迟/流量/错误/饱和度）+ Grafana dashboard + Sentry 异常自动上报。
- 限流：`/auth/login` 10/min/IP，`/auth/refresh` 20/min/IP，读 API 100/s/IP，写 API 30/min/IP。
- Feature Flag：3 个 flag 已接入业务代码路径，支持百分比灰度与环境变量热覆盖。

## 许可

Internal use only — BioPharma RCA Pilot
