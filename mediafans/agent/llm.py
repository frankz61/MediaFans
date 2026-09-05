"""让模型在「已探测过的事实」上做最终裁决。

刻意的设计边界：模型只看探测得到的客观事实（几集、多大、什么画质、缺哪几集），
不看 URL、也不负责判断链接死活——那是探测层的事，模型判断不了，硬让它判断就会瞎编。
模型解决的是规则搞不定的部分：别名、译名、合集里混了别的剧、季号写法混乱。

任何一步出问题都返回 None，让上层退回确定性排序——AI 是增强，不是依赖。

**关于兼容性**：走官方 Anthropic API 时，system 和 output_config 都生效；
但很多自建网关只是「长得像 Anthropic」——实测某网关会直接丢掉 system 参数、
也不理会 output_config 的 json_schema（返回 markdown 包着的、字段名自创的 JSON）。
所以这里的写法是：规则同时写进 system 和 user 消息，JSON 用容错解析。
两边都能跑，不依赖任何一方的高级特性。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

DEFAULT_MODEL = "claude-opus-5"

_RULES = """你在帮一个网盘自动追剧工具挑选资源。

下面给你用户想看的剧，和若干个**已经实际打开验证过**的候选资源事实摘要。
这些事实是探测得到的，不是猜的：playable 是真实可播放文件数，episodes 是从文件名
解析出的集号范围，missing 是中间缺的集，multi_season 表示这个包混装了多季
（混装时集号是跨季合并的，完整度不可靠），junk 是引流广告文件数。

判断哪个候选**确实是用户想要的那部剧**，并且完整度和质量最合适。

重点看这些规则搞不定的情况：
- 标题像但其实是另一个作品（同名电影 / 前传 / 纪录片 / 花絮合集）
- 合集里混装了多部作品，不是用户要的那一部
- 译名、别名、原名不一致
- 季号表述混乱（「第二部」「Part 2」「S02」未必指同一季）

**相关性永远优先于画质**。所有候选都对不上就把 reject 设为 true，不要硬挑。

只输出一个 JSON 对象，不要有任何其他文字、不要用 markdown 代码块包裹，字段固定为：
{"index": 选中候选的序号（整数，从0开始；reject为true时填-1),
 "reject": 布尔,
 "reason": "一句中文说明，要具体（提到集数/画质/为什么排除了别的）",
 "confidence": "high" | "medium" | "low"}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "reject": {"type": "boolean"},
        "reason": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["index", "reject", "reason", "confidence"],
    "additionalProperties": False,
}

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Optional[dict]:
    """从模型输出里抠出 JSON 对象。

    自建网关不保证结构化输出，实测会返回 ```json 包裹的内容，
    所以这里依次尝试：直接解析 -> 去掉代码块围栏 -> 抓第一个花括号块。
    """
    text = (text or "").strip()
    for candidate in _candidates(text):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _candidates(text: str):
    yield text
    m = _FENCE.search(text)
    if m:
        yield m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        yield text[start:end + 1]


@dataclass
class Verdict:
    index: int
    reject: bool
    reason: str
    confidence: str


class LLMPicker:
    """可选的 AI 裁决。没装 SDK / 没配 key 时 available 为 False。"""

    def __init__(self, api_key: str = "", model: str = DEFAULT_MODEL,
                 base_url: str = "", timeout: float = 90.0, client=None):
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self.base_url = (base_url or "").rstrip("/")
        self._client = client
        self._why_unavailable = ""
        if client is None:
            self._client = self._build(api_key)

    def _build(self, api_key: str):
        try:
            import anthropic
        except ImportError:
            self._why_unavailable = "未安装 anthropic（pip install 'mediafans[ai]'）"
            return None
        kwargs = {"timeout": self.timeout}
        if api_key:
            kwargs["api_key"] = api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            return anthropic.Anthropic(**kwargs)
        except Exception as e:
            self._why_unavailable = f"Anthropic 客户端初始化失败: {str(e)[:100]}"
            return None

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._why_unavailable

    def pick(self, want: dict, candidates: List[dict]) -> Optional[Verdict]:
        """从候选里挑一个。出任何问题都返回 None，让上层用确定性排序兜底。"""
        if not self._client or not candidates:
            return None
        payload = {
            "想看的": want,
            "候选（已实际打开验证）": [dict(c, index=i) for i, c in enumerate(candidates)],
        }
        # 规则同时放进 system 和 user：官方 API 认 system，自建网关只认 user
        user_text = _RULES + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=1)
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=_RULES,
                messages=[{"role": "user", "content": user_text}],
                # 官方 API 会据此保证结构化输出；网关会忽略，所以不能依赖它，
                # 下面用容错解析兜底
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except Exception:
            return None

        data = extract_json(text)
        if not data:
            return None
        try:
            idx = int(data.get("index", -1))
            reject = bool(data.get("reject"))
            if not reject and not (0 <= idx < len(candidates)):
                return None  # 越界的序号不能信
            conf = str(data.get("confidence") or "medium").lower()
            return Verdict(
                index=idx,
                reject=reject,
                reason=str(data.get("reason") or "")[:200],
                confidence=conf if conf in ("high", "medium", "low") else "medium",
            )
        except (TypeError, ValueError):
            return None
