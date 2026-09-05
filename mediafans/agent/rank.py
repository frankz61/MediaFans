"""给已探测过的候选打分排序。

只用探测拿到的事实（有几集、多大、什么画质），不猜。
这层没有 LLM 也能给出合理答案；LLM 那层是在这个排序之上再做一次裁决。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .probe import ProbeResult

# 画质档 -> 分
_HEIGHT_SCORE = {2160: 26, 1440: 22, 1080: 20, 720: 10, 480: 2}
# 片源 -> 分。REMUX 画质最好但体积大、浏览器解不了，日常观看未必最优，
# 所以给 web-dl 一个不低的分。
_SOURCE_SCORE = {"remux": 12, "bluray": 12, "webdl": 14, "hdtv": 6, "cam": -40}

_TOKEN_SPLIT = re.compile(r"[\s._\-\[\]()（）【】/、,，:：]+")
_YEAR = re.compile(r"(19|20)\d{2}")


@dataclass
class Scored:
    probe: ProbeResult
    score: float
    reasons: List[str]

    @property
    def title(self) -> str:
        return self.probe.link.display_title()


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_SPLIT.split(text or "") if t]


def title_match(want: str, got: str) -> float:
    """0~1 的标题相关度。中文标题常常整体出现，英文按词命中。"""
    want, got = (want or "").strip(), (got or "")
    if not want:
        return 0.0
    if want.lower() in got.lower():
        return 1.0
    wt = [t for t in _tokens(want) if len(t) > 1]
    if not wt:
        return 0.0
    gl = got.lower()
    hit = sum(1 for t in wt if t in gl)
    return hit / len(wt)


def _closest_target(want_episodes, actual: int) -> int:
    """把「期望集数」归一成一个数。

    传列表时（各季集数）取最接近实际集数的那个——用户没指定季的话，
    拿到任意一整季都该算完整，不该因为别的季更长而被判残缺。
    """
    if isinstance(want_episodes, (list, tuple, set)):
        opts = [int(x) for x in want_episodes if x]
        return min(opts, key=lambda n: abs(n - actual)) if opts else 0
    return int(want_episodes or 0)


def score_one(res: ProbeResult, want_title: str, want_year: str = "",
              want_season: Optional[int] = None,
              want_episodes=0) -> Scored:
    """给一个候选打分，同时记下理由（要能解释为什么选它）。"""
    reasons: List[str] = []
    if not res.ok:
        return Scored(res, -1000.0, [res.error or "不可用"])

    title = res.link.display_title()
    s = res.summary
    score = 0.0

    rel = title_match(want_title, title)
    score += rel * 40
    reasons.append(f"标题相关度 {rel:.0%}")
    if rel < 0.34:
        score -= 25
        reasons.append("标题对不上，降权")

    if want_year and want_year in title:
        score += 6
        reasons.append(f"年份 {want_year} 命中")

    if want_season is not None and s and s.seasons:
        if want_season in s.seasons:
            score += 12
            reasons.append(f"第 {want_season} 季命中")
        else:
            score -= 20
            reasons.append(f"季号不符（资源是第 {s.seasons} 季）")

    if s and s.count:
        # 没指定季时 want_episodes 可以是各季集数的列表：拿资源实际集数去对
        # 「最接近的那一季」，否则各季长度不一的剧会被误判成不完整
        target = _closest_target(want_episodes, s.count)
        if target:
            ratio = min(1.0, s.count / target)
            score += ratio * 30
            reasons.append(f"集数 {s.count}/{target}")
            if s.count >= target:
                score += 8
                reasons.append("全集")
        else:
            score += min(s.count, 24)
            reasons.append(f"{s.count} 集")
        if not s.contiguous:
            score -= 12
            reasons.append(f"缺集 {s.missing[:5]}")
        if len(s.seasons) > 1:
            # 多季混装：集号是跨季合并的，完整度判断不可靠，得让人（或 AI）看一眼
            reasons.append(f"多季合集（第 {'、'.join(map(str, s.seasons[:4]))} 季）")
    elif res.playable:
        score += min(res.playable, 10)
        reasons.append(f"{res.playable} 个可播放文件（未识别出集号）")

    if s:
        score += _HEIGHT_SCORE.get(s.height, 0)
        if s.height:
            reasons.append(f"{s.height}p")
        score += _SOURCE_SCORE.get(s.source, 0)
        if s.source:
            reasons.append(s.source)
        if s.chinese_sub:
            score += 8
            reasons.append("有中文字幕/国语")

    if res.junk:
        score -= min(res.junk * 2, 10)
        reasons.append(f"{res.junk} 个引流文件")

    # 体积明显不合理的降权：一集平均不到 80MB 基本是压缩过头或错配
    if s and s.count and res.total_size:
        per = res.total_size / max(1, s.count)
        if per < 80 * 1024 ** 2:
            score -= 15
            reasons.append("单集体积过小，可能是残片")

    return Scored(res, round(score, 1), reasons)


def rank(results: List[ProbeResult], want_title: str, want_year: str = "",
         want_season: Optional[int] = None, want_episodes=0) -> List[Scored]:
    """打分并按分数从高到低排。不可用的排最后。"""
    scored = [score_one(r, want_title, want_year, want_season, want_episodes)
              for r in results]
    scored.sort(key=lambda s: -s.score)
    return scored


def episode_yardstick(detail: dict, season: Optional[int]):
    """判断「完整」的基准集数。

    指定了季 -> 返回那一季的集数（一个整数）。
    没指定季 -> 返回**各季集数的列表**，让打分时按资源实际集数去对最接近的一季。
    网盘分享绝大多数是单季打包：拿全剧总集数当分母，一个完整单季只能算 25% 完整；
    拿最长的一季当分母，短的那些季又会被误判成残缺。
    """
    seasons = detail.get("seasons") or []
    if season is not None:
        return next((s["episodes"] for s in seasons if s["season"] == season), 0)
    per = sorted({s["episodes"] for s in seasons if s["episodes"]})
    return per or int(detail.get("total_episodes") or 0)
