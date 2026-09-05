from __future__ import annotations

import re
from typing import List, Optional

from ..models import ShareLink
from ..utils import classify_netdisk, normalize_netdisk
from .base import SearchProvider

_URL_IN_NOTE = re.compile(r"https?://\S+")


def _clean_note(note: str) -> str:
    """备注里常混入原始链接/提取码文本，去掉让展示干净."""
    cleaned = _URL_IN_NOTE.sub(" ", note)
    cleaned = re.sub(r"(提取码|密码)[:：]\s*\S+", " ", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _merged(body: dict) -> dict:
    """merged_by_type 在不同版本里可能在顶层，也可能包在 data 里."""
    merged = body.get("merged_by_type")
    if merged is None and isinstance(body.get("data"), dict):
        merged = body["data"].get("merged_by_type")
    return merged if isinstance(merged, dict) else {}


class PanSouProvider(SearchProvider):
    """PanSou 聚合搜索（github.com/fish2018/pansou，可 docker 自部署）.

    GET /api/search?kw=xxx&res=merge -> merged_by_type.{netdisk: [{url, password, note, ...}]}

    服务端本身就聚合了几十个 TG 频道和插件，所以「源更丰富」主要靠把这些开关用起来，
    而不是在客户端堆更多站点。可在 config 的 source 里配：
      cloud_types  只要指定网盘（服务端过滤，省得拿回一堆转存不了的）
      src          all | tg | plugin
      channels     指定 TG 频道（逗号分隔或列表），留空用服务端默认
      plugins      指定插件
      refresh      true 则跳过服务端缓存
      conc         并发度
    """

    name = "pansou"
    # 只有夸克能转存，默认就让服务端只回夸克，省带宽也省得列表里全是点不动的
    _PASSTHROUGH = ("src", "channels", "plugins", "refresh", "conc", "res", "cloud_types")

    def __init__(self, cfg: dict, timeout: float = 15.0, transport=None):
        super().__init__(cfg, timeout=timeout, transport=transport)
        self.extra = {}
        for key in self._PASSTHROUGH:
            val = cfg.get(key)
            if val in (None, "", []):
                continue
            self.extra[key] = ",".join(str(x) for x in val) if isinstance(val, (list, tuple)) \
                else str(val).lower() if isinstance(val, bool) else str(val)

    def search(self, kw: str, netdisk: Optional[str] = None) -> List[ShareLink]:
        params = {"kw": kw, "res": "merge"}
        params.update(self.extra)
        # 调用方指定了网盘就交给服务端过滤：同样一次请求，能用的结果多得多
        if netdisk and "cloud_types" not in self.extra:
            params["cloud_types"] = netdisk
        body = self._get_json(
            "/api/search", params,
            is_empty=lambda b: not any(v for v in _merged(b).values() if isinstance(v, list)),
        )
        merged = _merged(body)
        out: List[ShareLink] = []
        for raw_type, items in merged.items():
            nd = normalize_netdisk(str(raw_type))
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                url = str(it.get("url") or "")
                if not url:
                    continue
                final_nd = nd or classify_netdisk(url)
                if netdisk and final_nd != netdisk:
                    continue
                out.append(
                    ShareLink(
                        url=url,
                        netdisk=final_nd,
                        passcode=str(it.get("password") or ""),
                        note=_clean_note(str(it.get("note") or "")),
                        datetime=str(it.get("datetime") or ""),
                        source=str(it.get("source") or self.name),
                    )
                )
        return out

    def health(self) -> bool:
        try:
            body = self._get_json("/api/health", {})
            return bool(body.get("channels") or body.get("plugins") or body)
        except Exception:
            return False
