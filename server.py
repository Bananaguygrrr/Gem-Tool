from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
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

import application_system


ROOT = Path(__file__).resolve().parent
LAST_UPDATE = os.getenv("LAST_UPDATE", "").strip()
APP_NAME = os.getenv("APP_NAME", "Gem Tool").strip() or "Gem Tool"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://gemtool.bot").strip().rstrip("/")
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", "").strip()
DISCORD_CLIENT_ID = (
    os.getenv("DISCORD_CLIENT_ID")
    or os.getenv("CLIENT_ID")
    or os.getenv("APPLICATION_ID")
    or ""
).strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
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


def current_last_update() -> str:
    if LAST_UPDATE:
        return LAST_UPDATE
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def status_payload() -> dict[str, object]:
    support_bot = load_support_bot()
    support_health = support_bot.status_payload() if support_bot else {}
    online = bool(support_health.get("online"))
    invite_url = ""
    if DISCORD_CLIENT_ID:
        invite_url = (
            "https://discord.com/oauth2/authorize"
            f"?client_id={DISCORD_CLIENT_ID}&permissions=2147561408&scope=bot%20applications.commands"
        )
    payload = {
        "app_name": APP_NAME,
        "status": f"{APP_NAME} online" if online else f"{APP_NAME} offline",
        "online": online,
        "guild_count": str(support_health.get("guild_count") or 0),
        "last_update": current_last_update(),
        "invite_url": invite_url,
        "support_server_url": SUPPORT_SERVER_URL,
        "public_base_url": PUBLIC_BASE_URL,
        "support_bot_online": online,
        "support_bot_user": str(support_health.get("bot_user") or ""),
        "support_bot_guild_count": str(support_health.get("guild_count") or 0),
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if SUPPORT_BOT_IMPORT_ERROR:
        payload["support_bot_error"] = SUPPORT_BOT_IMPORT_ERROR
    return payload


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


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
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and PUBLIC_BASE_URL)


def bot_is_ready() -> bool:
    support_bot = load_support_bot()
    return bool(support_bot and support_bot.bot and support_bot.bot.is_ready())


def bot_guilds() -> dict[str, Any]:
    support_bot = load_support_bot()
    if not support_bot or not support_bot.bot:
        return {}
    return {str(guild.id): guild for guild in support_bot.bot.guilds}


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
    options = ['<option value="">None</option>'] if include_blank else []
    roles = [
        role
        for role in sorted(getattr(guild, "roles", []), key=lambda item: item.position, reverse=True)
        if not role.is_default() and not role.managed
    ]
    for role in roles:
        options.append(f'<option value="{role.id}"{selected(role.id, selected_id)}>{esc(role.name)}</option>')
    return "\n".join(options)


def multi_role_options(guild: Any, selected_ids: list[int]) -> str:
    selected_set = {str(role_id) for role_id in selected_ids}
    roles = [
        role
        for role in sorted(getattr(guild, "roles", []), key=lambda item: item.position, reverse=True)
        if not role.is_default() and not role.managed
    ]
    return "\n".join(
        f'<option value="{role.id}"{" selected" if str(role.id) in selected_set else ""}>{esc(role.name)}</option>'
        for role in roles
    )


def base_layout(title: str, body: str, *, session: Optional[dict[str, Any]] = None, active: str = "applications") -> str:
    user = session.get("user", {}) if session else {}
    avatar = user.get("avatar_url")
    user_label = esc(user.get("username") or "Login")
    login_button = (
        f'<a class="user-chip" href="/applications/logout">{f"<img src=\"{esc(avatar)}\" alt=\"\">" if avatar else "<span>D</span>"}'
        f"<strong>{user_label}</strong><em>Logout</em></a>"
        if session
        else '<a class="login-button" href="/applications/login"><span>D</span> Discord Login</a>'
    )
    nav = {
        "home": "/",
        "applications": "/applications",
        "support": SUPPORT_SERVER_URL or "/",
    }
    support_attrs = ' target="_blank" rel="noopener"' if SUPPORT_SERVER_URL else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - {esc(APP_NAME)}</title>
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
      width: 38px;
      height: 38px;
      display: grid;
      place-items: center;
      border-radius: 13px;
      color: #071014;
      background: linear-gradient(135deg, var(--aqua), var(--cyan), var(--violet));
      box-shadow: 0 14px 34px rgba(41,245,210,.18);
      font-weight: 1000;
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
    input:focus, textarea:focus, select:focus {{ border-color: var(--aqua); box-shadow: 0 0 0 3px rgba(41,245,210,.12); }}
    .button-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }}
    button, .button {{
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
    .question {{
      margin-top: 12px;
      padding: 12px;
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 13px;
      background: rgba(0,0,0,.16);
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
    code {{
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 7px;
      padding: 2px 6px;
      background: rgba(255,255,255,.08);
      color: var(--text);
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
    <a class="brand" href="/"><span class="logo">G</span><span>{esc(APP_NAME)}</span></a>
    <nav>
      <a class="{"active" if active == "home" else ""}" href="{nav["home"]}">Home</a>
      <a class="{"active" if active == "applications" else ""}" href="{nav["applications"]}">Applications</a>
      <a href="{esc(nav["support"])}"{support_attrs}>Support</a>
    </nav>
    {login_button}
  </header>
  <main>{body}</main>
</body>
</html>"""


def render_login(session: Optional[dict[str, Any]], query: dict[str, list[str]]) -> str:
    error = form_one(query, "error")
    setup = ""
    if not dashboard_ready():
        setup = f"""
        <div class="notice error">
          Discord login is not fully configured yet. Set <code>DISCORD_CLIENT_ID</code>,
          <code>DISCORD_CLIENT_SECRET</code>, and <code>PUBLIC_BASE_URL</code> in Render.
          Add this redirect URL in the Discord Developer Portal:
          <br><br><code>{esc(oauth_redirect_uri())}</code>
        </div>"""
    error_html = f'<div class="notice error">{esc(error)}</div>' if error else ""
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
        <p class="muted">No dashboard token is needed. Discord decides which servers you can manage.</p>
        {setup}
        {error_html}
        <div class="button-row">
          <a class="button primary" href="/applications/login"><span>D</span> Discord Login</a>
        </div>
      </aside>
    </section>"""
    return base_layout("Applications", body, session=session, active="applications")


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
              <span><b>{esc(guild.name)}</b><small>Configure applications and giveaways</small></span>
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
    return base_layout("Applications", body, session=session, active="applications")


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


def render_guild_dashboard(session: dict[str, Any], guild_id: int, query: dict[str, list[str]]) -> str:
    guild = bot_guilds().get(str(guild_id))
    if not guild:
        return render_server_selection(session, {"error": ["That server is not available. Is Gem Tool invited and online?"]})

    guild_state = application_system.get_guild_state(guild_id)
    panels = guild_state.setdefault("panels", {})
    panel_cards = []
    for panel_key, panel in sorted(panels.items()):
        questions_html = render_panel_questions(panel_key, panel, guild_id)
        accepted_role_id = panel.get("accepted_role_id") or ""
        panel_cards.append(
            f"""
            <article class="panel-item">
              <form method="post" action="/applications?guild_id={guild_id}">
                <input type="hidden" name="action" value="update_panel">
                <input type="hidden" name="panel_key" value="{esc(panel_key)}">
                <div class="two">
                  <div>
                    <label>Panel name</label>
                    <input name="name" maxlength="80" value="{esc(panel.get("name", panel_key))}">
                  </div>
                  <div>
                    <label>Accepted role</label>
                    <select name="accepted_role_id">{role_options(guild, accepted_role_id)}</select>
                  </div>
                </div>
                <label>Description</label>
                <input name="description" maxlength="100" value="{esc(panel.get("description", ""))}">
                <label><input style="width:auto; margin-right: 8px;" type="checkbox" name="enabled"{checked(panel.get("enabled", True) is not False)}> Show in dropdown</label>
                <div class="button-row">
                  <button class="primary" type="submit">Save panel</button>
                  <button class="danger" type="submit" name="action" value="delete_panel">Delete panel</button>
                </div>
              </form>
              <h4>Questions</h4>
              {questions_html}
              <form method="post" action="/applications?guild_id={guild_id}">
                <input type="hidden" name="action" value="add_question">
                <input type="hidden" name="panel_key" value="{esc(panel_key)}">
                <div class="two">
                  <div>
                    <label>Insert position</label>
                    <input name="question_number" type="number" min="1" value="{len(application_system.panel_questions(panel)) + 1}">
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
            </article>"""
        )

    if not panel_cards:
        panel_cards.append('<div class="notice">No panels yet. Create one below, then add questions.</div>')

    support_bot = load_support_bot()
    giveaway_settings = support_bot.get_giveaway_settings(guild_id) if support_bot else {"creator_role_ids": [], "manager_role_ids": []}
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

    body = f"""
    {render_flash(query)}
    <div class="button-row" style="margin-bottom: 16px;"><a class="button" href="/applications">Back to servers</a></div>
    <section class="grid">
      <div class="card pad span-12">
        <div class="section-title">
          <div>
            <span class="pill">Configuring</span>
            <h1 style="font-size: clamp(34px, 5vw, 58px); margin-top: 10px;">{esc(guild.name)}</h1>
            <p class="muted">Changes are saved only for this server.</p>
          </div>
          <span class="server-icon">{guild_icon(guild, guild.name)}</span>
        </div>
      </div>

      <div class="card pad span-5">
        <h2>Application channels</h2>
        <form method="post" action="/applications?guild_id={guild_id}">
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
      </div>

      <div class="card pad span-6">
        <div class="section-title"><h2>Giveaway access</h2><span class="pill">{active_count} active / {ended_count} ended</span></div>
        <form method="post" action="/applications?guild_id={guild_id}">
          <input type="hidden" name="action" value="save_giveaway_roles">
          <label>Creator roles</label>
          <select name="creator_role_ids" multiple>{multi_role_options(guild, giveaway_settings.get("creator_role_ids", []))}</select>
          <label>Manager roles</label>
          <select name="manager_role_ids" multiple>{multi_role_options(guild, giveaway_settings.get("manager_role_ids", []))}</select>
          <p class="muted">Users with Manage Server always have giveaway access. Hold Ctrl while clicking to select multiple roles.</p>
          <div class="button-row"><button class="primary" type="submit">Save giveaway roles</button></div>
        </form>
      </div>

      <div class="card pad span-6">
        <h2>Giveaway commands</h2>
        <p class="muted">Giveaway creation stays in Discord so uploaded images, roles, and channels work naturally.</p>
        <p><code>/giveaway create</code> <code>/giveaway edit</code> <code>/giveaway participants</code></p>
        <p><code>/giveaway remove-participant</code> <code>/giveaway end</code> <code>/giveaway reroll</code></p>
        <table class="mini-table">
          <thead><tr><th>ID</th><th>Prize</th><th>Status</th></tr></thead>
          <tbody>{giveaway_rows}</tbody>
        </table>
      </div>
    </section>"""
    return base_layout("Dashboard", body, session=session, active="applications")


def redirect_to_dashboard(guild_id: Optional[int], *, ok: str = "", error: str = "") -> str:
    query: dict[str, str] = {}
    if guild_id:
        query["guild_id"] = str(guild_id)
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
        if clean_path in {"/status", "/api/status"}:
            self._send_json(status_payload())
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
        if not dashboard_ready():
            self._send_redirect("/applications?error=" + quote("Discord login is not configured yet."))
            return
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": DISCORD_CLIENT_ID,
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
                    "client_id": DISCORD_CLIENT_ID,
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
        try:
            ok_message = self._apply_dashboard_action(guild_id, action, form)
            self._send_redirect(redirect_to_dashboard(guild_id, ok=ok_message))
        except ValueError as exc:
            self._send_redirect(redirect_to_dashboard(guild_id, error=str(exc)))
        except Exception as exc:
            self._send_redirect(redirect_to_dashboard(guild_id, error=f"{type(exc).__name__}: {exc}"))

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
