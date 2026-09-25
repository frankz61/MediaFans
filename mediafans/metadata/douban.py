from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import httpx

from ..models import MediaItem


@dataclass
class DoubanItem:
    """豆瓣榜单里的一条。只用来知道「华语现在在热什么」，作品本身仍以 TMDB 为准。"""

    douban_id: str
    title: str
    media_type: str = "tv"          # movie | tv
    year: str = ""
    rating: float = 0.0
    regions: List[str] = field(default_factory=list)

    CHINESE_REGIONS = ("中国大陆", "中国香港", "中国台湾", "中国澳门")

    @property
    def chinese(self) -> bool:
        # 没写地区的放行：只在混了外语片的榜（影院热映、即将上映）里才需要筛，
        # 那两个榜的条目都带地区
        return not self.regions or any(r in self.CHINESE_REGIONS for r in self.regions)


class DoubanClient:
    """豆瓣移动端的榜单接口。

    TMDB 的华语数据是「有，但不热」：它的 popularity 反映的是全球用户，
    国内正在追的剧、正在上映的国产片排不上来，综艺更是基本空白。
    豆瓣是国内的口径，所以华语段换成豆瓣，外语段仍用 TMDB。

    接口不用登录，但要带 Referer，否则 400。从国内服务器直连，不用走 TMDB 那条中转。
    """

    BASE = "https://m.douban.com/rexxar/api/v2"
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Referer": "https://m.douban.com/movie/",
    }

    def __init__(self, timeout: float = 10.0, transport=None):
        self.timeout = timeout
        self.transport = transport

    def _get(self, path: str, params: dict) -> dict:
        with httpx.Client(timeout=self.timeout, transport=self.transport,
                          headers=self.HEADERS) as c:
            r = c.get(f"{self.BASE}{path}", params=params)
            r.raise_for_status()
            return r.json()

    def collection(self, name: str, count: int = 50) -> List[DoubanItem]:
        """固定榜单：tv_domestic（近期热门国产剧）、show_domestic（国内综艺）、
        tv_chinese_best_weekly（华语口碑剧集榜）、movie_showing、movie_soon……"""
        body = self._get(f"/subject_collection/{name}/items",
                         {"start": 0, "count": count})
        return [self._to_item(x) for x in body.get("subject_collection_items") or []]

    def recent_hot(self, media: str = "movie", category: str = "热门",
                   kind: str = "华语", count: int = 50) -> List[DoubanItem]:
        """豆瓣选片页的「最近热门」，能按地区筛——华语热门电影没有现成的固定榜，只能走这个。"""
        body = self._get(f"/subject/recent_hot/{media}",
                         {"start": 0, "limit": count, "category": category, "type": kind})
        return [self._to_item(x) for x in body.get("items") or []]

    @staticmethod
    def _to_item(it: dict) -> DoubanItem:
        # card_subtitle 形如「2026 / 中国大陆 中国香港 / 剧情 犯罪 / 导演 / 主演」。
        # 口碑榜和 recent_hot 的条目没有 year 字段，年份只能从这里取
        parts = [p.strip() for p in (it.get("card_subtitle") or "").split("/")]
        year = str(it.get("year") or "")
        if not year and parts and re.fullmatch(r"\d{4}", parts[0]):
            year = parts[0]
        regions = parts[1].split() if len(parts) > 1 else []
        rating = (it.get("rating") or {}).get("value") or 0
        return DoubanItem(
            douban_id=str(it.get("id") or ""),
            title=(it.get("title") or "").strip(),
            media_type="movie" if it.get("type") == "movie" else "tv",
            year=year,
            rating=round(float(rating), 1),
            regions=regions,
        )


# ---------------------------------------------------------------- 对到 TMDB
# 点进作品页之后的一切（季、集、找资源、进度）都挂在 tmdb_id 上，
# 所以豆瓣条目得先对上 TMDB 才能用。对不上的只能丢掉——点进去也打不开。

_INVISIBLE = re.compile(r"[​-‏⁠﻿]")
_SEASON = re.compile(r"\s*第([一二三四五六七八九十\d]+)季\s*$")
# 综艺常见的两种写法：「歌手2026」按年份出，「一饭封神2」按续作号出。
# 只在前面是汉字时才剥，免得把「9-1-1」「1923」这种片名剥坏
_YEAR_TAIL = re.compile(r"(?<=[一-鿿])\s*(?:19|20)\d{2}$")
_NUM_TAIL = re.compile(r"(?<=[一-鿿])\s*(\d{1,2})$")
_CN_NUM = {c: i for i, c in enumerate("零一二三四五六七八九十")}


def _cn_to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if len(s) == 1:
        return _CN_NUM.get(s, 0)
    # 十一 / 二十 / 二十三
    tens, _, ones = s.partition("十")
    return (_CN_NUM.get(tens, 1) if tens else 1) * 10 + (_CN_NUM.get(ones, 0) if ones else 0)


def norm_title(s: str) -> str:
    """比较片名用：全角转半角、去空白和标点、小写。「年会不能停！2」≈「年会不能停!2」。"""
    s = unicodedata.normalize("NFKC", _INVISIBLE.sub("", s or ""))
    return re.sub(r"[\W_]+", "", s).lower()


def query_variants(item: DoubanItem) -> List[Tuple[str, Optional[int], bool]]:
    """去 TMDB 搜哪些名字：[(名字, 第几季, 要不要核对年份)]，按顺序试。

    豆瓣把每一季当成一个条目（「花儿与少年 第八季」），TMDB 是一部剧下挂多季，
    所以剧集要把季号剥出来。剥过季号的不核对年份——TMDB 的年份是第一季的首播年。
    """
    title = _INVISIBLE.sub("", item.title).strip()
    out = [(title, None, True)]
    if item.media_type != "tv":
        return out
    m = _SEASON.search(title)
    if m:
        out.append((title[:m.start()].strip(), _cn_to_int(m.group(1)) or None, False))
    elif _YEAR_TAIL.search(title):
        out.append((_YEAR_TAIL.sub("", title).strip(), None, False))
    else:
        m = _NUM_TAIL.search(title)
        if m and int(m.group(1)) >= 2:
            out.append((title[:m.start()].strip(), int(m.group(1)), False))
    return out


def pick_match(name: str, year: str, check_year: bool,
               candidates: List[MediaItem]) -> Optional[MediaItem]:
    """片名完全一致（中文名或原名）才算。宁可丢，不能对错——
    对错了点进去是另一部剧，比这一条不出现糟得多。"""
    want = norm_title(name)
    if not want:
        return None
    for c in candidates:
        if want not in (norm_title(c.title), norm_title(c.original_title)):
            continue
        if check_year and year and c.year:
            try:
                if abs(int(c.year) - int(year)) > 1:
                    continue
            except ValueError:
                pass
        return c
    return None
