from __future__ import annotations

from typing import List, Optional

import httpx

from ..errors import SearchError
from ..models import ShareLink

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class SearchProvider:
    """搜索源抽象：输入关键词，输出标准化 ShareLink 列表."""

    name: str = ""
    default_base: str = ""

    def __init__(self, cfg: dict, timeout: float = 15.0, transport=None):
        raw = cfg.get("base_url") or self.default_base
        urls = [raw] if isinstance(raw, str) else list(raw or [])
        urls += list(cfg.get("fallback_urls") or [])
        # 公共实例经常抽风（实测同一关键词三次分别返回 13 / 74 / 0 条），
        # 所以允许配多个地址，前一个失败或没结果就换下一个
        self.base_urls = [str(u).rstrip("/") for u in urls if str(u or "").strip()]
        self.base_url = self.base_urls[0] if self.base_urls else ""
        self.timeout = float(cfg.get("timeout") or timeout)
        self.transport = transport
        if not self.base_urls:
            raise SearchError(f"搜索源 {self.name} 未配置 base_url")

    def _fetch_one(self, base: str, path: str, params: dict) -> dict:
        with httpx.Client(
            headers={"user-agent": BROWSER_UA, "accept": "application/json"},
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=True,
        ) as c:
            r = c.get(f"{base}{path}", params=params)
            r.raise_for_status()
            return r.json()

    def _get_json(self, path: str, params: dict, is_empty=None) -> dict:
        """依次尝试配置的地址；报错或（可选地）结果为空都换下一个。

        is_empty(body) -> bool：用来判断「通了但没查到东西」，这种情况也值得换实例，
        因为公共实例返回 0 条往往是它自己缓存/限流的问题，不是真没资源。
        """
        problems = []
        last_body = None
        for i, base in enumerate(self.base_urls):
            try:
                body = self._fetch_one(base, path, params)
            except httpx.HTTPError as e:
                problems.append(f"{base} 请求失败: {e}")
                continue
            except ValueError as e:
                problems.append(f"{base} 返回非 JSON: {e}")
                continue
            last_body = body
            if is_empty is not None and is_empty(body) and i < len(self.base_urls) - 1:
                problems.append(f"{base} 无结果")
                continue
            return body
        if last_body is not None:
            return last_body  # 全都没结果，返回最后一个的空结果而不是报错
        raise SearchError(f"搜索源 {self.name} 不可用（{'; '.join(problems)}）")

    def search(self, kw: str, netdisk: Optional[str] = None) -> List[ShareLink]:
        raise NotImplementedError

    def health(self) -> bool:
        return True
