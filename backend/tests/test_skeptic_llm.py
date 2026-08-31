"""Skeptic LLM 化测试

覆盖：
- 规则版输出（flag 关闭时）
- 规则版 v2 增强（flag 关闭但调用 _build_rule_based_skeptic_output(use_v2=True)）
- LLM 路径成功（flag 开启 + mock LLM）
- LLM 路径 prompt 包含案例上下文 + Reasoner 输出
- LLM 未配置降级
- LLM 抛异常降级（LLMError / 未知异常）
- LLM 输出 Pydantic schema 校验（合法 / challenges 空 / type 非法 / 字段缺失）
"""
from __future__ import annotations

import contextlib
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
async def skeptic_case(test_db: AsyncSession, seed_db):
    """创建带证据的案例用于 Skeptic LLM 测试"""
    case = Deviation(
        id="DEV-SKP-001",
        title="B 级区沉降菌超标",
        description="B 级背景区沉降菌采样 22 CFU，超出阈值 15 CFU",
        severity="major",
        status="investigating",
        discovered_at=datetime.now(UTC),
        investigator_id="U-LC",
        product_impact="批号 BN-2025-08 待评估",
        immediate_action="暂停 B 级区作业，启动调查",
        progress=50,
        hitl_gates={"gate1": "passed", "gate2": "passed", "gate3": "pending", "gate4": "pending", "gate5": "pending"},
    )
    test_db.add(case)
    await test_db.flush()

    evidence = [
        Evidence(
            id="E-SKP-01",
            case_id="DEV-SKP-001",
            source_system="LMS",
            collected_at=datetime.now(UTC),
            summary="EMP-2187 更衣资质 2025-08-10 逾期",
            raw_data={"status": "expired"},
            content_hash="sha256:skp-1",
            confidence=4,
        ),
        Evidence(
            id="E-SKP-02",
            case_id="DEV-SKP-001",
            source_system="BMS",
            collected_at=datetime.now(UTC),
            summary="AHU-7 差压 470 Pa，超过预警值",
            raw_data={"delta_p": 470},
            content_hash="sha256:skp-2",
            confidence=3,
        ),
        Evidence(
            id="E-SKP-03",
            case_id="DEV-SKP-001",
            source_system="CCTV",
            collected_at=datetime.now(UTC),
            summary="更衣录像显示 EMP-2187 未按规定步骤消毒",
            raw_data={"duration": 180},
            content_hash="sha256:skp-3",
            confidence=4,
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
        .where(Deviation.id == "DEV-SKP-001")
        .options(selectinload(Deviation.evidence), selectinload(Deviation.five_whys))
    )
    return result.scalar_one()


@pytest.fixture
def reasoner_output():
    """Reasoner 输出（mock）—— Skeptic 的输入"""
    return {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": [
            {
                "n": 1,
                "question": "为什么 B 级区沉降菌超标？",
                "answer": "因为 EMP-2187 更衣资质逾期仍进入 B 级区",
                "evidence_ids": ["E-SKP-01"],
                "is_root_cause": False,
            },
            {
                "n": 2,
                "question": "为什么 EMP-2187 资质逾期仍能进入？",
                "answer": "LMS 与排班系统未集成，无门控",
                "evidence_ids": ["E-SKP-01"],
                "is_root_cause": True,
            },
        ],
        "fishbone": {
            "root": "B 级区沉降菌超标",
            "categories": [
                {"name": "人员 Man", "causes": [{"text": "更衣资质逾期", "evidence": ["E-SKP-01"]}]},
                {"name": "设备 Machine", "causes": [{"text": "AHU-7 差压异常", "evidence": ["E-SKP-02"]}]},
                {"name": "物料 Material", "causes": []},
                {"name": "方法 Method", "causes": []},
                {"name": "环境 Environment", "causes": []},
                {"name": "测量 Measurement", "causes": []},
            ],
        },
        "root_cause_summary": "根因：LMS 与排班系统未集成导致 EMP-2187 资质逾期仍能进入 B 级区（E-SKP-01）",
        "ai_model": "rule-template-v1",
        "ai_output_hash": "sha256:mock-reasoner",
    }


# ============================================================
# 规则版测试
# ============================================================
class TestSkepticRuleBased:
    """规则版 Skeptic 测试 — flag 关闭时"""

    async def test_rule_based_returns_3_challenges_when_v2_off(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        from agent_service.routes import _run_skeptic

        with patch("shared.feature_flag.is_enabled", return_value=False):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        assert result["agent"] == "skeptic"
        assert result["status"] == "completed"
        assert result["challenge_count"] == 3
        assert result["ai_model"] == "rule-template-v1"
        assert result["ai_output_hash"].startswith("sha256:")
        # 规则版不返回 LLM 字段
        assert "ai_prompt_hash" not in result
        assert "llm_enabled" not in result
        assert "llm_fallback_reason" not in result

    async def test_rule_based_v2_returns_5_challenges(self, test_db: AsyncSession, skeptic_case, reasoner_output):
        """v2 flag 关闭时直接调 _build_rule_based_skeptic_output(use_v2=True) 验证 v2 路径"""
        from agent_service.routes import _build_rule_based_skeptic_output

        result = _build_rule_based_skeptic_output(skeptic_case, use_v2=True)
        assert result["challenge_count"] == 4
        # v2 包含 EMS 采样策略挑战
        types = [c["type"] for c in result["challenges"]]
        assert "evidence_gap" in types
        assert "independent_cause" in types
        assert "alternative_explanation" in types

    async def test_rule_based_challenge_types_cover_3_dimensions(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        """规则版挑战覆盖 3 种类型"""
        from agent_service.routes import _run_skeptic

        with patch("shared.feature_flag.is_enabled", return_value=False):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        types = {c["type"] for c in result["challenges"]}
        assert "alternative_explanation" in types
        assert "independent_cause" in types
        assert "evidence_gap" in types


# ============================================================
# LLM 路径成功
# ============================================================
class TestSkepticLLMPath:
    """LLM 版 Skeptic 测试 — flag 开启且 LLM 可用时走 LLM 路径"""

    async def test_llm_path_returns_valid_output(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        from agent_service.routes import _run_skeptic

        # mock LLM 返回符合 schema 的挑战
        mock_challenges = {
            "challenges": [
                {
                    "target": "根因结论：EMP-2187 是污染源",
                    "challenge": "CCTV 录像显示 EMP-2187 更衣步骤错误（E-SKP-03），但未排除其他当班人员引入污染的可能",
                    "evidence_ids": ["E-SKP-03"],
                    "type": "alternative_explanation",
                },
                {
                    "target": "根因结论：AHU-7 差压是次要因素",
                    "challenge": "AHU-7 差压 470 Pa 持续超标（E-SKP-02），可能 HVAC 故障本身就是独立根因，而非次要因素",
                    "evidence_ids": ["E-SKP-02"],
                    "type": "independent_cause",
                },
                {
                    "target": "5 Whys #2：LMS 与排班系统未集成",
                    "challenge": "未提供排班系统接口文档，无法确认 LMS 是否真的未集成，可能存在数据同步延迟",
                    "evidence_ids": ["E-SKP-01"],
                    "type": "evidence_gap",
                },
            ]
        }

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus-mock"
        mock_client.hash_prompt.return_value = "sha256:mock-skeptic-prompt"

        # 用真实 Pydantic 校验生成 mock 返回值
        from agent_service.routes import _LLMSkepticOutput
        validated = _LLMSkepticOutput.model_validate(mock_challenges)
        mock_client.chat_json = AsyncMock(return_value=validated)

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        assert result["agent"] == "skeptic"
        assert result["status"] == "completed"
        assert result["ai_model"] == "glm-4-plus-mock"
        assert result["ai_prompt_hash"] == "sha256:mock-skeptic-prompt"
        assert result["ai_output_hash"].startswith("sha256:")
        assert result["llm_enabled"] is True
        assert result["challenge_count"] == 3
        # 每条挑战有 ai_model + ai_output_hash
        for c in result["challenges"]:
            assert c["ai_model"] == "glm-4-plus-mock"
            assert c["ai_output_hash"].startswith("sha256:")
            assert c["type"] in {"alternative_explanation", "independent_cause", "evidence_gap"}

    async def test_llm_path_calls_chat_json_with_correct_schema(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        """验证 LLM 路径调用 chat_json 时传入正确的 schema 类"""
        from agent_service.routes import _LLMSkepticOutput, _run_skeptic

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "qwen-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(return_value=MagicMock())

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            with contextlib.suppress(Exception):
                await _run_skeptic(test_db, skeptic_case, reasoner_output)

            assert mock_client.chat_json.called
            _, kwargs = mock_client.chat_json.call_args
            assert kwargs["schema"] is _LLMSkepticOutput

    async def test_llm_path_prompt_includes_case_and_reasoner_context(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        """验证 prompt 包含案例 ID、证据 ID、Reasoner 的 5 Whys + 根因结论"""
        from agent_service.routes import _run_skeptic

        captured_prompt = {}

        async def mock_chat_json(system, user_prompt, schema, **kwargs):
            captured_prompt["system"] = system
            captured_prompt["user"] = user_prompt
            raise RuntimeError("mock failure to capture prompt")

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = mock_chat_json

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        # prompt 应包含案例 ID、证据 ID、Reasoner 输出
        assert "DEV-SKP-001" in captured_prompt["user"]
        assert "E-SKP-01" in captured_prompt["user"]
        assert "E-SKP-02" in captured_prompt["user"]
        assert "E-SKP-03" in captured_prompt["user"]
        # Reasoner 5 Whys 应该在 prompt 里
        assert "Why #1" in captured_prompt["user"]
        assert "Why #2" in captured_prompt["user"]
        # Reasoner 根因结论应该在 prompt 里
        assert "LMS 与排班系统未集成" in captured_prompt["user"]
        # System prompt 应包含 3 种挑战类型说明
        assert "alternative_explanation" in captured_prompt["system"]
        assert "independent_cause" in captured_prompt["system"]
        assert "evidence_gap" in captured_prompt["system"]
        # LLM 失败 → 降级
        assert "llm-error" in result["ai_model"]


# ============================================================
# LLM 降级路径
# ============================================================
class TestSkepticLLMFallback:
    """LLM 失败时降级到规则版"""

    async def test_fallback_when_llm_not_configured(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        """flag 启用但 LLM api_key 未配置 → 降级"""
        from agent_service.routes import _run_skeptic

        mock_client = MagicMock()
        mock_client.is_configured = False

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        assert "llm-not-configured" in result["ai_model"]
        assert "llm_fallback_reason" in result
        # 规则版兜底输出仍完整
        assert result["agent"] == "skeptic"
        assert result["status"] == "completed"
        # v2 flag 开启时规则版有 5 条
        assert result["challenge_count"] == 4

    async def test_fallback_when_llm_raises_config_error(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        """flag 启用但 LLM 抛 LLMConfigError → 降级"""
        from agent_service.routes import _run_skeptic
        from shared.llm_client import LLMConfigError

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(side_effect=LLMConfigError("missing api key"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        assert "llm-fallback" in result["ai_model"]
        assert "missing api key" in result["llm_fallback_reason"]
        # 规则版兜底
        assert result["agent"] == "skeptic"
        assert result["status"] == "completed"

    async def test_fallback_when_llm_raises_response_error(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        """flag 启用但 LLM 抛 LLMResponseError → 降级"""
        from agent_service.routes import _run_skeptic
        from shared.llm_client import LLMResponseError

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(
            side_effect=LLMResponseError("JSON 解析失败")
        )

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        assert "llm-fallback" in result["ai_model"]
        assert "JSON 解析失败" in result["llm_fallback_reason"]

    async def test_fallback_when_llm_raises_unknown_exception(
        self, test_db: AsyncSession, skeptic_case, reasoner_output
    ):
        """flag 启用但 LLM 抛未知异常 → 兜底降级，不阻塞流水线"""
        from agent_service.routes import _run_skeptic

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(side_effect=RuntimeError("网络断开"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_skeptic(test_db, skeptic_case, reasoner_output)

        assert "llm-error" in result["ai_model"]
        assert "网络断开" in result["llm_fallback_reason"]
        # 关键：流水线不阻塞
        assert result["status"] == "completed"


# ============================================================
# LLM 输出 schema 校验
# ============================================================
class TestSkepticLLMSchema:
    """Skeptic LLM 输出 Pydantic schema 校验测试"""

    def test_valid_output_passes_validation(self):
        """合法 LLM 输出通过 schema 校验"""
        from agent_service.routes import _LLMSkepticOutput

        valid_data = {
            "challenges": [
                {
                    "target": "根因结论",
                    "challenge": "挑战内容",
                    "evidence_ids": ["E-01"],
                    "type": "alternative_explanation",
                }
            ]
        }
        result = _LLMSkepticOutput.model_validate(valid_data)
        assert len(result.challenges) == 1
        assert result.challenges[0].type == "alternative_explanation"

    def test_empty_challenges_rejected(self):
        """challenges 为空数组时拒绝"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMSkepticOutput

        with pytest.raises(ValidationError):
            _LLMSkepticOutput.model_validate({"challenges": []})

    def test_missing_target_rejected(self):
        """target 缺失时拒绝"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMSkepticOutput

        invalid_data = {
            "challenges": [
                {
                    # target 缺失
                    "challenge": "挑战内容",
                    "evidence_ids": ["E-01"],
                    "type": "evidence_gap",
                }
            ]
        }
        with pytest.raises(ValidationError):
            _LLMSkepticOutput.model_validate(invalid_data)

    def test_empty_challenge_text_rejected(self):
        """challenge 内容为空时拒绝"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMSkepticOutput

        invalid_data = {
            "challenges": [
                {
                    "target": "根因结论",
                    "challenge": "",  # 空
                    "evidence_ids": ["E-01"],
                    "type": "evidence_gap",
                }
            ]
        }
        with pytest.raises(ValidationError):
            _LLMSkepticOutput.model_validate(invalid_data)

    def test_missing_type_rejected(self):
        """type 缺失时拒绝（schema 要求 type 字段）"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMSkepticOutput

        invalid_data = {
            "challenges": [
                {
                    "target": "根因结论",
                    "challenge": "挑战内容",
                    "evidence_ids": ["E-01"],
                    # type 缺失
                }
            ]
        }
        with pytest.raises(ValidationError):
            _LLMSkepticOutput.model_validate(invalid_data)

    def test_multiple_challenge_types_allowed(self):
        """允许混合多种挑战类型"""
        from agent_service.routes import _LLMSkepticOutput

        valid_data = {
            "challenges": [
                {"target": "t1", "challenge": "c1", "evidence_ids": ["E-01"], "type": "alternative_explanation"},
                {"target": "t2", "challenge": "c2", "evidence_ids": ["E-02"], "type": "independent_cause"},
                {"target": "t3", "challenge": "c3", "evidence_ids": ["E-03"], "type": "evidence_gap"},
            ]
        }
        result = _LLMSkepticOutput.model_validate(valid_data)
        assert len(result.challenges) == 3
        types = {c.type for c in result.challenges}
        assert types == {"alternative_explanation", "independent_cause", "evidence_gap"}
