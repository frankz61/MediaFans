"""账号、会话、每个用户自己的片库。

**为什么要分三层**（用户说的「网关」）：

- **资源层**：网盘。凭据是管理员的，所有人共用同一份存储——不可能给每个用户配一套
  夸克账号，也没必要。
- **展示层**：每个用户自己的片库和观看进度。它是资源之上的一个**视图**：同一个文件，
  甲加进片库、乙没有，甲看到第 5 集、乙看到第 2 集。
- **账号层**：用户名密码换令牌。

分开的好处是资源可以共享而互相看不见对方在看什么；代价是「谁能浏览整个网盘」要单独
授权（`can_browse`），否则片库就形同虚设——网盘目录里什么都有。

存的是 JSON：单机自用，一个文件比一张表好懂，也好手改。并发写加锁 + 原子替换，
跟 watch.py 同一套。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# scrypt 的参数。N 越大越慢越安全；16384 在服务器上约 50ms 一次，
# 对登录够用，对爆破足够贵。用 stdlib 的 hashlib 就有，不引额外依赖。
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1

SESSION_TTL = 30 * 24 * 3600.0     # 令牌有效期。手机和电视不该几天就要重登一次
# 登录失败退避：同一个 IP 连续失败到这个次数就开始拖时间。
# 这个服务是公网可达的，登录页早晚会被扫到。
_FAIL_THRESHOLD = 5
_FAIL_WINDOW = 600.0


def hash_password(password: str) -> str:
    """scrypt，带随机盐。格式 `scrypt$<salt hex>$<hash hex>`."""
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                       n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, want = (stored or "").split("$", 2)
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                           n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    except (ValueError, TypeError):
        return False
    # 定时比较：别让响应时间泄漏前缀匹配了多少
    return hmac.compare_digest(h.hex(), want)


@dataclass
class LibraryItem:
    """片库里的一条。**只记「是哪部作品」，不记文件在哪**——

    文件属于资源层，可能被转存、被替换、被删；片库记的是「我在追这个」。
    真要播的时候再按 tmdb_id 去问资源层当前有哪些文件。
    """

    tmdb_id: int
    media_type: str = "tv"          # tv | movie
    title: str = ""
    year: str = ""
    poster: str = ""
    season: Optional[int] = None    # 剧集：加进来时看的那一季；电影为 None
    added: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class User:
    name: str
    password: str = ""              # scrypt$...
    admin: bool = False
    # 能不能浏览整个网盘（「我的网盘」标签页）。默认不能：网盘目录里什么都有，
    # 给了就等于片库白分了。管理员恒为 True。
    can_browse: bool = False
    created: float = 0.0
    library: List[LibraryItem] = field(default_factory=list)

    def browsable(self) -> bool:
        return self.admin or self.can_browse

    def as_public(self) -> dict:
        """给前端看的。永远不带密码哈希。"""
        return {"name": self.name, "admin": self.admin,
                "can_browse": self.browsable(), "library": len(self.library)}


class Accounts:
    """账号 + 会话 + 片库，一个 JSON 文件。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._users: Optional[Dict[str, User]] = None
        self._sessions: Dict[str, tuple] = {}     # token -> (username, expires)
        self._fails: Dict[str, tuple] = {}        # ip -> (count, window_start)

    # ---------------------------------------------------------------- 落盘
    def _load(self) -> Dict[str, User]:
        if self._users is not None:
            return self._users
        users: Dict[str, User] = {}
        sessions: Dict[str, tuple] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("users", []):
                lib = [LibraryItem(**{k: v for k, v in x.items()
                                      if k in LibraryItem.__dataclass_fields__})
                       for x in item.pop("library", [])]
                fields = {k: v for k, v in item.items()
                          if k in User.__dataclass_fields__ and k != "library"}
                if fields.get("name"):
                    users[fields["name"]] = User(library=lib, **fields)
            now = time.time()
            for t, (name, exp) in (raw.get("sessions") or {}).items():
                # 过期的直接丢；会话要能扛住重启，否则每次部署所有人被踢
                if exp > now and name in users:
                    sessions[t] = (name, exp)
        except Exception:
            # 文件不在、坏了、手改乱了都不该让服务起不来
            users, sessions = {}, {}
        self._users = users
        self._sessions = sessions
        return users

    def _flush(self) -> None:
        users = self._users or {}
        payload = {
            "version": 1,
            "users": [dict(asdict(u), library=[i.as_dict() for i in u.library])
                      for u in users.values()],
            "sessions": {t: [n, e] for t, (n, e) in self._sessions.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)      # 里面有密码哈希和会话令牌
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- 用户
    def list_users(self) -> List[User]:
        with self._lock:
            return sorted(self._load().values(), key=lambda u: (not u.admin, u.name))

    def get(self, name: str) -> Optional[User]:
        with self._lock:
            return self._load().get(name)

    def any_admin(self) -> bool:
        with self._lock:
            return any(u.admin for u in self._load().values())

    def add(self, name: str, password: str, admin: bool = False,
            can_browse: bool = False) -> User:
        name = (name or "").strip()
        if not name:
            raise ValueError("用户名不能为空")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        with self._lock:
            users = self._load()
            if name in users:
                raise ValueError(f"用户已存在: {name}")
            u = User(name=name, password=hash_password(password), admin=admin,
                     can_browse=can_browse, created=time.time())
            users[name] = u
            self._flush()
            return u

    def set_password(self, name: str, password: str) -> None:
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        with self._lock:
            u = self._load().get(name)
            if not u:
                raise ValueError(f"没有这个用户: {name}")
            u.password = hash_password(password)
            # 改密码要把该用户的会话全部作废，否则改了等于没改
            self._sessions = {t: v for t, v in self._sessions.items() if v[0] != name}
            self._flush()

    def set_flags(self, name: str, admin: Optional[bool] = None,
                  can_browse: Optional[bool] = None) -> User:
        with self._lock:
            u = self._load().get(name)
            if not u:
                raise ValueError(f"没有这个用户: {name}")
            if admin is not None:
                u.admin = bool(admin)
            if can_browse is not None:
                u.can_browse = bool(can_browse)
            self._flush()
            return u

    def remove(self, name: str) -> bool:
        with self._lock:
            users = self._load()
            if name not in users:
                return False
            if users[name].admin and sum(1 for u in users.values() if u.admin) == 1:
                raise ValueError("这是最后一个管理员，删掉就没人能管了")
            users.pop(name)
            self._sessions = {t: v for t, v in self._sessions.items() if v[0] != name}
            self._flush()
            return True

    # ---------------------------------------------------------------- 会话
    def login(self, name: str, password: str, ip: str = "") -> Optional[str]:
        """验密码，成功返回令牌。失败会记在 IP 上，见 throttle_delay。"""
        with self._lock:
            u = self._load().get((name or "").strip())
            ok = bool(u) and verify_password(password or "", u.password)
            if not ok:
                self._note_failure(ip)
                return None
            self._fails.pop(ip, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = (u.name, time.time() + SESSION_TTL)
            self._prune_sessions()
            self._flush()
            return token

    def resolve(self, token: str) -> Optional[User]:
        """令牌 -> 用户。过期或不认识都返回 None。"""
        if not token:
            return None
        with self._lock:
            users = self._load()
            hit = self._sessions.get(token)
            if not hit:
                return None
            name, exp = hit
            if exp <= time.time():
                self._sessions.pop(token, None)
                return None
            return users.get(name)

    def logout(self, token: str) -> None:
        with self._lock:
            if self._sessions.pop(token, None):
                self._flush()

    def _prune_sessions(self) -> None:
        now = time.time()
        self._sessions = {t: v for t, v in self._sessions.items() if v[1] > now}

    # ---------------------------------------------------------------- 防爆破
    def _note_failure(self, ip: str) -> None:
        if not ip:
            return
        n, start = self._fails.get(ip, (0, time.time()))
        if time.time() - start > _FAIL_WINDOW:
            n, start = 0, time.time()
        self._fails[ip] = (n + 1, start)

    def throttle_delay(self, ip: str) -> float:
        """这个 IP 该被拖多久再回应。

        不封禁、只拖延：封禁会被 NAT 后面的整栋楼连坐，而拖延对爆破一样致命
        （每次多等几秒，字典跑完要几年），对打错密码的正常用户几乎无感。
        """
        n, start = self._fails.get(ip, (0, 0.0))
        if not n or time.time() - start > _FAIL_WINDOW:
            return 0.0
        if n < _FAIL_THRESHOLD:
            return 0.0
        return min(8.0, 0.5 * (2 ** min(n - _FAIL_THRESHOLD, 4)))

    # ---------------------------------------------------------------- 片库
    def library(self, name: str) -> List[LibraryItem]:
        u = self.get(name)
        return list(u.library) if u else []

    def add_to_library(self, name: str, item: LibraryItem) -> bool:
        """加进片库。同一部作品（同 tmdb_id + 类型）只留一条，重复加就更新。"""
        with self._lock:
            u = self._load().get(name)
            if not u:
                return False
            item.added = item.added or time.time()
            for i, old in enumerate(u.library):
                if old.tmdb_id == item.tmdb_id and old.media_type == item.media_type:
                    item.added = old.added          # 保留最初加入的时间
                    u.library[i] = item
                    self._flush()
                    return True
            u.library.insert(0, item)
            self._flush()
            return True

    def remove_from_library(self, name: str, tmdb_id: int, media_type: str) -> bool:
        with self._lock:
            u = self._load().get(name)
            if not u:
                return False
            n = len(u.library)
            u.library = [x for x in u.library
                         if not (x.tmdb_id == tmdb_id and x.media_type == media_type)]
            if len(u.library) != n:
                self._flush()
                return True
            return False

    def in_library(self, name: str, tmdb_id: int, media_type: str) -> bool:
        return any(x.tmdb_id == tmdb_id and x.media_type == media_type
                   for x in self.library(name))
