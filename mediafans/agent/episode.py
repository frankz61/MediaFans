"""从文件名里抠出剧集信息。

网盘资源的文件名没有统一规范，中英混杂、季集写法五花八门，
这里只做"能识别就识别、识别不了不猜"的保守解析——
猜错一个集号会让整季的完整度判断全错，宁可返回 None。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# 季集：S01E02 / s1e2 / 1x02 / 第1季第2集 / EP02 / E02 / 第02集
_SE_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee][Pp]?(\d{1,3})"),      # S01E02, S01EP02
    re.compile(r"(?<!\d)(\d{1,2})[Xx](\d{2,3})(?!\d)"),           # 1x02
    re.compile(r"第\s*(\d{1,2})\s*季.*?第\s*(\d{1,3})\s*[集话話]"),  # 第1季第2集
)
# 只有集号没有季号
_EP_ONLY = (
    re.compile(r"第\s*(\d{1,3})\s*[集话話]"),
    re.compile(r"(?<![A-Za-z0-9])[Ee][Pp](\d{1,3})(?!\d)"),
    re.compile(r"(?<![A-Za-z0-9])[Ee](\d{2,3})(?!\d)"),
)
_SEASON_ONLY = (
    re.compile(r"[Ss](?:eason)?[\s._-]*(\d{1,2})(?![\dEePp])"),
    re.compile(r"第\s*(\d{1,2})\s*季"),
)

def _tag(pattern: str):
    """按「前后不是英数字」界定边界，而不是 \\b。

    中文字符在 Python 正则里算 \\w，所以 `1080p中英字幕` 里 p 和「中」之间**没有**
    词边界，用 \\b 会整个漏掉——而中文网盘文件名里这种写法极常见（实测漏过一次）。
    """
    return re.compile(rf"(?<![0-9a-z]){pattern}(?![0-9a-z])", re.I)


# 画质：按从高到低，值越大越好
_QUALITY = (
    (_tag(r"(?:4k|2160[pi]|uhd)"), 2160),
    (_tag(r"1440[pi]"), 1440),
    (_tag(r"(?:1080[pi]|fhd)"), 1080),
    (_tag(r"(?:720[pi]|hd)"), 720),
    (_tag(r"(?:480[pi]|576[pi]|sd)"), 480),
)
# 片源，越靠前越好
_SOURCE = (
    (re.compile(r"remux", re.I), "remux"),
    (re.compile(r"blu-?ray|bdrip|bdremux", re.I), "bluray"),
    (re.compile(r"web-?dl|webrip", re.I), "webdl"),
    (re.compile(r"hdtv", re.I), "hdtv"),
    (re.compile(r"(?<![0-9a-z])ts(?![0-9a-z])|枪版|抢先版", re.I), "cam"),
)
_CHINESE_SUB = re.compile(
    r"中字|中文字幕|简中|繁中|简体|繁体|双语|内封|内嵌|国语|国配|chs|cht|zh-?cn", re.I
)


@dataclass
class EpisodeInfo:
    """从一个文件名解析出来的信息。解析不出的字段保持 None/默认值。"""

    name: str
    season: Optional[int] = None
    episode: Optional[int] = None
    height: int = 0          # 2160 / 1080 / ...，0 = 未知
    source: str = ""         # remux / bluray / webdl / hdtv / cam
    chinese_sub: bool = False

    @property
    def key(self) -> Optional[Tuple[int, int]]:
        """(季, 集)，用于去重和排序；集号缺失则没有 key。"""
        if self.episode is None:
            return None
        return (self.season if self.season is not None else 1, self.episode)


def parse_episode(name: str) -> EpisodeInfo:
    """解析单个文件名。识别不出的部分留空，绝不瞎猜。"""
    info = EpisodeInfo(name=name or "")
    text = info.name

    for pat in _SE_PATTERNS:
        m = pat.search(text)
        if m:
            info.season, info.episode = int(m.group(1)), int(m.group(2))
            break
    else:
        for pat in _EP_ONLY:
            m = pat.search(text)
            if m:
                info.episode = int(m.group(1))
                break
        for pat in _SEASON_ONLY:
            m = pat.search(text)
            if m:
                info.season = int(m.group(1))
                break

    for pat, height in _QUALITY:
        if pat.search(text):
            info.height = height
            break
    for pat, tag in _SOURCE:
        if pat.search(text):
            info.source = tag
            break
    info.chinese_sub = bool(_CHINESE_SUB.search(text))
    return info


@dataclass
class SeasonSummary:
    """一批文件整体看下来是什么货色。"""

    episodes: List[int]           # 去重后的集号，升序
    seasons: List[int]            # 出现过的季号
    height: int = 0               # 这批文件里最常见的画质
    source: str = ""
    chinese_sub: bool = False
    unparsed: int = 0             # 有多少个文件没解析出集号

    @property
    def count(self) -> int:
        return len(self.episodes)

    @property
    def contiguous(self) -> bool:
        """集号连不连续——中间缺集是资源不完整的强信号。"""
        if len(self.episodes) < 2:
            return True
        return self.episodes == list(range(self.episodes[0], self.episodes[-1] + 1))

    @property
    def missing(self) -> List[int]:
        if len(self.episodes) < 2:
            return []
        full = set(range(self.episodes[0], self.episodes[-1] + 1))
        return sorted(full - set(self.episodes))


# 裸编号文件名：`01.mp4`、`02x.mkv`、`5.ts`。
# 数字之外最多带 2 个杂字符，否则 `4K 高码.mkv`、`2024.IMAX.2160p.mkv` 都会被误认。
_BARE_NUM = re.compile(r"^\D{0,2}(\d{1,3})\D{0,2}$")
# 一个目录里至少这么多个裸编号文件，才认为「这些数字是集号」
_BARE_MIN = 3


def bare_episode(name: str) -> Optional[int]:
    """单个文件名去掉扩展名后是不是「就是一个编号」。是就返回那个数。"""
    stem = (name or "").rsplit(".", 1)[0].strip()
    m = _BARE_NUM.match(stem)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 200 else None


def infer_bare_numbering(names: List[str]) -> Dict[str, int]:
    """一批文件名里的裸数字是不是集号。返回 {文件名: 集号}。

    `01.mp4`、`02x.mkv` 这种在中文网盘分享里非常常见——实测
    《异人之下之决战！碧游村》整季 13 集就是 `01x.mkv`…`13x.mkv`，
    《异人之下》剧版 27 集是 `01.mp4`…`27.mp4`。parse_episode 一个都认不出，
    结果是「5 个资源可用，但一集都补不上」。

    单看一个 `01.mp4`，那个 1 可能是任何东西，所以不能按单个文件猜；
    但**一个目录里躺着十几个只有编号的视频**，那就是集号。所以按批判断：
    够多、且编号互不重复，才认。重复就说明那些数字是别的意思（画质、分P、年份）。
    """
    hit = {n: e for n in names for e in [bare_episode(n)] if e is not None}
    if len(hit) < _BARE_MIN:
        return {}
    nums = list(hit.values())
    if len(set(nums)) != len(nums):
        return {}
    return hit


def summarize(names: List[str]) -> SeasonSummary:
    """把一批文件名归纳成整季概况。"""
    infos = [parse_episode(n) for n in names or []]
    eps = sorted({i.episode for i in infos if i.episode is not None})
    seasons = sorted({i.season for i in infos if i.season is not None})
    heights = [i.height for i in infos if i.height]
    sources = [i.source for i in infos if i.source]
    return SeasonSummary(
        episodes=eps,
        seasons=seasons,
        # 取众数而不是最大值：一堆 1080p 里混一个 4K 花絮不该把整季算成 4K
        height=max(set(heights), key=heights.count) if heights else 0,
        source=max(set(sources), key=sources.count) if sources else "",
        chinese_sub=any(i.chinese_sub for i in infos),
        unparsed=sum(1 for i in infos if i.episode is None),
    )
