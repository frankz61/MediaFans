from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..models import DriveFile, PlayTarget, SearchHit


@dataclass
class ShareContext:
    """一次分享会话的上下文（各网盘字段不同，夸克: pwd_id + stoken）."""

    url: str = ""
    pwd_id: str = ""
    passcode: str = ""
    stoken: str = ""
    extra: dict = field(default_factory=dict)


class BaseDrive:
    """网盘驱动抽象：每种网盘实现这一组原子能力，上层 CLI/Agent 只面对这组接口."""

    name: str = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    # ---- 账号 ----
    def account_name(self) -> str:
        """返回账号昵称，同时可用于校验 cookie 是否有效."""
        raise NotImplementedError

    # ---- 目录/文件 ----
    def list_files(self, dir_fid: str = "0") -> List[DriveFile]:
        raise NotImplementedError

    def resolve_path(self, path: str) -> Optional[str]:
        """路径 -> fid，找不到返回 None."""
        raise NotImplementedError

    def resolve_entry(self, path: str) -> Optional[str]:
        """路径 -> fid（目录或文件）。

        部分）网盘的路径解析接口只支持目录（如夸克 path_list），
        文件路径查不到时退回「解析父目录 + 列表精确匹配」。
        """
        fid = self.resolve_path(path)
        if fid:
            return fid
        from ..utils import norm_path

        p = norm_path(path)
        parent, _, name = p.rpartition("/")
        if not name:
            return None
        parent_fid = self.resolve_path(parent or "/")
        if not parent_fid:
            return None
        for f in self.list_files(parent_fid):
            if f.name == name:
                return f.fid
        return None

    def ensure_dir(self, path: str) -> str:
        """确保目录存在（不存在则创建），返回 fid."""
        raise NotImplementedError

    def find(self, query: str, root: str = "/", depth: int = 3,
             max_entries: int = 3000) -> List[SearchHit]:
        """在 root 下按关键词（空格分隔、全部命中才算匹配）找文件/目录，返回完整路径."""
        from ..utils import match_tokens, norm_path

        hits: List[SearchHit] = []
        visited = 0
        root_norm = norm_path(root)
        root_fid = "0" if root_norm == "/" else self.resolve_path(root_norm)
        if not root_fid:
            return hits
        queue = [(root_fid, root_norm, 0)]
        while queue and visited < max_entries:
            fid, cur_path, level = queue.pop(0)
            try:
                files = self.list_files(fid)
            except Exception:
                continue
            for f in files:
                visited += 1
                full = (cur_path.rstrip("/") + "/" + f.name) if cur_path != "/" else "/" + f.name
                if match_tokens(query, f.name):
                    hits.append(SearchHit(path=full, file=f))
                    if len(hits) >= 50:
                        return hits
                if f.is_dir and level < depth and visited < max_entries:
                    queue.append((f.fid, full, level + 1))
        return hits

    # ---- 分享 ----
    def open_share(self, share_url: str, passcode: str = "") -> ShareContext:
        """校验分享链接（含提取码），返回后续转存/列文件用的上下文."""
        raise NotImplementedError

    def list_share_files(self, ctx: ShareContext, dir_fid: str = "0") -> List[DriveFile]:
        raise NotImplementedError

    def save_share_files(self, ctx: ShareContext, files: Sequence[DriveFile],
                         to_dir: str) -> List[str]:
        """转存到自己的网盘目录，返回新文件 fid 列表."""
        raise NotImplementedError

    # ---- 播放 ----
    def get_play_target(self, fid: str, name: str = "") -> PlayTarget:
        raise NotImplementedError
