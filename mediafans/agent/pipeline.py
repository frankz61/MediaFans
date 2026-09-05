"""一键找片：搜索 -> 探测 -> 排序 -> (AI 裁决) -> 转存。

对用户来说只有「点一下」，网盘那侧的脏活全在这里。
每一步都往 on_step 回调里报进度——整条链路要跑十几秒到几十秒，
不给反馈的话用户会以为卡死了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..errors import MediaFansError
from ..models import ShareLink
from .llm import LLMPicker, Verdict
from .probe import ProbeResult, probe_many
from .rank import Scored, rank

# 探测很贵（每个候选要开分享、递归列目录），只探前 N 个
DEFAULT_PROBE_TOP = 6
# 一批不够就再探一批，最多探到这么多。搜索源前几条常常是死链，全靠它兜底
PROBE_CAP = 24
# 探到几个能用的就可以停手了——再多也只是给排序多几个选项
ENOUGH_ALIVE = 3
DEFAULT_WORKERS = 4


@dataclass
class AutoResult:
    query: str
    picked: Optional[Scored] = None
    scored: List[Scored] = field(default_factory=list)
    verdict: Optional[Verdict] = None
    ai_used: bool = False
    ai_note: str = ""
    saved: int = 0
    requested: int = 0
    already: int = 0        # 目标目录里本来就有的可播放文件数（已存在则跳过转存）
    saved_dir: str = ""
    steps: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return (self.picked is not None or self.already > 0) and not self.error


def _prefilter(links: List[ShareLink], netdisk: str, limit: int) -> List[ShareLink]:
    """探测前先按能不能用筛一遍：只有夸克能转存，重复链接去掉。"""
    seen, out = set(), []
    for l in links:
        if netdisk and l.netdisk != netdisk:
            continue
        key = l.url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(l)
        if len(out) >= limit:
            break
    return out


def auto_fetch(
    drive_factory: Callable[[], object],
    search_fn: Callable,
    query: str,
    *,
    year: str = "",
    season: Optional[int] = None,
    episodes: int = 0,
    netdisk: str = "quark",
    save_dir: str = "",
    picker: Optional[LLMPicker] = None,
    probe_top: int = DEFAULT_PROBE_TOP,
    workers: int = DEFAULT_WORKERS,
    do_save: bool = True,
    force: bool = False,
    work=None,
    on_step: Optional[Callable[[str, dict], None]] = None,
) -> AutoResult:
    """找到并转存最合适的资源。

    on_step(阶段, 详情) 会在每个阶段被调用，供网页端显示进度。
    """
    res = AutoResult(query=query)

    def step(stage: str, msg: str, **extra):
        res.steps.append(msg)
        if on_step:
            on_step(stage, dict(extra, message=msg))

    # 1) 搜索
    step("search", f"正在搜索「{query}」…")
    try:
        links, errors = search_fn(query, netdisk or None)
    except Exception as e:
        res.error = f"搜索失败: {str(e)[:120]}"
        step("error", res.error)
        return res
    if errors:
        step("search", f"（{len(errors)} 个搜索源异常，已跳过）")
    candidates = _prefilter(links, netdisk, PROBE_CAP)
    if not candidates:
        res.error = "没有搜到可用的网盘资源"
        step("error", res.error)
        return res
    step("search", f"搜到 {len(links)} 条，逐批实际验证",
         total=len(links), probing=len(candidates))

    # 2) 探测——这一步才能真正排除死链和空壳。
    # 分批探，够用就停：搜索源返回的前几条经常是死链（实测「异人之下」前 6 条
    # 全是「好友已取消了分享」），只探一批会得到「全部无效」；
    # 但一上来就探满，正常情况下白等好几倍时间。
    probes: List[ProbeResult] = []
    for start in range(0, len(candidates), probe_top):
        wave = candidates[start:start + probe_top]
        step("probe", (f"正在逐个打开验证（{len(wave)} 个）…" if not probes else
                       f"前 {len(probes)} 个都不可用，再验证 {len(wave)} 个…"))
        probes += probe_many(drive_factory, wave, workers=workers)
        if sum(1 for p in probes if p.ok) >= ENOUGH_ALIVE:
            break
    alive = [p for p in probes if p.ok]
    dead = [p for p in probes if not p.ok]
    if dead:
        step("probe", f"排除 {len(dead)} 个无效资源："
                      + "；".join(f"{p.link.display_title()[:18]}→{p.error}" for p in dead[:3]))
    if work is not None and getattr(work, "known", lambda: False)():
        # 同名不同作（《一人之下》动画 / 《异人之下》真人）会互相搜出来，
        # 里面一个属于本作的文件都没有的分享直接排除，别让它进排序
        from .identity import belongs, share_matches

        def paths_of(p):
            return [f"{p.link.display_title()}/{f.crumb}/{f.name}" for f in p.files]

        trust = work.titles and any(share_matches(paths_of(p), work.titles) for p in alive)

        def mine(p):
            if trust and not share_matches(paths_of(p), work.titles):
                return False        # 整份没提过这部作品，多半是同名的另一部
            return any(belongs(q, work, trust_titles=bool(trust)) is None
                       for q in paths_of(p))

        keep = [p for p in alive if mine(p)]
        if keep and len(keep) < len(alive):
            wrong = [p for p in alive if p not in keep]
            step("probe", f"排除 {len(wrong)} 个同名的另一部作品："
                          + "；".join(p.link.display_title()[:20] for p in wrong[:3]))
            alive = keep
    if not alive:
        res.error = "候选资源全部无效（死链、空目录或只有广告文件）"
        step("error", res.error)
        return res
    step("probe", f"{len(alive)} 个资源验证通过", alive=len(alive), dead=len(dead))

    # 3) 确定性排序
    res.scored = rank(alive, query, year, season, episodes)
    best = res.scored[0]
    step("rank", f"初选：{best.title[:40]}（{'、'.join(best.reasons[:3])}）")

    # 4) AI 裁决（可选）——只在已验证的事实上做选择
    if picker is not None and picker.available and len(res.scored) > 1:
        step("ai", "让 AI 复核哪个才是要找的剧…")
        # 没指定季时别把某一季的年份/集数说成硬需求——之前就是这么写的，
        # 模型据此把「第二季 36 集」判成不匹配「2019 年 46 集」，好资源全被拒了
        want = {"标题": query}
        if work is not None and getattr(work, "animation", None) is not None:
            # 同名的动画版和真人版是最常撞的一对，明确告诉模型要哪一种
            want["类型"] = "动画" if work.animation else "真人（非动画）"
        if season is not None:
            want["指定季"] = season
            if episodes:
                want["该季集数"] = episodes
            if year:
                want["年份"] = year
        else:
            want["季"] = "未指定，任意一季都可以（通常想看最新的那季）"
            if episodes:
                want["各季集数（仅供参考，用来判断某一季完不完整）"] = episodes
            if year:
                want["首播年份（仅供参考，不是筛选条件）"] = year
        verdict = picker.pick(want, [s.probe.brief() for s in res.scored])
        if verdict is None:
            res.ai_note = "AI 未给出有效判断，按评分选择"
            step("ai", res.ai_note)
        elif verdict.reject:
            res.verdict = verdict
            res.ai_used = True
            res.error = f"AI 判断没有匹配的资源：{verdict.reason}"
            step("error", res.error)
            return res
        else:
            res.verdict = verdict
            res.ai_used = True
            best = res.scored[verdict.index]
            step("ai", f"AI 选择：{best.title[:40]} —— {verdict.reason}",
                 confidence=verdict.confidence)
    elif picker is not None and not picker.available:
        res.ai_note = picker.unavailable_reason or "未启用 AI"

    res.picked = best

    # 5) 转存
    if not do_save:
        step("done", f"已选中：{best.title[:40]}（未转存）")
        return res
    target_dir = (save_dir or getattr(drive_factory(), "save_dir", "/MediaFans"))
    target_dir = f"{target_dir.rstrip('/')}/{_safe_name(query)}"

    # 「点一下就好」的功能天然会被重复点。不查一下的话，网盘会把同名文件
    # 存成「xxx(1).mkv」，点几次就多几份副本。
    if not force:
        existing = _existing_playable(drive_factory, target_dir)
        if existing:
            res.saved_dir = target_dir
            res.already = existing
            step("done", f"{target_dir} 里已经有 {existing} 个可播放文件，跳过转存",
                 dir=target_dir, already=existing)
            return res

    step("save", f"正在转存 {best.probe.playable} 个文件…")
    try:
        drive = drive_factory()
        ctx = drive.open_share(best.probe.link.url,
                              passcode=best.probe.link.passcode or "")
        files = drive.list_share_files(ctx, best.probe.dir_fid)
        wanted = [f for f in files if not f.is_dir and _playable(f.name)]
        if not wanted:
            raise MediaFansError("这一层没有可转存的文件")
        saved = drive.save_share_files(ctx, wanted, target_dir)
        res.saved, res.saved_dir = len(saved), target_dir
        res.requested = len(wanted)
    except Exception as e:
        res.error = f"转存失败: {str(e)[:140]}"
        step("error", res.error)
        return res
    # 网盘对目标目录里已存在的文件会去重，请求数和落地数对不上是正常的，说清楚
    extra = (f"（请求 {res.requested} 个，其余已存在或被网盘合并）"
             if res.requested and res.saved != res.requested else "")
    step("done", f"已转存 {res.saved} 个文件到 {res.saved_dir}{extra}",
         saved=res.saved, dir=res.saved_dir)
    return res


def _existing_playable(drive_factory, path: str) -> int:
    """目标目录里已经有几个可播放文件。目录不存在或查不了都当 0."""
    try:
        drive = drive_factory()
        fid = drive.resolve_path(path)
        if not fid:
            return 0
        return sum(1 for f in drive.list_files(fid)
                   if not f.is_dir and _playable(f.name))
    except Exception:
        return 0


def _playable(name: str) -> bool:
    from ..utils import is_playable

    return is_playable(name)


def _safe_name(name: str) -> str:
    """做目录名：去掉网盘不接受的字符。"""
    bad = '\\/:*?"<>|'
    cleaned = "".join(("_" if c in bad else c) for c in (name or "").strip())
    return cleaned[:60] or "未命名"
