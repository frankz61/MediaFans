from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from .errors import ConfigError
from .utils import json_path_get


class TokenRelay:
    """从用户的 token 中转站实时获取网盘 cookie.

    中转站 API 各不相同，这里做成配置驱动:
      url:      中转站接口地址
      method:   HTTP 方法，默认 GET
      headers:  附加请求头（鉴权用）
      json_path: 响应 JSON 里 cookie 的路径，如 data.cookie；留空则把响应体当纯文本
    """

    def __init__(self, spec: Dict[str, Any], timeout: float = 15.0, transport=None):
        if not spec or not spec.get("url"):
            raise ConfigError("token_provider 配置不完整：缺少 url")
        self.url = str(spec["url"])
        self.method = str(spec.get("method") or "GET").upper()
        self.headers = dict(spec.get("headers") or {})
        self.json_path = spec.get("json_path") or ""
        self.timeout = float(spec.get("timeout") or timeout)
        self.transport = transport

    def fetch_cookie(self) -> str:
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
                resp = c.request(self.method, self.url, headers=self.headers)
                resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ConfigError(f"token 中转站请求失败 {self.url}: {e}")

        cookie = ""
        if self.json_path:
            try:
                body = resp.json()
            except ValueError:
                raise ConfigError(
                    f"token 中转站返回的不是 JSON，无法按 json_path={self.json_path} 提取"
                )
            val = json_path_get(body, self.json_path)
            cookie = val if isinstance(val, str) else ""
        else:
            cookie = resp.text.strip()

        cookie = cookie.strip().strip('"').strip("'")
        if not cookie or "=" not in cookie:
            raise ConfigError(
                "token 中转站返回的内容不像 cookie（应包含 k=v），请检查 json_path 配置"
            )
        return cookie
