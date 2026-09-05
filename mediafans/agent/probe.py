"""探测候选分享：真去打开它，看里面到底有没有能播的东西。

这一层才是「避免空资源」的关键。搜索源给的标题再漂亮也不算数——
死链、空壳目录、只有广告 txt 的分享，只有打开过才知道。
LLM 判断不了这个，所以顺序是「先探测拿事实，再让模型在事实上做选择」。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..models import ShareLink
from ..utils import fmt_size, is_playable
from .episode import SeasonSummary, infer_bare_numbering, parse_episode, summarize

# 分享里常见的引流目录/文件，命中就不算有效内容
_JUNK_HINT = ("请保存", "限时", "免费", "关注", "更多资源", "领取", "推广", "广告",
              "说明", "必看", "教程", "资源合集导航")

# 往下钻几层去找视频（分享常见「片名/季/画质」这种套娃）
_MAX_DEPTH = 3
# 每个分享最多看多少个条目，防止超大合集把时间耗光
_MAX_ENTRIES = 400


@dataclass
class ProbeFile:
    """分享里的一个可播放文件。

    保留 fid 和 share_fid_token 是为了能**按单集**转存——整季打包转存会
    带进重复集和画质不一的拼盘，缺的那一集还是缺。
    """

    fid: str
    name: str
    size: int
    share_fid_token: str
    dir_fid: str = "0"          # 它所在的那一层，转存时要按这层列文件
    crumb: str = ""             # 它在分享里的路径。分享把两部作品打包在一起时
                                # （「异人之下…附动漫全系列」），只有路径能区分
    season: Optional[int] = None
    episode: Optional[int] = None
    height: int = 0
    source: str = ""
    chinese_sub: bool = False


@dataclass
class ProbeResult:
    """一个候选分享的体检报告。"""

    link: ShareLink
    ok: bool = False
    error: str = ""
    dir_fid: str = "0"          # 真正装着视频的那一层
    crumb: str = ""
    playable: int = 0
    total_size: int = 0
    junk: int = 0
    names: List[str] = field(default_factory=list)
    files: List[ProbeFile] = field(default_factory=list)
    summary: Optional[SeasonSummary] = None

    @property
    def size_h(self) -> str:
        return fmt_size(self.total_size) if self.total_size else "-"

    def brief(self) -> dict:
        """给 LLM 看的紧凑事实，不带 URL 之类的无关字段。"""
        s = self.summary
        return {
            "title": self.link.display_title()[:80],
            "playable": self.playable,
            "size": self.size_h,
            "episodes": (f"{s.episodes[0]}-{s.episodes[-1]}"
                         if s and s.count > 1 else (str(s.episodes[0]) if s and s.count == 1 else "")),
            "episode_count": s.count if s else 0,
            "missing": (s.missing[:8] if s else []),
            "seasons": (s.seasons if s else []),
            # 多季混装时集号是跨季合并的，完整度不可靠——明确告诉模型
            "multi_season": bool(s and len(s.seasons) > 1),
            "height": s.height if s else 0,
            "source": s.source if s else "",
            "chinese_sub": bool(s and s.chinese_sub),
            "junk": self.junk,
            "sample": self.names[:4],
        }


def probe_share(drive, link: ShareLink, max_entries: int = _MAX_ENTRIES) -> ProbeResult:
    """打开一个分享，递归找出可播放文件。失败不抛异常，写进 error 字段。"""
    res = ProbeResult(link=link)
    try:
        ctx = drive.open_share(link.url, passcode=link.passcode or "")
    except Exception as e:
        res.error = str(e)[:120]
        return res

    seen = 0
    best_dir, best_crumb, best_count = "0", "", -1
    stack = [("0", "", 0)]
    names: List[str] = []
    found: List[ProbeFile] = []
    total = 0
    junk = 0
    try:
        while stack and seen < max_entries:
            fid, crumb, depth = stack.pop(0)
            try:
                entries = drive.list_share_files(ctx, fid)
            except Exception:
                continue
            here = []
            for f in entries:
                seen += 1
                if f.is_dir:
                    if depth < _MAX_DEPTH and not _looks_junk(f.name):
                        stack.append((f.fid, (crumb + "/" + f.name).strip("/"), depth + 1))
                    continue
                if is_playable(f.name):
                    here.append(f)
                elif _looks_junk(f.name):
                    junk += 1
            if len(here) > best_count:
                # 记住视频最多的那一层，后面转存就存这一层
                best_dir, best_crumb, best_count = fid, crumb, len(here)
            for f in here:
                names.append(f.name)
                total += f.size
                info = parse_episode(f.name)
                found.append(ProbeFile(
                    fid=f.fid, name=f.name, size=f.size,
                    share_fid_token=f.share_fid_token, dir_fid=fid, crumb=crumb,
                    season=info.season, episode=info.episode,
                    height=info.height, source=info.source,
                    chinese_sub=info.chinese_sub,
                ))
    except Exception as e:
        res.error = str(e)[:120]

    _fill_bare_numbering(found)

    res.dir_fid, res.crumb = best_dir, best_crumb
    res.names = names
    res.files = found
    res.playable = len(names)
    res.total_size = total
    res.junk = junk
    res.summary = summarize(names)
    res.ok = res.playable > 0
    if not res.ok and not res.error:
        res.error = "分享里没有可播放文件"
    return res


def _fill_bare_numbering(files: List[ProbeFile]) -> None:
    """给「文件名只有编号」的那些补上集号。

    按目录分别判断：同一份分享里可能一层是正片 `01.mp4…13.mp4`，
    另一层是花絮 `01.mp4…03.mp4`，混在一起判会互相干扰。
    """
    by_dir: dict = {}
    for f in files:
        if f.episode is None:
            by_dir.setdefault(f.dir_fid, []).append(f)
    for group in by_dir.values():
        mapping = infer_bare_numbering([f.name for f in group])
        for f in group:
            if f.name in mapping:
                f.episode = mapping[f.name]


def _looks_junk(name: str) -> bool:
    return any(h in (name or "") for h in _JUNK_HINT)


def probe_many(drive_factory: Callable[[], object], links: List[ShareLink],
               workers: int = 4, max_entries: int = _MAX_ENTRIES) -> List[ProbeResult]:
    """并发探测多个分享。

    每个线程用自己的 drive 实例——夸克驱动内部有 httpx.Client 和分享上下文，
    多线程共用一个实例会互相踩。并发别开太大，夸克按账号限流。
    """
    if not links:
        return []
    results: List[Optional[ProbeResult]] = [None] * len(links)

    def run(idx_link):
        idx, link = idx_link
        try:
            return idx, probe_share(drive_factory(), link, max_entries=max_entries)
        except Exception as e:
            r = ProbeResult(link=link)
            r.error = f"{type(e).__name__}: {str(e)[:100]}"
            return idx, r

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(links)))) as ex:
        for idx, res in ex.map(run, enumerate(links)):
            results[idx] = res
    return [r for r in results if r is not None]
