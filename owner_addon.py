from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands

INSTALLED = False


def _format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _parse_id(dexbot: Any, value: Any) -> Optional[int]:
    parser = getattr(dexbot, "extract_last_discord_id", None) or getattr(dexbot, "parse_user_id", None)
    if parser:
        try:
            parsed = parser(value)
            return int(parsed) if parsed else None
        except Exception:
            return None
    try:
        text = "".join(ch for ch in str(value or "") if ch.isdigit())
        return int(text) if text else None
    except ValueError:
        return None


async def _send(dexbot: Any, interaction: discord.Interaction, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None) -> None:
    safe_send = getattr(dexbot, "safe_send", None)
    if safe_send:
        await safe_send(interaction, content, embed=embed, ephemeral=True)
        return
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(content=content, embed=embed, ephemeral=True)


async def _require_owner(dexbot: Any, interaction: discord.Interaction) -> bool:
    checker = getattr(dexbot, "is_application_owner", None)
    allowed = False
    if checker:
        try:
            allowed = bool(await checker(interaction.user.id))
        except Exception:
            allowed = False
    if not allowed:
        await _send(dexbot, interaction, "Only the bot owner can use this command.")
        return False
    return True


def _truncate(dexbot: Any, value: Any, limit: int) -> str:
    truncator = getattr(dexbot, "truncate", None)
    if truncator:
        return truncator(value, limit)
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def install(dexbot: Any) -> None:
    global INSTALLED
    if INSTALLED:
        return

    bot = getattr(dexbot, "bot", None)
    if bot is None:
        return

    owner_group = app_commands.Group(name="owner", description="Owner-only Gem Tool controls")

    @owner_group.command(name="status", description="Show private owner status for the bot")
    async def owner_status_slash(interaction: discord.Interaction) -> None:
        if not await _require_owner(dexbot, interaction):
            return
        payload = dexbot.status_payload() if hasattr(dexbot, "status_payload") else {}
        giveaways = {}
        if hasattr(dexbot, "load_giveaways"):
            try:
                giveaways = dexbot.load_giveaways()
            except Exception:
                giveaways = {}
        active_giveaways = sum(1 for item in giveaways.values() if not item.get("ended")) if isinstance(giveaways, dict) else 0
        owner_ids = sorted(str(owner_id) for owner_id in getattr(dexbot, "APPLICATION_OWNER_IDS", set()))
        embed = discord.Embed(
            title="Owner Status",
            description="Private Gem Tool owner panel.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Online", value=str(payload.get("online", False)), inline=True)
        embed.add_field(name="Guilds", value=str(payload.get("guild_count", len(getattr(bot, "guilds", [])))), inline=True)
        embed.add_field(name="Uptime", value=_format_seconds(int(payload.get("uptime_seconds") or 0)), inline=True)
        embed.add_field(name="Bot", value=str(payload.get("bot_user") or getattr(bot, "user", "Unknown")), inline=False)
        embed.add_field(name="Public URL", value=str(getattr(dexbot, "PUBLIC_BASE_URL", "Not set")) or "Not set", inline=False)
        embed.add_field(name="Data Dir", value=f"`{getattr(dexbot, 'DATA_DIR', 'unknown')}`", inline=False)
        embed.add_field(name="Active Giveaways", value=str(active_giveaways), inline=True)
        embed.add_field(name="Known Owners", value=", ".join(owner_ids) if owner_ids else "Application owner not cached yet", inline=False)
        await _send(dexbot, interaction, embed=embed)

    @owner_group.command(name="sync", description="Owner-only slash command sync")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Global", value="global"),
            app_commands.Choice(name="Current server", value="current"),
            app_commands.Choice(name="Server ID", value="guild"),
        ]
    )
    async def owner_sync_slash(interaction: discord.Interaction, scope: str = "global", guild_id: Optional[str] = None) -> None:
        if not await _require_owner(dexbot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if scope == "current":
                if interaction.guild is None:
                    await interaction.followup.send("Use this in a server or choose the global scope.", ephemeral=True)
                    return
                guild = discord.Object(id=interaction.guild.id)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                await interaction.followup.send(f"Synced {len(synced)} command(s) to this server.", ephemeral=True)
                return
            if scope == "guild":
                parsed_guild_id = _parse_id(dexbot, guild_id)
                if not parsed_guild_id:
                    await interaction.followup.send("Give me a valid guild/server ID.", ephemeral=True)
                    return
                guild = discord.Object(id=parsed_guild_id)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                await interaction.followup.send(f"Synced {len(synced)} command(s) to guild `{parsed_guild_id}`.", ephemeral=True)
                return
            synced = await bot.tree.sync()
            await interaction.followup.send(f"Globally synced {len(synced)} command(s).", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Sync failed: `{type(exc).__name__}: {_truncate(dexbot, exc, 160)}`", ephemeral=True)

    @owner_group.command(name="server-list", description="List servers the bot is in")
    async def owner_server_list_slash(interaction: discord.Interaction) -> None:
        if not await _require_owner(dexbot, interaction):
            return
        guilds = sorted(getattr(bot, "guilds", []), key=lambda item: item.name.lower())
        if not guilds:
            await _send(dexbot, interaction, "The bot is not in any servers yet.")
            return
        lines = []
        for guild in guilds[:25]:
            member_count = getattr(guild, "member_count", None) or "?"
            lines.append(f"`{guild.id}` - **{_truncate(dexbot, guild.name, 70)}** ({member_count} members)")
        extra = len(guilds) - len(lines)
        if extra > 0:
            lines.append(f"...and {extra} more server(s).")
        embed = discord.Embed(title="Bot Servers", description="\n".join(lines), color=discord.Color.blurple())
        await _send(dexbot, interaction, embed=embed)

    @owner_group.command(name="leave-server", description="Make the bot leave a server by ID")
    async def owner_leave_server_slash(
        interaction: discord.Interaction,
        guild_id: str,
        confirm: bool = False,
        reason: Optional[str] = None,
    ) -> None:
        if not await _require_owner(dexbot, interaction):
            return
        parsed_guild_id = _parse_id(dexbot, guild_id)
        guild = bot.get_guild(parsed_guild_id or 0)
        if guild is None:
            await _send(dexbot, interaction, "I am not in a server with that ID.")
            return
        if not confirm:
            await _send(dexbot, interaction, f"Run this again with `confirm: true` to leave **{guild.name}** (`{guild.id}`).")
            return
        guild_name = guild.name
        await guild.leave()
        suffix = f" Reason: {reason}" if reason else ""
        await _send(dexbot, interaction, f"Left **{guild_name}** (`{guild.id}`).{suffix}")

    @owner_group.command(name="dm", description="Send a DM as the bot")
    async def owner_dm_slash(interaction: discord.Interaction, user_id: str, message: str) -> None:
        if not await _require_owner(dexbot, interaction):
            return
        parsed_user_id = _parse_id(dexbot, user_id)
        if not parsed_user_id:
            await _send(dexbot, interaction, "Give me a valid user ID or mention.")
            return
        try:
            user = bot.get_user(parsed_user_id) or await bot.fetch_user(parsed_user_id)
            await user.send(_truncate(dexbot, message, 1900))
        except discord.Forbidden:
            await _send(dexbot, interaction, "I cannot DM that user.")
            return
        except discord.HTTPException as exc:
            await _send(dexbot, interaction, f"DM failed: `{_truncate(dexbot, exc, 160)}`")
            return
        await _send(dexbot, interaction, f"DM sent to **{user}** (`{parsed_user_id}`).")

    try:
        bot.tree.add_command(owner_group)
    except app_commands.CommandAlreadyRegistered:
        return

    INSTALLED = True
    print("Owner addon installed: /owner commands registered.", flush=True)
