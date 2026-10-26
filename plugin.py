"""WorkBuddy 模型接入 —— MaiBot LLM Provider 插件。

把 WorkBuddy(CN) 账号接入 MaiBot：
- 插件内部完成 OAuth 登录与 token 刷新（凭据 AES-GCM 加密保存）
- 通过 @LLMProvider 注册一个 client_type，模型配置里指向它即可

数据目录：<插件目录>/data
主密钥：环境变量 WORKBUDDY_MASTER_KEY，缺省时自动生成并保存在 data/.workbuddy_master_key
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from maibot_sdk import EventHandler, LLMProvider, LLMProviderBase, MaiBotPlugin
from maibot_sdk.types import EventType

try:  # 插件被作为包加载时走相对导入
    from .workbuddy.session import WorkBuddySession, resolve_master_key
except ImportError:  # 兜底：绝对导入
    from workbuddy.session import WorkBuddySession, resolve_master_key  # type: ignore

PLUGIN_DIR = Path(__file__).resolve().parent
# 与登录助手共用同一份凭据目录：<instance>/data/plugins/maibot-team.workbuddy
INSTANCE_DIR = PLUGIN_DIR.parents[1]
DATA_DIR = INSTANCE_DIR / "data" / "plugins" / "maibot-team.workbuddy"
KEY_FILE = DATA_DIR / ".workbuddy_master_key"

CLIENT_TYPE = "com.beichen24a1.workbuddy"
DEFAULT_MODEL = "deepseek-v4.1-flash"

log = logging.getLogger("maibot.plugin.workbuddy")

# 进行中的登录轮询任务（卸载时取消）
_LOGIN_TASKS: set[asyncio.Task] = set()


# --------------------------------------------------------------------------
# 凭据与会话
# --------------------------------------------------------------------------
def load_master_key() -> str:
    key = resolve_master_key()
    if key:
        return key
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


# --------------------------------------------------------------------------
# Context Item -> OpenAI messages
# --------------------------------------------------------------------------
def _parts_to_openai(parts: list[dict]) -> Any:
    """把 ContextItem.parts 转成 OpenAI 的 content（纯文本时用字符串）。"""
    texts: list[str] = []
    rich: list[dict] = []
    has_image = False
    for part in parts or []:
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            texts.append(text)
            rich.append({"type": "text", "text": text})
        elif ptype == "image":
            has_image = True
            fmt = (part.get("image_format") or "png").lstrip(".")
            b64 = part.get("image_base64") or ""
            rich.append({"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64,{b64}"}})
        elif ptype == "refusal":
            texts.append(part.get("refusal") or "")
    if has_image:
        return rich
    return "".join(texts)


def _text_of(item: dict) -> str:
    out = []
    for part in item.get("parts") or []:
        if part.get("type") == "text":
            out.append(part.get("text") or "")
    return "".join(out)


def context_items_to_messages(items: list[dict]) -> list[dict]:
    """把序列化后的 Context Items 转成 OpenAI chat messages。"""
    messages: list[dict] = []

    def attach_tool_call(call: dict) -> None:
        payload = {
            "id": call.get("call_id") or f"call_{secrets.token_hex(8)}",
            "type": "function",
            "function": {
                "name": call.get("func_name") or "",
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            },
        }
        if messages and messages[-1].get("role") == "assistant":
            messages[-1].setdefault("tool_calls", []).append(payload)
        else:
            messages.append({"role": "assistant", "content": "", "tool_calls": [payload]})

    for item in items or []:
        itype = item.get("item_type")
        if itype == "SystemMessageItem":
            messages.append({"role": "system", "content": _parts_to_openai(item.get("parts"))})
        elif itype == "UserMessageItem":
            messages.append({"role": "user", "content": _parts_to_openai(item.get("parts"))})
        elif itype == "AssistantMessageItem":
            messages.append({"role": "assistant", "content": _text_of(item)})
        elif itype == "ReasoningItem":
            # 上游不需要把历史推理再喂回去
            continue
        elif itype == "FunctionCallItem":
            attach_tool_call(item.get("tool_call") or {})
        elif itype == "FunctionCallOutputItem":
            content = item.get("output")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                }
            )
        # 其余 item 类型（活动记录 / 不透明块）对上游无意义，跳过
    return messages


def tool_options_to_openai(options: list[dict] | None) -> list[dict] | None:
    """tool_options 已经是接近 OpenAI 的形态，尽量原样透传。"""
    if not options:
        return None
    tools: list[dict] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if opt.get("type") == "function" and isinstance(opt.get("function"), dict):
            tools.append(opt)
            continue
        name = opt.get("name") or opt.get("func_name") or opt.get("function_name")
        if not name:
            continue
        params = opt.get("parameters") or opt.get("schema") or {"type": "object", "properties": {}}
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": opt.get("description") or "",
                    "parameters": params,
                },
            }
        )
    return tools or None


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------
class WorkBuddyProvider(LLMProviderBase):
    """把 Host 的 response 请求转发给 WorkBuddy 上游。"""

    def __init__(self) -> None:
        self._session: WorkBuddySession | None = None

    def session(self) -> WorkBuddySession:
        if self._session is None:
            self._session = WorkBuddySession(DATA_DIR, load_master_key(), login_timeout=600.0)
        return self._session

    async def get_response(self, request: dict[str, Any]) -> dict[str, Any]:
        api_provider = request.get("api_provider") or {}
        model_info = request.get("model_info") or {}
        model = (
            model_info.get("model_identifier")
            or api_provider.get("model")
            or DEFAULT_MODEL
        )
        extra = request.get("extra_params") or {}
        messages = context_items_to_messages(request.get("context_items") or [])
        if not messages:
            raise ValueError("WorkBuddy: 请求里没有可发送的 context_items")
        log.info("WorkBuddy 调用: model=%s messages=%d tools=%d", model, len(messages), len(request.get("tool_options") or []))

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        max_tokens = request.get("max_tokens") or extra.get("max_tokens")
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        temperature = request.get("temperature")
        if temperature is not None:
            payload["temperature"] = float(temperature)
        tools = tool_options_to_openai(request.get("tool_options"))
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = request.get("tool_choice") or "auto"
        fmt = request.get("response_format")
        if isinstance(fmt, dict) and fmt:
            payload["response_format"] = fmt
        for key in ("top_p", "presence_penalty", "frequency_penalty", "stop"):
            if key in extra and extra[key] is not None:
                payload[key] = extra[key]

        data = await self.session().chat.complete(payload)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}

        result: dict[str, Any] = {
            "content": message.get("content") or "",
            "usage": {
                "model_name": model,
                "provider_name": "WorkBuddy",
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            "raw_data": data,
        }
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            result["reasoning_content"] = reasoning
        tool_calls = message.get("tool_calls")
        if tool_calls:
            result["tool_calls"] = [
                {
                    "call_id": tc.get("id") or f"call_{secrets.token_hex(8)}",
                    "func_name": (tc.get("function") or {}).get("name") or "",
                    "args": (tc.get("function") or {}).get("arguments") or "{}",
                }
                for tc in tool_calls
                if isinstance(tc, dict)
            ]
        return result


class WorkBuddyPlugin(MaiBotPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.provider = WorkBuddyProvider()

    async def on_load(self) -> None:
        log.info("WorkBuddy Provider 已加载，client_type=%s", CLIENT_TYPE)

    async def on_unload(self) -> None:
        for task in list(_LOGIN_TASKS):
            task.cancel()
        _LOGIN_TASKS.clear()
        if self.provider._session is not None:
            await self.provider._session.close()
            self.provider._session = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        return None

    # ------------------------- /wb login -------------------------
    async def _reply(self, text: str, stream_id: str) -> None:
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("WorkBuddy 回复失败: %s", exc)

    async def _finish_login(self, state: str, stream_id: str) -> None:
        session = self.provider.session()
        try:
            await session.auth.complete_login(state)
        except Exception as exc:  # noqa: BLE001
            log.warning("WorkBuddy 登录失败: %s", exc)
            await self._reply(f"WorkBuddy 登录失败：{exc}", stream_id)
            return
        log.info("WorkBuddy 登录成功，凭据已加密保存")
        await self._reply("WorkBuddy 登录成功，凭据已加密保存，现在可以让模型用 workbuddy 了。", stream_id)

    @EventHandler("wb_login", event_type=EventType.ON_MESSAGE, intercept_message=False, weight=90)
    async def on_message(self, message: Any = None, **kwargs: Any) -> None:
        log.info(
            "WorkBuddy on_message 触发: type=%s kwargs=%s text=%r",
            type(message).__name__,
            sorted(kwargs.keys())[:8],
            getattr(message, "plain_text", None),
        )
        try:
            text = (getattr(message, "plain_text", "") or "").strip()
            stream_id = getattr(message, "stream_id", "") or ""
        except Exception:  # noqa: BLE001
            return
        low = text.lower()
        if not (
            low.startswith("/wb login")
            or text.startswith("/wb 登录")
            or low.startswith("/workbuddy login")
        ):
            return
        if not stream_id:
            log.warning("WorkBuddy /wb login 缺少 stream_id，无法回复")
            return
        session = self.provider.session()
        try:
            creds = await session.credentials()
        except Exception:  # noqa: BLE001
            creds = {}
        if creds:
            await self._reply(
                "WorkBuddy 已登录。要换账号请先删除 data 目录下的 credentials.enc，再执行 /wb login。",
                stream_id,
            )
            return
        try:
            begin = await session.login_begin()
        except Exception as exc:  # noqa: BLE001
            await self._reply(f"WorkBuddy 获取登录链接失败：{exc}", stream_id)
            return
        await self._reply(
            "WorkBuddy 登录：请在 10 分钟内在浏览器打开下面的链接完成授权\n" + begin["authUrl"],
            stream_id,
        )
        task = asyncio.create_task(self._finish_login(begin["state"], stream_id))
        _LOGIN_TASKS.add(task)
        task.add_done_callback(_LOGIN_TASKS.discard)

    @LLMProvider(
        CLIENT_TYPE,
        name="WorkBuddy (CN)",
        description="通过 WorkBuddy 账号转发的 OpenAI 兼容模型通道",
    )
    async def handle_llm(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        return await self.provider.dispatch(operation, request)


def create_plugin() -> MaiBotPlugin:
    return WorkBuddyPlugin()
