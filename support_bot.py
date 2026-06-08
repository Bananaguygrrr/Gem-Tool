from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import application_system


load_dotenv()

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

TOKEN = (
    os.getenv("DISCORD_TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("TOKEN")
    or ""
).strip()
DISCORD_CLIENT_ID = (
    os.getenv("DISCORD_CLIENT_ID")
    or os.getenv("CLIENT_ID")
    or os.getenv("APPLICATION_ID")
    or ""
).strip()
APP_NAME = os.getenv("APP_NAME", "Gem Tool").strip() or "Gem Tool"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://gemtool.bot").strip()
COMMAND_SYNC_MODE = os.getenv("COMMAND_SYNC_MODE", "global").strip().lower()
COMMAND_SYNC_GUILD_ID = os.getenv("COMMAND_SYNC_GUILD_ID", "").strip()

GIVEAWAY_EMOJI = os.getenv("GIVEAWAY_EMOJI", "\U0001f389").strip() or "\U0001f389"
DEFAULT_OWNER_ID = 1105451323584938075
SUGGESTION_UP_EMOJI = "\u2b06\ufe0f"
SUGGESTION_DOWN_EMOJI = "\u2b07\ufe0f"
GIVEAWAY_CHECK_INTERVAL_SECONDS = max(10, int(os.getenv("GIVEAWAY_CHECK_INTERVAL_SECONDS", "20")))
GIVEAWAY_MIN_DURATION_SECONDS = 60
GIVEAWAY_MAX_DURATION_SECONDS = 60 * 60 * 24 * 30
GIVEAWAY_PARTICIPANTS_PAGE_SIZE = 10
MESSAGE_STATS_SAVE_INTERVAL_SECONDS = max(1, int(os.getenv("MESSAGE_STATS_SAVE_INTERVAL_SECONDS", "15")))
APPLICATION_TIMEOUT_SECONDS = int(os.getenv("APPLICATION_TIMEOUT_SECONDS", "10800"))

GIVEAWAYS_FILE = DATA_DIR / "giveaways.json"
GIVEAWAY_SETTINGS_FILE = DATA_DIR / "giveaway_settings.json"
MESSAGE_STATS_FILE = DATA_DIR / "message_stats.json"
WELCOME_SETTINGS_FILE = DATA_DIR / "welcome_settings.json"
SUGGESTION_SETTINGS_FILE = DATA_DIR / "suggestion_settings.json"
SUGGESTIONS_FILE = DATA_DIR / "suggestions.json"
REACTION_ROLE_PANELS_FILE = DATA_DIR / "reaction_role_panels.json"
DEFAULT_WELCOME_MESSAGE = (
    "\U0001f44b | Welcome {user} to **{server}**, you are member **{member_count}**!\n\n"
    "\U0001f4d6 | Please look in {rules_channel} for the rules of the server."
)
DEFAULT_LEAVE_MESSAGE = (
    "\U0001f44b | **{username}** has left **{server}**.\n\n"
    "\U0001f465 | We now have **{member_count}** members."
)

NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
DIGIT_ID_RE = re.compile(r"(\d+)")
GIVEAWAY_ID_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
GIVEAWAY_DURATION_PART_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

BOT_STARTED_AT = int(time.time())
BOT_ONLINE = False
GIVEAWAYS_CACHE: Optional[dict[str, dict[str, Any]]] = None
GIVEAWAY_SETTINGS_CACHE: Optional[dict[str, Any]] = None
MESSAGE_STATS_CACHE: Optional[dict[str, Any]] = None
WELCOME_SETTINGS_CACHE: Optional[dict[str, Any]] = None
SUGGESTION_SETTINGS_CACHE: Optional[dict[str, Any]] = None
SUGGESTIONS_CACHE: Optional[dict[str, dict[str, Any]]] = None
REACTION_ROLE_PANELS_CACHE: Optional[dict[str, Any]] = None
MESSAGE_STATS_LAST_SAVE = 0.0
REGISTERED_GIVEAWAY_VIEW_IDS: set[str] = set()
REGISTERED_SUGGESTION_VIEW_IDS: set[str] = set()
REGISTERED_REACTION_ROLE_VIEW_IDS: set[str] = set()
APPLICATION_OWNER_IDS: set[int] = set()


def truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def parse_count(value: Any) -> Optional[int]:
    text = str(value or "").strip().lower().replace(",", "")
    if not text:
        return None
    multiplier = 1
    if text.endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def coerce_int(value: Any, default: int = 0, *, maximum: Optional[int] = None) -> int:
    parsed = parse_count(value)
    if parsed is None:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
    parsed = max(0, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def parse_user_id(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value if value > 0 else None
    match = DIGIT_ID_RE.search(str(value or ""))
    if not match:
        return None
    parsed = int(match.group(1))
    return parsed if parsed > 0 else None


def extract_last_discord_id(value: Any) -> Optional[int]:
    matches = re.findall(r"\d{15,25}", str(value or ""))
    if matches:
        parsed = int(matches[-1])
        return parsed if parsed > 0 else None
    return parse_user_id(value)


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except Exception as error:
        backup_path = path.with_suffix(path.suffix + ".bak")
        try:
            with backup_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            print(f"Could not load {path}: {error}")
            return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            backup_path = path.with_suffix(path.suffix + ".bak")
            backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as error:
            print(f"Could not refresh backup for {path}: {error}")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


async def refresh_application_owner_ids() -> set[int]:
    global APPLICATION_OWNER_IDS
    owner_ids: set[int] = set()
    try:
        app_info = await bot.application_info()
    except discord.HTTPException as error:
        print(f"Could not load Discord application owner: {error}", flush=True)
        return APPLICATION_OWNER_IDS

    owner = getattr(app_info, "owner", None)
    if owner and getattr(owner, "id", None):
        owner_ids.add(int(owner.id))

    team = getattr(app_info, "team", None)
    team_owner_id = parse_user_id(getattr(team, "owner_id", None))
    if team_owner_id:
        owner_ids.add(team_owner_id)

    APPLICATION_OWNER_IDS = owner_ids
    return APPLICATION_OWNER_IDS


async def is_application_owner(user_id: int) -> bool:
    owner_override = (
        parse_user_id(os.getenv("OWNER_ID"))
        or parse_user_id(os.getenv("BOT_OWNER_ID"))
        or DEFAULT_OWNER_ID
    )
    if user_id == owner_override or user_id in APPLICATION_OWNER_IDS:
        return True
    owner_ids = await refresh_application_owner_ids()
    return user_id == owner_override or user_id in owner_ids


async def safe_send(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    *,
    ephemeral: bool = False,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
):
    kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)
    except (discord.NotFound, discord.HTTPException) as error:
        print(f"Interaction response failed: {error}")
        return None


def format_discord_timestamp(timestamp: int, style: str = "R") -> str:
    return f"<t:{max(0, int(timestamp))}:{style}>"


def make_giveaway_id() -> str:
    for _ in range(20):
        candidate = f"gw{int(time.time()):x}{secrets.token_hex(2)}"[-20:]
        if candidate not in load_giveaways():
            return candidate
    return f"gw{secrets.token_hex(6)}"


def parse_duration(value: str) -> Optional[int]:
    raw = str(value or "").strip().lower()
    if raw.isdigit():
        seconds = int(raw)
    else:
        seconds = 0
        for amount, unit in GIVEAWAY_DURATION_PART_RE.findall(raw):
            amount_int = int(amount)
            seconds += amount_int * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit.lower()]
    if seconds < GIVEAWAY_MIN_DURATION_SECONDS or seconds > GIVEAWAY_MAX_DURATION_SECONDS:
        return None
    return seconds


def normalize_id_list(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        parsed = parse_user_id(value)
        if parsed and parsed not in seen:
            seen.add(parsed)
            result.append(parsed)
    return result[:5000]


def normalize_int_mapping(values: Any, *, maximum: int = 100) -> dict[str, int]:
    if not isinstance(values, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, value in values.items():
        parsed_key = parse_user_id(key)
        if parsed_key is None:
            continue
        count = max(1, min(maximum, coerce_int(value, 1)))
        normalized[str(parsed_key)] = count
    return normalized


def resolve_role_id(guild: Optional[discord.Guild], value: str) -> Optional[int]:
    text = str(value or "").strip()
    parsed = parse_user_id(text)
    if parsed:
        return parsed
    if not guild:
        return None
    normalized = text.lower().strip("@&<> ")
    compact = NON_ALNUM_RE.sub("", normalized)
    for role in guild.roles:
        if role.name.lower() == normalized:
            return role.id
    for role in guild.roles:
        if NON_ALNUM_RE.sub("", role.name.lower()) == compact:
            return role.id
    return None


def parse_role_entry_mapping(value: Optional[str], guild: Optional[discord.Guild]) -> dict[str, int]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    entries: dict[str, int] = {}
    for part in re.split(r"[,;\n]+", raw):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            role_part, count_part = part.rsplit(":", 1)
        elif "=" in part:
            role_part, count_part = part.rsplit("=", 1)
        else:
            continue
        role_id = resolve_role_id(guild, role_part)
        if role_id is None:
            continue
        entries[str(role_id)] = max(1, min(100, coerce_int(count_part, 1)))
    return entries


def normalize_giveaway(raw_id: Any, record: Any) -> Optional[dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    giveaway_id = re.sub(r"[^a-z0-9_-]", "", str(record.get("id") or raw_id).lower())[:32]
    if not GIVEAWAY_ID_RE.fullmatch(giveaway_id):
        return None
    guild_id = parse_user_id(record.get("guild_id"))
    channel_id = parse_user_id(record.get("channel_id"))
    host_id = parse_user_id(record.get("host_id"))
    if guild_id is None or channel_id is None or host_id is None:
        return None
    participant_ids = normalize_id_list(record.get("participant_ids"))
    participant_entries = normalize_int_mapping(record.get("participant_entries"), maximum=100)
    for user_id in participant_ids:
        participant_entries.setdefault(str(user_id), 1)
    return {
        "id": giveaway_id,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "message_id": parse_user_id(record.get("message_id")) or 0,
        "host_id": host_id,
        "prize": truncate(str(record.get("prize") or "Giveaway prize").strip(), 180),
        "winners": min(20, max(1, coerce_int(record.get("winners", 1), 1))),
        "end_at": max(0, coerce_int(record.get("end_at", 0), 0)),
        "created_at": max(0, coerce_int(record.get("created_at", int(time.time())), int(time.time()))),
        "participant_ids": participant_ids,
        "participant_entries": participant_entries,
        "forced_winner_id": parse_user_id(record.get("forced_winner_id")),
        "ended": bool(record.get("ended", False)),
        "ended_at": max(0, coerce_int(record.get("ended_at", 0), 0)),
        "winner_ids": normalize_id_list(record.get("winner_ids")),
        "required_role_id": parse_user_id(record.get("required_role_id")),
        "requirement_bypass_role_id": parse_user_id(record.get("requirement_bypass_role_id")),
        "blacklist_role_id": parse_user_id(record.get("blacklist_role_id")),
        "required_daily_messages": min(1_000_000, coerce_int(record.get("required_daily_messages", 0))),
        "required_weekly_messages": min(1_000_000, coerce_int(record.get("required_weekly_messages", 0))),
        "required_monthly_messages": min(1_000_000, coerce_int(record.get("required_monthly_messages", 0))),
        "required_total_messages": min(10_000_000, coerce_int(record.get("required_total_messages", 0))),
        "winner_role_id": parse_user_id(record.get("winner_role_id")),
        "image_url": str(record.get("image_url") or "").strip()[:500],
        "extra_entries": normalize_int_mapping(record.get("extra_entries"), maximum=100),
    }


def load_giveaways() -> dict[str, dict[str, Any]]:
    global GIVEAWAYS_CACHE
    if GIVEAWAYS_CACHE is not None:
        return GIVEAWAYS_CACHE
    raw = read_json(GIVEAWAYS_FILE, {})
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for raw_id, record in raw.items():
            giveaway = normalize_giveaway(raw_id, record)
            if giveaway:
                normalized[giveaway["id"]] = giveaway
    GIVEAWAYS_CACHE = normalized
    return GIVEAWAYS_CACHE


def save_giveaways(giveaways: dict[str, dict[str, Any]]) -> None:
    global GIVEAWAYS_CACHE
    normalized: dict[str, dict[str, Any]] = {}
    for raw_id, record in giveaways.items():
        giveaway = normalize_giveaway(raw_id, record)
        if giveaway:
            normalized[giveaway["id"]] = giveaway
    GIVEAWAYS_CACHE = normalized
    write_json(GIVEAWAYS_FILE, normalized)


def load_giveaway_settings() -> dict[str, Any]:
    global GIVEAWAY_SETTINGS_CACHE
    if GIVEAWAY_SETTINGS_CACHE is not None:
        return GIVEAWAY_SETTINGS_CACHE
    raw = read_json(GIVEAWAY_SETTINGS_FILE, {"guilds": {}})
    guilds = raw.get("guilds") if isinstance(raw, dict) else {}
    if not isinstance(guilds, dict):
        guilds = {}
    normalized: dict[str, Any] = {"guilds": {}}
    for guild_id, guild_settings in guilds.items():
        if not str(guild_id).isdigit() or not isinstance(guild_settings, dict):
            continue
        normalized["guilds"][str(guild_id)] = {
            "creator_role_ids": normalize_id_list(guild_settings.get("creator_role_ids")),
            "manager_role_ids": normalize_id_list(guild_settings.get("manager_role_ids")),
        }
    GIVEAWAY_SETTINGS_CACHE = normalized
    return GIVEAWAY_SETTINGS_CACHE


def save_giveaway_settings(settings: dict[str, Any]) -> None:
    global GIVEAWAY_SETTINGS_CACHE
    GIVEAWAY_SETTINGS_CACHE = settings
    write_json(GIVEAWAY_SETTINGS_FILE, settings)


def get_giveaway_settings(guild_id: int) -> dict[str, Any]:
    settings = load_giveaway_settings()
    guilds = settings.setdefault("guilds", {})
    guild_settings = guilds.setdefault(str(guild_id), {"creator_role_ids": [], "manager_role_ids": []})
    guild_settings.setdefault("creator_role_ids", [])
    guild_settings.setdefault("manager_role_ids", [])
    return guild_settings


def normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def normalize_welcome_settings(record: Any) -> dict[str, Any]:
    record = record if isinstance(record, dict) else {}
    return {
        "enabled": normalize_bool(record.get("enabled"), False),
        "channel_id": parse_user_id(record.get("channel_id")) or 0,
        "rules_channel_id": parse_user_id(record.get("rules_channel_id")) or 0,
        "message_template": truncate(record.get("message_template") or DEFAULT_WELCOME_MESSAGE, 1800),
        "leave_enabled": normalize_bool(record.get("leave_enabled"), False),
        "leave_channel_id": parse_user_id(record.get("leave_channel_id")) or 0,
        "leave_message_template": truncate(record.get("leave_message_template") or DEFAULT_LEAVE_MESSAGE, 1800),
    }


def load_welcome_settings() -> dict[str, Any]:
    global WELCOME_SETTINGS_CACHE
    if WELCOME_SETTINGS_CACHE is not None:
        return WELCOME_SETTINGS_CACHE
    raw = read_json(WELCOME_SETTINGS_FILE, {"guilds": {}})
    guilds = raw.get("guilds") if isinstance(raw, dict) else {}
    if not isinstance(guilds, dict):
        guilds = {}
    normalized: dict[str, Any] = {"guilds": {}}
    for guild_id, guild_settings in guilds.items():
        if str(guild_id).isdigit():
            normalized["guilds"][str(guild_id)] = normalize_welcome_settings(guild_settings)
    WELCOME_SETTINGS_CACHE = normalized
    return WELCOME_SETTINGS_CACHE


def save_welcome_settings(settings: dict[str, Any]) -> None:
    global WELCOME_SETTINGS_CACHE
    WELCOME_SETTINGS_CACHE = settings
    write_json(WELCOME_SETTINGS_FILE, settings)


def get_welcome_settings(guild_id: int) -> dict[str, Any]:
    settings = load_welcome_settings()
    guild_settings = settings.setdefault("guilds", {}).setdefault(str(guild_id), normalize_welcome_settings({}))
    normalized = normalize_welcome_settings(guild_settings)
    settings["guilds"][str(guild_id)] = normalized
    return normalized


def set_welcome_config(guild_id: int, **updates: Any) -> dict[str, Any]:
    settings = load_welcome_settings()
    guild_settings = get_welcome_settings(guild_id)
    for key in (
        "enabled",
        "channel_id",
        "rules_channel_id",
        "message_template",
        "leave_enabled",
        "leave_channel_id",
        "leave_message_template",
    ):
        if key in updates:
            guild_settings[key] = updates[key]
    guild_settings = normalize_welcome_settings(guild_settings)
    settings.setdefault("guilds", {})[str(guild_id)] = guild_settings
    save_welcome_settings(settings)
    return guild_settings


def get_guild_total_message_count(guild_id: int) -> int:
    stats = load_message_stats()
    users = stats.get("guilds", {}).get(str(guild_id), {}).get("users", {})
    if not isinstance(users, dict):
        return 0
    return sum(coerce_int(normalize_message_user_stats(record).get("total", 0)) for record in users.values())


def format_welcome_text(settings: dict[str, Any], member: discord.Member) -> str:
    guild = member.guild
    rules_channel_id = parse_user_id(settings.get("rules_channel_id")) or 0
    rules_channel = guild.get_channel(rules_channel_id) if rules_channel_id else None
    rules_value = rules_channel.mention if rules_channel else "#rules"
    member_count = guild.member_count or len(getattr(guild, "members", [])) or 0
    replacements = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{server}": guild.name,
        "{member_count}": format_count(member_count),
        "{rules_channel}": rules_value,
        "{rules}": rules_value,
    }
    text = str(settings.get("message_template") or DEFAULT_WELCOME_MESSAGE)
    for token, value in replacements.items():
        text = text.replace(token, str(value))
    return truncate(text, 1900)


async def send_welcome_message(member: discord.Member) -> None:
    settings = get_welcome_settings(member.guild.id)
    if not settings.get("enabled"):
        return
    channel_id = parse_user_id(settings.get("channel_id")) or 0
    if not channel_id:
        return
    channel = member.guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        try:
            channel = await member.guild.fetch_channel(channel_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        await channel.send(
            content=format_welcome_text(settings, member),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException as error:
        print(f"Could not send welcome message in {member.guild.id}: {error}", flush=True)


def format_leave_text(settings: dict[str, Any], member: discord.Member) -> str:
    guild = member.guild
    member_count = guild.member_count or len(getattr(guild, "members", [])) or 0
    replacements = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{server}": guild.name,
        "{member_count}": format_count(member_count),
    }
    text = str(settings.get("leave_message_template") or DEFAULT_LEAVE_MESSAGE)
    for token, value in replacements.items():
        text = text.replace(token, str(value))
    return truncate(text, 1900)


async def send_leave_message(member: discord.Member) -> None:
    settings = get_welcome_settings(member.guild.id)
    if not settings.get("leave_enabled"):
        return
    channel_id = parse_user_id(settings.get("leave_channel_id")) or 0
    if not channel_id:
        return
    channel = member.guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        try:
            channel = await member.guild.fetch_channel(channel_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        await channel.send(
            content=format_leave_text(settings, member),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException as error:
        print(f"Could not send leave message in {member.guild.id}: {error}", flush=True)


def load_suggestion_settings() -> dict[str, Any]:
    global SUGGESTION_SETTINGS_CACHE
    if SUGGESTION_SETTINGS_CACHE is not None:
        return SUGGESTION_SETTINGS_CACHE
    raw = read_json(SUGGESTION_SETTINGS_FILE, {"guilds": {}})
    guilds = raw.get("guilds") if isinstance(raw, dict) else {}
    if not isinstance(guilds, dict):
        guilds = {}
    normalized: dict[str, Any] = {"guilds": {}}
    for guild_id, guild_settings in guilds.items():
        if not str(guild_id).isdigit() or not isinstance(guild_settings, dict):
            continue
        normalized["guilds"][str(guild_id)] = {
            "channel_id": parse_user_id(guild_settings.get("channel_id")) or 0,
            "submit_channel_id": parse_user_id(guild_settings.get("submit_channel_id")) or 0,
            "move_channel_id": parse_user_id(guild_settings.get("move_channel_id")) or 0,
            "anonymous": normalize_bool(guild_settings.get("anonymous"), False),
            "dm_results": normalize_bool(guild_settings.get("dm_results"), True),
            "counter": max(0, coerce_int(guild_settings.get("counter", 0))),
        }
    SUGGESTION_SETTINGS_CACHE = normalized
    return SUGGESTION_SETTINGS_CACHE


def save_suggestion_settings(settings: dict[str, Any]) -> None:
    global SUGGESTION_SETTINGS_CACHE
    SUGGESTION_SETTINGS_CACHE = settings
    write_json(SUGGESTION_SETTINGS_FILE, settings)


def get_suggestion_settings(guild_id: int) -> dict[str, Any]:
    settings = load_suggestion_settings()
    guild_settings = settings.setdefault("guilds", {}).setdefault(
        str(guild_id),
        {
            "channel_id": 0,
            "submit_channel_id": 0,
            "move_channel_id": 0,
            "anonymous": False,
            "dm_results": True,
            "counter": 0,
        },
    )
    guild_settings.setdefault("channel_id", 0)
    guild_settings.setdefault("submit_channel_id", 0)
    guild_settings.setdefault("move_channel_id", 0)
    guild_settings.setdefault("anonymous", False)
    guild_settings.setdefault("dm_results", True)
    guild_settings.setdefault("counter", 0)
    return guild_settings


def normalize_suggestion(raw_key: Any, record: Any) -> Optional[dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    guild_id = parse_user_id(record.get("guild_id"))
    suggestion_number = coerce_int(record.get("number") or record.get("id"), 0)
    author_id = parse_user_id(record.get("author_id"))
    if not guild_id or not suggestion_number or not author_id:
        return None
    suggestion_key = str(record.get("key") or raw_key or f"{guild_id}-{suggestion_number}")
    suggestion_key = re.sub(r"[^a-zA-Z0-9_-]", "", suggestion_key)[:48] or f"{guild_id}-{suggestion_number}"
    status = str(record.get("status") or "pending").lower()
    if status not in {"pending", "approved", "denied", "considered", "implemented"}:
        status = "pending"
    return {
        "key": suggestion_key,
        "guild_id": guild_id,
        "number": suggestion_number,
        "channel_id": parse_user_id(record.get("channel_id")) or 0,
        "message_id": parse_user_id(record.get("message_id")) or 0,
        "author_id": author_id,
        "author_name": truncate(record.get("author_name") or "Unknown user", 80),
        "author_avatar": str(record.get("author_avatar") or "")[:500],
        "content": truncate(record.get("content") or "No suggestion text.", 1500),
        "status": status,
        "status_reason": truncate(record.get("status_reason") or "", 400),
        "anonymous": normalize_bool(record.get("anonymous"), False),
        "upvoter_ids": normalize_id_list(record.get("upvoter_ids")),
        "downvoter_ids": normalize_id_list(record.get("downvoter_ids")),
        "created_at": max(0, coerce_int(record.get("created_at", int(time.time())), int(time.time()))),
        "decided_at": max(0, coerce_int(record.get("decided_at", 0), 0)),
        "decided_by_id": parse_user_id(record.get("decided_by_id")) or 0,
    }


def load_suggestions() -> dict[str, dict[str, Any]]:
    global SUGGESTIONS_CACHE
    if SUGGESTIONS_CACHE is not None:
        return SUGGESTIONS_CACHE
    raw = read_json(SUGGESTIONS_FILE, {})
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for raw_key, record in raw.items():
            suggestion = normalize_suggestion(raw_key, record)
            if suggestion:
                normalized[suggestion["key"]] = suggestion
    SUGGESTIONS_CACHE = normalized
    return SUGGESTIONS_CACHE


def save_suggestions(suggestions: dict[str, dict[str, Any]]) -> None:
    global SUGGESTIONS_CACHE
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, record in suggestions.items():
        suggestion = normalize_suggestion(raw_key, record)
        if suggestion:
            normalized[suggestion["key"]] = suggestion
    SUGGESTIONS_CACHE = normalized
    write_json(SUGGESTIONS_FILE, normalized)


def load_reaction_role_panels() -> dict[str, Any]:
    global REACTION_ROLE_PANELS_CACHE
    if REACTION_ROLE_PANELS_CACHE is not None:
        return REACTION_ROLE_PANELS_CACHE
    raw = read_json(REACTION_ROLE_PANELS_FILE, {"guilds": {}})
    guilds = raw.get("guilds") if isinstance(raw, dict) else {}
    if not isinstance(guilds, dict):
        guilds = {}
    normalized: dict[str, Any] = {"guilds": {}}
    for guild_id, guild_state in guilds.items():
        if not str(guild_id).isdigit() or not isinstance(guild_state, dict):
            continue
        panels = guild_state.get("panels")
        if not isinstance(panels, dict):
            panels = {}
        clean_panels: dict[str, Any] = {}
        for raw_panel_id, panel in panels.items():
            if not isinstance(panel, dict):
                continue
            panel_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(panel.get("id") or raw_panel_id))[:32]
            if not panel_id:
                continue
            items: list[dict[str, Any]] = []
            for item in panel.get("items", []):
                if not isinstance(item, dict):
                    continue
                role_id = parse_user_id(item.get("role_id"))
                if not role_id:
                    continue
                items.append(
                    {
                        "role_id": role_id,
                        "label": truncate(item.get("label") or "Role", 80),
                        "emoji": truncate(item.get("emoji") or "", 32),
                        "style": str(item.get("style") or "primary").lower(),
                    }
                )
            clean_panels[panel_id] = {
                "id": panel_id,
                "guild_id": int(guild_id),
                "name": truncate(panel.get("name") or panel_id, 80),
                "channel_id": parse_user_id(panel.get("channel_id")) or 0,
                "message_id": parse_user_id(panel.get("message_id")) or 0,
                "title": truncate(panel.get("title") or panel.get("name") or "Reaction Roles", 180),
                "description": truncate(panel.get("description") or "Click a button to toggle a role.", 1800),
                "allow_multiple": normalize_bool(panel.get("allow_multiple"), True),
                "allowed_role_ids": normalize_id_list(panel.get("allowed_role_ids")),
                "ignored_role_ids": normalize_id_list(panel.get("ignored_role_ids")),
                "items": items[:25],
                "created_at": max(0, coerce_int(panel.get("created_at", int(time.time())), int(time.time()))),
            }
        normalized["guilds"][str(guild_id)] = {
            "counter": max(0, coerce_int(guild_state.get("counter", 0))),
            "panels": clean_panels,
        }
    REACTION_ROLE_PANELS_CACHE = normalized
    return REACTION_ROLE_PANELS_CACHE


def save_reaction_role_panels(state: dict[str, Any]) -> None:
    global REACTION_ROLE_PANELS_CACHE
    REACTION_ROLE_PANELS_CACHE = state
    write_json(REACTION_ROLE_PANELS_FILE, state)


def get_reaction_role_guild_state(guild_id: int) -> dict[str, Any]:
    state = load_reaction_role_panels()
    guild_state = state.setdefault("guilds", {}).setdefault(str(guild_id), {"counter": 0, "panels": {}})
    guild_state.setdefault("counter", 0)
    guild_state.setdefault("panels", {})
    return guild_state


def make_reaction_role_panel_id(guild_id: int) -> str:
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    guild_state["counter"] = max(0, coerce_int(guild_state.get("counter", 0))) + 1
    save_reaction_role_panels(state)
    return f"rr{guild_id:x}{guild_state['counter']:x}"[-30:]


def find_suggestion(guild_id: int, suggestion_ref: Any) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    ref = str(suggestion_ref or "").strip()
    ref_key = re.sub(r"[^a-zA-Z0-9_-]", "", ref)[:48]
    ref_number = coerce_int(ref, 0)
    for key, suggestion in load_suggestions().items():
        if int(suggestion.get("guild_id") or 0) != guild_id:
            continue
        if key == ref_key or coerce_int(suggestion.get("number"), 0) == ref_number:
            return key, suggestion
    return None, None


def set_suggestion_config(
    guild_id: int,
    *,
    channel_id: Optional[int] = None,
    submit_channel_id: Optional[int] = None,
    move_channel_id: Optional[int] = None,
    anonymous: Optional[bool] = None,
    dm_results: Optional[bool] = None,
) -> dict[str, Any]:
    state = load_suggestion_settings()
    settings = get_suggestion_settings(guild_id)
    if channel_id is not None:
        settings["channel_id"] = int(channel_id or 0)
    if submit_channel_id is not None:
        settings["submit_channel_id"] = int(submit_channel_id or 0)
    if move_channel_id is not None:
        settings["move_channel_id"] = int(move_channel_id or 0)
    if anonymous is not None:
        settings["anonymous"] = bool(anonymous)
    if dm_results is not None:
        settings["dm_results"] = bool(dm_results)
    state.setdefault("guilds", {})[str(guild_id)] = settings
    save_suggestion_settings(state)
    return settings


def create_reaction_role_panel_record(
    guild_id: int,
    *,
    name: str,
    channel_id: int = 0,
    title: str = "",
    description: str = "",
    allow_multiple: bool = True,
) -> dict[str, Any]:
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    panel_id = make_reaction_role_panel_id(guild_id)
    panel = {
        "id": panel_id,
        "guild_id": int(guild_id),
        "name": truncate(name.strip() or "Reaction Roles", 80),
        "channel_id": int(channel_id or 0),
        "message_id": 0,
        "title": truncate(title.strip() or name.strip() or "Reaction Roles", 180),
        "description": truncate(description.strip() or "Click a button to toggle a role.", 1800),
        "allow_multiple": bool(allow_multiple),
        "allowed_role_ids": [],
        "ignored_role_ids": [],
        "items": [],
        "created_at": int(time.time()),
    }
    guild_state.setdefault("panels", {})[panel_id] = panel
    state.setdefault("guilds", {})[str(guild_id)] = guild_state
    save_reaction_role_panels(state)
    return panel


def update_reaction_role_panel_record(
    guild_id: int,
    panel_id: str,
    *,
    name: Optional[str] = None,
    channel_id: Optional[int] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    allow_multiple: Optional[bool] = None,
) -> dict[str, Any]:
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    panel = guild_state.setdefault("panels", {}).get(panel_id)
    if not panel:
        raise ValueError("Reaction role panel not found.")
    if name is not None:
        panel["name"] = truncate(name.strip() or panel.get("name") or panel_id, 80)
    if channel_id is not None:
        panel["channel_id"] = int(channel_id or 0)
    if title is not None:
        panel["title"] = truncate(title.strip() or panel.get("title") or panel.get("name") or "Reaction Roles", 180)
    if description is not None:
        panel["description"] = truncate(description.strip() or "Click a button to toggle a role.", 1800)
    if allow_multiple is not None:
        panel["allow_multiple"] = bool(allow_multiple)
    guild_state["panels"][panel_id] = panel
    state.setdefault("guilds", {})[str(guild_id)] = guild_state
    save_reaction_role_panels(state)
    return panel


def add_reaction_role_item(
    guild_id: int,
    panel_id: str,
    *,
    role_id: int,
    label: str = "",
    emoji: str = "",
    style: str = "primary",
) -> dict[str, Any]:
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    panel = guild_state.setdefault("panels", {}).get(panel_id)
    if not panel:
        raise ValueError("Reaction role panel not found.")
    items = [item for item in panel.get("items", []) if parse_user_id(item.get("role_id")) != int(role_id)]
    if len(items) >= 25:
        raise ValueError("A reaction-role panel can only have 25 buttons.")
    items.append(
        {
            "role_id": int(role_id),
            "label": truncate(label or "Role", 80),
            "emoji": truncate(emoji or "", 32),
            "style": style.lower() if style.lower() in {"primary", "secondary", "success", "green", "danger", "red"} else "primary",
        }
    )
    panel["items"] = items
    guild_state["panels"][panel_id] = panel
    state.setdefault("guilds", {})[str(guild_id)] = guild_state
    save_reaction_role_panels(state)
    return panel


def remove_reaction_role_item(guild_id: int, panel_id: str, role_id: int) -> dict[str, Any]:
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    panel = guild_state.setdefault("panels", {}).get(panel_id)
    if not panel:
        raise ValueError("Reaction role panel not found.")
    panel["items"] = [item for item in panel.get("items", []) if parse_user_id(item.get("role_id")) != int(role_id)]
    guild_state["panels"][panel_id] = panel
    state.setdefault("guilds", {})[str(guild_id)] = guild_state
    save_reaction_role_panels(state)
    return panel


def set_reaction_role_access_role(
    guild_id: int,
    panel_id: str,
    *,
    list_name: str,
    action: str,
    role_id: Optional[int] = None,
) -> dict[str, Any]:
    if list_name not in {"allowed_role_ids", "ignored_role_ids"}:
        raise ValueError("Unknown access list.")
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    panel = guild_state.setdefault("panels", {}).get(panel_id)
    if not panel:
        raise ValueError("Reaction role panel not found.")
    current = set(normalize_id_list(panel.get(list_name)))
    action = action.strip().lower()
    if action == "clear":
        current.clear()
    elif action == "add" and role_id:
        current.add(int(role_id))
    elif action == "remove" and role_id:
        current.discard(int(role_id))
    else:
        raise ValueError("Use add, remove, or clear.")
    panel[list_name] = sorted(current)
    guild_state["panels"][panel_id] = panel
    state.setdefault("guilds", {})[str(guild_id)] = guild_state
    save_reaction_role_panels(state)
    return panel


def set_reaction_role_access_lists(
    guild_id: int,
    panel_id: str,
    *,
    allowed_role_ids: Optional[list[int]] = None,
    ignored_role_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    panel = guild_state.setdefault("panels", {}).get(panel_id)
    if not panel:
        raise ValueError("Reaction role panel not found.")
    if allowed_role_ids is not None:
        panel["allowed_role_ids"] = sorted({int(role_id) for role_id in allowed_role_ids if int(role_id) > 0})
    if ignored_role_ids is not None:
        panel["ignored_role_ids"] = sorted({int(role_id) for role_id in ignored_role_ids if int(role_id) > 0})
    guild_state["panels"][panel_id] = panel
    state.setdefault("guilds", {})[str(guild_id)] = guild_state
    save_reaction_role_panels(state)
    return panel


def delete_reaction_role_panel_record(guild_id: int, panel_id: str) -> Optional[dict[str, Any]]:
    state = load_reaction_role_panels()
    guild_state = get_reaction_role_guild_state(guild_id)
    panel = guild_state.setdefault("panels", {}).pop(panel_id, None)
    state.setdefault("guilds", {})[str(guild_id)] = guild_state
    save_reaction_role_panels(state)
    REGISTERED_REACTION_ROLE_VIEW_IDS.discard(panel_id)
    return panel


def member_has_role(member: discord.Member, role_id: Optional[int]) -> bool:
    return bool(role_id and any(role.id == role_id for role in member.roles))


def has_giveaway_manager_access(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    settings = get_giveaway_settings(member.guild.id)
    manager_ids = set(normalize_id_list(settings.get("manager_role_ids")))
    return any(role.id in manager_ids for role in member.roles)


def has_giveaway_creator_access(member: discord.Member) -> bool:
    if has_giveaway_manager_access(member):
        return True
    settings = get_giveaway_settings(member.guild.id)
    creator_ids = set(normalize_id_list(settings.get("creator_role_ids")))
    return any(role.id in creator_ids for role in member.roles)


async def require_giveaway_manager(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await safe_send(interaction, "This command can only be used in a server.", ephemeral=True)
        return False
    if not has_giveaway_manager_access(interaction.user):
        await safe_send(interaction, "You need Manage Server or a giveaway manager role to use this.", ephemeral=True)
        return False
    return True


async def require_giveaway_creator(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await safe_send(interaction, "This command can only be used in a server.", ephemeral=True)
        return False
    if not has_giveaway_creator_access(interaction.user):
        await safe_send(interaction, "You need Manage Server or a giveaway creator role to create giveaways.", ephemeral=True)
        return False
    return True


def current_message_period_keys(now: Optional[datetime] = None) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return {"day": now.strftime("%Y-%m-%d"), "week": f"{iso_year}-W{iso_week:02d}", "month": now.strftime("%Y-%m")}


def normalize_message_user_stats(record: Any, periods: Optional[dict[str, str]] = None) -> dict[str, Any]:
    periods = periods or current_message_period_keys()
    record = record if isinstance(record, dict) else {}
    stats = {
        "total": coerce_int(record.get("total", 0)),
        "day_key": str(record.get("day_key") or periods["day"]),
        "day_count": coerce_int(record.get("day_count", 0)),
        "week_key": str(record.get("week_key") or periods["week"]),
        "week_count": coerce_int(record.get("week_count", 0)),
        "month_key": str(record.get("month_key") or periods["month"]),
        "month_count": coerce_int(record.get("month_count", 0)),
    }
    for key, count_key in (("day", "day_count"), ("week", "week_count"), ("month", "month_count")):
        if stats[f"{key}_key"] != periods[key]:
            stats[f"{key}_key"] = periods[key]
            stats[count_key] = 0
    return stats


def load_message_stats() -> dict[str, Any]:
    global MESSAGE_STATS_CACHE
    if MESSAGE_STATS_CACHE is not None:
        return MESSAGE_STATS_CACHE
    raw = read_json(MESSAGE_STATS_FILE, {"guilds": {}})
    guilds = raw.get("guilds") if isinstance(raw, dict) else {}
    MESSAGE_STATS_CACHE = {"guilds": guilds if isinstance(guilds, dict) else {}}
    return MESSAGE_STATS_CACHE


def save_message_stats(*, force: bool = False) -> None:
    global MESSAGE_STATS_LAST_SAVE
    if MESSAGE_STATS_CACHE is None:
        return
    now = time.monotonic()
    if not force and MESSAGE_STATS_LAST_SAVE and now - MESSAGE_STATS_LAST_SAVE < MESSAGE_STATS_SAVE_INTERVAL_SECONDS:
        return
    write_json(MESSAGE_STATS_FILE, MESSAGE_STATS_CACHE)
    MESSAGE_STATS_LAST_SAVE = now


def record_message_stat(message: discord.Message) -> None:
    if not message.guild or message.author.bot:
        return
    periods = current_message_period_keys()
    stats = load_message_stats()
    users = stats.setdefault("guilds", {}).setdefault(str(message.guild.id), {}).setdefault("users", {})
    user_stats = normalize_message_user_stats(users.get(str(message.author.id)), periods)
    user_stats["total"] += 1
    user_stats["day_count"] += 1
    user_stats["week_count"] += 1
    user_stats["month_count"] += 1
    users[str(message.author.id)] = user_stats
    save_message_stats()


def get_message_counts(guild_id: int, user_id: int) -> dict[str, int]:
    stats = load_message_stats()
    raw = stats.get("guilds", {}).get(str(guild_id), {}).get("users", {}).get(str(user_id), {})
    user_stats = normalize_message_user_stats(raw)
    return {
        "daily": coerce_int(user_stats.get("day_count", 0)),
        "weekly": coerce_int(user_stats.get("week_count", 0)),
        "monthly": coerce_int(user_stats.get("month_count", 0)),
        "total": coerce_int(user_stats.get("total", 0)),
    }


def format_role(guild: Optional[discord.Guild], role_id: Any) -> str:
    parsed = parse_user_id(role_id)
    if not parsed:
        return ""
    role = guild.get_role(parsed) if guild else None
    return role.mention if role else f"<@&{parsed}>"


def giveaway_message_requirements(giveaway: dict[str, Any]) -> dict[str, int]:
    return {
        "daily": coerce_int(giveaway.get("required_daily_messages", 0), maximum=1_000_000),
        "weekly": coerce_int(giveaway.get("required_weekly_messages", 0), maximum=1_000_000),
        "monthly": coerce_int(giveaway.get("required_monthly_messages", 0), maximum=1_000_000),
        "total": coerce_int(giveaway.get("required_total_messages", 0), maximum=10_000_000),
    }


def format_message_requirements(giveaway: dict[str, Any]) -> str:
    labels = {
        "daily": "messages today",
        "weekly": "messages this week",
        "monthly": "messages this month",
        "total": "messages total",
    }
    return "\n".join(
        f"- **{format_count(amount)}** {labels[key]}"
        for key, amount in giveaway_message_requirements(giveaway).items()
        if amount > 0
    )


def giveaway_member_entry_count(giveaway: dict[str, Any], member: discord.Member) -> int:
    entries = 1
    for role_id, amount in normalize_int_mapping(giveaway.get("extra_entries"), maximum=100).items():
        if member_has_role(member, int(role_id)):
            entries = max(entries, int(amount))
    return min(100, entries)


def giveaway_entry_block_reason(guild: discord.Guild, member: discord.Member, giveaway: dict[str, Any]) -> Optional[str]:
    if member_has_role(member, giveaway.get("requirement_bypass_role_id")):
        return None
    if member_has_role(member, giveaway.get("blacklist_role_id")):
        return f"You cannot enter because you have the blacklisted role {format_role(guild, giveaway.get('blacklist_role_id'))}."
    if giveaway.get("required_role_id") and not member_has_role(member, giveaway.get("required_role_id")):
        return f"You need {format_role(guild, giveaway.get('required_role_id'))} to enter this giveaway."
    counts = get_message_counts(guild.id, member.id)
    missing: list[str] = []
    labels = {"daily": "today", "weekly": "this week", "monthly": "this month", "total": "total"}
    for key, required in giveaway_message_requirements(giveaway).items():
        if required > 0 and counts.get(key, 0) < required:
            missing.append(f"{labels[key]}: {format_count(counts.get(key, 0))}/{format_count(required)}")
    if missing:
        return "You do not meet the message requirements for this giveaway: " + ", ".join(missing)
    return None


def giveaway_message_url(giveaway: dict[str, Any]) -> str:
    guild_id = int(giveaway.get("guild_id") or 0)
    channel_id = int(giveaway.get("channel_id") or 0)
    message_id = int(giveaway.get("message_id") or 0)
    if guild_id and channel_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return PUBLIC_BASE_URL or "https://discord.com"


def find_giveaway_by_reference(value: Any, guild_id: Optional[int] = None) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    giveaways = load_giveaways()
    normalized = re.sub(r"[^a-z0-9_-]", "", str(value or "").lower())[:32]
    if normalized in giveaways:
        giveaway = giveaways[normalized]
        if guild_id is None or int(giveaway.get("guild_id") or 0) == int(guild_id):
            return normalized, giveaway

    message_id = extract_last_discord_id(value)
    if message_id:
        for giveaway_id, giveaway in giveaways.items():
            if int(giveaway.get("message_id") or 0) != message_id:
                continue
            if guild_id is not None and int(giveaway.get("guild_id") or 0) != int(guild_id):
                continue
            return giveaway_id, giveaway
    return None, None


def build_giveaway_link_view(giveaway: dict[str, Any]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Giveaway Message", style=discord.ButtonStyle.link, url=giveaway_message_url(giveaway)))
    return view


async def dm_giveaway_winner(user_id: int, giveaway: dict[str, Any]) -> None:
    guild = bot.get_guild(int(giveaway.get("guild_id") or 0))
    guild_name = guild.name if guild else "the server"
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    prize = str(giveaway.get("prize", "Giveaway prize"))
    embed = discord.Embed(
        title="Congratulations!",
        description=(
            f"Congratulations! You've won the giveaway of [{prize}]({giveaway_message_url(giveaway)}) "
            f"in the server: **{guild_name}**"
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    if bot.user and bot.user.display_avatar:
        embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
        embed.set_footer(text=bot.user.display_name)
    try:
        await user.send(embed=embed, view=build_giveaway_link_view(giveaway))
    except discord.HTTPException:
        pass


async def dm_entry_removed(user_id: int, giveaway: dict[str, Any], role: Optional[discord.Role]) -> None:
    guild = bot.get_guild(int(giveaway.get("guild_id") or 0))
    guild_name = guild.name if guild else "the server"
    role_name = role.name if role else "the blacklisted role"
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    prize = str(giveaway.get("prize", "Giveaway prize"))
    embed = discord.Embed(
        title="Entry Removed!",
        description=(
            f"Your entry for the giveaway of [{prize}]({giveaway_message_url(giveaway)}) is removed because "
            f"you have the blacklisted role **{role_name}** in the server **{guild_name}**"
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    if bot.user and bot.user.display_avatar:
        embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
        embed.set_footer(text=bot.user.display_name)
    try:
        await user.send(embed=embed, view=build_giveaway_link_view(giveaway))
    except discord.HTTPException:
        pass


async def dm_forced_winner_notice(
    giveaway_id: str,
    giveaway: dict[str, Any],
    forced_user_id: int,
    actor: Any,
) -> None:
    owner = bot.get_user(DEFAULT_OWNER_ID)
    if owner is None:
        try:
            owner = await bot.fetch_user(DEFAULT_OWNER_ID)
        except discord.HTTPException:
            return

    guild = bot.get_guild(int(giveaway.get("guild_id") or 0))
    guild_name = guild.name if guild else str(giveaway.get("guild_id") or "Unknown server")
    prize = str(giveaway.get("prize", "Giveaway prize"))
    embed = discord.Embed(
        title="Forced giveaway winner set",
        description=f"<@{forced_user_id}> will win **{prize}** when this giveaway ends.",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Giveaway", value=f"`{giveaway_id}`\n[Open message]({giveaway_message_url(giveaway)})", inline=False)
    embed.add_field(name="Server", value=guild_name, inline=True)
    embed.add_field(name="Set by", value=f"{actor.mention} (`{actor.id}`)", inline=True)
    if bot.user and bot.user.display_avatar:
        embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
        embed.set_footer(text=bot.user.display_name)
    try:
        await owner.send(embed=embed, view=build_giveaway_link_view(giveaway))
    except discord.HTTPException:
        pass


def format_extra_entries(guild: Optional[discord.Guild], giveaway: dict[str, Any]) -> str:
    entries = normalize_int_mapping(giveaway.get("extra_entries"), maximum=100)
    lines = []
    for role_id, amount in entries.items():
        lines.append(f"{format_role(guild, role_id)} - **{format_count(amount)} entries**")
    return "\n".join(lines[:15])


def build_giveaway_embed(giveaway: dict[str, Any]) -> discord.Embed:
    guild = bot.get_guild(int(giveaway.get("guild_id") or 0))
    participant_count = len(normalize_id_list(giveaway.get("participant_ids")))
    if giveaway.get("ended"):
        winner_ids = normalize_id_list(giveaway.get("winner_ids"))
        winners = ", ".join(f"<@{user_id}>" for user_id in winner_ids) if winner_ids else "No valid entries"
        embed = discord.Embed(
            title=f"\U0001f389 GIVEAWAY ENDED \U0001f389\n{giveaway.get('prize', 'Giveaway prize')}",
            description=(
                f"Winner{'s' if len(winner_ids) != 1 else ''}: {winners}\n"
                f"Entries: **{format_count(participant_count)}**\n"
                f"Ended at: {format_discord_timestamp(int(giveaway.get('ended_at') or giveaway.get('end_at') or time.time()), 'F')}"
            ),
            color=discord.Color.red(),
        )
    else:
        description = (
            f"Click {GIVEAWAY_EMOJI} button to enter!\n"
            f"Winners: **{format_count(giveaway.get('winners', 1))}**\n"
            f"Hosted by: <@{giveaway.get('host_id')}>\n"
            f"Ends: {format_discord_timestamp(int(giveaway.get('end_at') or 0), 'R')}\n"
            f"Ends at: {format_discord_timestamp(int(giveaway.get('end_at') or 0), 'F')}"
        )
        extra = format_extra_entries(guild, giveaway)
        if extra:
            description += f"\n\n**Extra Entries:**\n{extra}"
        requirements: list[str] = []
        if giveaway.get("required_role_id"):
            requirements.append(f"Must have role: {format_role(guild, giveaway.get('required_role_id'))}")
        message_requirements = format_message_requirements(giveaway)
        if message_requirements:
            requirements.append(f"Must have sent:\n{message_requirements}")
        if giveaway.get("requirement_bypass_role_id"):
            requirements.append(f"Requirements Bypass Role: {format_role(guild, giveaway.get('requirement_bypass_role_id'))}")
        if giveaway.get("blacklist_role_id"):
            requirements.append(f"Must not have the role: {format_role(guild, giveaway.get('blacklist_role_id'))}")
        if requirements:
            description += "\n\n" + "\n".join(requirements)
        embed = discord.Embed(
            title=str(giveaway.get("prize", "Giveaway prize")),
            description=description,
            color=discord.Color.blue(),
        )
    if giveaway.get("image_url"):
        embed.set_image(url=str(giveaway["image_url"]))
    if bot.user and bot.user.display_avatar:
        embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
    embed.set_footer(text=f"Giveaway ID: {giveaway.get('id')} | Entries: {format_count(participant_count)}")
    return embed


async def fetch_giveaway_message(giveaway: dict[str, Any]) -> Optional[discord.Message]:
    guild = bot.get_guild(int(giveaway.get("guild_id") or 0))
    if not guild:
        return None
    channel = guild.get_channel(int(giveaway.get("channel_id") or 0))
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await guild.fetch_channel(int(giveaway.get("channel_id") or 0))
        except discord.HTTPException:
            return None
    try:
        return await channel.fetch_message(int(giveaway.get("message_id") or 0))
    except discord.HTTPException:
        return None


async def update_giveaway_message(giveaway_id: str) -> bool:
    giveaway = load_giveaways().get(giveaway_id)
    if not giveaway:
        return False
    message = await fetch_giveaway_message(giveaway)
    if not message:
        return False
    try:
        await message.edit(embed=build_giveaway_embed(giveaway), view=GiveawayView(giveaway_id))
        return True
    except discord.HTTPException as error:
        print(f"Could not update giveaway {giveaway_id}: {error}")
        return False


def register_giveaway_view(giveaway_id: str) -> None:
    if giveaway_id in REGISTERED_GIVEAWAY_VIEW_IDS:
        return
    bot.add_view(GiveawayView(giveaway_id))
    REGISTERED_GIVEAWAY_VIEW_IDS.add(giveaway_id)


def restore_giveaway_views() -> None:
    for giveaway_id, giveaway in load_giveaways().items():
        if not giveaway.get("ended"):
            register_giveaway_view(giveaway_id)


def suggestion_status_color(suggestion: dict[str, Any]) -> discord.Color:
    status = str(suggestion.get("status") or "pending").lower()
    if status == "approved":
        return discord.Color.green()
    if status == "denied":
        return discord.Color.red()
    if status == "considered":
        return discord.Color.gold()
    if status == "implemented":
        return discord.Color.teal()
    return discord.Color.blurple()


def build_suggestion_embed(suggestion: dict[str, Any]) -> discord.Embed:
    status = str(suggestion.get("status") or "pending").title()
    reason = str(suggestion.get("status_reason") or "").strip()
    description = str(suggestion.get("content") or "No suggestion text.")
    if reason:
        description += f"\n\n**Moderator note:** {reason}"
    embed = discord.Embed(
        title=f"Suggestion #{suggestion.get('number')}",
        description=description,
        color=suggestion_status_color(suggestion),
        timestamp=datetime.fromtimestamp(int(suggestion.get("created_at") or time.time()), tz=timezone.utc),
    )
    if suggestion.get("anonymous"):
        embed.set_author(name="Anonymous suggestion")
    else:
        avatar = str(suggestion.get("author_avatar") or "")
        embed.set_author(name=str(suggestion.get("author_name") or "Unknown user"), icon_url=avatar or None)
    if status.lower() != "pending":
        embed.set_footer(text=status)
    return embed


async def fetch_suggestion_message(suggestion: dict[str, Any]) -> Optional[discord.Message]:
    guild = bot.get_guild(int(suggestion.get("guild_id") or 0))
    if not guild:
        return None
    channel = guild.get_channel(int(suggestion.get("channel_id") or 0))
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await guild.fetch_channel(int(suggestion.get("channel_id") or 0))
        except discord.HTTPException:
            return None
    try:
        return await channel.fetch_message(int(suggestion.get("message_id") or 0))
    except discord.HTTPException:
        return None


async def update_suggestion_message(suggestion_key: str) -> bool:
    suggestion = load_suggestions().get(suggestion_key)
    if not suggestion:
        return False
    message = await fetch_suggestion_message(suggestion)
    if not message:
        return False
    try:
        await message.edit(embed=build_suggestion_embed(suggestion), view=None)
        if suggestion.get("status") in {"pending", "considered"}:
            await ensure_suggestion_reactions(message)
        return True
    except discord.HTTPException as error:
        print(f"Could not update suggestion {suggestion_key}: {error}")
        return False


def register_suggestion_view(suggestion_key: str) -> None:
    return


def restore_suggestion_views() -> None:
    REGISTERED_SUGGESTION_VIEW_IDS.clear()


def suggestion_reaction_direction(emoji: Any) -> Optional[str]:
    text = str(emoji or "").replace("\ufe0f", "")
    if text == SUGGESTION_UP_EMOJI.replace("\ufe0f", ""):
        return "up"
    if text == SUGGESTION_DOWN_EMOJI.replace("\ufe0f", ""):
        return "down"
    return None


def find_suggestion_by_message(guild_id: int, message_id: int) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    for key, suggestion in load_suggestions().items():
        if int(suggestion.get("guild_id") or 0) == guild_id and int(suggestion.get("message_id") or 0) == message_id:
            return key, suggestion
    return None, None


async def ensure_suggestion_reactions(message: discord.Message) -> None:
    for emoji in (SUGGESTION_UP_EMOJI, SUGGESTION_DOWN_EMOJI):
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass


async def refresh_active_suggestion_messages() -> None:
    for suggestion_key, suggestion in list(load_suggestions().items()):
        if suggestion.get("status") not in {"pending", "considered"} or not suggestion.get("message_id"):
            continue
        await update_suggestion_message(suggestion_key)


async def record_suggestion_reaction_vote(
    payload: discord.RawReactionActionEvent,
    *,
    added: bool,
) -> None:
    if payload.guild_id is None or payload.user_id == getattr(bot.user, "id", None):
        return
    direction = suggestion_reaction_direction(payload.emoji)
    if not direction:
        return
    suggestion_key, suggestion = find_suggestion_by_message(int(payload.guild_id), int(payload.message_id))
    if not suggestion_key or not suggestion:
        return
    if suggestion.get("status") not in {"pending", "considered"}:
        return
    suggestions = load_suggestions()
    upvoters = set(normalize_id_list(suggestion.get("upvoter_ids")))
    downvoters = set(normalize_id_list(suggestion.get("downvoter_ids")))
    target = upvoters if direction == "up" else downvoters
    other = downvoters if direction == "up" else upvoters
    user_id = int(payload.user_id)
    if added:
        target.add(user_id)
        other.discard(user_id)
    else:
        target.discard(user_id)
    suggestion["upvoter_ids"] = sorted(upvoters)
    suggestion["downvoter_ids"] = sorted(downvoters)
    suggestions[suggestion_key] = suggestion
    save_suggestions(suggestions)

    if added:
        opposite = SUGGESTION_DOWN_EMOJI if direction == "up" else SUGGESTION_UP_EMOJI
        try:
            message = await fetch_suggestion_message(suggestion)
            guild = bot.get_guild(int(payload.guild_id))
            user = guild.get_member(user_id) if guild else None
            if user is None:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            if message and user:
                await message.remove_reaction(opposite, user)
        except discord.HTTPException:
            pass


async def post_new_suggestion(interaction: discord.Interaction, content: str) -> tuple[bool, str]:
    if not interaction.guild or not interaction.channel:
        return False, "Suggestions can only be created in a server."
    settings_state = load_suggestion_settings()
    settings = get_suggestion_settings(interaction.guild.id)
    submit_channel_id = parse_user_id(settings.get("submit_channel_id"))
    if submit_channel_id and interaction.channel.id != submit_channel_id:
        return False, f"Please submit suggestions in <#{submit_channel_id}>."
    target_channel_id = parse_user_id(settings.get("channel_id")) or interaction.channel.id
    target_channel = interaction.guild.get_channel(target_channel_id)
    if not isinstance(target_channel, discord.TextChannel):
        return False, "Suggestion channel not found. Ask an admin to run `/suggestion channel`."
    perms = target_channel.permissions_for(interaction.guild.me) if interaction.guild.me else None
    if not perms or not (
        perms.view_channel
        and perms.send_messages
        and perms.embed_links
        and perms.add_reactions
        and perms.read_message_history
    ):
        return False, (
            f"I need View Channel, Send Messages, Embed Links, Add Reactions, "
            f"and Read Message History in {target_channel.mention}."
        )

    settings["counter"] = max(0, coerce_int(settings.get("counter", 0))) + 1
    number = settings["counter"]
    suggestion_key = f"{interaction.guild.id}-{number}"
    anonymous = normalize_bool(settings.get("anonymous"), False)
    suggestion = {
        "key": suggestion_key,
        "guild_id": interaction.guild.id,
        "number": number,
        "channel_id": target_channel.id,
        "message_id": 0,
        "author_id": interaction.user.id,
        "author_name": getattr(interaction.user, "display_name", interaction.user.name),
        "author_avatar": interaction.user.display_avatar.url if getattr(interaction.user, "display_avatar", None) else "",
        "content": truncate(content.strip(), 1500),
        "status": "pending",
        "status_reason": "",
        "anonymous": anonymous,
        "upvoter_ids": [],
        "downvoter_ids": [],
        "created_at": int(time.time()),
        "decided_at": 0,
        "decided_by_id": 0,
    }
    suggestions = load_suggestions()
    suggestions[suggestion_key] = suggestion
    settings_state.setdefault("guilds", {})[str(interaction.guild.id)] = settings
    save_suggestion_settings(settings_state)
    save_suggestions(suggestions)

    message = await target_channel.send(embed=build_suggestion_embed(suggestion))
    suggestion["message_id"] = message.id
    suggestions[suggestion_key] = suggestion
    save_suggestions(suggestions)
    await ensure_suggestion_reactions(message)
    return True, f"Suggestion #{number} posted in {target_channel.mention}."


async def notify_suggestion_author(suggestion: dict[str, Any], status: str, reason: str) -> None:
    settings = get_suggestion_settings(int(suggestion.get("guild_id") or 0))
    if not normalize_bool(settings.get("dm_results"), True):
        return
    user_id = parse_user_id(suggestion.get("author_id"))
    if not user_id:
        return
    user = bot.get_user(user_id)
    try:
        if user is None:
            user = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title=f"Suggestion #{suggestion.get('number')} {status.title()}",
            description=str(suggestion.get("content") or "Your suggestion was reviewed."),
            color=suggestion_status_color(suggestion),
        )
        if reason:
            embed.add_field(name="Moderator note", value=truncate(reason, 900), inline=False)
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


async def announce_suggestion_status(suggestion: dict[str, Any]) -> None:
    settings = get_suggestion_settings(int(suggestion.get("guild_id") or 0))
    move_channel_id = parse_user_id(settings.get("move_channel_id"))
    guild = bot.get_guild(int(suggestion.get("guild_id") or 0))
    if not guild or not move_channel_id:
        return
    channel = guild.get_channel(move_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=build_suggestion_embed(suggestion))
    except discord.HTTPException:
        pass


async def set_suggestion_status(
    interaction: discord.Interaction,
    suggestion_ref: str,
    status: str,
    reason: Optional[str] = None,
) -> None:
    if not interaction.guild:
        await safe_send(interaction, "This command can only be used in a server.", ephemeral=True)
        return
    suggestion_key, suggestion = find_suggestion(interaction.guild.id, suggestion_ref)
    if not suggestion_key or not suggestion:
        await safe_send(interaction, "Suggestion not found.", ephemeral=True)
        return
    suggestions = load_suggestions()
    suggestion["status"] = status
    suggestion["status_reason"] = truncate(reason or "", 400)
    suggestion["decided_at"] = int(time.time())
    suggestion["decided_by_id"] = interaction.user.id
    suggestions[suggestion_key] = suggestion
    save_suggestions(suggestions)
    await update_suggestion_message(suggestion_key)
    await notify_suggestion_author(suggestion, status, suggestion["status_reason"])
    await announce_suggestion_status(suggestion)
    await safe_send(interaction, f"Suggestion #{suggestion.get('number')} marked as {status}.", ephemeral=True)


def build_reaction_role_embed(panel: dict[str, Any]) -> discord.Embed:
    return discord.Embed(
        title=str(panel.get("title") or panel.get("name") or "Reaction Roles"),
        description=str(panel.get("description") or "Click a button to toggle a role."),
        color=discord.Color.blurple(),
    )


def button_style_from_text(value: Any) -> discord.ButtonStyle:
    text = str(value or "primary").lower()
    return {
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "green": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
        "red": discord.ButtonStyle.danger,
    }.get(text, discord.ButtonStyle.primary)


def find_reaction_role_panel(panel_id: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    normalized_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(panel_id or ""))[:32]
    for guild_id, guild_state in load_reaction_role_panels().get("guilds", {}).items():
        panel = guild_state.get("panels", {}).get(normalized_id)
        if isinstance(panel, dict):
            return guild_id, panel
    return None, None


async def fetch_reaction_role_message(panel: dict[str, Any]) -> Optional[discord.Message]:
    guild = bot.get_guild(int(panel.get("guild_id") or 0))
    if not guild:
        return None
    channel = guild.get_channel(int(panel.get("channel_id") or 0))
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await guild.fetch_channel(int(panel.get("channel_id") or 0))
        except discord.HTTPException:
            return None
    try:
        return await channel.fetch_message(int(panel.get("message_id") or 0))
    except discord.HTTPException:
        return None


async def post_reaction_role_panel(guild: discord.Guild, panel_id: str, channel_id: Optional[int] = None) -> tuple[bool, str]:
    guild_state = get_reaction_role_guild_state(guild.id)
    panel = guild_state.get("panels", {}).get(panel_id)
    if not panel:
        return False, "Reaction role panel not found."
    target_channel_id = channel_id or parse_user_id(panel.get("channel_id"))
    channel = guild.get_channel(int(target_channel_id or 0))
    if not isinstance(channel, discord.TextChannel):
        return False, "Choose a valid text channel."
    perms = channel.permissions_for(guild.me) if guild.me else None
    if not perms or not (perms.view_channel and perms.send_messages and perms.embed_links):
        return False, f"I need View Channel, Send Messages, and Embed Links in {channel.mention}."
    message = await fetch_reaction_role_message(panel)
    panel["channel_id"] = channel.id
    panel_view = ReactionRoleView(panel_id) if panel.get("items") else None
    if message:
        await message.edit(embed=build_reaction_role_embed(panel), view=panel_view)
    else:
        message = await channel.send(embed=build_reaction_role_embed(panel), view=panel_view)
        panel["message_id"] = message.id
    load_reaction_role_panels().setdefault("guilds", {}).setdefault(str(guild.id), guild_state)
    guild_state.setdefault("panels", {})[panel_id] = panel
    save_reaction_role_panels(load_reaction_role_panels())
    register_reaction_role_view(panel_id)
    return True, f"Reaction role panel posted in {channel.mention}."


def register_reaction_role_view(panel_id: str) -> None:
    if panel_id in REGISTERED_REACTION_ROLE_VIEW_IDS:
        return
    bot.add_view(ReactionRoleView(panel_id))
    REGISTERED_REACTION_ROLE_VIEW_IDS.add(panel_id)


def restore_reaction_role_views() -> None:
    for guild_state in load_reaction_role_panels().get("guilds", {}).values():
        for panel_id, panel in guild_state.get("panels", {}).items():
            if panel.get("message_id") and panel.get("items"):
                register_reaction_role_view(panel_id)


class ReactionRoleView(discord.ui.View):
    def __init__(self, panel_id: str):
        super().__init__(timeout=None)
        self.panel_id = panel_id
        _guild_id, panel = find_reaction_role_panel(panel_id)
        for index, item in enumerate((panel or {}).get("items", [])[:25]):
            role_id = parse_user_id(item.get("role_id"))
            if not role_id:
                continue
            button = discord.ui.Button(
                label=truncate(item.get("label") or "Role", 80),
                emoji=str(item.get("emoji") or "") or None,
                style=button_style_from_text(item.get("style")),
                row=index // 5,
                custom_id=f"rr:{panel_id}:{role_id}",
            )
            button.callback = self.toggle_role
            self.add_item(button)

    async def toggle_role(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await safe_send(interaction, "Use this button inside the server.", ephemeral=True)
            return
        _guild_id, panel = find_reaction_role_panel(self.panel_id)
        if not panel or int(panel.get("guild_id") or 0) != interaction.guild.id:
            await safe_send(interaction, "Reaction role panel not found.", ephemeral=True)
            return
        custom_id = str(getattr(interaction.data, "custom_id", "") or interaction.data.get("custom_id", ""))
        role_id = parse_user_id(custom_id.rsplit(":", 1)[-1])
        role = interaction.guild.get_role(role_id or 0)
        if not role:
            await safe_send(interaction, "That role no longer exists.", ephemeral=True)
            return
        ignored_ids = set(normalize_id_list(panel.get("ignored_role_ids")))
        allowed_ids = set(normalize_id_list(panel.get("allowed_role_ids")))
        member_role_ids = {member_role.id for member_role in interaction.user.roles}
        if ignored_ids and member_role_ids.intersection(ignored_ids):
            await safe_send(interaction, "You cannot use this reaction-role panel.", ephemeral=True)
            return
        if allowed_ids and not member_role_ids.intersection(allowed_ids):
            await safe_send(interaction, "You do not have permission to use this reaction-role panel.", ephemeral=True)
            return
        if not interaction.guild.me:
            await safe_send(interaction, "I could not check my role permissions.", ephemeral=True)
            return
        if role >= interaction.guild.me.top_role:
            await safe_send(interaction, "I cannot manage that role because it is above my highest role.", ephemeral=True)
            return
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason=f"Reaction role panel {self.panel_id}")
                await safe_send(interaction, f"Removed **{role.name}**.", ephemeral=True)
                return
            if not normalize_bool(panel.get("allow_multiple"), True):
                panel_role_ids = {
                    parse_user_id(item.get("role_id"))
                    for item in panel.get("items", [])
                    if parse_user_id(item.get("role_id")) and parse_user_id(item.get("role_id")) != role.id
                }
                remove_roles = [member_role for member_role in interaction.user.roles if member_role.id in panel_role_ids]
                if remove_roles:
                    await interaction.user.remove_roles(*remove_roles, reason=f"Reaction role panel {self.panel_id} single role")
            await interaction.user.add_roles(role, reason=f"Reaction role panel {self.panel_id}")
            await safe_send(interaction, f"Added **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await safe_send(interaction, "I do not have permission to manage that role.", ephemeral=True)
        except discord.HTTPException as error:
            await safe_send(interaction, f"Discord could not update your roles: {error}", ephemeral=True)


def build_participants_embed(giveaway: dict[str, Any], page: int = 0) -> discord.Embed:
    participant_ids = normalize_id_list(giveaway.get("participant_ids"))
    entries = normalize_int_mapping(giveaway.get("participant_entries"), maximum=100)
    total_pages = max(1, (len(participant_ids) + GIVEAWAY_PARTICIPANTS_PAGE_SIZE - 1) // GIVEAWAY_PARTICIPANTS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * GIVEAWAY_PARTICIPANTS_PAGE_SIZE
    lines = []
    for index, user_id in enumerate(participant_ids[start : start + GIVEAWAY_PARTICIPANTS_PAGE_SIZE], start=start + 1):
        amount = max(1, entries.get(str(user_id), 1))
        lines.append(f"{index}. <@{user_id}> (**{format_count(amount)} entr{'y' if amount == 1 else 'ies'}**)")
    if not lines:
        lines.append("No participants yet.")
    embed = discord.Embed(
        title=f"Giveaway Participants (Page {page + 1}/{total_pages})",
        description=(
            f"Participants for **{giveaway.get('prize', 'Giveaway prize')}**:\n\n"
            + "\n".join(lines)
            + f"\n\nTotal Participants: **{format_count(len(participant_ids))}**"
        ),
        color=discord.Color.blue(),
    )
    if bot.user and bot.user.display_avatar:
        embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
    return embed


def remove_giveaway_participant(giveaway_id: str, user_id: int) -> tuple[bool, Optional[dict[str, Any]]]:
    giveaways = load_giveaways()
    giveaway = giveaways.get(giveaway_id)
    if not giveaway:
        return False, None
    participant_ids = normalize_id_list(giveaway.get("participant_ids"))
    if user_id not in participant_ids:
        return False, giveaway
    giveaway["participant_ids"] = [participant_id for participant_id in participant_ids if participant_id != user_id]
    entries = normalize_int_mapping(giveaway.get("participant_entries"), maximum=100)
    entries.pop(str(user_id), None)
    giveaway["participant_entries"] = entries
    save_giveaways(giveaways)
    return True, giveaway


class RemoveParticipantModal(discord.ui.Modal, title="Remove Participant"):
    user_text = discord.ui.TextInput(label="User ID or mention", placeholder="@user or 123456789012345678", max_length=80)

    def __init__(self, giveaway_id: str, page: int):
        super().__init__()
        self.giveaway_id = giveaway_id
        self.page = page

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_giveaway_manager(interaction):
            return
        user_id = parse_user_id(str(self.user_text.value))
        if not user_id:
            await safe_send(interaction, "I could not find a user ID in that input.", ephemeral=True)
            return
        removed, giveaway = remove_giveaway_participant(self.giveaway_id, user_id)
        if not giveaway:
            await safe_send(interaction, "Giveaway not found.", ephemeral=True)
            return
        if not removed:
            await safe_send(interaction, "That user is not participating in this giveaway.", ephemeral=True)
            return
        await update_giveaway_message(self.giveaway_id)
        try:
            await interaction.response.edit_message(
                embed=build_participants_embed(giveaway, self.page),
                view=ParticipantsView(self.giveaway_id, self.page),
            )
        except discord.HTTPException:
            await safe_send(interaction, f"Removed <@{user_id}> from the giveaway.", ephemeral=True)


class ParticipantsView(discord.ui.View):
    def __init__(self, giveaway_id: str, page: int = 0):
        super().__init__(timeout=120)
        self.giveaway_id = giveaway_id
        self.page = page
        giveaway = load_giveaways().get(giveaway_id, {})
        participant_count = len(normalize_id_list(giveaway.get("participant_ids")))
        total_pages = max(1, (participant_count + GIVEAWAY_PARTICIPANTS_PAGE_SIZE - 1) // GIVEAWAY_PARTICIPANTS_PAGE_SIZE)
        previous_button = discord.ui.Button(emoji="\u25c0", style=discord.ButtonStyle.secondary, disabled=page <= 0)
        next_button = discord.ui.Button(emoji="\u25b6", style=discord.ButtonStyle.secondary, disabled=page >= total_pages - 1)
        previous_button.callback = self.previous_page
        next_button.callback = self.next_page
        self.add_item(previous_button)
        self.add_item(next_button)
        remove_button = discord.ui.Button(label="Remove A Participant", style=discord.ButtonStyle.danger, disabled=participant_count <= 0)
        remove_button.callback = self.remove_participant
        self.add_item(remove_button)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        await self.show_page(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction) -> None:
        await self.show_page(interaction, self.page + 1)

    async def remove_participant(self, interaction: discord.Interaction) -> None:
        if not await require_giveaway_manager(interaction):
            return
        await interaction.response.send_modal(RemoveParticipantModal(self.giveaway_id, self.page))

    async def show_page(self, interaction: discord.Interaction, page: int) -> None:
        giveaway = load_giveaways().get(self.giveaway_id)
        if not giveaway:
            await safe_send(interaction, "Giveaway not found.", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(
                embed=build_participants_embed(giveaway, page),
                view=ParticipantsView(self.giveaway_id, page),
            )
        except discord.HTTPException as error:
            print(f"Could not edit giveaway participants page: {error}")


class GiveawayLeaveConfirmView(discord.ui.View):
    def __init__(self, giveaway_id: str, user_id: int):
        super().__init__(timeout=90)
        self.giveaway_id = giveaway_id
        self.user_id = user_id

    @discord.ui.button(label="Leave Giveaway", style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await safe_send(interaction, "This confirmation belongs to another user.", ephemeral=True)
            return
        giveaways = load_giveaways()
        giveaway = giveaways.get(self.giveaway_id)
        if not giveaway:
            await interaction.response.edit_message(content="Giveaway not found anymore.", view=None)
            return
        if giveaway.get("ended"):
            await interaction.response.edit_message(content="This giveaway has already ended.", view=None)
            return

        participant_ids = normalize_id_list(giveaway.get("participant_ids"))
        if self.user_id not in participant_ids:
            await interaction.response.edit_message(content="You are not entered in this giveaway anymore.", view=None)
            return

        giveaway["participant_ids"] = [user_id for user_id in participant_ids if user_id != self.user_id]
        entries = normalize_int_mapping(giveaway.get("participant_entries"), maximum=100)
        entries.pop(str(self.user_id), None)
        giveaway["participant_entries"] = entries
        giveaways[self.giveaway_id] = giveaway
        save_giveaways(giveaways)
        await update_giveaway_message(self.giveaway_id)
        await interaction.response.edit_message(content="Your giveaway entry was removed.", view=None)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: str):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        giveaway = load_giveaways().get(giveaway_id, {})
        ended = bool(giveaway.get("ended"))
        participant_count = len(normalize_id_list(giveaway.get("participant_ids")))
        enter_button = discord.ui.Button(
            label=format_count(participant_count),
            emoji=GIVEAWAY_EMOJI,
            style=discord.ButtonStyle.primary,
            custom_id=f"giveaway:enter:{giveaway_id}",
            disabled=ended,
        )
        participants_button = discord.ui.Button(
            label="Participants",
            emoji="\U0001f465",
            style=discord.ButtonStyle.secondary,
            custom_id=f"giveaway:participants:{giveaway_id}",
        )
        enter_button.callback = self.enter
        participants_button.callback = self.participants
        self.add_item(enter_button)
        self.add_item(participants_button)

    async def enter(self, interaction: discord.Interaction) -> None:
        giveaway = load_giveaways().get(self.giveaway_id)
        if not giveaway:
            await safe_send(interaction, "Giveaway not found.", ephemeral=True)
            return
        if giveaway.get("ended"):
            await safe_send(interaction, "This giveaway has ended.", ephemeral=True)
            return
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await safe_send(interaction, "Use this button inside the server.", ephemeral=True)
            return
        blocked = giveaway_entry_block_reason(interaction.guild, interaction.user, giveaway)
        if blocked:
            if member_has_role(interaction.user, giveaway.get("blacklist_role_id")):
                removed, refreshed = remove_giveaway_participant(self.giveaway_id, interaction.user.id)
                role = interaction.guild.get_role(int(giveaway.get("blacklist_role_id") or 0))
                await dm_entry_removed(interaction.user.id, refreshed or giveaway, role)
                if removed:
                    await update_giveaway_message(self.giveaway_id)
            await safe_send(interaction, blocked, ephemeral=True)
            return
        giveaways = load_giveaways()
        giveaway = giveaways[self.giveaway_id]
        participant_ids = normalize_id_list(giveaway.get("participant_ids"))
        entries = normalize_int_mapping(giveaway.get("participant_entries"), maximum=100)
        if interaction.user.id in participant_ids:
            await safe_send(
                interaction,
                'You have already entered this giveaway. If you want to leave, click "Leave Giveaway".',
                view=GiveawayLeaveConfirmView(self.giveaway_id, interaction.user.id),
                ephemeral=True,
            )
            return
        else:
            participant_ids.append(interaction.user.id)
            giveaway["participant_ids"] = participant_ids
            entries[str(interaction.user.id)] = giveaway_member_entry_count(giveaway, interaction.user)
            response = f"You entered the giveaway with **{format_count(entries[str(interaction.user.id)])}** entr{'y' if entries[str(interaction.user.id)] == 1 else 'ies'}."
        giveaway["participant_entries"] = entries
        save_giveaways(giveaways)
        await update_giveaway_message(self.giveaway_id)
        await safe_send(interaction, response, ephemeral=True)

    async def participants(self, interaction: discord.Interaction) -> None:
        giveaway = load_giveaways().get(self.giveaway_id)
        if not giveaway:
            await safe_send(interaction, "Giveaway not found.", ephemeral=True)
            return
        await safe_send(
            interaction,
            embed=build_participants_embed(giveaway),
            view=ParticipantsView(self.giveaway_id),
            ephemeral=True,
        )


async def remove_blacklisted_entries(giveaway: dict[str, Any]) -> dict[str, Any]:
    guild = bot.get_guild(int(giveaway.get("guild_id") or 0))
    role_id = parse_user_id(giveaway.get("blacklist_role_id"))
    if not guild or not role_id:
        return giveaway
    role = guild.get_role(role_id)
    participant_ids = normalize_id_list(giveaway.get("participant_ids"))
    removed: list[int] = []
    kept: list[int] = []
    entries = normalize_int_mapping(giveaway.get("participant_entries"), maximum=100)
    for user_id in participant_ids:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                kept.append(user_id)
                continue
        if role and role in member.roles:
            removed.append(user_id)
            entries.pop(str(user_id), None)
        else:
            kept.append(user_id)
    giveaway["participant_ids"] = kept
    giveaway["participant_entries"] = entries
    for user_id in removed:
        await dm_entry_removed(user_id, giveaway, role)
    return giveaway


def choose_winners(giveaway: dict[str, Any]) -> list[int]:
    participant_ids = normalize_id_list(giveaway.get("participant_ids"))
    entries = normalize_int_mapping(giveaway.get("participant_entries"), maximum=100)
    winner_count = min(20, max(1, coerce_int(giveaway.get("winners", 1), 1)))
    forced = parse_user_id(giveaway.get("forced_winner_id"))
    winners: list[int] = []
    if forced:
        winners.append(forced)
    tickets: list[int] = []
    for user_id in participant_ids:
        if user_id in winners:
            continue
        tickets.extend([user_id] * max(1, entries.get(str(user_id), 1)))
    while tickets and len(winners) < winner_count:
        user_id = random.choice(tickets)
        winners.append(user_id)
        tickets = [ticket for ticket in tickets if ticket != user_id]
    return winners[:winner_count]


async def finish_giveaway(giveaway_id: str, *, manual: bool = False) -> tuple[bool, str]:
    giveaways = load_giveaways()
    giveaway = giveaways.get(giveaway_id)
    if not giveaway:
        return False, "Giveaway not found."
    if giveaway.get("ended") and not manual:
        return True, "Giveaway already ended."
    giveaway = await remove_blacklisted_entries(giveaway)
    winners = choose_winners(giveaway)
    giveaway["ended"] = True
    giveaway["ended_at"] = int(time.time())
    giveaway["winner_ids"] = winners
    giveaways[giveaway_id] = giveaway
    save_giveaways(giveaways)
    await update_giveaway_message(giveaway_id)
    guild = bot.get_guild(int(giveaway.get("guild_id") or 0))
    winner_role_id = parse_user_id(giveaway.get("winner_role_id"))
    if guild and winner_role_id:
        role = guild.get_role(winner_role_id)
        if role:
            for user_id in winners:
                member = guild.get_member(user_id)
                if member:
                    try:
                        await member.add_roles(role, reason=f"Giveaway winner {giveaway_id}")
                    except discord.HTTPException:
                        pass
    for user_id in winners:
        await dm_giveaway_winner(user_id, giveaway)
    channel = guild.get_channel(int(giveaway.get("channel_id") or 0)) if guild else None
    if isinstance(channel, discord.TextChannel):
        try:
            if winners:
                winner_mentions = ", ".join(f"<@{user_id}>" for user_id in winners)
                result_embed = discord.Embed(
                    title="Congratulations! \U0001f389",
                    description=f"{winner_mentions} won the giveaway of **{giveaway.get('prize')}**!",
                    color=discord.Color.blue(),
                    timestamp=discord.utils.utcnow(),
                )
                if bot.user and bot.user.display_avatar:
                    result_embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
                await channel.send(embed=result_embed, view=build_giveaway_link_view(giveaway))
            else:
                await channel.send(f"Giveaway **{giveaway.get('prize')}** ended with no valid entries.")
        except discord.HTTPException:
            pass
    return True, f"Ended giveaway `{giveaway_id}`."


@tasks.loop(seconds=GIVEAWAY_CHECK_INTERVAL_SECONDS)
async def giveaway_end_task() -> None:
    now = int(time.time())
    for giveaway_id, giveaway in list(load_giveaways().items()):
        if giveaway.get("ended"):
            continue
        if int(giveaway.get("end_at") or 0) > now:
            continue
        try:
            await finish_giveaway(giveaway_id)
        except Exception as exc:
            print(f"Giveaway end failed for {giveaway_id}: {type(exc).__name__}: {exc}", flush=True)


def giveaway_attachment_url(attachment: Optional[discord.Attachment]) -> Optional[str]:
    if attachment is None:
        return None
    filename = (attachment.filename or "").lower()
    content_type = (attachment.content_type or "").lower()
    if not (content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
        return ""
    return attachment.url


def apply_giveaway_fields(
    giveaway: dict[str, Any],
    *,
    guild: Optional[discord.Guild],
    host: Optional[discord.Member] = None,
    image: Optional[discord.Attachment] = None,
    clear_image: Optional[bool] = None,
    required_role: Optional[discord.Role] = None,
    requirement_bypass_role: Optional[discord.Role] = None,
    set_giveaway_blacklist_role: Optional[discord.Role] = None,
    clear_requirements: Optional[bool] = None,
    required_daily_messages: Optional[int] = None,
    required_weekly_messages: Optional[int] = None,
    required_monthly_messages: Optional[int] = None,
    required_total_messages: Optional[int] = None,
    giveaway_winners_role: Optional[discord.Role] = None,
    extra_entries: Optional[str] = None,
    clear_extra_entries: Optional[bool] = None,
) -> Optional[str]:
    if host:
        giveaway["host_id"] = host.id
    if clear_image:
        giveaway["image_url"] = ""
    if image is not None:
        url = giveaway_attachment_url(image)
        if url == "":
            return "Image must be an uploaded image file (`png`, `jpg`, `gif`, or `webp`)."
        giveaway["image_url"] = url or ""
    if clear_requirements:
        giveaway["required_role_id"] = None
        giveaway["requirement_bypass_role_id"] = None
        giveaway["blacklist_role_id"] = None
        giveaway["required_daily_messages"] = 0
        giveaway["required_weekly_messages"] = 0
        giveaway["required_monthly_messages"] = 0
        giveaway["required_total_messages"] = 0
    if required_role is not None:
        giveaway["required_role_id"] = required_role.id
    if requirement_bypass_role is not None:
        giveaway["requirement_bypass_role_id"] = requirement_bypass_role.id
    if set_giveaway_blacklist_role is not None:
        giveaway["blacklist_role_id"] = set_giveaway_blacklist_role.id
    if giveaway_winners_role is not None:
        giveaway["winner_role_id"] = giveaway_winners_role.id
    for key, value in (
        ("required_daily_messages", required_daily_messages),
        ("required_weekly_messages", required_weekly_messages),
        ("required_monthly_messages", required_monthly_messages),
        ("required_total_messages", required_total_messages),
    ):
        if value is not None:
            giveaway[key] = max(0, int(value))
    if clear_extra_entries:
        giveaway["extra_entries"] = {}
    if extra_entries is not None:
        parsed = parse_role_entry_mapping(extra_entries, guild)
        if extra_entries.strip() and not parsed:
            return "Extra entries must use `@Role:2`, `Role Name:2`, or `role_id:2`."
        giveaway["extra_entries"] = parsed
    return None


async def create_giveaway_from_dashboard(
    *,
    guild_id: int,
    channel_id: int,
    host_id: int,
    duration: str,
    winners: int,
    prize: str,
    image_url: str = "",
    required_role_id: int = 0,
    requirement_bypass_role_id: int = 0,
    blacklist_role_id: int = 0,
    required_daily_messages: int = 0,
    required_weekly_messages: int = 0,
    required_monthly_messages: int = 0,
    required_total_messages: int = 0,
    winner_role_id: int = 0,
    extra_entries: str = "",
) -> tuple[bool, str]:
    duration_seconds = parse_duration(duration)
    if duration_seconds is None:
        return False, "Invalid duration. Use `10m`, `2h`, `3d`, or `1w`."
    prize_text = truncate(str(prize or "").strip() or "Giveaway prize", 180)
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return False, "Gem Tool is not connected to that server."
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await guild.fetch_channel(int(channel_id))
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return False, "Choose a text channel for the giveaway."
    me = guild.me
    if not me:
        return False, "I could not check my channel permissions."
    perms = channel.permissions_for(me)
    missing = [
        name for name, allowed in (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Read Message History", perms.read_message_history),
        )
        if not allowed
    ]
    if missing:
        return False, f"I am missing these permissions in #{channel.name}: {', '.join(missing)}."
    host = guild.get_member(int(host_id)) if host_id else None
    now = int(time.time())
    giveaway_id = make_giveaway_id()
    giveaway = {
        "id": giveaway_id,
        "guild_id": guild.id,
        "channel_id": channel.id,
        "message_id": 0,
        "host_id": host.id if host else (bot.user.id if bot.user else 0),
        "prize": prize_text,
        "winners": max(1, min(20, int(winners or 1))),
        "end_at": now + duration_seconds,
        "created_at": now,
        "participant_ids": [],
        "participant_entries": {},
        "forced_winner_id": None,
        "ended": False,
        "ended_at": 0,
        "winner_ids": [],
    }
    image_text = str(image_url or "").strip()
    if image_text:
        if not image_text.startswith(("http://", "https://")):
            return False, "Image URL must start with `http://` or `https://`."
        giveaway["image_url"] = image_text[:500]

    def role_or_none(role_id: int) -> Optional[discord.Role]:
        return guild.get_role(int(role_id)) if role_id else None

    error = apply_giveaway_fields(
        giveaway,
        guild=guild,
        host=host,
        required_role=role_or_none(required_role_id),
        requirement_bypass_role=role_or_none(requirement_bypass_role_id),
        set_giveaway_blacklist_role=role_or_none(blacklist_role_id),
        required_daily_messages=required_daily_messages,
        required_weekly_messages=required_weekly_messages,
        required_monthly_messages=required_monthly_messages,
        required_total_messages=required_total_messages,
        giveaway_winners_role=role_or_none(winner_role_id),
        extra_entries=extra_entries,
    )
    if error:
        return False, error
    try:
        message = await channel.send(embed=build_giveaway_embed(giveaway), view=GiveawayView(giveaway_id))
    except discord.HTTPException as exc:
        return False, f"Could not send giveaway: {exc}"
    giveaway["message_id"] = message.id
    giveaways = load_giveaways()
    giveaways[giveaway_id] = giveaway
    save_giveaways(giveaways)
    register_giveaway_view(giveaway_id)
    return True, f"Giveaway created in #{channel.name}. ID: `{giveaway_id}`"


async def ensure_channel_permissions(interaction: discord.Interaction, channel: Any) -> bool:
    if not isinstance(channel, discord.TextChannel):
        await safe_send(interaction, "Choose a text channel for the giveaway.", ephemeral=True)
        return False
    if not channel.guild.me:
        await safe_send(interaction, "I could not check my channel permissions.", ephemeral=True)
        return False
    perms = channel.permissions_for(channel.guild.me)
    missing = [
        name for name, allowed in (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Read Message History", perms.read_message_history),
        )
        if not allowed
    ]
    if missing:
        await safe_send(interaction, f"I am missing these permissions in {channel.mention}: {', '.join(missing)}.", ephemeral=True)
        return False
    return True


async def get_giveaway_for_command(interaction: discord.Interaction, value: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    giveaway_id, giveaway = find_giveaway_by_reference(value, interaction.guild_id)
    if giveaway_id and giveaway:
        return giveaway_id, giveaway
    await safe_send(interaction, "Giveaway not found in this server.", ephemeral=True)
    return None, None


giveaway_group = app_commands.Group(name="giveaway", description="Create and manage giveaways")


@giveaway_group.command(name="create", description="Create a giveaway")
@app_commands.guild_only()
@app_commands.describe(
    duration="How long it runs, like 10m, 2h, 3d, or 1w",
    winners="The number of winners",
    prize="The prize",
    channel="The channel this giveaway will be created in",
    host="Visible host",
    image="Image upload shown at the bottom of the embed",
    required_role="Role required to participate",
    requirement_bypass_role="Role that bypasses requirements",
    set_giveaway_blacklist_role="Role that is not allowed to participate",
    required_daily_messages="Messages required today",
    required_weekly_messages="Messages required this week",
    required_monthly_messages="Messages required this month",
    required_total_messages="Messages required in total",
    giveaway_winners_role="Role to give winners",
    extra_entries="Role boosts like @Booster:2, Donator:4",
)
async def giveaway_create_slash(
    interaction: discord.Interaction,
    duration: str,
    winners: app_commands.Range[int, 1, 20],
    prize: str,
    channel: Optional[discord.TextChannel] = None,
    host: Optional[discord.Member] = None,
    image: Optional[discord.Attachment] = None,
    required_role: Optional[discord.Role] = None,
    requirement_bypass_role: Optional[discord.Role] = None,
    set_giveaway_blacklist_role: Optional[discord.Role] = None,
    required_daily_messages: Optional[int] = None,
    required_weekly_messages: Optional[int] = None,
    required_monthly_messages: Optional[int] = None,
    required_total_messages: Optional[int] = None,
    giveaway_winners_role: Optional[discord.Role] = None,
    extra_entries: Optional[str] = None,
) -> None:
    if not await require_giveaway_creator(interaction):
        return
    duration_seconds = parse_duration(duration)
    if duration_seconds is None:
        await safe_send(interaction, "Invalid duration. Use `10m`, `2h`, `3d`, or `1w`.", ephemeral=True)
        return
    target_channel = channel or interaction.channel
    if not await ensure_channel_permissions(interaction, target_channel):
        return
    giveaway_id = make_giveaway_id()
    now = int(time.time())
    giveaway = {
        "id": giveaway_id,
        "guild_id": interaction.guild_id,
        "channel_id": target_channel.id,
        "message_id": 0,
        "host_id": host.id if host else interaction.user.id,
        "prize": truncate(prize.strip() or "Giveaway prize", 180),
        "winners": int(winners),
        "end_at": now + duration_seconds,
        "created_at": now,
        "participant_ids": [],
        "participant_entries": {},
        "forced_winner_id": None,
        "ended": False,
        "ended_at": 0,
        "winner_ids": [],
    }
    error = apply_giveaway_fields(
        giveaway,
        guild=interaction.guild,
        host=host,
        image=image,
        required_role=required_role,
        requirement_bypass_role=requirement_bypass_role,
        set_giveaway_blacklist_role=set_giveaway_blacklist_role,
        required_daily_messages=required_daily_messages,
        required_weekly_messages=required_weekly_messages,
        required_monthly_messages=required_monthly_messages,
        required_total_messages=required_total_messages,
        giveaway_winners_role=giveaway_winners_role,
        extra_entries=extra_entries,
    )
    if error:
        await safe_send(interaction, error, ephemeral=True)
        return
    message = await target_channel.send(embed=build_giveaway_embed(giveaway), view=GiveawayView(giveaway_id))
    giveaway["message_id"] = message.id
    giveaways = load_giveaways()
    giveaways[giveaway_id] = giveaway
    save_giveaways(giveaways)
    register_giveaway_view(giveaway_id)
    await safe_send(interaction, f"Giveaway created in {target_channel.mention}. ID: `{giveaway_id}`", ephemeral=True)


@giveaway_group.command(name="edit", description="Edit a giveaway")
@app_commands.guild_only()
@app_commands.describe(
    giveaway_id="The giveaway ID or message ID",
    prize="New prize",
    duration="New duration from now",
    winners="New number of winners",
    channel="Move to a different text channel",
    image="New image upload",
    clear_image="Remove the giveaway image",
    required_role="New required role",
    requirement_bypass_role="New bypass role",
    set_giveaway_blacklist_role="New blacklisted role",
    clear_requirements="Clear role and message requirements",
    required_daily_messages="Messages required today. Use 0 to clear",
    required_weekly_messages="Messages required this week. Use 0 to clear",
    required_monthly_messages="Messages required this month. Use 0 to clear",
    required_total_messages="Messages required total. Use 0 to clear",
    giveaway_winners_role="New winner role",
    extra_entries="New role boosts like @Booster:2, Donator:4",
    clear_extra_entries="Remove all role extra entries",
)
async def giveaway_edit_slash(
    interaction: discord.Interaction,
    giveaway_id: str,
    prize: Optional[str] = None,
    duration: Optional[str] = None,
    winners: Optional[int] = None,
    channel: Optional[discord.TextChannel] = None,
    image: Optional[discord.Attachment] = None,
    clear_image: Optional[bool] = None,
    required_role: Optional[discord.Role] = None,
    requirement_bypass_role: Optional[discord.Role] = None,
    set_giveaway_blacklist_role: Optional[discord.Role] = None,
    clear_requirements: Optional[bool] = None,
    required_daily_messages: Optional[int] = None,
    required_weekly_messages: Optional[int] = None,
    required_monthly_messages: Optional[int] = None,
    required_total_messages: Optional[int] = None,
    giveaway_winners_role: Optional[discord.Role] = None,
    extra_entries: Optional[str] = None,
    clear_extra_entries: Optional[bool] = None,
) -> None:
    if not await require_giveaway_manager(interaction):
        return
    resolved_id, giveaway = await get_giveaway_for_command(interaction, giveaway_id)
    if not resolved_id or not giveaway:
        return
    if giveaway.get("ended"):
        await safe_send(interaction, "This giveaway already ended. Use `/giveaway reroll`.", ephemeral=True)
        return
    if prize is not None:
        giveaway["prize"] = truncate(prize.strip() or "Giveaway prize", 180)
    if duration is not None:
        seconds = parse_duration(duration)
        if seconds is None:
            await safe_send(interaction, "Invalid duration. Use `10m`, `2h`, `3d`, or `1w`.", ephemeral=True)
            return
        giveaway["end_at"] = int(time.time()) + seconds
    if winners is not None:
        giveaway["winners"] = min(20, max(1, int(winners)))
    error = apply_giveaway_fields(
        giveaway,
        guild=interaction.guild,
        image=image,
        clear_image=clear_image,
        required_role=required_role,
        requirement_bypass_role=requirement_bypass_role,
        set_giveaway_blacklist_role=set_giveaway_blacklist_role,
        clear_requirements=clear_requirements,
        required_daily_messages=required_daily_messages,
        required_weekly_messages=required_weekly_messages,
        required_monthly_messages=required_monthly_messages,
        required_total_messages=required_total_messages,
        giveaway_winners_role=giveaway_winners_role,
        extra_entries=extra_entries,
        clear_extra_entries=clear_extra_entries,
    )
    if error:
        await safe_send(interaction, error, ephemeral=True)
        return
    if channel and channel.id != int(giveaway.get("channel_id") or 0):
        if not await ensure_channel_permissions(interaction, channel):
            return
        old_message = await fetch_giveaway_message(giveaway)
        new_message = await channel.send(embed=build_giveaway_embed(giveaway), view=GiveawayView(resolved_id))
        giveaway["channel_id"] = channel.id
        giveaway["message_id"] = new_message.id
        if old_message:
            try:
                await old_message.delete()
            except discord.HTTPException:
                pass
    giveaways = load_giveaways()
    giveaways[resolved_id] = giveaway
    save_giveaways(giveaways)
    register_giveaway_view(resolved_id)
    await update_giveaway_message(resolved_id)
    await safe_send(interaction, f"Edited giveaway `{resolved_id}`.", ephemeral=True)


@giveaway_group.command(name="delete", description="Delete a giveaway")
@app_commands.guild_only()
async def giveaway_delete_slash(interaction: discord.Interaction, giveaway_id: str) -> None:
    if not await require_giveaway_manager(interaction):
        return
    resolved_id, giveaway = await get_giveaway_for_command(interaction, giveaway_id)
    if not resolved_id or not giveaway:
        return
    message = await fetch_giveaway_message(giveaway)
    if message:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    giveaways = load_giveaways()
    giveaways.pop(resolved_id, None)
    save_giveaways(giveaways)
    await safe_send(interaction, f"Deleted giveaway `{resolved_id}`.", ephemeral=True)


@giveaway_group.command(name="end", description="End a giveaway early")
@app_commands.guild_only()
async def giveaway_end_slash(interaction: discord.Interaction, giveaway_id: str) -> None:
    if not await require_giveaway_manager(interaction):
        return
    resolved_id, giveaway = await get_giveaway_for_command(interaction, giveaway_id)
    if not resolved_id or not giveaway:
        return
    success, message = await finish_giveaway(resolved_id, manual=True)
    await safe_send(interaction, message if success else f"Could not end giveaway: {message}", ephemeral=True)


@giveaway_group.command(name="fix", description="Re-render a giveaway if the message view is broken")
@app_commands.guild_only()
async def giveaway_fix_slash(interaction: discord.Interaction, giveaway_id: str) -> None:
    if not await require_giveaway_manager(interaction):
        return
    resolved_id, giveaway = await get_giveaway_for_command(interaction, giveaway_id)
    if not resolved_id or not giveaway:
        return
    await update_giveaway_message(resolved_id)
    await safe_send(interaction, f"Fixed giveaway `{resolved_id}`.", ephemeral=True)


@giveaway_group.command(name="reroll", description="Reroll winners for an ended giveaway")
@app_commands.guild_only()
async def giveaway_reroll_slash(interaction: discord.Interaction, giveaway_id: str) -> None:
    if not await require_giveaway_manager(interaction):
        return
    resolved_id, giveaway = await get_giveaway_for_command(interaction, giveaway_id)
    if not resolved_id or not giveaway:
        return
    if not giveaway.get("ended"):
        await safe_send(interaction, "This giveaway has not ended yet.", ephemeral=True)
        return
    giveaway["winner_ids"] = choose_winners(giveaway)
    load_giveaways()[resolved_id] = giveaway
    save_giveaways(load_giveaways())
    await update_giveaway_message(resolved_id)
    for user_id in giveaway["winner_ids"]:
        await dm_giveaway_winner(user_id, giveaway)
    await safe_send(interaction, f"Rerolled giveaway `{resolved_id}`.", ephemeral=True)


@giveaway_group.command(name="participants", description="Show giveaway participants")
@app_commands.guild_only()
async def giveaway_participants_slash(interaction: discord.Interaction, giveaway_id: str) -> None:
    if not await require_giveaway_manager(interaction):
        return
    resolved_id, giveaway = await get_giveaway_for_command(interaction, giveaway_id)
    if not resolved_id or not giveaway:
        return
    await safe_send(interaction, embed=build_participants_embed(giveaway), view=ParticipantsView(resolved_id), ephemeral=True)


@giveaway_group.command(name="remove-participant", description="Remove a participant from a giveaway")
@app_commands.guild_only()
async def giveaway_remove_participant_slash(interaction: discord.Interaction, giveaway_id: str, user: discord.Member) -> None:
    if not await require_giveaway_manager(interaction):
        return
    resolved_id, giveaway = await get_giveaway_for_command(interaction, giveaway_id)
    if not resolved_id or not giveaway:
        return
    removed, _ = remove_giveaway_participant(resolved_id, user.id)
    if not removed:
        await safe_send(interaction, f"{user.mention} is not participating in that giveaway.", ephemeral=True)
        return
    await update_giveaway_message(resolved_id)
    await safe_send(interaction, f"Removed {user.mention} from giveaway `{resolved_id}`.", ephemeral=True)


async def update_giveaway_role_setting(interaction: discord.Interaction, setting_key: str, action: str, role: Optional[discord.Role]) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await safe_send(interaction, "This command can only be used in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await safe_send(interaction, "Only server admins can edit giveaway role settings.", ephemeral=True)
        return
    settings = load_giveaway_settings()
    guild_settings = get_giveaway_settings(interaction.guild.id)
    role_ids = set(normalize_id_list(guild_settings.get(setting_key)))
    normalized_action = action.lower().replace(" ", "-")
    if normalized_action == "clear":
        role_ids.clear()
    elif normalized_action in {"add", "set"} and role is not None:
        role_ids.add(role.id)
    elif normalized_action == "remove" and role is not None:
        role_ids.discard(role.id)
    else:
        await safe_send(interaction, "Use action `add`, `remove`, or `clear` and choose a role when needed.", ephemeral=True)
        return
    guild_settings[setting_key] = sorted(role_ids)
    settings.setdefault("guilds", {})[str(interaction.guild.id)] = guild_settings
    save_giveaway_settings(settings)
    mentions = ", ".join(format_role(interaction.guild, role_id) for role_id in sorted(role_ids)) or "none"
    await safe_send(interaction, f"Updated {setting_key.replace('_', ' ')}: {mentions}", ephemeral=True)


@giveaway_group.command(name="creator-roles", description="Set roles that can create giveaways")
@app_commands.guild_only()
@app_commands.describe(action="add, remove, or clear", role="Role to add/remove")
async def giveaway_creator_roles_slash(interaction: discord.Interaction, action: str, role: Optional[discord.Role] = None) -> None:
    await update_giveaway_role_setting(interaction, "creator_role_ids", action, role)


@giveaway_group.command(name="manager-roles", description="Set roles that can use every giveaway command")
@app_commands.guild_only()
@app_commands.describe(action="add, remove, or clear", role="Role to add/remove")
async def giveaway_manager_roles_slash(interaction: discord.Interaction, action: str, role: Optional[discord.Role] = None) -> None:
    await update_giveaway_role_setting(interaction, "manager_role_ids", action, role)


bot.tree.add_command(giveaway_group)


async def require_application_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await safe_send(interaction, "This command can only be used in a server.", ephemeral=True)
        return False
    if not interaction.user.guild_permissions.manage_guild:
        await safe_send(interaction, "You need Manage Server permission to manage applications.", ephemeral=True)
        return False
    return True


async def application_panel_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild_id:
        return []
    panels = application_system.get_guild_state(interaction.guild_id).get("panels", {})
    current_lower = current.lower()
    choices = []
    for key, panel in panels.items():
        name = str(panel.get("name", key))
        if current_lower in key.lower() or current_lower in name.lower():
            choices.append(app_commands.Choice(name=name[:100], value=key[:100]))
    return choices[:25]


async def suggestion_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild_id:
        return []
    current_lower = current.lower()
    choices = []
    for key, suggestion in sorted(load_suggestions().items(), key=lambda item: coerce_int(item[1].get("number")), reverse=True):
        if int(suggestion.get("guild_id") or 0) != interaction.guild_id:
            continue
        number = str(suggestion.get("number") or "")
        content = str(suggestion.get("content") or "")
        label = f"#{number} - {content[:72]}"
        if current_lower in key.lower() or current_lower in number or current_lower in content.lower():
            choices.append(app_commands.Choice(name=label[:100], value=number[:100]))
    return choices[:25]


async def reaction_role_panel_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild_id:
        return []
    guild_state = get_reaction_role_guild_state(interaction.guild_id)
    current_lower = current.lower()
    choices = []
    for panel_id, panel in guild_state.get("panels", {}).items():
        name = str(panel.get("name") or panel_id)
        if current_lower in panel_id.lower() or current_lower in name.lower():
            choices.append(app_commands.Choice(name=f"{name} ({panel_id})"[:100], value=panel_id[:100]))
    return choices[:25]


suggestion_group = app_commands.Group(name="suggestion", description="Create and manage suggestions")


@suggestion_group.command(name="suggest", description="Create a suggestion")
@app_commands.guild_only()
async def suggestion_suggest_slash(interaction: discord.Interaction, content: str) -> None:
    ok, message = await post_new_suggestion(interaction, content)
    await safe_send(interaction, message if ok else f"Suggestion could not be posted: {message}", ephemeral=True)


@suggestion_group.command(name="channel", description="Set the public suggestion channel")
@app_commands.guild_only()
async def suggestion_channel_slash(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await require_application_admin(interaction):
        return
    set_suggestion_config(interaction.guild.id, channel_id=channel.id)
    await safe_send(interaction, f"Suggestion channel set to {channel.mention}.", ephemeral=True)


@suggestion_group.command(name="submit", description="Set where users should submit suggestions")
@app_commands.guild_only()
async def suggestion_submit_slash(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
    if not await require_application_admin(interaction):
        return
    set_suggestion_config(interaction.guild.id, submit_channel_id=channel.id if channel else 0)
    message = f"Suggestion submission channel set to {channel.mention}." if channel else "Users can submit suggestions in any channel."
    await safe_send(interaction, message, ephemeral=True)


@suggestion_group.command(name="move", description="Set the channel that receives moderation result copies")
@app_commands.guild_only()
async def suggestion_move_slash(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
    if not await require_application_admin(interaction):
        return
    set_suggestion_config(interaction.guild.id, move_channel_id=channel.id if channel else 0)
    message = f"Suggestion result copies will be sent to {channel.mention}." if channel else "Suggestion result copy channel cleared."
    await safe_send(interaction, message, ephemeral=True)


@suggestion_group.command(name="anonymous", description="Toggle anonymous suggestions")
@app_commands.guild_only()
async def suggestion_anonymous_slash(interaction: discord.Interaction, enabled: bool) -> None:
    if not await require_application_admin(interaction):
        return
    set_suggestion_config(interaction.guild.id, anonymous=enabled)
    await safe_send(interaction, f"Anonymous suggestions are now {'enabled' if enabled else 'disabled'}.", ephemeral=True)


@suggestion_group.command(name="dm", description="Toggle DMs when moderators decide on suggestions")
@app_commands.guild_only()
async def suggestion_dm_slash(interaction: discord.Interaction, enabled: bool) -> None:
    if not await require_application_admin(interaction):
        return
    set_suggestion_config(interaction.guild.id, dm_results=enabled)
    await safe_send(interaction, f"Suggestion result DMs are now {'enabled' if enabled else 'disabled'}.", ephemeral=True)


@suggestion_group.command(name="server", description="Show this server's suggestion config")
@app_commands.guild_only()
async def suggestion_server_slash(interaction: discord.Interaction) -> None:
    if not await require_application_admin(interaction):
        return
    settings = get_suggestion_settings(interaction.guild.id)
    guild_suggestions = [
        suggestion
        for suggestion in load_suggestions().values()
        if int(suggestion.get("guild_id") or 0) == interaction.guild.id
    ]
    pending = sum(1 for suggestion in guild_suggestions if suggestion.get("status") in {"pending", "considered"})
    embed = discord.Embed(
        title="Suggestion Settings",
        description=(
            f"Public channel: {f'<#{settings.get('channel_id')}>' if settings.get('channel_id') else 'current channel fallback'}\n"
            f"Submit channel: {f'<#{settings.get('submit_channel_id')}>' if settings.get('submit_channel_id') else 'any channel'}\n"
            f"Result copy channel: {f'<#{settings.get('move_channel_id')}>' if settings.get('move_channel_id') else 'disabled'}\n"
            f"Anonymous: **{'on' if settings.get('anonymous') else 'off'}**\n"
            f"DM results: **{'on' if settings.get('dm_results') else 'off'}**\n"
            f"Stored suggestions: **{format_count(len(guild_suggestions))}** ({format_count(pending)} open)"
        ),
        color=discord.Color.blurple(),
    )
    await safe_send(interaction, embed=embed, ephemeral=True)


@suggestion_group.command(name="who", description="Show who created a suggestion")
@app_commands.guild_only()
@app_commands.autocomplete(suggestion=suggestion_autocomplete)
async def suggestion_who_slash(interaction: discord.Interaction, suggestion: str) -> None:
    if not await require_application_admin(interaction):
        return
    _key, record = find_suggestion(interaction.guild.id, suggestion)
    if not record:
        await safe_send(interaction, "Suggestion not found.", ephemeral=True)
        return
    await safe_send(interaction, f"Suggestion #{record.get('number')} was created by <@{record.get('author_id')}>.", ephemeral=True)


@suggestion_group.command(name="edit", description="Edit the text of a suggestion")
@app_commands.guild_only()
@app_commands.autocomplete(suggestion=suggestion_autocomplete)
async def suggestion_edit_slash(interaction: discord.Interaction, suggestion: str, content: str) -> None:
    if not await require_application_admin(interaction):
        return
    key, record = find_suggestion(interaction.guild.id, suggestion)
    if not key or not record:
        await safe_send(interaction, "Suggestion not found.", ephemeral=True)
        return
    suggestions = load_suggestions()
    record["content"] = truncate(content.strip(), 1500)
    suggestions[key] = record
    save_suggestions(suggestions)
    await update_suggestion_message(key)
    await safe_send(interaction, f"Suggestion #{record.get('number')} updated.", ephemeral=True)


@suggestion_group.command(name="approve", description="Mark a suggestion as approved")
@app_commands.guild_only()
@app_commands.autocomplete(suggestion=suggestion_autocomplete)
async def suggestion_approve_slash(interaction: discord.Interaction, suggestion: str, reason: Optional[str] = None) -> None:
    if not await require_application_admin(interaction):
        return
    await set_suggestion_status(interaction, suggestion, "approved", reason)


@suggestion_group.command(name="consider", description="Mark a suggestion as being considered")
@app_commands.guild_only()
@app_commands.autocomplete(suggestion=suggestion_autocomplete)
async def suggestion_consider_slash(interaction: discord.Interaction, suggestion: str, reason: Optional[str] = None) -> None:
    if not await require_application_admin(interaction):
        return
    await set_suggestion_status(interaction, suggestion, "considered", reason)


@suggestion_group.command(name="deny", description="Deny a suggestion")
@app_commands.guild_only()
@app_commands.autocomplete(suggestion=suggestion_autocomplete)
async def suggestion_deny_slash(interaction: discord.Interaction, suggestion: str, reason: Optional[str] = None) -> None:
    if not await require_application_admin(interaction):
        return
    await set_suggestion_status(interaction, suggestion, "denied", reason)


@suggestion_group.command(name="implemented", description="Mark a suggestion as implemented")
@app_commands.guild_only()
@app_commands.autocomplete(suggestion=suggestion_autocomplete)
async def suggestion_implemented_slash(interaction: discord.Interaction, suggestion: str, reason: Optional[str] = None) -> None:
    if not await require_application_admin(interaction):
        return
    await set_suggestion_status(interaction, suggestion, "implemented", reason)


bot.tree.add_command(suggestion_group)


reaction_role_group = app_commands.Group(name="reaction-role", description="Create and manage button reaction roles")


@reaction_role_group.command(name="create", description="Create a reaction-role panel")
@app_commands.guild_only()
async def reaction_role_create_slash(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    name: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    allow_multiple: bool = True,
) -> None:
    if not await require_application_admin(interaction):
        return
    panel = create_reaction_role_panel_record(
        interaction.guild.id,
        name=name,
        channel_id=channel.id,
        title=title or name,
        description=description or "Click a button to toggle a role.",
        allow_multiple=allow_multiple,
    )
    ok, message = await post_reaction_role_panel(interaction.guild, panel["id"], channel.id)
    await safe_send(interaction, f"{message} Panel ID: `{panel['id']}`" if ok else message, ephemeral=True)


@reaction_role_group.command(name="settings", description="Update a reaction-role panel")
@app_commands.guild_only()
@app_commands.autocomplete(panel=reaction_role_panel_autocomplete)
async def reaction_role_settings_slash(
    interaction: discord.Interaction,
    panel: str,
    channel: Optional[discord.TextChannel] = None,
    name: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    allow_multiple: Optional[bool] = None,
) -> None:
    if not await require_application_admin(interaction):
        return
    try:
        record = update_reaction_role_panel_record(
            interaction.guild.id,
            panel,
            name=name,
            channel_id=channel.id if channel else None,
            title=title,
            description=description,
            allow_multiple=allow_multiple,
        )
    except ValueError as exc:
        await safe_send(interaction, str(exc), ephemeral=True)
        return
    if record.get("message_id"):
        await post_reaction_role_panel(interaction.guild, panel)
    await safe_send(interaction, f"Reaction-role panel `{panel}` updated.", ephemeral=True)


@reaction_role_group.command(name="add", description="Add or update a role button")
@app_commands.guild_only()
@app_commands.autocomplete(panel=reaction_role_panel_autocomplete)
@app_commands.choices(
    style=[
        app_commands.Choice(name="Blue", value="primary"),
        app_commands.Choice(name="Gray", value="secondary"),
        app_commands.Choice(name="Green", value="success"),
        app_commands.Choice(name="Red", value="danger"),
    ]
)
async def reaction_role_add_slash(
    interaction: discord.Interaction,
    panel: str,
    role: discord.Role,
    label: Optional[str] = None,
    emoji: Optional[str] = None,
    style: str = "primary",
) -> None:
    if not await require_application_admin(interaction):
        return
    allowed, reason = application_system.bot_can_manage_role(interaction.guild, role)
    if not allowed:
        await safe_send(interaction, f"I cannot manage that role: {reason}.", ephemeral=True)
        return
    try:
        add_reaction_role_item(
            interaction.guild.id,
            panel,
            role_id=role.id,
            label=label or role.name,
            emoji=emoji or "",
            style=style,
        )
    except ValueError as exc:
        await safe_send(interaction, str(exc), ephemeral=True)
        return
    await post_reaction_role_panel(interaction.guild, panel)
    await safe_send(interaction, f"Added {role.mention} to reaction-role panel `{panel}`.", ephemeral=True)


@reaction_role_group.command(name="remove", description="Remove a role button")
@app_commands.guild_only()
@app_commands.autocomplete(panel=reaction_role_panel_autocomplete)
async def reaction_role_remove_slash(interaction: discord.Interaction, panel: str, role: discord.Role) -> None:
    if not await require_application_admin(interaction):
        return
    try:
        remove_reaction_role_item(interaction.guild.id, panel, role.id)
    except ValueError as exc:
        await safe_send(interaction, str(exc), ephemeral=True)
        return
    await post_reaction_role_panel(interaction.guild, panel)
    await safe_send(interaction, f"Removed {role.mention} from panel `{panel}`.", ephemeral=True)


@reaction_role_group.command(name="post", description="Post or refresh a reaction-role panel")
@app_commands.guild_only()
@app_commands.autocomplete(panel=reaction_role_panel_autocomplete)
async def reaction_role_post_slash(interaction: discord.Interaction, panel: str, channel: Optional[discord.TextChannel] = None) -> None:
    if not await require_application_admin(interaction):
        return
    ok, message = await post_reaction_role_panel(interaction.guild, panel, channel.id if channel else None)
    await safe_send(interaction, message if ok else f"Reaction-role panel could not be posted: {message}", ephemeral=True)


@reaction_role_group.command(name="list", description="List reaction-role panels")
@app_commands.guild_only()
async def reaction_role_list_slash(interaction: discord.Interaction) -> None:
    if not await require_application_admin(interaction):
        return
    guild_state = get_reaction_role_guild_state(interaction.guild.id)
    lines = []
    for panel_id, panel in sorted(guild_state.get("panels", {}).items()):
        lines.append(
            f"`{panel_id}` - **{panel.get('name', panel_id)}** "
            f"({format_count(len(panel.get('items', [])))} button{'s' if len(panel.get('items', [])) != 1 else ''})"
        )
    embed = discord.Embed(
        title="Reaction Role Panels",
        description="\n".join(lines) if lines else "No reaction-role panels yet.",
        color=discord.Color.teal(),
    )
    await safe_send(interaction, embed=embed, ephemeral=True)


@reaction_role_group.command(name="delete", description="Delete a reaction-role panel")
@app_commands.guild_only()
@app_commands.autocomplete(panel=reaction_role_panel_autocomplete)
async def reaction_role_delete_slash(interaction: discord.Interaction, panel: str) -> None:
    if not await require_application_admin(interaction):
        return
    record = delete_reaction_role_panel_record(interaction.guild.id, panel)
    if not record:
        await safe_send(interaction, "Reaction-role panel not found.", ephemeral=True)
        return
    message = await fetch_reaction_role_message(record)
    if message:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    await safe_send(interaction, f"Reaction-role panel `{panel}` deleted.", ephemeral=True)


@reaction_role_group.command(name="allowed-role", description="Set roles that may use a reaction-role panel")
@app_commands.guild_only()
@app_commands.autocomplete(panel=reaction_role_panel_autocomplete)
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="clear", value="clear")])
async def reaction_role_allowed_role_slash(interaction: discord.Interaction, panel: str, action: str, role: Optional[discord.Role] = None) -> None:
    if not await require_application_admin(interaction):
        return
    try:
        set_reaction_role_access_role(interaction.guild.id, panel, list_name="allowed_role_ids", action=action, role_id=role.id if role else None)
    except ValueError as exc:
        await safe_send(interaction, str(exc), ephemeral=True)
        return
    await safe_send(interaction, "Allowed-role settings updated.", ephemeral=True)


@reaction_role_group.command(name="ignored-role", description="Set roles blocked from using a reaction-role panel")
@app_commands.guild_only()
@app_commands.autocomplete(panel=reaction_role_panel_autocomplete)
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="clear", value="clear")])
async def reaction_role_ignored_role_slash(interaction: discord.Interaction, panel: str, action: str, role: Optional[discord.Role] = None) -> None:
    if not await require_application_admin(interaction):
        return
    try:
        set_reaction_role_access_role(interaction.guild.id, panel, list_name="ignored_role_ids", action=action, role_id=role.id if role else None)
    except ValueError as exc:
        await safe_send(interaction, str(exc), ephemeral=True)
        return
    await safe_send(interaction, "Ignored-role settings updated.", ephemeral=True)


bot.tree.add_command(reaction_role_group)


application_group = app_commands.Group(name="application", description="Manage application panels")


@application_group.command(name="panel", description="Post or refresh the application dropdown panel")
@app_commands.guild_only()
async def application_panel_slash(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await require_application_admin(interaction):
        return
    ok, message = await application_system.post_application_panel(interaction.guild, channel.id)
    await safe_send(interaction, message if ok else f"Application panel could not be posted: {message}", ephemeral=True)


@application_group.command(name="log", description="Set the application review/log channel")
@app_commands.guild_only()
async def application_log_slash(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await require_application_admin(interaction):
        return
    missing = application_system.bot_channel_permission_errors(channel)
    if missing:
        await safe_send(interaction, f"I am missing permissions in {channel.mention}: {', '.join(missing)}.", ephemeral=True)
        return
    guild_state = application_system.get_guild_state(interaction.guild.id)
    guild_state["log_channel_id"] = channel.id
    application_system.save_state()
    await safe_send(interaction, f"Application log channel set to {channel.mention}.", ephemeral=True)


@application_group.command(name="text", description="Set the text shown above the application dropdown")
@app_commands.guild_only()
async def application_text_slash(interaction: discord.Interaction, text: str) -> None:
    if not await require_application_admin(interaction):
        return
    guild_state = application_system.get_guild_state(interaction.guild.id)
    guild_state["panel_text"] = truncate(text.strip() or application_system.DEFAULT_PANEL_TEXT, 1000)
    application_system.save_state()
    await application_system.refresh_application_message(interaction.guild)
    await safe_send(interaction, "Application panel text updated.", ephemeral=True)


@application_group.command(name="create-panel", description="Create an application dropdown option")
@app_commands.guild_only()
async def application_create_panel_slash(interaction: discord.Interaction, name: str, description: Optional[str] = None) -> None:
    if not await require_application_admin(interaction):
        return
    panel_key = application_system.normalize_panel_key(name)
    if not panel_key:
        await safe_send(interaction, "Panel name cannot be empty.", ephemeral=True)
        return
    guild_state = application_system.get_guild_state(interaction.guild.id)
    panels = guild_state.setdefault("panels", {})
    if panel_key in panels:
        await safe_send(interaction, "That panel already exists.", ephemeral=True)
        return
    panels[panel_key] = {"name": truncate(name, 80), "description": truncate(description or "", 100), "questions": []}
    application_system.save_state()
    await application_system.refresh_application_message(interaction.guild)
    await safe_send(interaction, f"Created application panel `{panel_key}`.", ephemeral=True)


@application_group.command(name="edit-panel", description="Edit an application dropdown option")
@app_commands.guild_only()
@app_commands.autocomplete(panel=application_panel_autocomplete)
async def application_edit_panel_slash(
    interaction: discord.Interaction,
    panel: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    accepted_role: Optional[discord.Role] = None,
    clear_accepted_role: Optional[bool] = None,
) -> None:
    if not await require_application_admin(interaction):
        return
    panels = application_system.get_guild_state(interaction.guild.id).setdefault("panels", {})
    panel_key = application_system.normalize_panel_key(panel)
    if panel_key not in panels:
        await safe_send(interaction, "Unknown panel.", ephemeral=True)
        return
    panel_data = panels[panel_key]
    if name is not None:
        panel_data["name"] = truncate(name.strip() or panel_data.get("name", panel_key), 80)
    if description is not None:
        panel_data["description"] = truncate(description.strip(), 100)
    if clear_accepted_role:
        panel_data.pop("accepted_role_id", None)
    if accepted_role is not None:
        allowed, reason = application_system.bot_can_manage_role(interaction.guild, accepted_role)
        if not allowed:
            await safe_send(interaction, f"I cannot give that role: {reason}.", ephemeral=True)
            return
        panel_data["accepted_role_id"] = accepted_role.id
    application_system.save_state()
    await application_system.refresh_application_message(interaction.guild)
    await safe_send(interaction, f"Updated application panel `{panel_key}`.", ephemeral=True)


@application_group.command(name="delete-panel", description="Delete an application dropdown option")
@app_commands.guild_only()
@app_commands.autocomplete(panel=application_panel_autocomplete)
async def application_delete_panel_slash(interaction: discord.Interaction, panel: str) -> None:
    if not await require_application_admin(interaction):
        return
    panels = application_system.get_guild_state(interaction.guild.id).setdefault("panels", {})
    panel_key = application_system.normalize_panel_key(panel)
    if panel_key not in panels:
        await safe_send(interaction, "Unknown panel.", ephemeral=True)
        return
    panels.pop(panel_key, None)
    application_system.save_state()
    await application_system.refresh_application_message(interaction.guild)
    await safe_send(interaction, f"Deleted application panel `{panel_key}`.", ephemeral=True)


@application_group.command(name="add-question", description="Add a text or dropdown question to a panel")
@app_commands.guild_only()
@app_commands.autocomplete(panel=application_panel_autocomplete)
async def application_add_question_slash(
    interaction: discord.Interaction,
    panel: str,
    question_number: int,
    text: str,
    choices: Optional[str] = None,
) -> None:
    if not await require_application_admin(interaction):
        return
    panels = application_system.get_guild_state(interaction.guild.id).setdefault("panels", {})
    panel_key = application_system.normalize_panel_key(panel)
    if panel_key not in panels:
        await safe_send(interaction, "Unknown panel.", ephemeral=True)
        return
    parsed_choices = application_system.parse_question_choices(choices)
    if choices and parsed_choices is None:
        await safe_send(interaction, "Selection questions need at least 2 choices. Use `yes|no` or leave choices empty.", ephemeral=True)
        return
    questions = panels[panel_key].setdefault("questions", [])
    insert_index = max(0, min(len(questions), int(question_number) - 1))
    questions.insert(insert_index, application_system.make_question_value(text.strip(), choices))
    application_system.save_state()
    await safe_send(interaction, "Question added and questions were renumbered.", ephemeral=True)


@application_group.command(name="edit-question", description="Edit a text or dropdown question")
@app_commands.guild_only()
@app_commands.autocomplete(panel=application_panel_autocomplete)
async def application_edit_question_slash(
    interaction: discord.Interaction,
    panel: str,
    question_number: int,
    text: Optional[str] = None,
    choices: Optional[str] = None,
    clear_choices: Optional[bool] = None,
) -> None:
    if not await require_application_admin(interaction):
        return
    panels = application_system.get_guild_state(interaction.guild.id).setdefault("panels", {})
    panel_key = application_system.normalize_panel_key(panel)
    questions = panels.get(panel_key, {}).get("questions", [])
    index = int(question_number) - 1
    if panel_key not in panels or index < 0 or index >= len(questions):
        await safe_send(interaction, "That question number does not exist.", ephemeral=True)
        return
    current = application_system.normalize_question(questions[index])
    final_text = text.strip() if text is not None else current["text"]
    final_choices = "" if clear_choices else (choices if choices is not None else "|".join(current.get("choices", [])))
    if final_choices and application_system.parse_question_choices(final_choices) is None:
        await safe_send(interaction, "Selection questions need at least 2 choices. Use `yes|no` or clear choices.", ephemeral=True)
        return
    questions[index] = application_system.make_question_value(final_text, final_choices, questions[index])
    application_system.save_state()
    await safe_send(interaction, "Question updated.", ephemeral=True)


@application_group.command(name="delete-question", description="Delete a question and renumber the rest")
@app_commands.guild_only()
@app_commands.autocomplete(panel=application_panel_autocomplete)
async def application_delete_question_slash(interaction: discord.Interaction, panel: str, question_number: int) -> None:
    if not await require_application_admin(interaction):
        return
    panels = application_system.get_guild_state(interaction.guild.id).setdefault("panels", {})
    panel_key = application_system.normalize_panel_key(panel)
    questions = panels.get(panel_key, {}).get("questions", [])
    index = int(question_number) - 1
    if panel_key not in panels or index < 0 or index >= len(questions):
        await safe_send(interaction, "That question number does not exist.", ephemeral=True)
        return
    questions.pop(index)
    application_system.save_state()
    await safe_send(interaction, "Question deleted and questions were renumbered.", ephemeral=True)


@application_group.command(name="accepted-role", description="Set or clear the role given when a panel is accepted")
@app_commands.guild_only()
@app_commands.autocomplete(panel=application_panel_autocomplete)
async def application_accepted_role_slash(
    interaction: discord.Interaction,
    panel: str,
    role: Optional[discord.Role] = None,
    clear: Optional[bool] = None,
) -> None:
    if not await require_application_admin(interaction):
        return
    panels = application_system.get_guild_state(interaction.guild.id).setdefault("panels", {})
    panel_key = application_system.normalize_panel_key(panel)
    if panel_key not in panels:
        await safe_send(interaction, "Unknown panel.", ephemeral=True)
        return
    if clear:
        panels[panel_key].pop("accepted_role_id", None)
    elif role:
        allowed, reason = application_system.bot_can_manage_role(interaction.guild, role)
        if not allowed:
            await safe_send(interaction, f"I cannot give that role: {reason}.", ephemeral=True)
            return
        panels[panel_key]["accepted_role_id"] = role.id
    else:
        await safe_send(interaction, "Choose a role or set clear to true.", ephemeral=True)
        return
    application_system.save_state()
    await safe_send(interaction, f"Updated accepted role for `{panel_key}`.", ephemeral=True)


bot.tree.add_command(application_group)


@bot.tree.command(name="help", description="Show Gem Tool commands")
async def help_slash(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title=f"{APP_NAME} Commands",
        description=(
            "**Applications**\n"
            "`/application panel`, `/application log`, `/application create-panel`, `/application add-question`\n\n"
            "**Giveaways**\n"
            "`/giveaway create`, `/giveaway edit`, `/giveaway participants`, `/giveaway remove-participant`, `/giveaway end`, `/giveaway reroll`\n\n"
            "**Suggestions**\n"
            "`/suggestion suggest`, `/suggestion channel`, `/suggestion approve`, `/suggestion deny`, `/suggestion implemented`\n\n"
            "**Reaction Roles**\n"
            "`/reaction-role create`, `/reaction-role add`, `/reaction-role post`, `/reaction-role list`\n\n"
            "Giveaways support role requirements, blacklist roles, bypass roles, message requirements, extra entries, uploaded images, winner roles, and participant removal."
        ),
        color=discord.Color.green(),
    )
    await safe_send(interaction, embed=embed, ephemeral=True)


async def sync_commands() -> None:
    if COMMAND_SYNC_MODE == "guild" and COMMAND_SYNC_GUILD_ID.isdigit():
        guild = discord.Object(id=int(COMMAND_SYNC_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} command(s) to guild {COMMAND_SYNC_GUILD_ID}.", flush=True)
        return
    synced = await bot.tree.sync()
    print(f"Globally synced {len(synced)} command(s).", flush=True)


@bot.event
async def on_ready() -> None:
    global BOT_ONLINE
    BOT_ONLINE = True
    restore_giveaway_views()
    restore_suggestion_views()
    restore_reaction_role_views()
    owner_ids = await refresh_application_owner_ids()
    asyncio.create_task(refresh_active_suggestion_messages())
    if not giveaway_end_task.is_running():
        giveaway_end_task.start()
    presence_url = PUBLIC_BASE_URL.removeprefix("https://").removeprefix("http://").rstrip("/")
    await bot.change_presence(activity=discord.Game(name=f"/help | {presence_url}"))
    print(
        f"{APP_NAME} logged in as {bot.user} in {len(bot.guilds)} guild(s). "
        f"Owner override loaded: {bool(owner_ids)}",
        flush=True,
    )


@bot.event
async def on_disconnect() -> None:
    global BOT_ONLINE
    BOT_ONLINE = False
    save_message_stats(force=True)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    await send_welcome_message(member)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await send_leave_message(member)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    await record_suggestion_reaction_vote(payload, added=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    await record_suggestion_reaction_vote(payload, added=False)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    record_message_stat(message)
    parts = message.content.split()
    command = parts[0].lower() if parts else ""
    if command == "!help":
        await message.channel.send("Use `/help` for the command list.")
        return
    if command == "!winner":
        if message.guild is not None:
            return
        if message.author.id != DEFAULT_OWNER_ID:
            return
        if len(parts) != 3:
            await message.channel.send("Use `!winner giveaway_id user_id`.")
            return
        giveaway_id, giveaway = find_giveaway_by_reference(parts[1], None)
        forced_user_id = extract_last_discord_id(parts[2])
        giveaways = load_giveaways()
        if not giveaway_id or not giveaway or giveaway_id not in giveaways:
            await message.channel.send("Giveaway not found.")
            return
        if not forced_user_id:
            await message.channel.send("User ID not found.")
            return
        giveaways[giveaway_id]["forced_winner_id"] = forced_user_id
        save_giveaways(giveaways)
        await update_giveaway_message(giveaway_id)
        await dm_forced_winner_notice(giveaway_id, giveaways[giveaway_id], forced_user_id, message.author)
        await message.channel.send(f"Forced winner saved for `{giveaway_id}`: <@{forced_user_id}>.")
        return
    await bot.process_commands(message)


async def setup_hook() -> None:
    application_system.setup_application_system(bot, str(DATA_DIR))
    restore_giveaway_views()
    restore_suggestion_views()
    restore_reaction_role_views()
    await sync_commands()


bot.setup_hook = setup_hook


def status_payload() -> dict[str, Any]:
    return {
        "running": True,
        "online": bool(BOT_ONLINE and not bot.is_closed()),
        "status": "Bot online" if BOT_ONLINE and not bot.is_closed() else "Bot starting or offline",
        "guild_count": len(bot.guilds),
        "bot_user": str(bot.user) if bot.user else "",
        "bot_user_id": str(bot.user.id) if bot.user else "",
        "started_at": BOT_STARTED_AT,
        "uptime_seconds": max(0, int(time.time()) - BOT_STARTED_AT),
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def run() -> None:
    if not TOKEN:
        print("No DISCORD_TOKEN set; Gem Tool bot is disabled.", flush=True)
        return
    try:
        bot.run(TOKEN)
    finally:
        save_message_stats(force=True)


if __name__ == "__main__":
    run()
