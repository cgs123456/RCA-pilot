"""统一 LLM 客户端 — 支持 GLM / Qwen / OpenAI / Anthropic / Azure

设计目标：
1. **OpenAI 兼容优先**：GLM (智谱) 和 Qwen (阿里通义) 均提供 OpenAI 兼容 API，
   复用同一套请求协议，仅 base_url 与鉴权头不同。
2. **JSON 输出强制**：GxP 场景要求 LLM 输出可被程序解析，统一通过
   `response_format={"type": "json_object"}` + Pydantic schema 二次校验保证结构。
3. **可观测**：每次调用记录 token 用量、模型版本、调用耗时，便于审计与成本追踪。
4. **优雅降级**：API key 未配置 / 网络异常 / JSON 解析失败时抛 `LLMError`，
   由调用方决定是否降级到规则版兜底。
5. **重试与超时**：指数退避重试（默认 2 次），HTTP 超时 30s，避免阻塞 Agent 流水线。

用法：
    from shared.llm_client import llm_client, LLMError

    try:
        result = await llm_client.chat_json(
            system="你是 GMP 调查员...",
            user_prompt="证据：... 请输出 5 Whys JSON",
            schema=ReasonerOutput,  # Pydantic Model
        )
        # result 是 ReasonerOutput 实例
    except LLMError as e:
        # 降级到规则版
        ...

依赖：httpx（已存在于 requirements.txt）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import weakref
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import settings


# ============================================================
# 异常定义
# ============================================================
class LLMError(Exception):
    """LLM 调用通用异常（含配置缺失/网络失败/JSON 解析失败/Schema 校验失败）"""


class LLMConfigError(LLMError):
    """LLM 配置缺失（如未配置 api_key）"""


class LLMResponseError(LLMError):
    """LLM 响应格式异常（HTTP 非 200 / 响应缺字段 / JSON 解析失败 / Schema 校验失败）

    status_code：HTTP 状态码（非 HTTP 错误时为 None）。重试分类用它做数值判断，
    不再对错误文案做字符串匹配。
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _is_client_error(e: Exception) -> bool:
    """4xx（除 429）不应重试 —— 参数/鉴权问题重试无意义"""
    status = getattr(e, "status_code", None)
    return status is not None and 400 <= status < 500 and status != 429


# ============================================================
# Provider 配置 —— base_url 与鉴权头按 provider 自动选择
# ============================================================
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    # 智谱 GLM —— OpenAI 兼容协议
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "auth_header": "Authorization",  # Bearer {api_key}
        "auth_scheme": "Bearer",
        "default_model": "glm-4-plus",
    },
    # 阿里通义 Qwen —— DashScope OpenAI 兼容模式
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer",
        "default_model": "qwen-plus",
    },
    # OpenAI 官方
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer",
        "default_model": "gpt-4o",
    },
    # Anthropic —— 通过 Anthropic SDK 调用，此处仅占位，实际使用 Claude sonnet
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "auth_header": "x-api-key",
        "auth_scheme": "",  # Anthropic 直接传 key，不加 Bearer
        "default_model": "claude-sonnet-4-20250514",
    },
    # Azure OpenAI（部署在 Azure 的 OpenAI 服务）
    "azure": {
        "base_url": "",  # 必须从 llm_base_url 配置
        "auth_header": "api-key",
        "auth_scheme": "",
        "default_model": "gpt-4o",
    },
}


T = TypeVar("T", bound=BaseModel)


# ============================================================
# 统一 LLM 客户端
# ============================================================
class LLMClient:
    """统一 LLM 客户端 — 单例模式（通过模块级 `llm_client` 实例访问）

    设计参考运营平台 adapters/llm_backend.py 的 OpenAICompatibleBackend：
    - 所有 provider 走 OpenAI 兼容协议（/chat/completions）
    - Anthropic 与 Azure 通过适配层转换为兼容格式
    - JSON 输出模式通过 `response_format={"type": "json_object"}` 触发
    """

    def __init__(self) -> None:
        self._provider: str = settings.llm_provider.lower().strip()
        self._api_key: str = settings.llm_api_key.strip()
        self._model: str = settings.llm_model
        self._base_url: str = settings.llm_base_url.strip()
        self._max_tokens: int = settings.llm_max_tokens
        self._temperature: float = settings.llm_temperature
        self._timeout: float = float(settings.llm_timeout_seconds)
        self._max_retries: int = settings.llm_max_retries
        # httpx 连接复用：按事件循环缓存 AsyncClient（pytest 每个用例新建 loop，
        # 模块级单例必须与 loop 绑定，否则跨 loop 复用会报 "Event loop is closed"）
        self._http_clients: weakref.WeakKeyDictionary[Any, httpx.AsyncClient] = weakref.WeakKeyDictionary()
        self._resolve_provider_defaults()

    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取当前事件循环绑定的 httpx 客户端（连接池复用，避免每请求新建 TCP/TLS）"""
        loop = asyncio.get_running_loop()
        client = self._http_clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._http_clients[loop] = client
        return client

    def _resolve_provider_defaults(self) -> None:
        """根据 provider 解析 base_url / model / auth_header / auth_scheme

        抽取为方法以便 from_override 复用。
        """
        if self._provider and self._provider in _PROVIDER_DEFAULTS:
            cfg = _PROVIDER_DEFAULTS[self._provider]
            if not self._base_url:
                self._base_url = cfg["base_url"]
            if not self._model or self._model == "claude-sonnet-4-20250514":
                # model 未显式配置或仍是默认值（与 provider 不匹配），用 provider 默认
                self._model = cfg["default_model"]
            self._auth_header = cfg["auth_header"]
            self._auth_scheme = cfg["auth_scheme"]
        else:
            # provider 为空或未知 —— 客户端处于禁用状态，调用时抛 LLMConfigError
            self._auth_header = "Authorization"
            self._auth_scheme = "Bearer"

    @classmethod
    def from_override(
        cls,
        *,
        provider: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
    ) -> LLMClient:
        """创建带覆盖配置的 LLMClient 新实例

        未指定的字段从 settings 默认值继承。用于多 Agent 博弈场景下
        分别构造 primary / adversary 客户端（不同 provider/model/temperature）。

        Args:
            provider: 覆盖 provider（如 "qwen"），None 则用 settings.llm_provider
            api_key: 覆盖 api_key，None 则用 settings.llm_api_key
            model: 覆盖 model，None 则用 settings.llm_model（再走 provider 默认）
            base_url: 覆盖 base_url，None 则按 provider 选默认
            temperature: 覆盖 temperature，None 则用 settings.llm_temperature

        Returns:
            LLMClient: 新实例（独立于单例 llm_client）
        """
        instance = cls.__new__(cls)
        instance._provider = (provider if provider is not None else settings.llm_provider).lower().strip()
        instance._api_key = (api_key if api_key is not None else settings.llm_api_key).strip()
        instance._model = model if model is not None else settings.llm_model
        instance._base_url = (base_url if base_url is not None else settings.llm_base_url).strip()
        instance._max_tokens = settings.llm_max_tokens
        instance._temperature = temperature if temperature is not None else settings.llm_temperature
        instance._timeout = float(settings.llm_timeout_seconds)
        instance._max_retries = settings.llm_max_retries
        instance._resolve_provider_defaults()
        return instance

    # ------------------------------------------------------------
    # 公共状态查询
    # ------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        """是否已配置可用 — provider + api_key + base_url 三者齐备"""
        return bool(self._provider and self._api_key and self._base_url)

    @property
    def model_name(self) -> str:
        """当前使用的模型名（用于审计 ai_model 字段）"""
        return self._model or "unknown"

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def identity(self) -> tuple[str, str]:
        """(provider, model) 标识 —— 多 Agent 博弈用于校验 primary/adversary 是不同模型"""
        return (self._provider, self._model)

    # ------------------------------------------------------------
    # 核心 API：chat_completion
    # ------------------------------------------------------------
    async def chat_completion(
        self,
        system: str,
        user_prompt: str,
        *,
        json_mode: bool = False,
        extra_headers: dict[str, str] | None = None,
        agent_label: str = "unknown",
        max_tokens: int | None = None,
    ) -> str:
        """调用 LLM 完成对话，返回助手回复文本

        Args:
            system: System prompt
            user_prompt: User message
            json_mode: 是否强制 JSON 输出（OpenAI compatible response_format）
            extra_headers: 额外 HTTP 头（如 Anthropic 的 anthropic-version）
            agent_label: 调用方 Agent 名（用于 token 计量指标）
            max_tokens: 本次调用的输出上限（Drafter 长报告用，默认取 settings）

        Returns:
            str: 助手回复内容

        Raises:
            LLMConfigError: 未配置 api_key / base_url
            LLMResponseError: HTTP 非 200 / 响应缺 choices[0].message.content / 超时
        """
        if not self.is_configured:
            raise LLMConfigError(
                f"LLM 未配置：provider={self._provider!r}, api_key={'已设' if self._api_key else '缺失'}, "
                f"base_url={self._base_url!r}"
            )

        if self._provider == "anthropic":
            # Anthropic 走 /v1/messages 原生协议（其无 /chat/completions 端点）
            return await self._anthropic_chat_completion(
                system, user_prompt, json_mode=json_mode, agent_label=agent_label, max_tokens=max_tokens
            )

        url = f"{self._base_url.rstrip('/')}/chat/completions"
        headers = self._build_headers(extra_headers)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "temperature": self._temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._get_http_client()
                resp = await client.post(url, headers=headers, json=payload)

                if resp.status_code != 200:
                    raise LLMResponseError(
                        f"HTTP {resp.status_code}: {resp.text[:500]}",
                        status_code=resp.status_code,
                    )

                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not content:
                    raise LLMResponseError("LLM 返回空内容")

                # finish_reason=length 意味着输出被截断 —— 上游（尤其 Drafter 报告）
                # 必须知道，否则静默产出不完整内容
                finish_reason = data.get("choices", [{}])[0].get("finish_reason")
                self._record_usage(agent_label, data.get("usage"), finished=finish_reason != "length")
                if finish_reason == "length":
                    raise LLMResponseError(
                        f"LLM 输出被 max_tokens={payload['max_tokens']} 截断（finish_reason=length）"
                    )

                return content

            except (httpx.TimeoutException, httpx.HTTPError) as e:
                last_error = LLMResponseError(f"网络异常: {type(e).__name__}: {e}")
            except (KeyError, json.JSONDecodeError) as e:
                last_error = LLMResponseError(f"响应解析失败: {type(e).__name__}: {e}")
            except LLMResponseError as e:
                # 4xx 客户端错误（除 429 外）不重试 —— 数值判断，不匹配文案
                if _is_client_error(e):
                    raise
                last_error = e

            # 指数退避：0.5s, 1s, 2s, ...
            if attempt < self._max_retries:
                await asyncio.sleep(0.5 * (2 ** attempt))

        raise last_error or LLMResponseError("LLM 调用失败：未知错误")

    async def _anthropic_chat_completion(
        self,
        system: str,
        user_prompt: str,
        *,
        json_mode: bool = False,
        agent_label: str = "unknown",
        max_tokens: int | None = None,
    ) -> str:
        """Anthropic 原生协议（/v1/messages）适配

        - system 单独传参（不在 messages 里）
        - 鉴权用 x-api-key 头 + anthropic-version
        - 无 response_format 参数：JSON 模式在 system prompt 里追加强约束
        """
        url = f"{self._base_url.rstrip('/')}/messages"
        headers = self._build_headers(None)
        payload: dict[str, Any] = {
            "model": self._model,
            "system": (f"{system}\n\n只输出合法 JSON，不要输出任何其他内容。" if json_mode else system),
            "messages": [{"role": "user", "content": user_prompt}],
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "temperature": self._temperature,
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._get_http_client()
                resp = await client.post(url, headers=headers, json=payload)

                if resp.status_code != 200:
                    raise LLMResponseError(
                        f"HTTP {resp.status_code}: {resp.text[:500]}",
                        status_code=resp.status_code,
                    )

                data = resp.json()
                # Anthropic 响应：{"content": [{"type": "text", "text": "..."}], ...}
                text_parts = [
                    block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
                ]
                content = "".join(text_parts).strip()
                if not content:
                    raise LLMResponseError("LLM 返回空内容")

                self._record_usage(
                    agent_label, data.get("usage"), finished=data.get("stop_reason") != "max_tokens"
                )
                if data.get("stop_reason") == "max_tokens":
                    raise LLMResponseError(
                        f"LLM 输出被 max_tokens={payload['max_tokens']} 截断（stop_reason=max_tokens）"
                    )
                return content

            except (httpx.TimeoutException, httpx.HTTPError) as e:
                last_error = LLMResponseError(f"网络异常: {type(e).__name__}: {e}")
            except (KeyError, json.JSONDecodeError) as e:
                last_error = LLMResponseError(f"响应解析失败: {type(e).__name__}: {e}")
            except LLMResponseError as e:
                if _is_client_error(e):
                    raise
                last_error = e

            if attempt < self._max_retries:
                await asyncio.sleep(0.5 * (2 ** attempt))

        raise last_error or LLMResponseError("LLM 调用失败：未知错误")

    def _record_usage(self, agent_label: str, usage: dict[str, Any] | None, *, finished: bool = True) -> None:
        """记录 token 用量到 Prometheus（成本可观测）并暂存最近一次用量"""
        self.last_usage: dict[str, Any] = {
            "model": self._model,
            "prompt_tokens": (usage or {}).get("prompt_tokens") or (usage or {}).get("input_tokens") or 0,
            "completion_tokens": (usage or {}).get("completion_tokens") or (usage or {}).get("output_tokens") or 0,
            "truncated": not finished,
        }
        try:
            from .metrics import LLM_CALLS_TOTAL, LLM_TOKENS_TOTAL

            u = self.last_usage
            if u["prompt_tokens"]:
                LLM_TOKENS_TOTAL.labels(agent=agent_label, model=self._model, kind="prompt").inc(u["prompt_tokens"])
            if u["completion_tokens"]:
                LLM_TOKENS_TOTAL.labels(agent=agent_label, model=self._model, kind="completion").inc(
                    u["completion_tokens"]
                )
            LLM_CALLS_TOTAL.labels(agent=agent_label, model=self._model, outcome="success").inc()
        except Exception:
            pass  # 指标失败不影响业务调用

    # ------------------------------------------------------------
    # JSON 输出 + Pydantic 校验
    # ------------------------------------------------------------
    async def chat_json(
        self,
        system: str,
        user_prompt: str,
        schema: type[T],
        *,
        agent_label: str = "unknown",
        max_tokens: int | None = None,
    ) -> T:
        """调用 LLM 并返回 JSON 对象（通过 Pydantic schema 校验）

        Args:
            system: System prompt（应明确告知 LLM 输出 JSON 格式）
            user_prompt: User message
            schema: Pydantic BaseModel 子类，用于校验响应
            agent_label: 调用方 Agent 名（token 计量）
            max_tokens: 输出上限覆盖

        Returns:
            T: schema 实例

        Raises:
            LLMError: 任何 LLM 调用或校验失败
        """
        content = await self.chat_completion(
            system, user_prompt, json_mode=True, agent_label=agent_label, max_tokens=max_tokens
        )

        # 提取 JSON（容忍 LLM 在 JSON 前后加文字说明）
        json_str = self._extract_json(content)
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise LLMResponseError(
                f"JSON 解析失败: {e}；原始内容前 500 字符: {content[:500]!r}"
            ) from e

        try:
            return schema.model_validate(data)
        except ValidationError as e:
            raise LLMResponseError(
                f"Schema 校验失败 ({schema.__name__}): {e}"
            ) from e

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------
    def _build_headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_scheme:
            headers[self._auth_header] = f"{self._auth_scheme} {self._api_key}"
        else:
            headers[self._auth_header] = self._api_key
        # Anthropic 强制要求 anthropic-version 头
        if self._provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _extract_json(content: str) -> str:
        """从 LLM 输出中提取 JSON —— 处理 ```json fenced 代码块"""
        s = content.strip()
        # 处理 ```json ... ``` 代码块
        if s.startswith("```"):
            lines = s.splitlines()
            # 跳过首行 ```json 和尾行 ```
            body = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
            return body.strip()
        # 处理 ```json ... ``` （单行包裹的情况）
        if "```json" in s:
            start = s.find("```json") + len("```json")
            end = s.rfind("```")
            if end > start:
                return s[start:end].strip()
        return s

    def hash_prompt(self, system: str, user_prompt: str) -> str:
        """计算 prompt 的 hash（用于审计 ai_prompt_hash 字段）"""
        canonical = json.dumps(
            {"system": system, "user": user_prompt, "model": self._model},
            ensure_ascii=False,
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------
    # Embedding API（语义检索）
    # ------------------------------------------------------------
    @property
    def embedding_model_name(self) -> str:
        """当前 embedding 模型名（按 provider 选择默认）"""
        embedding_defaults = {
            "glm": "embedding-3",
            "qwen": "text-embedding-v3",
            "openai": "text-embedding-3-small",
            "azure": "text-embedding-3-small",
            "anthropic": "",  # Anthropic 暂不提供 embedding API
        }
        if self._provider in embedding_defaults:
            return embedding_defaults[self._provider]
        return ""

    @property
    def supports_embeddings(self) -> bool:
        """是否支持 embedding API（Anthropic 暂不支持）"""
        return self.is_configured and bool(self.embedding_model_name)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本向量化

        Args:
            texts: 待向量化的文本列表（建议单次 ≤ 64 条）

        Returns:
            list[list[float]]: 与 texts 等长的向量列表

        Raises:
            LLMConfigError: 未配置或 provider 不支持 embedding
            LLMResponseError: HTTP 失败 / 响应解析失败
        """
        if not self.supports_embeddings:
            raise LLMConfigError(
                f"Embedding 不可用：provider={self._provider!r}，"
                f"embedding_model={self.embedding_model_name!r}"
            )
        if not texts:
            return []

        url = f"{self._base_url.rstrip('/')}/embeddings"
        headers = self._build_headers(None)
        payload = {
            "model": self.embedding_model_name,
            "input": texts,
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._get_http_client()
                resp = await client.post(url, headers=headers, json=payload)

                if resp.status_code != 200:
                    raise LLMResponseError(f"HTTP {resp.status_code}: {resp.text[:500]}", status_code=resp.status_code)

                data = resp.json()
                # OpenAI 兼容协议：{"data": [{"embedding": [...]}, ...]}
                embeddings = [item["embedding"] for item in data["data"]]
                if len(embeddings) != len(texts):
                    raise LLMResponseError(
                        f"embedding 数量不匹配：输入 {len(texts)} 条，返回 {len(embeddings)} 条"
                    )
                return embeddings

            except (httpx.TimeoutException, httpx.HTTPError) as e:
                last_error = LLMResponseError(f"网络异常: {type(e).__name__}: {e}")
            except (KeyError, json.JSONDecodeError, IndexError) as e:
                last_error = LLMResponseError(f"响应解析失败: {type(e).__name__}: {e}")
            except LLMResponseError as e:
                if _is_client_error(e):
                    raise
                last_error = e

            if attempt < self._max_retries:
                await asyncio.sleep(0.5 * (2 ** attempt))

        raise last_error or LLMResponseError("Embedding 调用失败：未知错误")

    async def embed_text(self, text: str) -> list[float]:
        """单条文本向量化（embed_texts 的便捷封装）"""
        results = await self.embed_texts([text])
        return results[0]


# ============================================================
# 模块级单例
# ============================================================
llm_client = LLMClient()


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例"""
    return llm_client


def get_debate_client() -> LLMClient | None:
    """获取多 Agent 博弈用的 secondary LLM 客户端

    从 settings.llm_debate_* 配置构造独立实例（不同 provider/model/temperature）。
    用于 Improvement 5：Reasoner 并行博弈。

    Returns:
        LLMClient 或 None：secondary 未配置时返回 None（调用方应降级到单 Reasoner）
    """
    if not settings.llm_debate_provider or not settings.llm_debate_api_key:
        return None
    return LLMClient.from_override(
        provider=settings.llm_debate_provider,
        api_key=settings.llm_debate_api_key,
        model=settings.llm_debate_model or None,
        temperature=settings.llm_debate_temperature,
    )
