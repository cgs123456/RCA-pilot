# RCA Pilot — AI 用途说明（Context of Use, COU）

> 版本 1.0.0 · 生效日期 2026-10-25 · 文档所有者：质量保证（QA）
>
> 本文档依据 FDA《Considerations for the Use of Artificial Intelligence To Support
> Regulatory Decision-Making for Drug and Biological Products》（FDA-2024-D-4689 draft）
> 的基于用途（COU-based）风险评估框架，以及《Guiding Principles of Good AI Practice
> in Drug Development》（2026-01）十项原则编写，是本系统 AI 功能的受控文档。
> 任何 AI 用途、模型、提示词的变更都必须先修订本文件并通过变更控制。

## 1. 系统与用途概述

RCA Pilot 是面向 GxP 环境的偏差调查辅助平台。AI 仅用于**起草与辅助分析**，
不自动做出任何监管决策。所有影响案件结论的 AI 输出必须经人工审批（HITL 关卡）
与双人电子签名（21 CFR Part 11）后方可生效。

## 2. AI 用途清单（Context of Use）

| # | Agent | COU 描述 | 输出性质 | 人工监督 | 风险分级 |
|---|-------|----------|----------|----------|----------|
| 1 | Evidence Hunter | 从只读源系统（LIMS/MES/EMS/CMMS）检索与偏差相关的证据记录并去重 | 证据清单草稿 | gate1 人工确认证据集 | 低（检索） |
| 2 | Reasoner | 基于确认的证据生成 5 Whys 根因分析草稿 | 分析草稿 | gate2 可逐条编辑/接受/拒绝 | 中 |
| 3 | Skeptic | 对 Reasoner 草稿做对抗性质询，暴露证据缺口 | 质询清单 | gate3 人工裁决 | 中 |
| 4 | Reasoner+Adversary 辩论 | 主/反方双模型交叉论证根因，Meta-Controller 给出置信度 | 置信度建议（0.0-1.0） | 仅提示，不触发自动结论 | 中 |
| 5 | Drafter | 生成 ICH Q9 风格调查报告草稿 | 报告草稿 | gate4 评审 + 报告版本化 + 双签 | 中 |
| 6 | Guardian | 合规检查清单（审计完整性、签名完整性、必要字段） | 检查清单 | gate5 人工放行 | 低 |

**排除用途（明确不在 COU 内）**：自动关闭偏差、自动签署报告、自动决定 CAPA、
自动向监管机构提交任何材料、员工绩效评估。

## 3. 风险评估摘要（COU-based）

- **影响面**：AI 输出仅作为草稿进入人工审批流；错误输出的最终防线是
  gate2- gate5 五道人工关卡与双人签名。最坏情形（AI 输出完全错误且被人工接受）
  由 Skeptic 对抗审查、双人不同人签名、QA 指定审核人机制共同缓解。
- **可追溯性**：每个 Agent 阶段在审计链（SHA-256 hash 链，DB 级防 UPDATE/DELETE，
  外部周期锚定）中记录 ai_model、ai_prompt_hash（内容寻址）、ai_output_hash、
  prompt_version（语义版本）。
- **降级路径**：LLM 不可用时各阶段回退规则模板（rule-template），输出同样留痕，
  审计 ai_model=rule-template-v1，人工可见。
- **残余风险**：LLM 幻觉（证据引用已做存在性过滤）、提示注入（用户数据经
  定界符隔离与控制字符中和，见 shared/text_guard.py）、模型漂移（见第 6 节监控）。

## 4. 模型清单与配置

- 模型经 OpenAI 兼容协议统一接入（GLM / Qwen / OpenAI / Anthropic / Azure），
  每次调用记录 provider/model；辩论主/反方强制异模型。
- 模型不接触生产决策系统：连接器为只读，凭 X-Service-Key 服务间鉴权。
- 出站嵌入调用统一执行 PII 掩码（shared/text_guard.py mask_pii）。
- 模型切换属重大变更：需更新本文件 + 回归测试 + QA 批准，并在 feature flag
  灰度验证后全量。

## 5. 提示词治理

- 各 Agent 系统提示词在 `agent_service/routes.py` 内受控维护，对应语义版本见
  `PROMPT_VERSIONS`；任何提示词修改必须：升版本号 → 附变更说明 → 回归测试 →
  QA 批准。
- 运行实例记录：审计链 ai_prompt_hash（内容 hash，逐次调用）+ agent_states 内
  prompt_version（语义版本），二者结合可精确复现当时输入。

## 6. 性能监控与漂移管理

| 监控项 | 指标 | 告警 |
|--------|------|------|
| 调用成功率 | rca_llm_calls_total{outcome} | LlmCallFailureRateHigh（30 分钟失败率>30%） |
| 成本异常 | rca_llm_cost_estimated_cny_total | LlmCostSpike（24 小时估算成本超阈值） |
| 截断 | LLMTruncatedError（不重试，计 outcome） | 经 rca_llm_calls_total 体现 |
| jti 降级放行 | rca_jti_failopen_total | JtiBlacklistFailOpen（critical） |

- 漂移信号（成功率骤降、单案 token 用量骤增、截断率上升）触发 QA 评审，
  必要时回退上一 prompt_version 或切换模型。
- 每季度复核：模型清单、提示词版本、告警阈值、本文件与现实的一致性。

## 7. 生命周期与变更控制

1. 变更申请（COU/模型/提示词任一）→ 影响评估 → QA 批准。
2. 实施：代码 + 测试 + 本文件同步更新（原子提交）。
3. 验证：pytest 全绿 + 灰度 flag 验证 + 审计抽样核对。
4. 全量发布后关闭变更单；审计链全程留痕。

## 8. 职责

- **QA**：本文件所有者；COU 变更批准；漂移评审；签名策略。
- **系统所有者（IT）**：模型接入、密钥管理、监控告警值班。
- **调查员**：AI 输出的人工复核者；对最终结论负责（AI 不承担责任主体）。

## 9. 已知限制

- 多 provider 维度不一：向量检索在 PG 上为无维度列库内精确扫描（未建 HNSW），
  跨模型余弦分数仅作排序参考，不做绝对相似度断言。
- 规则模板回退版输出质量低于 LLM 版，界面与审计均可见，需人工补正。
- 中文 magic string 与界面文案耦合（如「提交签名」），变更提示词时需同步前端。
