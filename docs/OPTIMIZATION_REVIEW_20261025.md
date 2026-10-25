# RCA Pilot 优化分析报告（第二轮复审）

- **日期**: 2026-10-25
- **审查对象**: HEAD `e630441`（"Update README.md", 2026-08-31）
- **对照基线**: `docs/OPTIMIZATION_REVIEW.md`（2026-08-30，18 Critical + 39 High + 业务缺口）
- **方法**: 5 路并行深度审查（旧报告逐项验证 ×2 + 新问题猎取 ×3）+ 外部调研（同类开源项目 + FDA/EMA 监管动态）+ 主审独立抽查交叉验证。覆盖度声明见附录。
- **结论速览**: 旧报告 18 项 Critical 中 **14 项已修复、4 项部分修复、0 项未修**；39 项 High 中 **31 项已修复、6 项部分、2 项未修**——"Roadmap 36/36 complete" 声称基本属实，Critical 层无虚报。**但本次复审新发现约 90 项旧报告未覆盖的问题（其中 11 项 High 级）**，最严重的意外是：**部署层当前是坏的**（nginx 变量 typo 导致 frontend 容器无法启动）、**前端 CI 门禁必红**、**CD 可能部署未经 CI 验证的提交**。8-31 修复爆发的精力全部落在代码层，deploy/CI/运维层没有同等 rigor。

---

## 一、旧报告修复验证

### 1.1 Critical C1–C18（18 项：14 修复 / 4 部分修复 / 0 未修）

| # | 问题 | 状态 | 关键证据（HEAD e630441） | 残留 |
|---|------|------|--------------------------|------|
| C1 | 规则版 Agent 硬编码 demo 案例 EMP-2187 结论 | ✅ 已修复 | `backend/agent_service/routes.py:229-308,547-624,835-877` 三个规则版构造器全部数据驱动，docstring 明言禁止携带案例事实性结论 | 无 |
| C2 | 电子签名无顺序/状态校验 | ✅ 已修复 | `backend/case_service/routes.py:683-812` submit→approve 顺序、双签人不同、closed 禁签、确定性 sig_id 防重放；DB 唯一约束在 alembic `20260830_0002:68-70` | 见新发现 B3（qa_reviewer 归属未校验） |
| C3 | POST /audit/logs 可伪造 operator | ✅ 已修复 | `backend/audit_service/routes.py:108-135` 权限收紧至 `system:configure`，服务端强制归因 | 无 |
| C4 | seed TRUNCATE 审计表绕触发器 | ✅ 已修复 | `backend/db/01_schema.sql:231-240,275-291` 防 TRUNCATE 触发器；`backend/seed.py:272-310` 默认不碰审计表，`SEED_RESET_AUDIT=1` 受控通道 | 卸载/重建触发器间隙的微小窗口（可接受） |
| C5 | HITL edit 被当 rejected / gate 顺序不强制 | ✅ 已修复 | `backend/case_service/routes.py:337-527` 顺序强制、edit 分支真实写入 FiveWhy、progress 系统计算、行锁 | 见新发现 B2（edit 分支权限缺口） |
| C6 | Evidence Hunter 不调 connector 不落库 | ✅ 已修复 | `backend/evidence_service/routes.py:215-298` 四源 connector_map + content_hash 去重落库 + EVIDENCE_FETCHED 审计 + employee_id 推导 | Hunter 不主动触发 fetch、无四源聚合入口（低危待办） |
| C7 | 分钟级流水线跑 BackgroundTasks 无恢复 | ⚠️ 部分修复 | 已加 `recover_zombie_runs`（`routes.py:1736-1770`，15 分钟阈值，启动时标记 failed + 审计）；**仍无持久化队列**（`routes.py:2117` 依旧 BackgroundTasks） | 进程重启丢 running 任务，只能标 failed 不能续跑（P1） |
| C8 | 全程单事务 / pause 失效 | ✅ 已修复 | `backend/shared/database.py:51-60` 短事务；每阶段前状态点检查（`routes.py:1817-2003` 共 8 处）；pause/cancel/resume 带行锁 | 无 |
| C9 | except 路径 PendingRollbackError | ✅ 已修复 | `routes.py:2023-2049` 全新 session 兜底 + 内层防二次崩溃 | 无 |
| C10 | run 无去重 + 时间戳主键冲突 | ⚠️ 部分修复 | 应用层 ACTIVE_RUN_STATUSES 查重→409（`routes.py:2082-2093`）；run_id 改 uuid4 后缀（`:2096`） | **DB 层无部分唯一索引**，check-then-insert 并发竞态仍在（P0 一条 DDL 可闭环） |
| C11 | 审计 seq 竞态 | ✅ 已修复 | `backend/shared/audit.py:96-106` pg_advisory_xact_lock + seq UNIQUE 双保险 | 引入新问题（见 B10：持锁=整个事务） |
| C12 | 默认 JWT secret 无 prod 防呆 | ✅ 已修复 | `backend/shared/config.py:140-155` prod RuntimeError fail-fast + 强制 service_api_key | staging 仍不拦（见 E3） |
| C13 | alembic 缺 embedding 三列 | ✅ 已修复 | alembic `20260830_0002:33-36` + `20260831_0004`；CI `alembic check` + downgrade 往返（`ci.yml:195-205`） | 01_schema.sql 与 alembic 仍双轨（CI 兜底） |
| C14 | 前端 HITL 按钮缺失 | ✅ 已修复 | `frontend/src/pages/InvestigationPage.tsx:76-93,255-262,378-490` accept/reject/edit 三按钮 + gate2 五条 why 编辑 + 3s 轮询 + run 控制按钮 | 无 |
| C15 | compose 缺 3 个 connector | ✅ 已修复 | `deploy/docker-compose.yml:176-230` 四 connector 齐全，与 CD 九服务清单一致 | 无 |
| C16 | bind-mount + CD pull 不通 | ✅ 已修复 | compose 全服务带 image 字段、dev 挂载移至 `docker-compose.dev.yml`、CD `docker compose pull` + 真实健康检查循环（`cd.yml:133-156`） | 无 |
| C17 | 无备份 | ⚠️ 部分修复 | `deploy/docker-compose.yml:264-286` 新增 db-backup（每日 pg_dump -F c，保留 30 天） | **挂在 `profiles:["backup"]` 下，默认 up 不启动**；无 WAL 归档/PITR、无异地上传、无恢复演练 runbook（残留问题里业务连续性风险最大项） |
| C18 | CI 零安全扫描 | ⚠️ 部分修复 | `ci.yml:72-98` pip-audit `--strict` CVE 门禁 + Trivy fs 扫描（CRITICAL/HIGH 阻断）；`cd.yml:93-98` Syft SBOM | 无 SAST（bandit/semgrep/codeql 零命中）；无 cosign 镜像签名 |

### 1.2 High H1–H39（39 项：31 修复 / 6 部分修复 / 2 未修）

**已修复（31）**: #1 refresh jti 黑名单轮换吊销、#2 access jti、#3 token 仅内存、#5 422 脱敏、#6 bcrypt anyio 包装、#8 时间窗锁定、#10 审计 hash v2+精确比对、#11 single-flight 刷新、#12 登出清缓存、#13 导出 axios blob、#14 幻觉证据过滤、#15 connector X-Service-Key、#16 demo 口令 DEV 门控、#17 MFA 关闭声明、#18 anthropic /messages 协议、#19 debate 异模型+clamp≤0.7、#22 连接池 5+10、#23 embed SKIP LOCKED+64 分批+模型过滤、#24 嵌入审计、#26 httpx 按循环缓存、#29 Unleash 单例、#30 flags deepcopy、#31 HITL 行锁、#32 gate3 真重试、#33 案例 ID 冲突重试、#34 KPI 实算、#35 错误码注册表、#36 /ready 503、#37 fingerprint HMAC、#38 metrics 低基数、#39 客户端 IP 统一口径。

**部分修复（6）**:

| # | 问题 | 已做 | 残留 |
|---|------|------|------|
| H4 | XFF 伪造 + 内存限流 | Redis storage + 默认不信 XFF | `deploy/nginx.conf:25` 变量 typo（见 A1） |
| H7 | 密码策略 | min_length 12、72 字节拒收 | 仍无复杂度要求 |
| H9 | 防篡改触发器覆盖 | 补了 signatures 表 | **evidence、agent_runs 仍可 UPDATE/DELETE** |
| H20 | 向量全表扫 | limit(2000) 上限 | 未迁 pgvector，2000 即天花板（见 C1，本次已量化危害） |
| H21 | selectin 级联 | 六个重集合改 lazy="select" | investigator/qa_reviewer 两个 User 关系仍 selectin（危害小） |
| H25 | token usage 丢弃 | 指标 + length 截断抛错 | 无 run 级预算、无成本核算 |

**未修（2）**:
- **H27** `agent_service/routes.py:70-73` `_hash_output`（`json.dumps(..., sort_keys=True, default=str)`）与修复前逐字相同（diff 确认），重复查询亦无改造 —— 修复提交完全未覆盖此项。
- **H28** `deploy/docker-compose.yml:104-228` 九个服务全部单 worker uvicorn。

**验证结论**: "逐项修复 code review P0/P1/P2" 提交声称中，仅 H27/H28 属于"声称修复但没修/没修对"，其余 36 项 High 的修复均有真实代码且多处超出报告要求的修法（如 C5 顺带修了 H32 与并发 JSONB 覆盖，C11 顺带修了 H10 宽容匹配假通过）。

---

## 二、新发现问题（本次复审，旧报告未覆盖）

### 2.1 A 组 · 部署与可用性（P0 —— 当前状态部署即坏或守不住）

| # | 严重度 | 问题 | 位置 | 说明与修法 |
|---|--------|------|------|------------|
| A1 | **P0** | nginx 反代变量 typo，frontend 容器无法启动 | `deploy/nginx.conf:25` | `proxy_set_header X-Forwarded-For $proxy_x_forwarded_for;` —— `$proxy_x_forwarded_for` 不是 nginx 变量（标准是 `$proxy_add_x_forwarded_for`），nginx 启动即 `[emerg] unknown variable` → frontend crash loop，**整个系统不可用**。注释意图正确（覆盖而非追加防伪造），修复时变量名打错。主审已核对原文。修后 `docker compose run --rm frontend nginx -t` 验证 |
| A2 | **P0** | 前端刷新即坏：F5 后"已登录无 token" + logout 递归死循环 | `frontend/src/stores/auth.store.ts:73-76`、`frontend/src/services/api-client.ts:36-62` + `auth.store.ts:58-64` | partialize 持久化 `isAuthenticated:true` 但 token 仅内存 → F5 后所有请求 401；`logout()` 先 `await POST /auth/logout` 再清 state，无 token 时该请求 401 → 拦截器再调 `logout()` → 无限网络循环。修法：不持久化 isAuthenticated（或 token 入 sessionStorage）；logout 先清 state、用裸 axios（跳过拦截器）调登出端点 |
| A3 | **P0** | 前端 CI 门禁必红 | `frontend/package.json:10` | `"lint": "eslint . --ext ts,tsx"` —— flat config（ESLint 9+，`eslint.config.js` 已存在）下 `--ext` 为无效选项，lint 必失败，而 `ci.yml:126` 直接跑 `npm run lint`。去掉 `--ext` 并修复 lint 暴露的问题 |
| A4 | **P0** | CD 可能部署未经 CI 验证的提交 | `.github/workflows/cd.yml:39-45,113-117` | workflow_run 触发的 CD 用 `GITHUB_SHA` 检出打镜像，但该事件里 GITHUB_SHA 是**默认分支 HEAD** 而非 CI 验证的 `head_sha` → 连续 push 时错位。修法：checkout `ref: ${{ github.event.workflow_run.head_sha }}` |
| A5 | P0 | 缺 `.dockerignore` | 仓库根 + `deploy/Dockerfile.backend:26`、`Dockerfile.frontend:10` | `COPY backend /app`、`COPY frontend /app` 把 htmlcov/`__pycache__`/node_modules/dist 全打进构建上下文与镜像；宿主 node_modules 还会**覆盖 builder 内 npm ci 产物**。补两个 .dockerignore |
| A6 | P0 | 部署路径无迁移 + 配置兜底默认值 | `deploy/docker-compose.yml:51,59,270`、`deploy/.env.example:7,14`、`deploy/prometheus.yml:5-6` | ① 全新部署只执行 01_schema.sql initdb + seed，运行时无 `alembic upgrade head` → 与迁移 head 漂移时启动即崩，CI 的 alembic-check 挡不住部署路径；② POSTGRES_PASSWORD 兜底 `"rca-secret"`（compose/db-backup 双处）、DEBUG=true 示例值 → 漏配 .env 静默用弱配置，改 `${POSTGRES_PASSWORD:?err}`；③ prometheus rule_files 引用不存在的 `prometheus-alerts.yml`，挂载即启动失败 |

### 2.2 B 组 · GxP 合规硬伤（P0/P1）

| # | 严重度 | 问题 | 位置 | 说明 |
|---|--------|------|------|------|
| B1 | **P0** | 结案后仍可改写调查结论 | `backend/case_service/routes.py:552-607`（update_five_why）、`613-670`（resolve_skeptic） | update_case/handle_hitl_gate/sign_case 都有 `closed` 拦截，**唯独这两个写端点没有任何案例状态检查**（主审核对 552-581 确认：直接查 FiveWhy 即改，case 根本未加载）。closed 后持 `hitl:edit` 仍可改 5 Whys、持 `hitl:accept` 可翻转 Skeptic 结论——签发后篡改调查记录，状态机被绕过；evidence_ids 还可设任意值无存在性校验 |
| B2 | **P0** | HITL gate2 edit 绕过 `hitl:edit` 权限 | `backend/case_service/routes.py:349,397-440` | 依赖只声明 `require("hitl:accept")`，edit 分支直接改写 five_whys；对照独立端点 `PUT /five-whys/{id}` 要求 `hitl:edit`（:564），而 qa_reviewer 有 accept 无 edit（`shared/security.py:158-164`）→ 同一能力两条路径两套权限，RBAC 矩阵被绕过。修法：edit 分支内补 `auth.check("hitl:edit")` |
| B3 | **P0** | admin 可单人双签；qa_reviewer_id 全程零消费 | `backend/shared/security.py:141-149`、`backend/case_service/routes.py:706-709,754-758` | admin 同时持有 sign:submit + sign:approve，批准校验仅"存在他人 submit + 签名人不同"→ 一人可完成 submit→approve 结案；`Deviation.qa_reviewer_id`（models.py:80）创建后从不校验——指派 A 做 QA，实际 B 也能批。修法：approve 校验 signer==case.qa_reviewer_id（或角色）；文档化 admin 双签策略 |
| B4 | P1 | HITL/签名无数据级隔离 | `backend/shared/security.py:140-176`、`case_service/routes.py:343-527` | 纯角色制矩阵，无"本人案例"维度 → investigator 可对自己调查的案例连过 5 关（自我审批）；多厂区场景无跨区约束 |
| B5 | P1 | reviewing 状态下仍可改核心字段 | `backend/case_service/routes.py:250-327` | PATCH 只拦 closed/reviewing 状态设置与 progress，未拦"处于 reviewing 时改 title/severity/description"→ 已审批的 HITL 结论与签名覆盖的案例内容可能不一致。修法：reviewing 下字段白名单，或改结论性字段即重置相关 gate |
| B6 | **P1** | 证据/查询含 PII 直通第三方 embedding API | `backend/evidence_service/routes.py:132-140,335,420,471` | `_summary_of` 兜底拼接 item 前 3 个字段（可含员工姓名/工号），summary 与用户查询原文发送外部 provider，无脱敏无开关。修法：白名单字段提取 + PII 掩码 + 显式配置开关 |
| B7 | **P1** | 源系统数据未隔离直接拼入 LLM 提示词（提示注入） | `backend/agent_service/routes.py:383-391,402-407` | connector 内容是不可信输入，无定界符/转义/长度上限，注入可操纵 5 Whys 与 root_cause_summary，**且结论经 hash 入审计链**——被污染的结论获得形式上的完整性背书。skeptic/drafter 同构路径大概率同模式。修法：数据区显式定界 + 控制字符剥离 + 字段级截断，输出只信结构化字段 |
| B8 | P1 | jti 吊销 fail-open 无告警 | `backend/shared/security.py:229-241`、`backend/shared/auth_middleware.py:88-91` | Redis 故障窗口内已吊销 token 全部复活，logout 仍显示成功，仅审计 detail 有字样。两个子代理独立收敛到此。修法：prod 打 ERROR 指标/告警，评估 fail-closed |
| B9 | P1 | 审计链尾截断不可检测 | `backend/shared/audit.py:226-286` | 无外部锚点：删除链尾 N 条后校验仍通过，GxP 10 年取证完整性存疑。修法：定期把 (seq, prev_hash) 锚定到外部只读存储（WORM/对象锁） |
| B10 | **P1** | 审计 advisory lock 持锁=整个业务事务 + 冲突打挂业务请求 | `backend/shared/audit.py:96-106,144` | C11 修复引入的新瓶颈：事务级锁使 pipeline 长事务期间全部审计写全局排队；锁失败 `except Exception: pass` 静默回退竞态，seq UNIQUE 冲突时 `append_audit_log` 异常上抛 → **业务事务整体回滚**（create_case/sign_case 等全部写端点同事务写审计）。修法：审计行独立短事务写入 + IntegrityError 捕获重试 |
| B11 | P1 | 服务间调用在本地事务提交前 + 失败静默 | `backend/case_service/routes.py:485,530-546`、`backend/evidence_service/routes.py:261-275` | 本地事务随后回滚时，远端已创建的 run/已拉取数据成孤儿无补偿；gate3 重试触发失败 `except: pass`（:544-546），gate 已重置 pending、案例卡 investigating，**审计却记"已触发"**（记录失真）。修法：after-commit 调用 + 失败写 RETRY_TRIGGER_FAILED 审计 |

### 2.3 C 组 · 性能（P1）

| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| C1 | 向量检索 2000 条上限：老证据静默不可召回 + 事件循环阻塞（量化） | `backend/shared/vector_store.py:88-97,104-109,52-65` | `LIMIT 2000 ORDER BY created_at DESC` → 超 2000 条后老证据召回静默丢失无告警；2000×1024 维纯 Python 余弦 ≈0.6-1s 同步 CPU 阻塞 + 约 40MB JSON 反序列化；SELECT 整行 ORM 含 raw_data+embedding 大字段只用 id/summary；读写均无 embedding_model 过滤（换模型后同维度旧向量参与余弦，结果无意义）。pgvector（HNSW）仍是正解，`01_schema.sql:13` 注释自认"Phase 2 启用" |
| C2 | 九服务单 worker | `deploy/docker-compose.yml:104-228` | H28 未修；配合 C3 构成并发瓶颈 |
| C3 | db 会话跨 LLM await 持有 | `backend/agent_service/routes.py:154-167` | `embed_text → find_related` 期间持有请求级 AsyncSession，30s 级网络调用占池化连接，9 进程 × pool15 下并发 run 耗尽连接池 → 全站 500。修法：LLM 调用前释放会话或向量检索独立短事务 |
| C4 | H27 未修：hash 序列化 + 重复查询 | `backend/agent_service/routes.py:70-73` | 见 1.2 |
| C5 | KPI 7 个 count 串行往返 | `backend/case_service/routes.py:832-920` | 可合并条件聚合单查询 |
| C6 | LLM 客户端缺工程化基线 | `backend/shared/llm_client.py:456-470,311-314,148,403-423` | chat_json 解析/schema 校验失败不 reprompt 直接降级规则版；`finish_reason=length` 确定性截断仍进重试循环白烧 token；timeout 单值 30s 无分级；无 semaphore 并发限流、无 provider 故障转移、无成本核算（有 token 数无 `LLM_COST_TOTAL`） |
| C7 | 限流实现瑕疵 | `backend/shared/rate_limit.py:33-44`、`backend/shared/metrics.py:90-95` | slowapi `redis://` 是同步存储，事件循环内同步网络 IO；无视 `settings.redis_enabled` 与注释不符；`RATE_LIMITED_TOTAL` 疑似无 inc 调用点，限流不可观测 |
| C8 | agent_states JSONB 整包读改写 | `backend/shared/models.py:256` | 每 step 重写全量 JSONB（TOAST 放大）+ 并发丢失 + 无大小上限；改 `jsonb_set` 增量 |

### 2.4 D 组 · 前端体验（P2）

| # | 问题 | 位置 |
|---|------|------|
| D1 | 启动 Agent Modal 无 stopPropagation（点内容冒泡关窗）、无 role=dialog/aria-modal/Esc/焦点陷阱；a11y 批：LoginPage label 缺 htmlFor、Toast 无 aria-live、InboxPage 筛选无 aria-pressed | `frontend/src/pages/InvestigationPage.tsx:338-372,404-418`、`LoginPage.tsx:100,115`、`Toast.tsx:42-64`、`InboxPage.tsx:92-121` |
| D2 | 无 `path="*"` 404 兜底（未知路径白内容区）；无 React ErrorBoundary、QueryClient 无全局 onError（渲染异常/发版后旧 hash chunk 404 白屏） | `frontend/src/App.tsx:63-108`、`main.tsx:12-20` |
| D3 | AuditPage 搜索无防抖且 keyword 变化不重置 page=1（第 N 页过滤必空）；DashboardPage `items?.map() \|\| 空态` 模式——空数组 truthy，真实空数据显示空白，"暂无案例"不可达 | `frontend/src/pages/AuditPage.tsx:19,26-32`、`DashboardPage.tsx:105-146,161-183,201-218` |
| D4 | 死 UI："新建偏差"按钮无 onClick（create API 已就绪）、通知假红点、"系统正常"硬编码；gate2 edit 零修改提交发空 five_whys → 后端 ValidationError 只弹笼统报错 | `InboxPage.tsx:70-72`、`AppShell.tsx:147-150`、`DashboardPage.tsx:49-51`、`InvestigationPage.tsx:404-418` |
| D5 | 拦截器 reject 裸对象非 Error 实例（丢堆栈、instanceof 失效） | `frontend/src/services/api-client.ts:68` |

### 2.5 E 组 · 卫生与可维护性（P2/P3）

- **E1** `backend/agent_service/routes.py` 2276 行单文件：6 个 Agent 实现 + prompt + schema + 编排 + 端点耦合，无法独立测试/灰度/复用。拆 `agents/`（每 Agent 一模块 + 基类）、`pipeline.py`、`api.py`（亦符合单文件 ≤500 行的工程约定）。
- **E2** 供应链卫生：pytest/ruff/mypy 混入运行时镜像（`backend/requirements.txt:50-55`），GxP 场景无 `--require-hashes`；`deploy/Dockerfile.backend` 单阶段构建留 gcc/libpq 编译链；`.pre-commit-config.yaml:44-51` mirrors-eslint hook 缺 typescript-eslint/@eslint/js 等直接 import 的依赖 → hook 必崩，前端 pre-commit 形同虚设。
- **E3** 配置防呆缺口：`_INSECURE_DEFAULTS` 仅 prod 拦截，staging 带默认密钥照常启动（`shared/config.py:133-155`）；`postgres_dsn` 密码未 `quote_plus`（`config.py:116-121`）；CORS `*` + allow_credentials 无互斥校验（`service_base.py:117-138`）；SQL `echo=settings.debug` 把含 PII 的参数写日志；`X-Trace-Id` 未校验直入审计与日志（`error_handler.py:82`）；`llm_fallback_reason` 内嵌 `str(e)[:200]` 入审计链与响应，可能带上游 URL/拓扑（`agent_service/routes.py:220,225`）。
- **E4** CI/CD 成熟度：cd.yml 无回滚机制（健康检查失败仅 exit 1）；SBOM 仅 auth-service 一个镜像；actions 未 pin sha（GxP 可复现性）；测试矩阵单一（Python 3.11/Node 20 无 matrix）；k6 只压只读路径（agent 流水线/HITL 写路径未压），且 100VU 真实登录会触发限流/锁定致压测失真。
- **E5** 低危正确性杂项：`get_trend` 分类 `"em" in ct` 子串误归（`case_service/routes.py:961-962`，system/temperature 误归 microbe）；audit list/export LIKE 通配符未转义（`audit_service/routes.py:58-59,154`）；submit_sig 匹配中文魔法串 `"提交签名"`（`case_service/routes.py:730,773`）；`_collected_at_of` 解析失败静默回退 `now()`（`evidence_service/routes.py:143-151`，外部时间戳失真）；未认证 LOGIN_FAILED 审计 operator 取攻击者可控 email（`auth_service/routes.py:83`）；`Capa.effectiveness_rate` 注解 float 列 Integer（`models.py:323`）；旧事件循环 AsyncClient 永不 aclose（fd 泄漏）；`llm_client.last_usage` 单例字段并发覆盖。
- **E6** `SourceSystem.last_sync_at / health_status` 从不更新（`evidence_service/routes.py:215-298`）—— `/source-systems` 展示的连接健康恒为 seed 值 "unknown"，监控假数据。
- **E7** MFA/TOTP、管理员解锁端点、会话超时语义（refresh 重签 access 刷新 iat 使 `session_timeout_minutes` 形同虚设，`auth_middleware.py:102-107`）—— 认证域待补清单。

---

## 三、业务功能模块对标

### 3.1 同类开源项目格局（agent-reach 调研，2026-10）

| 项目 | 定位 | 对本项目的参考价值 |
|------|------|--------------------|
| **Arvo-AI/aurora**（444★，Apache-2.0，活跃） | LangGraph agentic incident management & RCA（SRE 域），集成 PagerDuty/Datadog/Grafana/Slack | 最强对标：任务持久化编排、事件源集成、运行时可观测的成熟实现 |
| AgentForge（22★） | FastAPI+LangGraph+React+HITL approvals+pgvector | 技术栈几乎同构，可直接参考其 pgvector + approval 工作流实现 |
| ForgeFlow | LangGraph+MCP+A2A+pgvector，circuit breakers，LLM-as-judge | 值得抄两点：**LLM 调用 circuit breaker**（本项目 LLM 无故障转移）与 **LLM-as-judge 佐证 Skeptic 评分** |
| OPSLENS-AI | K8s 调查，supervisor + guardrails | guardrails 模式对应本项目 Guardian Agent 的强化方向 |
| dearauditor-qms-baseline（16★） | GitHub-native eQMS，ISO 13485/14971/IEC 62304 + GAMP5/21 CFR Part 11 overlay | **eQMS 公共基线 = CAPA 闭环 + 变更控制 + 有效性验证**，业务补全的现成清单 |
| OpenQMS（32★，AGPL，活跃至 2026-04） | 传统 eQMS | 文档控制/培训管理模块参考 |

**格局结论**: agentic RCA 在 SRE/AIOps 赛道已拥挤（aurora 为头部），在**制药 GxP 偏差调查赛道仍是空白**——本项目差异化定位成立且无人对标；短板不在"AI 编排"（多 Agent/HITL/审计链已是同类中上水平），而在（a）部署可靠性、（b）性能工程、（c）eQMS 业务闭环。

### 3.2 业务功能差距（结合代码证据更新旧报告第三节）

1. **CAPA 管理是"假完成度"** —— 新发现 M7：alembic `20260830_0003` 已建 `capas`/`reports` 表 + `risk_rpn/risk_category/risk_owner` 列（`shared/models.py:302-370,92-94`），但 **grep 全 backend 无任何路由读写**（僵尸表）；KPI 的 `capa_overdue_rate` 仍按 `deviations.deadline` 口径计算（`case_service/routes.py:883-890`），与 Capa 表语义冲突。要么补 API 激活，要么降级迁移，最忌现在这种"表在 API 无"的状态。
2. **报告版本管理缺失**：Report 表建了没用；无版本链、无 diff、无再签发流程（结案后修订是 GxP 高频场景，配合 B1 的 closed 后可改问题更显紧迫——**正确做法是"closed 后走版本化修订 + 重签"，而不是直接 UPDATE**）。
3. **ICH Q9 风险评估缺失**：risk_* 列已建，Drafter 模板有 ICH Q9 章节占位，但无 RPN 计算/风险矩阵 UI/评审记录。
4. **偏差分级 / OOS-OOT 联动 / SLA / 变更控制 / 批次影响评估 / 协作通知**：均无（旧报告结论维持）。
5. **证据 ALCOA+**：C6 修复后落库+审计已达标，但"Attributable"维度仍弱（connector 拉取的原始操作人仅存 raw JSON，无实体归一化）。
6. **用户管理端点**：无（用户只能 seed）；配合 E7 的管理员解锁缺失，运营期账号管理靠 SQL。

### 3.3 demo↔生产一致性

旧报告 §四 的 17 项差距（⌘K、AI Copilot 流式可视化、Skeptic 弹窗、5 Whys 按钮、鱼骨图、报告预览/PDF、通知面板等）**结论维持**——本次未精读 demo/js，无新增清单；但需注意方向性问题：demo 的流式体验要求 LLM 客户端支持 streaming，而当前 `llm_client.py` 无流式（C6），补齐时先做后端 SSE。

---

## 四、监管动态（2026-10 视角）与落地映射

| 动态 | 状态 | 对本项目的要求 |
|------|------|----------------|
| **FDA 草案指南 FDA-2024-D-4689**《AI 支持的设备/质量监管决策可信度评估》（2025-01-07 发布，意见期 2025-04-07 截止，截至 2026-05 仍为 draft） | 7 步风险导向模型可信度评估框架，按 context of use (COU)；排除"运营效率"用途但**包括影响药品质量的用途 → RCA 调查报告在范围内** | 为流水线编写 COU 声明文档 + 模型可信度评估记录（评审、测试数据集、性能标准）；把"模型版本/提示词版本"纳入审计字段 |
| **FDA《Guiding Principles of Good AI Practice in Drug Development》**（2026-01，fda.gov/media/189581） | 10 项原则：human-centric / risk-based / GxP adherence / clear COU / multidisciplinary / 数据治理与文档化 / 模型设计最佳实践 / 风险导向性能评估（**含人机交互**）/ **生命周期管理（含数据漂移监控）** / 清晰 essential information | 逐条映射：HITL 5 关已覆盖 human-centric；**生命周期/漂移监控是缺口**——`evidence.embedding_model` 列已有，但无模型版本指标与漂移告警（对应 C1 的模型过滤缺失）；文档化对应 B9 审计锚定 |
| **EMA 反思论文 EMA/CHMP/CVMP/83833/2023**（2024-09-30 正式发布） | 制造阶段 AI（§2.3.6）须遵循 ICH Q8/Q9/Q10 质量风险管理；引用 EU AI Act，强调 human-centric | ICH Q9 模块（3.2-3）从"加分项"升级为"合规必选项"；EU 部署还需评估 EU AI Act 透明度义务 |

**一句话**：监管方向从"能不能用 AI"转向"如何证明 AI 可信"——本项目的审计链/HITL 是正确资产，缺的是 **COU 文档、模型生命周期治理、外部审计锚定** 三件套；而 B1/B3/B7 这类"签发后可篡改/可被注入操纵"的缺陷，恰是监管检查最先打的点。

---

## 五、优化路线图

### P0 —— 立即（约 2-3 人日，全部是小改动，但决定系统"能不能用/敢不敢签"）

1. `deploy/nginx.conf:25` → `$proxy_add_x_forwarded_for`，`nginx -t` 验证（A1）
2. 前端 auth：不持久化 isAuthenticated + logout 先清 state/裸 axios（A2）
3. `package.json` 去掉 `--ext`，修 lint（A3）
4. cd.yml checkout `head_sha`（A4）
5. 补 `.dockerignore` ×2（A5）
6. `update_five_why`/`resolve_skeptic` 补 closed 拦截（先加载 case）+ evidence_ids 存在性校验（B1）
7. gate2 edit 分支补 `auth.check("hitl:edit")`（B2）
8. compose：`${POSTGRES_PASSWORD:?}`、db-backup 移出 `profiles`、部署路径加 `alembic upgrade head`、删/建 prometheus-alerts.yml（A6 + C17 残留）
9. alembic 0005：agent_runs 部分唯一索引 `WHERE status IN ('pending','running')`（C10 残留）
10. approve 校验 qa_reviewer_id + admin 双签策略文档（B3）

### P1 —— 近期（2-4 周，可靠性与性能工程）

- **流水线持久化执行**：最小改动是 DB 轮询 worker（runs 表 queued 状态 + SKIP LOCKED 取任务），替代 BackgroundTasks（C7 残留）
- **pgvector 迁移** + embedding_model 过滤 + 截断告警（C1/H20）
- uvicorn 多 worker/副本（C2）；db 会话不跨 LLM await（C3）
- 审计写入独立短事务 + IntegrityError 重试（B10）；after-commit 服务间调用 + 重试失败显式审计（B11）
- jti fail-open 告警指标（B8）；审计链外部锚定（B9）
- 提示注入防护：定界符 + 控制字符剥离 + 字段截断（B7）；PII 掩码/白名单 before embed（B6）
- fetch_from_source：case_id 校验 + `ON CONFLICT DO NOTHING` + 批量去重（H4 同类，并发 500）
- CI/CD：SAST（semgrep 或 bandit）+ cosign 签名 + CD 回滚 job（C18 残留 + E4）
- LLM 客户端：校验失败 reprompt 1-2 次、length 不重试、分级 timeout、Semaphore、成本指标（C6）

### P2 —— 中期（1-2 月，业务补全与治理）

- **激活 Capa/Report 僵尸表**：CAPA 闭环 API（计划→执行→验证→有效性检查，参照 dearauditor 基线）+ KPI 口径对齐（3.2-1/2）
- 报告版本化修订 + 结案后修订走重签流程（替代直接 UPDATE）
- ICH Q9 风险评估落地（risk_* 列已有）；偏差分级、OOS/OOT、变更控制、批次影响、协作通知
- 数据级隔离：investigator 不可自批、qa_reviewer 只审指派案例（B4）；reviewing 字段白名单（B5）
- 模型生命周期治理：COU 文档、提示词/模型版本审计字段、漂移监控（第四节）
- 前端 D 组批量修复（a11y、404、ErrorBoundary、空态、防抖）
- requirements 拆分 + `--require-hashes`、Dockerfile multi-stage、pre-commit 修复（E2）

### P3 —— 长期

- 架构收敛 9 服务 → 2-3 单元（旧报告建议维持；2276 行 routes.py 的拆分先行，收敛后自然归位）
- WAL 归档/PITR + 异地备份 + 恢复演练 runbook（C17 完全体）
- 多环境测试矩阵、actions pin sha、k6 写路径压测（E4）
- TOTP/MFA 实现（当前如实声明关闭，实现后再开）

---

## 附录 · 审查方法与覆盖度

- **验证方式**：5 个并行子代理（两个逐项验证旧报告、三个猎取新问题），所有结论带 file:line；主审对 3 个最关键新发现（A1/A3/B1）独立读码复核属实；两路子代理在 4 处独立收敛（nginx typo、jti fail-open、审计锁静默、gate2 权限），交叉可信。
- **覆盖**: shared/ 15 文件全读；case/evidence/auth/audit 四服务全读（含端点×权限矩阵逐项核查）；agent_service 大部（routes.py 精读至 L560 + 验证任务覆盖其余关键路径）；frontend/src 主要页面与 stores/services；deploy/、.github/、Dockerfile、pre-commit 全读。
- **未覆盖**: connectors/{lms,bms,ems,mes} 精读、seed.py、tests/ 盲区扫描（SQLite/JSONB 失真、双签/越权/并发测试缺失的旧结论维持）、demo/js 精读、alembic 与 01_schema.sql 逐列 diff。
- **行号基准**: HEAD `e630441`。行内引用的密码/邮箱形如 `{{SECRET_xxx}}` 系阅读层脱敏 artifact，非仓库真实内容，勿据此上报。
- **旧报告已过时项**（勿再引用）: 路由未懒加载、eslint 配置缺失、date-fns/d3 未移除、k6 路径错误、compose 缺 connector/backup、前端 token 入 localStorage 等，均已修复（见 1.1/1.2 及 f5e82e54 核对清单）。

---

## 实施记录（2026-10-25，同日完成）

> 本节为实施落账：上述 P0–P3 全部条目已实施完毕并有测试/部署验证背书。行内 file:line 以当日代码为准。

### P0（当日修复，全部完成）
- A1 nginx XFF 变量修复（$remote_addr 覆盖语义）；A2 前端 401 递归 logout/refresh 自环死锁/persist 假登录态三连修（bareApi + clear() + partialize 只存 user）；A3 eslint --ext 移除；A4 CD DEPLOY_SHA 以 CI 验证提交为准；A5 根 .dockerignore（node_modules/测试产物不入镜像）；A6 update_five_why/resolve_skeptic 行锁 + closed 拦截 + evidence_ids 存在性校验；gate2 edit 补 hitl:edit；compose POSTGRES_PASSWORD `:?` 防呆 + db-backup 默认启动 + prometheus 服务补齐；C10 补 DB 部分唯一索引（agent_runs 一个案例仅一个活动 run，alembic 0005 + IntegrityError→409）；签名 approve 校验指定 QA 审核人。
- P0 验证：全量 pytest 绿 + ruff/mypy 绿 + npm lint/build 绿 + alembic 三路径 Docker PG 实测（upgrade/check/downgrade/legacy stamp/调和兜底）。

### P1（九项全部完成）
1. C7 DB 轮询 worker（agent_service/worker.py：queued→claim SKIP LOCKED + active 唯一索引兜底 + BackgroundTasks 快路径并存）。
2. pgvector 迁移（alembic 0006 + 回填 + <=> 库内检索 + vector_dims/embedding_model 过滤 + Python 回退告警；镜像 pgvector/pgvector:pg15）。
3. uvicorn 多 worker（UVICORN_WORKERS=2 + prometheus_client multiproc 聚合 + 锚定 advisory lock 防重）+ C3 会话不跨 LLM await（release_db_connection 分阶段归还连接）。
4. B10 审计 SAVEPOINT 重试（IntegrityError→退避重算 seq，5 次耗尽 fail-closed E3002）+ B11 gate3 重试派发三态化（dispatched/skipped/failed，失败显式审计）。
5. B8 jti fail-open 指标 + 告警；B9 审计链外部锚定（WORM JSONL + 周期任务 + 三端点 + 篡改/链尾删除检测）。
6. B7 提示注入防护（text_guard：控制字符/Bidi/定界符中和 + 五个 prompt 全量 fence + system 硬性约束）+ B6 PII 出站掩码（embed_text 唯一咽喉）。
7. fetch_from_source case_id 校验 + begin_nested 幂等去重。
8. CI Bandit(-ll) 基线清零 + CD cosign 签名/验签 + 部署失败自动回滚 + 手动回滚 job。
9. LLM 客户端：截断不可重试 + chat_json 修复性 reprompt + 分级超时 + 并发 Semaphore + 成本估算指标。

### P2（八项全部完成）
1. CAPA 全生命周期状态机 + 报告版本化（四眼批准）+ gate4 自动快照 Drafter 报告 v1（僵尸表复活）。
2. ICH Q9 风险矩阵：FMEA 三分量→RPN→风险类别 + 严重度自动升级 + PATCH /cases/{id}/risk + 审计。
3. 数据级隔离：qa_reviewer 仅指派案例 + reviewing 字段白名单冻结。
4. 模型治理：docs/COU.md（FDA COU 框架）+ PROMPT_VERSIONS 双轨留痕 + LLM 失败率/成本告警。
5. 前端 D 组：ErrorBoundary/404/防抖/空态/Modal a11y/建案 Modal/ApiError 类型化。
6. E2 构建卫生：requirements 拆分 + requirements.lock（--require-hashes）+ Dockerfile multi-stage + pre-commit 修复 + CI 同步。
7. 结案重审：POST /cases/{id}/reopen（受控重开→修订→重签）。
8. 业务补全：deviation_type 七分类 + affected_batches 批次影响（alembic 0008）+ 站内通知（notifications 表 + 4 类发射点 + 前端 NotificationBell）+ capa_overdue_rate 口径对齐 CAPA 实体。

### P3（四项全部完成）
1. 架构收敛 9 容器 → 3 单元：routes.py 2276 行拆为 7 模块（全部 ≤500 行）后，core_service.main 聚合四业务 router + connectors.gateway 网关 + agent 独立；nginx/compose/dev/prometheus/CD 全量改齐。**实施中发现并修复隐藏缺陷：evidence_service connector URL 漏 /api/v1 前缀（真实部署 fetch 必 404），新增路由表契约测试锁死。**
2. WAL 归档 + PITR runbook（docs/RUNBOOK_BACKUP.md：恢复五步 + 季度演练 + GxP 恢复验证清单）。
3. Actions 全部 pin commit SHA（11 处，git ls-remote 解析）+ k6 写路径压测（deploy/k6/write-path.js）。
4. TOTP/MFA 全套（shared/totp.py RFC 6238 + alembic 0007 + 登录强制/激活防重放/禁用验密 + 前端 SecurityPage + 登录页 MFA 码；config.mfa_required 全员开关，默认 off）。

### 最终验证快照（2026-10-25 23:5x）
- backend：pytest 606+ passed / coverage 89.07%（门槛 80%）/ mypy 0 issues / ruff 全绿。
- frontend：npm lint 0 error / vite build 过。
- 迁移：alembic base→head、check（models↔迁移零漂移）、downgrade 往返、legacy stamp 路径均在真实 PG 15+pgvector 容器验证。
- 部署：docker compose config 通过；CD 镜像矩阵/cosign/回滚脚本与新三单元拓扑一致。
