"""外挂字幕：找到它、转成浏览器认的格式。

**为什么只能做外挂**：`<video>` 只认 WebVTT 的 `<track>`，不渲染 MKV 内封字幕。
内封的要么靠播放器自己解（TV 端的 ExoPlayer 可以，外部播放器如 mpv 也可以），
要么在服务端用 ffmpeg 抽——但字幕轨在 MKV 里是**交错存储**的，抽一条字幕等于
把整个文件读一遍，56GB 的 remux 根本不可行。所以网页端这条路只覆盖外挂字幕，
内封的老老实实告诉用户「浏览器放不了，换 TV 端或外部播放器」。

支持 srt / vtt / ass / ssa。前两个几乎是同一种东西，ass 只取 Dialogue 行的文本。
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa")

# 文件名里的语言标记。中文资源的命名习惯很杂，能认出来就标上，认不出就算了
_LANG_HINTS = (
    (("chs", "gb", "简体", "简中", "sc"), "简体中文", "zh-Hans"),
    (("cht", "big5", "繁体", "繁中", "tc"), "繁体中文", "zh-Hant"),
    (("zh", "chi", "中文", "中字"), "中文", "zh"),
    (("eng", "en", "英文"), "English", "en"),
    (("jpn", "jp", "日文"), "日本語", "ja"),
)


def is_subtitle(name: str) -> bool:
    return (name or "").lower().endswith(SUB_EXTS)


def _stem(name: str) -> str:
    return (name or "").rsplit(".", 1)[0].lower()


def describe(name: str) -> Tuple[str, str]:
    """(给人看的标签, BCP-47 语言码)。认不出语言就用文件名当标签。"""
    low = (name or "").lower()
    for keys, label, lang in _LANG_HINTS:
        if any(k in low for k in keys):
            return label, lang
    return (name or "").rsplit(".", 1)[0][-24:], ""


def match_for(video_name: str, names: List[str]) -> List[str]:
    """这个视频该配哪些字幕。

    优先同名（`Movie.2012.mkv` ↔ `Movie.2012.chs.srt`）。一个都没匹配上时，
    如果目录里只有一个视频，就把所有字幕都算它的——单片目录里字幕名字乱起
    是常态（`简体.srt`、`中文字幕.ass`），按名字死抠会一个都找不到。
    """
    subs = [n for n in names if is_subtitle(n)]
    if not subs:
        return []
    stem = _stem(video_name)
    exact = [n for n in subs if _stem(n).startswith(stem) or stem.startswith(_stem(n))]
    return exact or subs


def _ts(value: str) -> str:
    """把 `00:01:02,345` 这种时间戳统一成 VTT 的 `00:01:02.345`."""
    return value.strip().replace(",", ".")


def srt_to_vtt(text: str) -> str:
    out = ["WEBVTT", ""]
    for line in text.splitlines():
        if "-->" in line:
            left, _, right = line.partition("-->")
            out.append(f"{_ts(left)} --> {_ts(right)}")
        else:
            # 纯数字的序号行 VTT 里可有可无，留着也不影响，去掉更干净
            out.append("" if line.strip().isdigit() else line)
    return "\n".join(out)


_ASS_TS = re.compile(r"^(\d+):(\d{2}):(\d{2})[.,](\d{1,3})$")
_ASS_TAGS = re.compile(r"\{[^}]*\}")


def _ass_ts(value: str) -> Optional[str]:
    """ass 的时间是 `0:00:01.23`（厘秒、小时不补零），VTT 要 `00:00:01.230`."""
    m = _ASS_TS.match(value.strip())
    if not m:
        return None
    h, mm, ss, frac = m.groups()
    return f"{int(h):02d}:{mm}:{ss}.{frac.ljust(3, '0')[:3]}"


def ass_to_vtt(text: str) -> str:
    """只取 Dialogue 行的时间和文本。

    ass 的排版能力（位置、字体、特效）VTT 表达不了，硬转只会变成一堆乱码，
    所以样式一概丢掉——能看懂台词就够了。
    """
    out = ["WEBVTT", ""]
    fmt: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("format:") and not fmt:
            fmt = [x.strip().lower() for x in line.split(":", 1)[1].split(",")]
        if not line.lower().startswith("dialogue:"):
            continue
        body = line.split(":", 1)[1]
        # Text 是最后一个字段，它自己可以含逗号，所以按字段数切
        n = len(fmt) if fmt else 10
        parts = body.split(",", n - 1)
        if len(parts) < 3:
            continue
        idx = {k: i for i, k in enumerate(fmt)} if fmt else {"start": 1, "end": 2}
        start = _ass_ts(parts[idx.get("start", 1)])
        end = _ass_ts(parts[idx.get("end", 2)])
        if not start or not end:
            continue
        txt = parts[-1]
        txt = _ASS_TAGS.sub("", txt).replace("\\N", "\n").replace("\\n", "\n")
        if not txt.strip():
            continue
        out += [f"{start} --> {end}", txt, ""]
    return "\n".join(out)


def to_vtt(name: str, data: bytes) -> str:
    """任意字幕 -> WebVTT。

    编码是最容易翻车的一环：中文字幕 utf-8 和 gbk 各占一半，还有带 BOM 的。
    按顺序试，都不行就用 utf-8 忽略错误——宁可少几个字，也别整条字幕打不开。
    """
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "shift_jis"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = data.decode("utf-8", errors="ignore")
    low = (name or "").lower()
    if low.endswith((".ass", ".ssa")):
        return ass_to_vtt(text)
    if low.endswith(".vtt"):
        return text if text.lstrip().startswith("WEBVTT") else "WEBVTT\n\n" + text
    return srt_to_vtt(text)
