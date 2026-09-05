"""这个文件到底是不是这部作品的。

同名不同作太常见了，而且比想象的更难分：

  《一人之下》是动画（2016，6 季 94 集），真人版叫《异人之下》（2023，剧版 27 集
  + 影版 2024 + 第二季 2025）。两者**连分季名都撞**——动画第 5 季「决战·碧游村篇」，
  真人剧第 2 季「决战！碧游村」；动画第 6 季「唐门篇」，真人剧第 3 季「血战！唐门」。

光靠集号归位必然串台，而且改查询词治不了：实测有分享标题直接写着
「异人之下 第二季 / 决战碧游村 (2025) 更新13 完结 附第一季及动漫全系列」——
**一个分享里就同时装着真人剧和整套动画**，两边的集号完全重叠。所以要看的是
文件在分享里的**路径**（`动漫全系列/一人之下 第一季/E05.mp4`）说的是哪一部。

两个判据，都只在「说得出口」的时候才用，说不出就不拦：

- **片名**：路径里出现的片名对不对得上。裸文件名（`E05.mp4`）没片名可比，不算数。
- **类型**：动画 / 真人。TMDB 的 genre 16 就是动画，路径里的「动漫」「真人版」
  这类词是资源方自己标的，两边对不上才拦。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .rank import title_match

# 从路径里剥掉季集/画质/片源/年份/编码这些技术标记，剩下的才是「片名部分」。
# 季号一定要连中文数字一起剥：中文网盘分享里 `第一季/第二季` 这种目录满地都是，
# 剥不掉就会被当成「有片名但对不上」，把整季正片全误杀。
_TECH = re.compile(
    r"[Ss]\d{1,2}[\s._-]*[Ee][Pp]?\d{1,3}"
    r"|第\s*[0-9一二三四五六七八九十百零两]+\s*[季集话話部篇]|[Ee][Pp]?\d{1,3}"
    r"|\d{3,4}[pi]\b|4k|uhd|hdr|dv|remux|blu-?ray|web-?dl|webrip|hdtv|x26[45]|h\.?26[45]"
    r"|hevc|avc|aac|ddp?\d?\.?\d?|atmos|truehd|dts(-hd)?|ma|flac|10bit"
    r"|(19|20)\d{2}|内封|内嵌|中英|中字|简繁|国语|字幕|合集|全集|更新?至?\d*",
    re.I)

# 资源方常用的类型标记。只收那些**只可能指一种**的词：
# 「剧场版」是动画电影、「真人剧」是真人，但光一个「剧」字什么都说明不了。
ANIME_MARKS = ("动漫", "动画", "番剧", "国漫", "anime", "剧场版")
LIVE_MARKS = ("真人版", "真人剧", "真人电影", "剧版")


@dataclass
class Work:
    """要找的是哪一部。"""

    titles: List[str] = field(default_factory=list)   # 中文名 + 原名
    animation: Optional[bool] = None                  # None = 不知道，就不按类型拦

    def known(self) -> bool:
        return bool(self.titles) or self.animation is not None


def strip_tech(text: str) -> str:
    """去掉技术标记和数字后剩下的字，用来判断「这一段里有没有片名」。"""
    stem = (text or "").rsplit(".", 1)[0]
    # 只留字母和中日文。数字不算片名——`01.mp4`、`12x.mkv` 这种裸集号
    # 在中文分享里满地都是，把它们当成「有片名但对不上」会整份误杀正片。
    return re.sub(r"[^A-Za-z一-鿿぀-ヿ]+", "", _TECH.sub(" ", stem))


def title_verdict(path: str, titles: List[str]) -> Optional[bool]:
    """这一段里的片名对不对得上。没有可比的片名时返回 None。"""
    if not titles:
        return None
    if len(strip_tech(path)) < 2:
        return None
    return any(title_match(t, path) >= 0.5 for t in titles if t)


def kind_verdict(path: str, animation: Optional[bool]) -> Optional[bool]:
    """路径里的「动漫/真人」标记跟目标作品是不是一类。没标记返回 None。

    两种标记同时出现（`异人之下 剧版/附动漫全系列`）说明这一层是打包目录，
    说明不了具体文件属于哪边，交给下一层判断。
    """
    if animation is None:
        return None
    low = (path or "").lower()
    is_anime = any(m in low for m in ANIME_MARKS)
    is_live = any(m in low for m in LIVE_MARKS)
    if is_anime == is_live:          # 都没有，或者两个都有
        return None
    return is_anime if animation else is_live


def belongs(path: str, work: Work, trust_titles: bool = True,
            title_can_reject: bool = False) -> Optional[str]:
    """这个文件属于目标作品吗。属于/说不准返回 None，不属于返回原因。

    **从最里层往外逐段看，第一个说得出话的那一段说了算。**
    整条路径拼起来判会串味：分享标题写着「异人之下 第二季 附动漫全系列」，
    那个「动漫」说的是旁边那个目录，不是根下这个 `E07.mp4`；
    整条判会把真人剧的正片当动画拒掉。

    `title_can_reject` 决定「片名对不上」算不算拒的理由，两种场景不一样：

    - **网盘分享里的文件**（默认 False）：文件名几乎从不带片名
      （`01.mp4`、`12x.mkv`、`4K 高码.mkv`），片名在目录名和分享标题上。
      按「这一段没对上片名」去拒，会把 `异人之下（2023）全27集 4K/01.mp4`
      这种正片整份误杀。整份分享是不是另一部作品，交给 share_matches()。
    - **本地已存目录里的文件**（传 True）：那是转存下来的完整发布名，
      片名信号可靠。实测某个「末日地堡」目录里混进了 `The.Gentlemen.S01E01~E08`，
      不按片名拒的话第一季会被凑成「已存 10 集」，点播放放出来的是《绅士们》。
    """
    for seg in reversed(_segments(path)):
        if trust_titles:
            t = title_verdict(seg, work.titles)
            if t is True:
                # 这一层明说了是本作，它捎带提到的「附动漫全系列」是旁边的东西
                return None
            if t is False and title_can_reject:
                return "别的作品"
        k = kind_verdict(seg, work.animation)
        if k is False:
            return "动画版" if not work.animation else "真人版"
        if k is True:
            return None
    return None


def share_matches(paths: List[str], titles: List[str]) -> bool:
    """整份分享里有没有哪一层提到了这部作品。

    分享粒度上片名可以用来拒：一份分享从头到尾没提过要找的片名，
    多半就是同名的另一部作品。这跟 belongs() 里「片名不能单独拒」不矛盾——
    那里是文件粒度，这里是分享粒度，一份分享总有一层会写片名。

    代价是命名被刻意混淆的分享（实测有一个叫 `Y .异.R.之.下. 4K[臻彩]`）会被误伤。
    这里接受这个代价：候选分享有几十份，丢一份成本很低；而把动画混进真人剧的
    集列表里，正是这套判断要解决的问题。**本地已存文件不适用这条**——
    那边误伤会让用户以为缺集去重复转存，代价完全不同。
    """
    return any(title_verdict(p, titles) for p in paths)


def _segments(path: str) -> List[str]:
    return [x for x in re.split(r"[/\\]+", path or "") if x.strip()]


def titles_are_usable(paths: List[str], titles: List[str]) -> bool:
    """这批路径里的片名信号能不能信。

    片名过滤本身有风险：发布组常用 TMDB 里没有的别名（`The.Knockout` 之于
    《狂飙》），硬过滤会把合法文件静默吃掉，用户看到「缺」就去重复转存，
    比多显示几个更糟。所以先看这批文件里有没有对得上片名的——
    有，说明这里的命名习惯带片名，过滤才可信；一个都没有就是别名场景，不过滤。
    """
    return any(title_verdict(p, titles) for p in paths)
