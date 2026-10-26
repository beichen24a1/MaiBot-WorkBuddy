"""WorkBuddy 上游会话组装。

把从 WorkBuddy-Access-Platform 移植过来的 core/* 组件拼起来：
    CredentialStore -> AuthManager -> UpstreamClient -> ChatService

与 AstrBot 版的区别：这里不启动任何 HTTP 网关（MaiBot 不需要），
直接由 WorkBuddyClient 调用 ChatService 发请求。
"""
from __future__ import annotations

import os
from pathlib import Path

import aiohttp

from .core.auth import AuthManager
from .core.chat import ChatService
from .core.client import UpstreamClient
from .core.storage import CredentialStore

# WorkBuddy 的两个站点（与上游插件保持一致）
ORIGIN_CN = "https://www.codebuddy.cn"
ORIGIN_GLOBAL = "https://www.workbuddy.ai"

DEFAULT_MASTER_KEY_ENV = "WORKBUDDY_MASTER_KEY"


class WorkBuddySession:
    """持有 aiohttp 会话与 WorkBuddy 各组件。"""

    def __init__(
        self,
        data_dir: str | Path,
        master_key: str,
        *,
        chat_base: str = ORIGIN_CN,
        login_timeout: float = 120.0,
    ) -> None:
        if not master_key:
            raise ValueError(
                f"缺少 WorkBuddy 主密钥：请设置环境变量 {DEFAULT_MASTER_KEY_ENV}"
                "（标准 base64，解码后恰好 32 字节）"
            )
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._chat_base = chat_base.rstrip("/")
        self._master_key = master_key
        self._login_timeout = float(login_timeout)

        self._http: aiohttp.ClientSession | None = None
        self._store: CredentialStore | None = None
        self._auth: AuthManager | None = None
        self._upstream: UpstreamClient | None = None
        self._chat: ChatService | None = None

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def chat_base(self) -> str:
        return self._chat_base

    @property
    def auth(self) -> AuthManager:
        self._ensure_built()
        assert self._auth is not None
        return self._auth

    @property
    def chat(self) -> ChatService:
        self._ensure_built()
        assert self._chat is not None
        return self._chat

    def _ensure_built(self) -> None:
        if self._chat is not None:
            return
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
        )
        self._store = CredentialStore(self._data_dir, self._master_key)
        self._auth = AuthManager(
            self._http,
            self._store,
            chat_base=self._chat_base,
            poll_interval=1.0,
            login_timeout=self._login_timeout,
        )
        self._upstream = UpstreamClient(
            self._http,
            self._auth,
            chat_base=self._chat_base,
        )
        self._chat = ChatService(self._upstream)

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None
        self._store = None
        self._auth = None
        self._upstream = None
        self._chat = None

    async def credentials(self) -> dict:
        """已保存的凭据；空 dict 表示尚未登录。"""
        return dict(self.auth.credentials or {})

    async def login_begin(self) -> dict:
        """开始登录，返回 {'state':..., 'authUrl':...}，让用户打开 authUrl。"""
        return await self.auth.begin_login()

    async def login_complete(self, state: str) -> dict:
        """轮询直到授权完成并落盘凭据。"""
        return await self.auth.complete_login(state)


def resolve_master_key(config_value: str | None = None) -> str:
    """优先用插件配置，其次环境变量。"""
    return (config_value or os.environ.get(DEFAULT_MASTER_KEY_ENV) or "").strip()
