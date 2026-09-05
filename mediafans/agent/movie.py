"""电影：把「网盘里已有的 × 各来源能转的」拼成一张表。

跟剧集共用同一套视图结构（SeriesView / EpisodeRow），电影就是**只有一行的剧**。
这样播放、进度、转存、前端渲染全都不用写第二遍——真正不同的只有一件事：

**电影没有集号可以归位。** 剧集靠 `parse_episode` 把文件铺到 E01…E27 上，
一个文件对不上集号就直接丢掉；电影里每个文件都是「整部片」，能不能要全靠
片名判断（identity.py），再按画质体积排出个先后。所以来源收集单独写一套，
不能复用 index_sources。

第二个不同是**同一部片会有很多版本**：4K/1080p、国语/原声、导演剪辑版。
这些都是合法候选，不是重复——所以来源列表按画质排开让人挑，而不是去重成一个。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from ..utils import is_playable
from .identity import Work, belongs, share_matches, titles_are_usable
from .probe import ProbeResult, probe_many
from .series import (
    PROBE_CAP,
    DEFAULT_PROBE_TOP,
    EpisodeRow,
    LocalFile,
    SeriesView,
    SourceFile,
    _share_path,
    copy_rank,
    multi_search,
)

# 电影的「一行」用这个集号占位。剧集的集号从 1 开始，这里跟着用 1，
# 前端和转存都按 episode 找行，不用为电影另开一条路径。
MOVIE_EPISODE = 1

# 小于这个体积的基本不是正片（预告、花絮、样片）。电影没有集号可以交叉验证，
# 只能靠体积挡一手——实测搜索结果里混进来的多是几十 MB 的预告片。
MIN_MOVIE_BYTES = 200 * 1024 * 1024


def movie_queries(title: str, original: str = "", year: str = "") -> List[str]:
    """电影的查询词。

    比剧集少一层季号，多一层年份：同名重拍太常见（《无间道》《木兰》《忠犬八公》），
    加上年份能把版本分开。但年份不能是唯一写法——网盘分享的标题里经常不写年份，
    只用「片名 年份」会大幅漏召回，所以两种都发出去合并。
    """
    out: List[str] = []
    for q in (title,
              f"{title} {year}" if title and year else "",
              original if original and original.lower() != (title or "").lower() else ""):
        q = (q or "").strip()
        if q and q not in out:
            out.append(q)
    return out


def scan_local_movie(drive, path: str, work: Work,
                     foreign: Optional[List[str]] = None) -> List[LocalFile]:
    """网盘目录里已经存着的这部片，**所有**副本，画质最好的排前面。

    电影比剧集更需要留多份：同一部片的 4K 原盘、1080p WEB-DL、国语配音版
    往往同时存在，而「哪个能播」要播了才知道（原盘 MKV 浏览器多半解不了）。

    跟剧集的 scan_local 相比少了集号这一维，于是**片名判断成了唯一的闸门**：
    一个目录里混进别的片子时，剧集那边还能靠集号对不上兜住，这里兜不住。
    所以同样先看目录里的命名是否带得动片名信号（titles_are_usable），
    带不动就不按片名拒——宁可多留，也不要把用户自己存的片判成「别人的」。
    """
    from .episode import parse_episode

    try:
        fid = drive.resolve_path(path)
        if not fid:
            return []
        entries = drive.list_files(fid)
    except Exception:
        return []

    cand = [f for f in entries if not f.is_dir and is_playable(f.name)]
    if not cand:
        return []
    trust = titles_are_usable([f.name for f in cand], work.titles)
    out: List[LocalFile] = []
    for f in cand:
        why = belongs(f.name, work, trust_titles=trust, title_can_reject=True)
        if why:
            if foreign is not None:
                foreign.append(f.name)
            continue
        info = parse_episode(f.name)      # 只为了拿分辨率/来源标签，集号不用
        out.append(LocalFile(path=f"{path.rstrip('/')}/{f.name}", name=f.name,
                             size=f.size, height=info.height, source=info.source,
                             category=f.category))
    out.sort(key=copy_rank)
    return out


def movie_sources(probes: List[ProbeResult], work: Work,
                  dropped: Optional[List[str]] = None) -> List[SourceFile]:
    """把探测结果摊成一个候选列表：这部片能从哪些分享转。

    没有集号可归位，所以判断只剩两层：整份分享是不是这部作品（share_matches），
    以及单个文件是不是（belongs）。剩下的按画质、体积排——同一部片的多个版本
    都是合法候选，不去重。
    """
    out: List[SourceFile] = []
    alive = [pr for pr in probes if pr.ok]
    paths = {id(pr): [_share_path(pr, f) for f in pr.files] for pr in alive}
    trust = bool(work.titles) and any(share_matches(paths[id(pr)], work.titles)
                                      for pr in alive)
    for pr in alive:
        if trust and not share_matches(paths[id(pr)], work.titles):
            if dropped is not None:
                dropped.append(f"{pr.link.display_title()[:30]}（整份是别的作品）")
            continue
        for f in pr.files:
            if not is_playable(f.name):
                continue
            if f.size and f.size < MIN_MOVIE_BYTES:
                if dropped is not None:
                    dropped.append(f"{f.name[:40]}（太小，多半是预告片）")
                continue
            if work.known():
                why = belongs(_share_path(pr, f), work, trust_titles=trust,
                              require_title=trust)
                if why:
                    if dropped is not None:
                        dropped.append(f"{f.name[:40]}（{why}）")
                    continue
            out.append(SourceFile(share_title=pr.link.display_title(),
                                  share_url=pr.link.url,
                                  passcode=pr.link.passcode or "",
                                  file=f))
    out.sort(key=lambda s: (-s.file.height, -s.file.size))
    return out


def build_movie(
    drive_factory: Callable[[], object],
    search_fn: Optional[Callable],
    tmdb,
    tmdb_id: int,
    *,
    title: str = "",
    local_dir: str = "",
    netdisk: str = "quark",
    probe_top: int = DEFAULT_PROBE_TOP,
    with_sources: bool = True,
    on_step: Optional[Callable[[str, dict], None]] = None,
) -> SeriesView:
    """拼出一部电影的视图。形状跟 build_series 一致，只有一行。"""

    def step(stage, msg, **extra):
        if on_step:
            on_step(stage, dict(extra, message=msg))

    detail = tmdb.movie_detail(tmdb_id)
    name = title or detail.get("title") or ""
    row = EpisodeRow(episode=MOVIE_EPISODE, title=name,
                     air_date=detail.get("release_date") or "")
    view = SeriesView(tmdb_id=tmdb_id, title=name, season=0, seasons=[], rows=[row],
                      media_type="movie", year=detail.get("year") or "",
                      overview=detail.get("overview") or "",
                      poster=detail.get("poster") or "",
                      runtime=int(detail.get("runtime") or 0))
    step("meta", f"{name}（{detail.get('year') or '年份未知'}）")

    drive = drive_factory()
    base = local_dir or f"{getattr(drive, 'save_dir', '/MediaFans').rstrip('/')}/{name}"
    view.local_dir = base
    titles = [t for t in (name, detail.get("original_title") or "") if t]
    # strict_sequel：电影几乎每个热门 IP 都有续集，而片名判断分不开
    # 《流浪地球》和《流浪地球2》（数字被当技术标记剥掉了）。剧集不开这条，
    # 那边 `末日地堡2 E01.mkv` 里的 2 常常是季号。
    work = Work(titles=titles, animation=detail.get("animation"), strict_sequel=True)
    foreign: List[str] = []
    row.copies = scan_local_movie(drive, base, work, foreign)
    step("local", (f"网盘里已经有 {len(row.copies)} 个版本" if len(row.copies) > 1
                   else "网盘里已经有了" if row.copies else "网盘里还没有"),
         saved=1 if row.copies else 0)
    if foreign:
        view.notes.append(
            f"这个目录里还有 {len(foreign)} 个文件不属于本片，已跳过"
            f"（如 {foreign[0][:40]}）")
        step("local", view.notes[-1])

    if not with_sources or search_fn is None or row.local:
        if row.local and with_sources and search_fn is not None:
            view.notes.append("网盘里已经有了，没有再去搜资源")
        return view

    queries = movie_queries(name, detail.get("original_title") or "",
                            detail.get("year") or "")
    step("search", f"用 {len(queries)} 种写法找来源：{'、'.join(queries)}")
    failures: List[str] = []
    links = multi_search(search_fn, queries, netdisk, failures=failures)
    cands = links[:PROBE_CAP]
    if not cands:
        view.notes.append(f"搜索失败：{'；'.join(failures[:2])}" if len(failures) == len(queries)
                          else f"没搜到可用的{netdisk}资源")
        step("error", view.notes[-1])
        return view

    # 跟剧集一样分批探、够用就停。电影的「够用」门槛比剧集低：剧集要凑齐一整季，
    # 电影只要有得挑就行，所以 3 个候选就收手，不必把 24 个全探完。
    probes: List[ProbeResult] = []
    dropped: List[str] = []
    for start in range(0, len(cands), probe_top):
        wave = cands[start:start + probe_top]
        step("probe", (f"正在打开验证 {len(wave)} 个候选…" if not probes else
                       f"前 {len(probes)} 个不够用，再验证 {len(wave)} 个…"))
        probes += probe_many(drive_factory, wave)
        dropped = []
        row.sources = movie_sources(probes, work, dropped)
        if len(row.sources) >= 3:
            break

    view.probed = sum(1 for p in probes if p.ok)
    view.dead = sum(1 for p in probes if not p.ok)
    if view.dead:
        step("probe", f"{view.dead} 个候选打不开（失效/空壳），{view.probed} 个可用")
    if dropped:
        view.notes.append(
            f"跳过了 {len(dropped)} 个不属于本片的文件（如 {dropped[0]}）")
        step("probe", view.notes[-1])
    step("done", (f"{len(row.sources)} 个版本可以转存" if row.sources
                  else "没找到能用的资源"),
         probed=view.probed, dead=view.dead)
    if not row.sources:
        view.notes.append("所有来源里都没有这部片")
    return view
