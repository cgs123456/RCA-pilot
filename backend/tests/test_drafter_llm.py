"""Drafter LLM 化测试 — ICH Q9 自动撰写

覆盖：
- 规则版输出（flag 关闭时）
- LLM 路径成功（flag 开启 + mock LLM 返回 Markdown）
- LLM 路径 prompt 包含案例 + Reasoner + Skeptic 上下文
- LLM 路径清理 ```markdown 代码块包裹
- LLM 未配置降级
- LLM 抛异常降级（LLMError / 未知异常）
- LLM 输出包含 ICH Q9 必需章节
- 审计字段（ai_model / ai_prompt_hash / ai_output_hash）完整性
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Deviation, Evidence

pytestmark = pytest.mark.asyncio


# ============================================================
# fixture
# ============================================================
@pytest.fixture
async def drafter_case(test_db: AsyncSession, seed_db):
    """创建带证据 + Reasoner 输入的案例"""
    case = Deviation(
        id="DEV-DRF-001",
        title="B 级区沉降菌超标",
        description="B 级背景区沉降菌采样 22 CFU，超出阈值 15 CFU",
        severity="major",
        status="investigating",
        discovered_at=datetime.now(UTC),
        investigator_id="U-LC",
        product_impact="批号 BN-2025-08 待评估",
        immediate_action="暂停 B 级区作业，启动调查",
        progress=70,
        hitl_gates={"gate1": "passed", "gate2": "passed", "gate3": "passed", "gate4": "pending", "gate5": "pending"},
    )
    test_db.add(case)
    await test_db.flush()

    evidence = [
        Evidence(
            id="E-DRF-01",
            case_id="DEV-DRF-001",
            source_system="LMS",
            collected_at=datetime.now(UTC),
            summary="EMP-2187 更衣资质 2025-08-10 逾期",
            raw_data={},
            content_hash="sha256:drf-1",
            confidence=4,
        ),
        Evidence(
            id="E-DRF-02",
            case_id="DEV-DRF-001",
            source_system="BMS",
            collected_at=datetime.now(UTC),
            summary="AHU-7 差压 470 Pa 超过预警值",
            raw_data={},
            content_hash="sha256:drf-2",
            confidence=3,
        ),
    ]
    for ev in evidence:
        test_db.add(ev)
    await test_db.commit()

    # 重新加载 case with relationships
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    result = await test_db.execute(
        select(Deviation)
        .where(Deviation.id == "DEV-DRF-001")
        .options(selectinload(Deviation.evidence), selectinload(Deviation.five_whys))
    )
    return result.scalar_one()


@pytest.fixture
def reasoner_output():
    """Reasoner 输出"""
    return {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": [
            {
                "n": 1,
                "question": "为什么 B 级区沉降菌超标？",
                "answer": "因为 EMP-2187 更衣资质逾期仍进入 B 级区",
                "evidence_ids": ["E-DRF-01"],
                "is_root_cause": False,
            },
            {
                "n": 2,
                "question": "为什么 EMP-2187 资质逾期仍能进入？",
                "answer": "LMS 与排班系统未集成，无门控",
                "evidence_ids": ["E-DRF-01"],
                "is_root_cause": True,
            },
        ],
        "fishbone": {
            "root": "B 级区沉降菌超标",
            "categories": [
                {"name": "人员 Man", "causes": [{"text": "更衣资质逾期", "evidence": ["E-DRF-01"]}]},
                {"name": "设备 Machine", "causes": [{"text": "AHU-7 差压异常", "evidence": ["E-DRF-02"]}]},
                {"name": "物料 Material", "causes": []},
                {"name": "方法 Method", "causes": []},
                {"name": "环境 Environment", "causes": []},
                {"name": "测量 Measurement", "causes": []},
            ],
        },
        "root_cause_summary": "根因：LMS 与排班系统未集成导致 EMP-2187 资质逾期仍能进入 B 级区（E-DRF-01）",
        "ai_model": "rule-template-v1",
        "ai_output_hash": "sha256:mock-reasoner",
    }


@pytest.fixture
def skeptic_output():
    """Skeptic 输出"""
    return {
        "agent": "skeptic",
        "status": "completed",
        "challenges": [
            {
                "target": "根因结论：EMP-2187 是污染源",
                "challenge": "未排除其他当班人员引入污染的可能",
                "evidence_ids": ["E-DRF-01"],
                "type": "alternative_explanation",
            },
            {
                "target": "根因结论：AHU-7 差压是次要因素",
                "challenge": "AHU-7 差压持续超标，可能 HVAC 本身存在独立故障",
                "evidence_ids": ["E-DRF-02"],
                "type": "independent_cause",
            },
        ],
        "challenge_count": 2,
        "ai_model": "rule-template-v1",
        "ai_output_hash": "sha256:mock-skeptic",
    }


# ============================================================
# 规则版测试
# ============================================================
class TestDrafterRuleBased:
    """规则版 Drafter 测试 — flag 关闭时"""

    async def test_rule_based_returns_template_report(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        from agent_service.routes import _run_drafter

        with patch("shared.feature_flag.is_enabled", return_value=False):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        assert result["agent"] == "drafter"
        assert result["status"] == "completed"
        assert result["report_format"] == "ICH-Q9-template-v1"
        assert result["ai_model"] == "rule-template-v1"
        assert result["ai_output_hash"].startswith("sha256:")
        # 规则版不返回 LLM 字段
        assert "ai_prompt_hash" not in result
        assert "llm_enabled" not in result
        assert "llm_fallback_reason" not in result

    async def test_rule_based_report_contains_ich_q9_sections(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        """规则版报告包含 ICH Q9 关键章节"""
        from agent_service.routes import _run_drafter

        with patch("shared.feature_flag.is_enabled", return_value=False):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        report = result["report_markdown"]
        assert "偏差描述" in report
        assert "严重等级" in report
        assert "立即措施" in report
        assert "根本原因分析" in report
        assert "CAPA" in report
        assert "ICH Q9" in report
        # 案例信息应该出现在报告中
        assert "DEV-DRF-001" in report
        assert "EMP-2187" in report


# ============================================================
# LLM 路径成功
# ============================================================
class TestDrafterLLMPath:
    """LLM 版 Drafter 测试 — flag 开启且 LLM 可用"""

    async def test_llm_path_returns_valid_output(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        from agent_service.routes import _run_drafter

        # mock LLM 返回 ICH Q9 Markdown 报告
        mock_report = """# 偏差调查报告 — DEV-DRF-001

## 1. 偏差描述
B 级背景区沉降菌采样 22 CFU，超出阈值 15 CFU。

## 2. 严重等级评估
**Major** — 可能影响产品无菌性。

## 3. 影响产品评估
批号 BN-2025-08 待评估。

## 4. 立即措施
暂停 B 级区作业，启动调查。

## 5. 根本原因分析（5 Whys）
1. 为什么沉降菌超标？→ EMP-2187 更衣资质逾期 [E-DRF-01]
2. 为什么仍能进入？→ LMS 与排班未集成 [E-DRF-01]

## 6. 根本原因结论
LMS 与排班系统未集成导致资质逾期仍能进入 B 级区。

## 7. Skeptic 反例审阅
- 替代解释：未排除其他人员 [E-DRF-01]
- 独立根因：HVAC 可能独立故障 [E-DRF-02]

## 8. CAPA 建议
- 立即：暂停 EMP-2187 作业
- 短期：LMS↔排班集成
- 长期：HVAC 独立调查

## 9. 风险评估
残余风险等级：**Medium**

## 10. 合规声明
本调查遵循 ICH Q9 质量风险管理原则。"""

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus-mock"
        mock_client.hash_prompt.return_value = "sha256:mock-drafter-prompt"
        mock_client.chat_completion = AsyncMock(return_value=mock_report)

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        assert result["agent"] == "drafter"
        assert result["status"] == "completed"
        assert result["report_format"] == "ICH-Q9-llm-v1"
        assert result["ai_model"] == "glm-4-plus-mock"
        assert result["ai_prompt_hash"] == "sha256:mock-drafter-prompt"
        assert result["ai_output_hash"].startswith("sha256:")
        assert result["llm_enabled"] is True
        # 报告内容
        assert "DEV-DRF-001" in result["report_markdown"]
        assert "ICH Q9" in result["report_markdown"]
        # chat_completion 被调用且 json_mode=False
        mock_client.chat_completion.assert_awaited_once()
        _, kwargs = mock_client.chat_completion.call_args
        assert kwargs["json_mode"] is False

    async def test_llm_path_prompt_includes_all_context(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        """验证 prompt 包含案例 ID、证据 ID、Reasoner/Skeptic 输出"""
        from agent_service.routes import _run_drafter

        captured_prompt = {}

        async def mock_chat(system, user_prompt, json_mode=False, **kwargs):
            captured_prompt["system"] = system
            captured_prompt["user"] = user_prompt
            return "# Mock report"

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = mock_chat

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        # user prompt 应包含案例 + 证据 + Reasoner + Skeptic 信息
        user = captured_prompt["user"]
        assert "DEV-DRF-001" in user
        assert "E-DRF-01" in user
        assert "E-DRF-02" in user
        assert "EMP-2187" in user
        assert "Why #1" in user
        assert "Why #2" in user
        assert "LMS 与排班系统未集成" in user  # Reasoner 根因结论
        assert "未排除其他当班人员" in user  # Skeptic 挑战
        # system prompt 应包含 ICH Q9 章节
        sys_prompt = captured_prompt["system"]
        assert "ICH Q9" in sys_prompt
        assert "偏差描述" in sys_prompt
        assert "CAPA" in sys_prompt
        assert "风险评估" in sys_prompt

    async def test_llm_path_strips_markdown_code_fence(self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output):
        """LLM 返回被 ```markdown 包裹的报告时自动清理"""
        from agent_service.routes import _run_drafter

        mock_report = "```markdown\n# 报告标题\n\n正文内容\n```"
        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = AsyncMock(return_value=mock_report)

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        # 应该剥离 ```markdown 包裹
        report = result["report_markdown"]
        assert not report.startswith("```")
        assert not report.endswith("```")
        assert report.startswith("# 报告标题")

    async def test_llm_path_strips_plain_code_fence(self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output):
        """LLM 返回被 ``` 包裹的报告时自动清理"""
        from agent_service.routes import _run_drafter

        mock_report = "```\n# 报告\n正文\n```"
        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = AsyncMock(return_value=mock_report)

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        report = result["report_markdown"]
        assert not report.startswith("```")
        assert "# 报告" in report


# ============================================================
# LLM 降级路径
# ============================================================
class TestDrafterLLMFallback:
    """LLM 失败时降级到规则版"""

    async def test_fallback_when_llm_not_configured(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        """flag 启用但 LLM 未配置 → 降级"""
        from agent_service.routes import _run_drafter

        mock_client = MagicMock()
        mock_client.is_configured = False

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        assert "llm-not-configured" in result["ai_model"]
        assert "llm_fallback_reason" in result
        # 规则版兜底
        assert result["report_format"] == "ICH-Q9-template-v1"
        assert result["status"] == "completed"

    async def test_fallback_when_llm_raises_config_error(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        from agent_service.routes import _run_drafter
        from shared.llm_client import LLMConfigError

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = AsyncMock(side_effect=LLMConfigError("missing api key"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        assert "llm-fallback" in result["ai_model"]
        assert "missing api key" in result["llm_fallback_reason"]
        # 规则版兜底
        assert result["report_format"] == "ICH-Q9-template-v1"

    async def test_fallback_when_llm_raises_response_error(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        from agent_service.routes import _run_drafter
        from shared.llm_client import LLMResponseError

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = AsyncMock(side_effect=LLMResponseError("HTTP 500"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        assert "llm-fallback" in result["ai_model"]
        assert "HTTP 500" in result["llm_fallback_reason"]

    async def test_fallback_when_llm_raises_unknown_exception(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        """flag 启用但 LLM 抛未知异常 → 兜底降级，不阻塞流水线"""
        from agent_service.routes import _run_drafter

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = AsyncMock(side_effect=RuntimeError("网络断开"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        assert "llm-error" in result["ai_model"]
        assert "网络断开" in result["llm_fallback_reason"]
        # 关键：流水线不阻塞
        assert result["status"] == "completed"
        # 规则版兜底报告仍可用
        assert "report_markdown" in result


# ============================================================
# LLM 报告内容质量测试
# ============================================================
class TestDrafterLLMReportQuality:
    """LLM 生成的报告内容质量校验"""

    async def test_llm_report_contains_ich_q9_required_sections(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        """LLM 报告包含 ICH Q9 必需章节"""
        from agent_service.routes import _run_drafter

        mock_report = """# 偏差调查报告

## 1. 偏差描述
描述内容

## 5. 根本原因分析
5 Whys 分析

## 6. 根本原因结论
根因结论

## 8. CAPA 建议
- 立即措施
- 短期措施

## 9. 风险评估
残余风险等级：Low

## 10. 合规声明
遵循 ICH Q9
"""
        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = AsyncMock(return_value=mock_report)

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        report = result["report_markdown"]
        # 必需章节
        assert "偏差描述" in report
        assert "根本原因分析" in report
        assert "根本原因结论" in report
        assert "CAPA" in report
        assert "风险评估" in report
        assert "合规声明" in report
        assert "ICH Q9" in report

    async def test_llm_report_evidence_citations_preserved(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        """LLM 报告保留证据引用"""
        from agent_service.routes import _run_drafter

        mock_report = """# 报告
## 根因分析
- EMP-2187 资质逾期 [E-DRF-01]
- AHU-7 差压异常 [E-DRF-02]
"""
        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_completion = AsyncMock(return_value=mock_report)

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        report = result["report_markdown"]
        assert "E-DRF-01" in report
        assert "E-DRF-02" in report

    async def test_llm_report_hash_changes_with_content(
        self, test_db: AsyncSession, drafter_case, reasoner_output, skeptic_output
    ):
        """不同报告内容生成不同的 hash"""
        from agent_service.routes import _run_drafter

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"

        # 第一次调用返回报告 A
        mock_client.chat_completion = AsyncMock(return_value="# 报告 A\n内容 A")

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result_a = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        # 第二次调用返回报告 B
        mock_client.chat_completion = AsyncMock(return_value="# 报告 B\n内容 B")

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result_b = await _run_drafter(test_db, drafter_case, reasoner_output, skeptic_output)

        assert result_a["ai_output_hash"] != result_b["ai_output_hash"]
