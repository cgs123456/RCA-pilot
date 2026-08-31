# RCA Pilot 优化审查报告

> 审查日期：2026-08-30
> 审查方式：全量代码精读（backend 1.3 万行 Python / frontend 2.4 千行 TS / demo / deploy / CI）+ 业界开源项目与监管指南调研
> 说明：所有发现均带 `文件:行号` 证据；严重程度分 Critical（上线阻断）/ High / Medium / Low。

---

## 总体结论

项目骨架质量不错：统一错误格式、RBAC 矩阵、hash 链审计、Feature Flag 灰度、LLM 三级降级、202 条测试等基线完整。但存在 **10 项上线阻断级问题**，集中在四条主线：

1. **Agent 流水线运行时不可靠**（BackgroundTasks + 单事务 + 无恢复）；
2. **审计与签名合规有硬伤**（seq 竞态、可跳步签批、审计写入权限过宽、seed 可清空审计）；
3. **默认路径输出错误结论**（规则版 Agent 把 demo 案例 EMP-2187 的根因硬编码进通用流水线）；
4. **生产版前端核心交互未接线**（HITL 审批按钮不存在，5 道关卡永远无法通过）。

业务层面，当前是"RCA 调查 demo"而非可承接偏差全生命周期的系统：**CAPA 管理、报告版本管理、ICH Q9 风险评估三大块整体缺失**。部署/CI 存在"承诺与实现不符"（Redis 未用、Prometheus/Grafana 未部署、k6 打不中路由、CD 拉不到镜像、无备份策略）。

---

## 一、Critical（上线阻断，建议最先修复）

### A. 业务正确性 / 合规

| # | 问题 | 证据 | 修法 |
|---|------|------|------|
| C1 | **规则版 Agent 把 demo 案例 EMP-2187 的根因硬编码进通用流水线**：鱼骨图、根因摘要、Skeptic 挑战、Drafter CAPA 全部写死"EMP-2187 更衣资质逾期→微生物污染"。LLM flag 默认全关 → 新建任何偏差（如 HPLC 杂质超标）走降级路径会输出与案例无关的结论，且带 `ai_output_hash` 进入审计链 | `backend/agent_service/routes.py:222,264,494-495,765,1483` | 规则版只输出数据驱动的模板占位符，禁止携带具体案例事实性结论 |
| C2 | **电子签名缺顺序强制与状态校验**：docstring 声称"强制 submit→approve"，实际只验密码和权限；approve 不检查 submit 签名是否存在、不检查 `case.status=="reviewing"`，可直接批准未提交的案例；重复签同含义 → sig_id 主键冲突 500 未捕获；对已 closed 案例可重复 approve | `backend/case_service/routes.py:534-560` | approve 前校验存在对应 submit 记录且 status=="reviewing"；签名表加 `(case_id, meaning, signer)` 唯一约束 |
| C3 | **`POST /audit/logs` 写操作只要求 `audit:read` 权限，且 `operator` 由客户端任意指定** → 任意 investigator/auditor 可伪造他人署名的合法审计日志（链本身合法，污染"可归因性"） | `backend/audit_service/routes.py:89-95` | operator 强制取当前认证用户；端点改 `system:configure` 或仅服务间调用 |
| C4 | **seed 可 TRUNCATE 审计表绕过防篡改触发器**：PG 的 TRUNCATE 不触发 DELETE 触发器，`TRUNCATE ... CASCADE` 一次清空全部审计并重建链 | `backend/seed.py:275`；`backend/db/01_schema.sql:210-216`（无 ON TRUNCATE 触发器） | 加 `BEFORE TRUNCATE` 触发器 RAISE EXCEPTION；seed 改专用受控流程 |
| C5 | **HITL `edit` 动作被当 rejected 处理，`edit_payload` 全程被忽略**；gate 顺序不强制（可直接 accept gate5）；进度可经 PATCH 任意设置绕过 gate 推进 | `backend/case_service/routes.py:322-350`；`backend/shared/schemas.py:108` | 实现 edit 分支消费 edit_payload；强制 gate 顺序；progress 由系统计算 |
| C6 | **Evidence Hunter 声称跨源拉取但从不调用 connector、不落库、不写审计**；`fetch_from_source` 仅转发 HTTP 响应，硬编码 `employee_id="EMP-2187"`，一次只拉一个源，无四源聚合 | `backend/agent_service/routes.py:66-148`；`backend/evidence_service/routes.py:103-172,141` | fetch 后 content_hash 去重入库 + 审计；employee_id 从案例关联人推导 |

### B. Agent 流水线运行时

| # | 问题 | 证据 | 修法 |
|---|------|------|------|
| C7 | **BackgroundTasks 执行分钟级长任务，无持久化/恢复**：进程重启/OOM 后 run 永远停在 pending/running，无启动时僵尸 run 扫描，审计链只有 START 没有 FAILED | `backend/agent_service/routes.py:1820`；compose 单 uvicorn 进程 | 持久化任务队列（arq/Celery/Temporal）或至少启动时标记超时 run 为 failed 并补审计 |
| C8 | **流水线全程单 DB 事务**：外部会话在跑完前只能看到 pending 和空 agent_states（进度不可见）；`pause_run` 因 `status!="running"` 永远抛错——**暂停功能 100% 不可用**；且后台代码从不读 run.status，即使改了也不会停 | `backend/agent_service/routes.py:1514,1888-1890`；`backend/shared/database.py:49-58` | 每步独立短事务落进度；每步前检查 run.status；补 resume/cancel |
| C9 | **异常处理路径二次崩溃**：except 里直接 `flush()+commit()`，DB 类异常时 session 已 pending-rollback，`PendingRollbackError` 冲出 → run 永远无法标记 failed | `backend/agent_service/routes.py:1744-1755` | 先 rollback 再用独立 session 更新状态 + 补审计 |
| C10 | **无重复触发防护**：同案例可并发启动多条流水线（重复消耗 LLM 成本、互相覆盖 agent_states）；`run_id=RUN-{case}-{秒级时间戳}` 同秒两次请求主键冲突 500 | `backend/agent_service/routes.py:1794-1806` | partial unique index（case_id where status in pending/running）+ run_id 加 uuid |

### C. 审计链与认证

| # | 问题 | 证据 | 修法 |
|---|------|------|------|
| C11 | **审计 hash 链 seq 分配竞态**：读 `max(seq)` 后应用层 +1 写入，无锁无重试；两人并发写审计（高频路径）必有一个撞 UNIQUE 500 或断链 | `backend/shared/audit.py:104-105` | `pg_advisory_xact_lock` 或对尾行 `FOR UPDATE` 串行化 + 冲突重试 |
| C12 | **JWT secret/DB 密码有公开默认值且无防呆**：`dev-secret-change-in-prod` / `rca-secret`，漏配环境变量系统照常启动，任何人可伪造 admin token | `backend/shared/config.py:32,42-45` | `env=="prod"` 时检测默认值拒绝启动 |
| C13 | **alembic 迁移与 01_schema.sql/ORM 三方漂移**：迁移库缺 `embedding/embedding_model/embedded_at` 三列 → `alembic upgrade head` 建的库上语义检索直接 UndefinedColumn；另有约束/索引/级联多处缺失 | `backend/shared/models.py:112-115` vs `alembic/versions/20250817_0001_*.py:86-99` | 以 alembic 为唯一真相来源，补迁移对齐；01_schema.sql 降级为文档 |
| C14 | **前端 HITL 审批 UI 完全缺失**：`hitlMutation` 定义后从未被 `.mutate()` 调用，5 个 gate 只渲染徽章无任何按钮 → 调查员在 UI 上永远无法通过任何关卡，签发链路断裂（后端 `PUT /cases/{id}/hitl/{gate_id}` 和 `caseApi.hitl` 都已就绪） | `frontend/src/pages/InvestigationPage.tsx:54-60`（对照 `backend/case_service/routes.py:297`） | 在 5 Whys/Skeptic/gate 条上加 accept/reject/edit 按钮接上已有 mutation |

### D. 部署

| # | 问题 | 证据 | 修法 |
|---|------|------|------|
| C15 | **compose 缺 BMS/EMS/MES 三个 connector 服务** → 部署后 fetch_from_source 对三个源 connection refused，核心证据流水线不可用 | `deploy/docker-compose.yml`（仅 lms-connector）vs `backend/shared/config.py:72-74` | 补 3 个服务定义 |
| C16 | **compose 把源码 bind-mount 进容器**（`- ../backend:/app/backend` ×7）→ CD 拉的镜像形同虚设，破坏 GxP 版本可追溯；且 CD 的 `docker compose pull` 拉不到镜像（自建服务只有 build 没有 image 字段），staging 部署按现状跑不通 | `deploy/docker-compose.yml:70 等`；`.github/workflows/cd.yml:126-128` | 拆 compose.dev.yml；compose 增带 `${SHA}` 的 image 字段 |
| C17 | **无任何数据库备份/10 年保留落地**：仅 named volume，全仓无 pg_dump/WAL 归档；volume 损坏 = GxP 记录永久丢失（`.env.example` 声明 RETENTION_YEARS=10 无实现） | `deploy/docker-compose.yml:26-27` | pg_dump/WAL 归档定时容器 + 异地存储 + 恢复演练 runbook |
| C18 | **CI/CD 零安全扫描**：无 pip-audit/trivy/SAST/SBOM/镜像签名；依赖 CVE 无门禁，GxP 供应链不可证明 | `.github/workflows/ci.yml`、`cd.yml` 全文 | CI 加 pip-audit + trivy + CodeQL；CD 加 syft SBOM + cosign |

---

## 二、High（安全 / 性能 / 正确性）

### 安全

1. **Refresh token 只轮换不撤销，logout 不失效任何 token**：纯无状态 JWT 无 jti 黑名单，被盗 refresh 7 天有效；登出/禁用账号后 access 仍可用（`shared/security.py:62-72`、`auth_service/routes.py:142,165-172`）。
2. **access token 无 jti → 所有审计日志 session_id 恒为空串**，Part 11 "会话可追溯"断链（`shared/security.py:52-58`、`shared/auth_middleware.py:85`）。
3. **JWT 存 localStorage**（XSS 可窃取长效 refresh token）（`frontend/src/stores/auth.store.ts:69-77`）→ refresh 改 httpOnly Cookie + access 仅内存。
4. **限流可被 X-Forwarded-For 伪造绕过 + 计数器是进程内存**：多副本/多 worker 下失效；nginx 用 `$proxy_add_x_forwarded_for` 追加而非覆盖（`shared/rate_limit.py:20-34`、`deploy/nginx.conf:20,29`）。
5. **422 校验错误回显 pydantic `errors()` 含提交值**：提交 3 位密码，响应体含 `input:"abc"`（`shared/error_handler.py:107-114`）。
6. **async 路径同步 bcrypt 阻塞事件循环**（cost 12 单次数百 ms）（`shared/security.py:30-39`）→ `anyio.to_thread` 包装。
7. **密码策略薄弱**：最短 6 位无复杂度；bcrypt 72 字节静默截断（`shared/schemas.py:37`、`shared/security.py:22-25`）。
8. **连续失败 5 次永久锁定**，无时间窗自动解锁、无管理员解锁端点（`auth_service/routes.py:68-69`）。
9. **仅 audit_log 有防篡改触发器**：signatures / evidence / agent_runs 可被 UPDATE/DELETE，电子签名记录可静默改写（`db/01_schema.sql:201-248`）。
10. **审计 hash 覆盖字段不全**：ip/session_id/ai_* 三字段不在链内；`verify_hash_chain` 的宽容匹配实际把校验降级为前 32 hex 字符比较，且该兜底对其声称的场景永远不生效（`shared/audit.py:34-45,199-204`）。
11. **前端 token 刷新无 single-flight**：Dashboard 首屏 4 个并发查询同时 401 → 并发 4 次 refresh，轮换后第 2+ 次携带作废 token 误登出（`frontend/src/services/api-client.ts:29-45`）。
12. **登出不清 TanStack Query 缓存**：公用终端换账号可见上一用户的案例/KPI（`frontend/src/components/AppShell.tsx:46-49`）。
13. **审计导出 URL 拼错（缺 /v1）+ `<a href>` 跳转不带 JWT → 导出功能 100% 失败**（`frontend/src/services/audit.service.ts:24`、`AuditPage.tsx:45,50`）。
14. **LLM 输出引用的证据 ID 不做存在性校验**：`AgentHallucinationError` 定义了但从未 raise，幻觉证据 ID 直通审计链（`agent_service/routes.py:410-423,666-678`）。
15. **connector 无鉴权**：4 个 connector 全端点无认证，含员工培训/资质 PII（`backend/connectors/*/routes.py`）。
16. **生产登录页硬编码演示账号密码且默认预填**（`frontend/src/pages/LoginPage.tsx:10-14,19-20`）→ `import.meta.env.DEV` 包裹。
17. **mfa_required=True 是摆设**：登录流程从不校验 mfa_code，GxP 声明与实现不符（`shared/config.py:49`、`auth_service/routes.py:35-113`）。
18. **anthropic provider 实际不可用**：统一 POST `/chat/completions`，Anthropic 真实协议是 `/v1/messages`，配置即全部降级（`shared/llm_client.py:230`）。
19. **debate 两个 Reasoner 不保证不同 LLM**：与主 provider 相同时退化为同模型自博弈，审计却记 `parallel` 模式；`primary_only` 时无 confidence≤0.7 的代码级 clamp，只有 prompt 约束（`shared/llm_client.py:469-476`、`agent_service/routes.py:992`）。

### 性能

20. **向量检索全表加载 + 纯 Python 余弦**：`SELECT * WHERE embedding IS NOT NULL` 无 LIMIT，O(N×D) Python 循环阻塞事件循环；关键词搜索同样全表载入（`shared/vector_store.py:83-97,128-131`）→ 迁移 pgvector + HNSW（业界共识：JSONB 存向量无索引必全表扫）。
21. **Deviation 关系默认 `lazy="selectin"`**：任何列表/统计查询都四表级联急加载，含 raw_data 和 embedding 大字段（`shared/models.py:85-90`）→ 详情显式 selectinload，模型默认 lazy="raise"。
22. **连接池数学不可行**：9 进程 × (10+20) ≈ 270 连接 ≫ PG 默认 100 → `too many connections`（`shared/database.py:16-23`）。
23. **embedding 批量端点**：单批 500 条（client 自述上限 64）、无 `FOR UPDATE SKIP LOCKED` 并发重复嵌入、不过滤 embedding_model（切模型后同列混存不同维度，cosine 直接 0 分）（`evidence_service/routes.py:231-258`、`llm_client.py:387`）。
24. **embedding 变更证据行却 commit 且不写审计**：ALCOA+ 违规（`evidence_service/routes.py:215,269`）。
25. **LLM token 用量被丢弃**：docstring 声称记录用量，实际从不读取 `usage`，无成本核算、无 run 级预算；`llm_max_tokens=2048` 全局一刀切可截断 Drafter 长报告（`shared/llm_client.py:244-260`、`config.py:88`）。
26. **LLM 输出证据/时间线跨案例关联对任何案例都一样**（硬编码）见业务清单；**`httpx.AsyncClient` 每请求新建**无连接复用（`llm_client.py:247,414`）。
27. **每步 Agent 重复查询相同数据 + `_hash_output` 用 `default=str` 序列化不稳定**（同数据不同 hash）（`agent_service/routes.py:59`）。
28. **uvicorn 全部单 worker**：无法利用多核，LLM 并发时事件循环阻塞全服务（`deploy/docker-compose.yml:61 等`）。
29. **Unleash 模式每次 `is_enabled` 新建客户端 + 同步 initialize**，阻塞事件循环且连接泄漏（`shared/feature_flag.py:146-156`）。
30. **feature flag DEFAULT_FLAGS 被浅拷贝污染**：env 覆盖后 `reload_flags` 的"默认值"已是脏数据，灰度回滚失效（`shared/feature_flag.py:90,98-99`）。

### 正确性

31. **HITL 并发丢失更新**：注释声称加锁实际没有 `.with_for_update()`，JSONB 读-改-写互相覆盖（`case_service/routes.py:322-335`）。
32. **gate3 reject 的"自动重试"是空操作**：只写一条审计日志，不调 agent-service、不建 AgentRun，flag 的 max_retries 无处生效（`case_service/routes.py:355-371`）。
33. **案例 ID 用 `count(*)+1` 生成**：并发创建同月案例撞 ID（`case_service/routes.py:157-163`）。
34. **KPI 硬编码 0**：capa_overdue_rate 与 4 个 trend delta 恒 0；平均闭环天数用 `updated_at` 计算（任何 PATCH 都刷新）失真（`case_service/routes.py:605-623`）。
35. **error_handler 未覆盖多数 RCAError 子类**：ConflictError/ConnectorError 等全兜底 500，异常自带 status_code 被忽略；AuthError 靠中文子串匹配错误码（`shared/error_handler.py:85-103`）。
36. **`/ready` 失败仍返回 200**：K8s readinessProbe 摘不了故障实例（`shared/service_base.py:115-133`）。
37. **签名的 certificate_fingerprint 是伪造值** `fp:{user_id}:{ts}`，非真实证书指纹（`case_service/routes.py:543-554`）。
38. **Prometheus `AGENT_RUNS_TOTAL` 以 case_id 为 label**——无界高基数可打爆监控；404 路径原始 path 进 label 同样无界（`shared/metrics.py:79,129`）。
39. **审计 IP 记录的是代理 IP**（`request.client.host`）与限流取 IP 逻辑不一致，生产挂 Nginx 后全记网关 IP（`shared/auth_middleware.py:86`）。

---

## 三、业务功能模块缺口（对照真实制药偏差管理）

| 模块 | 现状 | 差距 |
|------|------|------|
| **CAPA 管理** | 仅存在于 Drafter 生成的报告文本里，无表/实体/端点 | 措施跟踪、责任人、期限、状态流转、**有效性验证（effectiveness check）**、延期审批全部缺失；KPI 的 capa_overdue_rate 恒 0 |
| **报告版本管理** | 报告仅存于 AgentRun.agent_states JSONB | 无 Report 实体、无版本号/修订历史；HITL 编辑 5 Whys 后不重算 hash 不重生成报告，报告与最终确认的根因可能不一致 |
| **ICH Q9 风险评估** | 无风险矩阵/FMEA/RPN 字段；complexity 1-3 不参与任何逻辑 | "残余风险等级"全靠 LLM 自由发挥 |
| **偏差分级** | severity 手填，无分级决策树 | 无患者安全/数据完整性/法规影响评估、critical 自动升级、时限收紧规则 |
| **OOS/OOT** | case_type 自由文本 | 无实验室调查阶段/复检规则/批次处置流程、无趋势分析 |
| **SLA/期限** | deadline 字段存在但无消费 | 无逾期计算、提醒、升级（escalation）；无"30 天关闭重大偏差"监控 |
| **变更控制** | 无 | CAPA 产生的 SOP/设备变更无实体引用，"根因→CAPA→变更→有效性"不闭环 |
| **证据 ALCOA+** | content_hash 仅覆盖 raw_data | 无采集人归因字段、is_signed 恒 False 无写入路径、无创建端点（证据只能来自 seed） |
| **批次/产品影响** | product_impact 是文本字段 | 无批次表联动、无放行状态拦截、MES 数据不回写处置结果 |
| **协作/通知** | 无 | 无评论、@、邮件/站内通知、调查移交链路 |
| **用户管理** | `user:manage`/`case:delete`/`agent:reset` 权限已定义 | 无任何对应端点 |
| **审计导出** | CSV/JSON 裸导出 | 无 RFC3161 时间戳/数字签名/范围清单；CSV 无公式注入防护；全表载入内存 |

对标产品：商业（TrackWise、Veeva Vault Quality、SimplerQMS）以 CAPA 闭环 + 有效性验证为核心卖点；开源（OpenQMS、FlinkISO）也把 CAPA/变更控制/文档管理作为基础模块。本项目的差异化（Agentic RCA）有价值，但需要先补齐 QMS 底座才能承接真实业务。

---

## 四、demo 有而生产版缺失的功能（17 项摘要）

生产版前端**没有 mock**、数据全走真实 API（这点是好的），但 demo 的可视化/交互能力大半未迁移，其中直接影响核心流程的：

1. **⌘K 命令栏**（demo 有，生产版搜索按钮无功能）
2. **AI Copilot 流水线可视化**：流式打字机 + 进度条 + 运行/暂停/重置（生产版 Agent Tab 静态列表，且**从不轮询** `refetchInterval` 零命中——启动后界面永久停留在快照）
3. **Skeptic 反例弹窗**（HITL 关卡3 交互，生产版只读）
4. **5 Whys 逐条 accept/reject/edit 按钮**（见 C14，最严重）
5. **鱼骨图 6M d3 渲染**、证据拓扑力导向图、Chart.js 趋势图 5 张（后端 `/dashboard/trend` 端点闲置未接）
6. **证据详情弹窗**（原始 JSON/hash/置信度；生产版证据卡不可点击，ALCOA+ "Original" 在 UI 不可验证）
7. **报告预览页**（TOC/版本 diff/PDF 导出/签名块；生产版是 PlaceholderPage，且 InvestigationPage 的报告 Tab 是前端字符串模板拼装——GxP 交付物不应在前端生成）
8. 通知面板、Toast 系统、"我的待办"、证据库页、知识库页、管理页（RBAC 矩阵/集成健康/签名策略）

其他前端问题：4/7 导航页占位；搜索/过滤无防抖逐键发请求；`any` 类型泛滥；无前端 RBAC 视觉控制；路由零懒加载 + d3/date-fns 是死依赖；API 错误被吞渲染成误导性空态/永久的"加载中"；无前端测试、`npm run lint` 因无 eslint 配置直接失败。

---

## 五、部署 / CI / 测试 / 可观测性

**部署**：容器全 root 无 USER 指令；应用服务无 healthcheck、无资源限制；PG/Redis 端口发布到宿主机（Redis 无密码且无人使用——compose 起了 redis:7 但 requirements 里连 redis 包都没有）；nginx 无 TLS/gzip/安全头，/metrics 公开暴露，`location /health` 被正则抢占是死配置；compose 无日志轮转。

**CI/CD**：主测试跑在 SQLite（conftest 显式把 JSONB/ARRAY patch 成 JSON）——JSONB 查询、DB 层审计不可变触发器、asyncpg 行为全部测不到；仅 alembic-check 一个 job 见 PG；CD 不被 CI 门禁（CI 红着也能部署）；迁移无 downgrade 往返测试；k6 脚本路径全缺 `/api/v1`，实际打中 SPA fallback 返回 200——压测数据无意义且硬编码已知口令。

**测试盲区**（对 GxP 系统最致命的三块）：
1. **双人电子签名结案零测试**（`POST /signatures` 无任何用例）；
2. **零越权测试**：conftest 把所有请求固定为同一个用户 + 权限全集，没有任何 403/跨用户访问用例（RBAC fixture 是死代码）；
3. **零并发/竞态测试**（grep gather/concurrent 零命中）。

**可观测性**：无 LLM token/成本/流水线时长/HITL 通过率业务指标；prometheus.yml 无告警规则且 Prometheus/Grafana 根本没部署进 compose（孤儿配置）；无日志聚合；`AGENT_RUNS_TOTAL` 高基数 label。

---

## 六、与业界实践对照

1. **长任务编排**：业界共识是 BackgroundTasks 不适合分钟级多步 LLM 工作流。Temporal（durable execution，每步 Activity 持久化 + 自动重试 + HITL 等待信号）或 Celery/arq + 状态机是标准做法；注意"token 不写进 workflow history"的模式。（参考：[Temporal Durable AI](https://docs.temporal.io/ai)、[Of course you can build dynamic AI agents with Temporal](https://temporal.io/blog/of-course-you-can-build-dynamic-ai-agents-with-temporal)、[maxtokens.ai: Why we chose Temporal over Celery for AI agents]）
2. **向量检索**：pgvector 是 JSONB 存向量的正式继任者——JSONB 无索引必全表扫，pgvector 的 HNSW/IVFFlat 提供亚线性查询与原生距离算子。（参考：[Vectors are the new JSON in PostgreSQL](https://jkatz05.com/post/postgres/vectors-json-postgresql/)、[How to store and query embeddings in PostgreSQL](https://www.postgresql.fastware.com/blog/how-to-store-and-query-embeddings-in-postgresql-without-losing-your-mind)、[pgvector](https://github.com/pgvector/pgvector)）
3. **监管**：FDA 2025 年 1 月《AI 支持药品/生物制品监管决策》指南草案（FDA-2024-D-4689）提出 7 步风险导向的 AI 模型可信度评估框架，强调模型选择/调优/验证/监控的全生命周期文档化——本项目 LLM 输出直接进入受监管报告，需要按此框架准备模型风险评估与人类问责记录。（参考：[FDA Guidance](https://www.fda.gov/regulatory-information/search-fda-guidance-documents/considerations-use-artificial-intelligence-support-regulatory-decision-making-drug-and-biological)、[7-Step Credibility Framework](https://intuitionlabs.ai/articles/fda-ai-drug-development-guidance)、[AI/ML in GMP 综述](https://pmc.ncbi.nlm.nih.gov/articles/PMC12195787/)）
4. **同类开源**：GitHub `root-cause-analysis` 主题下已有 LangGraph 多 Agent 调查项目（SRE 方向），但"制药偏差 + 5 Whys + HITL 门控 + Part 11 审计"组合在开源界基本空白——本项目的差异化定位成立，值得补齐底座后开源/发布。（参考：[root-cause-analysis topic](https://github.com/topics/root-cause-analysis)）
5. **QMS 基线**：开源 QMS（OpenQMS、FlinkISO）与商业 eQMS（TrackWise/Veeva/SimplerQMS）的公共基线是 CAPA 闭环、变更控制、文档/报告版本、有效性验证——见第三节差距表。（参考：[OpenRegulatory 开源 QMS 综述](https://openregulatory.com/articles/open-source-qms-software)）

---

## 七、建议路线图

**Phase 0 — 上线阻断项（1-2 周）**
- 修 C1（去掉硬编码 demo 根因，规则版改数据驱动占位符）——这是错误结论进审计链的最严重形态；
- 修 C2/C3/C4（签名顺序校验、审计写权限、TRUNCATE 触发器）；
- 修 C8/C9/C10（每步短事务 + 状态检查 + 异步独立 session 标记失败 + run 去重）；
- 修 C11（审计 seq advisory lock）；
- 修 C13（alembic 补 embedding 迁移）；
- 前端接上 HITL 按钮（C14）+ 流水线轮询；
- compose 补 3 个 connector、去 bind mount（C15/C16）。

**Phase 1 — 可靠性与安全加固（2-4 周）**
- 流水线迁 arq/Temporal（C7），补 resume/cancel/进度查询；
- Redis 真用起来：jti 黑名单 + 限流共享存储（否则从 compose 删除）；
- pgvector 替换 JSONB 向量（H20）+ 嵌入分批/SKIP LOCKED/模型隔离（H23）；
- refresh token 撤销、access 加 jti、httpOnly cookie、single-flight 刷新；
- LLM usage 落审计 + run 级预算 + token 指标；证据 ID 存在性校验（防幻觉直通）；
- CI 加 pip-audit/trivy；PG service container 跑一组 API 测试；补双签/越权/并发测试。

**Phase 2 — 业务补全（4-8 周）**
- CAPA 模块（实体/端点/有效性验证/延期）+ 报告实体与版本管理；
- ICH Q9 风险矩阵与偏差分级决策树；SLA 监控与升级；
- 证据创建端点 + connector 拉取落库去重（C6）；
- demo 可视化迁移（流水线进度、鱼骨图、证据详情、报告预览/PDF、⌘K）；
- 备份/归档方案 + 恢复演练 runbook（C17）；Prometheus/Grafana 进 compose + 告警规则。

**架构建议**：当前 9 进程拆分收益有限（共享 PG/secret/shared 包，独立限流内存计数互相失效）。建议核心收敛为 2-3 个部署单元（API 网关 + 模块化单体 + agent worker），agent_service 是唯一有独立扩缩容诉求的服务。连接池预算、TraceId 透传、XFF 处理随收敛一并解决。
