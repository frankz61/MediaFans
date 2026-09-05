from __future__ import annotations

import random
import time
from typing import List, Optional, Sequence

import httpx

from ..errors import ConfigError, DriveError
from ..models import DriveFile, PlayTarget, StreamVariant
from ..tokens import TokenRelay
from ..utils import join_set_cookies, merge_cookies, now_ms, parse_quark_share
from .base import BaseDrive, ShareContext

# 夸克 PC 客户端 UA（直链通常校验 UA，保持与请求直链时一致）
QUARK_PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 Electron/24.1.3.8 "
    "Safari/537.36 Channel/pckk_other_ch"
)

# 夸克转码档位 -> 展示名（与夸克客户端一致）
RESOLUTION_LABELS = {
    "4k": "4K", "2k": "2K", "super": "超清", "high": "高清",
    "normal": "标清", "low": "流畅",
}


class QuarkDrive(BaseDrive):
    """夸克网盘驱动（逆向 PC 端接口，参数与 quark-auto-save 项目对齐）."""

    name = "quark"
    API = "https://drive-pc.quark.cn/1/clouddrive"
    ACCOUNT_API = "https://pan.quark.cn/account/info"

    def __init__(self, cfg: dict, transport=None):
        super().__init__(cfg)
        self.transport = transport
        self.cookie_source = ""  # token_provider | login | config
        self.cookie = self._acquire_cookie()
        self.ua = str(cfg.get("ua") or QUARK_PC_UA)
        self.save_dir = str(cfg.get("save_dir") or "/MediaFans")
        self.client = httpx.Client(
            headers={
                "user-agent": self.ua,
                "referer": "https://pan.quark.cn/",
                "cookie": self.cookie,
                "content-type": "application/json",
            },
            timeout=float(cfg.get("timeout") or 20),
            transport=transport,
        )

    def _acquire_cookie(self) -> str:
        """凭据优先级: token 中转站 > 扫码登录缓存文件 > 配置文件静态 cookie."""
        provider = (self.cfg or {}).get("token_provider")
        if provider:
            self.cookie_source = "token_provider"
            return TokenRelay(provider).fetch_cookie()
        cookie_file = (self.cfg or {}).get("cookie_file")
        if cookie_file:
            try:
                from pathlib import Path

                text = Path(cookie_file).read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                self.cookie_source = "login"
                return text
        cookie = str((self.cfg or {}).get("cookie") or "").strip()
        if cookie:
            self.cookie_source = "config"
            return cookie
        raise ConfigError(
            "未配置夸克凭据。三种方式任选：\n"
            "  1. mediafans login        扫码登录（推荐，自动保存）\n"
            "  2. drive.quark.cookie     浏览器复制 cookie\n"
            "  3. drive.quark.token_provider  对接 token 中转站"
        )

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 base: Optional[str] = None) -> tuple:
        url = (base or self.API) + path
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params.setdefault("pr", "ucpro")
        params.setdefault("fr", "pc")
        params.setdefault("__dt", random.randint(60, 300) * 1000)
        params.setdefault("__t", now_ms())
        try:
            r = self.client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as e:
            raise DriveError(f"夸克接口网络错误 [{path}]: {e}")
        if r.status_code >= 500:
            raise DriveError(f"夸克接口 HTTP {r.status_code} [{path}]")
        try:
            body = r.json()
        except ValueError:
            raise DriveError(f"夸克接口返回非 JSON [{path}] HTTP {r.status_code}")
        status, code = body.get("status"), body.get("code")
        if "success" in body:
            # account/info 等接口用 success 布尔语义（可能同时带非 200 的 status 字段）
            ok = bool(body.get("success"))
        else:
            ok = (status is None or status == 200) and (code is None or code == 0)
        if not ok:
            msg = body.get("message") or str(body)[:200]
            raise DriveError(f"夸克接口失败 [{path}]: {msg}")
        return body.get("data", body), r

    # ------------------------------------------------------------------ 账号
    def account_name(self) -> str:
        data, _ = self._request(
            "GET", "", params={"fr": "pc", "platform": "pc"}, base=self.ACCOUNT_API
        )
        name = (data or {}).get("nickname") if isinstance(data, dict) else None
        return name or ""

    # ------------------------------------------------------------------ 文件
    @staticmethod
    def _fmt_time(v) -> str:
        """updated_at 可能是 ISO 字符串，也可能是毫秒时间戳."""
        s = str(v or "")
        if s.isdigit() and len(s) >= 12:
            from datetime import datetime

            try:
                return datetime.fromtimestamp(int(s) / 1000).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError, OverflowError):
                pass
        return s[:19]

    @staticmethod
    def _to_file(it: dict) -> DriveFile:
        return DriveFile(
            fid=str(it.get("fid") or ""),
            name=it.get("file_name") or "",
            is_dir=bool(it.get("dir", False)),
            size=int(it.get("size") or 0),
            updated_at=QuarkDrive._fmt_time(it.get("updated_at")),
        )

    def list_files(self, dir_fid: str = "0", page_size: int = 100) -> List[DriveFile]:
        files: List[DriveFile] = []
        page = 1
        while True:
            data, _ = self._request(
                "GET", "/file/sort",
                params={
                    "pdir_fid": dir_fid,
                    "_page": page,
                    "_size": page_size,
                    "_fetch_total": 1,
                    "_fetch_sub_dirs": 0,
                    "_sort": "file_type:asc,updated_at:desc",
                    "fetch_risk_file_name": 1,
                },
            )
            lst = data.get("list") or []
            files.extend(self._to_file(x) for x in lst)
            total = (data.get("metadata") or {}).get("_total")
            total = int(total) if total is not None else len(files)
            if not lst or len(files) >= total or page > 100:
                break
            page += 1
        return files

    def resolve_path(self, path: str) -> Optional[str]:
        from ..utils import norm_path

        p = norm_path(path)
        if p == "/":
            return "0"
        data, _ = self._request(
            "POST", "/file/info/path_list", json_body={"file_path": [p], "namespace": "0"}
        )
        lst = data if isinstance(data, list) else (data or {}).get("list") or []
        for it in lst:
            if it.get("file_path") in (p, p.rstrip("/")) or len(lst) == 1:
                return str(it.get("fid") or "") or None
        return None

    def ensure_dir(self, path: str) -> str:
        from ..utils import norm_path

        p = norm_path(path)
        if p == "/":
            return "0"
        fid = self.resolve_path(p)
        if fid:
            return fid
        data, _ = self._request(
            "POST", "/file",
            json_body={
                "pdir_fid": "0",
                "file_name": p.strip("/").split("/")[-1],
                "dir_path": p,
                "dir_init_lock": False,
            },
        )
        fid = str((data or {}).get("fid") or "")
        if fid:
            return fid
        # 并发/重复创建冲突时目录实际已存在，再查一次
        fid = self.resolve_path(p)
        if fid:
            return fid
        raise DriveError(f"创建目录失败: {p}")

    # ------------------------------------------------------------------ 分享
    def open_share(self, share_url: str, passcode: str = "") -> ShareContext:
        parsed = parse_quark_share(share_url or "")
        if not parsed:
            raise DriveError(f"无法从链接解析夸克分享 ID: {share_url}")
        pwd_id, url_passcode = parsed
        code = passcode or url_passcode
        data, _ = self._request(
            "POST", "/share/sharepage/token", json_body={"pwd_id": pwd_id, "passcode": code}
        )
        stoken = (data or {}).get("stoken")
        if not stoken:
            raise DriveError("获取分享 stoken 失败（分享可能已失效）")
        return ShareContext(url=share_url, pwd_id=pwd_id, passcode=code, stoken=stoken)

    def list_share_files(self, ctx: ShareContext, dir_fid: str = "0") -> List[DriveFile]:
        files: List[DriveFile] = []
        page = 1
        while True:
            data, _ = self._request(
                "GET", "/share/sharepage/detail",
                params={
                    "pwd_id": ctx.pwd_id,
                    "stoken": ctx.stoken,
                    "pdir_fid": dir_fid,
                    "force": 0,
                    "_page": page,
                    "_size": 50,
                    "_fetch_banner": 0,
                    "_fetch_share": 0,
                    "_fetch_total": 1,
                    "_sort": "file_type:asc,updated_at:desc",
                },
            )
            lst = data.get("list") or []
            for it in lst:
                f = self._to_file(it)
                f.share_fid_token = str(it.get("share_fid_token") or "")
                files.append(f)
            total = (data.get("metadata") or {}).get("_total")
            total = int(total) if total is not None else len(files)
            if not lst or len(files) >= total or page > 100:
                break
            page += 1
        return files

    def save_share_files(self, ctx: ShareContext, files: Sequence[DriveFile],
                         to_dir: str) -> List[str]:
        to_fid = self.ensure_dir(to_dir)
        fids = [f.fid for f in files if f.fid]
        tokens = [f.share_fid_token for f in files if f.fid]
        if not fids:
            raise DriveError("没有可转存的文件（缺少 fid）")
        if any(not t for t in tokens):
            raise DriveError("缺少 share_fid_token，请先用 open_share + list_share_files 获取文件列表")

        saved: List[str] = []
        batch = 50  # 夸克单次转存上限 100，留余量
        for i in range(0, len(fids), batch):
            data, _ = self._request(
                "POST", "/share/sharepage/save",
                json_body={
                    "fid_list": fids[i:i + batch],
                    "fid_token_list": tokens[i:i + batch],
                    "to_pdir_fid": to_fid,
                    "pwd_id": ctx.pwd_id,
                    "stoken": ctx.stoken,
                    "pdir_fid": "0",
                    "scene": "link",
                },
            )
            task_id = (data or {}).get("task_id")
            if not task_id:
                raise DriveError(f"转存未返回 task_id: {str(data)[:200]}")
            saved.extend(self._wait_task(task_id))
        return saved

    def _wait_task(self, task_id: str, timeout_s: float = 120.0) -> List[str]:
        deadline = time.time() + timeout_s
        retry = 1
        while time.time() < deadline:
            data, _ = self._request(
                "GET", "/task", params={"task_id": task_id, "retry_index": retry}
            )
            status = (data or {}).get("status")
            if status == 2:  # 完成
                save_as = (data or {}).get("save_as") or {}
                return [str(x) for x in (save_as.get("save_as_top_fids") or [])]
            if status not in (0, 1):
                raise DriveError(
                    f"转存任务失败: {(data or {}).get('task_title') or ''} status={status}"
                )
            time.sleep(0.5)
            retry += 1
        raise DriveError(f"转存任务超时 task_id={task_id}")

    # ------------------------------------------------------------------ 直链
    def get_play_target(self, fid: str, name: str = "") -> PlayTarget:
        """原画直链 + 全部转码档。

        两个接口各管一段，缺一不可：
          /file/download  → 原画 download_url（REMUX 原盘只有这一档）
          /file/v2/play   → 转码档 video_list（4K/超清/高清/流畅，h264+aac，浏览器能直接解）
        两个接口都会下发一次性的 __puus cookie，直链缺 cookie 会被上游 412 拒绝。
        """
        data, resp = self._request("POST", "/file/download", json_body={"fids": [fid]})
        item = {}
        if isinstance(data, list) and data:
            item = data[0]
        elif isinstance(data, dict):
            item = data
        # 夸克直链需要带上响应 Set-Cookie 下发的临时 cookie
        cookies = join_set_cookies(resp.headers.get_list("set-cookie"))
        download_url = str(item.get("download_url") or "")
        file_name = name or str(item.get("file_name") or "")

        variants: List[StreamVariant] = []
        default_key = ""
        if self._is_video(item):
            play_data, play_cookie = self._fetch_transcodes(fid)
            if play_cookie:
                cookies = merge_cookies(cookies, play_cookie)
            variants = self._parse_transcodes(play_data)
            default_key = str((play_data or {}).get("default_resolution") or "")

        if download_url:
            variants.append(StreamVariant(
                key="origin", label="原画", url=download_url,
                height=int(item.get("video_height") or 0),
                width=int(item.get("video_width") or 0),
                size=int(item.get("size") or 0),
                fmt=str(item.get("format_type") or ""),
                origin=True,
            ))
        if not variants:
            raise DriveError(
                f"未拿到直链: {str(item)[:200]}（部分资源需要会员或受风控限制）"
            )
        if not any(v.key == default_key for v in variants):
            # 网盘没给建议档（或该档不可用）时退到最高可用转码档，再退到原画
            default_key = variants[0].key
        return PlayTarget(
            file_name=file_name,
            url=(self._pick(variants, default_key) or variants[0]).url,
            download_url=download_url,
            cookie=cookies,
            ua=self.ua,
            variants=variants,
            default_key=default_key,
        )

    @staticmethod
    def _is_video(item: dict) -> bool:
        return (item.get("obj_category") == "video"
                or bool(item.get("video_max_resolution"))
                or str(item.get("format_type") or "").startswith("video/"))

    def _fetch_transcodes(self, fid: str) -> tuple:
        """查转码档。非视频/未转码/接口变动都不该让播放本身失败，失败即当作没有转码档。"""
        try:
            data, resp = self._request(
                "POST", "/file/v2/play",
                json_body={
                    "fid": fid,
                    "resolutions": "normal,low,high,super,2k,4k",
                    "supports": "fmp4,m3u8",
                },
            )
        except DriveError:
            return {}, ""
        return (data or {}), join_set_cookies(resp.headers.get_list("set-cookie"))

    @staticmethod
    def _parse_transcodes(data: dict) -> List[StreamVariant]:
        """video_list -> StreamVariant 列表（按分辨率从高到低，跳过没权限/没转好的档）。"""
        out: List[StreamVariant] = []
        for entry in (data or {}).get("video_list") or []:
            info = entry.get("video_info") or {}
            url = str(info.get("url") or "")
            if not url:
                continue
            if entry.get("accessable") is False:            # 需要会员且当前账号没有
                continue
            if entry.get("trans_status") not in (None, "success"):  # 还在转码
                continue
            key = str(entry.get("resolution") or info.get("resolution") or "")
            out.append(StreamVariant(
                key=key,
                label=RESOLUTION_LABELS.get(key, key.upper() or "转码"),
                url=url,
                height=int(info.get("height") or 0),
                width=int(info.get("width") or 0),
                size=int(info.get("size") or 0),
                bitrate=float(info.get("bitrate") or 0),
                fmt=str(info.get("format") or ""),
            ))
        out.sort(key=lambda v: (-v.height, -v.width))
        return out

    @staticmethod
    def _pick(variants: List[StreamVariant], key: str) -> Optional[StreamVariant]:
        for v in variants:
            if v.key == key:
                return v
        return None
