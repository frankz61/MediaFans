from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MediaItem:
    """影视条目（来自 TMDB 等元数据源）."""

    title: str
    original_title: str = ""
    original_language: str = ""   # TMDB 的 ISO 639-1，华语是 zh（国语）/ cn（粤语）
    genres: List[int] = field(default_factory=list)   # TMDB genre id，16 = 动画
    media_type: str = "movie"  # movie | tv
    year: str = ""
    rating: float = 0.0
    overview: str = ""
    tmdb_id: int = 0
    poster: str = ""
    backdrop: str = ""


@dataclass
class ShareLink:
    """网盘分享链接（来自搜索聚合源）."""

    url: str
    netdisk: str = ""  # quark / aliyun / baidu / xunlei / ...
    passcode: str = ""
    title: str = ""
    note: str = ""
    datetime: str = ""
    source: str = ""

    def display_title(self) -> str:
        return self.title or self.note[:60] or self.url


@dataclass
class DriveFile:
    """网盘文件/目录（自己的盘 或 分享内）."""

    fid: str
    name: str
    is_dir: bool = False
    size: int = 0
    updated_at: str = ""
    share_fid_token: str = ""  # 仅分享文件列表里有，转存时需要
    # 网盘自己给这个文件打的类型（video / image / doc / …），列目录时白送。
    # 值得留着：夸克只给它认成 video 的文件转码，认成别的（实测有一批 .mkv 被
    # 标成 image/png）就只有原画一档，播不动也换不了档。空字符串 = 网盘没说。
    category: str = ""


@dataclass
class StreamVariant:
    """一档可播放的清晰度（网盘转码档 或 原画）."""

    key: str            # 4k / 2k / super / high / low / origin —— 前端记忆偏好用
    label: str          # 4K / 超清 / 原画 …
    url: str
    height: int = 0
    width: int = 0
    size: int = 0       # 字节
    bitrate: float = 0.0  # kbps
    fmt: str = ""       # mp4 / m3u8
    origin: bool = False  # 原画（未转码），浏览器可能解不了

    def display(self) -> str:
        return f"{self.label} {self.height}P" if self.height else self.label


@dataclass
class PlayTarget:
    """直链播放目标：URL + 访问所需的请求头 + 全部可选清晰度."""

    file_name: str
    url: str = ""
    download_url: str = ""
    cookie: str = ""
    ua: str = ""
    variants: List["StreamVariant"] = field(default_factory=list)
    default_key: str = ""  # 建议默认播放的档位（网盘给的 default_resolution）

    def headers(self) -> dict:
        h = {}
        if self.ua:
            h["User-Agent"] = self.ua
        if self.cookie:
            h["Cookie"] = self.cookie
        return h


@dataclass
class SearchHit:
    """find 命令的命中结果."""

    path: str
    file: DriveFile

    def __str__(self) -> str:
        kind = "目录" if self.file.is_dir else "文件"
        return f"[{kind}] {self.path}"


def optional_str(v) -> Optional[str]:
    return v if isinstance(v, str) and v else None
