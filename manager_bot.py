"""
manager_bot.py — Telegram Manager Bot (Railway Edition)
========================================================
All secrets loaded from environment variables. No hardcoded values.
Storage: private Telegram channel (infinite, survives redeploys).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from database import (
    delete_bot,
    get_all_bots,
    get_bots_by_owner,
    init_storage,
    upsert_bot,
)

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

MANAGER_BOT_TOKEN: str = os.environ["MANAGER_BOT_TOKEN"]
MANAGER_USERNAME: str  = os.environ["MANAGER_USERNAME"]
OWNER_ID: int          = int(os.environ["OWNER_ID"])

_API_BASE = f"https://api.telegram.org/bot{MANAGER_BOT_TOKEN}"
_TIMEOUT  = 20  # seconds

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# TELEGRAM API HELPERS
# ─────────────────────────────────────────────

async def _api_call(
    method: str,
    payload: dict,
    token: Optional[str] = None,
) -> dict:
    """
    POST to any Telegram Bot API method.
    Uses the manager token by default; pass token= to use another bot's token.
    Always returns a dict (never raises).
    """
    use_token = token or MANAGER_BOT_TOKEN
    url = f"https://api.telegram.org/bot{use_token}/{method}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        data: dict = resp.json()
    except httpx.TimeoutException:
        logger.error("Timeout calling Telegram API method '%s'.", method)
        return {"ok": False, "description": "Request timed out"}
    except Exception as exc:
        logger.error("HTTP error on '%s': %s", method, exc)
        return {"ok": False, "description": str(exc)}

    if not data.get("ok"):
        logger.error(
            "Telegram API '%s' returned error: %s",
            method, data.get("description", data),
        )

    return data


async def _safe_send(chat_id: int, text: str, parse_mode: str = "Markdown") -> bool:
    """Send a message, swallowing any error. Returns True on success."""
    data = await _api_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    })
    return bool(data.get("ok"))


# ─────────────────────────────────────────────
# CHILD BOT TOKEN RETRIEVAL
# ─────────────────────────────────────────────

async def _get_managed_bot_token(bot_id: int) -> Optional[str]:
    """
    Fetch the child bot's token via getManagedBotToken.
    The Telegram API result may be a plain string OR a dict with a "token" key.
    Returns None if the call fails or the result is unrecognised.
    """
    data = await _api_call("getManagedBotToken", {"bot_id": bot_id})
    if not data.get("ok"):
        return None

    result = data.get("result")
    if isinstance(result, str) and result:
        return result
    if isinstance(result, dict):
        token = result.get("token")
        if isinstance(token, str) and token:
            return token

    logger.error(
        "getManagedBotToken returned an unrecognised result for bot %d: %r",
        bot_id, result,
    )
    return None


# ─────────────────────────────────────────────
# CHILD BOT CONFIGURATION
# ─────────────────────────────────────────────

async def _configure_child_bot(child_token: str, bot_username: str) -> None:
    """
    Auto-configure a newly created child bot.
    Every step is independently try/excepted so a single failure
    does not abort the rest of the setup.
    """
    steps = [
        (
            "setMyDescription",
            {
                "description": (
                    f"👋 I am @{bot_username}.\n"
                    f"Created and managed via @{MANAGER_USERNAME}.\n\n"
                    f"Use /help to see what I can do."
                )
            },
            "description",
        ),
        (
            "setMyShortDescription",
            {"short_description": f"Managed bot — created via @{MANAGER_USERNAME}."},
            "short description",
        ),
        (
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Start the bot"},
                    {"command": "help",  "description": "Show help"},
                ]
            },
            "commands",
        ),
    ]

    for method, payload, label in steps:
        try:
            result = await _api_call(method, payload, token=child_token)
            if result.get("ok"):
                logger.info("[@%s] Set %s successfully.", bot_username, label)
            else:
                logger.warning(
                    "[@%s] Failed to set %s: %s",
                    bot_username, label, result.get("description"),
                )
        except Exception as exc:
            logger.warning("[@%s] Exception setting %s: %s", bot_username, label, exc)

    logger.info("[@%s] Configuration complete.", bot_username)


# ─────────────────────────────────────────────
# MANAGED BOT UPDATE HANDLER
# ─────────────────────────────────────────────

async def _handle_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    TypeHandler(Update) catches ALL update types including managed_bot.
    MessageHandler(filters.ALL) does NOT catch managed_bot — do not use it.
    """
    raw: dict = update.to_dict()

    if "managed_bot" not in raw:
        return

    managed      = raw["managed_bot"]
    new_bot      = managed.get("bot") or {}
    creator      = managed.get("user") or {}

    bot_id: Optional[int]       = new_bot.get("id")
    bot_username: str            = new_bot.get("username") or "unknown"
    owner_id: Optional[int]     = creator.get("id")
    owner_username: str          = creator.get("username") or "unknown"

    if not bot_id or not owner_id:
        logger.error("managed_bot update missing bot_id or owner_id: %s", raw)
        return

    logger.info(
        "managed_bot update: @%s (ID=%d) created by @%s (ID=%d)",
        bot_username, bot_id, owner_username, owner_id,
    )

    # ── Step 1: Fetch child token ──────────────────────────────────────────
    child_token = await _get_managed_bot_token(bot_id)
    if not child_token:
        logger.error("Could not retrieve token for bot %d. Aborting setup.", bot_id)
        await _safe_send(
            owner_id,
            f"⚠️ Your bot @{bot_username} was created, but I could not retrieve "
            f"its token. Please contact the admin @{MANAGER_USERNAME}.",
        )
        return

    # ── Step 2: Persist to Telegram storage ───────────────────────────────
    saved = await upsert_bot(
        bot_id=bot_id,
        bot_username=bot_username,
        bot_token=child_token,
        owner_id=owner_id,
        owner_username=owner_username,
    )
    if not saved:
        logger.error("Storage write failed for @%s — proceeding anyway.", bot_username)

    # ── Step 3: Configure child bot ───────────────────────────────────────
    await _configure_child_bot(child_token, bot_username)

    # ── Step 4: Notify creator ────────────────────────────────────────────
    await _safe_send(
        owner_id,
        (
            f"✅ *Your bot has been created and configured!*\n\n"
            f"🤖 @{bot_username}\n"
            f"🔗 https://t.me/{bot_username}\n\n"
            f"Use /mybots to see all your bots."
        ),
    )

    # ── Step 5: Notify platform owner ────────────────────────────────────
    await _safe_send(
        OWNER_ID,
        (
            f"📦 *New child bot created*\n\n"
            f"Bot: @{bot_username} (`{bot_id}`)\n"
            f"Owner: @{owner_username} (`{owner_id}`)"
        ),
    )


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    # Build a personalised creation link using the user's Telegram ID for uniqueness
    suggested_username = f"Bot{user.id}"
    suggested_name     = f"{user.first_name}s Bot".replace(" ", "+")
    creation_link = (
        f"https://t.me/BotFather?start=newbot"
        # The Managed Bots creation link format:
        f"\n\n_(Or ask BotFather to create a bot under @{MANAGER_USERNAME})_"
    )

    await update.message.reply_text(
        f"👋 Hello, *{user.first_name}*\\!\n\n"
        f"I am a *Manager Bot*\\. I can create and configure Telegram bots for you\\.\n\n"
        f"To create your bot, open BotFather and select @{MANAGER_USERNAME} "
        f"as the manager, or use the link below\\.\n"
        f"{creation_link}\n\n"
        f"Use /help to see all available commands\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Available Commands*\n\n"
        "/start — Introduction and bot creation guide\n"
        "/mybots — List all bots you have created\n"
        "/help — Show this message\n\n"
        "👑 *Admin Only*\n"
        "/allbots — List every bot from all users\n"
        "/deletebot \\<bot\\_id\\> — Remove a bot record",
        parse_mode="MarkdownV2",
    )


async def cmd_mybots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    bots = get_bots_by_owner(user.id)

    if not bots:
        await update.message.reply_text(
            "You have no bots yet.\n"
            "Use /start to learn how to create one."
        )
        return

    lines = [f"🤖 *Your Bots ({len(bots)})*\n"]
    for b in bots:
        lines.append(
            f"• @{b['bot_username']} — `{b['bot_id']}`\n"
            f"  Created: {b['created_at'][:10]}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_allbots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ You are not authorised to use this command.")
        return

    bots = get_all_bots()

    if not bots:
        await update.message.reply_text("No child bots have been created yet.")
        return

    lines = [f"📋 *All Child Bots ({len(bots)})*\n"]
    for b in bots:
        lines.append(
            f"• @{b['bot_username']} — owner: @{b['owner_username']}\n"
            f"  Bot ID: `{b['bot_id']}` | Created: {b['created_at'][:10]}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_deletebot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: remove a bot record by bot_id."""
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ You are not authorised to use this command.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /deletebot <bot_id>\nExample: /deletebot 7654321098"
        )
        return

    bot_id = int(args[0])
    removed = await delete_bot(bot_id)

    if removed:
        await update.message.reply_text(f"✅ Bot record `{bot_id}` removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ No record found for bot ID `{bot_id}`.", parse_mode="Markdown")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    """Called after the Application is built but before polling starts."""
    await init_storage()
    logger.info("Storage initialised. Bot is ready.")


def main() -> None:
    app = (
        Application.builder()
        .token(MANAGER_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("mybots",     cmd_mybots))
    app.add_handler(CommandHandler("allbots",    cmd_allbots))
    app.add_handler(CommandHandler("deletebot",  cmd_deletebot))

    # MUST use TypeHandler(Update) — MessageHandler never fires for managed_bot updates
    app.add_handler(TypeHandler(Update, _handle_any_update), group=1)

    logger.info("Manager Bot starting (polling mode)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
