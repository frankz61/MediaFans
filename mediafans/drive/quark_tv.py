from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from ..auth import QuarkTVLogin, tv_sign
from ..errors import ConfigError, DriveError
from ..models import PlayTarget, StreamVariant

_API = "https://open-api-drive.quark.cn"
_CLIENT_ID = "d3194e61504e493eb6222857bccfed94"
_APP_VER = "1.8.2.2"
_CHANNEL = "GENERAL"
_PLATFORM = "tv"

# 转码档位 -> 展示名（与 PC 版保持一致）
RESOLUTION_LABELS = {
    "4k": "4K", "2k": "2K", "super": "超清", "high": "高清",
    "normal": "标清", "low": "流畅",
}
_RESOLUTIONS = "low,normal,high,super,2k,4k"

_ERRNO_TOKEN_INVALID = (11001, 11002, 14001, 31001)  # token 过期/无效，触发一次刷新
_ERRNO_DEVICE_LIMIT = 32009  # 夸克限制一个账号能绑几台 TV 设备

# 光看 errno 没法自救，给出具体怎么办
_ERRNO_HINTS = {
    _ERRNO_DEVICE_LIMIT: (
        "夸克限制一个账号绑定的 TV 设备数，当前已满。"
        "去夸克 APP -> 我的 -> 设置 -> 设备管理，解绑一台旧设备后重试；"
        "解绑后不用重新扫码，token 和 device_id 都还在。"
    ),
}

# TV 客户端 UA（用于对照测试：直链是只认签名，还是也校验 UA）
QUARK_TV_UA = "Mozilla/5.0 (Linux; Android 11) quark-tv/1.8.2.2"


def _accessable(entry: dict) -> bool:
    """TV 版这个字段有时是布尔、有时是 0/1（audio_info 就是 0），两种都得认."""
    v = entry.get("accessable")
    return True if v is None else bool(v)


class QuarkTVClient:
    """夸克 TV 版接口客户端。

    和 PC 版是两套完全不同的接口：这边走 OAuth，access_token 作为 query 参数下发，
    请求头只有签名，没有 Cookie。直链要不要带凭据是这次要验证的核心问题。
    """

    def __init__(self, token_file: Path, timeout: float = 20.0, transport=None):
        self.token_file = Path(token_file)
        self.timeout = timeout
        self.token = self._load()
        self.client = httpx.Client(timeout=timeout, follow_redirects=True,
                                   transport=transport)

    # ------------------------------------------------------------------ token
    def _load(self) -> Dict[str, str]:
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
        except OSError:
            raise ConfigError(
                f"没有 TV 登录 token（{self.token_file}）。先跑 mediafans login --tv 扫码"
            )
        except ValueError:
            raise ConfigError(f"TV token 文件不是合法 JSON: {self.token_file}")
        if not data.get("access_token"):
            raise ConfigError(f"TV token 文件缺少 access_token: {self.token_file}")
        return data

    def _save(self) -> None:
        self.token_file.write_text(
            json.dumps(self.token, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def device_id(self) -> str:
        return str(self.token.get("device_id") or "")

    def refresh_token(self) -> None:
        """access_token 过期时用 refresh_token 换新的（同样经第三方中转）."""
        rt = str(self.token.get("refresh_token") or "")
        if not rt:
            raise DriveError("TV token 已失效且没有 refresh_token，请重新 mediafans login --tv")
        fresh = QuarkTVLogin(device_id=self.device_id).refresh(rt)
        self.token.update(fresh)
        self._save()

    # ------------------------------------------------------------------ http
    def _headers_params(self, method: str, pathname: str) -> Tuple[dict, dict]:
        tm = str(int(time.time() * 1000))
        headers = {
            "x-pan-tm": tm,
            "x-pan-token": tv_sign(method, pathname, tm),
            "x-pan-client-id": _CLIENT_ID,
        }
        params = {
            "req_id": hashlib.md5((self.device_id + tm).encode()).hexdigest(),
            "access_token": str(self.token.get("access_token") or ""),
            "app_ver": _APP_VER,
            "device_id": self.device_id,
            "platform": _PLATFORM,
            "channel": _CHANNEL,
        }
        return headers, params

    def request(self, pathname: str, params: Optional[dict] = None,
                _retried: bool = False) -> dict:
        headers, base = self._headers_params("GET", pathname)
        base.update(params or {})
        try:
            r = self.client.get(_API + pathname, headers=headers, params=base)
        except httpx.HTTPError as e:
            raise DriveError(f"夸克 TV 接口网络错误 [{pathname}]: {e}")
        try:
            body = r.json()
        except ValueError:
            raise DriveError(f"夸克 TV 接口返回非 JSON [{pathname}] HTTP {r.status_code}")
        if body.get("status") == 0 or body.get("data") is not None:
            return body
        errno = body.get("errno")
        if errno in _ERRNO_TOKEN_INVALID and not _retried:
            self.refresh_token()
            return self.request(pathname, params, _retried=True)
        msg = f"夸克 TV 接口失败 [{pathname}] errno={errno}: {body.get('error_info') or str(body)[:200]}"
        hint = _ERRNO_HINTS.get(errno)
        raise DriveError(msg + ("\n  -> " + hint if hint else ""))

    # ------------------------------------------------------------------ api
    def account_name(self) -> str:
        data = self.request("/user", {"method": "user_info"}).get("data") or {}
        return str(data.get("nickname") or data.get("name") or "")

    def file_link(self, fid: str, streaming: bool = False) -> dict:
        """method=download 拿原画，method=streaming 拿转码档."""
        return self.request("/file", {
            "method": "streaming" if streaming else "download",
            "group_by": "source",
            "fid": fid,
            "resolution": _RESOLUTIONS,
            "support": "dolby_vision",
        })

    def get_play_target(self, fid: str, name: str = "") -> PlayTarget:
        """原画 + 转码档，形状与 PC 版一致，方便上层直接复用."""
        variants: List[StreamVariant] = []
        dl = (self.file_link(fid, streaming=False).get("data") or {})
        download_url = str(dl.get("download_url") or dl.get("url") or "")

        default_key = ""
        try:
            sm = (self.file_link(fid, streaming=True).get("data") or {})
        except DriveError:
            sm = {}
        default_key = str(sm.get("default_resolution") or "")
        # TV 版的转码档在 data.video_info，而且是扁平结构；
        # PC 版叫 data.video_list 且每档再套一层 video_info —— 两边字段名别搞混。
        for e in sm.get("video_info") or []:
            url = str(e.get("url") or "")
            if not url or not _accessable(e):
                continue
            if e.get("trans_status") not in (None, "", "success"):
                continue
            key = str(e.get("resolution") or "")
            variants.append(StreamVariant(
                key=key, label=RESOLUTION_LABELS.get(key, key.upper() or "转码"),
                url=url,
                height=int(e.get("height") or 0), width=int(e.get("width") or 0),
                size=int(e.get("size") or 0), bitrate=float(e.get("bitrate") or 0),
                fmt=str(e.get("format") or ""),
            ))
        variants.sort(key=lambda v: (-v.height, -v.width))
        if download_url:
            variants.append(StreamVariant(
                key="origin", label="原画", url=download_url,
                size=int(dl.get("size") or 0),
                fmt=str(dl.get("format_type") or ""), origin=True,
            ))
        if not variants:
            raise DriveError(f"TV 接口未返回直链 fid={fid}")
        if not any(v.key == default_key for v in variants):
            default_key = variants[0].key
        chosen = next((v for v in variants if v.key == default_key), variants[0])
        # TV 版不下发 cookie —— 直链到底要不要凭据，交给 tv-check 实测
        return PlayTarget(
            file_name=name or str(dl.get("file_name") or ""),
            url=chosen.url, download_url=download_url,
            cookie="", ua="", variants=variants, default_key=default_key,
        )
