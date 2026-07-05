from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, unquote
from urllib.request import Request, urlopen

APPLICATION_SYSTEM_IMPORT_ERROR = ""
try:
    import application_system
except ModuleNotFoundError as exc:
    APPLICATION_SYSTEM_IMPORT_ERROR = str(exc)

    class _ApplicationSystemUnavailable:
        DEFAULT_PANEL_TEXT = "Select an option to begin!"

        def __getattr__(self, name: str):
            raise RuntimeError(f"Application system is unavailable: {APPLICATION_SYSTEM_IMPORT_ERROR}")

    application_system = _ApplicationSystemUnavailable()


ROOT = Path(__file__).resolve().parent
LAST_UPDATE = os.getenv("LAST_UPDATE", "").strip()
APP_NAME = os.getenv("APP_NAME", "Gem Tool").strip() or "Gem Tool"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://gem-tool.onrender.com").strip().rstrip("/")
SUPPORT_SERVER_URL = (os.getenv("SUPPORT_SERVER_URL") or "https://discord.gg/sUxqbyV87F").strip()
DISCORD_CLIENT_ID = (
    os.getenv("DISCORD_CLIENT_ID")
    or os.getenv("CLIENT_ID")
    or os.getenv("APPLICATION_ID")
    or ""
).strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
DASHBOARD_OWNER_TOKEN = (
    os.getenv("DASHBOARD_OWNER_TOKEN")
    or os.getenv("DASHBOARD_TOKEN")
    or ""
).strip()
SESSION_SECRET = (
    os.getenv("DASHBOARD_SESSION_SECRET")
    or DISCORD_CLIENT_SECRET
    or os.getenv("DISCORD_TOKEN")
    or secrets.token_urlsafe(32)
).encode("utf-8")
SESSION_COOKIE = "gem_tool_session"
STATE_COOKIE = "gem_tool_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 7
DISCORD_API = "https://discord.com/api"
MANAGE_GUILD_PERMISSION = 0x20
ADMINISTRATOR_PERMISSION = 0x8

SUPPORT_BOT = None
SUPPORT_BOT_IMPORT_ERROR = ""
SESSIONS: dict[str, dict[str, Any]] = {}


def load_support_bot():
    global SUPPORT_BOT, SUPPORT_BOT_IMPORT_ERROR
    if SUPPORT_BOT is not None:
        return SUPPORT_BOT
    try:
        import support_bot
    except ModuleNotFoundError as exc:
        SUPPORT_BOT_IMPORT_ERROR = str(exc)
        print(f"Gem Tool bot dependency missing; website will stay online: {exc}", flush=True)
        return None
    except Exception as exc:
        SUPPORT_BOT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"Gem Tool bot failed to import; website will stay online: {SUPPORT_BOT_IMPORT_ERROR}", flush=True)
        return None

    try:
        support_bot.application_system.setup_application_system(support_bot.bot, str(support_bot.DATA_DIR))
    except Exception as exc:
        SUPPORT_BOT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"Application system failed to initialize for dashboard: {SUPPORT_BOT_IMPORT_ERROR}", flush=True)

    SUPPORT_BOT = support_bot
    return SUPPORT_BOT


def effective_client_id(support_health: Optional[dict[str, Any]] = None) -> str:
    if DISCORD_CLIENT_ID:
        return DISCORD_CLIENT_ID
    if support_health is None:
        live_bot = load_support_bot()
        support_health = live_bot.status_payload() if live_bot else {}
    return str(support_health.get("bot_user_id") or "").strip()


def current_last_update() -> str:
    if LAST_UPDATE:
        return LAST_UPDATE
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def status_payload() -> dict[str, object]:
    support_bot = load_support_bot()
    support_health = support_bot.status_payload() if support_bot else {}
    online = bool(support_health.get("online"))
    client_id = effective_client_id(support_health)
    status_text = str(
        support_health.get("status")
        or (f"{APP_NAME} online" if online else f"{APP_NAME} offline")
    )
    invite_url = ""
    if client_id:
        invite_url = (
            "https://discord.com/oauth2/authorize"
            f"?client_id={client_id}&permissions=2147561408&scope=bot%20applications.commands"
        )
    payload = {
        "app_name": APP_NAME,
        "status": status_text,
        "online": online,
        "guild_count": str(support_health.get("guild_count") or 0),
        "last_update": current_last_update(),
        "invite_url": invite_url,
        "support_server_url": SUPPORT_SERVER_URL,
        "public_base_url": PUBLIC_BASE_URL,
        "support_bot_online": online,
        "support_bot_user": str(support_health.get("bot_user") or ""),
        "support_bot_guild_count": str(support_health.get("guild_count") or 0),
        "support_bot_state": str(support_health.get("bot_state") or ("online" if online else "offline")),
        "support_bot_message": status_text,
        "support_bot_retry_at": support_health.get("retry_at") or 0,
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if SUPPORT_BOT_IMPORT_ERROR:
        payload["support_bot_error"] = SUPPORT_BOT_IMPORT_ERROR
    if APPLICATION_SYSTEM_IMPORT_ERROR:
        payload["application_system_error"] = APPLICATION_SYSTEM_IMPORT_ERROR
    return payload


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def fmt_count(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return str(value or 0)


def format_timestamp(value: Any) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return "Unknown"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_value(value: str) -> str:
    signature = hmac.new(SESSION_SECRET, value.encode("utf-8"), hashlib.sha256).digest()
    return f"{value}.{b64url(signature)}"


def verify_signed_value(signed_value: str) -> Optional[str]:
    if "." not in signed_value:
        return None
    value, signature = signed_value.rsplit(".", 1)
    expected = sign_value(value).rsplit(".", 1)[1]
    if hmac.compare_digest(signature, expected):
        return value
    return None


def make_cookie(name: str, value: str, *, max_age: int = SESSION_MAX_AGE) -> str:
    cookie = SimpleCookie()
    cookie[name] = value
    morsel = cookie[name]
    morsel["Path"] = "/"
    morsel["HttpOnly"] = True
    morsel["SameSite"] = "Lax"
    morsel["Max-Age"] = str(max_age)
    if PUBLIC_BASE_URL.startswith("https://"):
        morsel["Secure"] = True
    return morsel.OutputString()


def expire_cookie(name: str) -> str:
    return make_cookie(name, "", max_age=0)


def compact_avatar(user: dict[str, Any]) -> str:
    user_id = str(user.get("id") or "")
    avatar = str(user.get("avatar") or "")
    if user_id and avatar:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=96"
    return ""


def oauth_redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/applications/callback"


def dashboard_ready() -> bool:
    return bool(effective_client_id() and DISCORD_CLIENT_SECRET and PUBLIC_BASE_URL)


def bot_is_ready() -> bool:
    support_bot = load_support_bot()
    return bool(support_bot and support_bot.bot and support_bot.bot.is_ready())


def bot_guilds() -> dict[str, Any]:
    support_bot = load_support_bot()
    if not support_bot or not support_bot.bot:
        return {}
    return {str(guild.id): guild for guild in support_bot.bot.guilds}


def owner_token_matches(token: str) -> bool:
    return bool(
        DASHBOARD_OWNER_TOKEN
        and token
        and hmac.compare_digest(token, DASHBOARD_OWNER_TOKEN)
    )


def create_owner_session_from_bot() -> tuple[bool, str, str]:
    support_bot = load_support_bot()
    health = support_bot.status_payload() if support_bot else {}
    guild_lookup = bot_guilds()
    if not support_bot or not support_bot.bot or not support_bot.bot.is_ready() or not guild_lookup:
        detail = str(health.get("status") or "The bot is not connected to Discord yet.")
        return False, "", f"Owner login is ready, but Gem Tool is not connected to Discord yet. {detail}"

    guilds = []
    for guild in sorted(guild_lookup.values(), key=lambda item: item.name.lower()):
        icon_key = getattr(getattr(guild, "icon", None), "key", "") or ""
        guilds.append(
            {
                "id": str(guild.id),
                "name": guild.name,
                "icon": str(icon_key),
                "owner": True,
                "permissions": ADMINISTRATOR_PERMISSION,
            }
        )

    user = {"id": "owner", "username": "Owner access", "global_name": "Owner access", "avatar": ""}
    return True, create_session(user, guilds), ""


def run_bot_coro(coro, timeout: float = 12.0):
    support_bot = load_support_bot()
    if not support_bot or not support_bot.bot or not support_bot.bot.is_ready():
        return False, "Gem Tool is not connected to Discord yet."
    future = asyncio.run_coroutine_threadsafe(coro, support_bot.bot.loop)
    try:
        return True, future.result(timeout=timeout)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def create_session(user: dict[str, Any], guilds: list[dict[str, Any]]) -> str:
    manageable = {}
    for guild in guilds:
        guild_id = str(guild.get("id") or "")
        try:
            permissions = int(guild.get("permissions") or 0)
        except (TypeError, ValueError):
            permissions = 0
        if permissions & (MANAGE_GUILD_PERMISSION | ADMINISTRATOR_PERMISSION):
            manageable[guild_id] = {
                "id": guild_id,
                "name": str(guild.get("name") or "Unknown server"),
                "icon": str(guild.get("icon") or ""),
                "owner": bool(guild.get("owner")),
                "permissions": permissions,
            }

    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = {
        "created_at": int(time.time()),
        "user": {
            "id": str(user.get("id") or ""),
            "username": str(user.get("global_name") or user.get("username") or "Discord user"),
            "avatar_url": compact_avatar(user),
        },
        "manageable_guilds": manageable,
    }
    return session_id


def prune_sessions() -> None:
    now = int(time.time())
    for session_id, session in list(SESSIONS.items()):
        if now - int(session.get("created_at") or 0) > SESSION_MAX_AGE:
            SESSIONS.pop(session_id, None)


def discord_rate_limit_message() -> str:
    return (
        "Discord is temporarily rate limiting this Render server (Cloudflare 1015). "
        "Wait 15-60 minutes, avoid repeated login attempts or redeploys, then try again."
    )


def discord_request_json(
    url: str,
    *,
    method: str = "GET",
    token: str = "",
    form: Optional[dict[str, str]] = None,
) -> Any:
    data = urlencode(form).encode("utf-8") if form is not None else None
    headers = {"User-Agent": f"{APP_NAME} Dashboard"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as error:
        details = error.read().decode("utf-8", "replace")
        details_lower = details.lower()
        if error.code == 429 or "error 1015" in details_lower or "rate limited" in details_lower:
            raise RuntimeError(discord_rate_limit_message()) from error
        raise RuntimeError(f"Discord HTTP {error.code}: {details[:300]}") from error
    except URLError as error:
        raise RuntimeError(f"Discord request failed: {error.reason}") from error
    return json.loads(raw) if raw else {}


def form_one(form: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form.get(key)
    if not values:
        return default
    return str(values[-1]).strip()


def form_int(form: dict[str, list[str]], key: str) -> Optional[int]:
    value = form_one(form, key)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def form_count(form: dict[str, list[str]], key: str, *, maximum: int = 1_000_000) -> int:
    value = form_one(form, key)
    if not value:
        return 0
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return max(0, min(maximum, parsed))


def selected(value: Any, expected: Any) -> str:
    return " selected" if str(value or "") == str(expected or "") else ""


def checked(condition: bool) -> str:
    return " checked" if condition else ""


def role_name(guild: Any, role_id: Any) -> str:
    try:
        role = guild.get_role(int(role_id))
    except Exception:
        role = None
    return role.name if role else str(role_id or "None")


def guild_icon(guild: Any, fallback: str = "G") -> str:
    if getattr(guild, "icon", None):
        return f'<img src="{esc(guild.icon.url)}" alt="">'
    return f"<span>{esc(fallback[:1].upper())}</span>"


def channel_options(guild: Any, selected_id: Any = "") -> str:
    options = ['<option value="">Not set</option>']
    channels = sorted(getattr(guild, "text_channels", []), key=lambda channel: (channel.position, channel.name.lower()))
    for channel in channels:
        options.append(f'<option value="{channel.id}"{selected(channel.id, selected_id)}>#{esc(channel.name)}</option>')
    return "\n".join(options)


def role_options(guild: Any, selected_id: Any = "", *, include_blank: bool = True) -> str:
    options = ['<option value="" data-search="none">None</option>'] if include_blank else []
    roles = [
        role
        for role in sorted(getattr(guild, "roles", []), key=lambda item: item.position, reverse=True)
        if not role.is_default() and not role.managed
    ]
    for role in roles:
        search_text = f"{role.name} {role.id}".lower()
        options.append(
            f'<option value="{role.id}" data-search="{esc(search_text)}"{selected(role.id, selected_id)}>'
            f'{esc(role.name)}</option>'
        )
    return "\n".join(options)


EMOJI_PICKER_GROUPS = [
    (
        "Frequently Used",
        [
            "\U0001f381", "\U0001f389", "\u2753", "\U0001f4e3", "\U0001f4b8", "\U0001f4c8",
            "\U0001f48e", "\U0001f525", "\U0001f44b", "\U0001f4d6", "\U0001f52d", "\u2705",
            "\u274c", "\u2764\ufe0f", "\u2b50", "\U0001f6e1\ufe0f", "\U0001f6e0\ufe0f", "\u26a1",
            "\u2620\ufe0f", "\U0001f440",
        ],
    ),
    (
        "Smileys & Emotion",
        [
            "\U0001f600", "\U0001f603", "\U0001f604", "\U0001f601", "\U0001f606", "\U0001f605",
            "\U0001f923", "\U0001f602", "\U0001f642", "\U0001f643", "\U0001f609", "\U0001f60a",
            "\U0001f607", "\U0001f970", "\U0001f60d", "\U0001f929", "\U0001f618", "\U0001f617",
            "\U0001f61c", "\U0001f92a", "\U0001f61d", "\U0001f911", "\U0001f917", "\U0001f92d",
            "\U0001f914", "\U0001f910", "\U0001f928", "\U0001f610", "\U0001f611", "\U0001f636",
            "\U0001f60f", "\U0001f612", "\U0001f644", "\U0001f62c", "\U0001f62e\U0000200d\U0001f4a8",
            "\U0001f925", "\U0001f60c", "\U0001f614", "\U0001f62a", "\U0001f924", "\U0001f634",
            "\U0001f637", "\U0001f912", "\U0001f915", "\U0001f922", "\U0001f92e", "\U0001f927",
            "\U0001f975", "\U0001f976", "\U0001f974", "\U0001f635", "\U0001f92f", "\U0001f920",
            "\U0001f973", "\U0001f978", "\U0001f60e", "\U0001f9d0", "\U0001f615", "\U0001f61f",
            "\U0001f641", "\U00002639\ufe0f", "\U0001f62e", "\U0001f62f", "\U0001f632", "\U0001f633",
            "\U0001f97a", "\U0001f626", "\U0001f627", "\U0001f628", "\U0001f630", "\U0001f625",
            "\U0001f622", "\U0001f62d", "\U0001f631", "\U0001f616", "\U0001f623", "\U0001f61e",
            "\U0001f613", "\U0001f629", "\U0001f62b", "\U0001f971", "\U0001f624", "\U0001f621",
            "\U0001f620", "\U0001f92c", "\U0001f608", "\U0001f47f", "\U0001f480", "\U0001f4a9",
        ],
    ),
    (
        "People & Hands",
        [
            "\U0001f44d", "\U0001f44e", "\U0001f44a", "\u270a", "\U0001f44b", "\U0001f91a",
            "\U0001f590\ufe0f", "\u270b", "\U0001f596", "\U0001f44c", "\U0001f90c", "\U0001f90f",
            "\u270c\ufe0f", "\U0001f91e", "\U0001f91f", "\U0001f918", "\U0001f919", "\U0001f448",
            "\U0001f449", "\U0001f446", "\U0001f595", "\U0001f447", "\u261d\ufe0f", "\U0001faf5",
            "\U0001faf6", "\U0001f44f", "\U0001f64c", "\U0001faf6", "\U0001f932", "\U0001f64f",
            "\U0001f4aa", "\U0001f9e0", "\U0001f9d1", "\U0001f468", "\U0001f469", "\U0001f9d1\U0000200d\U0001f4bb",
            "\U0001f9d1\U0000200d\U0001f527", "\U0001f9d1\U0000200d\U0001f680", "\U0001f575\ufe0f",
            "\U0001f482", "\U0001f477", "\U0001f9d9", "\U0001f9db", "\U0001f9df", "\U0001f3c3",
            "\U0001f483", "\U0001f57a", "\U0001f46f", "\U0001f9d8", "\U0001f6cc",
        ],
    ),
    (
        "Objects & Symbols",
        [
            "\U0001f4a1", "\U0001f4a3", "\U0001f4af", "\U0001f4ac", "\U0001f4ad", "\U0001f4a4",
            "\U0001f4e2", "\U0001f514", "\U0001f515", "\U0001f3b5", "\U0001f3b6", "\U0001f3a4",
            "\U0001f3a7", "\U0001f3ae", "\U0001f3af", "\U0001f3b2", "\U0001f9e9", "\U0001f3c6",
            "\U0001f947", "\U0001f948", "\U0001f949", "\U0001f3c5", "\U0001f396\ufe0f", "\U0001f3f5\ufe0f",
            "\U0001f39f\ufe0f", "\U0001f4f1", "\U0001f4bb", "\U00002328\ufe0f", "\U0001f5a5\ufe0f",
            "\U0001f4be", "\U0001f4bf", "\U0001f4c0", "\U0001f4f7", "\U0001f4f8", "\U0001f4f9",
            "\U0001f50d", "\U0001f50e", "\U0001f56f\ufe0f", "\U0001f4d5", "\U0001f4d7", "\U0001f4d8",
            "\U0001f4d9", "\U0001f4da", "\U0001f4dd", "\U0001f4cc", "\U0001f4cd", "\U00002702\ufe0f",
            "\U0001f512", "\U0001f513", "\U0001f511", "\U0001f528", "\u2692\ufe0f", "\u2699\ufe0f",
            "\U0001f9f0", "\U0001f9f2", "\U0001f52b", "\U0001f52a", "\U0001f6e1\ufe0f", "\U0001fa96",
        ],
    ),
    (
        "Nature & Weather",
        [
            "\U0001f31f", "\u2728", "\u26a1", "\U0001f525", "\U0001f4a7", "\U0001f30a",
            "\U0001f32a\ufe0f", "\U0001f308", "\u2600\ufe0f", "\U0001f324\ufe0f", "\u2601\ufe0f",
            "\U0001f327\ufe0f", "\u2744\ufe0f", "\u2603\ufe0f", "\U0001f4a8", "\U0001f331",
            "\U0001f332", "\U0001f333", "\U0001f334", "\U0001f335", "\U0001f337", "\U0001f339",
            "\U0001f33a", "\U0001f33b", "\U0001f33c", "\U0001f341", "\U0001f342", "\U0001f343",
            "\U0001faa8", "\U0001f30d", "\U0001f30e", "\U0001f30f", "\U0001f315", "\U0001f319",
        ],
    ),
    (
        "Vehicles & Places",
        [
            "\U0001f697", "\U0001f699", "\U0001f695", "\U0001f68c", "\U0001f692", "\U0001f691",
            "\U0001f693", "\U0001f69a", "\U0001f69b", "\U0001f69c", "\U0001f3ce\ufe0f", "\U0001f3cd\ufe0f",
            "\U0001f6f5", "\U0001f6fa", "\U0001f682", "\U0001f686", "\U0001f687", "\U0001f681",
            "\u2708\ufe0f", "\U0001f6e9\ufe0f", "\U0001f6eb", "\U0001f6ec", "\U0001f680",
            "\U0001f6f8", "\U0001f6a2", "\u26f4\ufe0f", "\U0001f6a4", "\u2693", "\U0001f5fa\ufe0f",
            "\U0001f3d4\ufe0f", "\U0001f3d5\ufe0f", "\U0001f3d6\ufe0f", "\U0001f3dc\ufe0f",
            "\U0001f3dd\ufe0f", "\U0001f3df\ufe0f", "\U0001f3f0", "\U0001f3ef", "\U0001f3ed",
        ],
    ),
    (
        "Food & Fun",
        [
            "\U0001f354", "\U0001f355", "\U0001f32d", "\U0001f35f", "\U0001f37f", "\U0001f36a",
            "\U0001f370", "\U0001f382", "\U0001f36d", "\U0001f36c", "\U0001f36b", "\u2615",
            "\U0001f379", "\U0001f37a", "\U0001f37b", "\U0001f942", "\U0001f3c0", "\u26bd",
            "\U0001f3c8", "\u26be", "\U0001f3be", "\U0001f3d0", "\U0001f3d3", "\U0001f3f8",
        ],
    ),
]


def emoji_choice_html(
    target_id: str,
    mode: str,
    value: str,
    *,
    label: Optional[str] = None,
    image_url: str = "",
) -> str:
    label_text = label or value
    content = (
        f'<img src="{esc(image_url)}" alt="{esc(label_text)}"><span>{esc(label_text)}</span>'
        if image_url
        else esc(value)
    )
    classes = "emoji-choice custom-emoji" if image_url else "emoji-choice"
    return (
        f'<button class="{classes}" type="button" data-emoji-target="{esc(target_id)}" '
        f'data-emoji-mode="{mode}" data-emoji-value="{esc(value)}" '
        f'data-emoji-label="{esc(label_text)}" title="{esc(label_text)}">{content}</button>'
    )


def emoji_picker(target_id: str, *, replace: bool = False, guild: Any = None) -> str:
    mode = "replace" if replace else "insert"
    groups = []
    custom_buttons = []
    if guild is not None:
        custom_emojis = sorted(getattr(guild, "emojis", []), key=lambda item: str(getattr(item, "name", "")).lower())
        for emoji in custom_emojis[:250]:
            emoji_name = str(getattr(emoji, "name", "emoji"))
            emoji_id = str(getattr(emoji, "id", ""))
            if not emoji_id:
                continue
            prefix = "a" if bool(getattr(emoji, "animated", False)) else ""
            value = f"<{prefix}:{emoji_name}:{emoji_id}>"
            image_url = str(getattr(emoji, "url", ""))
            custom_buttons.append(
                emoji_choice_html(target_id, mode, value, label=f":{emoji_name}:", image_url=image_url)
            )
    if custom_buttons:
        groups.append(
            '<section class="emoji-group"><h4>Server Emojis</h4>'
            f'<div class="emoji-grid custom-grid">{"".join(custom_buttons)}</div></section>'
        )
    for group_name, emojis in EMOJI_PICKER_GROUPS:
        buttons = "".join(emoji_choice_html(target_id, mode, emoji, label=emoji) for emoji in emojis)
        groups.append(
            f'<section class="emoji-group"><h4>{esc(group_name)}</h4>'
            f'<div class="emoji-grid">{buttons}</div></section>'
        )
    return (
        '<details class="emoji-picker">'
        '<summary>Add emoji</summary>'
        '<input class="emoji-search" type="search" data-emoji-search placeholder="Search emoji or server emoji...">'
        f'<div class="emoji-picker-body">{"".join(groups)}</div>'
        '</details>'
    )


def multi_role_options(guild: Any, selected_ids: list[int]) -> str:
    selected_set = {str(role_id) for role_id in selected_ids}
    roles = [
        role
        for role in sorted(getattr(guild, "roles", []), key=lambda item: item.position, reverse=True)
        if not role.is_default() and not role.managed
    ]
    return "\n".join(
        f'<option value="{role.id}" data-search="{esc((role.name + " " + str(role.id)).lower())}"'
        f'{" selected" if str(role.id) in selected_set else ""}>{esc(role.name)}</option>'
        for role in roles
    )


def safe_dom_id(prefix: str, value: Any) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "field")).strip("-")
    return f"{prefix}-{raw or 'field'}"


def searchable_role_select(
    guild: Any,
    name: str,
    selected_id: Any = "",
    *,
    include_blank: bool = True,
    select_id: str = "",
    placeholder: str = "Search roles...",
) -> str:
    select_id = select_id or safe_dom_id(name, selected_id or "role")
    return (
        f'<input class="select-search" type="search" data-select-filter="#{esc(select_id)}" '
        f'placeholder="{esc(placeholder)}">'
        f'<select class="role-select" id="{esc(select_id)}" name="{esc(name)}" data-role-select>'
        f'{role_options(guild, selected_id, include_blank=include_blank)}</select>'
    )


def searchable_multi_role_select(
    guild: Any,
    name: str,
    selected_ids: list[int],
    *,
    select_id: str = "",
    placeholder: str = "Search roles...",
) -> str:
    select_id = select_id or safe_dom_id(name, "roles")
    return (
        f'<input class="select-search" type="search" data-select-filter="#{esc(select_id)}" '
        f'placeholder="{esc(placeholder)}">'
        f'<select class="role-select" id="{esc(select_id)}" name="{esc(name)}" multiple data-role-select>'
        f'{multi_role_options(guild, selected_ids)}</select>'
    )


def discord_logo_svg() -> str:
    return (
        '<svg class="discord-icon" viewBox="0 0 127.14 96.36" aria-hidden="true" focusable="false">'
        '<path fill="currentColor" d="M107.7 8.07A105.15 105.15 0 0 0 81.47 0a72.06 72.06 0 0 0-3.36 6.83A97.68 97.68 0 0 0 49 6.83 72.37 72.37 0 0 0 45.64 0 105.89 105.89 0 0 0 19.39 8.09C2.79 32.65-1.71 56.6.54 80.21A105.73 105.73 0 0 0 32.71 96.36a77.7 77.7 0 0 0 6.89-11.11 68.42 68.42 0 0 1-10.85-5.18c.91-.66 1.8-1.34 2.66-2a75.57 75.57 0 0 0 64.32 0c.87.71 1.76 1.39 2.66 2a68.68 68.68 0 0 1-10.87 5.19 77 77 0 0 0 6.89 11.1 105.25 105.25 0 0 0 32.19-16.14c2.64-27.38-4.51-51.11-18.9-72.15ZM42.45 65.69C36.18 65.69 31 60 31 53s5-12.74 11.43-12.74S54 46 53.89 53s-5.05 12.69-11.44 12.69Zm42.24 0C78.41 65.69 73.25 60 73.25 53s5-12.74 11.44-12.74S96.23 46 96.12 53s-5.04 12.69-11.43 12.69Z"/>'
        '</svg>'
    )


def base_layout(title: str, body: str, *, session: Optional[dict[str, Any]] = None, active: str = "dashboard") -> str:
    if active == "applications":
        active = "dashboard"
    user = session.get("user", {}) if session else {}
    avatar = user.get("avatar_url")
    user_label = esc(user.get("username") or "Login")
    discord_icon = discord_logo_svg()
    user_avatar_markup = f'<img src="{esc(avatar)}" alt="">' if avatar else f"<span>{discord_icon}</span>"
    login_button = (
        f'<a class="user-chip" href="/applications/logout">{user_avatar_markup}'
        f"<strong>{user_label}</strong><em>Logout</em></a>"
        if session
        else f'<a class="login-button" href="/applications/login">{discord_icon}<strong>Login</strong></a>'
    )
    nav = {
        "home": "/",
        "dashboard": "/applications",
        "support": SUPPORT_SERVER_URL or "/",
    }
    support_attrs = ' target="_blank" rel="noopener"' if SUPPORT_SERVER_URL else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - {esc(APP_NAME)}</title>
  <link rel="icon" href="/assets/gem-tool-logo.svg" type="image/svg+xml">
  <link rel="shortcut icon" href="/assets/gem-tool-logo.svg" type="image/svg+xml">
  <style>
    :root {{
      color-scheme: dark;
      --bg: #050811;
      --panel: rgba(11, 18, 29, .82);
      --panel-2: rgba(19, 28, 45, .88);
      --line: rgba(210, 236, 255, .15);
      --line-strong: rgba(111, 245, 221, .36);
      --text: #f8fbff;
      --muted: rgba(235, 244, 255, .72);
      --soft: rgba(255, 255, 255, .07);
      --aqua: #29f5d2;
      --cyan: #65c8ff;
      --violet: #7a6cff;
      --red: #ff6172;
      --green: #42ee91;
      --gold: #ffd06a;
      --shadow: rgba(0, 0, 0, .38);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 18% 18%, rgba(41, 245, 210, .18), transparent 28%),
        radial-gradient(circle at 84% 24%, rgba(101, 200, 255, .16), transparent 28%),
        linear-gradient(132deg, #04070d 0%, #08140f 38%, #15190e 68%, #1b1309 100%);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(116deg, transparent 0 16%, rgba(41,245,210,.09) 16% 25%, transparent 25% 43%, rgba(255,208,106,.08) 43% 54%, transparent 54% 72%, rgba(101,200,255,.1) 72% 82%, transparent 82%);
      opacity: .85;
    }}
    a {{ color: inherit; }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      min-height: 72px;
      padding: 16px clamp(18px, 5vw, 64px);
      border-bottom: 1px solid var(--line);
      background: rgba(5, 9, 14, .78);
      backdrop-filter: blur(18px);
    }}
    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 12px;
      text-decoration: none;
      font-weight: 1000;
      letter-spacing: 0;
    }}
    .logo {{
      width: 46px;
      height: 46px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      filter: drop-shadow(0 14px 28px rgba(41,245,210,.22));
      flex: 0 0 auto;
    }}
    .logo img {{
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }}
    nav {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    nav a {{
      color: var(--muted);
      text-decoration: none;
      padding: 10px 12px;
      border-radius: 12px;
      font-weight: 900;
    }}
    nav a.active, nav a:hover {{
      color: var(--aqua);
      background: rgba(41,245,210,.08);
    }}
    .login-button, .user-chip {{
      display: inline-flex;
      align-items: center;
      gap: 9px;
      text-decoration: none;
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 13px;
      padding: 10px 13px;
      background: rgba(88, 101, 242, .95);
      font-weight: 900;
      box-shadow: 0 16px 32px var(--shadow);
    }}
    .login-button span, .user-chip span {{
      width: 22px;
      height: 22px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: rgba(255,255,255,.18);
      font-weight: 1000;
    }}
    .discord-icon {{
      width: 22px;
      height: 22px;
      flex: 0 0 auto;
      display: block;
    }}
    .user-chip {{
      background: rgba(255,255,255,.07);
      padding: 8px 11px;
    }}
    .user-chip img {{
      width: 28px;
      height: 28px;
      border-radius: 50%;
      object-fit: cover;
    }}
    .user-chip em {{
      color: var(--muted);
      font-style: normal;
      font-size: 12px;
      margin-left: 4px;
    }}
    main {{
      position: relative;
      z-index: 1;
      width: min(1220px, 100%);
      margin: 0 auto;
      padding: clamp(34px, 6vw, 82px) clamp(18px, 5vw, 44px);
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(320px, .95fr);
      gap: clamp(24px, 5vw, 58px);
      align-items: center;
      min-height: calc(100vh - 160px);
    }}
    .kicker {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      color: var(--aqua);
      background: rgba(41,245,210,.08);
      font-size: 13px;
      font-weight: 900;
    }}
    h1 {{
      margin: 18px 0 0;
      font-size: clamp(42px, 7vw, 82px);
      line-height: .98;
      letter-spacing: 0;
    }}
    h1 span {{ color: var(--aqua); }}
    .lead {{
      max-width: 720px;
      margin: 22px 0 0;
      color: var(--muted);
      line-height: 1.58;
      font-size: 18px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 16px;
      align-items: start;
    }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
      box-shadow: 0 22px 54px var(--shadow);
      backdrop-filter: blur(18px);
    }}
    .card.pad {{ padding: 22px; }}
    .span-4 {{ grid-column: span 4; }}
    .span-3 {{ grid-column: span 3; }}
    .span-5 {{ grid-column: span 5; }}
    .span-6 {{ grid-column: span 6; }}
    .span-7 {{ grid-column: span 7; }}
    .span-8 {{ grid-column: span 8; }}
    .span-12 {{ grid-column: span 12; }}
    .server-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
      gap: 16px;
      margin-top: 18px;
    }}
    .server-card {{
      display: grid;
      grid-template-columns: 52px 1fr;
      gap: 14px;
      align-items: center;
      padding: 16px;
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255,255,255,.055);
    }}
    .server-card:hover {{ border-color: var(--line-strong); transform: translateY(-1px); }}
    .server-icon {{
      width: 52px;
      height: 52px;
      display: grid;
      place-items: center;
      border-radius: 14px;
      overflow: hidden;
      background: linear-gradient(135deg, var(--aqua), var(--violet));
      color: #071014;
      font-weight: 1000;
    }}
    .server-icon img {{ width: 100%; height: 100%; object-fit: cover; }}
    .server-card b {{ display: block; }}
    .server-card small, .muted {{ color: var(--muted); }}
    .section-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .section-title h2, .section-title h3 {{ margin: 0; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border-radius: 999px;
      padding: 6px 10px;
      background: rgba(41,245,210,.09);
      border: 1px solid var(--line-strong);
      color: var(--aqua);
      font-weight: 900;
      font-size: 12px;
    }}
    label {{
      display: block;
      margin: 12px 0 7px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 850;
    }}
    input, textarea, select {{
      width: 100%;
      border: 1px solid rgba(255,255,255,.14);
      border-radius: 12px;
      padding: 11px 12px;
      color: var(--text);
      background: rgba(3, 6, 12, .58);
      font: inherit;
      outline: none;
    }}
    textarea {{ min-height: 92px; resize: vertical; }}
    select[multiple] {{ min-height: 132px; }}
    select.role-select.is-filtering {{
      min-height: 132px;
      overflow-y: auto;
      background: rgba(3, 6, 12, .92);
    }}
    select.role-select option[hidden] {{
      display: none;
    }}
    input:focus, textarea:focus, select:focus {{ border-color: var(--aqua); box-shadow: 0 0 0 3px rgba(41,245,210,.12); }}
    .button-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 12px;
      padding: 10px 13px;
      color: var(--text);
      background: rgba(255,255,255,.08);
      font-weight: 950;
      cursor: pointer;
      text-decoration: none;
    }}
    button.primary, .button.primary {{ background: linear-gradient(135deg, var(--aqua), var(--cyan)); color: #061014; }}
    button.danger {{ background: rgba(255,97,114,.16); color: #ffd9de; border-color: rgba(255,97,114,.36); }}
    button:hover, .button:hover {{ transform: translateY(-1px); }}
    .notice {{
      margin-bottom: 18px;
      border: 1px solid rgba(255,255,255,.14);
      border-radius: 14px;
      padding: 13px 15px;
      background: rgba(255,255,255,.065);
      color: var(--muted);
    }}
    .notice.ok {{ border-color: rgba(66,238,145,.32); color: #bcffd7; }}
    .notice.error {{ border-color: rgba(255,97,114,.38); color: #ffd3d9; }}
    .panel-list {{ display: grid; gap: 14px; }}
    .panel-item {{
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 15px;
      background: rgba(255,255,255,.045);
    }}
    details.panel-item {{
      padding: 0;
      overflow: hidden;
    }}
    details.panel-item[open] {{
      border-color: rgba(41,245,210,.28);
      background: rgba(41,245,210,.045);
    }}
    .panel-summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 16px;
      cursor: pointer;
      user-select: none;
      list-style: none;
    }}
    .panel-summary::-webkit-details-marker {{ display: none; }}
    .panel-summary-main {{
      min-width: 0;
    }}
    .panel-summary-main b {{
      display: block;
      font-size: 18px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .panel-summary-main span {{
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .panel-summary-meta {{
      display: inline-flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      flex: 0 0 auto;
    }}
    .panel-body {{
      padding: 0 16px 16px;
      border-top: 1px solid rgba(255,255,255,.08);
    }}
    .question {{
      margin-top: 12px;
      padding: 12px;
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 13px;
      background: rgba(0,0,0,.16);
    }}
    .select-search {{
      min-height: 38px;
      margin: 0 0 8px;
      border-radius: 11px;
      font-size: 14px;
    }}
    .select-search:focus + select.role-select {{
      border-color: var(--aqua);
    }}
    .two {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .mini-table {{
      width: 100%;
      border-collapse: collapse;
      color: var(--muted);
    }}
    .mini-table th, .mini-table td {{
      border-bottom: 1px solid rgba(255,255,255,.08);
      padding: 9px 0;
      text-align: left;
    }}
    .mini-table tr.active-row td {{
      background: rgba(41,245,210,.065);
    }}
    .mini-table a {{
      color: var(--cyan);
      text-decoration: none;
    }}
    .feature-tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 0 0 16px;
    }}
    .feature-tabs a {{
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      padding: 10px 14px;
      text-decoration: none;
      font-weight: 950;
      background: rgba(255,255,255,.055);
    }}
    .feature-tabs a.active, .feature-tabs a:hover {{
      color: #061014;
      border-color: transparent;
      background: linear-gradient(135deg, var(--aqua), var(--cyan));
    }}
    .module-note {{
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.55;
    }}
    .inline-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 10px 0 0;
    }}
    .inline-list span {{
      display: inline-flex;
      align-items: center;
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 999px;
      background: rgba(255,255,255,.07);
      color: var(--muted);
      padding: 5px 8px;
      font-size: 12px;
      font-weight: 850;
    }}
    .subtle-card {{
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 14px;
      background: rgba(0,0,0,.18);
      padding: 14px;
      margin-top: 12px;
    }}
    .answer-box {{
      border-radius: 12px;
      background: rgba(3, 6, 12, .48);
      border: 1px solid rgba(255,255,255,.08);
      padding: 12px;
      color: var(--muted);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .overview-hero {{
      display: grid;
      grid-template-columns: 76px 1fr;
      gap: 16px;
      align-items: center;
    }}
    .overview-hero .server-icon {{
      width: 76px;
      height: 76px;
      border-radius: 20px;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }}
    .metric-card {{
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 16px;
      padding: 16px;
      background: linear-gradient(145deg, rgba(255,255,255,.07), rgba(255,255,255,.025));
      min-height: 106px;
    }}
    .metric-card span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      font-weight: 850;
    }}
    .metric-card b {{
      display: block;
      margin-top: 8px;
      font-size: 28px;
      line-height: 1;
    }}
    .module-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .module-card {{
      display: block;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      background: rgba(255,255,255,.045);
      text-decoration: none;
    }}
    .module-card:hover {{
      border-color: var(--line-strong);
      background: rgba(41,245,210,.07);
    }}
    .module-card b {{
      display: block;
      margin-bottom: 6px;
    }}
    .welcome-preview {{
      border-left: 4px solid var(--aqua);
      border-radius: 10px;
      background: rgba(35,37,43,.9);
      padding: 16px;
      white-space: pre-wrap;
      line-height: 1.52;
      color: var(--text);
    }}
    .emoji-picker {{
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255,255,255,.045);
      padding: 10px;
      max-width: 560px;
    }}
    .emoji-picker summary {{
      cursor: pointer;
      color: var(--aqua);
      font-weight: 950;
      user-select: none;
    }}
    .emoji-search {{
      margin-top: 10px;
      min-height: 38px;
      border-radius: 12px;
      font-size: 14px;
    }}
    .emoji-picker-body {{
      margin-top: 10px;
      max-height: 360px;
      overflow: auto;
      padding-right: 4px;
    }}
    .emoji-group {{
      margin: 0 0 12px;
    }}
    .emoji-group h4 {{
      margin: 0 0 8px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .emoji-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(38px, 1fr));
      gap: 7px;
    }}
    .custom-grid {{
      grid-template-columns: repeat(auto-fill, minmax(104px, 1fr));
    }}
    .emoji-choice {{
      min-height: 38px;
      padding: 6px;
      border-radius: 10px;
      font-size: 20px;
      line-height: 1;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.12);
    }}
    .emoji-choice:hover {{
      transform: translateY(-1px);
      border-color: var(--line-strong);
      background: rgba(41,245,210,.12);
    }}
    .custom-emoji {{
      display: inline-flex;
      align-items: center;
      justify-content: flex-start;
      gap: 7px;
      min-width: 0;
      font-size: 12px;
      font-weight: 850;
      overflow: hidden;
    }}
    .custom-emoji img {{
      width: 24px;
      height: 24px;
      border-radius: 6px;
      flex: 0 0 auto;
      object-fit: contain;
    }}
    .custom-emoji span {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .emoji-choice[hidden],
    .emoji-group[hidden] {{
      display: none !important;
    }}
    .submission-toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 12px 0;
    }}
    .submission-table-scroll {{
      max-height: 520px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(0,0,0,.14);
    }}
    .submission-table-scroll table {{
      margin: 0;
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 900;
      color: var(--muted);
      background: rgba(255,255,255,.055);
    }}
    .status-pending {{ color: #ffe58a; border-color: rgba(255,229,138,.35); background: rgba(255,229,138,.08); }}
    .status-accepted {{ color: #7cffbb; border-color: rgba(124,255,187,.35); background: rgba(124,255,187,.08); }}
    .status-denied {{ color: #ff8a9b; border-color: rgba(255,138,155,.35); background: rgba(255,138,155,.08); }}
    .status-ticket {{ color: #84d6ff; border-color: rgba(132,214,255,.35); background: rgba(132,214,255,.08); }}
    .token-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .token-list code {{
      font-size: 12px;
    }}
    .rr-panel-preview {{
      border-left: 4px solid #5865f2;
      padding: 14px 16px;
      border-radius: 8px;
      background: rgba(35, 37, 43, .88);
      margin: 12px 0;
      max-width: 620px;
    }}
    .rr-panel-preview h3 {{
      margin: 0 0 10px;
      font-size: 16px;
    }}
    .rr-panel-preview p {{
      margin: 0;
      color: var(--text);
      white-space: pre-wrap;
      line-height: 1.45;
    }}
    .rr-buttons {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .rr-button-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 8px;
      padding: 8px 12px;
      background: #5865f2;
      color: white;
      font-weight: 900;
      font-size: 14px;
    }}
    code {{
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 7px;
      padding: 2px 6px;
      background: rgba(255,255,255,.08);
      color: var(--text);
    }}
    .policy {{
      max-width: 860px;
      margin: 0 auto;
      padding: clamp(12px, 4vw, 38px) 0 64px;
    }}
    .policy h1 {{
      margin-bottom: 12px;
    }}
    .policy h2 {{
      margin-top: 28px;
    }}
    .policy p, .policy li {{
      color: var(--muted);
      line-height: 1.7;
    }}
    footer {{
      width: min(1220px, 100%);
      margin: 0 auto;
      padding: 0 clamp(18px, 5vw, 44px) 32px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }}
    footer a {{
      color: var(--text);
      text-decoration: none;
      font-weight: 850;
      margin-left: 12px;
    }}
    footer a:hover {{
      color: var(--aqua);
    }}
    @media (max-width: 900px) {{
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .hero, .two {{ grid-template-columns: 1fr; }}
      .span-3, .span-4, .span-5, .span-6, .span-7, .span-8, .span-12 {{ grid-column: span 12; }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="/"><span class="logo"><img src="/assets/gem-tool-logo.svg" alt=""></span><span>{esc(APP_NAME)}</span></a>
    <nav>
      <a class="{"active" if active == "home" else ""}" href="{nav["home"]}">Home</a>
      <a class="{"active" if active == "dashboard" else ""}" href="{nav["dashboard"]}">Dashboard</a>
      <a href="{esc(nav["support"])}"{support_attrs}>Support</a>
    </nav>
    {login_button}
  </header>
  <main>{body}</main>
  <footer>
    <span>{esc(APP_NAME)} for Discord communities.</span>
    <span><a href="/terms">Terms</a><a href="/privacy">Privacy Policy</a></span>
  </footer>
  <script>
    function filterRoleSelect(input) {{
      const select = document.querySelector(input.dataset.selectFilter || "");
      if (!select) return;
      const needle = (input.value || "").trim().toLowerCase();
      let visible = 0;
      Array.from(select.options).forEach((option) => {{
        const hay = (option.dataset.search || `${{option.textContent || ""}} ${{option.value || ""}}`).toLowerCase();
        const hidden = Boolean(needle && option.value && !hay.includes(needle));
        option.hidden = hidden;
        if (!hidden) visible += 1;
      }});
      if (needle) {{
        select.classList.add("is-filtering");
        select.size = Math.min(Math.max(visible, select.multiple ? 4 : 2), 9);
      }} else {{
        select.classList.remove("is-filtering");
        select.removeAttribute("size");
      }}
    }}

    function collapseRoleSelect(select) {{
      if (!select || select.multiple) return;
      select.classList.remove("is-filtering");
      select.removeAttribute("size");
    }}

    document.addEventListener("click", (event) => {{
      const button = event.target.closest("[data-emoji-value]");
      if (!button) return;
      const target = document.getElementById(button.dataset.emojiTarget);
      if (!target) return;
      const emoji = button.dataset.emojiValue || "";
      const mode = button.dataset.emojiMode || "insert";
      target.focus();
      if (mode === "replace" || target.tagName !== "TEXTAREA") {{
        target.value = emoji;
        target.dispatchEvent(new Event("input", {{ bubbles: true }}));
        return;
      }}
      const start = target.selectionStart ?? target.value.length;
      const end = target.selectionEnd ?? target.value.length;
      target.value = target.value.slice(0, start) + emoji + target.value.slice(end);
      target.selectionStart = target.selectionEnd = start + emoji.length;
      target.dispatchEvent(new Event("input", {{ bubbles: true }}));
    }});
    document.addEventListener("input", (event) => {{
      const roleSearch = event.target.closest("[data-select-filter]");
      if (roleSearch) {{
        filterRoleSelect(roleSearch);
        return;
      }}
      const search = event.target.closest("[data-emoji-search]");
      if (!search) return;
      const picker = search.closest(".emoji-picker");
      if (!picker) return;
      const needle = (search.value || "").trim().toLowerCase();
      picker.querySelectorAll(".emoji-choice").forEach((button) => {{
        const label = (button.dataset.emojiLabel || button.textContent || "").toLowerCase();
        button.hidden = Boolean(needle && !label.includes(needle));
      }});
      picker.querySelectorAll(".emoji-group").forEach((group) => {{
        const hasVisible = Array.from(group.querySelectorAll(".emoji-choice")).some((button) => !button.hidden);
        group.hidden = Boolean(needle && !hasVisible);
      }});
    }});
    document.addEventListener("change", (event) => {{
      const select = event.target.closest("select[data-role-select]");
      if (select) collapseRoleSelect(select);
    }});
    document.addEventListener("focusout", (event) => {{
      const input = event.target.closest("[data-select-filter]");
      if (!input) return;
      window.setTimeout(() => {{
        const select = document.querySelector(input.dataset.selectFilter || "");
        if (document.activeElement !== select) collapseRoleSelect(select);
      }}, 100);
    }});
  </script>
</body>
</html>"""


def render_login(session: Optional[dict[str, Any]], query: dict[str, list[str]]) -> str:
    error = form_one(query, "error")
    setup = ""
    if not dashboard_ready():
        setup = f"""
        <div class="notice error">
          Discord login is not fully configured yet. Set <code>DISCORD_CLIENT_SECRET</code>
          and <code>PUBLIC_BASE_URL</code> in Render. <code>DISCORD_CLIENT_ID</code>
          is optional once the bot is online, but adding it makes startup login available immediately.
          Add this redirect URL in the Discord Developer Portal:
          <br><br><code>{esc(oauth_redirect_uri())}</code>
        </div>"""
    error_html = f'<div class="notice error">{esc(error)}</div>' if error else ""
    owner_login = ""
    if DASHBOARD_OWNER_TOKEN:
        owner_login = """
        <div class="notice">
          <strong>Owner recovery login</strong>
          <p class="muted">Use this only when Discord OAuth is temporarily rate limited.</p>
          <form method="post" action="/applications/owner-login">
            <label>Owner token</label>
            <input type="password" name="token" autocomplete="current-password" required>
            <div class="button-row">
              <button type="submit">Open dashboard</button>
            </div>
          </form>
        </div>"""
    body = f"""
    <section class="hero">
      <div>
        <span class="kicker">Admin dashboard</span>
        <h1><span>Gem Tool Application Panel</span></h1>
        <p class="lead">
          Log in with Discord, choose a server you manage, then configure application panels,
          questions, review channels, accepted roles, tickets, and giveaway access settings.
        </p>
        <div class="grid" style="margin-top: 28px;">
          <div class="card pad span-3"><span class="pill">1</span><h3>Pick server</h3><p class="muted">Only servers where you have Manage Server or Administrator are shown.</p></div>
          <div class="card pad span-3"><span class="pill">2</span><h3>Create panels</h3><p class="muted">Build staff, partner, creator, or custom application forms.</p></div>
          <div class="card pad span-3"><span class="pill">3</span><h3>Add questions</h3><p class="muted">Text answers and dropdown selections are both supported.</p></div>
          <div class="card pad span-3"><span class="pill">4</span><h3>Post panel</h3><p class="muted">Users apply from the server and answer in DMs.</p></div>
        </div>
      </div>
      <aside class="card pad">
        <span class="pill">Secure Discord login</span>
        <h2>Login</h2>
        <p class="muted">Sign in with Discord to manage the servers where you have permission.</p>
        {setup}
        {error_html}
        <div class="button-row">
          <a class="button primary" href="/applications/login">{discord_logo_svg()}<strong>Login</strong></a>
        </div>
        {owner_login}
      </aside>
    </section>"""
    return base_layout("Dashboard", body, session=session, active="dashboard")


def render_policy_page(kind: str) -> str:
    is_terms = kind == "terms"
    title = "Terms of Service" if is_terms else "Privacy Policy"
    if is_terms:
        body = f"""
        <section class="policy">
          <span class="kicker">Legal</span>
          <h1>{esc(APP_NAME)} Terms of Service</h1>
          <p>By inviting or using {esc(APP_NAME)}, you agree to use the bot responsibly and only in servers where you have permission to manage applications, giveaways, and related settings.</p>
          <h2>Allowed Use</h2>
          <p>{esc(APP_NAME)} provides Discord application panels, review tools, and giveaway utilities. Server administrators are responsible for the content they configure, including questions, roles, channels, prizes, and giveaway requirements.</p>
          <h2>Availability</h2>
          <p>The service is provided as-is. We try to keep the bot online and reliable, but we cannot guarantee uninterrupted access or message delivery from Discord or Render.</p>
          <h2>Server Content</h2>
          <p>Do not use the bot for illegal, abusive, hateful, fraudulent, or unsafe activity. We may remove access for servers that abuse the service or attempt to exploit it.</p>
          <h2>Contact</h2>
          <p>For support, join the official support server linked on the website.</p>
        </section>"""
    else:
        body = f"""
        <section class="policy">
          <span class="kicker">Legal</span>
          <h1>{esc(APP_NAME)} Privacy Policy</h1>
          <p>{esc(APP_NAME)} stores only the data needed to run application panels, submissions, giveaway entries, server settings, and the dashboard login session.</p>
          <h2>Data We Store</h2>
          <ul>
            <li>Discord user IDs, usernames, and avatars for application submissions and dashboard sessions.</li>
            <li>Server IDs, channel IDs, role IDs, panel settings, questions, and review settings.</li>
            <li>Giveaway IDs, prizes, participants, winner IDs, requirements, and message links.</li>
            <li>Message count statistics only when giveaway message requirements are used.</li>
          </ul>
          <h2>How Data Is Used</h2>
          <p>Data is used to show dashboards, process applications, manage giveaways, enforce requirements, and keep per-server settings after redeploys.</p>
          <h2>Data Sharing</h2>
          <p>We do not sell your data. Data may be processed by Discord and Render because the bot runs through those services.</p>
          <h2>Removal</h2>
          <p>Server administrators can delete panels, questions, submissions, and giveaways from the dashboard or Discord commands. For support, use the official support server.</p>
        </section>"""
    return base_layout(title, body, active="")


def render_server_selection(session: dict[str, Any], query: dict[str, list[str]]) -> str:
    bot_lookup = bot_guilds()
    manageable = session.get("manageable_guilds", {})
    visible_guilds = []
    for guild_id, record in manageable.items():
        guild = bot_lookup.get(str(guild_id))
        if guild:
            visible_guilds.append((guild, record))
    visible_guilds.sort(key=lambda item: item[0].name.lower())

    cards = []
    for guild, _record in visible_guilds:
        cards.append(
            f"""
            <a class="server-card" href="/applications?guild_id={guild.id}">
              <span class="server-icon">{guild_icon(guild, guild.name)}</span>
              <span><b>{esc(guild.name)}</b><small>Configure applications, giveaways, suggestions, reaction roles, and welcome messages</small></span>
            </a>"""
        )

    empty = ""
    if not visible_guilds:
        empty = """
        <div class="notice">
          No manageable servers are available yet. Make sure the bot is invited to the server and
          your Discord account has Manage Server or Administrator there.
        </div>"""

    bot_warning = "" if bot_is_ready() else '<div class="notice error">Gem Tool is starting or offline, so server data may not be available yet.</div>'
    body = f"""
    {render_flash(query)}
    {bot_warning}
    <section class="card pad">
      <div class="section-title">
        <div>
          <span class="pill">Server selection</span>
          <h1 style="font-size: clamp(34px, 5vw, 58px); margin-top: 12px;">Choose a server</h1>
          <p class="muted">Only servers you can manage and where Gem Tool is installed are shown.</p>
        </div>
      </div>
      {empty}
      <div class="server-grid">{"".join(cards)}</div>
    </section>"""
    return base_layout("Dashboard", body, session=session, active="dashboard")


def render_flash(query: dict[str, list[str]]) -> str:
    ok = form_one(query, "ok")
    error = form_one(query, "error")
    if error:
        return f'<div class="notice error">{esc(error)}</div>'
    if ok:
        return f'<div class="notice ok">{esc(ok)}</div>'
    return ""


def render_panel_questions(panel_key: str, panel: dict[str, Any], guild_id: int) -> str:
    rows = []
    questions = application_system.panel_questions(panel)
    for index, question in enumerate(questions, start=1):
        choices = "|".join(question.get("options", []))
        rows.append(
            f"""
            <div class="question">
              <form method="post" action="/applications?guild_id={guild_id}">
                <input type="hidden" name="action" value="update_question">
                <input type="hidden" name="panel_key" value="{esc(panel_key)}">
                <input type="hidden" name="question_number" value="{index}">
                <label>Question {index}</label>
                <textarea name="text" maxlength="300">{esc(question.get("text"))}</textarea>
                <label>Dropdown choices, optional. Use <code>yes|no</code> format.</label>
                <input name="choices" value="{esc(choices)}" placeholder="Leave empty for text answer">
                <div class="button-row">
                  <button class="primary" type="submit">Save question</button>
                  <button class="danger" type="submit" name="action" value="delete_question">Delete question</button>
                </div>
              </form>
            </div>"""
        )
    if not rows:
        return '<p class="muted">No questions yet. Add the first one below.</p>'
    return "".join(rows)


def dashboard_tab_nav(guild_id: int, active_tab: str) -> str:
    tabs = [
        ("overview", "Overview"),
        ("applications", "Applications"),
        ("submissions", "Submissions"),
        ("giveaways", "Giveaways"),
        ("suggestions", "Suggestions"),
        ("reaction-roles", "Reaction Roles"),
        ("welcome", "Welcomer"),
    ]
    return '<div class="feature-tabs">' + "".join(
        f'<a class="{"active" if key == active_tab else ""}" href="/applications?guild_id={guild_id}&tab={key}">{label}</a>'
        for key, label in tabs
    ) + "</div>"


def role_badges(guild: Any, role_ids: list[int]) -> str:
    if not role_ids:
        return '<span>None</span>'
    return "".join(f"<span>@{esc(role_name(guild, role_id))}</span>" for role_id in role_ids)


def submission_status_label(submission: dict[str, Any]) -> str:
    status = str(submission.get("status") or "pending").title()
    if submission.get("ticket_closed_at"):
        return f"{status} / Ticket Closed"
    if submission.get("ticket_channel_id"):
        return f"{status} / Ticket Open"
    return status


def render_submission_answers(submission: dict[str, Any]) -> str:
    answers = submission.get("answers", [])
    if not answers:
        return '<p class="muted">No answers were saved for this submission.</p>'
    rows = []
    for index, answer in enumerate(answers, start=1):
        rows.append(
            f"""
            <div class="subtle-card">
              <h4 style="margin: 0 0 8px;">{index}. {esc(answer.get("question") or f"Question {index}")}</h4>
              <div class="answer-box">{esc(answer.get("answer") or "No answer")}</div>
            </div>"""
        )
    return "".join(rows)


def render_submission_overview(
    guild_id: int,
    panels: dict[str, Any],
    submissions: dict[str, Any],
    selected_submission_id: str,
) -> str:
    all_submissions = {
        submission_id: submission
        for submission_id, submission in submissions.items()
        if isinstance(submission, dict)
    }
    sorted_submissions = sorted(
        all_submissions.items(),
        key=lambda item: int(item[1].get("created_at") or 0),
        reverse=True,
    )
    selected_submission = all_submissions.get(selected_submission_id) if selected_submission_id else None
    if not selected_submission and sorted_submissions:
        selected_submission_id, selected_submission = sorted_submissions[0]

    def status_badge_class(submission: dict[str, Any]) -> str:
        if submission.get("ticket_channel_id") and not submission.get("ticket_closed_at"):
            return "status-ticket"
        status = str(submission.get("status") or "pending").lower()
        if status == "accepted":
            return "status-accepted"
        if status == "denied":
            return "status-denied"
        return "status-pending"

    pending_count = sum(1 for submission in all_submissions.values() if str(submission.get("status") or "pending").lower() == "pending")
    accepted_count = sum(1 for submission in all_submissions.values() if str(submission.get("status") or "").lower() == "accepted")
    denied_count = sum(1 for submission in all_submissions.values() if str(submission.get("status") or "").lower() == "denied")
    ticket_open_count = sum(1 for submission in all_submissions.values() if submission.get("ticket_channel_id") and not submission.get("ticket_closed_at"))

    table_rows = []
    for submission_id, submission in sorted_submissions:
        panel_key = str(submission.get("panel_key") or "")
        panel = panels.get(panel_key, {})
        panel_name = panel.get("name") or panel_key or "Application"
        active_class = " active-row" if submission_id == selected_submission_id else ""
        submitted_label = format_timestamp(submission.get("created_at"))
        table_rows.append(
            f"""
            <tr class="{active_class}">
              <td><a href="/applications?guild_id={guild_id}&tab=submissions&submission_id={esc(submission_id)}"><code>{esc(submission_id)}</code></a></td>
              <td>{esc(submission.get("username") or submission.get("user_id") or "Unknown")}</td>
              <td>{esc(panel_name)}</td>
              <td><span class="status-badge {status_badge_class(submission)}">{esc(submission_status_label(submission))}</span></td>
              <td><span class="muted">{submitted_label}</span></td>
            </tr>"""
        )
    rows_html = "".join(table_rows) or '<tr><td colspan="5" class="muted">No saved applications yet.</td></tr>'

    if selected_submission:
        panel_key = str(selected_submission.get("panel_key") or "")
        panel = panels.get(panel_key, {})
        panel_name = str(panel.get("name") or panel_key or "Application")
        created_at = format_timestamp(selected_submission.get("created_at"))
        review_link = ""
        if selected_submission.get("review_channel_id") and selected_submission.get("review_message_id"):
            review_link = (
                f'<a class="button" target="_blank" rel="noopener" '
                f'href="https://discord.com/channels/{guild_id}/{int(selected_submission["review_channel_id"])}/{int(selected_submission["review_message_id"])}">'
                "Open Discord review</a>"
            )
        ticket_note = ""
        if selected_submission.get("ticket_closed_at"):
            ticket_note = f'<span class="pill">Ticket closed {esc(format_timestamp(selected_submission.get("ticket_closed_at")))}</span>'
        elif selected_submission.get("ticket_channel_id"):
            ticket_note = '<span class="pill">Ticket open</span>'
        detail = f"""
        <div class="card pad span-7">
          <div class="section-title">
            <div>
              <span class="pill">Submission detail</span>
              <h2 style="margin: 10px 0 2px;">{esc(selected_submission.get("username") or "Unknown applicant")}</h2>
              <p class="muted" style="margin: 0;">{esc(panel_name)} - {esc(submission_status_label(selected_submission))}</p>
            </div>
            {ticket_note}
          </div>
          <div class="metric-grid">
            <div class="metric-card"><span>Submitted</span><b style="font-size:18px;">{esc(created_at)}</b></div>
            <div class="metric-card"><span>Duration</span><b style="font-size:18px;">{esc(application_system.format_duration(int(selected_submission.get("duration_seconds") or 0)))}</b></div>
            <div class="metric-card"><span>User ID</span><b style="font-size:18px;">{esc(selected_submission.get("user_id") or "Unknown")}</b></div>
          </div>
          {render_submission_answers(selected_submission)}
          <div class="button-row">{review_link}</div>
        </div>"""
    else:
        detail = """
        <div class="card pad span-7">
          <h2>Submission detail</h2>
          <p class="muted">Select an application from the table to view the saved answers.</p>
        </div>"""

    return f"""
      <div class="card pad span-5">
        <div class="section-title"><h2>Applications overview</h2><span class="pill">{fmt_count(len(all_submissions))} saved</span></div>
        <p class="module-note">Review every saved submission from this server. Scroll the table to reach older applications.</p>
        <div class="submission-toolbar">
          <span class="pill">Pending {fmt_count(pending_count)}</span>
          <span class="pill">Accepted {fmt_count(accepted_count)}</span>
          <span class="pill">Denied {fmt_count(denied_count)}</span>
          <span class="pill">Ticket open {fmt_count(ticket_open_count)}</span>
        </div>
        <div class="submission-table-scroll">
          <table class="mini-table">
            <thead><tr><th>ID</th><th>User</th><th>Panel</th><th>Status</th><th>Submitted</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
      </div>
      {detail}"""


def reaction_panel_options(panels: dict[str, Any], selected_id: str = "") -> str:
    options = ['<option value="">Choose panel</option>']
    for panel_id, panel in sorted(panels.items(), key=lambda item: str(item[1].get("name", item[0])).lower()):
        label = f"{panel.get('name') or panel_id} ({panel_id})"
        options.append(f'<option value="{esc(panel_id)}"{selected(panel_id, selected_id)}>{esc(label)}</option>')
    return "\n".join(options)


def style_options(selected_style: str = "primary") -> str:
    labels = {
        "primary": "Blue",
        "secondary": "Grey",
        "success": "Green",
        "danger": "Red",
    }
    return "\n".join(
        f'<option value="{key}"{selected(key, selected_style)}>{label}</option>'
        for key, label in labels.items()
    )


def render_guild_dashboard(session: dict[str, Any], guild_id: int, query: dict[str, list[str]]) -> str:
    guild = bot_guilds().get(str(guild_id))
    if not guild:
        return render_server_selection(session, {"error": ["That server is not available. Is Gem Tool invited and online?"]})

    tab = form_one(query, "tab", "overview").lower().replace("_", "-")
    valid_tabs = {"overview", "applications", "submissions", "giveaways", "suggestions", "reaction-roles", "welcome"}
    if tab not in valid_tabs:
        tab = "overview"
    tab_input = f'<input type="hidden" name="tab" value="{esc(tab)}">'
    guild_state = application_system.get_guild_state(guild_id)
    panels = guild_state.setdefault("panels", {})
    submissions = guild_state.setdefault("submissions", {})
    selected_submission_id = form_one(query, "submission_id")

    panel_cards = []
    for panel_key, panel in sorted(panels.items()):
        questions_html = render_panel_questions(panel_key, panel, guild_id)
        accepted_role_id = panel.get("accepted_role_id") or ""
        panel_name = panel.get("name", panel_key)
        panel_description = panel.get("description", "")
        question_count = len(application_system.panel_questions(panel))
        enabled = panel.get("enabled", True) is not False
        accepted_role_select = searchable_role_select(
            guild,
            "accepted_role_id",
            accepted_role_id,
            select_id=safe_dom_id("accepted-role", panel_key),
        )
        panel_cards.append(
            f"""
            <details class="panel-item">
              <summary class="panel-summary">
                <span class="panel-summary-main">
                  <b>{esc(panel_name)}</b>
                  <span>{esc(panel_description or "No description")}</span>
                </span>
                <span class="panel-summary-meta">
                  <span class="pill">{question_count} question{'s' if question_count != 1 else ''}</span>
                  <span class="status-badge {'status-accepted' if enabled else 'status-denied'}">{'Open' if enabled else 'Hidden'}</span>
                </span>
              </summary>
              <div class="panel-body">
                <form method="post" action="/applications?guild_id={guild_id}">
                  <input type="hidden" name="tab" value="applications">
                  <input type="hidden" name="action" value="update_panel">
                  <input type="hidden" name="panel_key" value="{esc(panel_key)}">
                  <div class="two">
                    <div>
                      <label>Panel name</label>
                      <input name="name" maxlength="80" value="{esc(panel_name)}">
                    </div>
                    <div>
                      <label>Accepted role</label>
                      {accepted_role_select}
                    </div>
                  </div>
                  <label>Description</label>
                  <input name="description" maxlength="100" value="{esc(panel_description)}">
                  <label><input style="width:auto; margin-right: 8px;" type="checkbox" name="enabled"{checked(enabled)}> Show in dropdown</label>
                  <div class="button-row">
                    <button class="primary" type="submit">Save panel</button>
                    <button class="danger" type="submit" name="action" value="delete_panel">Delete panel</button>
                  </div>
                </form>
                <h4>Questions</h4>
                {questions_html}
                <form method="post" action="/applications?guild_id={guild_id}">
                  <input type="hidden" name="tab" value="applications">
                  <input type="hidden" name="action" value="add_question">
                  <input type="hidden" name="panel_key" value="{esc(panel_key)}">
                  <div class="two">
                    <div>
                      <label>Insert position</label>
                      <input name="question_number" type="number" min="1" value="{question_count + 1}">
                    </div>
                    <div>
                      <label>Dropdown choices, optional</label>
                      <input name="choices" placeholder="yes|no or leave empty">
                    </div>
                  </div>
                  <label>New question</label>
                  <textarea name="text" maxlength="300" placeholder="What should the applicant answer?"></textarea>
                  <div class="button-row"><button class="primary" type="submit">Add question</button></div>
                </form>
              </div>
            </details>"""
        )

    if not panel_cards:
        panel_cards.append('<div class="notice">No panels yet. Create one below, then add questions.</div>')

    support_bot = load_support_bot()
    giveaway_settings = support_bot.get_giveaway_settings(guild_id) if support_bot else {"creator_role_ids": [], "manager_role_ids": []}
    giveaway_defaults = giveaway_settings.get("defaults", {}) if isinstance(giveaway_settings, dict) else {}
    if support_bot:
        giveaway_defaults = support_bot.normalize_giveaway_defaults(giveaway_defaults)
    def giveaway_default(key: str, fallback: int = 0) -> int:
        try:
            return int(giveaway_defaults.get(key) or fallback)
        except (TypeError, ValueError):
            return fallback

    default_extra_entries = ""
    if isinstance(giveaway_defaults.get("extra_entries"), dict):
        default_extra_rows = []
        for raw_role_id, amount in giveaway_defaults.get("extra_entries", {}).items():
            try:
                role_id = int(raw_role_id)
            except (TypeError, ValueError):
                continue
            default_extra_rows.append(f"{role_name(guild, role_id)}:{int(amount)}")
        default_extra_entries = "\n".join(default_extra_rows)
    giveaways = []
    if support_bot:
        giveaways = [
            giveaway
            for giveaway in support_bot.load_giveaways().values()
            if int(giveaway.get("guild_id") or 0) == guild_id
        ]
    active_count = sum(1 for giveaway in giveaways if not giveaway.get("ended"))
    ended_count = len(giveaways) - active_count
    giveaway_rows = "".join(
        f"<tr><td><code>{esc(giveaway.get('id'))}</code></td><td>{esc(giveaway.get('prize'))}</td><td>{'Ended' if giveaway.get('ended') else 'Active'}</td></tr>"
        for giveaway in sorted(giveaways, key=lambda item: int(item.get("created_at") or 0), reverse=True)[:8]
    ) or '<tr><td colspan="3" class="muted">No giveaways stored for this server yet.</td></tr>'

    member_count = getattr(guild, "member_count", None) or len(getattr(guild, "members", []) or [])
    text_channel_count = len(getattr(guild, "text_channels", []) or [])
    voice_channel_count = len(getattr(guild, "voice_channels", []) or [])
    total_message_count = support_bot.get_guild_total_message_count(guild_id) if support_bot else 0
    default_welcome_message = getattr(
        support_bot,
        "DEFAULT_WELCOME_MESSAGE",
        "\U0001f44b | Welcome {user} to **{server}**, you are member **{member_count}**!\n\n"
        "\U0001f4d6 | Please look in {rules_channel} for the rules of the server.",
    )
    default_leave_message = getattr(
        support_bot,
        "DEFAULT_LEAVE_MESSAGE",
        "\U0001f44b | **{username}** has left **{server}**.\n\n"
        "\U0001f465 | We now have **{member_count}** members.",
    )
    welcome_settings = support_bot.get_welcome_settings(guild_id) if support_bot else {
        "enabled": False,
        "channel_id": 0,
        "rules_channel_id": 0,
        "message_template": default_welcome_message,
        "leave_enabled": False,
        "leave_channel_id": 0,
        "leave_message_template": default_leave_message,
    }
    rules_channel_id = int(welcome_settings.get("rules_channel_id") or 0)
    rules_channel = guild.get_channel(rules_channel_id) if rules_channel_id else None
    rules_label = f"#{getattr(rules_channel, 'name', 'rules')}" if rules_channel else "#rules"
    preview_text = str(welcome_settings.get("message_template") or default_welcome_message)
    for token, value in {
        "{user}": "@NewMember",
        "{username}": "NewMember",
        "{server}": guild.name,
        "{member_count}": fmt_count(member_count),
        "{rules_channel}": rules_label,
        "{rules}": rules_label,
    }.items():
        preview_text = preview_text.replace(token, str(value))
    leave_preview_text = str(welcome_settings.get("leave_message_template") or default_leave_message)
    for token, value in {
        "{user}": "@LeavingMember",
        "{username}": "LeavingMember",
        "{server}": guild.name,
        "{member_count}": fmt_count(max(member_count - 1, 0)),
    }.items():
        leave_preview_text = leave_preview_text.replace(token, str(value))

    overview_section = f"""
      <div class="card pad span-12">
        <div class="overview-hero">
          <span class="server-icon">{guild_icon(guild, guild.name)}</span>
          <div>
            <span class="pill">Server overview</span>
            <h2 style="margin: 10px 0 4px;">{esc(guild.name)}</h2>
            <p class="muted" style="margin: 0;">Live Discord data plus saved Gem Tool module data for this server.</p>
          </div>
        </div>
        <div class="metric-grid">
          <div class="metric-card"><span>Members</span><b>{fmt_count(member_count)}</b></div>
          <div class="metric-card"><span>Text Channels</span><b>{fmt_count(text_channel_count)}</b></div>
          <div class="metric-card"><span>Voice Channels</span><b>{fmt_count(voice_channel_count)}</b></div>
          <div class="metric-card"><span>Total Messages Tracked</span><b>{fmt_count(total_message_count)}</b></div>
          <div class="metric-card"><span>Active Giveaways</span><b>{fmt_count(active_count)}</b></div>
          <div class="metric-card"><span>Ended Giveaways</span><b>{fmt_count(ended_count)}</b></div>
          <div class="metric-card"><span>Application Panels</span><b>{fmt_count(len(panels))}</b></div>
          <div class="metric-card"><span>Welcome Messages</span><b>{'On' if welcome_settings.get('enabled') else 'Off'}</b></div>
          <div class="metric-card"><span>Leave Messages</span><b>{'On' if welcome_settings.get('leave_enabled') else 'Off'}</b></div>
        </div>
      </div>
      <div class="card pad span-12">
        <div class="section-title"><h2>Modules</h2><span class="pill">Configure this server</span></div>
        <div class="module-grid">
          <a class="module-card" href="/applications?guild_id={guild_id}&tab=applications"><b>Applications</b><span class="muted">Panels, questions, logs, tickets, accepted roles.</span></a>
          <a class="module-card" href="/applications?guild_id={guild_id}&tab=submissions"><b>Submissions</b><span class="muted">Saved answers, timing, review links, and ticket status.</span></a>
          <a class="module-card" href="/applications?guild_id={guild_id}&tab=giveaways"><b>Giveaways</b><span class="muted">Role access and stored active/ended giveaway data.</span></a>
          <a class="module-card" href="/applications?guild_id={guild_id}&tab=suggestions"><b>Suggestions</b><span class="muted">Channels, voting, anonymous mode, moderator decisions.</span></a>
          <a class="module-card" href="/applications?guild_id={guild_id}&tab=reaction-roles"><b>Reaction Roles</b><span class="muted">Button-based role panels users can toggle.</span></a>
          <a class="module-card" href="/applications?guild_id={guild_id}&tab=welcome"><b>Welcomer</b><span class="muted">Welcome and leave text, rules channel, and member count.</span></a>
        </div>
      </div>"""

    submissions_section = render_submission_overview(guild_id, panels, submissions, selected_submission_id)

    welcome_section = f"""
      <div class="card pad span-6">
        <div class="section-title"><h2>Welcome message</h2><span class="pill">{'Enabled' if welcome_settings.get('enabled') else 'Disabled'}</span></div>
        <p class="module-note">Send a polished message when a new member joins. Placeholders are replaced when the message is sent.</p>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="welcome">
          <input type="hidden" name="action" value="save_welcome_settings">
          <input type="hidden" name="save_section" value="welcome">
          <label><input style="width:auto; margin-right: 8px;" type="checkbox" name="enabled"{checked(bool(welcome_settings.get("enabled")))}> Enable welcome messages</label>
          <label>Welcome channel</label>
          <select name="welcome_channel_id">{channel_options(guild, welcome_settings.get("channel_id"))}</select>
          <label>Rules channel, optional</label>
          <select name="rules_channel_id">{channel_options(guild, welcome_settings.get("rules_channel_id"))}</select>
          <label>Welcome text</label>
          <textarea id="welcome-message-template" name="message_template" maxlength="1800">{esc(welcome_settings.get("message_template") or default_welcome_message)}</textarea>
          {emoji_picker("welcome-message-template", guild=guild)}
          <div class="token-list">
            <code>{'{user}'}</code><code>{'{username}'}</code><code>{'{server}'}</code><code>{'{member_count}'}</code><code>{'{rules_channel}'}</code>
          </div>
          <div class="button-row"><button class="primary" type="submit">Save welcomer</button></div>
        </form>
      </div>
      <div class="card pad span-6">
        <h2>Welcome preview</h2>
        <p class="module-note">This is only a preview. Discord will use real member mentions and counts when someone joins.</p>
        <div class="welcome-preview">{esc(preview_text)}</div>
      </div>
      <div class="card pad span-6">
        <div class="section-title"><h2>Leave message</h2><span class="pill">{'Enabled' if welcome_settings.get('leave_enabled') else 'Disabled'}</span></div>
        <p class="module-note">Send a message when a member leaves. Keep it short and clean for busy servers.</p>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="welcome">
          <input type="hidden" name="action" value="save_welcome_settings">
          <input type="hidden" name="save_section" value="leave">
          <label><input style="width:auto; margin-right: 8px;" type="checkbox" name="leave_enabled"{checked(bool(welcome_settings.get("leave_enabled")))}> Enable leave messages</label>
          <label>Leave channel</label>
          <select name="leave_channel_id">{channel_options(guild, welcome_settings.get("leave_channel_id"))}</select>
          <label>Leave text</label>
          <textarea id="leave-message-template" name="leave_message_template" maxlength="1800">{esc(welcome_settings.get("leave_message_template") or default_leave_message)}</textarea>
          {emoji_picker("leave-message-template", guild=guild)}
          <div class="token-list">
            <code>{'{user}'}</code><code>{'{username}'}</code><code>{'{server}'}</code><code>{'{member_count}'}</code>
          </div>
          <div class="button-row"><button class="primary" type="submit">Save leave message</button></div>
        </form>
      </div>
      <div class="card pad span-6">
        <h2>Leave preview</h2>
        <p class="module-note">Discord will use the real user and server member count when someone leaves.</p>
        <div class="welcome-preview">{esc(leave_preview_text)}</div>
      </div>"""

    application_section = f"""
      <div class="card pad span-5">
        <h2>Application channels</h2>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="applications">
          <input type="hidden" name="action" value="save_channels">
          <label>Application panel channel</label>
          <select name="application_channel_id">{channel_options(guild, guild_state.get("application_channel_id"))}</select>
          <label>Review/log channel</label>
          <select name="log_channel_id">{channel_options(guild, guild_state.get("log_channel_id"))}</select>
          <div class="button-row">
            <button class="primary" type="submit">Save channels</button>
            <button type="submit" name="action" value="post_panel">Post or refresh panel</button>
          </div>
        </form>
      </div>

      <div class="card pad span-7">
        <h2>Application text</h2>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="applications">
          <input type="hidden" name="action" value="save_text">
          <label>Text above the dropdown</label>
          <textarea name="panel_text" maxlength="1000">{esc(guild_state.get("panel_text", application_system.DEFAULT_PANEL_TEXT))}</textarea>
          <div class="button-row"><button class="primary" type="submit">Save text</button></div>
        </form>
      </div>

      <div class="card pad span-12">
        <div class="section-title"><h2>Application panels</h2><span class="pill">{len(panels)} panel(s)</span></div>
        <div class="panel-list">{"".join(panel_cards)}</div>
      </div>

      <div class="card pad span-12">
        <h2>Create panel</h2>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="applications">
          <input type="hidden" name="action" value="create_panel">
          <div class="two">
            <div>
              <label>Name</label>
              <input name="name" maxlength="80" placeholder="Moderation team">
            </div>
            <div>
              <label>Description</label>
              <input name="description" maxlength="100" placeholder="Apply for the moderation team">
            </div>
          </div>
          <div class="button-row"><button class="primary" type="submit">Create panel</button></div>
        </form>
      </div>"""

    session_user = session.get("user", {}) if isinstance(session.get("user"), dict) else {}
    dashboard_user_id = str(session_user.get("id") or "")
    giveaway_creator_roles = searchable_multi_role_select(
        guild,
        "creator_role_ids",
        giveaway_settings.get("creator_role_ids", []),
        select_id="giveaway-creator-roles",
        placeholder="Search creator roles...",
    )
    giveaway_manager_roles = searchable_multi_role_select(
        guild,
        "manager_role_ids",
        giveaway_settings.get("manager_role_ids", []),
        select_id="giveaway-manager-roles",
        placeholder="Search manager roles...",
    )
    giveaway_required_role = searchable_role_select(
        guild,
        "required_role_id",
        giveaway_default("required_role_id"),
        select_id="giveaway-required-role",
        placeholder="Search required role...",
    )
    giveaway_bypass_role = searchable_role_select(
        guild,
        "requirement_bypass_role_id",
        giveaway_default("requirement_bypass_role_id"),
        select_id="giveaway-bypass-role",
        placeholder="Search bypass role...",
    )
    giveaway_blacklist_role = searchable_role_select(
        guild,
        "blacklist_role_id",
        giveaway_default("blacklist_role_id"),
        select_id="giveaway-blacklist-role",
        placeholder="Search blacklisted role...",
    )
    giveaway_winner_role = searchable_role_select(
        guild,
        "winner_role_id",
        giveaway_default("winner_role_id"),
        select_id="giveaway-winner-role",
        placeholder="Search winner role...",
    )
    default_required_role = searchable_role_select(
        guild,
        "required_role_id",
        giveaway_default("required_role_id"),
        select_id="giveaway-default-required-role",
        placeholder="Search default required role...",
    )
    default_bypass_role = searchable_role_select(
        guild,
        "requirement_bypass_role_id",
        giveaway_default("requirement_bypass_role_id"),
        select_id="giveaway-default-bypass-role",
        placeholder="Search default bypass role...",
    )
    default_blacklist_role = searchable_role_select(
        guild,
        "blacklist_role_id",
        giveaway_default("blacklist_role_id"),
        select_id="giveaway-default-blacklist-role",
        placeholder="Search default blacklisted role...",
    )
    default_winner_role = searchable_role_select(
        guild,
        "winner_role_id",
        giveaway_default("winner_role_id"),
        select_id="giveaway-default-winner-role",
        placeholder="Search default winner role...",
    )

    giveaway_section = f"""
      <div class="card pad span-6">
        <div class="section-title"><h2>Giveaway access</h2><span class="pill">{active_count} active / {ended_count} ended</span></div>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="giveaways">
          <input type="hidden" name="action" value="save_giveaway_roles">
          <label>Creator roles</label>
          {giveaway_creator_roles}
          <label>Manager roles</label>
          {giveaway_manager_roles}
          <p class="muted">Users with Manage Server always have giveaway access. Hold Ctrl while clicking to select multiple roles.</p>
          <div class="button-row"><button class="primary" type="submit">Save giveaway roles</button></div>
        </form>
      </div>

      <div class="card pad span-6">
        <h2>Recent giveaways</h2>
        <p class="muted">The auto-end worker checks active giveaways regularly. You can still use Discord commands for edits and rerolls.</p>
        <p><code>/giveaway create</code> <code>/giveaway edit</code> <code>/giveaway participants</code></p>
        <p><code>/giveaway remove-participant</code> <code>/giveaway end</code> <code>/giveaway reroll</code></p>
        <table class="mini-table">
          <thead><tr><th>ID</th><th>Prize</th><th>Status</th></tr></thead>
          <tbody>{giveaway_rows}</tbody>
        </table>
      </div>

      <div class="card pad span-12">
        <div class="section-title"><h2>Default giveaway settings</h2><span class="pill">Saved per server</span></div>
        <p class="module-note">These values pre-fill new website giveaways and slash-command giveaways when the same option is left empty.</p>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="giveaways">
          <input type="hidden" name="action" value="save_giveaway_defaults">
          <div class="two">
            <div>
              <label>Default required role</label>
              {default_required_role}
            </div>
            <div>
              <label>Default requirements bypass role</label>
              {default_bypass_role}
            </div>
          </div>
          <div class="two">
            <div>
              <label>Default blacklisted role</label>
              {default_blacklist_role}
            </div>
            <div>
              <label>Default winner role</label>
              {default_winner_role}
            </div>
          </div>
          <div class="two">
            <div>
              <label>Default messages today</label>
              <input name="required_daily_messages" type="number" min="0" value="{giveaway_default("required_daily_messages")}">
            </div>
            <div>
              <label>Default messages this week</label>
              <input name="required_weekly_messages" type="number" min="0" value="{giveaway_default("required_weekly_messages")}">
            </div>
          </div>
          <div class="two">
            <div>
              <label>Default messages this month</label>
              <input name="required_monthly_messages" type="number" min="0" value="{giveaway_default("required_monthly_messages")}">
            </div>
            <div>
              <label>Default total messages</label>
              <input name="required_total_messages" type="number" min="0" value="{giveaway_default("required_total_messages")}">
            </div>
          </div>
          <label>Default extra entries</label>
          <textarea name="extra_entries" maxlength="1000" placeholder="@Booster:2&#10;Donator:4&#10;Role ID:5">{esc(default_extra_entries)}</textarea>
          <p class="muted">Example: <code>@Booster:2</code> gives boosters 2 entries instead of 1.</p>
          <div class="button-row"><button class="primary" type="submit">Save default settings</button></div>
        </form>
      </div>

      <div class="card pad span-12">
        <div class="section-title"><h2>Create giveaway</h2><span class="pill">Website host</span></div>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="giveaways">
          <input type="hidden" name="action" value="create_giveaway">
          <input type="hidden" name="dashboard_user_id" value="{esc(dashboard_user_id)}">
          <div class="two">
            <div>
              <label>Channel</label>
              <select name="channel_id">{channel_options(guild)}</select>
            </div>
            <div>
              <label>Prize</label>
              <input name="prize" maxlength="180" placeholder="Discord Nitro, gems, custom prize..." required>
            </div>
          </div>
          <div class="two">
            <div>
              <label>Duration</label>
              <input name="duration" maxlength="24" placeholder="10m, 2h, 3d, 1w" required>
            </div>
            <div>
              <label>Winners</label>
              <input name="winners" type="number" min="1" max="20" value="1">
            </div>
          </div>
          <label>Image URL, optional</label>
          <input name="image_url" maxlength="500" placeholder="https://...png / jpg / gif / webp">
          <div class="two">
            <div>
              <label>Required role</label>
              {giveaway_required_role}
            </div>
            <div>
              <label>Requirements bypass role</label>
              {giveaway_bypass_role}
            </div>
          </div>
          <div class="two">
            <div>
              <label>Blacklisted role</label>
              {giveaway_blacklist_role}
            </div>
            <div>
              <label>Winner role</label>
              {giveaway_winner_role}
            </div>
          </div>
          <div class="two">
            <div>
              <label>Messages today</label>
              <input name="required_daily_messages" type="number" min="0" value="{giveaway_default("required_daily_messages")}">
            </div>
            <div>
              <label>Messages this week</label>
              <input name="required_weekly_messages" type="number" min="0" value="{giveaway_default("required_weekly_messages")}">
            </div>
          </div>
          <div class="two">
            <div>
              <label>Messages this month</label>
              <input name="required_monthly_messages" type="number" min="0" value="{giveaway_default("required_monthly_messages")}">
            </div>
            <div>
              <label>Total messages</label>
              <input name="required_total_messages" type="number" min="0" value="{giveaway_default("required_total_messages")}">
            </div>
          </div>
          <label>Extra entries, optional</label>
          <textarea name="extra_entries" maxlength="1000" placeholder="@Booster:2&#10;Donator:4&#10;Role ID:5">{esc(default_extra_entries)}</textarea>
          <div class="button-row"><button class="primary" type="submit">Create giveaway</button></div>
        </form>
      </div>"""

    suggestion_settings = support_bot.get_suggestion_settings(guild_id) if support_bot else {
        "channel_id": 0,
        "submit_channel_id": 0,
        "move_channel_id": 0,
        "anonymous": False,
        "dm_results": True,
    }
    suggestions = []
    if support_bot:
        suggestions = [
            suggestion
            for suggestion in support_bot.load_suggestions().values()
            if int(suggestion.get("guild_id") or 0) == guild_id
        ]
    suggestion_rows = "".join(
        (
            f"<tr><td>#{int(suggestion.get('number') or 0)}</td>"
            f"<td>{esc(str(suggestion.get('content') or '')[:100])}</td>"
            f"<td>{esc(suggestion.get('status') or 'pending')}</td>"
            f"<td>{len(support_bot.normalize_id_list(suggestion.get('upvoter_ids'))) if support_bot else 0} / "
            f"{len(support_bot.normalize_id_list(suggestion.get('downvoter_ids'))) if support_bot else 0}</td></tr>"
        )
        for suggestion in sorted(suggestions, key=lambda item: int(item.get("created_at") or 0), reverse=True)[:10]
    ) or '<tr><td colspan="4" class="muted">No suggestions stored for this server yet.</td></tr>'

    suggestion_section = f"""
      <div class="card pad span-6">
        <h2>Suggestion channels</h2>
        <p class="module-note">Users can submit with <code>/suggestion suggest</code>. You can also set a submit-only channel if you want suggestions typed in one place and posted in another.</p>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="suggestions">
          <input type="hidden" name="action" value="save_suggestion_settings">
          <label>Public suggestion channel</label>
          <select name="suggestion_channel_id">{channel_options(guild, suggestion_settings.get("channel_id"))}</select>
          <label>Submit channel, optional</label>
          <select name="suggestion_submit_channel_id">{channel_options(guild, suggestion_settings.get("submit_channel_id"))}</select>
          <label>Moderator result copy channel, optional</label>
          <select name="suggestion_move_channel_id">{channel_options(guild, suggestion_settings.get("move_channel_id"))}</select>
          <div class="two">
            <label><input style="width:auto; margin-right: 8px;" type="checkbox" name="anonymous"{checked(bool(suggestion_settings.get("anonymous")))}> Anonymous suggestions</label>
            <label><input style="width:auto; margin-right: 8px;" type="checkbox" name="dm_results"{checked(bool(suggestion_settings.get("dm_results", True)))}> DM users on decisions</label>
          </div>
          <div class="button-row"><button class="primary" type="submit">Save suggestion settings</button></div>
        </form>
      </div>
      <div class="card pad span-6">
        <h2>Suggestion commands</h2>
        <p class="module-note">Moderators can approve, consider, deny, implement, edit, move, and inspect suggestions from Discord.</p>
        <p><code>/suggestion suggest</code> <code>/suggestion approve</code> <code>/suggestion deny</code></p>
        <p><code>/suggestion implemented</code> <code>/suggestion edit</code> <code>/suggestion who</code></p>
        <table class="mini-table">
          <thead><tr><th>No.</th><th>Content</th><th>Status</th><th>Votes</th></tr></thead>
          <tbody>{suggestion_rows}</tbody>
        </table>
      </div>"""

    rr_state = support_bot.get_reaction_role_guild_state(guild_id) if support_bot else {"panels": {}}
    rr_panels = rr_state.get("panels", {}) if isinstance(rr_state, dict) else {}
    rr_cards = []
    for panel_id, panel in sorted(rr_panels.items(), key=lambda item: str(item[1].get("name", item[0])).lower()):
        items = panel.get("items", [])
        button_preview = []
        item_rows = []
        for item in items:
            role_id = int(item.get("role_id") or 0)
            button_text = f"{item.get('emoji') or ''} {item.get('label') or role_name(guild, role_id)}".strip()
            button_preview.append(f'<span class="rr-button-pill">{esc(button_text)}</span>')
            item_rows.append(
                f"""
                <div class="subtle-card">
                  <b>{esc(button_text)}</b>
                  <p class="muted" style="margin: 6px 0 0;">Gives @{esc(role_name(guild, role_id))}</p>
                  <form method="post" action="/applications?guild_id={guild_id}" class="button-row">
                    <input type="hidden" name="tab" value="reaction-roles">
                    <input type="hidden" name="action" value="remove_reaction_role_item">
                    <input type="hidden" name="panel_id" value="{esc(panel_id)}">
                    <input type="hidden" name="role_id" value="{role_id}">
                    <button class="danger" type="submit">Remove button</button>
                  </form>
                </div>"""
            )
        items_html = "".join(item_rows) or '<p class="muted">No role buttons yet.</p>'
        preview_buttons_html = "".join(button_preview) or '<span class="muted">Add buttons below.</span>'
        rr_cards.append(
            f"""
            <article class="panel-item">
              <div class="rr-panel-preview">
                <h3>{esc(panel.get("title") or panel.get("name") or "PING ROLES")}</h3>
                <p>{esc(panel.get("description") or "Clicking a button will get you the role indicated on the button and clicking the same button again will remove the role indicated on the button.")}</p>
              </div>
              <div class="rr-buttons">{preview_buttons_html}</div>
              <form method="post" action="/applications?guild_id={guild_id}">
                <input type="hidden" name="tab" value="reaction-roles">
                <input type="hidden" name="action" value="update_reaction_role_panel">
                <input type="hidden" name="panel_id" value="{esc(panel_id)}">
                <div class="two">
                  <div>
                    <label>Title</label>
                    <input name="title" maxlength="180" value="{esc(panel.get("title") or panel.get("name") or "PING ROLES")}">
                  </div>
                  <div>
                    <label>Channel</label>
                    <select name="channel_id">{channel_options(guild, panel.get("channel_id"))}</select>
                  </div>
                </div>
                <input type="hidden" name="name" value="{esc(panel.get("name") or panel_id)}">
                <input type="hidden" name="allow_multiple" value="on">
                <label>Text</label>
                <textarea name="description" maxlength="1800">{esc(panel.get("description") or "Clicking a button will get you the role indicated on the button and clicking the same button again will remove the role indicated on the button.")}</textarea>
                <div class="button-row">
                  <button class="primary" type="submit">Save</button>
                  <button type="submit" name="action" value="post_reaction_role_panel">Post or refresh</button>
                  <button class="danger" type="submit" name="action" value="delete_reaction_role_panel">Delete</button>
                </div>
              </form>
              <h4>Buttons</h4>
              {items_html}
            </article>"""
        )
    rr_cards_html = "".join(rr_cards) or '<div class="notice">No reaction-role panel yet. Create one, add buttons, then post it.</div>'

    rr_panel_select = reaction_panel_options(rr_panels)
    reaction_role_section = f"""
      <div class="card pad span-6">
        <h2>Reaction role panel</h2>
        <p class="module-note">Simple setup: choose the channel, write the title, write the text, then add blue role buttons.</p>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="reaction-roles">
          <input type="hidden" name="action" value="create_reaction_role_panel">
          <input type="hidden" name="name" value="Ping Roles">
          <input type="hidden" name="allow_multiple" value="on">
          <label>Channel</label>
          <select name="channel_id">{channel_options(guild)}</select>
          <label>Title</label>
          <input name="title" maxlength="180" value="PING ROLES">
          <label>Text</label>
          <textarea name="description" maxlength="1800">Clicking a button will get you the role indicated on the button and clicking the same button again will remove the role indicated on the button.</textarea>
          <div class="button-row"><button class="primary" type="submit">Create</button></div>
        </form>
      </div>
      <div class="card pad span-6">
        <h2>Add button</h2>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="tab" value="reaction-roles">
          <input type="hidden" name="action" value="add_reaction_role_item">
          <input type="hidden" name="style" value="primary">
          <label>Panel</label>
          <select name="panel_id">{rr_panel_select}</select>
          <div class="two">
            <div>
              <label>Role</label>
              {searchable_role_select(guild, "role_id", "", select_id="reaction-role-button-role", placeholder="Search button role...")}
            </div>
            <div>
              <label>Emoji</label>
              <input id="reaction-role-emoji" name="emoji" maxlength="32" placeholder="\U0001f381">
              {emoji_picker("reaction-role-emoji", replace=True, guild=guild)}
            </div>
          </div>
          <label>Button text</label>
          <input name="label" maxlength="80" placeholder="Giveaway Ping">
          <div class="button-row"><button class="primary" type="submit">Add button</button></div>
        </form>
      </div>
      <div class="card pad span-12">
        <div class="section-title"><h2>Your panels</h2><span class="pill">{len(rr_panels)} panel(s)</span></div>
        <div class="panel-list">{rr_cards_html}</div>
      </div>"""

    active_section = {
        "overview": overview_section,
        "applications": application_section,
        "submissions": submissions_section,
        "giveaways": giveaway_section,
        "suggestions": suggestion_section,
        "reaction-roles": reaction_role_section,
        "welcome": welcome_section,
    }[tab]

    body = f"""
    {render_flash(query)}
    <div class="button-row" style="margin-bottom: 16px;"><a class="button" href="/applications">Back to servers</a></div>
    <section class="grid">
      <div class="card pad span-12">
        <div class="section-title">
          <div>
            <span class="pill">Configuring</span>
            <h1 style="font-size: clamp(34px, 5vw, 58px); margin-top: 10px;">{esc(guild.name)}</h1>
            <p class="muted">Choose a module below. Changes are saved only for this server.</p>
          </div>
          <span class="server-icon">{guild_icon(guild, guild.name)}</span>
        </div>
        {dashboard_tab_nav(guild_id, tab)}
      </div>
      {active_section}
    </section>"""
    return base_layout("Dashboard", body, session=session, active="dashboard")


def redirect_to_dashboard(guild_id: Optional[int], *, tab: str = "", ok: str = "", error: str = "") -> str:
    query: dict[str, str] = {}
    if guild_id:
        query["guild_id"] = str(guild_id)
    if tab:
        query["tab"] = tab
    if ok:
        query["ok"] = ok
    if error:
        query["error"] = error
    suffix = f"?{urlencode(query)}" if query else ""
    return f"/applications{suffix}"


class GemToolSiteHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        clean_path = unquote(path.split("?", 1)[0].split("#", 1)[0]).lstrip("/")
        if not clean_path:
            clean_path = "index.html"
        candidate = (ROOT / clean_path).resolve()
        if ROOT not in candidate.parents and candidate != ROOT:
            return str(ROOT / "index.html")
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.exists() and candidate.suffix == "":
            candidate = ROOT / "index.html"
        return str(candidate)

    def do_GET(self) -> None:
        clean_path, query = self._path_and_query()
        if clean_path in {"/status", "/api/status", "/health"}:
            self._send_json(status_payload())
            return
        if clean_path in {"/terms", "/terms-of-service"}:
            self._send_html(render_policy_page("terms"))
            return
        if clean_path in {"/privacy", "/privacy-policy"}:
            self._send_html(render_policy_page("privacy"))
            return
        if clean_path == "/applications":
            self._handle_applications(query)
            return
        if clean_path == "/applications/login":
            self._handle_login()
            return
        if clean_path == "/applications/callback":
            self._handle_callback(query)
            return
        if clean_path == "/applications/logout":
            self._handle_logout()
            return
        super().do_GET()

    def do_POST(self) -> None:
        clean_path, _query = self._path_and_query()
        if clean_path == "/applications/owner-login":
            self._handle_owner_login()
            return
        if clean_path == "/applications":
            self._handle_applications_post()
            return
        self.send_error(404)

    def _path_and_query(self) -> tuple[str, dict[str, list[str]]]:
        path, _, raw_query = self.path.partition("?")
        return path.split("#", 1)[0], parse_qs(raw_query, keep_blank_values=True)

    def _cookies(self) -> SimpleCookie:
        return SimpleCookie(self.headers.get("Cookie", ""))

    def _current_session(self) -> Optional[dict[str, Any]]:
        prune_sessions()
        cookie = self._cookies().get(SESSION_COOKIE)
        if not cookie:
            return None
        session_id = verify_signed_value(cookie.value)
        if not session_id:
            return None
        return SESSIONS.get(session_id)

    def _read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        return parse_qs(body, keep_blank_values=True)

    def _handle_applications(self, query: dict[str, list[str]]) -> None:
        session = self._current_session()
        if not session:
            self._send_html(render_login(None, query))
            return
        guild_id = form_int(query, "guild_id")
        if guild_id is not None:
            if not self._session_can_manage(session, guild_id):
                self._send_html(render_server_selection(session, {"error": ["You cannot manage that server."]}))
                return
            self._send_html(render_guild_dashboard(session, guild_id, query))
            return
        self._send_html(render_server_selection(session, query))

    def _handle_login(self) -> None:
        client_id = effective_client_id()
        if not dashboard_ready():
            self._send_redirect("/applications?error=" + quote("Discord login is not configured yet."))
            return
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": client_id,
            "redirect_uri": oauth_redirect_uri(),
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
        url = f"https://discord.com/oauth2/authorize?{urlencode(params)}"
        self._send_redirect(url, cookies=[make_cookie(STATE_COOKIE, sign_value(state), max_age=600)])

    def _handle_callback(self, query: dict[str, list[str]]) -> None:
        code = form_one(query, "code")
        state = form_one(query, "state")
        signed_state = self._cookies().get(STATE_COOKIE)
        expected_state = verify_signed_value(signed_state.value) if signed_state else None
        client_id = effective_client_id()
        if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
            self._send_redirect(
                "/applications?error=" + quote("Discord login failed because the session state did not match."),
                cookies=[expire_cookie(STATE_COOKIE)],
            )
            return
        try:
            token_payload = discord_request_json(
                f"{DISCORD_API}/oauth2/token",
                method="POST",
                form={
                    "client_id": client_id,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": oauth_redirect_uri(),
                },
            )
            access_token = str(token_payload.get("access_token") or "")
            user = discord_request_json(f"{DISCORD_API}/users/@me", token=access_token)
            guilds = discord_request_json(f"{DISCORD_API}/users/@me/guilds", token=access_token)
            if not isinstance(guilds, list):
                guilds = []
        except Exception as exc:
            self._send_redirect(
                "/applications?error=" + quote(f"Discord login failed. {exc}"),
                cookies=[expire_cookie(STATE_COOKIE)],
            )
            return

        session_id = create_session(user, guilds)
        self._send_redirect(
            "/applications",
            cookies=[make_cookie(SESSION_COOKIE, sign_value(session_id)), expire_cookie(STATE_COOKIE)],
        )

    def _handle_owner_login(self) -> None:
        form = self._read_form()
        token = form_one(form, "token")
        if not owner_token_matches(token):
            self._send_redirect("/applications?error=" + quote("Owner login failed. Check DASHBOARD_OWNER_TOKEN."))
            return
        ok, session_id, error = create_owner_session_from_bot()
        if not ok:
            self._send_redirect("/applications?error=" + quote(error))
            return
        self._send_redirect("/applications", cookies=[make_cookie(SESSION_COOKIE, sign_value(session_id))])

    def _handle_logout(self) -> None:
        cookie = self._cookies().get(SESSION_COOKIE)
        if cookie:
            session_id = verify_signed_value(cookie.value)
            if session_id:
                SESSIONS.pop(session_id, None)
        self._send_redirect("/applications", cookies=[expire_cookie(SESSION_COOKIE)])

    def _session_can_manage(self, session: dict[str, Any], guild_id: int) -> bool:
        return str(guild_id) in session.get("manageable_guilds", {}) and str(guild_id) in bot_guilds()

    def _handle_applications_post(self) -> None:
        session = self._current_session()
        if not session:
            self._send_redirect("/applications?error=" + quote("Please log in first."))
            return
        form = self._read_form()
        guild_id = form_int(form, "guild_id") or form_int(parse_qs(self.path.partition("?")[2]), "guild_id")
        guild_id = guild_id or form_int({"guild_id": [form_one(form, "guild_id")]}, "guild_id")
        if guild_id is None:
            ref = self.headers.get("Referer", "")
            parsed_ref = parse_qs(ref.partition("?")[2])
            guild_id = form_int(parsed_ref, "guild_id")
        if guild_id is None or not self._session_can_manage(session, guild_id):
            self._send_redirect("/applications?error=" + quote("Choose a server you can manage."))
            return

        action = form_one(form, "action")
        tab = form_one(form, "tab", "overview").lower().replace("_", "-")
        if tab not in {"overview", "applications", "submissions", "giveaways", "suggestions", "reaction-roles", "welcome"}:
            tab = "overview"
        try:
            ok_message = self._apply_dashboard_action(guild_id, action, form)
            self._send_redirect(redirect_to_dashboard(guild_id, tab=tab, ok=ok_message))
        except ValueError as exc:
            self._send_redirect(redirect_to_dashboard(guild_id, tab=tab, error=str(exc)))
        except Exception as exc:
            self._send_redirect(redirect_to_dashboard(guild_id, tab=tab, error=f"{type(exc).__name__}: {exc}"))

    def _apply_dashboard_action(self, guild_id: int, action: str, form: dict[str, list[str]]) -> str:
        guild = bot_guilds().get(str(guild_id))
        if not guild:
            raise ValueError("Gem Tool is not connected to that server.")

        guild_state = application_system.get_guild_state(guild_id)
        panels = guild_state.setdefault("panels", {})

        if action == "save_text":
            guild_state["panel_text"] = form_one(form, "panel_text", application_system.DEFAULT_PANEL_TEXT)[:1000]
            application_system.save_state()
            run_bot_coro(application_system.refresh_application_message(guild))
            return "Application text saved."

        if action == "save_channels":
            app_channel_id = form_int(form, "application_channel_id")
            log_channel_id = form_int(form, "log_channel_id")
            guild_state["application_channel_id"] = app_channel_id or 0
            guild_state["log_channel_id"] = log_channel_id or 0
            application_system.save_state()
            return "Channels saved."

        if action == "post_panel":
            app_channel_id = form_int(form, "application_channel_id") or int(guild_state.get("application_channel_id") or 0)
            if not app_channel_id:
                raise ValueError("Choose an application panel channel first.")
            log_channel_id = form_int(form, "log_channel_id")
            if log_channel_id is not None:
                guild_state["log_channel_id"] = log_channel_id
                application_system.save_state()
            ok, result = run_bot_coro(application_system.post_application_panel(guild, app_channel_id))
            if not ok:
                raise ValueError(str(result))
            posted, message = result
            if not posted:
                raise ValueError(message)
            return message

        if action == "create_panel":
            name = form_one(form, "name")
            description = form_one(form, "description")
            panel_key = application_system.normalize_panel_key(name)
            if not panel_key:
                raise ValueError("Panel name cannot be empty.")
            if panel_key in panels:
                raise ValueError("That panel already exists.")
            panels[panel_key] = {"name": name[:80], "description": description[:100], "questions": [], "enabled": True}
            application_system.save_state()
            run_bot_coro(application_system.refresh_application_message(guild))
            return f"Panel {panel_key} created."

        if action in {"update_panel", "delete_panel"}:
            panel_key = application_system.normalize_panel_key(form_one(form, "panel_key"))
            if panel_key not in panels:
                raise ValueError("Panel not found.")
            if action == "delete_panel":
                panels.pop(panel_key, None)
                application_system.save_state()
                run_bot_coro(application_system.refresh_application_message(guild))
                return f"Panel {panel_key} deleted."
            panel = panels[panel_key]
            panel["name"] = form_one(form, "name", panel.get("name", panel_key))[:80]
            panel["description"] = form_one(form, "description", panel.get("description", ""))[:100]
            panel["enabled"] = "enabled" in form
            accepted_role_id = form_int(form, "accepted_role_id")
            if accepted_role_id:
                role = guild.get_role(accepted_role_id)
                if not role:
                    raise ValueError("Accepted role was not found.")
                allowed, reason = application_system.bot_can_manage_role(guild, role)
                if not allowed:
                    raise ValueError(f"I cannot give that role: {reason}.")
                panel["accepted_role_id"] = accepted_role_id
            else:
                panel.pop("accepted_role_id", None)
            application_system.save_state()
            run_bot_coro(application_system.refresh_application_message(guild))
            return f"Panel {panel_key} saved."

        if action in {"add_question", "update_question", "delete_question"}:
            panel_key = application_system.normalize_panel_key(form_one(form, "panel_key"))
            if panel_key not in panels:
                raise ValueError("Panel not found.")
            questions = panels[panel_key].setdefault("questions", [])
            if action == "add_question":
                text = form_one(form, "text")
                if not text:
                    raise ValueError("Question text cannot be empty.")
                choices = form_one(form, "choices")
                if choices and application_system.parse_question_choices(choices) is None:
                    raise ValueError("Dropdown questions need at least two choices, like yes|no.")
                insert_at = max(0, min(len(questions), (form_int(form, "question_number") or len(questions) + 1) - 1))
                questions.insert(insert_at, application_system.make_question_value(text, choices))
                application_system.save_state()
                return "Question added."
            question_number = form_int(form, "question_number")
            if not question_number or question_number < 1 or question_number > len(questions):
                raise ValueError("Question number does not exist.")
            index = question_number - 1
            if action == "delete_question":
                questions.pop(index)
                application_system.save_state()
                return "Question deleted and questions renumbered."
            text = form_one(form, "text")
            if not text:
                raise ValueError("Question text cannot be empty.")
            choices = form_one(form, "choices")
            if choices and application_system.parse_question_choices(choices) is None:
                raise ValueError("Dropdown questions need at least two choices, like yes|no.")
            questions[index] = application_system.make_question_value(text, choices, questions[index])
            application_system.save_state()
            return "Question saved."

        if action == "save_giveaway_roles":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Giveaway bot is not loaded.")
            settings = support_bot.load_giveaway_settings()
            guild_settings = support_bot.get_giveaway_settings(guild_id)
            guild_settings["creator_role_ids"] = sorted({int(value) for value in form.get("creator_role_ids", []) if value.isdigit()})
            guild_settings["manager_role_ids"] = sorted({int(value) for value in form.get("manager_role_ids", []) if value.isdigit()})
            settings.setdefault("guilds", {})[str(guild_id)] = guild_settings
            support_bot.save_giveaway_settings(settings)
            return "Giveaway role settings saved."

        if action == "save_giveaway_defaults":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Giveaway bot is not loaded.")
            parsed_extra = support_bot.parse_role_entry_mapping(form_one(form, "extra_entries"), guild)
            if form_one(form, "extra_entries") and not parsed_extra:
                raise ValueError("Extra entries must use @Role:2, Role Name:2, or role_id:2.")
            settings = support_bot.load_giveaway_settings()
            guild_settings = support_bot.get_giveaway_settings(guild_id)
            guild_settings["defaults"] = support_bot.normalize_giveaway_defaults(
                {
                    "required_role_id": form_int(form, "required_role_id") or 0,
                    "requirement_bypass_role_id": form_int(form, "requirement_bypass_role_id") or 0,
                    "blacklist_role_id": form_int(form, "blacklist_role_id") or 0,
                    "winner_role_id": form_int(form, "winner_role_id") or 0,
                    "required_daily_messages": form_count(form, "required_daily_messages"),
                    "required_weekly_messages": form_count(form, "required_weekly_messages"),
                    "required_monthly_messages": form_count(form, "required_monthly_messages"),
                    "required_total_messages": form_count(form, "required_total_messages", maximum=10_000_000),
                    "extra_entries": parsed_extra,
                }
            )
            settings.setdefault("guilds", {})[str(guild_id)] = guild_settings
            support_bot.save_giveaway_settings(settings)
            return "Default giveaway settings saved."

        if action == "create_giveaway":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Giveaway bot is not loaded.")
            ok, result = run_bot_coro(
                support_bot.create_giveaway_from_dashboard(
                    guild_id=guild_id,
                    channel_id=form_int(form, "channel_id") or 0,
                    host_id=form_int(form, "dashboard_user_id") or 0,
                    duration=form_one(form, "duration"),
                    winners=form_int(form, "winners") or 1,
                    prize=form_one(form, "prize"),
                    image_url=form_one(form, "image_url"),
                    required_role_id=form_int(form, "required_role_id") or 0,
                    requirement_bypass_role_id=form_int(form, "requirement_bypass_role_id") or 0,
                    blacklist_role_id=form_int(form, "blacklist_role_id") or 0,
                    required_daily_messages=form_count(form, "required_daily_messages"),
                    required_weekly_messages=form_count(form, "required_weekly_messages"),
                    required_monthly_messages=form_count(form, "required_monthly_messages"),
                    required_total_messages=form_count(form, "required_total_messages", maximum=10_000_000),
                    winner_role_id=form_int(form, "winner_role_id") or 0,
                    extra_entries=form_one(form, "extra_entries"),
                )
            )
            if not ok:
                raise ValueError(str(result))
            created, message = result
            if not created:
                raise ValueError(message)
            return message

        if action == "save_welcome_settings":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Welcomer module is not loaded.")
            current = support_bot.get_welcome_settings(guild_id)
            section = form_one(form, "save_section", "welcome")
            channel_id = (
                int(form_one(form, "welcome_channel_id") or 0)
                if section == "welcome"
                else int(current.get("channel_id") or 0)
            )
            leave_channel_id = (
                int(form_one(form, "leave_channel_id") or 0)
                if section == "leave"
                else int(current.get("leave_channel_id") or 0)
            )
            welcome_enabled = "enabled" in form if section == "welcome" else bool(current.get("enabled"))
            leave_enabled = "leave_enabled" in form if section == "leave" else bool(current.get("leave_enabled"))
            if section == "welcome" and welcome_enabled and not channel_id:
                raise ValueError("Choose a welcome channel before enabling welcomer.")
            if section == "leave" and leave_enabled and not leave_channel_id:
                raise ValueError("Choose a leave channel before enabling leave messages.")
            support_bot.set_welcome_config(
                guild_id,
                enabled=welcome_enabled,
                channel_id=channel_id,
                rules_channel_id=(
                    int(form_one(form, "rules_channel_id") or 0)
                    if section == "welcome"
                    else int(current.get("rules_channel_id") or 0)
                ),
                message_template=(
                    form_one(form, "message_template", support_bot.DEFAULT_WELCOME_MESSAGE)
                    if section == "welcome"
                    else str(current.get("message_template") or support_bot.DEFAULT_WELCOME_MESSAGE)
                ),
                leave_enabled=leave_enabled,
                leave_channel_id=leave_channel_id,
                leave_message_template=(
                    form_one(form, "leave_message_template", support_bot.DEFAULT_LEAVE_MESSAGE)
                    if section == "leave"
                    else str(current.get("leave_message_template") or support_bot.DEFAULT_LEAVE_MESSAGE)
                ),
            )
            return "Welcome settings saved." if section == "welcome" else "Leave message settings saved."

        if action == "save_suggestion_settings":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Suggestion module is not loaded.")
            support_bot.set_suggestion_config(
                guild_id,
                channel_id=int(form_one(form, "suggestion_channel_id") or 0),
                submit_channel_id=int(form_one(form, "suggestion_submit_channel_id") or 0),
                move_channel_id=int(form_one(form, "suggestion_move_channel_id") or 0),
                anonymous="anonymous" in form,
                dm_results="dm_results" in form,
            )
            return "Suggestion settings saved."

        if action == "create_reaction_role_panel":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Reaction-role module is not loaded.")
            name = form_one(form, "name")
            if not name:
                raise ValueError("Panel name cannot be empty.")
            panel = support_bot.create_reaction_role_panel_record(
                guild_id,
                name=name,
                channel_id=int(form_one(form, "channel_id") or 0),
                title=form_one(form, "title"),
                description=form_one(form, "description"),
                allow_multiple="allow_multiple" in form,
            )
            return f"Reaction-role panel {panel['id']} created."

        if action in {"update_reaction_role_panel", "post_reaction_role_panel", "delete_reaction_role_panel"}:
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Reaction-role module is not loaded.")
            panel_id = form_one(form, "panel_id")
            if not panel_id:
                raise ValueError("Choose a reaction-role panel.")
            if action == "delete_reaction_role_panel":
                support_bot.delete_reaction_role_panel_record(guild_id, panel_id)
                return "Reaction-role panel deleted."
            if action == "post_reaction_role_panel":
                ok, result = run_bot_coro(support_bot.post_reaction_role_panel(guild, panel_id))
                if not ok:
                    raise ValueError(str(result))
                posted, message = result
                if not posted:
                    raise ValueError(message)
                return message
            panel = support_bot.update_reaction_role_panel_record(
                guild_id,
                panel_id,
                name=form_one(form, "name"),
                channel_id=int(form_one(form, "channel_id") or 0),
                title=form_one(form, "title"),
                description=form_one(form, "description"),
                allow_multiple="allow_multiple" in form,
            )
            if "allowed_role_ids" in form or "ignored_role_ids" in form:
                support_bot.set_reaction_role_access_lists(
                    guild_id,
                    panel_id,
                    allowed_role_ids=[int(value) for value in form.get("allowed_role_ids", []) if value.isdigit()],
                    ignored_role_ids=[int(value) for value in form.get("ignored_role_ids", []) if value.isdigit()],
                )
            run_bot_coro(support_bot.post_reaction_role_panel(guild, panel_id))
            return f"Reaction-role panel {panel.get('name') or panel_id} saved."

        if action == "add_reaction_role_item":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Reaction-role module is not loaded.")
            panel_id = form_one(form, "panel_id")
            role_id = int(form_one(form, "role_id") or 0)
            if not panel_id:
                raise ValueError("Choose a reaction-role panel.")
            role = guild.get_role(role_id)
            if not role:
                raise ValueError("Choose a valid role.")
            allowed, reason = application_system.bot_can_manage_role(guild, role)
            if not allowed:
                raise ValueError(f"I cannot manage that role: {reason}.")
            panel = support_bot.add_reaction_role_item(
                guild_id,
                panel_id,
                role_id=role.id,
                label=form_one(form, "label") or role.name,
                emoji=form_one(form, "emoji"),
                style=form_one(form, "style", "primary"),
            )
            run_bot_coro(support_bot.post_reaction_role_panel(guild, panel_id))
            return f"Added role button to {panel.get('name') or panel_id}."

        if action == "remove_reaction_role_item":
            support_bot = load_support_bot()
            if not support_bot:
                raise ValueError("Reaction-role module is not loaded.")
            panel_id = form_one(form, "panel_id")
            role_id = int(form_one(form, "role_id") or 0)
            if not panel_id or not role_id:
                raise ValueError("Missing reaction-role item.")
            support_bot.remove_reaction_role_item(guild_id, panel_id, role_id)
            run_bot_coro(support_bot.post_reaction_role_panel(guild, panel_id))
            return "Role button removed."

        raise ValueError("Unknown dashboard action.")

    def _send_html(self, body: str, status: int = 200, cookies: Optional[list[str]] = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, url: str, cookies: Optional[list[str]] = None) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


def main() -> None:
    port = int(os.getenv("PORT", "10000"))
    support_bot = load_support_bot()
    if support_bot and support_bot.TOKEN:
        threading.Thread(target=support_bot.run, daemon=True, name="gem-tool-discord-bot").start()
    else:
        print("No DISCORD_TOKEN set; website will run without the Gem Tool bot.", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), GemToolSiteHandler)
    print(f"Gem Tool website listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

