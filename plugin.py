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

from maibot_sdk import Command, LLMProvider, LLMProviderBase, MaiBotPlugin

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
            cleaned: list[dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "").strip()
                if not name:
                    # 上游偶尔回不完整的 tool_call（缺 function.name），
                    # 留着会让宿主解析失败并整条请求报错，所以直接丢弃。
                    log.warning(
                        "WorkBuddy: 上游返回的 tool_call 缺少 function.name，已跳过: %s",
                        str(tc)[:200],
                    )
                    continue
                cleaned.append(
                    {
                        # Host 用 deserialize_tool_calls_snapshot 解析：
                        # 需要 OpenAI 形态（id + function.name/arguments），
                        # 传 call_id/func_name/args 会让 function.name 为 None 而报错。
                        "id": tc.get("id") or f"call_{secrets.token_hex(8)}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": fn.get("arguments") or "{}",
                        },
                    }
                )
            if cleaned:
                result["tool_calls"] = cleaned
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

    @Command(
        "wb_login",
        description="获取 WorkBuddy 登录链接（仅管理员）",
        pattern=r"^/wb\s+(?:login|登录)",
    )
    async def handle_wb_login(self, **kwargs: Any) -> str:
        text = str(kwargs.get("text") or "")
        stream_id = str(kwargs.get("stream_id") or "")
        is_admin = bool(kwargs.get("is_local_operator"))
        log.info(
            "WorkBuddy /wb login 命中: text=%r admin=%s stream_id=%s user=%s",
            text, is_admin, stream_id, kwargs.get("user_id"),
        )
        if not is_admin:
            # 不硬拦：授权链接只会把「点击的人自己的 WorkBuddy 账号」绑上来，
            # 不涉及已保存凭据的泄露。这里只留痕，方便排查谁发起过登录。
            log.info(
                "WorkBuddy /wb login 来自非 local_operator（user_id=%s），继续放行",
                kwargs.get("user_id"),
            )
        try:
            async def respond(msg: str) -> str:
                """统一出口：能主动发就主动发，拿不到 stream_id 才退回返回值。"""
                if stream_id:
                    await self._reply(msg, stream_id)
                    return ""
                return msg

            session = self.provider.session()
            log.info("wb login: step=session ok data_dir=%s", DATA_DIR)

            creds: dict = {}
            try:
                creds = await session.credentials()
                log.info("wb login: step=credentials ok has_creds=%s keys=%s", bool(creds), sorted(creds)[:6])
            except Exception as exc:  # noqa: BLE001
                log.warning("wb login: step=credentials 异常 %s: %s", type(exc).__name__, exc)

            if creds:
                return await respond(
                    "WorkBuddy 已登录。要换账号请先删除 data 目录下的 credentials.enc，再执行 /wb login。"
                )

            log.info("wb login: step=login_begin start")
            begin = await session.login_begin()
            url = begin.get("authUrl") or ""
            log.info("wb login: step=login_begin ok url=%s state=%s", url[:70], str(begin.get("state"))[:12])
            if not url:
                return await respond("WorkBuddy 登录请求没有返回授权链接，请稍后重试。")

            if stream_id:
                task = asyncio.create_task(self._finish_login(begin["state"], stream_id))
                _LOGIN_TASKS.add(task)
                task.add_done_callback(_LOGIN_TASKS.discard)
                log.info("wb login: 已启动后台轮询任务")
            return await respond(
                "请在 10 分钟内在浏览器打开下面的链接完成 WorkBuddy 授权：\n" + url
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("wb login 失败")
            try:
                if stream_id:
                    await self._reply(f"WorkBuddy /wb login 出错：{type(exc).__name__}: {exc}", stream_id)
                    return ""
            except Exception:
                pass
            return f"WorkBuddy /wb login 出错：{type(exc).__name__}: {exc}"

    @LLMProvider(
        CLIENT_TYPE,
        name="WorkBuddy (CN)",
        description="通过 WorkBuddy 账号转发的 OpenAI 兼容模型通道",
    )
    async def handle_llm(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        return await self.provider.dispatch(operation, request)


def create_plugin() -> MaiBotPlugin:
    return WorkBuddyPlugin()
