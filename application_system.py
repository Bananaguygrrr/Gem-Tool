from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from typing import Any, Dict, Optional

import discord
from discord.ext import commands


APPLICATION_TIMEOUT_SECONDS = max(300, int(os.getenv("APPLICATION_TIMEOUT_SECONDS", "10800")))
APPLICATION_START_TIMEOUT_SECONDS = max(60, min(900, APPLICATION_TIMEOUT_SECONDS))
DEFAULT_PANEL_TEXT = "Select an option to begin!"
FORM_NAME_RE = re.compile(r"[^a-z0-9]+")
STATE_FILE = ""
STATE_DIR = ""
STATE: Dict[str, Any] = {"guilds": {}}
BOT: Optional[commands.Bot] = None
REGISTERED = False
VIEWS_RESTORED = False
ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}


def utc_now() -> int:
    return int(time.time())


def normalize_panel_key(name: str) -> str:
    key = FORM_NAME_RE.sub("-", str(name or "").strip().lower()).strip("-")
    return key[:64]


def stable_suffix(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_user(user_id: int) -> str:
    return f"<@{user_id}> (`{user_id}`)"


def bot_channel_permission_errors(channel: discord.TextChannel) -> list[str]:
    if BOT is None or BOT.user is None:
        return ["bot is not ready"]
    bot_member = channel.guild.me or channel.guild.get_member(BOT.user.id)
    if bot_member is None:
        return ["bot member could not be resolved"]
    permissions = channel.permissions_for(bot_member)
    checks = (
        ("View Channel", permissions.view_channel),
        ("Send Messages", permissions.send_messages),
        ("Embed Links", permissions.embed_links),
        ("Read Message History", permissions.read_message_history),
    )
    return [name for name, allowed in checks if not allowed]


def bot_can_manage_role(guild: discord.Guild, role: discord.Role) -> tuple[bool, str]:
    if BOT is None or BOT.user is None:
        return False, "bot is not ready"
    if role.is_default():
        return False, "that is the @everyone role"
    if role.managed:
        return False, "that role is managed by an integration"
    bot_member = guild.me or guild.get_member(BOT.user.id)
    if bot_member is None:
        return False, "bot member could not be resolved"
    if not bot_member.guild_permissions.manage_roles:
        return False, "I need the Manage Roles permission"
    if role >= bot_member.top_role:
        return False, "that role must be below my highest role"
    return True, ""


def prune_application_sessions(user_id: Optional[int] = None) -> int:
    now = utc_now()
    removed = 0
    for session_id, session in list(ACTIVE_SESSIONS.items()):
        if user_id is not None and int(session.get("user_id") or 0) != int(user_id):
            continue
        created_at = int(session.get("created_at") or now)
        start_timeout = not session.get("started") and now - created_at >= APPLICATION_START_TIMEOUT_SECONDS
        full_timeout = now - created_at >= APPLICATION_TIMEOUT_SECONDS
        if start_timeout or full_timeout:
            ACTIVE_SESSIONS.pop(session_id, None)
            removed += 1
    return removed


def cleanup_application_task(session_id: str, task: asyncio.Task) -> None:
    try:
        error = task.exception()
    except asyncio.CancelledError:
        error = None
    if error:
        print(f"Application session {session_id} crashed: {error}")
    ACTIVE_SESSIONS.pop(session_id, None)


def default_state() -> Dict[str, Any]:
    return {"guilds": {}}


def validate_state(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return default_state()
    guilds = data.get("guilds")
    if not isinstance(guilds, dict):
        data["guilds"] = {}
    return data


def validate_guild_state(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("panel_text", DEFAULT_PANEL_TEXT)
    panels = data.get("panels")
    if not isinstance(panels, dict):
        data["panels"] = {}
    submissions = data.get("submissions")
    if not isinstance(submissions, dict):
        data["submissions"] = {}
    return data


def read_state_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return validate_state(json.load(handle))


def read_guild_state_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return validate_guild_state(json.load(handle))


def write_json_with_backup(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup_path = f"{path}.bak"
    if os.path.exists(path):
        try:
            shutil.copy2(path, backup_path)
        except OSError as error:
            print(f"Failed to refresh backup {backup_path}: {error}")
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temp_path, path)


def load_state() -> Dict[str, Any]:
    if not STATE_FILE and not STATE_DIR:
        return default_state()

    loaded_from_legacy = False
    data = default_state()
    backup_path = f"{STATE_FILE}.bak"
    if STATE_FILE:
        try:
            data = read_state_file(STATE_FILE)
            loaded_from_legacy = True
        except FileNotFoundError:
            try:
                data = read_state_file(backup_path)
                loaded_from_legacy = True
            except FileNotFoundError:
                data = default_state()
            except Exception as error:
                print(f"Failed to load backup application state {backup_path}: {error}")
                data = default_state()
        except Exception as error:
            print(f"Failed to load {STATE_FILE}: {error}")
            try:
                data = read_state_file(backup_path)
                loaded_from_legacy = True
            except Exception as backup_error:
                print(f"Failed to load backup application state {backup_path}: {backup_error}")
                data = default_state()
            else:
                print(f"Recovered application state from backup {backup_path}.")

    data = validate_state(data)

    if STATE_DIR and os.path.isdir(STATE_DIR):
        for entry in os.scandir(STATE_DIR):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            guild_id = entry.name[:-5]
            if not guild_id.isdigit():
                continue
            try:
                data["guilds"][guild_id] = read_guild_state_file(entry.path)
            except Exception as error:
                backup_file = f"{entry.path}.bak"
                try:
                    data["guilds"][guild_id] = read_guild_state_file(backup_file)
                except Exception as backup_error:
                    print(f"Failed to load application state for guild {guild_id}: {error}; backup failed: {backup_error}")
                else:
                    print(f"Recovered application state for guild {guild_id} from backup.")

    if loaded_from_legacy and STATE_DIR:
        print("Loaded legacy application state; it will be migrated into per-server files on save.")
    return data


def save_state() -> None:
    if STATE_DIR:
        for guild_id, guild_state in validate_state(STATE).get("guilds", {}).items():
            safe_guild_id = re.sub(r"[^0-9]", "", str(guild_id))
            if not safe_guild_id:
                continue
            write_json_with_backup(os.path.join(STATE_DIR, f"{safe_guild_id}.json"), validate_guild_state(guild_state))
        return
    if STATE_FILE:
        write_json_with_backup(STATE_FILE, validate_state(STATE))


def get_guild_state(guild_id: int) -> Dict[str, Any]:
    guilds = STATE.setdefault("guilds", {})
    guild_state = guilds.setdefault(str(guild_id), {})
    guild_state.setdefault("panel_text", DEFAULT_PANEL_TEXT)
    guild_state.setdefault("panels", {})
    guild_state.setdefault("submissions", {})
    return guild_state


def get_panel(guild_id: int, panel_key: str) -> Optional[Dict[str, Any]]:
    return get_guild_state(guild_id).get("panels", {}).get(panel_key)


def get_submission(guild_id: int, submission_id: str) -> Optional[Dict[str, Any]]:
    return get_guild_state(guild_id).get("submissions", {}).get(submission_id)


def has_application_admin(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and (
        interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator
    )


async def require_application_admin(interaction: discord.Interaction) -> bool:
    if has_application_admin(interaction):
        return True
    await interaction.response.send_message(
        "You need Manage Server permission to manage applications.",
        ephemeral=True,
    )
    return False


async def resolve_text_channel(guild: discord.Guild, channel_id: int) -> Optional[discord.TextChannel]:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


def parse_question_choices(choices: Optional[str]) -> Optional[list[str]]:
    if choices is None:
        return None
    raw = choices.strip()
    if raw.lower() in {"clear", "none", "text"}:
        return []
    separator = "|" if "|" in raw else ","
    options: list[str] = []
    seen: set[str] = set()
    for part in raw.split(separator):
        option = part.strip()
        if not option:
            continue
        normalized = option.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        options.append(option[:100])
        if len(options) >= 25:
            break
    return options


def normalize_question(question: Any) -> Dict[str, Any]:
    if isinstance(question, dict):
        text = str(question.get("text") or question.get("question") or "").strip()
        raw_options = question.get("options") if isinstance(question.get("options"), list) else []
        options = []
        seen: set[str] = set()
        for raw_option in raw_options:
            option = str(raw_option or "").strip()
            if not option:
                continue
            normalized = option.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            options.append(option[:100])
            if len(options) >= 25:
                break
        if str(question.get("type", "")).lower() == "select" and len(options) >= 2:
            return {"type": "select", "text": text[:300], "options": options}
        return {"type": "text", "text": text[:300], "options": []}
    return {"type": "text", "text": str(question or "").strip()[:300], "options": []}


def question_text(question: Any) -> str:
    return normalize_question(question).get("text", "")


def make_question_value(text: str, choices: Optional[str], existing_question: Any = None) -> Any:
    question = text.strip()[:300]
    parsed_choices = parse_question_choices(choices)
    if parsed_choices is None and existing_question is not None:
        existing = normalize_question(existing_question)
        if existing.get("type") == "select":
            return {"type": "select", "text": question, "options": existing.get("options", [])}
    if parsed_choices:
        return {"type": "select", "text": question, "options": parsed_choices}
    return question


def panel_questions(panel: Dict[str, Any]) -> list[Dict[str, Any]]:
    questions = panel.get("questions")
    if not isinstance(questions, list):
        return []
    normalized_questions = [normalize_question(question) for question in questions]
    return [question for question in normalized_questions if question.get("text")]


def build_application_panel_embed(guild: discord.Guild) -> discord.Embed:
    guild_state = get_guild_state(guild.id)
    embed = discord.Embed(
        title="Applications",
        description=guild_state.get("panel_text") or DEFAULT_PANEL_TEXT,
        color=discord.Color.blue(),
    )
    embed.set_author(name="Application bot")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def build_question_embed(panel: Dict[str, Any], index: int, total: int, question: Dict[str, Any]) -> discord.Embed:
    title = panel.get("name", "Application")
    if not title.lower().endswith("application"):
        title = f"{title} Application"
    embed = discord.Embed(
        title=truncate(title, 256),
        description=f"**{index}/{total}. {question.get('text', '')}**",
        color=discord.Color.blue(),
    )
    if question.get("type") == "select":
        embed.set_footer(text="To answer this question, please select an option from the dropdown below.")
    else:
        embed.set_footer(text="To answer this question, please send a message to the bot with your response.")
    return embed


def build_submission_embed(guild: discord.Guild, panel: Dict[str, Any], submission: Dict[str, Any]) -> discord.Embed:
    status = submission.get("status", "pending")
    color = {
        "accepted": discord.Color.green(),
        "denied": discord.Color.red(),
    }.get(status, discord.Color.orange())

    title = f"{submission.get('username', 'Unknown')}'s '{panel.get('name', 'Application')}' Application"
    embed = discord.Embed(
        title=truncate(title, 256),
        description=f"**{status.title()}**",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    if submission.get("avatar_url"):
        embed.set_thumbnail(url=submission["avatar_url"])

    answers = submission.get("answers", [])
    for index, answer in enumerate(answers[:20], start=1):
        embed.add_field(
            name=f"{index}. {truncate(answer.get('question', 'Question'), 256)}",
            value=truncate(answer.get("answer", "No answer"), 950) or "No answer",
            inline=False,
        )

    if len(answers) > 20:
        embed.add_field(
            name="More answers",
            value="This application has more than 20 answers. The full data is saved in the application data file.",
            inline=False,
        )

    stats = [
        f"UserId: `{submission['user_id']}`",
        f"Username: `{submission.get('username', 'unknown')}`",
        f"User: <@{submission['user_id']}>",
        f"Duration: `{format_duration(int(submission.get('duration_seconds') or 0))}`",
        f"Submitted: <t:{int(submission.get('created_at', utc_now()))}:R>",
    ]
    if submission.get("reviewer_id"):
        stats.append(f"Reviewer: <@{submission['reviewer_id']}>")
    if submission.get("reason"):
        stats.append(f"Reason: {truncate(submission['reason'], 500)}")
    embed.add_field(name="Submission stats", value="\n".join(stats), inline=False)
    embed.set_footer(text=f"Submission {submission['id']}")
    return embed


async def send_applicant_dm(user_id: int, *, content: Optional[str] = None, embed: Optional[discord.Embed] = None) -> None:
    if BOT is None:
        return
    try:
        user = BOT.get_user(user_id) or await BOT.fetch_user(user_id)
    except discord.HTTPException:
        return
    try:
        await user.send(content=content, embed=embed)
    except discord.HTTPException:
        pass


async def grant_panel_accept_role(guild: discord.Guild, panel: Dict[str, Any], user_id: int) -> tuple[bool, str]:
    role_id = int(panel.get("accepted_role_id") or 0)
    if not role_id:
        return False, ""
    role = guild.get_role(role_id)
    if role is None:
        return False, "Accepted role is missing. Set it again in the application dashboard."
    allowed, reason = bot_can_manage_role(guild, role)
    if not allowed:
        return False, f"Could not give {role.mention}: {reason}."
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False, "Could not give the accepted role because the applicant is not in this server."
    if role in member.roles:
        return True, f"{role.mention} was already on the applicant."
    try:
        await member.add_roles(role, reason="Application accepted")
    except discord.HTTPException as error:
        return False, f"Could not give {role.mention}: `{truncate(error, 160)}`"
    return True, f"Gave {role.mention} to the applicant."


async def post_application_log(guild: discord.Guild, panel: Dict[str, Any], submission: Dict[str, Any], text: str) -> bool:
    guild_state = get_guild_state(guild.id)
    channel_id = int(guild_state.get("log_channel_id") or 0)
    if not channel_id:
        print(f"Application log skipped for guild {guild.id}: no log channel configured.")
        return False
    channel = await resolve_text_channel(guild, channel_id)
    if not channel:
        print(f"Application log skipped for guild {guild.id}: channel {channel_id} was not found.")
        return False
    missing_permissions = bot_channel_permission_errors(channel)
    if missing_permissions:
        print(
            f"Application log skipped for guild {guild.id}: missing permissions in "
            f"#{channel.name}: {', '.join(missing_permissions)}"
        )
        return False
    embed = discord.Embed(
        title="Application log",
        description=text,
        color=discord.Color.dark_teal(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Applicant", value=format_user(int(submission["user_id"])), inline=False)
    embed.add_field(name="Panel", value=panel.get("name", submission.get("panel_key", "application")), inline=True)
    embed.add_field(name="Status", value=str(submission.get("status", "pending")).title(), inline=True)
    try:
        await channel.send(embed=embed)
        return True
    except discord.HTTPException as error:
        print(f"Application log failed for guild {guild.id} in channel {channel.id}: {error}")
        return False


async def post_or_update_submission(guild: discord.Guild, panel: Dict[str, Any], submission: Dict[str, Any]) -> bool:
    guild_state = get_guild_state(guild.id)
    log_channel_id = int(guild_state.get("log_channel_id") or 0)
    if not log_channel_id:
        print(f"Application submission skipped for guild {guild.id}: no log channel configured.")
        return False
    channel = await resolve_text_channel(guild, log_channel_id)
    if not channel:
        print(f"Application submission skipped for guild {guild.id}: channel {log_channel_id} was not found.")
        return False
    missing_permissions = bot_channel_permission_errors(channel)
    if missing_permissions:
        print(
            f"Application submission skipped for guild {guild.id}: missing permissions in "
            f"#{channel.name}: {', '.join(missing_permissions)}"
        )
        return False

    embed = build_submission_embed(guild, panel, submission)
    view = ApplicationReviewView(guild.id, submission["id"], disabled=submission.get("status") != "pending")

    if submission.get("review_message_id"):
        try:
            message = await channel.fetch_message(int(submission["review_message_id"]))
            await message.edit(embed=embed, view=view)
            return True
        except discord.HTTPException as error:
            print(f"Application submission update failed for guild {guild.id}: {error}")
            pass

    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException as error:
        print(f"Application submission send failed for guild {guild.id} in channel {channel.id}: {error}")
        return False
    else:
        submission["review_channel_id"] = channel.id
        submission["review_message_id"] = message.id
        save_state()
        return True


def ticket_channel_name(member: discord.Member, submission_id: str) -> str:
    base = FORM_NAME_RE.sub("-", member.name.lower()).strip("-") or "user"
    return truncate(f"app-{base}-{submission_id[:5]}", 90).strip("-")


def build_ticket_closed_embed(
    guild: discord.Guild,
    panel: Dict[str, Any],
    submission: Dict[str, Any],
    closer: discord.abc.User,
    reason: str,
) -> discord.Embed:
    closed_at = int(submission.get("ticket_closed_at") or utc_now())
    opened_at = int(submission.get("ticket_opened_at") or submission.get("created_at") or closed_at)
    opened_by_id = int(submission.get("ticket_opened_by_id") or submission.get("reviewer_id") or 0)
    close_reason = reason or "No reason provided."
    embed = discord.Embed(
        title="Ticket Closed",
        description=f"The ticket from the server **{guild.name}** was closed.",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Ticket Id", value=f"`{submission.get('id', 'unknown')}`", inline=True)
    embed.add_field(name="Category", value=f"{panel.get('name', 'Application')} ticket", inline=True)
    embed.add_field(name="Opened at", value=f"<t:{opened_at}:R>", inline=True)
    embed.add_field(name="Closed at", value=f"<t:{closed_at}:R>", inline=True)
    embed.add_field(name="Opened by", value=f"<@{opened_by_id}>" if opened_by_id else "Unknown", inline=True)
    embed.add_field(name="Closed by", value=f"{closer.mention}\n`{closer}`", inline=True)
    embed.add_field(name="Open reason", value="N/A", inline=False)
    embed.add_field(name="Close reason", value=truncate(close_reason, 1000), inline=False)
    return embed


async def send_ticket_closed_dm(
    guild: discord.Guild,
    panel: Dict[str, Any],
    submission: Dict[str, Any],
    closer: discord.abc.User,
    reason: str,
) -> None:
    if BOT is None:
        return
    try:
        user = BOT.get_user(int(submission["user_id"])) or await BOT.fetch_user(int(submission["user_id"]))
    except (discord.HTTPException, KeyError, ValueError):
        return

    embed = build_ticket_closed_embed(guild, panel, submission, closer, reason)
    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        return


class TicketCloseReasonModal(discord.ui.Modal):
    def __init__(self, guild_id: int, submission_id: str):
        super().__init__(title="Close ticket with reason")
        self.guild_id = guild_id
        self.submission_id = submission_id
        self.reason = discord.ui.TextInput(
            label="Close reason",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            placeholder="Why are you closing this ticket?",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await close_application_ticket(interaction, self.guild_id, self.submission_id, str(self.reason.value).strip())


class ApplicationTicketView(discord.ui.View):
    def __init__(self, guild_id: int, submission_id: str):
        super().__init__(timeout=None)
        close_button = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.primary,
            custom_id=f"gemapp:ticket:close:{guild_id}:{submission_id}",
        )
        reason_button = discord.ui.Button(
            label="Close with reason",
            style=discord.ButtonStyle.primary,
            custom_id=f"gemapp:ticket:reason:{guild_id}:{submission_id}",
        )
        close_button.callback = self.close_button(guild_id, submission_id)
        reason_button.callback = self.reason_button(guild_id, submission_id)
        self.add_item(close_button)
        self.add_item(reason_button)

    def close_button(self, guild_id: int, submission_id: str):
        async def callback(interaction: discord.Interaction) -> None:
            await close_application_ticket(interaction, guild_id, submission_id, "")

        return callback

    def reason_button(self, guild_id: int, submission_id: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(TicketCloseReasonModal(guild_id, submission_id))

        return callback


async def close_application_ticket(
    interaction: discord.Interaction,
    guild_id: int,
    submission_id: str,
    reason: str,
) -> None:
    if not interaction.guild or interaction.guild_id != guild_id:
        await interaction.response.send_message("This ticket belongs to another server.", ephemeral=True)
        return
    submission = get_submission(guild_id, submission_id)
    if not submission:
        await interaction.response.send_message("That submission no longer exists.", ephemeral=True)
        return
    is_applicant = int(submission.get("user_id") or 0) == interaction.user.id
    if not is_applicant and not await require_application_admin(interaction):
        return
    panel = get_panel(guild_id, submission.get("panel_key", ""))
    if not panel:
        await interaction.response.send_message("The panel for this ticket no longer exists.", ephemeral=True)
        return
    channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    if channel is None:
        await interaction.response.send_message("This can only be used inside the ticket channel.", ephemeral=True)
        return
    expected_channel_id = int(submission.get("ticket_channel_id") or 0)
    if expected_channel_id and channel.id != expected_channel_id:
        await interaction.response.send_message("Use this button in the active ticket channel.", ephemeral=True)
        return
    if submission.get("ticket_closed_at"):
        await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    closed_at = utc_now()
    submission["ticket_closed_at"] = closed_at
    submission["ticket_closed_by_id"] = interaction.user.id
    submission["ticket_close_reason"] = reason or ""
    submission["ticket_channel_name"] = channel.name
    save_state()

    await send_ticket_closed_dm(interaction.guild, panel, submission, interaction.user, reason)
    await interaction.followup.send("Ticket closed. Closing channel in 2 seconds...", ephemeral=True)
    try:
        await channel.send("Closing ticket in 2 seconds...")
    except discord.HTTPException:
        pass
    await asyncio.sleep(2)
    try:
        await channel.delete(reason=f"Application ticket closed by {interaction.user} ({interaction.user.id})")
    except discord.HTTPException as error:
        await interaction.followup.send(f"I could not delete the channel: `{truncate(error, 160)}`", ephemeral=True)


async def open_application_ticket(interaction: discord.Interaction, guild_id: int, submission_id: str) -> None:
    if not interaction.guild or interaction.guild_id != guild_id:
        await interaction.response.send_message("This ticket button belongs to another server.", ephemeral=True)
        return
    if not await require_application_admin(interaction):
        return
    if BOT is None or BOT.user is None:
        await interaction.response.send_message("Application system is not ready.", ephemeral=True)
        return

    submission = get_submission(guild_id, submission_id)
    if not submission:
        await interaction.response.send_message("That submission no longer exists.", ephemeral=True)
        return
    panel = get_panel(guild_id, submission.get("panel_key", ""))
    if not panel:
        await interaction.response.send_message("The panel for this submission no longer exists.", ephemeral=True)
        return

    existing_channel_id = int(submission.get("ticket_channel_id") or 0)
    existing_channel = interaction.guild.get_channel(existing_channel_id) if existing_channel_id else None
    if isinstance(existing_channel, discord.TextChannel):
        await interaction.response.send_message(f"Ticket already exists: {existing_channel.mention}", ephemeral=True)
        return

    bot_member = interaction.guild.me or interaction.guild.get_member(BOT.user.id)
    if bot_member is None:
        await interaction.response.send_message("I could not resolve my server member.", ephemeral=True)
        return
    if not bot_member.guild_permissions.manage_channels:
        await interaction.response.send_message("I need the Manage Channels permission to open tickets.", ephemeral=True)
        return

    applicant = interaction.guild.get_member(int(submission["user_id"]))
    if applicant is None:
        try:
            applicant = await interaction.guild.fetch_member(int(submission["user_id"]))
        except discord.HTTPException:
            await interaction.response.send_message("The applicant is not in this server anymore.", ephemeral=True)
            return

    reviewer = interaction.user if isinstance(interaction.user, discord.Member) else None
    overwrites: Dict[Any, discord.PermissionOverwrite] = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            manage_channels=True,
        ),
        applicant: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            attach_files=True,
        ),
    }
    if reviewer:
        overwrites[reviewer] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            attach_files=True,
        )

    category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None
    await interaction.response.defer(ephemeral=True)
    try:
        ticket = await interaction.guild.create_text_channel(
            name=ticket_channel_name(applicant, submission_id),
            category=category,
            overwrites=overwrites,
            reason=f"Application ticket for {submission_id}",
        )
        ticket_message = await ticket.send(
            content=f"{applicant.mention} {interaction.user.mention}",
            embed=build_submission_embed(interaction.guild, panel, submission),
            view=ApplicationTicketView(interaction.guild.id, submission_id),
        )
    except discord.HTTPException as error:
        await interaction.followup.send(f"I could not open the ticket: `{truncate(error, 180)}`", ephemeral=True)
        return

    submission["ticket_channel_id"] = ticket.id
    submission["ticket_message_id"] = ticket_message.id
    submission["ticket_opened_at"] = utc_now()
    submission["ticket_opened_by_id"] = interaction.user.id
    save_state()
    await interaction.followup.send(f"Ticket opened: {ticket.mention}", ephemeral=True)


async def refresh_application_message(guild: discord.Guild) -> bool:
    guild_state = get_guild_state(guild.id)
    channel_id = int(guild_state.get("application_channel_id") or 0)
    message_id = int(guild_state.get("application_message_id") or 0)
    if not channel_id or not message_id:
        return False

    channel = await resolve_text_channel(guild, channel_id)
    if not channel:
        return False

    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=build_application_panel_embed(guild), view=ApplicationSelectView(guild.id))
        return True
    except discord.HTTPException:
        return False


async def post_application_panel(guild: discord.Guild, channel_id: int) -> tuple[bool, str]:
    guild_state = get_guild_state(guild.id)
    channel = await resolve_text_channel(guild, int(channel_id))
    if not channel:
        return False, "Application channel was not found."

    missing_permissions = bot_channel_permission_errors(channel)
    if missing_permissions:
        return False, f"I am missing permissions in #{channel.name}: {', '.join(missing_permissions)}."

    old_channel_id = int(guild_state.get("application_channel_id") or 0)
    old_message_id = int(guild_state.get("application_message_id") or 0)

    if old_channel_id and old_message_id:
        old_channel = await resolve_text_channel(guild, old_channel_id)
        if old_channel:
            try:
                old_message = await old_channel.fetch_message(old_message_id)
            except discord.HTTPException:
                old_message = None
            if old_message and old_channel.id == channel.id:
                await old_message.edit(embed=build_application_panel_embed(guild), view=ApplicationSelectView(guild.id))
                guild_state["application_channel_id"] = channel.id
                guild_state["application_message_id"] = old_message.id
                save_state()
                return True, f"Application panel refreshed in #{channel.name}."
            if old_message:
                try:
                    await old_message.delete()
                except discord.HTTPException:
                    pass

    message = await channel.send(embed=build_application_panel_embed(guild), view=ApplicationSelectView(guild.id))
    guild_state["application_channel_id"] = channel.id
    guild_state["application_message_id"] = message.id
    save_state()
    return True, f"Application panel posted in #{channel.name}."


class ApplicationSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options: list[discord.SelectOption] = []
        panels = get_guild_state(guild_id).get("panels", {})
        for panel_key, panel in sorted(panels.items()):
            if panel.get("enabled", True) is False:
                continue
            options.append(
                discord.SelectOption(
                    label=truncate(panel.get("name", panel_key), 100),
                    description=truncate(panel.get("description", "Start this application."), 100),
                    value=panel_key,
                )
            )
            if len(options) >= 25:
                break
        if not options:
            options = [
                discord.SelectOption(
                    label="No applications open",
                    description="Staff can create panels in the dashboard.",
                    value="__none__",
                )
            ]

        super().__init__(
            placeholder="Make a selection",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"gemapp:select:{guild_id}",
            disabled=options[0].value == "__none__",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This application panel belongs to another server.", ephemeral=True)
            return
        await start_application_from_interaction(interaction, self.values[0])


class ApplicationSelectView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(ApplicationSelect(guild_id))


class ApplicationQuestionSelect(discord.ui.Select):
    def __init__(self, session_id: str, user_id: int, options: list[str]):
        self.session_id = session_id
        self.user_id = user_id
        select_options = [
            discord.SelectOption(label=truncate(option, 100), value=str(index))
            for index, option in enumerate(options[:25])
        ]
        super().__init__(
            placeholder="Select an option...",
            min_values=1,
            max_values=1,
            options=select_options,
            custom_id=f"gemapp:qselect:{session_id}:{stable_suffix('|'.join(options))}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ApplicationQuestionSelectView):
            await interaction.response.send_message("This question is no longer active.", ephemeral=True)
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This application question is not yours.", ephemeral=True)
            return
        if view.answer_future.done():
            await interaction.response.send_message("This question was already answered.", ephemeral=True)
            return
        try:
            selected_index = int(self.values[0])
            answer = view.options[selected_index]
        except (ValueError, IndexError):
            await interaction.response.send_message("That option is not valid anymore.", ephemeral=True)
            return
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(view=view)
        await interaction.followup.send(
            embed=discord.Embed(
                title="Question answered",
                description=f"You chose option: `{truncate(answer, 900)}`",
                color=discord.Color.green(),
            )
        )
        view.answer_future.set_result(answer)
        view.stop()


class ApplicationQuestionSelectView(discord.ui.View):
    def __init__(self, session_id: str, user_id: int, options: list[str], timeout_seconds: int):
        super().__init__(timeout=max(1, timeout_seconds))
        self.options = options[:25]
        self.answer_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.add_item(ApplicationQuestionSelect(session_id, user_id, self.options))

    async def wait_for_answer(self) -> str:
        return await self.answer_future

    async def on_timeout(self) -> None:
        if not self.answer_future.done():
            self.answer_future.set_exception(asyncio.TimeoutError())


class ApplicationStartView(discord.ui.View):
    def __init__(self, session_id: str):
        super().__init__(timeout=APPLICATION_TIMEOUT_SECONDS)
        self.session_id = session_id

        start_button = discord.ui.Button(
            label="Start application",
            style=discord.ButtonStyle.success,
            custom_id=f"gemapp:start:{session_id}",
        )
        cancel_button = discord.ui.Button(
            label="Cancel application",
            style=discord.ButtonStyle.danger,
            custom_id=f"gemapp:cancel:{session_id}",
        )
        start_button.callback = self.start_button
        cancel_button.callback = self.cancel_button
        self.add_item(start_button)
        self.add_item(cancel_button)

    async def start_button(self, interaction: discord.Interaction) -> None:
        session = ACTIVE_SESSIONS.get(self.session_id)
        if not session or interaction.user.id != session["user_id"]:
            await interaction.response.send_message("This application session is not yours.", ephemeral=True)
            return
        if session.get("started"):
            await interaction.response.send_message("This application already started.", ephemeral=True)
            return
        session["started"] = True
        session["started_notice_sent"] = True
        await interaction.response.defer()
        if interaction.message:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                try:
                    await interaction.message.edit(view=None)
                except discord.HTTPException:
                    pass
        await interaction.followup.send(
            embed=discord.Embed(
                title="Application Started",
                description="Please answer the questions below, either by clicking dropdown menus or sending a message to the bot.",
                color=discord.Color.green(),
            )
        )
        task = asyncio.create_task(run_application_session(self.session_id))
        task.add_done_callback(lambda done_task: cleanup_application_task(self.session_id, done_task))

    async def cancel_button(self, interaction: discord.Interaction) -> None:
        session = ACTIVE_SESSIONS.pop(self.session_id, None)
        if not session or interaction.user.id != session["user_id"]:
            await interaction.response.send_message("This application session is not yours.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Application cancelled",
                description="Your application was cancelled.",
                color=discord.Color.red(),
            ),
            view=None,
        )


class ReviewReasonModal(discord.ui.Modal):
    def __init__(self, guild_id: int, submission_id: str, status: str):
        super().__init__(title=f"{status.title()} with reason")
        self.guild_id = guild_id
        self.submission_id = submission_id
        self.status = status
        self.reason = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            placeholder="Tell the applicant why.",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await handle_review(interaction, self.guild_id, self.submission_id, self.status, str(self.reason.value).strip())


class ApplicationReviewView(discord.ui.View):
    def __init__(self, guild_id: int, submission_id: str, *, disabled: bool = False):
        super().__init__(timeout=None)
        buttons = [
            ("Accept", discord.ButtonStyle.success, "accepted", None, 0),
            ("Deny", discord.ButtonStyle.danger, "denied", None, 0),
            ("Accept with reason", discord.ButtonStyle.success, "accepted", "reason", 0),
            ("Deny with reason", discord.ButtonStyle.danger, "denied", "reason", 0),
            ("History", discord.ButtonStyle.primary, "history", None, 1),
            ("Open ticket with user", discord.ButtonStyle.secondary, "ticket", None, 1),
        ]
        for label, style, action, mode, row in buttons:
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"gemapp:review:{stable_suffix(label)}:{guild_id}:{submission_id}",
                disabled=disabled and action not in {"history", "ticket"},
                row=row,
            )
            if action == "history":
                button.callback = self.history_button(guild_id, submission_id)
            elif action == "ticket":
                button.callback = self.ticket_button(guild_id, submission_id)
            elif mode == "reason":
                button.callback = self.reason_button(guild_id, submission_id, action)
            else:
                button.callback = self.review_button(guild_id, submission_id, action)
            self.add_item(button)

    def review_button(self, guild_id: int, submission_id: str, status: str):
        async def callback(interaction: discord.Interaction) -> None:
            await handle_review(interaction, guild_id, submission_id, status, None)

        return callback

    def reason_button(self, guild_id: int, submission_id: str, status: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(ReviewReasonModal(guild_id, submission_id, status))

        return callback

    def history_button(self, guild_id: int, submission_id: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.guild_id != guild_id:
                await interaction.response.send_message("This review belongs to another server.", ephemeral=True)
                return
            if not await require_application_admin(interaction):
                return
            submission = get_submission(guild_id, submission_id)
            if not submission:
                await interaction.response.send_message("That submission no longer exists.", ephemeral=True)
                return
            await send_application_history(interaction, int(submission["user_id"]))

        return callback

    def ticket_button(self, guild_id: int, submission_id: str):
        async def callback(interaction: discord.Interaction) -> None:
            await open_application_ticket(interaction, guild_id, submission_id)

        return callback


async def start_application_from_interaction(interaction: discord.Interaction, panel_key: str) -> None:
    if BOT is None:
        await interaction.response.send_message("Application system is not ready.", ephemeral=True)
        return
    if not interaction.guild or not interaction.guild_id:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return

    panel_key = normalize_panel_key(panel_key)
    panel = get_panel(interaction.guild_id, panel_key)
    if not panel or panel.get("enabled", True) is False:
        await interaction.response.send_message("That application is not open.", ephemeral=True)
        return

    questions = panel_questions(panel)
    if not questions:
        await interaction.response.send_message("This application has no questions yet.", ephemeral=True)
        return

    prune_application_sessions(interaction.user.id)
    if any(int(session.get("user_id") or 0) == interaction.user.id for session in ACTIVE_SESSIONS.values()):
        await interaction.response.send_message("You already have an application open in DMs.", ephemeral=True)
        return

    try:
        dm_channel = await interaction.user.create_dm()
    except discord.HTTPException:
        await interaction.response.send_message("I could not DM you. Open your DMs and try again.", ephemeral=True)
        return

    session_id = uuid.uuid4().hex[:12]
    ACTIVE_SESSIONS[session_id] = {
        "id": session_id,
        "guild_id": interaction.guild_id,
        "panel_key": panel_key,
        "user_id": interaction.user.id,
        "dm_channel_id": dm_channel.id,
        "created_at": utc_now(),
        "started": False,
    }

    confirm = discord.Embed(
        title=panel.get("name", "Application"),
        description=(
            "Are you sure you want to apply?\n\n"
            f"Once you start, I will send you **{len(questions)}** questions. "
            f"You have **{format_duration(APPLICATION_TIMEOUT_SECONDS)}** to finish. "
            "You can type `cancel` at any time."
        ),
        color=discord.Color.blue(),
    )
    dm_message = await dm_channel.send(embed=confirm, view=ApplicationStartView(session_id))

    started_embed = discord.Embed(
        title="Application started",
        description="Application has been started in your direct messages!",
        color=discord.Color.green(),
    )
    jump = discord.ui.View()
    jump.add_item(discord.ui.Button(label="Jump to application", url=dm_message.jump_url))
    await interaction.response.send_message(embed=started_embed, view=jump, ephemeral=True)


async def run_application_session(session_id: str) -> None:
    if BOT is None:
        return
    session = ACTIVE_SESSIONS.get(session_id)
    if not session:
        return

    guild_id = int(session["guild_id"])
    user_id = int(session["user_id"])
    panel = get_panel(guild_id, session["panel_key"])
    guild = BOT.get_guild(guild_id)
    if not guild or not panel:
        ACTIVE_SESSIONS.pop(session_id, None)
        return

    try:
        user = BOT.get_user(user_id) or await BOT.fetch_user(user_id)
        dm_channel = user.dm_channel or await user.create_dm()
    except discord.HTTPException:
        ACTIVE_SESSIONS.pop(session_id, None)
        return

    questions = panel_questions(panel)
    if not session.get("started_notice_sent"):
        await dm_channel.send(
            embed=discord.Embed(
                title="Application Started",
                description="Please answer the questions below, either by clicking dropdown menus or sending a message to the bot.",
                color=discord.Color.green(),
            )
        )

    start_time = utc_now()
    deadline = int(session.get("created_at", start_time)) + APPLICATION_TIMEOUT_SECONDS
    answers = []

    for index, question in enumerate(questions, start=1):
        remaining_time = max(0, deadline - utc_now())
        if remaining_time <= 0:
            await dm_channel.send(
                embed=discord.Embed(
                    title="Application expired",
                    description="You did not complete the application in time.",
                    color=discord.Color.red(),
                )
            )
            ACTIVE_SESSIONS.pop(session_id, None)
            return

        question_data = normalize_question(question)
        embed = build_question_embed(panel, index, len(questions), question_data)

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == user_id
                and message.channel.id == dm_channel.id
                and not message.author.bot
            )

        if question_data.get("type") == "select":
            view = ApplicationQuestionSelectView(session_id, user_id, question_data.get("options", []), remaining_time)
            await dm_channel.send(embed=embed, view=view)
            try:
                answer = await asyncio.wait_for(view.wait_for_answer(), timeout=remaining_time)
            except asyncio.TimeoutError:
                await dm_channel.send(
                    embed=discord.Embed(
                        title="Application expired",
                        description="You did not complete the application in time.",
                        color=discord.Color.red(),
                    )
                )
                ACTIVE_SESSIONS.pop(session_id, None)
                return
        else:
            await dm_channel.send(embed=embed)
            try:
                message = await BOT.wait_for("message", check=check, timeout=remaining_time)
            except asyncio.TimeoutError:
                await dm_channel.send(
                    embed=discord.Embed(
                        title="Application expired",
                        description="You did not complete the application in time.",
                        color=discord.Color.red(),
                    )
                )
                ACTIVE_SESSIONS.pop(session_id, None)
                return
            answer = message.content.strip()

        if answer.lower() in {"cancel", "stop"}:
            await dm_channel.send(
                embed=discord.Embed(
                    title="Application cancelled",
                    description="Your application was cancelled.",
                    color=discord.Color.red(),
                )
            )
            ACTIVE_SESSIONS.pop(session_id, None)
            return

        answers.append({"question": question_data.get("text", ""), "answer": answer or "No answer"})

    member = guild.get_member(user_id)
    username = str(member or user)
    avatar_url = (member.display_avatar.url if member else user.display_avatar.url)
    submission_id = uuid.uuid4().hex[:10]
    submission = {
        "id": submission_id,
        "panel_key": session["panel_key"],
        "user_id": user_id,
        "username": username,
        "avatar_url": avatar_url,
        "answers": answers,
        "status": "pending",
        "created_at": utc_now(),
        "duration_seconds": utc_now() - start_time,
    }
    get_guild_state(guild_id)["submissions"][submission_id] = submission
    save_state()
    ACTIVE_SESSIONS.pop(session_id, None)

    await dm_channel.send(
        embed=discord.Embed(
            title="Application submitted.",
            description="Your application has been submitted.",
            color=discord.Color.green(),
        )
    )
    posted_review = await post_or_update_submission(guild, panel, submission)
    posted_log = await post_application_log(guild, panel, submission, "A new application was submitted.")
    if not posted_review and not posted_log:
        await dm_channel.send(
            embed=discord.Embed(
                title="Application log warning",
                description=(
                    "Your application was saved, but I could not post it to the server log channel. "
                    "Please tell a server admin to set the application log channel again in the dashboard."
                ),
                color=discord.Color.orange(),
            )
        )


async def handle_review(
    interaction: discord.Interaction,
    guild_id: int,
    submission_id: str,
    status: str,
    reason: Optional[str],
) -> None:
    if not interaction.guild or interaction.guild_id != guild_id:
        await interaction.response.send_message("This review belongs to another server.", ephemeral=True)
        return
    if not await require_application_admin(interaction):
        return

    submission = get_submission(guild_id, submission_id)
    if not submission:
        await interaction.response.send_message("That submission no longer exists.", ephemeral=True)
        return
    if submission.get("status") != "pending":
        await interaction.response.send_message("This application was already reviewed.", ephemeral=True)
        return

    panel = get_panel(guild_id, submission.get("panel_key", ""))
    if not panel:
        await interaction.response.send_message("The panel for this submission no longer exists.", ephemeral=True)
        return

    submission["status"] = status
    submission["reason"] = reason or ""
    submission["reviewer_id"] = interaction.user.id
    submission["reviewed_at"] = utc_now()
    role_success = False
    role_message = ""
    if status == "accepted":
        role_success, role_message = await grant_panel_accept_role(interaction.guild, panel, int(submission["user_id"]))
        if role_message:
            submission["accepted_role_result"] = role_message
    save_state()

    embed = build_submission_embed(interaction.guild, panel, submission)
    disabled_view = ApplicationReviewView(guild_id, submission_id, disabled=True)
    if interaction.message:
        await interaction.response.edit_message(embed=embed, view=disabled_view)
    else:
        await post_or_update_submission(interaction.guild, panel, submission)
        await interaction.response.send_message(f"Application {status}.", ephemeral=True)

    applicant_embed = discord.Embed(
        title=f"Application {status}",
        description=f"Your **{panel.get('name', 'application')}** application in **{interaction.guild.name}** was **{status}**.",
        color=discord.Color.green() if status == "accepted" else discord.Color.red(),
    )
    if reason:
        applicant_embed.add_field(name="Reason", value=truncate(reason, 1000), inline=False)
    if role_message:
        applicant_embed.add_field(name="Role", value=truncate(role_message, 1000), inline=False)
    await send_applicant_dm(int(submission["user_id"]), embed=applicant_embed)
    log_text = f"Application was **{status}** by {interaction.user.mention}."
    if role_message:
        log_text = f"{log_text}\n{role_message}"
    await post_application_log(interaction.guild, panel, submission, log_text)
    if status == "accepted" and role_message and not role_success:
        await interaction.followup.send(role_message, ephemeral=True)


async def send_application_history(interaction: discord.Interaction, user_id: int) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    submissions = [
        submission
        for submission in get_guild_state(interaction.guild_id).get("submissions", {}).values()
        if int(submission.get("user_id", 0)) == user_id
    ]
    submissions.sort(key=lambda item: int(item.get("created_at", 0)), reverse=True)
    if not submissions:
        await interaction.response.send_message("No applications found for that user.", ephemeral=True)
        return

    lines = []
    for submission in submissions[:15]:
        panel = get_panel(interaction.guild_id, submission.get("panel_key", ""))
        panel_name = panel.get("name", submission.get("panel_key", "application")) if panel else submission.get("panel_key", "application")
        lines.append(
            f"`{submission['id']}` - **{panel_name}** - {submission.get('status', 'pending').title()} - "
            f"<t:{int(submission.get('created_at', utc_now()))}:R>"
        )

    embed = discord.Embed(
        title="Application history",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Showing {min(len(submissions), 15)} of {len(submissions)} submissions")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def restore_application_views() -> None:
    if BOT is None:
        return
    restored_panels = 0
    restored_reviews = 0
    restored_tickets = 0
    for guild_id_text, guild_state in STATE.get("guilds", {}).items():
        try:
            guild_id = int(guild_id_text)
        except ValueError:
            continue
        BOT.add_view(ApplicationSelectView(guild_id))
        restored_panels += 1
        for submission_id, submission in guild_state.get("submissions", {}).items():
            if submission.get("status") == "pending" and submission.get("review_message_id"):
                BOT.add_view(
                    ApplicationReviewView(guild_id, submission_id),
                    message_id=int(submission["review_message_id"]),
                )
                restored_reviews += 1
            if (
                submission.get("ticket_message_id")
                and submission.get("ticket_channel_id")
                and not submission.get("ticket_closed_at")
            ):
                BOT.add_view(
                    ApplicationTicketView(guild_id, submission_id),
                    message_id=int(submission["ticket_message_id"]),
                )
                restored_tickets += 1
    print(
        f"Restored {restored_panels} application panel view(s), "
        f"{restored_reviews} review view(s), and {restored_tickets} ticket view(s)."
    )


async def application_ready_listener() -> None:
    global VIEWS_RESTORED
    if VIEWS_RESTORED:
        return
    await restore_application_views()
    VIEWS_RESTORED = True


def setup_application_system(discord_bot: commands.Bot, data_dir: str) -> None:
    global BOT, REGISTERED, STATE, STATE_FILE, STATE_DIR
    if REGISTERED:
        return

    BOT = discord_bot
    configured_state_file = os.getenv("APPLICATION_STATE_FILE", "").strip()
    if configured_state_file:
        STATE_FILE = (
            configured_state_file
            if os.path.isabs(configured_state_file)
            else os.path.join(data_dir, configured_state_file)
        )
    else:
        STATE_FILE = os.path.join(data_dir, "applications.json")
    configured_state_dir = os.getenv("APPLICATION_STATE_DIR", "").strip()
    STATE_DIR = (
        configured_state_dir
        if configured_state_dir and os.path.isabs(configured_state_dir)
        else os.path.join(data_dir, configured_state_dir or "applications")
    )
    STATE = load_state()
    if STATE_DIR:
        os.makedirs(STATE_DIR, exist_ok=True)
        save_state()
    print(f"Application legacy import file: {os.path.abspath(STATE_FILE)}")
    print(f"Application per-server settings directory: {os.path.abspath(STATE_DIR)}")

    discord_bot.add_listener(application_ready_listener, "on_ready")
    REGISTERED = True
