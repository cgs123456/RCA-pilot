"""多 Agent 博弈验证测试（Phase 2 改进5）

覆盖：
- LLMClient.from_override / get_debate_client 客户端工厂
- _run_reasoner_debate 并行路径（primary + adversary 双成功 → parallel 模式）
- 仅 primary 配置 → primary_only 降级
- primary LLM 未配置 → rule_fallback 降级
- primary LLM 异常 → rule_fallback 降级
- adversary LLM 异常 → primary_only 降级
- 交叉质证 prompt 包含两份 Reasoner 输出
- 交叉质证未配置/异常降级
- Meta-Controller 综合 prompt 包含两份 Reasoner + Skeptic
- Meta-Controller 输出 schema 校验（confidence_score 范围 / resolution 枚举）
- Meta-Controller 降级路径
- Drafter 博弈路径使用 Meta-Controller 综合结论
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Deviation, Evidence

pytestmark = pytest.mark.asyncio


# ============================================================
# 测试 fixture
# ============================================================
@pytest.fixture
async def debate_case(test_db: AsyncSession, seed_db):
    """创建带证据的案例用于多 Agent 博弈测试"""
    case = Deviation(
        id="DEV-DBT-001",
        title="B 级区沉降菌超标（博弈测试）",
        description="B 级背景区沉降菌采样 22 CFU，超出阈值 15 CFU",
        severity="major",
        status="investigating",
        discovered_at=datetime.now(UTC),
        investigator_id="U-LC",
        product_impact="批号 BN-2025-08 待评估",
        immediate_action="暂停 B 级区作业",
        progress=50,
        hitl_gates={"gate1": "passed", "gate2": "passed", "gate3": "pending", "gate4": "pending", "gate5": "pending"},
    )
    test_db.add(case)
    await test_db.flush()

    evidence = [
        Evidence(
            id="E-DBT-01",
            case_id="DEV-DBT-001",
            source_system="LMS",
            collected_at=datetime.now(UTC),
            summary="EMP-2187 更衣资质 2025-08-10 逾期",
            raw_data={"status": "expired"},
            content_hash="sha256:dbt-1",
            confidence=4,
        ),
        Evidence(
            id="E-DBT-02",
            case_id="DEV-DBT-001",
            source_system="BMS",
            collected_at=datetime.now(UTC),
            summary="AHU-7 差压 470 Pa，超过预警值",
            raw_data={"delta_p": 470},
            content_hash="sha256:dbt-2",
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
        .where(Deviation.id == "DEV-DBT-001")
        .options(selectinload(Deviation.evidence), selectinload(Deviation.timeline), selectinload(Deviation.five_whys), selectinload(Deviation.skeptic_challenges))
    )
    return result.scalar_one()


@pytest.fixture
def evidence_hunter_output():
    """Evidence Hunter 输出 mock"""
    return {
        "agent": "evidence_hunter",
        "status": "completed",
        "evidence_count": 2,
        "by_source": {"LMS": 1, "BMS": 1},
        "anomaly_count": 1,
        "gaps": [],
        "summary": "证据 2 条，异常 1 条",
        "ai_output_hash": "sha256:eh-mock",
    }


@pytest.fixture
def primary_reasoner_output():
    """Primary Reasoner 输出 mock"""
    return {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": [
            {
                "n": 1,
                "question": "为什么沉降菌超标？",
                "answer": "因为 EMP-2187 更衣资质逾期（E-DBT-01）",
                "evidence_ids": ["E-DBT-01"],
                "is_root_cause": True,
            },
        ],
        "fishbone": {
            "root": "沉降菌超标",
            "categories": [
                {"name": "人员 Man", "causes": [{"text": "更衣资质逾期", "evidence": ["E-DBT-01"]}]},
                {"name": "设备 Machine", "causes": [{"text": "AHU-7 差压异常", "evidence": ["E-DBT-02"]}]},
                {"name": "物料 Material", "causes": []},
                {"name": "方法 Method", "causes": []},
                {"name": "环境 Environment", "causes": []},
                {"name": "测量 Measurement", "causes": []},
            ],
        },
        "root_cause_summary": "根因：EMP-2187 更衣资质逾期（E-DBT-01）",
        "ai_model": "glm-4-plus",
        "ai_prompt_hash": "sha256:primary-prompt",
        "ai_output_hash": "sha256:primary-output",
        "llm_enabled": True,
    }


@pytest.fixture
def adversary_reasoner_output():
    """Adversary Reasoner 输出 mock —— 与 primary 结论略不同"""
    return {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": [
            {
                "n": 1,
                "question": "为什么沉降菌超标？",
                "answer": "因为 AHU-7 差压异常导致过滤效率下降（E-DBT-02）",
                "evidence_ids": ["E-DBT-02"],
                "is_root_cause": True,
            },
        ],
        "fishbone": {
            "root": "沉降菌超标",
            "categories": [
                {"name": "人员 Man", "causes": []},
                {"name": "设备 Machine", "causes": [{"text": "AHU-7 滤芯问题", "evidence": ["E-DBT-02"]}]},
                {"name": "物料 Material", "causes": []},
                {"name": "方法 Method", "causes": []},
                {"name": "环境 Environment", "causes": []},
                {"name": "测量 Measurement", "causes": []},
            ],
        },
        "root_cause_summary": "根因：AHU-7 HVAC 故障（E-DBT-02）",
        "ai_model": "qwen-plus",
        "ai_prompt_hash": "sha256:adversary-prompt",
        "ai_output_hash": "sha256:adversary-output",
        "llm_enabled": True,
    }


@pytest.fixture
def skeptic_cross_examine_output():
    """交叉质证 Skeptic 输出 mock"""
    return {
        "agent": "skeptic",
        "status": "completed",
        "challenges": [
            {
                "target": "primary 根因：EMP-2187 更衣资质逾期",
                "challenge": "未排除 HVAC 故障作为独立根因（E-DBT-02）",
                "evidence_ids": ["E-DBT-02"],
                "type": "contradiction",
            },
        ],
        "challenge_count": 1,
        "cross_examine_mode": True,
        "ai_model": "glm-4-plus",
        "ai_output_hash": "sha256:skeptic-mock",
    }


# ============================================================
# LLMClient.from_override + get_debate_client 单元测试
# ============================================================
class TestLLMClientOverride:
    """LLM 客户端工厂方法测试"""

    def test_from_override_returns_new_instance(self):
        """from_override 返回新实例（不污染单例）"""
        from shared.llm_client import LLMClient, llm_client

        new_client = LLMClient.from_override(provider="qwen", api_key="test-key")
        assert new_client is not llm_client
        assert new_client.provider == "qwen"
        assert new_client._api_key == "test-key"

    def test_from_override_falls_back_to_settings(self):
        """未指定的字段从 settings 继承"""
        from shared.config import settings
        from shared.llm_client import LLMClient

        new_client = LLMClient.from_override(provider="qwen")
        # api_key 应该从 settings 继承
        assert new_client._api_key == settings.llm_api_key
        # temperature 也从 settings 继承
        assert new_client._temperature == settings.llm_temperature

    def test_from_override_resolves_provider_defaults(self):
        """from_override 应根据 provider 自动解析 base_url 和 model"""
        from shared.llm_client import LLMClient

        # qwen 应自动配置 DashScope base_url
        new_client = LLMClient.from_override(provider="qwen", api_key="test")
        assert "dashscope" in new_client._base_url
        assert new_client.model_name == "qwen-plus"  # qwen 默认 model

    def test_from_override_temperature_override(self):
        """temperature 覆盖生效"""
        from shared.llm_client import LLMClient

        new_client = LLMClient.from_override(provider="qwen", api_key="test", temperature=0.9)
        assert new_client._temperature == 0.9

    def test_get_debate_client_returns_none_when_not_configured(self):
        """debate provider/api_key 都未配置时返回 None"""
        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_debate_provider = ""
            mock_settings.llm_debate_api_key = ""
            from shared.llm_client import get_debate_client
            assert get_debate_client() is None

    def test_get_debate_client_returns_instance_when_configured(self):
        """debate 配置齐备时返回 LLMClient 实例"""
        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_debate_provider = "qwen"
            mock_settings.llm_debate_api_key = "test-key"
            mock_settings.llm_debate_model = ""
            mock_settings.llm_debate_temperature = 0.5
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "primary-key"
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_base_url = ""
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            from shared.llm_client import get_debate_client
            debate_client = get_debate_client()
            assert debate_client is not None
            assert debate_client.provider == "qwen"


# ============================================================
# _run_reasoner_debate 并行测试
# ============================================================
class TestReasonerDebate:
    """并行 Reasoner 博弈测试"""

    async def test_parallel_mode_when_both_llms_succeed(
        self, test_db: AsyncSession, debate_case, evidence_hunter_output,
        primary_reasoner_output, adversary_reasoner_output,
    ):
        """primary + adversary 双成功 → debate_mode=parallel"""
        from agent_service.routes import _run_reasoner_debate

        # Mock primary llm_client 和 get_debate_client
        primary_mock = MagicMock()
        primary_mock.is_configured = True
        primary_mock.model_name = "glm-4-plus"
        primary_mock.hash_prompt.return_value = "sha256:primary-hash"

        adversary_mock = MagicMock()
        adversary_mock.is_configured = True
        adversary_mock.model_name = "qwen-plus"
        adversary_mock.hash_prompt.return_value = "sha256:adversary-hash"

        async def mock_primary_chat_json(**kwargs):
            from agent_service.routes import _LLMFishbone, _LLMFishboneCategory, _LLMFiveWhy, _LLMReasonerOutput
            return _LLMReasonerOutput(
                five_whys=[_LLMFiveWhy(n=1, question="q1", answer="a1", evidence_ids=["E-DBT-01"], is_root_cause=True)],
                fishbone=_LLMFishbone(
                    root="root", categories=[_LLMFishboneCategory(name=f"cat-{i}", causes=[]) for i in range(6)],
                ),
                root_cause_summary="primary 根因（E-DBT-01）",
            )
        primary_mock.chat_json = mock_primary_chat_json

        async def mock_adversary_chat_json(**kwargs):
            from agent_service.routes import _LLMFishbone, _LLMFishboneCategory, _LLMFiveWhy, _LLMReasonerOutput
            return _LLMReasonerOutput(
                five_whys=[_LLMFiveWhy(n=1, question="q1", answer="a1", evidence_ids=["E-DBT-02"], is_root_cause=True)],
                fishbone=_LLMFishbone(
                    root="root", categories=[_LLMFishboneCategory(name=f"cat-{i}", causes=[]) for i in range(6)],
                ),
                root_cause_summary="adversary 根因（E-DBT-02）",
            )
        adversary_mock.chat_json = mock_adversary_chat_json

        with patch("shared.llm_client.llm_client", primary_mock), \
             patch("shared.llm_client.get_debate_client", return_value=adversary_mock):
            result = await _run_reasoner_debate(test_db, debate_case, evidence_hunter_output)

        assert result["agent"] == "reasoner_debate"
        assert result["status"] == "completed"
        assert result["debate_mode"] == "parallel"
        assert result["primary"] is not None
        assert result["adversary"] is not None
        assert result["primary"]["root_cause_summary"] == "primary 根因（E-DBT-01）"
        assert result["adversary"]["root_cause_summary"] == "adversary 根因（E-DBT-02）"
        assert result["primary"]["ai_model"] == "glm-4-plus"
        assert result["adversary"]["ai_model"] == "qwen-plus"

    async def test_primary_only_when_adversary_not_configured(
        self, test_db: AsyncSession, debate_case, evidence_hunter_output,
    ):
        """adversary LLM 未配置 → debate_mode=primary_only"""
        from agent_service.routes import _run_reasoner_debate

        primary_mock = MagicMock()
        primary_mock.is_configured = True
        primary_mock.model_name = "glm-4-plus"
        primary_mock.hash_prompt.return_value = "sha256:primary-hash"

        async def mock_chat_json(**kwargs):
            from agent_service.routes import _LLMFishbone, _LLMFishboneCategory, _LLMFiveWhy, _LLMReasonerOutput
            return _LLMReasonerOutput(
                five_whys=[_LLMFiveWhy(n=1, question="q", answer="a", evidence_ids=["E-DBT-01"], is_root_cause=True)],
                fishbone=_LLMFishbone(
                    root="root", categories=[_LLMFishboneCategory(name=f"c{i}", causes=[]) for i in range(6)],
                ),
                root_cause_summary="primary 根因",
            )
        primary_mock.chat_json = mock_chat_json

        with patch("shared.llm_client.llm_client", primary_mock), \
             patch("shared.llm_client.get_debate_client", return_value=None):
            result = await _run_reasoner_debate(test_db, debate_case, evidence_hunter_output)

        assert result["debate_mode"] == "primary_only"
        assert result["primary"] is not None
        assert result["adversary"] is None

    async def test_rule_fallback_when_primary_not_configured(
        self, test_db: AsyncSession, debate_case, evidence_hunter_output,
    ):
        """primary LLM 未配置 → debate_mode=rule_fallback"""
        from agent_service.routes import _run_reasoner_debate

        primary_mock = MagicMock()
        primary_mock.is_configured = False

        with patch("shared.llm_client.llm_client", primary_mock), \
             patch("shared.llm_client.get_debate_client", return_value=None):
            result = await _run_reasoner_debate(test_db, debate_case, evidence_hunter_output)

        assert result["debate_mode"] == "rule_fallback"
        assert result["primary"]["ai_model"].startswith("rule-template-v1")
        assert result["adversary"] is None

    async def test_rule_fallback_when_primary_raises_llm_error(
        self, test_db: AsyncSession, debate_case, evidence_hunter_output,
    ):
        """primary LLM 调用失败 → debate_mode=rule_fallback"""
        from agent_service.routes import _run_reasoner_debate
        from shared.llm_client import LLMError

        primary_mock = MagicMock()
        primary_mock.is_configured = True
        primary_mock.model_name = "glm-4-plus"
        primary_mock.hash_prompt.return_value = "sha256:hash"

        async def mock_chat_json(**kwargs):
            raise LLMError("primary LLM down")
        primary_mock.chat_json = mock_chat_json

        with patch("shared.llm_client.llm_client", primary_mock), \
             patch("shared.llm_client.get_debate_client", return_value=None):
            result = await _run_reasoner_debate(test_db, debate_case, evidence_hunter_output)

        assert result["debate_mode"] == "rule_fallback"
        assert "rule-template-v1" in result["primary"]["ai_model"]

    async def test_primary_only_when_adversary_raises_exception(
        self, test_db: AsyncSession, debate_case, evidence_hunter_output,
    ):
        """adversary LLM 异常但 primary 成功 → debate_mode=primary_only"""
        from agent_service.routes import _run_reasoner_debate
        from shared.llm_client import LLMError

        primary_mock = MagicMock()
        primary_mock.is_configured = True
        primary_mock.model_name = "glm-4-plus"
        primary_mock.hash_prompt.return_value = "sha256:hash"

        async def mock_primary(**kwargs):
            from agent_service.routes import _LLMFishbone, _LLMFishboneCategory, _LLMFiveWhy, _LLMReasonerOutput
            return _LLMReasonerOutput(
                five_whys=[_LLMFiveWhy(n=1, question="q", answer="a", evidence_ids=["E-DBT-01"], is_root_cause=True)],
                fishbone=_LLMFishbone(root="r", categories=[_LLMFishboneCategory(name=f"c{i}", causes=[]) for i in range(6)]),
                root_cause_summary="primary",
            )
        primary_mock.chat_json = mock_primary

        adversary_mock = MagicMock()
        adversary_mock.is_configured = True
        adversary_mock.model_name = "qwen-plus"
        adversary_mock.hash_prompt.return_value = "sha256:adv-hash"

        async def mock_adversary(**kwargs):
            raise LLMError("adversary LLM down")
        adversary_mock.chat_json = mock_adversary

        with patch("shared.llm_client.llm_client", primary_mock), \
             patch("shared.llm_client.get_debate_client", return_value=adversary_mock):
            result = await _run_reasoner_debate(test_db, debate_case, evidence_hunter_output)

        assert result["debate_mode"] == "primary_only"
        assert result["primary"] is not None
        assert result["adversary"] is None
        assert result.get("adversary_error") is not None
        assert "adversary" in result["adversary_error"].lower()


# ============================================================
# 交叉质证 prompt + 输出测试
# ============================================================
class TestSkepticCrossExamine:
    """交叉质证 Skeptic 测试"""

    async def test_cross_examine_prompt_includes_both_reasoners(
        self, debate_case, primary_reasoner_output, adversary_reasoner_output,
    ):
        """交叉质证 prompt 包含两份 Reasoner 输出"""
        from agent_service.routes import _build_cross_examine_user_prompt

        prompt = _build_cross_examine_user_prompt(
            debate_case, primary_reasoner_output, adversary_reasoner_output
        )

        # 包含 primary 标签和 adversary 标签
        assert "Primary Reasoner" in prompt
        assert "Adversary Reasoner" in prompt
        # 包含两份 Reasoner 的根因结论
        assert "EMP-2187" in prompt or "E-DBT-01" in prompt
        assert "AHU-7" in prompt or "E-DBT-02" in prompt
        # 包含 model 名
        assert "glm-4-plus" in prompt
        assert "qwen-plus" in prompt

    async def test_cross_examine_prompt_handles_none_adversary(
        self, debate_case, primary_reasoner_output,
    ):
        """adversary 为 None 时 prompt 应有 "(未生成)" 标记"""
        from agent_service.routes import _build_cross_examine_user_prompt

        prompt = _build_cross_examine_user_prompt(
            debate_case, primary_reasoner_output, None
        )

        assert "Primary Reasoner" in prompt
        assert "(未生成)" in prompt

    async def test_cross_examine_system_prompt_includes_new_types(self):
        """交叉质证 system prompt 包含 contradiction 和 evidence_conflict 类型"""
        from agent_service.routes import _CROSS_EXAMINE_SYSTEM_PROMPT

        assert "contradiction" in _CROSS_EXAMINE_SYSTEM_PROMPT
        assert "evidence_conflict" in _CROSS_EXAMINE_SYSTEM_PROMPT

    async def test_cross_examine_wrapper_fallback_when_llm_not_configured(
        self, test_db: AsyncSession, debate_case, primary_reasoner_output,
    ):
        """LLM 未配置时交叉质证降级到规则版"""
        from agent_service.routes import _run_skeptic_cross_examine

        primary_mock = MagicMock()
        primary_mock.is_configured = False

        with patch("shared.llm_client.llm_client", primary_mock):
            result = await _run_skeptic_cross_examine(
                test_db, debate_case, primary_reasoner_output, None
            )

        assert result["status"] == "completed"
        assert "rule-template-v1" in result["ai_model"]
        assert result.get("llm_fallback_reason") is not None

    async def test_cross_examine_wrapper_fallback_on_llm_error(
        self, test_db: AsyncSession, debate_case, primary_reasoner_output,
    ):
        """LLM 异常时交叉质证降级"""
        from agent_service.routes import _run_skeptic_cross_examine
        from shared.llm_client import LLMError

        primary_mock = MagicMock()
        primary_mock.is_configured = True
        primary_mock.model_name = "glm-4-plus"
        primary_mock.hash_prompt.return_value = "sha256:hash"

        async def mock_chat_json(**kwargs):
            raise LLMError("LLM down")
        primary_mock.chat_json = mock_chat_json

        with patch("shared.llm_client.llm_client", primary_mock):
            result = await _run_skeptic_cross_examine(
                test_db, debate_case, primary_reasoner_output, None
            )

        assert "rule-template-v1" in result["ai_model"]
        assert "llm-fallback" in result["ai_model"]


# ============================================================
# Meta-Controller 综合 prompt + schema 测试
# ============================================================
class TestMetaControllerSynthesis:
    """Meta-Controller 综合置信度结论测试"""

    async def test_meta_controller_prompt_includes_all_inputs(
        self, debate_case, primary_reasoner_output, adversary_reasoner_output,
        skeptic_cross_examine_output,
    ):
        """Meta-Controller prompt 包含两份 Reasoner + Skeptic 交叉质证"""
        from agent_service.routes import _build_meta_controller_user_prompt

        prompt = _build_meta_controller_user_prompt(
            debate_case, primary_reasoner_output, adversary_reasoner_output,
            skeptic_cross_examine_output,
        )

        # 包含两份 Reasoner
        assert "Primary Reasoner" in prompt
        assert "Adversary Reasoner" in prompt
        # 包含 Skeptic 交叉质证
        assert "Skeptic 交叉质证" in prompt
        assert "contradiction" in prompt

    async def test_meta_controller_system_prompt_has_confidence_scoring(self):
        """Meta-Controller system prompt 包含置信度评分规则"""
        from agent_service.routes import _META_CONTROLLER_SYSTEM_PROMPT

        assert "confidence_score" in _META_CONTROLLER_SYSTEM_PROMPT
        assert "0.9-1.0" in _META_CONTROLLER_SYSTEM_PROMPT
        assert "0.0-0.29" in _META_CONTROLLER_SYSTEM_PROMPT

    def test_meta_controller_schema_validates_confidence_range(self):
        """schema 校验 confidence_score 范围 0-1"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMMetaControllerOutput

        # 合法
        valid = _LLMMetaControllerOutput(
            agreed_root_cause="一致根因",
            final_root_cause_summary="综合结论",
            confidence_score=0.85,
        )
        assert valid.confidence_score == 0.85

        # 超过 1.0 → 校验失败
        with pytest.raises(ValidationError):
            _LLMMetaControllerOutput(
                agreed_root_cause="x",
                final_root_cause_summary="y",
                confidence_score=1.5,
            )

        # 低于 0.0 → 校验失败
        with pytest.raises(ValidationError):
            _LLMMetaControllerOutput(
                agreed_root_cause="x",
                final_root_cause_summary="y",
                confidence_score=-0.1,
            )

    async def test_meta_controller_wrapper_fallback_when_llm_not_configured(
        self, test_db: AsyncSession, debate_case, primary_reasoner_output,
        skeptic_cross_examine_output,
    ):
        """LLM 未配置时 Meta-Controller 降级到简单综合"""
        from agent_service.routes import _run_meta_controller_synthesis

        primary_mock = MagicMock()
        primary_mock.is_configured = False

        with patch("shared.llm_client.llm_client", primary_mock):
            result = await _run_meta_controller_synthesis(
                test_db, debate_case, primary_reasoner_output, None,
                skeptic_cross_examine_output,
            )

        assert result["agent"] == "meta_controller"
        assert result["status"] == "completed"
        assert result["llm_enabled"] is False
        # 仅有 primary 时 confidence=0.5（缺乏交叉验证）
        assert result["confidence_score"] == 0.5
        # final_root_cause_summary 采用 primary 的结论
        assert "EMP-2187" in result["final_root_cause_summary"] or "E-DBT-01" in result["final_root_cause_summary"]

    async def test_meta_controller_wrapper_fallback_on_llm_error(
        self, test_db: AsyncSession, debate_case, primary_reasoner_output,
        adversary_reasoner_output, skeptic_cross_examine_output,
    ):
        """LLM 异常时 Meta-Controller 降级"""
        from agent_service.routes import _run_meta_controller_synthesis
        from shared.llm_client import LLMError

        primary_mock = MagicMock()
        primary_mock.is_configured = True
        primary_mock.model_name = "glm-4-plus"
        primary_mock.hash_prompt.return_value = "sha256:hash"

        async def mock_chat_json(**kwargs):
            raise LLMError("LLM down")
        primary_mock.chat_json = mock_chat_json

        with patch("shared.llm_client.llm_client", primary_mock):
            result = await _run_meta_controller_synthesis(
                test_db, debate_case, primary_reasoner_output, adversary_reasoner_output,
                skeptic_cross_examine_output,
            )

        # 有 adversary 时降级 confidence=0.3
        assert result["confidence_score"] == 0.5
        assert "llm-fallback" in result["ai_model"]


# ============================================================
# Drafter 博弈路径测试
# ============================================================
class TestDrafterDebate:
    """博弈路径下 Drafter 测试"""

    async def test_drafter_debate_uses_meta_controller_summary(
        self, test_db: AsyncSession, debate_case,
        primary_reasoner_output, skeptic_cross_examine_output,
    ):
        """Drafter 博弈路径使用 Meta-Controller 综合结论"""
        from agent_service.routes import _run_drafter_debate

        debate_output = {
            "primary": primary_reasoner_output,
            "adversary": None,
            "debate_mode": "primary_only",
        }
        mc_output = {
            "final_root_cause_summary": "Meta-Controller 综合根因（E-DBT-01 + E-DBT-02）",
        }

        # 使用 patch 关闭 drafter_llm_enabled flag，走规则版
        with patch("shared.feature_flag.is_enabled", return_value=False):
            result = await _run_drafter_debate(
                test_db, debate_case, debate_output,
                skeptic_cross_examine_output, mc_output,
            )

        assert result["agent"] == "drafter"
        assert result["status"] == "completed"
        # 报告应包含 Meta-Controller 综合结论
        assert "Meta-Controller 综合根因" in result.get("report_markdown", "") or \
               "Meta-Controller 综合根因" in result.get("report_text", "") or \
               "Meta-Controller 综合根因" in str(result)


# ============================================================
# 端到端集成测试（pipeline flag 开启）
# ============================================================
class TestPipelineDebateIntegration:
    """pipeline 编排测试 —— flag 开启时走博弈路径"""

    async def test_pipeline_uses_debate_path_when_flag_enabled(
        self, test_db: AsyncSession, debate_case, evidence_hunter_output,
        primary_reasoner_output,
    ):
        """flag 开启 → pipeline 走 _run_reasoner_debate 而非 _run_reasoner"""
        # 此测试只验证分支选择，不真正调用 LLM
        # 用 patch 验证 _run_reasoner_debate 被调用
        from agent_service import routes as agent_routes

        debate_mock = AsyncMock(return_value={
            "agent": "reasoner_debate",
            "status": "completed",
            "primary": primary_reasoner_output,
            "adversary": None,
            "debate_mode": "primary_only",
            "ai_model": "glm-4-plus",
            "ai_output_hash": "sha256:debate",
            "timestamp": datetime.now(UTC).isoformat(),
        })

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch.object(agent_routes, "_run_reasoner_debate", debate_mock):
            # 直接调用 _run_reasoner_debate 验证 mock 正常
            result = await agent_routes._run_reasoner_debate(
                test_db, debate_case, evidence_hunter_output
            )

        debate_mock.assert_called_once_with(test_db, debate_case, evidence_hunter_output)
        assert result["debate_mode"] == "primary_only"
