# 1. 选择 FastAPI 作为微服务框架

- **Status**: Accepted (2025-08-17)
- **Deciders**: 后端架构组、平台工程组
- **Tags**: 后端、微服务、框架选型

## Context（决策背景）

RCA-pilot 项目是在 GxP（Good Practice）合规环境下，面向医药/生命科学行业的根因分析（Root Cause Analysis）平台。在确定后端技术栈时，需重点考虑以下约束：

1. **GxP 合规要求**：系统需满足 21 CFR Part 11 / Annex 11 等监管要求，涉及完整的审计日志、电子签名、数据完整性（ALCOA+ 原则）。框架必须支持强类型、可校验的请求/响应模型，以便生成可追溯的接口契约。
2. **Agentic AI 工作流**：核心 RCA 能力由多个 LLM Agent 协同完成（信息收集 → 假设生成 → 验证 → 结论），单次分析可能持续数十秒至数分钟，存在大量 I/O 阻塞（调用 LLM、检索向量库、查询外部系统）。框架必须原生支持异步并发。
3. **团队 Python 技术栈**：团队在数据科学、LLM 工具链（LangChain / LlamaIndex / 自研 Agent SDK）上积累了深厚的 Python 经验，数据/算法同学可以直接参与后端开发。
4. **性能需求**：面向企业内部部署，并发量在百级 QPS 量级，但单请求耗时长，需高吞吐的 I/O 处理能力；同时需支持多 worker 水平扩展。
5. **微服务架构**：平台拆分为 Auth、Workflow、Evidence、Report 等多个独立服务，每个服务需独立部署、独立演进。

## Decision（决策）

**选择 FastAPI 作为 RCA-pilot 的微服务 Web 框架。**

主要理由：

1. **原生 async/await 支持**：FastAPI 基于 Starlette + ASGI，原生支持 `async def` 路由，可在一个事件循环内并发处理多个 Agent 长任务，避免线程阻塞，充分利用 I/O 等待时间。
2. **Pydantic v2 自动校验 + OpenAPI 文档生成**：请求/响应模型基于 Pydantic v2（Rust 内核，性能优异），自动完成类型校验、序列化、文档生成。对于 GxP 合规场景，强类型契约本身就是可追溯性的体现，OpenAPI 文档可作为接口基线纳入配置管理。
3. **微服务友好**：每个服务一个 FastAPI app，启动轻量、依赖少，便于独立部署与版本管理；通过共享的 Pydantic schema 包实现跨服务类型一致性。
4. **性能接近 Node.js / Go**：在 uvloop + httptools 加持下，单实例 QPS 与 Express / Gin 同量级，满足业务性能需求；CPU 瓶颈可通过多 worker + 进程池解决。
5. **生态成熟**：与 SQLAlchemy 2.0 async、httpx（async HTTP client）、pytest、Celery 等主流库无缝集成；LLM 生态（openai、anthropic 等 SDK）均提供 async 接口，与 FastAPI 协同顺畅。

## Consequences（影响）

### 正面影响

- **开发效率高**：类型注解 + 自动文档显著降低前后端协作成本；Pydantic 模型即接口契约，减少手工维护文档。
- **类型安全**：mypy + Pydantic 在编码期与运行期双重保障，降低 GxP 关键路径的回归风险。
- **自动文档**：`/docs`（Swagger）与 `/redoc` 开箱即用，便于审计与 QA 验证。
- **异步友好**：Agent 流水线中的并发 LLM 调用、向量检索等 I/O 密集场景可高效复用事件循环。

### 负面影响

- **GIL 限制 CPU 密集任务**：CPython 的 GIL 使得单进程无法并行执行 CPU 密集型计算（如本地模型推理、大文本嵌入计算）。
- **async 生态仍有边角案例**：部分第三方库（如某些数据库驱动、旧版 SDK）仅提供同步接口，在 async 路由中调用需 `run_in_threadpool` 包装，存在引入阻塞的风险。
- **运维复杂度**：多 worker + 进程池的部署模型比同步框架更复杂，需关注 worker 数量、内存占用、进程间状态同步等问题。

### 缓解措施

- **CPU 密集任务卸载**：Agent 推理、嵌入计算等 CPU bound 任务通过 `concurrent.futures.ProcessPoolExecutor` 卸载到独立进程，绕开 GIL；重负载场景可进一步拆分为独立 Worker 服务（Celery / 独立进程），通过消息队列解耦。
- **同步库隔离**：在 async 路由中调用同步库时，统一使用 `starlette.concurrency.run_in_threadpool` 包装，并在 CI 中通过 `asyncio` lint 规则检测潜在的阻塞调用。
- **可观测性补强**：通过 OpenTelemetry 采集 async 链路 trace，结合 Prometheus 监控事件循环延迟（`asyncio.get_event_loop().time()`），及时发现阻塞点。

## Alternatives Considered（备选方案）

### Django REST Framework

- **优势**：成熟的 admin、ORM、auth 体系，社区生态完善。
- **劣势**：同步阻塞模型为主，async 支持仍处于逐步完善阶段；ORM 与异步 Agent 工作流配合不佳；整体偏「大而全」，与微服务拆分诉求不匹配。
- **结论**：不适用于以 async Agent 为核心的微服务架构。

### Flask + async extensions

- **优势**：轻量、灵活，团队熟悉度高。
- **劣势**：Flask 2.x 的 async 支持仍依赖 `asyncio.run` 包装，并非原生 ASGI；扩展生态碎片化，不同扩展的 async 兼容性参差不齐；缺乏内置的请求校验与文档生成能力，需额外引入 marshmallow / apispec 等库，集成成本高。
- **结论**：在 async 一等公民、类型校验、自动文档三个核心诉求上均需自行拼装，整体 TCO 高于 FastAPI。

### Node.js (NestJS)

- **优势**：异步 I/O 原生支持，TypeScript 类型系统成熟，前后端语言统一。
- **劣势**：团队 Python 优先，LLM/数据科学工具链以 Python 为主；切换到 Node.js 意味着算法与后端分属两套技术栈，跨团队协作成本上升；NestJS 的依赖注入体系对 Python 团队存在学习曲线。
- **结论**：技术栈战略不匹配。

### Go (Gin)

- **优势**：性能优异，并发模型（goroutine）简洁，部署单二进制。
- **劣势**：团队不熟 Go，开发速度慢；LLM/数据生态薄弱，Agent 工作流需大量胶水代码；缺乏类似 Pydantic 的运行期校验生态，GxP 合规的接口契约需手工维护。
- **结论**：开发效率与生态成熟度不占优。

## References（参考）

- FastAPI 官方文档：https://fastapi.tiangolo.com/
- Pydantic v2：https://docs.pydantic.dev/2.0/
- ADR 模板（michaelneale/adr-template）
- 21 CFR Part 11 电子记录/电子签名
- GAMP 5 第二版（ISPE）
