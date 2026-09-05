from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from typing import List, Optional, Tuple

from .errors import PlayError
from .models import PlayTarget

# Windows 常见安装路径（按优先级）
_POTPLAYER_PATHS = (
    r"C:\Program Files\PotPlayer\PotPlayerMini64.exe",
    r"C:\Program Files\PotPlayer\PotPlayerMini.exe",
    r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
    r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini.exe",
)
_VLC_PATHS = (
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
)


def detect_player() -> Tuple[str, str]:
    """返回 (播放器类型, 可执行文件路径)，找不到返回 (None, None)。"""
    mpv = shutil.which("mpv") or shutil.which("mpv.exe")
    if mpv:
        return "mpv", mpv
    for p in _POTPLAYER_PATHS:
        if os.path.isfile(p):
            return "potplayer", p
    vlc = shutil.which("vlc") or shutil.which("vlc.exe")
    if not vlc:
        for p in _VLC_PATHS:
            if os.path.isfile(p):
                vlc = p
                break
    if vlc:
        return "vlc", vlc
    return None, None


def _subst(template_item: str, target: PlayTarget, title: str) -> str:
    return (
        template_item.replace("{url}", target.url)
        .replace("{ua}", target.ua)
        .replace("{cookie}", target.cookie)
        .replace("{title}", title or target.file_name)
    )


def build_command(kind: str, exe: str, target: PlayTarget, title: str = "") -> List[str]:
    """生成播放器命令行。不同播放器用各自支持的方式带上 UA/Cookie."""
    title = title or target.file_name
    if kind == "custom":
        cmd = list(exe) if isinstance(exe, list) else _split_command(exe)
        return [_subst(x, target, title) for x in cmd]
    if kind == "mpv":
        cmd = [exe]
        if target.ua:
            cmd.append(f"--http-header-fields=User-Agent: {target.ua}")
        if target.cookie:
            cmd.append(f"--http-header-fields=Cookie: {target.cookie}")
        if title:
            cmd.append(f"--force-media-title={title}")
        cmd.append(target.url)
        return cmd
    if kind == "potplayer":
        # PotPlayer 命令行不支持自定义请求头，直链带校验时建议安装 mpv
        return [exe, "/new", target.url]
    if kind == "vlc":
        cmd = [exe]
        if target.ua:
            cmd.append(f"--http-user-agent={target.ua}")
        cmd.append(target.url)
        return cmd
    raise PlayError(f"未知播放器类型: {kind}")


def _strip_quotes(tok: str) -> str:
    for q in ('"', "'"):
        if len(tok) >= 2 and tok.startswith(q) and tok.endswith(q):
            return tok[1:-1]
    return tok


def _split_command(s: str) -> List[str]:
    # posix=False 保住 Windows 反斜杠路径，再去掉 token 首尾及 = 后的引号
    out = []
    for x in shlex.split(s, posix=False):
        x = x.strip()
        if not x:
            continue
        x = re.sub(
            r'=(?:"([^"]*)"|\'([^\']*)\')',
            lambda m: "=" + (m.group(1) if m.group(1) is not None else m.group(2)),
            x,
        )
        out.append(_strip_quotes(x))
    return out


def launch(target: PlayTarget, player_cfg: Optional[dict], title: str = "",
           prefer: str = "auto"):
    """启动播放器。返回 (使用的播放器类型, 命令行, 是否启动成功, 播放器进程或 None)."""
    player_cfg = player_cfg or {}
    command_cfg = player_cfg.get("command")

    if prefer in ("none",):
        return "none", [target.url], False, None

    if command_cfg and prefer in ("auto", "custom"):
        cmd = build_command("custom", command_cfg, target, title)
        return "custom", cmd, True, spawn(cmd)

    if prefer == "custom":
        raise PlayError("未配置 player.command，无法使用 custom 播放器")

    kind, exe = detect_player()
    if prefer != "auto":
        kind, exe = (prefer, shutil.which(prefer) or shutil.which(f"{prefer}.exe"))
        if prefer == "potplayer":
            for p in _POTPLAYER_PATHS:
                if os.path.isfile(p):
                    exe = p
                    break
        if not exe:
            raise PlayError(f"找不到播放器: {prefer}")

    if kind is None:
        if prefer == "system" or os.name == "nt":
            if hasattr(os, "startfile"):
                try:
                    os.startfile(target.url)  # noqa: PTH  Windows only
                    return "system", [target.url], True, None
                except OSError:
                    pass
        return "none", [target.url], False, None

    cmd = build_command(kind, exe, target, title)
    return kind, cmd, True, spawn(cmd)


def spawn(cmd: List[str], wait: bool = False):
    """启动外部播放器进程；wait=True 时阻塞到播放器退出。"""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        if wait:
            proc.wait()
        return proc
    except OSError as e:
        raise PlayError(f"播放器启动失败: {' '.join(cmd)}\n{e}")
