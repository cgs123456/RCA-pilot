"""Agent Service — Agent 编排引擎微服务

职责：
- Agent 流水线编排（Meta-Controller 状态机）
- 6 个 Agent 规则版实现（Phase 1）：
  - Evidence Hunter: 调用 Connector 收集证据
  - Reasoner: 模板版 5 Whys + 鱼骨图
  - Skeptic: 基于证据缺口的反例挑战
  - Drafter: ICH Q9 模板版报告生成
  - Guardian: ALCOA+ 9 项合规检查
- Agent 运行状态管理
- 每步输出 hash 存证

Phase 1: 规则版（无 LLM）
Phase 2: 升级为 LangGraph + LLM 真实调用
"""
