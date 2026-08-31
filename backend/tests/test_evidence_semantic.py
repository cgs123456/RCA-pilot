"""Evidence Hunter 语义检索 + 跨案例关联测试

覆盖：
- vector_store: cosine_similarity / vector_search / keyword_search / search_evidence / find_related_cases
- llm_client embedding: embed_text / embed_texts / supports_embeddings / 配置降级
- evidence_service: embed_evidence / embed_batch / semantic_search / related-cases
- _run_evidence_hunter: flag 关闭（无 related_cases）+ flag 开启（有 related_cases + keyword 降级）
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
async def semantic_case(test_db: AsyncSession, seed_db):
    """创建带证据的案例 + 一个历史案例（用于跨案例关联测试）"""
    # 当前案例
    current_case = Deviation(
        id="DEV-SEM-001",
        title="B 级区沉降菌超标",
        description="B 级背景区沉降菌采样 22 CFU",
        severity="major",
        status="investigating",
        discovered_at=datetime.now(UTC),
        investigator_id="U-LC",
        progress=20,
        hitl_gates={"gate1": "pending", "gate2": "pending", "gate3": "pending", "gate4": "pending", "gate5": "pending"},
    )
    # 历史案例（应被关联出来）
    historical_case = Deviation(
        id="DEV-HIST-001",
        title="B 级区微生物污染历史事件",
        description="2024 年类似 HVAC 差压异常导致沉降菌超标",
        severity="major",
        status="closed",
        discovered_at=datetime(2024, 8, 15, tzinfo=UTC),
        investigator_id="U-OLD",
        progress=100,
        hitl_gates={"gate1": "passed", "gate2": "passed", "gate3": "passed", "gate4": "passed", "gate5": "passed"},
    )
    test_db.add_all([current_case, historical_case])
    await test_db.flush()

    # 当前案例的证据
    current_evidence = [
        Evidence(
            id="E-SEM-01",
            case_id="DEV-SEM-001",
            source_system="LMS",
            collected_at=datetime.now(UTC),
            summary="EMP-2187 更衣资质逾期",
            raw_data={},
            content_hash="sha256:sem-1",
            confidence=4,
        ),
        Evidence(
            id="E-SEM-02",
            case_id="DEV-SEM-001",
            source_system="BMS",
            collected_at=datetime.now(UTC),
            summary="AHU-7 差压 470 Pa 超过预警值",
            raw_data={},
            content_hash="sha256:sem-2",
            confidence=3,
        ),
    ]
    # 历史案例的证据（应被搜索出来）
    historical_evidence = [
        Evidence(
            id="E-HIST-01",
            case_id="DEV-HIST-001",
            source_system="BMS",
            collected_at=datetime(2024, 8, 14, tzinfo=UTC),
            summary="AHU-7 差压历史异常，HVAC 故障",
            raw_data={},
            content_hash="sha256:hist-1",
            confidence=4,
        ),
        Evidence(
            id="E-HIST-02",
            case_id="DEV-HIST-001",
            source_system="LMS",
            collected_at=datetime(2024, 8, 14, tzinfo=UTC),
            summary="EMP-1234 更衣资质历史逾期",
            raw_data={},
            content_hash="sha256:hist-2",
            confidence=3,
        ),
    ]
    test_db.add_all(current_evidence + historical_evidence)
    await test_db.commit()

    # 重新加载 case with relationships
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    result = await test_db.execute(
        select(Deviation)
        .where(Deviation.id == "DEV-SEM-001")
        .options(selectinload(Deviation.evidence), selectinload(Deviation.five_whys))
    )
    return result.scalar_one()


# ============================================================
# vector_store 单元测试
# ============================================================
class TestCosineSimilarity:
    """余弦相似度计算"""

    def test_identical_vectors_return_one(self):
        from shared.vector_store import _cosine_similarity
        v = [1.0, 2.0, 3.0, 4.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0, rel=1e-6)

    def test_orthogonal_vectors_return_zero(self):
        from shared.vector_store import _cosine_similarity
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_empty_vectors_return_zero(self):
        from shared.vector_store import _cosine_similarity
        assert _cosine_similarity([], []) == 0.0

    def test_different_length_vectors_return_zero(self):
        from shared.vector_store import _cosine_similarity
        assert _cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_zero_vector_returns_zero(self):
        from shared.vector_store import _cosine_similarity
        assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestVectorSearch:
    """向量搜索"""

    async def test_vector_search_returns_by_similarity(
        self, test_db: AsyncSession, semantic_case
    ):
        """向量搜索按相似度降序返回"""
        from shared.vector_store import vector_search

        # 给历史案例的证据设置 embedding
        ev_hist = await test_db.get(Evidence, "E-HIST-01")
        ev_hist.embedding = [1.0, 0.0, 0.0]  # 与查询向量相同
        ev_curr = await test_db.get(Evidence, "E-SEM-02")
        ev_curr.embedding = [0.0, 1.0, 0.0]  # 与查询正交
        await test_db.commit()

        results = await vector_search(
            test_db, query_embedding=[1.0, 0.0, 0.0], top_k=10
        )

        assert len(results) >= 1
        # E-HIST-01 应该排第一（相似度 1.0）
        assert results[0].evidence_id == "E-HIST-01"
        assert results[0].score == pytest.approx(1.0, rel=1e-6)
        assert results[0].matched_by == "semantic"

    async def test_vector_search_excludes_case(
        self, test_db: AsyncSession, semantic_case
    ):
        """exclude_case_id 排除指定案例"""
        from shared.vector_store import vector_search

        # 给所有证据设置 embedding
        for ev_id in ["E-SEM-01", "E-SEM-02", "E-HIST-01", "E-HIST-02"]:
            ev = await test_db.get(Evidence, ev_id)
            ev.embedding = [1.0, 0.0]
        await test_db.commit()

        # 排除当前案例 DEV-SEM-001
        results = await vector_search(
            test_db, query_embedding=[1.0, 0.0], top_k=10,
            exclude_case_id="DEV-SEM-001",
        )

        # 只返回历史案例的证据
        for r in results:
            assert r.case_id != "DEV-SEM-001"
        # 应该包含 E-HIST-01 / E-HIST-02
        ev_ids = {r.evidence_id for r in results}
        assert "E-HIST-01" in ev_ids
        assert "E-HIST-02" in ev_ids

    async def test_vector_search_empty_when_no_embeddings(self, test_db: AsyncSession, semantic_case):
        """无 embedding 数据时返回空列表"""
        from shared.vector_store import vector_search

        results = await vector_search(test_db, query_embedding=[1.0], top_k=10)
        assert results == []


class TestKeywordSearch:
    """关键词搜索（降级路径）"""

    async def test_keyword_search_finds_matches(self, test_db: AsyncSession, semantic_case):
        from shared.vector_store import keyword_search

        results = await keyword_search(test_db, query="AHU-7 差压", top_k=10)
        assert len(results) > 0
        # 应该匹配到包含"差压"的证据
        summaries = [r.summary for r in results]
        assert any("差压" in s for s in summaries)
        assert all(r.matched_by == "keyword" for r in results)

    async def test_keyword_search_excludes_case(self, test_db: AsyncSession, semantic_case):
        from shared.vector_store import keyword_search

        results = await keyword_search(
            test_db, query="差压", top_k=10,
            exclude_case_id="DEV-SEM-001",
        )
        # 不应包含当前案例的证据
        for r in results:
            assert r.case_id != "DEV-SEM-001"

    async def test_keyword_search_empty_query_returns_empty(self, test_db: AsyncSession, semantic_case):
        from shared.vector_store import keyword_search

        results = await keyword_search(test_db, query="", top_k=10)
        assert results == []


class TestSearchEvidence:
    """search_evidence 统一入口（自动降级）"""

    async def test_search_evidence_falls_back_to_keyword_when_no_embedding(
        self, test_db: AsyncSession, semantic_case
    ):
        """无 query_embedding 时走关键词搜索"""
        from shared.vector_store import search_evidence

        results = await search_evidence(
            test_db, query="差压超标", top_k=10,
            query_embedding=None,
        )
        assert len(results) > 0
        assert all(r.matched_by == "keyword" for r in results)

    async def test_search_evidence_uses_semantic_when_embedding_available(
        self, test_db: AsyncSession, semantic_case
    ):
        """有 query_embedding 时走语义搜索"""
        from shared.vector_store import search_evidence

        # 给证据设置 embedding
        for ev_id in ["E-SEM-02", "E-HIST-01"]:
            ev = await test_db.get(Evidence, ev_id)
            ev.embedding = [1.0, 0.0, 0.0]
        await test_db.commit()

        results = await search_evidence(
            test_db, query="差压", top_k=10,
            query_embedding=[1.0, 0.0, 0.0],
        )
        assert len(results) > 0
        assert all(r.matched_by == "semantic" for r in results)

    async def test_search_evidence_falls_back_when_vector_returns_empty(
        self, test_db: AsyncSession, semantic_case
    ):
        """向量搜索返回空时降级到关键词搜索"""
        from shared.vector_store import search_evidence

        # query_embedding 提供但无证据有 embedding → 向量搜索返回空 → 降级
        results = await search_evidence(
            test_db, query="差压", top_k=10,
            query_embedding=[1.0, 0.0],  # 提供但无匹配
        )
        # 应该有关键词搜索结果
        assert len(results) > 0
        assert all(r.matched_by == "keyword" for r in results)


class TestFindRelatedCases:
    """跨案例关联"""

    async def test_find_related_cases_excludes_self(
        self, test_db: AsyncSession, semantic_case
    ):
        from shared.vector_store import find_related_cases

        related = await find_related_cases(
            test_db, query="差压超标 HVAC", top_k=5,
            exclude_case_id="DEV-SEM-001",
        )
        # 不应包含当前案例
        for r in related:
            assert r["case_id"] != "DEV-SEM-001"

    async def test_find_related_cases_aggregates_by_case(
        self, test_db: AsyncSession, semantic_case
    ):
        """证据按案例聚合"""
        from shared.vector_store import find_related_cases

        related = await find_related_cases(
            test_db, query="AHU-7 差压", top_k=5,
            exclude_case_id="DEV-SEM-001",
        )

        # 历史案例 DEV-HIST-001 有 2 条证据都含"差压"
        hist_cases = [r for r in related if r["case_id"] == "DEV-HIST-001"]
        if hist_cases:
            assert hist_cases[0]["evidence_count"] >= 1
            assert "max_score" in hist_cases[0]
            assert "matched_by" in hist_cases[0]
            assert "sample_summaries" in hist_cases[0]


# ============================================================
# llm_client embedding 测试
# ============================================================
class TestLLMClientEmbedding:
    """LLM 客户端 embedding 方法测试"""

    def test_supports_embeddings_false_when_not_configured(self):
        from shared.llm_client import LLMClient

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
            assert client.supports_embeddings is False

    def test_supports_embeddings_true_for_glm(self):
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
            assert client.supports_embeddings is True
            assert client.embedding_model_name == "embedding-3"

    def test_supports_embeddings_false_for_anthropic(self):
        """Anthropic 不提供 embedding API"""
        from shared.llm_client import LLMClient

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = "anthropic"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = "claude-sonnet-4-20250514"
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 2
            client = LLMClient()
            assert client.supports_embeddings is False
            assert client.embedding_model_name == ""

    async def test_embed_texts_empty_input_returns_empty(self):
        from shared.llm_client import LLMClient

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

            result = await client.embed_texts([])
            assert result == []

    async def test_embed_texts_raises_when_not_supported(self):
        from shared.llm_client import LLMClient, LLMConfigError

        with patch("shared.llm_client.settings") as mock_settings:
            mock_settings.llm_provider = ""
            mock_settings.llm_api_key = ""
            mock_settings.llm_base_url = ""
            mock_settings.llm_model = ""
            mock_settings.llm_max_tokens = 2048
            mock_settings.llm_temperature = 0.2
            mock_settings.llm_timeout_seconds = 30
            mock_settings.llm_max_retries = 0
            client = LLMClient()

            with pytest.raises(LLMConfigError):
                await client.embed_texts(["test"])

    async def test_embed_texts_success(self):
        from shared.llm_client import LLMClient

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

            mock_http_resp = MagicMock()
            mock_http_resp.status_code = 200
            mock_http_resp.json.return_value = {
                "data": [
                    {"embedding": [0.1, 0.2, 0.3]},
                    {"embedding": [0.4, 0.5, 0.6]},
                ]
            }
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client):
                results = await client.embed_texts(["text1", "text2"])

            assert len(results) == 2
            assert results[0] == [0.1, 0.2, 0.3]
            assert results[1] == [0.4, 0.5, 0.6]

    async def test_embed_text_single(self):
        from shared.llm_client import LLMClient

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

            mock_http_resp = MagicMock()
            mock_http_resp.status_code = 200
            mock_http_resp.json.return_value = {
                "data": [{"embedding": [0.1, 0.2]}]
            }
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client):
                result = await client.embed_text("test text")

            assert result == [0.1, 0.2]

    async def test_embed_texts_count_mismatch_raises(self):
        """返回 embedding 数量与输入不匹配抛错"""
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

            # 输入 2 条但只返回 1 条
            mock_http_resp = MagicMock()
            mock_http_resp.status_code = 200
            mock_http_resp.json.return_value = {
                "data": [{"embedding": [0.1, 0.2]}]  # 只有 1 条
            }
            mock_http_client = MagicMock()
            mock_http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=None)

            with patch("shared.llm_client.httpx.AsyncClient", return_value=mock_http_client):
                with pytest.raises(LLMResponseError):
                    await client.embed_texts(["a", "b"])


# ============================================================
# Evidence Hunter 升级测试
# ============================================================
class TestEvidenceHunterSemantic:
    """_run_evidence_hunter 升级测试"""

    async def test_hunter_returns_related_cases_when_flag_on_keyword_mode(
        self, test_db: AsyncSession, semantic_case
    ):
        """flag 开启但 LLM 未配置 → 走关键词搜索，仍返回 related_cases"""
        from agent_service.routes import _run_evidence_hunter

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client") as mock_llm:
            mock_llm.supports_embeddings = False  # LLM 未配置

            result = await _run_evidence_hunter(test_db, semantic_case)

        assert result["agent"] == "evidence_hunter"
        assert result["status"] == "completed"
        assert "related_historical_cases" in result
        assert result["semantic_search_status"] == "keyword"
        # ai_model 反映使用了语义检索
        assert "semantic-keyword" in result["ai_model"]

    async def test_hunter_no_related_cases_when_flag_off(
        self, test_db: AsyncSession, semantic_case
    ):
        """flag 关闭时 related_cases 为空"""
        from agent_service.routes import _run_evidence_hunter

        with patch("shared.feature_flag.is_enabled", return_value=False):
            result = await _run_evidence_hunter(test_db, semantic_case)

        assert result["semantic_search_status"] == "disabled"
        assert result["related_historical_cases"] == []
        assert result["ai_model"] == "rule-template-v1"

    async def test_hunter_semantic_mode_when_llm_available(
        self, test_db: AsyncSession, semantic_case
    ):
        """flag 开启 + LLM 可用 → 走语义搜索"""
        from agent_service.routes import _run_evidence_hunter

        mock_llm = MagicMock()
        mock_llm.supports_embeddings = True
        mock_llm.embed_text = AsyncMock(return_value=[1.0, 0.0, 0.0])

        # 给历史证据设置 embedding
        ev_hist = await test_db.get(Evidence, "E-HIST-01")
        ev_hist.embedding = [1.0, 0.0, 0.0]
        ev_curr = await test_db.get(Evidence, "E-SEM-02")
        ev_curr.embedding = [0.0, 1.0, 0.0]
        await test_db.commit()

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_llm):
            result = await _run_evidence_hunter(test_db, semantic_case)

        assert result["semantic_search_status"] == "semantic"
        assert "semantic-semantic" in result["ai_model"]
        # related_cases 应该有内容
        assert isinstance(result["related_historical_cases"], list)

    async def test_hunter_fallback_when_llm_raises_exception(
        self, test_db: AsyncSession, semantic_case
    ):
        """flag 开启但 LLM embed_text 抛异常 → 降级到 keyword，不阻塞流水线"""
        from agent_service.routes import _run_evidence_hunter
        from shared.llm_client import LLMError

        mock_llm = MagicMock()
        mock_llm.supports_embeddings = True
        mock_llm.embed_text = AsyncMock(side_effect=LLMError("network error"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_llm):
            result = await _run_evidence_hunter(test_db, semantic_case)

        # 应该降级到 keyword 搜索
        assert result["semantic_search_status"] == "keyword"
        # 流水线不阻塞
        assert result["status"] == "completed"

    async def test_hunter_preserves_basic_fields(
        self, test_db: AsyncSession, semantic_case
    ):
        """升级后基础字段（evidence_count, by_source, gaps）保持不变"""
        from agent_service.routes import _run_evidence_hunter

        with patch("shared.feature_flag.is_enabled", return_value=False):
            result = await _run_evidence_hunter(test_db, semantic_case)

        # 基础统计字段不变
        assert result["evidence_count"] == 2  # 当前案例有 2 条证据
        assert "LMS" in result["by_source"]
        assert "BMS" in result["by_source"]
        assert "gaps" in result
        assert "ai_output_hash" in result
        assert result["ai_output_hash"].startswith("sha256:")

    async def test_embedding_audit_rollback_on_failure(self, test_db: AsyncSession):
        """嵌入失败时审计日志应被回滚或记录错误"""
        from agent_service.routes import _run_evidence_hunter
        from shared.audit import append_audit_log
        from shared.llm_client import LLMError

        # 给案例添加证据
        from shared.models import Evidence
        ev = Evidence(
            id="E-ROLLBACK-001",
            case_id="DEV-ROLLBACK-001",
            source_system="TEST",
            collected_at=datetime.now(UTC),
            summary="Rollback test evidence",
            raw_data={},
            content_hash="sha256:rollback",
            confidence=3,
        )
        test_db.add(ev)
        await test_db.commit()

        # 创建一个最小案例
        from shared.models import Deviation
        case = Deviation(
            id="DEV-ROLLBACK-001",
            title="回滚测试案例",
            severity="minor",
            status="investigating",
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
        )
        test_db.add(case)
        await test_db.commit()

        # 重新加载案例，预加载关系（_run_evidence_hunter 会同步访问 case.evidence，
        # 未加载的关系在 async 上下文中惰性加载会抛 MissingGreenlet）
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        case = (
            await test_db.execute(
                select(Deviation)
                .where(Deviation.id == "DEV-ROLLBACK-001")
                .options(selectinload(Deviation.evidence), selectinload(Deviation.five_whys))
            )
        ).scalar_one()

        # LLM 故障：embed_text 抛出异常
        mock_llm = MagicMock()
        mock_llm.supports_embeddings = True
        mock_llm.embed_text = AsyncMock(side_effect=LLMError("embedding service unavailable"))

        with patch("shared.feature_flag.is_enabled", return_value=True), \
             patch("shared.llm_client.llm_client", mock_llm):
            result = await _run_evidence_hunter(test_db, case)

        # 流水线应降级并完成，不应因 LLM 故障而崩溃
        assert result["status"] == "completed"
        assert result["semantic_search_status"] == "keyword"

        # 验证审计链完整性（即使 embedding 失败， audit log 也应可验证）
        from shared.audit import verify_hash_chain
        valid = await verify_hash_chain(test_db)
        assert valid is True, "审计链应即使在 embedding 失败时也保持完整"
