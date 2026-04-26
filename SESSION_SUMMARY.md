# Telegram Manager Bot — Session 3 Summary

## Architecture

| Layer | Technology |
|---|---|
| Bot framework | python-telegram-bot 21.5 |
| HTTP client | httpx 0.27.2 |
| Storage | Private Telegram channel (infinite, free, survives redeploys) |
| Hosting | Railway (worker dyno) |

---

## Storage: Telegram Channel as Database

**No SQLite. No PostgreSQL. No cost.**

The bot stores all data in a **private Telegram channel** that the manager bot is admin of.

### How it works

1. On startup the bot calls `getChat` on the storage channel.
2. The **pinned message** in the channel holds the entire dataset as a JSON index:
   ```
   [BOT_INDEX] {"version": 1, "records": [{...}, ...]}
   ```
3. Every read (`/mybots`, `/allbots`) uses the **in-memory cache** — zero Telegram API calls.
4. Every write (`upsert_bot`, `delete_bot`) edits the pinned index message and updates the cache.
5. An additional human-readable audit log message is posted per bot creation (non-critical).

### Setup required

1. Create a **private Telegram group or channel**.
2. Add your manager bot as **admin** (needs post/edit/pin message permissions).
3. Get the channel's numeric ID (e.g. `-1001234567890`):
   - Add `@userinfobot` to the group → it will show the chat ID.
   - Or forward any message from the group to `@userinfobot`.
4. Set `STORAGE_CHANNEL_ID` in Railway Variables.

---

## Environment Variables (Railway)

| Variable | Value | Required |
|---|---|---|
| `MANAGER_BOT_TOKEN` | Token from @BotFather for `@sararightbot` | ✅ |
| `MANAGER_USERNAME` | `sararightbot` | ✅ |
| `OWNER_ID` | `7232714487` | ✅ |
| `STORAGE_CHANNEL_ID` | Numeric ID of your private storage channel | ✅ |

---

## Bot Commands

| Command | Who | What |
|---|---|---|
| `/start` | Anyone | Introduction and bot creation guide |
| `/help` | Anyone | All commands |
| `/mybots` | Anyone | Lists that user's bots |
| `/allbots` | Owner only | Lists every bot from all users |
| `/deletebot <bot_id>` | Owner only | Removes a bot record |

---

## Files

| File | Purpose |
|---|---|
| `manager_bot.py` | Main bot — all handlers, commands, API calls |
| `database.py` | Telegram-channel storage engine |
| `requirements.txt` | Dependencies |
| `Procfile` | Railway worker entry point |
| `railway.json` | Auto-restart on crash |
| `.gitignore` | Excludes secrets and cache |
| `push_to_github.bat` | One-click push to GitHub from Windows CMD |

---

## Push to GitHub (Windows CMD)

1. Copy all files into one folder (e.g. `E:\ManagerBot\`).
2. Double-click `push_to_github.bat`.
3. When prompted for a password by git, paste your **GitHub Personal Access Token**
   (not your GitHub password — GitHub disabled password auth in 2021).
   - Generate one at: https://github.com/settings/tokens → "repo" scope.

---

## Railway Deployment

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo.
2. Select `sayfalse/copyrightpost`.
3. Add the four environment variables listed above.
4. In **Settings → Deploy**, confirm service type is **Worker** (reads from `Procfile`).
5. Deploy. Watch **Logs** tab for: `Manager Bot starting (polling mode)…`

---

## Bugs Fixed (Session 3)

| # | Issue | Fix |
|---|---|---|
| 1 | SQLite resets on every Railway redeploy | Replaced with Telegram channel storage (pinned JSON index) |
| 2 | `getManagedBotToken` — both str/dict result not handled robustly | Explicit type checks with early return |
| 3 | `configure_child_bot` — no per-step error isolation | Each API call in its own try/except |
| 4 | `MessageHandler` never fires for `managed_bot` updates | `TypeHandler(Update)` in group=1 |
| 5 | In-memory cache divergence on write failure | Cache rolled back atomically if Telegram write fails |
| 6 | No `/deletebot` command for admin | Added `cmd_deletebot` (owner-only) |
| 7 | No post_init hook — `init_storage` was called synchronously in `main()` | Moved to `Application.post_init` async hook |
| 8 | No audit trail | Non-critical audit log messages posted to storage channel |

---

## Important Notes

- Only **one deployment** can run polling at a time. Disable local runs when Railway is active.
- Bot Management Mode must be enabled in BotFather before the creation link works.
- The storage channel bot must have **admin rights with edit + pin permissions**.
- The pinned index is the single source of truth. If it's manually deleted, run `/start` to recreate.
