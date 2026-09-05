"""看到哪儿了：播放进度与最近观看。

进度存服务端，不存浏览器 localStorage。这个服务是手机、平板、电脑一起用的，
进度只记在某一台上等于没记——「继续观看」的价值全在跨设备接着看。

一条记录对应一个文件（网盘路径唯一）。带上剧集上下文（哪部剧、第几季第几集）之后，
「最近观看」才能按**剧**聚合：一部剧只占一行，显示看到第几集、还能接着看下一集，
而不是把同一部剧的十几集平铺出来。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# 开头 30 秒内不算「看过」——点错了、试个画质都会留下记录，下次从那儿续播很莫名
HEAD = 30.0
# 结尾 20 秒内算看完（片尾曲基本不看）
TAIL = 20.0
LIMIT = 400


@dataclass
class Mark:
    path: str
    name: str = ""
    size_h: str = ""
    position: float = 0.0
    duration: float = 0.0
    updated: float = 0.0
    finished: bool = False
    # 剧集上下文，散片没有
    tmdb_id: Optional[int] = None
    title: str = ""
    year: str = ""
    poster: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    ep_title: str = ""

    @property
    def percent(self) -> int:
        if self.finished:
            return 100
        if not self.duration:
            return 0
        return max(0, min(100, int(self.position / self.duration * 100)))

    @property
    def resume_at(self) -> int:
        """下次从哪儿开始。看完了或才看了个开头就从头来。"""
        if self.finished or self.position < HEAD:
            return 0
        return int(self.position)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["percent"] = self.percent
        d["resume_at"] = self.resume_at
        return d


class WatchStore:
    """进度存储。单用户单文件，够用且看得懂；并发写加锁 + 原子替换。"""

    def __init__(self, path: Path, limit: int = LIMIT):
        self.path = Path(path)
        self.limit = limit
        self._lock = threading.Lock()
        self._marks: Optional[Dict[str, Mark]] = None

    # ---------------------------------------------------------------- 落盘
    def _load(self) -> Dict[str, Mark]:
        if self._marks is not None:
            return self._marks
        marks: Dict[str, Mark] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("marks", []):
                fields = {k: v for k, v in item.items()
                          if k in Mark.__dataclass_fields__}
                if fields.get("path"):
                    marks[fields["path"]] = Mark(**fields)
        except Exception:
            # 文件不在、坏了、手改乱了都不该让播放挂掉，进度丢了就丢了
            marks = {}
        self._marks = marks
        return marks

    def _flush(self) -> None:
        marks = self._marks or {}
        ordered = sorted(marks.values(), key=lambda m: m.updated, reverse=True)
        if len(ordered) > self.limit:
            ordered = ordered[: self.limit]
            self._marks = {m.path: m for m in ordered}
        payload = {"version": 1, "marks": [asdict(m) for m in ordered]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- 读写
    def get(self, path: str) -> Optional[Mark]:
        with self._lock:
            return self._load().get(path)

    def save(self, path: str, position: float = 0.0, duration: float = 0.0,
             **meta) -> Mark:
        if not path:
            raise ValueError("缺少文件路径")
        with self._lock:
            marks = self._load()
            m = marks.get(path) or Mark(path=path)
            m.position = max(0.0, float(position or 0))
            # 刚点开时前端还不知道时长（元数据没加载完）。这时候不能当成 0：
            # `position >= duration - TAIL` 会把 0/0 判成「看完了」，
            # 那一集就永久标成已看完了。时长未知就沿用上次的，也不判完成。
            dur = max(0.0, float(duration or 0))
            m.duration = dur or m.duration
            m.finished = bool(dur and m.position >= dur - TAIL)
            m.updated = time.time()
            for k, v in meta.items():
                if k in Mark.__dataclass_fields__ and v not in (None, ""):
                    setattr(m, k, v)
            marks[path] = m
            self._flush()
            return m

    def forget(self, path: str) -> bool:
        with self._lock:
            marks = self._load()
            if path not in marks:
                return False
            del marks[path]
            self._flush()
            return True

    # ---------------------------------------------------------------- 最近看
    def recent(self, limit: int = 12) -> List[Mark]:
        """最近看过的，一部剧只出最近的那一集。

        同一部剧连看十集会把列表冲满，那样「最近观看」就没法用了。
        """
        with self._lock:
            marks = list(self._load().values())
        marks.sort(key=lambda m: m.updated, reverse=True)
        out: List[Mark] = []
        seen_shows = set()
        for m in marks:
            if m.position < HEAD and not m.finished:
                continue          # 点开就退的不算看过
            if m.tmdb_id:
                if m.tmdb_id in seen_shows:
                    continue
                seen_shows.add(m.tmdb_id)
            out.append(m)
            if len(out) >= limit:
                break
        return out
