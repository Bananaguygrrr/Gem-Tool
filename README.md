# Gem Tool

Gem Tool is a standalone Discord utility bot for application panels and giveaways.

It includes:

- application panels with Discord dropdowns
- DM application flows
- text and dropdown application questions
- per-server application settings
- application logs, accepted roles, and ticket buttons
- giveaway creation, editing, ending, rerolling, and participant management
- giveaway requirements, blacklist roles, bypass roles, message requirements, extra entries, and winner DMs
- a public status and invite website
- built-in Terms of Service and Privacy Policy pages

## Render

Recommended Render service:

- Name: `Gemtool.bot`
- Runtime: Python
- Build command: `pip install -r requirements.txt && python3 -m py_compile server.py support_bot.py application_system.py`
- Start command: `python3 server.py`

## Required Environment Variables

Set these in Render:

- `DISCORD_TOKEN`: Discord bot token for the Gem Tool bot.
- `DISCORD_CLIENT_ID`: Discord application/client ID. Used for the Add to server button and dashboard login.
- `DISCORD_CLIENT_SECRET`: Discord OAuth2 client secret. Used for the web dashboard login.
- `DATA_DIR`: Persistent data directory. Use `/var/data` if the Render service has a disk.

Recommended:

- `APP_NAME`: `Gem Tool`
- `PUBLIC_BASE_URL`: Your public website URL, for example `https://gemtool.bot` or your Render URL.
- `SUPPORT_SERVER_URL`: Your Discord support server invite.
- `LAST_UPDATE`: Text shown on the website, for example `June 5, 2026`.
- `DASHBOARD_SESSION_SECRET`: Long random text used to sign dashboard sessions.
- `COMMAND_SYNC_MODE`: `global` for production, or `guild` for quick testing.
- `COMMAND_SYNC_GUILD_ID`: Only needed when `COMMAND_SYNC_MODE=guild`.

Optional:

- `GIVEAWAY_EMOJI`: Button emoji for giveaway entry. Default is the party popper emoji.
- `GIVEAWAY_CHECK_INTERVAL_SECONDS`: How often ended giveaways are checked. Default is `20`.
- `APPLICATION_TIMEOUT_SECONDS`: Time users have to finish an application. Default is `10800`.

## Discord Developer Portal

Create a Discord application and bot, then enable:

- Server Members Intent
- Message Content Intent

Use this permission set for the invite:

- bot
- applications.commands
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Use External Emojis
- Manage Roles
- Manage Channels
- Create Public Threads
- Manage Messages

The website creates the invite automatically from `DISCORD_CLIENT_ID`.

For Discord dashboard login, add this exact redirect URL in the Discord Developer Portal:

```text
https://gemtool.bot/applications/callback
```

If you use the Render URL instead of a custom domain, use:

```text
https://your-render-service.onrender.com/applications/callback
```

Use these policy links in the Discord Developer Portal:

```text
https://your-domain-or-render-url/terms
https://your-domain-or-render-url/privacy
```

## Main Commands

Application commands:

- `/application panel`
- `/application text`
- `/application log`
- `/application create-panel`
- `/application edit-panel`
- `/application delete-panel`
- `/application add-question`
- `/application edit-question`
- `/application delete-question`
- `/application accepted-role`

Giveaway commands:

- `/giveaway create`
- `/giveaway edit`
- `/giveaway delete`
- `/giveaway end`
- `/giveaway reroll`
- `/giveaway participants`
- `/giveaway remove-participant`
- `/giveaway creator-roles`
- `/giveaway manager-roles`
- `/giveaway fix`

General:

- `/help`

## Local Test

```bash
pip install -r requirements.txt
python -m py_compile server.py support_bot.py application_system.py
python server.py
```

Open:

```text
http://localhost:10000
```
