from __future__ import annotations

from typing import List, Optional

from ..models import ShareLink
from ..utils import classify_netdisk
from .base import SearchProvider


class YisoProvider(SearchProvider):
    """易搜（yiso.work，非官方接口）.

    GET /api/search?name=xxx&pageNo=1 -> data[].fileList[].{fileName, url}
    """

    name = "yiso"

    def search(self, kw: str, netdisk: Optional[str] = None) -> List[ShareLink]:
        body = self._get_json("/api/search", {"name": kw, "pageNo": 1})
        groups = body.get("data") or []
        out: List[ShareLink] = []
        if not isinstance(groups, list):
            groups = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            for f in g.get("fileList") or []:
                if not isinstance(f, dict):
                    continue
                url = str(f.get("url") or "")
                if not url:
                    continue
                nd = classify_netdisk(url)
                if netdisk and nd != netdisk:
                    continue
                out.append(
                    ShareLink(
                        url=url,
                        netdisk=nd,
                        title=str(f.get("fileName") or ""),
                        source=self.name,
                    )
                )
        return out
