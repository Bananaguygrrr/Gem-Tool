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
GIVEAWAY_VOTE_URL = os.getenv("GIVEAWAY_VOTE_URL", PUBLIC_BASE_URL).strip()
GIVEAWAY_CHECK_INTERVAL_SECONDS = max(10, int(os.getenv("GIVEAWAY_CHECK_INTERVAL_SECONDS", "20")))
GIVEAWAY_MIN_DURATION_SECONDS = 60
GIVEAWAY_MAX_DURATION_SECONDS = 60 * 60 * 24 * 30
GIVEAWAY_PARTICIPANTS_PAGE_SIZE = 10
MESSAGE_STATS_SAVE_INTERVAL_SECONDS = max(1, int(os.getenv("MESSAGE_STATS_SAVE_INTERVAL_SECONDS", "15")))
APPLICATION_TIMEOUT_SECONDS = int(os.getenv("APPLICATION_TIMEOUT_SECONDS", "10800"))

GIVEAWAYS_FILE = DATA_DIR / "giveaways.json"
GIVEAWAY_SETTINGS_FILE = DATA_DIR / "giveaway_settings.json"
MESSAGE_STATS_FILE = DATA_DIR / "message_stats.json"

NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
DIGIT_ID_RE = re.compile(r"(\d+)")
GIVEAWAY_ID_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
GIVEAWAY_DURATION_PART_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

BOT_STARTED_AT = int(time.time())
BOT_ONLINE = False
GIVEAWAYS_CACHE: Optional[dict[str, dict[str, Any]]] = None
GIVEAWAY_SETTINGS_CACHE: Optional[dict[str, Any]] = None
MESSAGE_STATS_CACHE: Optional[dict[str, Any]] = None
MESSAGE_STATS_LAST_SAVE = 0.0
REGISTERED_GIVEAWAY_VIEW_IDS: set[str] = set()
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
    if user_id in APPLICATION_OWNER_IDS:
        return True
    owner_ids = await refresh_application_owner_ids()
    return user_id in owner_ids


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
    return GIVEAWAY_VOTE_URL or "https://discord.com"


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
            f"in the server: **{guild_name}**\n\n"
            f"[Please help me by voting \U0001f682]({GIVEAWAY_VOTE_URL})"
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
            f"you have the blacklisted role **{role_name}** in the server **{guild_name}**\n\n"
            f"[Please help me by voting \U0001f682]({GIVEAWAY_VOTE_URL})"
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
            giveaway["participant_ids"] = [user_id for user_id in participant_ids if user_id != interaction.user.id]
            entries.pop(str(interaction.user.id), None)
            response = "Your giveaway entry was removed."
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
                await channel.send(f"Congratulations {', '.join(f'<@{user_id}>' for user_id in winners)}! You won **{giveaway.get('prize')}**.")
            else:
                await channel.send(f"Giveaway **{giveaway.get('prize')}** ended with no valid entries.")
        except discord.HTTPException:
            pass
    return True, f"Ended giveaway `{giveaway_id}`."


@tasks.loop(seconds=GIVEAWAY_CHECK_INTERVAL_SECONDS)
async def giveaway_end_task() -> None:
    now = int(time.time())
    for giveaway_id, giveaway in list(load_giveaways().items()):
        if not giveaway.get("ended") and int(giveaway.get("end_at") or 0) <= now:
            await finish_giveaway(giveaway_id)


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
    giveaways = load_giveaways()
    normalized = re.sub(r"[^a-z0-9_-]", "", str(value or "").lower())[:32]
    if normalized in giveaways and giveaways[normalized].get("guild_id") == interaction.guild_id:
        return normalized, giveaways[normalized]
    message_id = parse_user_id(value)
    if message_id:
        for giveaway_id, giveaway in giveaways.items():
            if giveaway.get("guild_id") == interaction.guild_id and int(giveaway.get("message_id") or 0) == message_id:
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
    owner_ids = await refresh_application_owner_ids()
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
        if not await is_application_owner(message.author.id):
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if len(parts) != 3:
            return
        giveaway_id = re.sub(r"[^a-z0-9_-]", "", parts[1].lower())[:32]
        forced_user_id = parse_user_id(parts[2])
        giveaways = load_giveaways()
        if giveaway_id not in giveaways or not forced_user_id:
            return
        giveaways[giveaway_id]["forced_winner_id"] = forced_user_id
        save_giveaways(giveaways)
        await update_giveaway_message(giveaway_id)
        return
    await bot.process_commands(message)


async def setup_hook() -> None:
    application_system.setup_application_system(bot, str(DATA_DIR))
    restore_giveaway_views()
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
