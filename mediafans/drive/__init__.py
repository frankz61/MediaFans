from __future__ import annotations

from typing import Dict, List

from ..errors import ConfigError
from ..models import DriveFile, PlayTarget
from .base import BaseDrive
from .quark import QuarkDrive

_REGISTRY = {"quark": QuarkDrive}


def supported_netdisks() -> List[str]:
    return list(_REGISTRY.keys())


def create_drive(netdisk: str, cfg_section: dict, transport=None) -> BaseDrive:
    """按网盘类型实例化驱动。cfg_section 是 config 里 drive.<netdisk> 的那一段."""
    netdisk = (netdisk or "").strip().lower()
    cls = _REGISTRY.get(netdisk)
    if cls is None:
        raise ConfigError(
            f"暂不支持网盘 '{netdisk}'（当前支持: {', '.join(supported_netdisks())}）"
        )
    return cls(cfg_section or {}, transport=transport)


__all__ = ["BaseDrive", "QuarkDrive", "create_drive", "supported_netdisks", "DriveFile", "PlayTarget"]
