"""Reasoner LLM 化测试

覆盖：
- LLM 客户端配置状态查询
- LLM 客户端 JSON 提取（含 ```json fenced 代码块）
- LLM 客户端 prompt hash 计算
- Reasoner 规则版输出（flag 关闭时）
- Reasoner LLM 路径（flag 开启 + mock LLM client）
- Reasoner LLM 失败降级（flag 开启但 LLM 抛异常）
- Reasoner LLM 未配置降级（flag 开启但 api_key 缺失）
- LLM 输出 Pydantic schema 校验
- 完整 LLM 路径产出符合 Agent 输出契约
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Deviation, Evidence

pytestmark = pytest.mark.asyncio


# ============================================================
# 测试 fixture
# ============================================================
@pytest.fixture
async def llm_case(test_db: AsyncSession, seed_db):
    """创建带证据 + 时间线的案例用于 LLM Reasoner 测试"""
    case = Deviation(
        id="DEV-LLM-001",
        title="B 级区沉降菌超标",
        description="B 级背景区沉降菌采样 22 CFU，超出阈值 15 CFU",
        severity="major",
        status="investigating",
        discovered_at=datetime.now(UTC),
        investigator_id="U-LC",
        product_impact="批号 BN-2025-08 待评估",
        immediate_action="暂停 B 级区作业，启动调查",
        progress=30,
        hitl_gates={"gate1": "pending", "gate2": "pending", "gate3": "pending", "gate4": "pending", "gate5": "pending"},
    )
    test_db.add(case)
    await test_db.flush()

    evidence = [
        Evidence(
            id="E-LLM-01",
            case_id="DEV-LLM-001",
            source_system="LIMS",
            collected_at=datetime.now(UTC),
            summary="B 级区沉降菌 22 CFU/4h，阈值 15 CFU",
            raw_data={"cfu": 22, "threshold": 15},
            content_hash="sha256:llm-test-1",
            confidence=4,
        ),
        Evidence(
            id="E-LLM-02",
            case_id="DEV-LLM-001",
            source_system="LMS",
            collected_at=datetime.now(UTC),
            summary="EMP-2187 更衣资质 2025-08-10 逾期",
            raw_data={"status": "expired", "expired_at": "2025-08-10"},
            content_hash="sha256:llm-test-2",
            confidence=4,
        ),
        Evidence(
            id="E-LLM-03",
            case_id="DEV-LLM-001",
            source_system="BMS",
            collected_at=datetime.now(UTC),
            summary="AHU-7 差压 470 Pa，超过预警值 400 Pa",
            raw_data={"delta_p": 470, "warning_threshold": 400},
            content_hash="sha256:llm-test-3",
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
        .where(Deviation.id == "DEV-LLM-001")
        .options(
            selectinload(Deviation.evidence),
            selectinload(Deviation.timeline),
            selectinload(Deviation.five_whys),
        )
    )
    return result.scalar_one()


@pytest.fixture
def eh_output():
    """Evidence Hunter 输出（mock）"""
    return {
        "agent": "evidence_hunter",
        "status": "completed",
        "summary": "收集 3 条证据",
        "evidence_count": 3,
        "by_source": {"LIMS": 1, "LMS": 1, "BMS": 1},
        "anomaly_count": 2,
        "gaps": [],
    }


# ============================================================
# LLM 客户端单元测试
# ============================================================
class TestLLMClientConfig:
    """LLM 客户端配置状态测试"""

    def test_is_configured_false_when_no_provider(self):
        """provider 为空时 is_configured=False"""
        from shared.llm_client import LLMClient

        client = LLMClient()
        # 默认配置下 provider="" 应该未配置
        # 注意：测试环境未设置 LLM_PROVIDER 环境变量
        # 重新实例化以读取最新配置
        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = ""
            mock_settings.llm_api_key = ""
            mock_settings.llm_base_url = ""
            client = LLMClient()
            assert client.is_configured is False

    def test_is_configured_true_when_all_set(self):
        """provider + api_key + base_url 齐备时 is_configured=True"""
        from shared.llm_client import LLMClient

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""  # 留空，让 client 从 provider 默认值填充
            mock_settings.llm_model = "claude-sonnet-4-20250514"  # 默认值，应被覆盖
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()
            assert client.is_configured is True
            assert client.provider == "glm"
            assert client.model_name == "glm-4-plus"  # provider 默认 model
            assert "open.bigmodel.cn" in client._base_url

    def test_provider_qwen_uses_dashscope_endpoint(self):
        """qwen provider 使用 DashScope OpenAI 兼容 endpoint"""
        from shared.llm_client import LLMClient

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "qwen"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "claude-sonnet-4-20250514"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()
            assert "dashscope.aliyuncs.com" in client._base_url
            assert client.model_name == "qwen-plus"

    def test_chat_completion_raises_config_error_when_not_configured(self):
        """未配置时调用 chat_completion 抛 LLMConfigError"""
        from shared.llm_client import LLMClient, LLMConfigError

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = ""
            mock_settings.llm_api_key = ""
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = ""
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()

            import asyncio
            loop = asyncio.new_event_loop()
            try:
                with pytest.raises(LLMConfigError):
                    loop.run_until_complete(client.chat_completion("sys", "user"))
            finally:
                loop.close()


class TestLLMClientJSONExtraction:
    """LLM 客户端 JSON 提取测试"""

    def test_extract_plain_json(self):
        from shared.llm_client import LLMClient

        content = '{"a": 1, "b": [2, 3]}'
        result = LLMClient._extract_json(content)
        assert '"a"' in result

    def test_extract_fenced_json_block(self):
        """处理 ```json ... ``` 代码块"""
        from shared.llm_client import LLMClient

        content = '''```json
{"five_whys": [{"n": 1}]}
```'''
        result = LLMClient._extract_json(content)
        assert result.strip() == '{"five_whys": [{"n": 1}]}'

    def test_extract_json_with_leading_text(self):
        """LLM 在 JSON 前后加文字说明时的兜底（直接返回原文让 json.loads 报错）"""
        from shared.llm_client import LLMClient

        content = '这是结果：{"a": 1}'
        result = LLMClient._extract_json(content)
        # _extract_json 不做语义分析，返回原文；由 json.loads 决定是否能解析
        assert "这是结果" in result


class TestLLMClientPromptHash:
    """LLM prompt hash 测试（用于审计 ai_prompt_hash）"""

    def test_prompt_hash_deterministic(self):
        from shared.llm_client import LLMClient

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()

            h1 = client.hash_prompt("sys", "user")
            h2 = client.hash_prompt("sys", "user")
            assert h1 == h2
            assert h1.startswith("sha256:")
            assert len(h1) == 7 + 64

    def test_prompt_hash_changes_with_input(self):
        from shared.llm_client import LLMClient

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()

            h1 = client.hash_prompt("sys-a", "user")
            h2 = client.hash_prompt("sys-b", "user")
            assert h1 != h2


# ============================================================
# Reasoner 规则版（flag 关闭）
# ============================================================
class TestReasonerRuleBased:
    """规则版 Reasoner 测试 — flag 关闭时走规则版"""

    async def test_rule_based_returns_five_whys_structure(self, test_db: AsyncSession, llm_case, eh_output):
        from agent_service.routes import _run_reasoner

        # 默认 flag 关闭（reasoner_llm_enabled.enabled=False）
        result = await _run_reasoner(test_db, llm_case, eh_output)

        assert result["agent"] == "reasoner"
        assert result["status"] == "completed"
        assert result["ai_model"] == "rule-template-v1"
        assert "ai_output_hash" in result
        assert result["ai_output_hash"].startswith("sha256:")
        # 鱼骨图 6M 分类
        assert len(result["fishbone"]["categories"]) == 6
        # 规则版不返回 ai_prompt_hash（无 LLM 调用）
        assert "ai_prompt_hash" not in result

    async def test_rule_based_no_llm_fallback_reason(self, test_db: AsyncSession, llm_case, eh_output):
        """flag 关闭时不应该有 llm_fallback_reason 字段"""
        from agent_service.routes import _run_reasoner

        result = await _run_reasoner(test_db, llm_case, eh_output)
        assert "llm_fallback_reason" not in result
        assert "llm_enabled" not in result


# ============================================================
# Reasoner LLM 路径（flag 开启 + mock LLM client）
# ============================================================
class TestReasonerLLMPath:
    """LLM 版 Reasoner 测试 — flag 开启且 LLM 可用时走 LLM 路径"""

    async def test_llm_path_returns_valid_output(self, test_db: AsyncSession, llm_case, eh_output):
        """flag 启用 + LLM 可用 → 返回 LLM 输出"""
        from agent_service.routes import _run_reasoner

        # mock LLM client 返回符合 schema 的 JSON
        mock_llm_output = {
            "five_whys": [
                {
                    "n": 1,
                    "question": "为什么 B 级区沉降菌超标？",
                    "answer": "因为 EMP-2187 更衣资质逾期，引入人员污染（E-LLM-02）",
                    "evidence_ids": ["E-LLM-02"],
                    "is_root_cause": False,
                },
                {
                    "n": 2,
                    "question": "为什么 EMP-2187 资质逾期仍能进入 B 级区？",
                    "answer": "因为 LMS 与排班系统未集成，无资质门控（E-LLM-02）",
                    "evidence_ids": ["E-LLM-02"],
                    "is_root_cause": True,
                },
            ],
            "fishbone": {
                "root": "B 级区沉降菌超标",
                "categories": [
                    {"name": "人员 Man", "causes": [{"text": "更衣资质逾期", "evidence": ["E-LLM-02"], "severity": "danger"}]},
                    {"name": "设备 Machine", "causes": [{"text": "AHU-7 差压异常", "evidence": ["E-LLM-03"], "severity": "warning"}]},
                    {"name": "物料 Material", "causes": []},
                    {"name": "方法 Method", "causes": []},
                    {"name": "环境 Environment", "causes": [{"text": "差压波动", "evidence": ["E-LLM-03"], "severity": "warning"}]},
                    {"name": "测量 Measurement", "causes": []},
                ],
            },
            "root_cause_summary": "根因：LMS 与排班系统未集成导致 EMP-2187 资质逾期仍能进入 B 级区（E-LLM-02），叠加 AHU-7 差压异常（E-LLM-03）",
        }

        # mock LLMClient 实例方法
        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus-mock"
        mock_client.hash_prompt.return_value = "sha256:mock-prompt-hash"
        mock_client.chat_json = AsyncMock(return_value=type("MockOutput", (), {
            "five_whys": [type("MockWhy", (), fw)() for fw in mock_llm_output["five_whys"]],
            "fishbone": type("MockFishbone", (), {
                "model_dump": lambda self: mock_llm_output["fishbone"]
            })(),
            "root_cause_summary": mock_llm_output["root_cause_summary"],
        })())

        # 启用 flag + mock LLM client
        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client), \
             patch("agent_service.routes._LLMReasonerOutput") as mock_schema:
            # 让 schema.model_validate 直接返回带原始字段的 mock 对象
            mock_schema.return_value = MagicMock()
            # 用真实的 Pydantic 校验逻辑
            from agent_service.routes import _LLMReasonerOutput
            validated = _LLMReasonerOutput.model_validate(mock_llm_output)
            mock_schema.model_validate.return_value = validated

            result = await _run_reasoner(test_db, llm_case, eh_output)

        assert result["agent"] == "reasoner"
        assert result["status"] == "completed"
        assert result["ai_model"] == "glm-4-plus-mock"
        assert result["ai_prompt_hash"] == "sha256:mock-prompt-hash"
        assert result["ai_output_hash"].startswith("sha256:")
        assert result["llm_enabled"] is True
        # 5 Whys 输出结构
        assert len(result["five_whys"]) == 2
        for fw in result["five_whys"]:
            assert "n" in fw
            assert "question" in fw
            assert "answer" in fw
            assert "evidence_ids" in fw
            assert "is_root_cause" in fw
            assert fw["ai_model"] == "glm-4-plus-mock"
            assert fw["ai_output_hash"].startswith("sha256:")
        # 鱼骨图 6M
        assert len(result["fishbone"]["categories"]) == 6
        # 根因结论
        assert "EMP-2187" in result["root_cause_summary"]

    async def test_llm_path_calls_chat_json_with_correct_schema(
        self, test_db: AsyncSession, llm_case, eh_output
    ):
        """验证 LLM 路径调用 chat_json 时传入正确的 schema 类"""
        from agent_service.routes import _LLMReasonerOutput, _run_reasoner

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "qwen-plus-mock"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(return_value=MagicMock())

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            with contextlib.suppress(Exception):
                await _run_reasoner(test_db, llm_case, eh_output)

            # 验证 chat_json 被调用，且第三个参数是 _LLMReasonerOutput 类
            assert mock_client.chat_json.called
            call_args = mock_client.chat_json.call_args
            args, kwargs = call_args
            assert kwargs["schema"] is _LLMReasonerOutput

    async def test_llm_path_includes_case_context_in_prompt(
        self, test_db: AsyncSession, llm_case, eh_output
    ):
        """验证 prompt 包含案例 ID、证据 ID、Evidence Hunter 摘要"""
        from agent_service.routes import _run_reasoner

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"

        # 让 chat_json 直接抛异常，从而能检查传入的 user_prompt
        captured_prompt = {}

        async def mock_chat_json(system, user_prompt, schema, **kwargs):
            captured_prompt["system"] = system
            captured_prompt["user"] = user_prompt
            raise RuntimeError("mock failure to capture prompt")

        mock_client.chat_json = mock_chat_json

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_reasoner(test_db, llm_case, eh_output)

        # 即使 LLM 失败，prompt 应该被捕获
        assert "DEV-LLM-001" in captured_prompt["user"]
        assert "E-LLM-01" in captured_prompt["user"]
        assert "E-LLM-02" in captured_prompt["user"]
        assert "E-LLM-03" in captured_prompt["user"]
        assert "Evidence Hunter" in captured_prompt["user"]
        # 失败后降级到规则版
        assert "llm-fallback" in result["ai_model"] or "llm-error" in result["ai_model"]


# ============================================================
# Reasoner LLM 降级路径
# ============================================================
class TestReasonerLLMFallback:
    """LLM 失败时降级到规则版"""

    async def test_fallback_when_llm_not_configured(
        self, test_db: AsyncSession, llm_case, eh_output
    ):
        """flag 启用但 LLM api_key 未配置 → 降级到规则版"""
        from agent_service.routes import _run_reasoner

        mock_client = MagicMock()
        mock_client.is_configured = False  # 未配置

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_reasoner(test_db, llm_case, eh_output)

        assert result["agent"] == "reasoner"
        assert "llm-not-configured" in result["ai_model"]
        assert "llm_fallback_reason" in result
        # 仍然返回完整结构（规则版输出）
        assert "five_whys" in result
        assert "fishbone" in result
        assert "root_cause_summary" in result

    async def test_fallback_when_llm_raises_config_error(
        self, test_db: AsyncSession, llm_case, eh_output
    ):
        """flag 启用但 LLM 抛 LLMConfigError → 降级"""
        from agent_service.routes import _run_reasoner
        from shared.llm_client import LLMConfigError

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(side_effect=LLMConfigError("missing api key"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_reasoner(test_db, llm_case, eh_output)

        assert "llm-fallback" in result["ai_model"]
        assert "missing api key" in result["llm_fallback_reason"]
        # 规则版兜底输出仍完整
        assert result["agent"] == "reasoner"
        assert result["status"] == "completed"
        assert len(result["fishbone"]["categories"]) == 6

    async def test_fallback_when_llm_raises_response_error(
        self, test_db: AsyncSession, llm_case, eh_output
    ):
        """flag 启用但 LLM 抛 LLMResponseError（如 JSON 解析失败）→ 降级"""
        from agent_service.routes import _run_reasoner
        from shared.llm_client import LLMResponseError

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(
            side_effect=LLMResponseError("JSON 解析失败: Expecting value")
        )

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_reasoner(test_db, llm_case, eh_output)

        assert "llm-fallback" in result["ai_model"]
        assert "JSON 解析失败" in result["llm_fallback_reason"]

    async def test_fallback_when_llm_raises_unknown_exception(
        self, test_db: AsyncSession, llm_case, eh_output
    ):
        """flag 启用但 LLM 抛未知异常 → 兜底降级，不阻塞流水线"""
        from agent_service.routes import _run_reasoner

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.model_name = "glm-4-plus"
        mock_client.hash_prompt.return_value = "sha256:mock"
        mock_client.chat_json = AsyncMock(side_effect=RuntimeError("网络断开"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_client):
            result = await _run_reasoner(test_db, llm_case, eh_output)

        assert "llm-error" in result["ai_model"]
        assert "网络断开" in result["llm_fallback_reason"]
        # 关键：流水线不阻塞，仍然返回规则版输出
        assert result["status"] == "completed"


# ============================================================
# LLM 输出 schema 校验
# ============================================================
class TestLLMReasonerSchema:
    """LLM 输出 Pydantic schema 校验测试"""

    def test_valid_llm_output_passes_validation(self):
        """合法 LLM 输出通过 schema 校验"""
        from agent_service.routes import _LLMReasonerOutput

        valid_data = {
            "five_whys": [
                {
                    "n": 1,
                    "question": "为什么？",
                    "answer": "因为...",
                    "evidence_ids": ["E-01"],
                    "is_root_cause": False,
                }
            ],
            "fishbone": {
                "root": "问题陈述",
                "categories": [
                    {"name": "人员 Man", "causes": []},
                    {"name": "设备 Machine", "causes": []},
                    {"name": "物料 Material", "causes": []},
                    {"name": "方法 Method", "causes": []},
                    {"name": "环境 Environment", "causes": []},
                    {"name": "测量 Measurement", "causes": []},
                ],
            },
            "root_cause_summary": "根因结论，引用 E-01",
        }
        result = _LLMReasonerOutput.model_validate(valid_data)
        assert len(result.five_whys) == 1
        assert result.five_whys[0].n == 1
        assert len(result.fishbone.categories) == 6

    def test_invalid_fishbone_categories_count_rejected(self):
        """鱼骨图分类数 ≠ 6 时拒绝"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMReasonerOutput

        invalid_data = {
            "five_whys": [{"n": 1, "question": "q", "answer": "a", "evidence_ids": ["E-01"]}],
            "fishbone": {
                "root": "问题",
                "categories": [  # 只有 5 个
                    {"name": "人员 Man", "causes": []},
                    {"name": "设备 Machine", "causes": []},
                    {"name": "物料 Material", "causes": []},
                    {"name": "方法 Method", "causes": []},
                    {"name": "环境 Environment", "causes": []},
                ],
            },
            "root_cause_summary": "结论",
        }
        with pytest.raises(ValidationError):
            _LLMReasonerOutput.model_validate(invalid_data)

    def test_empty_five_whys_rejected(self):
        """five_whys 为空数组时拒绝"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMReasonerOutput

        invalid_data = {
            "five_whys": [],  # 空
            "fishbone": {
                "root": "问题",
                "categories": [  # 6 个
                    {"name": "人员 Man", "causes": []},
                    {"name": "设备 Machine", "causes": []},
                    {"name": "物料 Material", "causes": []},
                    {"name": "方法 Method", "causes": []},
                    {"name": "环境 Environment", "causes": []},
                    {"name": "测量 Measurement", "causes": []},
                ],
            },
            "root_cause_summary": "结论",
        }
        with pytest.raises(ValidationError):
            _LLMReasonerOutput.model_validate(invalid_data)

    def test_missing_root_cause_summary_rejected(self):
        """root_cause_summary 缺失时拒绝"""
        from pydantic import ValidationError

        from agent_service.routes import _LLMReasonerOutput

        invalid_data = {
            "five_whys": [{"n": 1, "question": "q", "answer": "a", "evidence_ids": ["E-01"]}],
            "fishbone": {
                "root": "问题",
                "categories": [
                    {"name": "人员 Man", "causes": []},
                    {"name": "设备 Machine", "causes": []},
                    {"name": "物料 Material", "causes": []},
                    {"name": "方法 Method", "causes": []},
                    {"name": "环境 Environment", "causes": []},
                    {"name": "测量 Measurement", "causes": []},
                ],
            },
            # root_cause_summary 缺失
        }
        with pytest.raises(ValidationError):
            _LLMReasonerOutput.model_validate(invalid_data)


# ============================================================
# LLM 客户端 HTTP 路径测试（mock httpx）
# ============================================================
class TestLLMClientHTTPPath:
    """LLM 客户端 HTTP 调用路径测试 — 使用 mock httpx.AsyncClient"""

    def _make_mock_response(self, status_code: int = 200, content: str = "", json_body: dict = None):
        """构造 mock httpx.Response"""
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        if json_body is not None:
            mock_resp.json.return_value = json_body
            mock_resp.text = '{"ok": true}'
        else:
            mock_resp.text = content
        return mock_resp

    async def test_chat_completion_success(self):
        """chat_completion 成功路径 — 返回 LLM 文本"""
        from shared.llm_client import LLMClient

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 0  # 不重试，加速测试
            client = LLMClient()

            mock_http_resp = self._make_mock_response(
                status_code=200,
                json_body={"choices": [{"message": {"content": "LLM 推理结果"}}]},
            )
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client):
                result = await client.chat_completion("system prompt", "user input")

            assert result == "LLM 推理结果"
            mock_http_client.post.assert_awaited_once()
            call_args = mock_http_client.post.call_args
            url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
            assert "chat/completions" in url
            headers = call_args.kwargs.get("headers", {})
            assert headers.get("Authorization") == "Bearer test-key"
            assert headers.get("Content-Type") == "application/json"

    async def test_chat_completion_http_500_retries(self):
        """HTTP 500 触发重试"""
        from shared.llm_client import LLMClient, LLMResponseError

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()

            mock_http_resp = self._make_mock_response(status_code=500, content="Internal Server Error")
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client), \
                 patch("shared.llm_client.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(LLMResponseError):
                    await client.chat_completion("sys", "user")

            assert mock_http_client.post.await_count == 3  # 1 初始 + 2 重试

    async def test_chat_completion_http_400_no_retry(self):
        """HTTP 400（非 429）不重试，直接抛错"""
        from shared.llm_client import LLMClient, LLMResponseError

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()

            mock_http_resp = self._make_mock_response(status_code=400, content="Bad Request")
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client):
                with pytest.raises(LLMResponseError):
                    await client.chat_completion("sys", "user")

            assert mock_http_client.post.await_count == 1  # 没重试

    async def test_chat_completion_network_error_retries(self):
        """httpx.HTTPError 触发重试"""
        from shared.llm_client import LLMClient, LLMResponseError

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 1
            client = LLMClient()

            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client), \
                 patch("shared.llm_client.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(LLMResponseError):
                    await client.chat_completion("sys", "user")

            assert mock_http_client.post.await_count == 2

    async def test_chat_completion_missing_choices_raises(self):
        """响应缺 choices[0].message.content 抛 LLMResponseError"""
        from shared.llm_client import LLMClient, LLMResponseError

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 0
            client = LLMClient()

            mock_http_resp = self._make_mock_response(
                status_code=200, json_body={"error": "invalid"}
            )
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client):
                with pytest.raises(LLMResponseError):
                    await client.chat_completion("sys", "user")

    async def test_chat_completion_empty_content_raises(self):
        """choices[0].message.content 为空抛 LLMResponseError"""
        from shared.llm_client import LLMClient, LLMResponseError

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 0
            client = LLMClient()

            mock_http_resp = self._make_mock_response(
                status_code=200,
                json_body={"choices": [{"message": {"content": ""}}]},
            )
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client):
                with pytest.raises(LLMResponseError):
                    await client.chat_completion("sys", "user")


class TestLLMClientChatJSON:
    """chat_json 路径测试 — schema 校验"""

    async def test_chat_json_valid_schema(self):
        """chat_json 成功路径 — 返回 Pydantic 实例"""
        from pydantic import BaseModel as PydBaseModel

        from shared.llm_client import LLMClient

        class TestSchema(PydBaseModel):
            answer: str
            score: int

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 0
            client = LLMClient()

            json_content = '{"answer": "test", "score": 42}'
            with patch.object(client, "chat_completion", new=AsyncMock(return_value=json_content)):
                result = await client.chat_json("sys", "user", schema=TestSchema)

            assert isinstance(result, TestSchema)
            assert result.answer == "test"
            assert result.score == 42

    async def test_chat_json_invalid_json_raises(self):
        """chat_json 收到非 JSON 抛 LLMResponseError"""
        from pydantic import BaseModel as PydBaseModel

        from shared.llm_client import LLMClient, LLMResponseError

        class TestSchema(PydBaseModel):
            answer: str

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 0
            client = LLMClient()

            with patch.object(client, "chat_completion", new=AsyncMock(return_value="not a json")):
                with pytest.raises(LLMResponseError):
                    await client.chat_json("sys", "user", schema=TestSchema)

    async def test_chat_json_schema_validation_fails(self):
        """chat_json schema 校验失败抛 LLMResponseError"""
        from pydantic import BaseModel as PydBaseModel

        from shared.llm_client import LLMClient, LLMResponseError

        class TestSchema(PydBaseModel):
            answer: str
            score: int

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "glm"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "glm-4-plus"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 0
            client = LLMClient()

            bad_json = '{"answer": "test", "score": "not-int"}'
            with patch.object(client, "chat_completion", new=AsyncMock(return_value=bad_json)):
                with pytest.raises(LLMResponseError):
                    await client.chat_json("sys", "user", schema=TestSchema)


class TestLLMClientHeaders:
    """_build_headers 测试 — 各 provider 的鉴权头差异"""

    def _make_client(self, provider: str):
        from shared.llm_client import LLMClient

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = provider
            mock_settings.llm_api_key = "test-key-12345"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "claude-sonnet-4-20250514"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            return LLMClient()

    def test_glm_uses_bearer_auth(self):
        client = self._make_client("glm")
        headers = client._build_headers(None)
        assert headers["Authorization"] == "Bearer test-key-12345"
        assert headers["Content-Type"] == "application/json"
        assert "anthropic-version" not in headers

    def test_qwen_uses_bearer_auth(self):
        client = self._make_client("qwen")
        headers = client._build_headers(None)
        assert headers["Authorization"] == "Bearer test-key-12345"

    def test_openai_uses_bearer_auth(self):
        client = self._make_client("openai")
        headers = client._build_headers(None)
        assert headers["Authorization"] == "Bearer test-key-12345"

    def test_anthropic_uses_api_key_header(self):
        client = self._make_client("anthropic")
        headers = client._build_headers(None)
        assert headers["x-api-key"] == "test-key-12345"
        assert "anthropic-version" in headers
        assert headers["anthropic-version"] == "2023-06-01"

    def test_azure_uses_api_key_header(self):
        client = self._make_client("azure")
        client._base_url = "https://test.openai.azure.com"
        headers = client._build_headers(None)
        assert headers["api-key"] == "test-key-12345"

    def test_extra_headers_merged(self):
        client = self._make_client("glm")
        headers = client._build_headers({"X-Trace-Id": "abc-123"})
        assert headers["X-Trace-Id"] == "abc-123"
        assert headers["Authorization"] == "Bearer test-key-12345"


class TestGetLLMClient:
    """get_llm_client 单例测试"""

    def test_get_llm_client_returns_instance(self):
        from shared.llm_client import LLMClient, get_llm_client

        client = get_llm_client()
        assert isinstance(client, LLMClient)
