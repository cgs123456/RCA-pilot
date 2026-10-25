"""Feature Flag 框架 —— 灰度发布控制

支持两种模式：
1. 本地配置（默认）—— 从环境变量/配置文件读取，适合 Phase 2 单机部署
2. Unleash 远程（生产）—— 对接 Unleash 服务，支持动态调整 + A/B 测试

用法：
    from shared.feature_flag import is_enabled, flag_context

    if is_enabled("skeptic_v2_enhanced", user_id="U-LC"):
        # 新版 Skeptic 逻辑
        ...
    else:
        # 旧版逻辑
        ...
"""
from __future__ import annotations

import copy  # noqa: F401
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import settings

# ============================================================
# 默认 flag 配置（本地模式）
# ============================================================
DEFAULT_FLAGS: dict[str, dict[str, Any]] = {
    # Skeptic v2 增强版 —— 更激进的挑战策略
    "skeptic_v2_enhanced": {
        "enabled": False,  # 默认关闭，灰度开启
        "description": "Skeptic v2 增强挑战策略（3→5 条挑战 + 独立证据校验）",
        "rollout_percentage": 0,  # 0-100，按用户 hash 灰度
    },
    # Reasoner 使用 LLM 替代规则
    "reasoner_llm_enabled": {
        "enabled": False,
        "description": "Reasoner 使用 LLM 替代规则推理",
        "rollout_percentage": 0,
    },
    # Evidence Hunter 语义检索（跨案例关联）
    "evidence_hunter_semantic": {
        "enabled": False,
        "description": "Evidence Hunter 启用语义检索 + 跨案例关联（需 LLM embedding 配置）",
        "rollout_percentage": 0,
    },
    # Drafter 使用 LLM 生成 ICH Q9 报告
    "drafter_llm_enabled": {
        "enabled": False,
        "description": "Drafter 使用 LLM 生成 ICH Q9 格式报告（替代模板拼接）",
        "rollout_percentage": 0,
    },
    # 多 Agent 博弈验证（Phase 2 改进5）
    "multi_agent_debate_enabled": {
        "enabled": False,
        "description": "并行启动 2 个 Reasoner（primary + adversary）+ Skeptic 交叉质证 + Meta-Controller 综合置信度结论",
        "rollout_percentage": 0,
    },
    # 实时审计日志流（WebSocket）
    "realtime_audit_stream": {
        "enabled": False,
        "description": "审计日志实时推送（WebSocket）",
        "rollout_percentage": 0,
    },
    # 新版 Dashboard（含趋势预测）
    "dashboard_v2_predictive": {
        "enabled": True,  # 已全量上线
        "description": "Dashboard v2 含趋势预测",
        "rollout_percentage": 100,
    },
    # HITL gate3 自动重试上限
    "hitl_gate3_auto_retry": {
        "enabled": True,
        "description": "gate3 reject 后自动触发 Reasoner 重试（上限 3 次）",
        "rollout_percentage": 100,
        "max_retries": 3,
    },
}


@lru_cache
def _load_flags() -> dict[str, dict[str, Any]]:
    """加载 flag 配置

    优先级：环境变量 RCA_FEATURE_FLAGS > 配置文件 > DEFAULT_FLAGS
    必须深拷贝 DEFAULT_FLAGS —— 浅拷贝会让 env/file 覆盖就地污染默认值，
    导致 reload_flags 后灰度百分比回滚失效。
    """
    flags = copy.deepcopy(DEFAULT_FLAGS)

    # 从环境变量加载（JSON 字符串）
    env_flags = os.getenv("RCA_FEATURE_FLAGS")
    if env_flags:
        try:
            override = json.loads(env_flags)
            for k, v in override.items():
                if k in flags:
                    flags[k].update(v)
                else:
                    flags[k] = v
        except json.JSONDecodeError:
            pass

    # 从配置文件加载（如果存在）
    flag_file = Path(os.getenv("RCA_FEATURE_FLAGS_FILE", "feature_flags.json"))
    if flag_file.exists():
        try:
            file_flags = json.loads(flag_file.read_text(encoding="utf-8"))
            for k, v in file_flags.items():
                if k in flags:
                    flags[k].update(v)
                else:
                    flags[k] = v
        except (json.JSONDecodeError, OSError):
            pass

    return flags


def _hash_user_id(user_id: str) -> int:
    """对 user_id 做稳定 hash（0-99），用于百分比灰度"""
    import hashlib

    h = hashlib.md5(user_id.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(h[:8], 16) % 100


# Unleash 客户端进程级单例 —— 旧实现每次 is_enabled 都新建客户端并同步
# initialize_client()（阻塞事件循环 + 连接泄漏）
_unleash_client: Any | None = None
_unleash_failed: bool = False


def _get_unleash_client() -> Any | None:
    """懒初始化 Unleash 客户端单例；失败后不再反复重试（降级本地配置）"""
    global _unleash_client, _unleash_failed
    if _unleash_client is not None or _unleash_failed:
        return _unleash_client
    try:
        from UnleashClient import UnleashClient

        client = UnleashClient(url=settings.unleash_url, app_name="rca-pilot")
        client.initialize_client()
        _unleash_client = client
        return _unleash_client
    except Exception:
        _unleash_failed = True
        return None


def is_enabled(
    flag_name: str,
    *,
    user_id: str | None = None,
    default: bool = False,
) -> bool:
    """判断 feature flag 是否启用

    Args:
        flag_name: flag 名称
        user_id: 用户 ID（用于百分比灰度，不传则只看 enabled）
        default: flag 未定义时的默认返回值

    Returns:
        bool: 是否启用
    """
    # 优先尝试 Unleash 远程（如果配置了）
    if settings.unleash_url:
        client = _get_unleash_client()
        if client is not None:
            try:
                context = {"userId": user_id} if user_id else {}
                return bool(client.is_enabled(flag_name, context=context))
            except Exception:
                # Unleash 判定异常时降级到本地配置
                pass

    # 本地配置模式
    flags = _load_flags()
    flag = flags.get(flag_name)
    if not flag:
        return default

    if not flag.get("enabled", False):
        return False

    # 百分比灰度
    rollout = flag.get("rollout_percentage", 100)
    # 验证 rollout_percentage 合法性（0-100）
    if not isinstance(rollout, int | float) or rollout < 0 or rollout > 100:
        raise ValueError(f"Feature flag {flag_name} rollout_percentage 必须为 0-100 之间的数值，当前值: {rollout}")
    if rollout >= 100:
        return True
    if rollout <= 0:
        return False
    if not user_id:
        return False  # 需要用户上下文做灰度

    return _hash_user_id(user_id) < rollout


def flag_context(flag_name: str) -> dict[str, Any]:
    """获取 flag 的完整上下文（用于审计/调试）"""
    flags = _load_flags()
    return flags.get(flag_name, {})


def list_flags() -> dict[str, dict[str, Any]]:
    """列出所有 flag（管理端用）"""
    return _load_flags()


def reload_flags() -> None:
    """重新加载 flag 配置（管理端修改后调用）"""
    _load_flags.cache_clear()
