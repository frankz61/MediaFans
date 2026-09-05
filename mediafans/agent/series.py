"""剧集矩阵：把「TMDB 权威集列表 × 网盘已存的 × 各来源能补的」拼成一张表。

为什么需要这个：整季打包转存必然产生拼盘。实测某剧第二季存下来是
E01-05 是 2160p、E06 掉到 1080p、E07 存了两份、**E08 直接没有**、E09-10 又是 1080p。
按集来看就一清二楚：哪一集缺、哪一集有更好的版本、缺的那集能从哪个来源补。

播放时优先用网盘里已有的文件——那是现成的，直接生成直链就能播，不用等转存。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..models import ShareLink
from ..utils import fmt_size, is_playable
from .episode import infer_bare_numbering, parse_episode
from .probe import ProbeFile, ProbeResult, probe_many
from .identity import Work, belongs, share_matches, titles_are_usable

# 探测很慢（每个分享要开、要递归列目录），同一部剧短时间内别重复探
CACHE_TTL = 1800.0
DEFAULT_PROBE_TOP = 6
# 一批不够就再探一批，最多探到这么多。死链比例高的时候全靠它兜底
PROBE_CAP = 24


@dataclass
class LocalFile:
    """网盘里已经存着的一集。"""

    path: str
    name: str
    size: int
    height: int = 0
    source: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "size": self.size,
                "size_h": fmt_size(self.size), "height": self.height,
                "source": self.source}


@dataclass
class SourceFile:
    """某个分享里的这一集（还没转存）。"""

    share_title: str
    share_url: str
    passcode: str
    file: ProbeFile

    def as_dict(self, index: int) -> dict:
        f = self.file
        return {
            "index": index,
            "label": f"源{index + 1}",
            "share_title": self.share_title,
            "name": f.name,
            "size": f.size,
            "size_h": fmt_size(f.size),
            "height": f.height,
            "source": f.source,
            "chinese_sub": f.chinese_sub,
        }


@dataclass
class EpisodeRow:
    episode: int
    title: str = ""
    air_date: str = ""
    still: str = ""
    local: Optional[LocalFile] = None
    sources: List[SourceFile] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.local:
            return "saved"          # 网盘里有，点开就能播
        if self.sources:
            return "available"      # 有来源，点一下补
        return "missing"            # 谁都没有

    def as_dict(self) -> dict:
        return {
            "episode": self.episode,
            "title": self.title,
            "air_date": self.air_date,
            "still": self.still,
            "status": self.status,
            "local": self.local.as_dict() if self.local else None,
            "sources": [s.as_dict(i) for i, s in enumerate(self.sources)],
        }


@dataclass
class SeriesView:
    tmdb_id: int
    title: str
    season: int
    seasons: List[dict]
    rows: List[EpisodeRow]
    probed: int = 0
    dead: int = 0
    local_dir: str = ""
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        rows = [r.as_dict() for r in self.rows]
        return {
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "season": self.season,
            "seasons": self.seasons,
            "local_dir": self.local_dir,
            "probed": self.probed,
            "dead": self.dead,
            "notes": self.notes,
            "episodes": rows,
            "counts": {
                "total": len(rows),
                "saved": sum(1 for r in rows if r["status"] == "saved"),
                "available": sum(1 for r in rows if r["status"] == "available"),
                "missing": sum(1 for r in rows if r["status"] == "missing"),
            },
        }


class SeriesCache:
    """(tmdb_id, season) -> SeriesView，带 TTL。探测太贵，不能每次开页面都重来。"""

    def __init__(self, ttl: float = CACHE_TTL, cap: int = 12):
        self.ttl, self.cap = ttl, cap
        self._lock = threading.Lock()
        self._items: Dict[tuple, tuple] = {}

    def get(self, key) -> Optional[SeriesView]:
        with self._lock:
            hit = self._items.get(key)
            if not hit:
                return None
            ts, view = hit
            if time.time() - ts > self.ttl:
                self._items.pop(key, None)
                return None
            return view

    def put(self, key, view: SeriesView) -> None:
        with self._lock:
            self._items[key] = (time.time(), view)
            while len(self._items) > self.cap:
                self._items.pop(next(iter(self._items)))

    def drop(self, key) -> None:
        with self._lock:
            self._items.pop(key, None)


def scan_local(drive, path: str, season: Optional[int],
               titles: Optional[List[str]] = None,
               foreign: Optional[List[str]] = None,
               work: Optional[Work] = None) -> Dict[int, LocalFile]:
    """扫网盘目录，按集号归位。同一集存了多份时保留画质最高、体积最大的那个。

    只按集号归位是不够的：实测某个「末日地堡」目录里混进了
    `The.Gentlemen.2024.S01E01~E08`，第一季因此被凑成「已存 10 集」，
    点播放会放出另一部剧。判断一个文件是不是这部作品的，见 identity.py。
    传入 foreign 列表可以收集被判为其它作品的文件名（调用方自己持有，并发安全）。
    """
    out: Dict[int, LocalFile] = {}
    try:
        fid = drive.resolve_path(path)
        if not fid:
            return out
        entries = drive.list_files(fid)
    except Exception:
        return out

    cand = [(f, parse_episode(f.name)) for f in entries
            if not f.is_dir and is_playable(f.name)]
    # 转存下来的常常是 `01.mp4`…`13.mp4` 这种裸编号，parse_episode 认不出，
    # 但一个目录里十几个纯编号视频就是集号
    bare = infer_bare_numbering([f.name for f, i in cand if i.episode is None])
    for f, i in cand:
        if i.episode is None and f.name in bare:
            i.episode = bare[f.name]
    cand = [(f, i) for f, i in cand if i.episode is not None]

    w = work or Work(titles=list(titles or []))
    # 先在整个目录上验证「片名信号」是否可用，再决定要不要按片名过滤
    trust = titles_are_usable([f.name for f, _ in cand], w.titles)

    for f, info in cand:
        # 文件名标了季号就得对得上；没标季号的按当前季算（单季目录很常见）
        if season is not None and info.season is not None and info.season != season:
            continue
        # 本地是转存下来的完整发布名，片名信号可靠，可以据此拒
        why = belongs(f.name, w, trust_titles=trust, title_can_reject=True)
        if why:
            if foreign is not None:
                foreign.append(f.name)
            continue
        cur = out.get(info.episode)
        pick = LocalFile(path=f"{path.rstrip('/')}/{f.name}", name=f.name,
                         size=f.size, height=info.height, source=info.source)
        if cur is None or (pick.height, pick.size) > (cur.height, cur.size):
            out[info.episode] = pick
    return out


def index_sources(probes: List[ProbeResult], season: Optional[int],
                  work: Optional[Work] = None,
                  dropped: Optional[List[str]] = None) -> Dict[int, List[SourceFile]]:
    """把探测结果按集号铺开：这一集有哪几个来源可以补。

    分享经常把两部同名作品打包在一起（「异人之下 第二季…附第一季及动漫全系列」），
    里面动画和真人剧的集号完全重叠。所以按集号归位之前，先看文件在分享里的
    路径说的是哪一部——判断逻辑在 identity.py。
    """
    by_ep: Dict[int, List[SourceFile]] = {}
    w = work or Work()
    alive = [pr for pr in probes if pr.ok]
    paths = {id(pr): [_share_path(pr, f) for f in pr.files] for pr in alive}
    # 片名信号在这批结果里到底能不能用（发布组的别名不在 TMDB 里时就不能用）
    trust = w.titles and any(share_matches(paths[id(pr)], w.titles) for pr in alive)
    for pr in alive:
        # 一份分享从头到尾没提过要找的片名，多半是同名的另一部作品，整份排除
        mine = (not trust) or share_matches(paths[id(pr)], w.titles)
        if not mine:
            if dropped is not None:
                dropped.append(f"{pr.link.display_title()[:30]}（整份是别的作品）")
            continue
        for f in pr.files:
            if f.episode is None:
                continue
            if season is not None and f.season is not None and f.season != season:
                continue
            if w.known():
                why = belongs(_share_path(pr, f), w, trust_titles=bool(trust))
                if why:
                    if dropped is not None:
                        dropped.append(f"{f.name[:40]}（{why}）")
                    continue
            by_ep.setdefault(f.episode, []).append(SourceFile(
                share_title=pr.link.display_title(),
                share_url=pr.link.url,
                passcode=pr.link.passcode or "",
                file=f,
            ))
    # 每一集内部按画质、体积排：默认给用户最好的那个
    for eps in by_ep.values():
        eps.sort(key=lambda s: (-s.file.height, -s.file.size))
    return by_ep


def _share_path(pr: ProbeResult, f) -> str:
    """文件在分享里的完整位置：分享标题 / 目录 / 文件名。

    片名常常只写在分享标题或目录名上（里面的文件叫 `E05.mp4`），
    所以三段都要参与判断，不能只看文件名。
    """
    return "/".join(x for x in (pr.link.display_title(), f.crumb, f.name) if x)


def build_series(
    drive_factory: Callable[[], object],
    search_fn: Optional[Callable],
    tmdb,
    tmdb_id: int,
    season: int,
    *,
    title: str = "",
    local_dir: str = "",
    probe_top: int = DEFAULT_PROBE_TOP,
    with_sources: bool = True,
    on_step: Optional[Callable[[str, dict], None]] = None,
) -> SeriesView:
    """拼出一季的完整视图。"""
    def step(stage, msg, **extra):
        if on_step:
            on_step(stage, dict(extra, message=msg))

    detail = tmdb.tv_detail(tmdb_id)
    show_title = title or detail.get("title") or ""
    season_info = tmdb.season_detail(tmdb_id, season)
    rows = [EpisodeRow(episode=e["episode"], title=e["name"],
                       air_date=e["air_date"], still=e["still"])
            for e in season_info["episodes"]]
    view = SeriesView(tmdb_id=tmdb_id, title=show_title, season=season,
                      seasons=detail.get("seasons") or [], rows=rows)
    step("meta", f"{show_title} 第 {season} 季，共 {len(rows)} 集")

    # 1) 网盘里已经有什么——这部分是现成的，能直接播
    drive = drive_factory()
    base = local_dir or f"{getattr(drive, 'save_dir', '/MediaFans').rstrip('/')}/{show_title}"
    view.local_dir = base
    titles = [t for t in (show_title, detail.get("original_title") or "") if t]
    # 要找的到底是哪一部：片名 + 动画/真人。同名不同作时全靠这个分开
    work = Work(titles=titles, animation=detail.get("animation"))
    foreign: List[str] = []
    local = scan_local(drive, base, season, titles, foreign, work)
    for r in rows:
        r.local = local.get(r.episode)
    step("local", f"网盘里已有 {len(local)} 集", saved=len(local))
    if foreign:
        # 同一目录里混进别的剧很常见，不说清楚会让人以为集数对不上是 bug
        view.notes.append(
            f"这个目录里还有 {len(foreign)} 个文件不属于本剧，已跳过"
            f"（如 {foreign[0][:40]}）")
        step("local", view.notes[-1])

    if not with_sources or search_fn is None:
        return view

    # 2) 搜索并探测，把每一集能从哪补上铺开
    missing = [r.episode for r in rows if not r.local]
    if not missing:
        view.notes.append("这一季已经齐了，没有再去搜资源")
        step("done", "这一季已经齐了")
        return view

    queries = search_queries(show_title, detail.get("original_title") or "", season)
    step("search", f"缺 {len(missing)} 集（{_fmt_eps(missing)}），"
                   f"用 {len(queries)} 种写法找来源：{'、'.join(queries)}")
    failures: List[str] = []
    links = multi_search(search_fn, queries, "quark", failures=failures)
    cands = links[:PROBE_CAP]
    if not cands:
        # 「搜索源挂了」和「真没这个资源」对用户是两件事，别混为一谈
        view.notes.append(f"搜索失败：{'；'.join(failures[:2])}" if len(failures) == len(queries)
                          else "没搜到可用的夸克资源")
        step("error", view.notes[-1])
        return view
    if failures:
        step("search", f"（{len(failures)} 个查询词失败，用其余的结果继续）")

    # 分批探测，够用就停。实测搜索源返回的前几条经常是死链——「异人之下」
    # 前 6 条全是「好友已取消了分享」，只探 6 条就得到「0 个资源可用」，
    # 功能等于没有；但一上来就探 20 条，正常情况下白等三倍时间。
    probes: List[ProbeResult] = []
    dropped: List[str] = []
    by_ep: Dict[int, List[SourceFile]] = {}
    for start in range(0, len(cands), probe_top):
        wave = cands[start:start + probe_top]
        step("probe", (f"正在打开验证 {len(wave)} 个候选…" if not probes else
                       f"前 {len(probes)} 个不够用，再验证 {len(wave)} 个…"))
        probes += probe_many(drive_factory, wave)
        dropped = []
        by_ep = index_sources(probes, season, work, dropped)
        alive = sum(1 for p in probes if p.ok)
        # 「够用」得是「有得选」，不是「刚好够」：只找到一个来源时没有比较余地，
        # 实测异人之下第二季第一个覆盖全季的来源是 286MB/集、画质都读不出来的
        # 版本，再探一批才见到 4K。两个来源起步，plan_batch 才有得挑。
        if alive >= 2 and all(e in by_ep for e in missing):
            break

    view.probed = sum(1 for p in probes if p.ok)
    view.dead = sum(1 for p in probes if not p.ok)
    if view.dead:
        step("probe", f"{view.dead} 个候选打不开（失效/空壳），{view.probed} 个可用")
    if dropped:
        # 分享把同名的另一部作品打包在一起很常见，不说清楚会让人以为是漏了
        view.notes.append(
            f"跳过了 {len(dropped)} 个不属于本作的文件（如 {dropped[0]}）")
        step("probe", view.notes[-1])
    for r in rows:
        if not r.local:
            r.sources = by_ep.get(r.episode, [])
    still_missing = [r.episode for r in rows if r.status == "missing"]
    step("done",
         f"{view.probed} 个资源可用，"
         + (f"仍缺 {_fmt_eps(still_missing)}" if still_missing else "缺的集都能补上"),
         probed=view.probed, dead=view.dead)
    if still_missing:
        view.notes.append(f"这些集所有来源里都没有：{_fmt_eps(still_missing)}")
    return view


_CN_NUM = "零一二三四五六七八九十"


def cn_season(n: int) -> str:
    """季号转中文数字。搜索源里「第2季」几乎搜不到，「第二季」才有结果（实测
    末日地堡：第2季 0 条、第二季 8 条），所以查询词必须用中文数字。"""
    if n <= 10:
        return _CN_NUM[n]
    if n < 20:
        return "十" + (_CN_NUM[n - 10] if n > 10 else "")
    return f"{n // 10 and _CN_NUM[n // 10]}十{_CN_NUM[n % 10] if n % 10 else ''}"


def search_queries(title: str, original: str = "", season: Optional[int] = None) -> List[str]:
    """生成多个查询词。

    实测同一部剧不同写法的命中数差很多：
      末日地堡      24 条      末日地堡 第二季   8 条
      Silo         11 条      末日地堡 第2季    0 条  ← 阿拉伯数字搜不到
    所以不能只用一种写法，得多路合并。
    """
    out = []
    for q in (title,
              f"{title} 第{cn_season(season)}季" if season and season > 1 else "",
              original if original and original.lower() != (title or "").lower() else "",
              f"{original} S{season:02d}" if original and season else ""):
        q = (q or "").strip()
        if q and q not in out:
            out.append(q)
    return out


def multi_search(search_fn: Callable, queries: List[str], netdisk: str = "quark",
                 workers: int = 3, failures: Optional[List[str]] = None) -> List[ShareLink]:
    """并发跑多个查询词并按 URL 合并去重。

    单个查询词失败不影响其它——但失败的会记进 failures，
    好让上层区分「搜索源挂了」和「真没这个资源」，这两种对用户是不同的事。
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(q):
        try:
            links, _ = search_fn(q, netdisk or None)
            return q, links or [], ""
        except Exception as e:
            return q, [], f"{q}: {str(e)[:60]}"

    merged, seen = [], set()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(queries)))) as ex:
        for _q, links, err in ex.map(one, queries):
            if err and failures is not None:
                failures.append(err)
            for l in links:
                key = l.url.split("?")[0]
                if (not netdisk or l.netdisk == netdisk) and key not in seen:
                    seen.add(key)
                    merged.append(l)
    return merged


def _fmt_eps(eps: List[int]) -> str:
    return "E" + "、E".join(f"{e:02d}" for e in eps[:10]) + ("…" if len(eps) > 10 else "")


@dataclass
class BatchGroup:
    """一次转存：同一个分享的同一层目录里的一批文件。"""

    share_title: str
    share_url: str
    passcode: str
    dir_fid: str
    episodes: List[int] = field(default_factory=list)
    files: List[ProbeFile] = field(default_factory=list)
    height: int = 0

    def as_dict(self) -> dict:
        return {"share_title": self.share_title, "episodes": sorted(self.episodes),
                "count": len(self.files), "height": self.height}


@dataclass
class BatchResult:
    """批量转存的结果。"""

    saved: List[int] = field(default_factory=list)       # 成功补上的集号
    skipped: List[int] = field(default_factory=list)     # 目录里本来就有的
    failed: List[int] = field(default_factory=list)
    paths: Dict[int, str] = field(default_factory=dict)  # 集号 -> 网盘路径
    names: Dict[int, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    groups: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"saved": sorted(self.saved), "skipped": sorted(self.skipped),
                "failed": sorted(self.failed), "errors": self.errors[:5],
                "groups": self.groups}


def plan_batch(rows: List["EpisodeRow"]) -> List[BatchGroup]:
    """把「要补的集」分组：尽量少的来源，尽量一致的画质。

    逐集各挑各的最高画质会拼出一个大杂烩——E01-E05 2160p、E06 掉到 1080p、
    E07 两份不同版本，正是用户在末日地堡上遇到的那种。而且 27 集意味着
    开 27 次分享、发 27 次转存请求，慢且容易被限流。

    所以按「一个来源能覆盖多少集」贪心分组：先用覆盖最多的那个来源把能补的
    都补了，剩下的再找下一个。覆盖数一样时选画质高的。一份分享的同一层目录
    只开一次、只发一次转存请求。
    """
    need: Dict[int, List[SourceFile]] = {
        r.episode: list(r.sources) for r in rows if not r.local and r.sources}
    groups: List[BatchGroup] = []
    while need:
        # 同一层目录能一次转存，所以按 (分享, 目录) 归桶
        buckets: Dict[tuple, Dict[int, SourceFile]] = {}
        for ep, srcs in need.items():
            for sf in srcs:
                key = (sf.share_url, sf.file.dir_fid)
                cur = buckets.setdefault(key, {}).get(ep)
                # 同一桶里同一集可能有多份，留画质高的
                if cur is None or (sf.file.height, sf.file.size) > (cur.file.height, cur.file.size):
                    buckets[key][ep] = sf
        if not buckets:
            break
        key, picked = max(
            buckets.items(),
            # 覆盖多的优先；一样多就挑画质高的；再一样就按 url 定序，保证结果稳定
            key=lambda kv: (len(kv[1]),
                            max(s.file.height for s in kv[1].values()),
                            kv[0][0]))
        first = next(iter(picked.values()))
        groups.append(BatchGroup(
            share_title=first.share_title, share_url=first.share_url,
            passcode=first.passcode, dir_fid=first.file.dir_fid,
            episodes=sorted(picked),
            files=[picked[e].file for e in sorted(picked)],
            height=max(s.file.height for s in picked.values()),
        ))
        for ep in picked:
            need.pop(ep, None)
    return groups


def fetch_batch(drive_factory: Callable[[], object], groups: List[BatchGroup],
                to_dir: str, on_step: Optional[Callable[[str, dict], None]] = None,
                skip_existing: bool = True) -> BatchResult:
    """按组批量转存。一组开一次分享、发一次转存请求。

    一组失败不影响其它组——分享随时可能失效，不能让一个坏链毁掉整批。
    """
    res = BatchResult(groups=[g.as_dict() for g in groups])

    def step(stage, msg, **extra):
        if on_step:
            on_step(stage, dict(extra, message=msg))

    drive = drive_factory()
    # 「一键」天然会被重复点。已经在目录里的按名字跳过，
    # 否则网盘会存出一堆 `xxx(1).mkv`，比没存还难收拾。
    have = set()
    if skip_existing:
        try:
            fid = drive.resolve_path(to_dir)
            if fid:
                have = {f.name for f in drive.list_files(fid) if not f.is_dir}
        except Exception:
            have = set()

    for i, g in enumerate(groups, 1):
        want = [(e, f) for e, f in zip(g.episodes, g.files) if f.name not in have]
        for e, f in zip(g.episodes, g.files):
            if f.name in have:
                res.skipped.append(e)
                res.paths[e] = f"{to_dir.rstrip('/')}/{f.name}"
                res.names[e] = f.name
        if not want:
            step("save", f"来源 {i}/{len(groups)}：{len(g.episodes)} 集都已经存过了")
            continue
        step("save", f"来源 {i}/{len(groups)}「{g.share_title[:28]}」"
                     f"转存 {len(want)} 集（{_fmt_eps([e for e, _ in want])}）…")
        try:
            ctx = drive.open_share(g.share_url, passcode=g.passcode)
            # share_fid_token 只在「列出所在那一层」时才拿得到，必须按 dir_fid 重列
            listing = drive.list_share_files(ctx, g.dir_fid)
            by_fid = {f.fid: f for f in listing}
            by_name = {f.name: f for f in listing}
            targets, eps = [], []
            for e, f in want:
                t = by_fid.get(f.fid) or by_name.get(f.name)
                if t is None:
                    res.failed.append(e)
                    res.errors.append(f"E{e:02d}：分享里找不到 {f.name[:36]}")
                    continue
                targets.append(t)
                eps.append(e)
            if targets:
                drive.save_share_files(ctx, targets, to_dir)
                for e, t in zip(eps, targets):
                    res.saved.append(e)
                    res.paths[e] = f"{to_dir.rstrip('/')}/{t.name}"
                    res.names[e] = t.name
                step("save", f"来源 {i}/{len(groups)}：补上 {len(eps)} 集")
        except Exception as ex:
            for e, _ in want:
                if e not in res.saved:
                    res.failed.append(e)
            res.errors.append(f"{g.share_title[:24]}：{str(ex)[:80]}")
            step("error", f"来源 {i}/{len(groups)} 转存失败：{str(ex)[:60]}")
    return res


def fetch_episode(drive_factory: Callable[[], object], src: SourceFile,
                  to_dir: str) -> str:
    """把单独一集转存到网盘，返回它的路径。

    按集转存是这套设计的重点：整季打包会带进重复集和画质不一的拼盘，
    缺的那集还是缺；按集补就能精确填坑。
    """
    drive = drive_factory()
    ctx = drive.open_share(src.share_url, passcode=src.passcode)
    # share_fid_token 只在「列出所在那一层」时才拿得到，必须按 dir_fid 重列
    files = drive.list_share_files(ctx, src.file.dir_fid)
    target = next((f for f in files if f.fid == src.file.fid), None)
    if target is None:
        target = next((f for f in files if f.name == src.file.name), None)
    if target is None:
        raise ValueError(f"分享里找不到这个文件了：{src.file.name}")
    drive.save_share_files(ctx, [target], to_dir)
    return f"{to_dir.rstrip('/')}/{target.name}"
