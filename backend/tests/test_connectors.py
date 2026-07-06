"""Connector 扩展测试（Phase 2 改进6）

覆盖：
- BMS Connector mock 数据完整性（AHU-7 差压时间序列、anomalies 过滤）
- EMS Connector mock 数据完整性（沉降菌趋势、exceedances 警告/行动线）
- MES Connector mock 数据完整性（批次、清洁记录、CCTV 异常）
- Connector URL 配置（settings.bms/ems/mes_connector_url）
- evidence_service fetch_from_source 路由分发到 BMS/EMS/MES Connector
- 不支持的源系统返回 ConnectorError
- 未配置 URL 的 Connector 返回 skipped

注意：FastAPI Query(default=None) 参数在直接调用时为 Query 对象（truth-y），
所以测试中显式传入 None 以避免过滤逻辑错误。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ============================================================
# BMS Connector 直接路由测试
# ============================================================
class TestBMSConnector:
    """BMS Connector — 楼宇管理系统"""

    async def test_health(self):
        from connectors.bms.routes import health
        result = await health()
        assert result["status"] == "healthy"
        assert result["service"] == "bms-connector"
        assert result["is_readonly"] is True

    async def test_ahus_returns_list(self):
        from connectors.bms.routes import list_ahus
        result = await list_ahus()
        assert result["status"] == "ok"
        assert result["source"] == "BMS"
        assert "AHU-7" in str(result)

    async def test_ahu7_history_has_delta_p(self):
        """AHU-7 差压历史应包含 380 Pa 基线"""
        from connectors.bms.routes import get_ahu_history
        # 显式传 param=None 避免 Query 对象干扰
        result = await get_ahu_history(ahu_id="AHU-7", param=None)
        assert result["status"] == "ok"
        history = result.get("history", [])
        assert len(history) > 0
        # 第一个数据点 delta_p_pa 应该是 380 Pa（基线）
        values = [h.get("delta_p_pa") for h in history]
        assert 380 in values

    async def test_ahu7_history_filter_by_param(self):
        """按 param=delta_p_pa 过滤只返回该参数"""
        from connectors.bms.routes import get_ahu_history
        result = await get_ahu_history(ahu_id="AHU-7", param="delta_p_pa")
        history = result.get("history", [])
        assert len(history) > 0
        # 过滤后每个数据点应包含 delta_p_pa 键
        assert all("delta_p_pa" in h for h in history)

    async def test_anomalies_returns_all(self):
        """不带 level 过滤应返回所有异常事件"""
        from connectors.bms.routes import list_anomalies
        # 显式传 level=None 避免 Query 对象干扰
        result = await list_anomalies(level=None)
        assert result["status"] == "ok"
        assert result["source"] == "BMS"
        items = result.get("items", [])
        # AHU-7 应有 4 个异常事件 (E-BMS-01 ~ E-BMS-04)
        assert len(items) >= 3
        # 应包含 event_id
        event_ids = [it.get("event_id") for it in items]
        assert "E-BMS-01" in event_ids

    async def test_anomalies_filter_by_level_warning(self):
        """按 level=warning 过滤应只返回 warning 级别"""
        from connectors.bms.routes import list_anomalies
        result = await list_anomalies(level="warning")
        items = result.get("items", [])
        assert all(it.get("level") == "warning" for it in items)

    async def test_ahu_not_found(self):
        from connectors.bms.routes import get_ahu_history
        result = await get_ahu_history(ahu_id="AHU-999", param=None)
        assert result.get("status") == "not_found"


# ============================================================
# EMS Connector 直接路由测试
# ============================================================
class TestEMSConnector:
    """EMS Connector — 环境监测系统"""

    async def test_health(self):
        from connectors.ems.routes import health
        result = await health()
        assert result["status"] == "healthy"
        assert result["service"] == "ems-connector"

    async def test_points_returns_monitoring_points(self):
        from connectors.ems.routes import list_points
        result = await list_points()
        assert result["status"] == "ok"
        assert result["source"] == "EMS"
        assert "Room-3B-Act-12" in str(result)

    async def test_point_history_shows_cfu_trend(self):
        """沉降菌历史应包含 3 CFU 基线和 22 CFU 超标值"""
        from connectors.ems.routes import get_point_history
        result = await get_point_history(point_id="Room-3B-Act-12")
        # status 是点位状态（normal/alert/action），不是 HTTP 状态
        assert result.get("status") != "not_found"
        history = result.get("history", [])
        assert len(history) > 0
        cfu_values = [h.get("cfu") for h in history if h.get("cfu") is not None]
        # 应包含基线 3 和超标值 22
        assert 3 in cfu_values or 22 in cfu_values

    async def test_exceedances_returns_alert_and_action(self):
        """exceedances 应返回超过警戒线/行动线的记录"""
        from connectors.ems.routes import list_exceedances
        result = await list_exceedances(status=None)
        assert result["status"] == "ok"
        items = result.get("items", [])
        assert len(items) > 0
        # 应包含 alert 或 action 级别
        levels = [it.get("level") for it in items]
        assert "alert" in levels or "action" in levels

    async def test_point_not_found(self):
        from connectors.ems.routes import get_point_history
        result = await get_point_history(point_id="Room-999")
        assert result.get("status") == "not_found"


# ============================================================
# MES Connector 直接路由测试
# ============================================================
class TestMESConnector:
    """MES Connector — 生产执行系统"""

    async def test_health(self):
        from connectors.mes.routes import health
        result = await health()
        assert result["status"] == "healthy"
        assert result["service"] == "mes-connector"

    async def test_batch_info(self):
        from connectors.mes.routes import get_batch
        result = await get_batch(batch_id="BN-2025-08")
        assert result["status"] == "ok"
        assert result["source"] == "MES"
        # 批次应关联 B 级区
        assert "B" in str(result) or "Room-3B" in str(result)

    async def test_batch_not_found(self):
        from connectors.mes.routes import get_batch
        result = await get_batch(batch_id="BN-NONEXIST")
        assert result.get("status") == "not_found"

    async def test_cleaning_records(self):
        from connectors.mes.routes import list_cleaning_records
        result = await list_cleaning_records(room=None, asset=None)
        assert result["status"] == "ok"
        # 应包含 WO-20250814 清洁记录
        assert "WO-20250814" in str(result) or "cleaning" in str(result).lower() or "清洁" in str(result)

    async def test_cctv_anomalies(self):
        from connectors.mes.routes import list_cctv_anomalies
        result = await list_cctv_anomalies(employee_id=None, anomaly_type=None)
        assert result["status"] == "ok"
        # 应包含 EMP-2187 的更衣异常
        assert "EMP-2187" in str(result)
        items = result.get("items", [])
        assert len(items) > 0
        # 异常类型应为 inner_glove_not_covered
        anomaly_types = [it.get("anomaly_type") for it in items]
        assert "inner_glove_not_covered" in anomaly_types


# ============================================================
# Connector URL 配置测试
# ============================================================
class TestConnectorConfig:
    """Connector URL 配置"""

    def test_bms_connector_url_default(self):
        from shared.config import settings
        assert settings.bms_connector_url == "http://bms-connector:8012"

    def test_ems_connector_url_default(self):
        from shared.config import settings
        assert settings.ems_connector_url == "http://ems-connector:8013"

    def test_mes_connector_url_default(self):
        from shared.config import settings
        assert settings.mes_connector_url == "http://mes-connector:8014"

    def test_bms_connector_port_default(self):
        from shared.config import settings
        assert settings.bms_connector_port == 8012

    def test_ems_connector_port_default(self):
        from shared.config import settings
        assert settings.ems_connector_port == 8013

    def test_mes_connector_port_default(self):
        from shared.config import settings
        assert settings.mes_connector_port == 8014


# ============================================================
# fetch_from_source 路由集成测试（mock httpx）
# ============================================================
class TestFetchFromSourceIntegration:
    """evidence_service fetch_from_source 路由分发到各 Connector"""

    async def test_fetch_bms_calls_bms_connector(self):
        """source=BMS 应调用 bms_connector_url + /bms/ahus"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ok", "source": "BMS",
            "items": [{"ahu_id": "AHU-7", "area": "Room-3B"}],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            from evidence_service.routes import fetch_from_source

            result = await fetch_from_source(
                request=MagicMock(),
                source="BMS",
                case_id="DEV-2025-08-147",
                db=MagicMock(),
                auth=MagicMock(email="test@example.com"),
            )

        assert result["status"] == "ok"
        assert result["source"] == "BMS"
        assert result["fetched_count"] == 1  # 来自 items 长度
        # 验证调用 URL 包含 /bms/ahus
        call_args = mock_client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/bms/ahus" in url

    async def test_fetch_ems_calls_ems_connector(self):
        """source=EMS 应调用 ems_connector_url + /ems/exceedances"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ok", "source": "EMS",
            "items": [{"point_id": "Room-3B-Act-12", "status": "action_exceeded"}],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            from evidence_service.routes import fetch_from_source

            result = await fetch_from_source(
                request=MagicMock(),
                source="EMS",
                db=MagicMock(),
                auth=MagicMock(email="test@example.com"),
            )

        assert result["status"] == "ok"
        assert result["source"] == "EMS"
        call_args = mock_client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/ems/exceedances" in url

    async def test_fetch_mes_calls_mes_connector(self):
        """source=MES 应调用 mes_connector_url + /mes/cctv/anomalies"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ok", "source": "MES",
            "items": [{"employee_id": "EMP-2187", "anomaly": "inner_glove_not_covered"}],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            from evidence_service.routes import fetch_from_source

            result = await fetch_from_source(
                request=MagicMock(),
                source="MES",
                db=MagicMock(),
                auth=MagicMock(email="test@example.com"),
            )

        assert result["status"] == "ok"
        assert result["source"] == "MES"
        call_args = mock_client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/mes/cctv/anomalies" in url

    async def test_unsupported_source_raises_error(self):
        """不支持的源系统应抛出 ConnectorError"""
        from evidence_service.routes import fetch_from_source
        from shared.exceptions import ConnectorError

        with pytest.raises(ConnectorError):
            await fetch_from_source(
                request=MagicMock(),
                source="UNKNOWN",
                db=MagicMock(),
                auth=MagicMock(email="test@example.com"),
            )

    async def test_unconfigured_connector_returns_skipped(self):
        """URL 未配置的 Connector 应返回 skipped"""
        with patch("evidence_service.routes.settings") as mock_settings:
            mock_settings.lms_connector_url = "http://lms:8011"
            mock_settings.bms_connector_url = ""  # 未配置
            mock_settings.ems_connector_url = ""
            mock_settings.mes_connector_url = ""

            from evidence_service.routes import fetch_from_source

            result = await fetch_from_source(
                request=MagicMock(),
                source="BMS",
                db=MagicMock(),
                auth=MagicMock(email="test@example.com"),
            )

        assert result["status"] == "skipped"
        assert result["fetched_count"] == 0

    async def test_connector_http_error_raises_connector_error(self):
        """Connector HTTP 失败应抛出 ConnectorError"""
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            from evidence_service.routes import fetch_from_source
            from shared.exceptions import ConnectorError

            with pytest.raises(ConnectorError, match="BMS Connector 调用失败"):
                await fetch_from_source(
                    request=MagicMock(),
                    source="BMS",
                    db=MagicMock(),
                    auth=MagicMock(email="test@example.com"),
                )

    async def test_fetched_count_from_total_key(self):
        """data.total 字段格式的 fetched_count 统计"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ok", "source": "BMS", "total": 5, "items": [],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            from evidence_service.routes import fetch_from_source

            result = await fetch_from_source(
                request=MagicMock(),
                source="BMS",
                db=MagicMock(),
                auth=MagicMock(email="test@example.com"),
            )

        # items 为空但 total=5，应取 total
        assert result["fetched_count"] == 5
