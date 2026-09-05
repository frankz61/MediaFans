"""自动找片：把「搜网盘 → 验证 → 挑最合适的 → 转存」这套脏活封起来。

分三层，职责刻意分开：
  probe.py  实际打开分享验证——「避免空资源」靠的是这层，不是 AI
  identity.py 这个文件是不是这部作品的——同名不同作（动画版/真人版）靠它分开
  rank.py   在探测事实上打分排序——没有 AI 也能给出合理结果
  llm.py    可选的 AI 裁决——只在已验证的候选里做选择，解决别名/混装/季号这类模糊问题
  series.py 剧集矩阵——按 TMDB 的季/集把「已存的」和「能补的」铺开，支持按单集转存
"""

from .episode import (EpisodeInfo, SeasonSummary, bare_episode,
                      infer_bare_numbering, parse_episode, summarize)
from .identity import Work, belongs, kind_verdict, title_verdict, titles_are_usable
from .llm import LLMPicker, Verdict
from .pipeline import AutoResult, auto_fetch
from .probe import ProbeFile, ProbeResult, probe_many, probe_share
from .rank import Scored, episode_yardstick, rank, score_one, title_match
from .series import (
    EpisodeRow, LocalFile, SeriesCache, SeriesView, SourceFile,
    BatchGroup, BatchResult, build_series, fetch_batch, fetch_episode,
    index_sources, plan_batch, scan_local,
    cn_season, multi_search, search_queries,
)

__all__ = [
    "EpisodeInfo", "SeasonSummary", "parse_episode", "summarize",
    "bare_episode", "infer_bare_numbering",
    "Work", "belongs", "title_verdict", "kind_verdict", "titles_are_usable",
    "ProbeFile", "ProbeResult", "probe_share", "probe_many",
    "Scored", "rank", "score_one", "title_match", "episode_yardstick",
    "LLMPicker", "Verdict",
    "AutoResult", "auto_fetch",
    "EpisodeRow", "LocalFile", "SourceFile", "SeriesView", "SeriesCache",
    "build_series", "fetch_episode", "scan_local", "index_sources",
    "BatchGroup", "BatchResult", "plan_batch", "fetch_batch",
    "search_queries", "multi_search", "cn_season",
]
