"""WorkBuddy 接入（MaiBot 插件内部包）。"""

from .session import (
    DEFAULT_MASTER_KEY_ENV,
    ORIGIN_CN,
    ORIGIN_GLOBAL,
    WorkBuddySession,
    resolve_master_key,
)

__all__ = [
    "DEFAULT_MASTER_KEY_ENV",
    "ORIGIN_CN",
    "ORIGIN_GLOBAL",
    "WorkBuddySession",
    "resolve_master_key",
]
