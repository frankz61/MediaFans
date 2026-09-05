from __future__ import annotations

import re
import time
from typing import Any, List, Optional, Sequence

# 常见网盘域名 → 统一类型名
_NETDISK_HOSTS = (
    ("pan.quark.cn", "quark"),
    ("quark.cn", "quark"),
    ("pan.baidu.com", "baidu"),
    ("alipan.com", "aliyun"),
    ("aliyundrive.com", "aliyun"),
    ("pan.xunlei.com", "xunlei"),
    ("cloud.189.cn", "tianyi"),
    ("115.com", "pan115"),
    ("123pan.com", "pan123"),
    ("lanzou", "lanzou"),
    ("weiyun.com", "weiyun"),
    ("mypikpak.com", "pikpak"),
)

# PanSou / 各源返回的类型别名 → 统一类型名
_NETDISK_ALIASES = {
    "quark": "quark",
    "baidu": "baidu",
    "baidupan": "baidu",
    "aliyun": "aliyun",
    "aliyundrive": "aliyun",
    "ali": "aliyun",
    "xunlei": "xunlei",
    "thunder": "xunlei",
    "tianyi": "tianyi",
    "189": "tianyi",
    "115": "pan115",
    "pan115": "pan115",
    "123pan": "pan123",
    "uc": "uc",
    "pikpak": "pikpak",
    "lanzou": "lanzou",
    "weiyun": "weiyun",
}

_QUARK_SHARE_RE = re.compile(r"pan\.quark\.cn/s/([A-Za-z0-9_-]+)")
_QUARK_PASS_RE = re.compile(r"[?&]pwd=([A-Za-z0-9]{4})")
# /s/1xxxx 的 surl 不含前缀 1；/share/init?surl=xxxx 本来就不带 1
_BAIDU_S_RE = re.compile(r"pan\.baidu\.com/s/1([A-Za-z0-9_-]+)")
_BAIDU_INIT_RE = re.compile(r"[?&]surl=([A-Za-z0-9_-]+)")
_BAIDU_PASS_RE = re.compile(r"[?&]pwd=([A-Za-z0-9]{4})")


def now_ms() -> int:
    return int(time.time() * 1000)


def classify_netdisk(url: str) -> str:
    """根据分享 URL 域名推断网盘类型."""
    url = (url or "").lower()
    for host, name in _NETDISK_HOSTS:
        if host in url:
            return name
    return ""


def normalize_netdisk(t: str) -> str:
    t = (t or "").strip().lower()
    return _NETDISK_ALIASES.get(t, t)


def parse_quark_share(url: str) -> Optional[tuple]:
    """从夸克分享链接提取 (pwd_id, passcode)。passcode 可能为空。"""
    m = _QUARK_SHARE_RE.search(url or "")
    if not m:
        return None
    pwd_id = m.group(1)
    pm = _QUARK_PASS_RE.search(url)
    return pwd_id, (pm.group(1) if pm else "")


def parse_baidu_share(url: str) -> Optional[tuple]:
    """从百度分享链接提取 (surl, passcode, legacy)。

    surl 不含前缀 1；旧式 uk/shareid 链接返回 ("", "", {"uk": .., "shareid": ..})。
    """
    u = url or ""
    m = _BAIDU_S_RE.search(u) or _BAIDU_INIT_RE.search(u)
    if m:
        pm = _BAIDU_PASS_RE.search(u)
        return m.group(1), (pm.group(1) if pm else ""), None
    sid = re.search(r"[?&]shareid=(\d+)", u)
    uk = re.search(r"[?&]uk=(\d+)", u)
    if sid and uk:
        return "", "", {"shareid": sid.group(1), "uk": uk.group(1)}
    return None


def json_path_get(obj: Any, path: str) -> Any:
    """按 a.b.0.c 形式的路径取嵌套值，取不到返回 None."""
    cur = obj
    for key in (path or "").split("."):
        if key == "":
            continue
        if isinstance(cur, list) and key.isdigit():
            idx = int(key)
            if idx >= len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            if key not in cur:
                return None
            cur = cur[key]
        else:
            return None
    return cur


def fmt_size(n) -> str:
    if n is None or n == "":
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return "-"


def norm_path(p: str) -> str:
    """把任意路径规范成 /a/b 形式（根为 /）。"""
    parts = [x for x in (p or "").replace("\\", "/").split("/") if x not in ("", ".")]
    return "/" + "/".join(parts)


def path_segments(p: str) -> List[str]:
    return [x for x in norm_path(p).split("/") if x]


def join_set_cookies(raw_cookies: Sequence[str]) -> str:
    """把 HTTP Set-Cookie 列表拼成可复用的 Cookie 请求头值."""
    pairs = []
    for raw in raw_cookies or []:
        first = raw.split(";", 1)[0].strip()
        if first and "=" in first:
            pairs.append(first)
    return "; ".join(pairs)


# 按扩展名分类，用来把「能看的」和「垃圾文件」分开
VIDEO_EXT = frozenset(
    ".mkv .mp4 .ts .m2ts .avi .wmv .mov .flv .webm .rmvb .rm .mpg .mpeg .m4v .iso".split()
)
AUDIO_EXT = frozenset(".mp3 .flac .m4a .wav .ape .ogg .aac .wma .dsf".split())
SUBTITLE_EXT = frozenset(".srt .ass .ssa .sub .idx .sup .vtt".split())


def media_kind(name: str) -> str:
    """video | audio | subtitle | other —— other 基本就是说明.txt / 广告图片这类."""
    dot = (name or "").rfind(".")
    ext = (name[dot:] if dot > 0 else "").lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in SUBTITLE_EXT:
        return "subtitle"
    return "other"


def is_playable(name: str) -> bool:
    return media_kind(name) in ("video", "audio")


def merge_cookies(*cookie_headers: str) -> str:
    """合并多个 Cookie 头，同名以后者为准（夸克每个直链接口都会重发一次 __puus）."""
    merged = {}
    for header in cookie_headers:
        for pair in (header or "").split(";"):
            name, sep, value = pair.strip().partition("=")
            if sep and name:
                merged[name] = value
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def match_tokens(query: str, name: str) -> bool:
    """所有空白分隔的关键词都出现（不区分大小写）才算命中."""
    tokens = [t for t in (query or "").lower().split() if t]
    if not tokens:
        return True
    target = (name or "").lower()
    return all(t in target for t in tokens)


def truncate(s: str, width: int = 48) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "…"
