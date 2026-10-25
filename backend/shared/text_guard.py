"""不可信文本进入 LLM prompt 前的清洗（提示注入防护）。

威胁模型：案例描述、证据摘要、时间线等字段来自用户输入与源系统 Connector，
可能携带"忽略以上指令 / 泄露系统提示"类注入文本，试图操纵 Reasoner/Skeptic
的调查结论。三层缓解：

1. 控制字符剥离 —— 防破坏 prompt 模板结构、隐藏指令；
2. 零宽/Bidi 字符剥离 —— 防不可见指令与视觉欺骗（Trojan Source 类攻击）；
3. 定界符中和 + fence() 数据区 —— 防逃逸出"数据区"边界。

配合使用：自由文本字段一律经 fence() 包裹进 prompt，system prompt 中
已声明数据区内容一律视为资料而非指令。sanitize 单独用于结构化行内插值
（Agent 输出回显等不适合整块包裹的场景）。
"""

from __future__ import annotations

import re

# 控制字符（保留 \n \t \r 之外的可见结构统一抹掉；\r 归一为空格防止行注入）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\r]")
# 零宽字符与 Bidi 控制符（\u202a-\u202e 方向重排、\u2060-\u2064 不可见连接符等）
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
# 定界符同形序列：3 个以上连续 < 或 > 一律压成 2 个，使 fence 无法被内容伪造
_FENCE_RE = re.compile(r"<{3,}|>{3,}")

DEFAULT_MAX_CHARS = 2000


def sanitize_untrusted(text: str | None, max_chars: int | None = DEFAULT_MAX_CHARS) -> str:
    """清洗不可信文本：剥离控制/不可见字符、中和定界符、截断超长。

    max_chars=None 表示不截断（用于调用方已逐行限长的整块数据区）。
    """
    if not text:
        return ""
    s = _CONTROL_RE.sub(" ", text)
    s = _INVISIBLE_RE.sub("", s)
    s = _FENCE_RE.sub(lambda m: m.group(0)[:2], s)
    s = s.strip()
    if max_chars is not None and len(s) > max_chars:
        s = s[:max_chars] + "…[内容已截断]"
    return s


def fenced(label: str, text: str | None, max_chars: int | None = DEFAULT_MAX_CHARS) -> str:
    """把不可信文本包进显式数据区，配合 system prompt 的"数据区非指令"规则使用。

    标签是受信常量，内容已经 sanitize —— 定界符不可能从数据区内部伪造。
    max_chars=None 表示不截断（用于调用方已逐行限长的整块数据区）。
    """
    body = sanitize_untrusted(text, max_chars)
    if not body:
        body = "（无）"
    return f"<<<{label}_数据开始>>>\n{body}\n<<<{label}_数据结束>>>"


# ============================================================
#  PII 掩码 —— 出站到外部 embedding API 前的脱敏
# ============================================================
# 只覆盖高置信度模式；业务标识（EMP-2187、批号 20240501A）不在掩码范围，
# 它们是调查工作的主键。掩码是单向的：只用于向量化/检索出站，不回写数据库。
_PII_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PII_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PII_TEL_RE = re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")
_PII_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 其余 13-19 位纯数字长串：银行卡 / 设备序列号等（证件号已被上一条优先命中）
_PII_LONG_NUM_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


def mask_pii(text: str | None) -> str:
    """掩码文本中的常见 PII：邮箱 / 手机号 / 座机 / 证件号 / 长数字串。"""
    if not text:
        return ""
    s = _PII_EMAIL_RE.sub("[EMAIL]", text)
    s = _PII_PHONE_RE.sub("[PHONE]", s)
    s = _PII_TEL_RE.sub("[PHONE]", s)
    s = _PII_ID_CARD_RE.sub("[ID_NUMBER]", s)
    s = _PII_LONG_NUM_RE.sub("[NUMBER]", s)
    return s
