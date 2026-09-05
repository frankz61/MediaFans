from __future__ import annotations

import datetime
from typing import List, Optional

import httpx

from ..errors import ConfigError
from ..models import MediaItem


class TmdbClient:
    """TMDB 元数据客户端：热门/搜索影视，用于'最近有什么可看'."""

    BASE = "https://api.themoviedb.org/3"
    # TMDB 把华语拆成 zh（国语）和 cn（粤语）两个码，只筛 zh 会漏掉整个港片库
    CHINESE = ("zh", "cn")
    CHINESE_FILTER = "zh|cn"
    # TMDB 的「动画」类型 id。同名不同作里，动画版和真人版是最常撞的一对
    # （《一人之下》动画 / 《异人之下》真人），这个 id 是最硬的区分依据。
    ANIMATION_GENRE = 16

    def __init__(self, api_key: str, timeout: float = 15.0, transport=None):
        if not api_key:
            raise ConfigError("未配置 tmdb.api_key（themoviedb.org 免费申请）")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.transport = transport

    def _get(self, path: str, params: dict) -> List[dict]:
        params = dict(params or {})
        params.setdefault("language", "zh-CN")
        headers = {}
        if self.api_key.startswith("eyJ"):  # v4 Bearer Token
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params.setdefault("api_key", self.api_key)
        with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
            r = c.get(f"{self.BASE}{path}", params=params, headers=headers)
            r.raise_for_status()
            body = r.json()
        return body.get("results") or []

    IMG = "https://image.tmdb.org/t/p"

    @classmethod
    def poster_url(cls, path: str, size: str = "w342") -> str:
        return f"{cls.IMG}/{size}{path}" if path else ""

    @staticmethod
    def _to_item(it: dict) -> MediaItem:
        media_type = it.get("media_type") or ("tv" if it.get("name") is not None else "movie")
        date = it.get("release_date") or it.get("first_air_date") or ""
        return MediaItem(
            title=it.get("title") or it.get("name") or "",
            original_title=it.get("original_title") or it.get("original_name") or "",
            original_language=it.get("original_language") or "",
            genres=[int(g) for g in (it.get("genre_ids") or []) if str(g).isdigit()],
            media_type=media_type if media_type in ("movie", "tv") else "movie",
            year=(date or "")[:4],
            rating=round(float(it.get("vote_average") or 0), 1),
            overview=it.get("overview") or "",
            tmdb_id=int(it.get("id") or 0),
            poster=TmdbClient.poster_url(it.get("poster_path") or ""),
            backdrop=TmdbClient.poster_url(it.get("backdrop_path") or "", "w780"),
        )

    @classmethod
    def is_animation(cls, item: MediaItem) -> bool:
        """动画还是真人。同名不同作里这是最硬的区分依据。"""
        return cls.ANIMATION_GENRE in (item.genres or [])

    @classmethod
    def is_chinese(cls, item: MediaItem) -> bool:
        return (item.original_language or "").lower() in cls.CHINESE

    @staticmethod
    def _cn_date(offset_days: int = 0) -> str:
        """中国时区的日期。「今日播出」得按国内的今天算，不是 UTC 的今天。"""
        now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
        return (now.date() + datetime.timedelta(days=offset_days)).isoformat()

    def chinese_tv(self, kind: str = "airing", page: int = 1) -> List[MediaItem]:
        """华语剧集榜。

        /tv/airing_today 这类榜单是全球榜，被各国日播肥皂剧刷屏——实测「今日播出」
        前 8 条里只有 1 条华语，其余是西班牙、德国、荷兰、南非的日播节目。
        TMDB 的榜单端点不支持按语言过滤，只有 /discover/tv 支持，所以华语这一档
        走 discover 单独取（今日播出 43 部、一周在播 153 部，够铺满一屏）。
        """
        params = {"with_original_language": self.CHINESE_FILTER,
                  "sort_by": "popularity.desc", "page": page}
        if kind == "airing":
            params["air_date.gte"] = params["air_date.lte"] = self._cn_date()
        elif kind == "onair":
            params["air_date.gte"] = self._cn_date()
            params["air_date.lte"] = self._cn_date(7)
        else:
            # 华语热度榜按 popularity 排会顶上来一堆没人评分的擦边条目
            # （实测前三有两条是这种）。要「已经热起来的」就得有评分基数；
            # 这个门槛只加在热门上——今日播出/一周在播是新番，本来就没票数。
            params["vote_count.gte"] = 10
        return [self._to_item(dict(x, media_type="tv"))
                for x in self._get("/discover/tv", params)]

    def trending(self, media_type: str = "all", window: str = "week") -> List[MediaItem]:
        media_type = media_type if media_type in ("all", "movie", "tv") else "all"
        items = [self._to_item(x) for x in self._get(f"/trending/{media_type}/{window}", {})]
        return items

    def airing_today(self, page: int = 1) -> List[MediaItem]:
        """今天播出的剧集——「每日最新」用这个最贴切."""
        return [self._to_item(dict(x, media_type="tv"))
                for x in self._get("/tv/airing_today", {"page": page})]

    def on_the_air(self, page: int = 1) -> List[MediaItem]:
        """未来一周内有更新的在播剧集."""
        return [self._to_item(dict(x, media_type="tv"))
                for x in self._get("/tv/on_the_air", {"page": page})]

    def popular_tv(self, page: int = 1) -> List[MediaItem]:
        return [self._to_item(dict(x, media_type="tv"))
                for x in self._get("/tv/popular", {"page": page})]

    def tv_detail(self, tmdb_id: int) -> dict:
        """拿剧集的季/集数——判断资源完不完整要用这个做基准."""
        params = {}
        headers = {}
        if self.api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        params["language"] = "zh-CN"
        with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
            r = c.get(f"{self.BASE}/tv/{int(tmdb_id)}", params=params, headers=headers)
            r.raise_for_status()
            body = r.json()
        seasons = [
            {"season": int(s.get("season_number") or 0),
             "episodes": int(s.get("episode_count") or 0),
             "name": s.get("name") or "",
             "air_date": s.get("air_date") or ""}
            for s in (body.get("seasons") or [])
            if int(s.get("season_number") or 0) > 0        # 第 0 季是特别篇
        ]
        genres = [int(g.get("id") or 0) for g in (body.get("genres") or [])]
        return {
            "tmdb_id": int(body.get("id") or 0),
            "title": body.get("name") or "",
            "original_title": body.get("original_name") or "",
            "genres": genres,
            "animation": self.ANIMATION_GENRE in genres,
            "year": (body.get("first_air_date") or "")[:4],
            "total_episodes": int(body.get("number_of_episodes") or 0),
            "total_seasons": int(body.get("number_of_seasons") or 0),
            "seasons": seasons,
            "status": body.get("status") or "",
        }

    def season_detail(self, tmdb_id: int, season: int) -> dict:
        """某一季的权威集列表——判断「缺哪几集」要以这个为准，不能靠文件名猜。"""
        params, headers = {"language": "zh-CN"}, {}
        if self.api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
            r = c.get(f"{self.BASE}/tv/{int(tmdb_id)}/season/{int(season)}",
                      params=params, headers=headers)
            r.raise_for_status()
            body = r.json()
        return {
            "season": int(body.get("season_number") or season),
            "name": body.get("name") or "",
            "air_date": body.get("air_date") or "",
            "episodes": [{
                "episode": int(e.get("episode_number") or 0),
                "name": e.get("name") or "",
                "air_date": e.get("air_date") or "",
                "runtime": e.get("runtime") or 0,
                "overview": e.get("overview") or "",
                "still": TmdbClient.poster_url(e.get("still_path") or "", "w300"),
            } for e in (body.get("episodes") or [])],
        }

    def search(self, query: str, media_type: str = "multi",
               prefer_chinese: bool = True) -> List[MediaItem]:
        media_type = media_type if media_type in ("multi", "movie", "tv") else "multi"
        items = [self._to_item(x) for x in self._get(f"/search/{media_type}", {"query": query})]
        if media_type == "multi":
            items = [i for i in items if i.media_type in ("movie", "tv")]
        return self.rank_chinese_first(query, items) if prefer_chinese else items

    @classmethod
    def rank_chinese_first(cls, query: str, items: List[MediaItem]) -> List[MediaItem]:
        """华语优先，但**完全同名的先保住**。

        中文查询下 TMDB 自己的相关性大多已经把华语排在前面（三体、狂飙、流浪地球
        都不用动），真正需要纠正的是同名撞车：搜「无间道」，斯科塞斯的《无间道风云》
        排在港版《无间道》前面；搜「木兰」，迪士尼版盖住华语版。

        但不能简单按语言排：张艺谋的《长城》在 TMDB 里 original_language 是 en，
        硬把华语提前会让《巨兵长城传》顶掉正主。所以先按「标题和查询词完全一致」
        分档，同档之内才华语优先，档内保持 TMDB 的原始顺序。
        """
        want = (query or "").strip().lower()

        def key(pair):
            i, item = pair
            exact = (item.title or "").strip().lower() == want or \
                    (item.original_title or "").strip().lower() == want
            return (not exact, not cls.is_chinese(item), i)

        return [it for _, it in sorted(enumerate(items), key=key)]

    def healthy(self) -> bool:
        return bool(self._get("/trending/movie/week", {}) is not None)
