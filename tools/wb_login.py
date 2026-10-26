"""WorkBuddy 一次性登录助手。

用法：
    python tools/wb_login.py

流程：调用 WorkBuddy 的 begin_login 拿到授权链接 -> 在浏览器打开并同意 ->
本脚本轮询直到授权完成 -> 凭据用 AES-256-GCM 加密保存到插件数据目录。

主密钥优先取环境变量 WORKBUDDY_MASTER_KEY；没设时使用（或自动生成）
数据目录下的 .workbuddy_master_key，与插件运行时保持一致。
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
# <instance>/plugins/<plugin>/tools/wb_login.py -> instance = parents[3]
INSTANCE_DIR = PLUGIN_DIR.parents[2]
DATA_DIR = INSTANCE_DIR / "data" / "plugins" / "maibot-team.workbuddy"
KEY_FILE = DATA_DIR / ".workbuddy_master_key"

sys.path.insert(0, str(PLUGIN_DIR))

from workbuddy.session import WorkBuddySession  # noqa: E402


def load_or_create_key() -> str:
    env = (os.environ.get("WORKBUDDY_MASTER_KEY") or "").strip()
    if env:
        return env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    KEY_FILE.write_text(key, encoding="utf-8")
    try:
        KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


async def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = load_or_create_key()
    print(f"数据目录 : {DATA_DIR}")
    print(f"主密钥   : {KEY_FILE if not os.environ.get('WORKBUDDY_MASTER_KEY') else '来自环境变量 WORKBUDDY_MASTER_KEY'}")

    session = WorkBuddySession(DATA_DIR, key, login_timeout=600.0)
    try:
        creds = await session.credentials()
        if creds:
            print(f"检测到已有凭据（uid={creds.get('uid')}），如需换账号请先删除 credentials.enc")
            return 0

        begin = await session.login_begin()
        print("\n请在 10 分钟内在浏览器打开下面的链接并完成授权：\n")
        print("  " + begin["authUrl"])
        print("\n等待授权中…（Ctrl+C 可取消）", flush=True)

        try:
            await session.login_complete(begin["state"])
        except KeyboardInterrupt:
            print("\n已取消")
            return 130
        except Exception as exc:  # noqa: BLE001
            print(f"\n登录失败：{type(exc).__name__}: {exc}")
            return 1

        creds = await session.credentials()
        print(f"\n登录成功！uid={creds.get('uid')} realm={creds.get('realm')}")
        print(f"凭据已保存到：{DATA_DIR / 'credentials.enc'}")
        print("\n下一步：在 model_config.toml 里把 api_providers[].client_type 设为")
        print("  com.beichen24a1.workbuddy")
        return 0
    finally:
        await session.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
