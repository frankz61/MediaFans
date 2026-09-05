from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from ..errors import ConfigError
from ..models import ShareLink
from .base import SearchProvider
from .pansou import PanSouProvider
from .yiso import YisoProvider

_PROVIDERS = {"pansou": PanSouProvider, "yiso": YisoProvider}


def build_providers(cfg_section: dict, transport=None) -> List[SearchProvider]:
    specs = (cfg_section or {}).get("sources") or []
    if not specs:
        raise ConfigError(
            "未配置搜索源（search.sources）。运行 mediafans config init 生成配置模板。"
        )
    timeout = float((cfg_section or {}).get("timeout") or 15)
    providers = []
    for spec in specs:
        stype = str(spec.get("type") or "").lower()
        cls = _PROVIDERS.get(stype)
        if cls is None:
            raise ConfigError(
                f"未知搜索源类型 '{stype}'（支持: {', '.join(_PROVIDERS)}）"
            )
        providers.append(cls(spec, timeout=timeout, transport=transport))
    return providers


def aggregate_search(providers: List[SearchProvider], kw: str,
                     netdisk: Optional[str] = None) -> Tuple[List[ShareLink], List[Tuple[str, Exception]]]:
    """并发搜所有源，按 URL 去重，夸克结果排前面（当前只有夸克可转存）."""
    results: List[ShareLink] = []
    errors: List[Tuple[str, Exception]] = []

    def _run(p: SearchProvider):
        return p.search(kw, netdisk=netdisk)

    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as ex:
        futures = {ex.submit(_run, p): p for p in providers}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                results.extend(fut.result())
            except Exception as e:  # 单源失败不阻断整体
                errors.append((p.name, e))

    seen = set()
    unique: List[ShareLink] = []
    for r in results:
        key = r.url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    unique.sort(key=lambda r: (0 if r.netdisk == "quark" else 1, r.url))
    return unique, errors


__all__ = ["SearchProvider", "PanSouProvider", "YisoProvider", "build_providers", "aggregate_search"]
