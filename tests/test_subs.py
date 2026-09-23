"""外挂字幕：找到它、转成浏览器认的 WebVTT。

只做外挂——`<video>` 只认 WebVTT 的 `<track>`，MKV 内封字幕浏览器不渲染，
而服务端抽内封要把整个文件读一遍（字幕轨交错存储），几十 GB 的 remux 不可行。
"""

import pytest

from mediafans.subs import (
    ass_to_vtt,
    describe,
    is_subtitle,
    match_for,
    srt_to_vtt,
    to_vtt,
)


def test_srt_timestamps_become_vtt():
    out = srt_to_vtt("1\n00:00:01,000 --> 00:00:03,500\n你好\n")
    assert out.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:03.500" in out      # 逗号要变点
    assert "你好" in out
    assert "\n1\n" not in out                          # 纯数字序号行去掉


def test_ass_keeps_the_lines_and_drops_the_styling():
    """ass 的排版（位置、字体、特效）VTT 表达不了，硬转只会变成乱码。"""
    ass = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        r"Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\an8\fs20}你好\N世界" + "\n"
        r"Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,{\pos(1,2)}" + "\n"
    )
    out = ass_to_vtt(ass)
    assert "00:00:01.000 --> 00:00:03.500" in out      # 厘秒补成毫秒、小时补零
    assert "你好\n世界" in out                          # \N 是换行
    assert "\\an8" not in out and "{" not in out        # 样式标记全去掉
    assert out.count("-->") == 1                        # 只有标记、没有文字的那行丢掉


def test_text_with_commas_survives():
    """Text 是最后一个字段，它自己含逗号时不能被切碎."""
    ass = (
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,一，二，三\n"
    )
    assert "一，二，三" in ass_to_vtt(ass)


def test_gbk_subtitles_decode():
    """中文字幕 utf-8 和 gbk 各占一半，认错编码就是一屏乱码."""
    data = "1\n00:00:01,000 --> 00:00:02,000\n中文字幕\n".encode("gb18030")
    assert "中文字幕" in to_vtt("x.srt", data)


def test_undecodable_bytes_still_produce_something():
    """宁可少几个字，也别整条字幕打不开."""
    out = to_vtt("x.srt", b"\xff\xfe\x00bad bytes\n")
    assert out.startswith("WEBVTT")


def test_vtt_passes_through_and_gets_a_header_if_missing():
    assert to_vtt("a.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n").count("WEBVTT") == 1
    assert to_vtt("a.vtt", b"00:00:01.000 --> 00:00:02.000\nhi\n").startswith("WEBVTT")


# ---------------------------------------------------------------- 匹配
def test_same_stem_wins():
    got = match_for("Movie.2012.mkv",
                    ["Movie.2012.mkv", "Movie.2012.chs.srt", "Other.Show.ass"])
    assert got == ["Movie.2012.chs.srt"]


def test_falls_back_to_everything_when_nothing_matches():
    """单片目录里字幕名字乱起是常态（`简体.srt`、`中文字幕.ass`），
    按名字死抠会一个都找不到。"""
    assert match_for("Movie.mkv", ["Movie.mkv", "简体.srt", "繁體.ass"]) == \
        ["简体.srt", "繁體.ass"]


def test_no_subtitles_means_empty():
    assert match_for("Movie.mkv", ["Movie.mkv", "cover.jpg"]) == []


@pytest.mark.parametrize("name,lang", [
    ("Movie.chs.srt", "zh-Hans"),
    ("Movie.简体.ass", "zh-Hans"),
    ("Movie.cht.srt", "zh-Hant"),
    ("Movie.eng.srt", "en"),
    ("Movie.中字.srt", "zh"),
])
def test_language_is_guessed_from_the_name(name, lang):
    assert describe(name)[1] == lang


def test_unknown_language_still_gets_a_label():
    label, lang = describe("随便什么.srt")
    assert label and lang == ""


def test_is_subtitle():
    assert all(is_subtitle(n) for n in ("a.srt", "a.ASS", "a.vtt", "a.ssa"))
    assert not any(is_subtitle(n) for n in ("a.mkv", "a.srt.mkv", "a.txt", ""))
