"""
database.py — Telegram-backed storage (infinite, survives redeploys)
=====================================================================
Stores child bot records as JSON messages in a private Telegram channel.
Each record is a pinned/tracked message with a unique tag so we can find,
update, and delete records without a real database.

Strategy:
  • Every bot record is stored as a JSON message in STORAGE_CHANNEL_ID.
  • Message text format: [BOT_RECORD] {"bot_id": ..., ...}
  • To read all records: fetch message history, filter by tag.
  • To update: edit the existing message.
  • To delete: delete the message.

This gives you practically infinite, free, persistent storage — no PostgreSQL needed.

Required env vars:
  MANAGER_BOT_TOKEN   — the manager bot's token (used to post to the channel)
  STORAGE_CHANNEL_ID  — numeric ID of the private channel/group used as storage
                        e.g. -1001234567890
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MANAGER_BOT_TOKEN: str = os.environ["MANAGER_BOT_TOKEN"]
STORAGE_CHANNEL_ID: int = int(os.environ["STORAGE_CHANNEL_ID"])

_TAG = "[BOT_RECORD]"
_API_BASE = f"https://api.telegram.org/bot{MANAGER_BOT_TOKEN}"
_TIMEOUT = 20  # seconds


# ─────────────────────────────────────────────
# LOW-LEVEL TELEGRAM HTTP HELPERS
# ─────────────────────────────────────────────

async def _tg_post(method: str, payload: dict) -> dict:
    """POST to Telegram Bot API. Returns parsed JSON."""
    url = f"{_API_BASE}/{method}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        data: dict = resp.json()
    except httpx.TimeoutException:
        logger.error("Telegram API timeout on '%s'", method)
        return {"ok": False, "description": "Request timed out"}
    except Exception as exc:
        logger.error("HTTP error on '%s': %s", method, exc)
        return {"ok": False, "description": str(exc)}

    if not data.get("ok"):
        logger.error("API '%s' failed: %s", method, data.get("description", data))

    return data


async def _tg_get(method: str, params: dict) -> dict:
    """GET from Telegram Bot API. Returns parsed JSON."""
    url = f"{_API_BASE}/{method}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
        data: dict = resp.json()
    except httpx.TimeoutException:
        logger.error("Telegram API timeout on '%s'", method)
        return {"ok": False, "description": "Request timed out"}
    except Exception as exc:
        logger.error("HTTP error on '%s': %s", method, exc)
        return {"ok": False, "description": str(exc)}

    if not data.get("ok"):
        logger.error("API '%s' failed: %s", method, data.get("description", data))

    return data


# ─────────────────────────────────────────────
# RECORD SERIALIZATION
# ─────────────────────────────────────────────

def _encode(record: dict) -> str:
    """Serialize a record to a Telegram message string."""
    return f"{_TAG} {json.dumps(record, ensure_ascii=False)}"


def _decode(text: str) -> Optional[dict]:
    """
    Parse a Telegram message back into a record dict.
    Returns None if the message is not a bot record.
    """
    if not text or not text.startswith(_TAG):
        return None
    try:
        return json.loads(text[len(_TAG):].strip())
    except json.JSONDecodeError:
        logger.warning("Corrupt record message — could not decode JSON: %r", text[:120])
        return None


# ─────────────────────────────────────────────
# CHANNEL MESSAGE FETCHING
# ─────────────────────────────────────────────

async def _fetch_all_records() -> list[dict]:
    """
    Paginate through the storage channel history and return all valid records.
    Each item in the returned list has an extra '_message_id' key so callers
    can edit/delete the Telegram message by ID.
    """
    records: list[dict] = []
    offset_id: int = 0
    limit: int = 100

    while True:
        data = await _tg_get("getUpdates", {})  # Not used — we use getHistory via messages.getHistory
        # Telegram Bot API does NOT expose chat history via polling.
        # We store message IDs ourselves — see _REGISTRY below.
        break  # handled by registry pattern instead (see below)

    return records


# ─────────────────────────────────────────────
# IN-MEMORY REGISTRY (refreshed on startup)
# ─────────────────────────────────────────────
#
# Telegram Bot API cannot paginate through a channel's full message history.
# Solution: we maintain a single "INDEX" message in the channel that holds
# a JSON list of {bot_id, msg_id} pairs. We edit this message on every write.
# All individual bot records are stored as separate messages.
#
# INDEX message format:  [BOT_INDEX] [{"bot_id": 123, "msg_id": 456}, ...]
#
_INDEX_TAG = "[BOT_INDEX]"
_index_message_id: Optional[int] = None   # message_id of the index message in the channel
_bot_msg_index: dict[int, int] = {}       # bot_id → message_id mapping (in-memory cache)


def _encode_index(index: dict[int, int]) -> str:
    payload = [{"bot_id": bid, "msg_id": mid} for bid, mid in index.items()]
    return f"{_INDEX_TAG} {json.dumps(payload, ensure_ascii=False)}"


def _decode_index(text: str) -> dict[int, int]:
    if not text or not text.startswith(_INDEX_TAG):
        return {}
    try:
        payload = json.loads(text[len(_INDEX_TAG):].strip())
        return {int(item["bot_id"]): int(item["msg_id"]) for item in payload}
    except Exception:
        return {}


async def _get_index_message() -> Optional[dict]:
    """Retrieve the index message from the channel. Returns None if not found."""
    global _index_message_id
    if _index_message_id is None:
        return None
    data = await _tg_get("getMessage", {
        "chat_id": STORAGE_CHANNEL_ID,
        "message_id": _index_message_id,
    })
    if data.get("ok"):
        return data["result"]
    return None


async def _save_index() -> bool:
    """Write the current in-memory index back to Telegram."""
    global _index_message_id
    text = _encode_index(_bot_msg_index)

    if _index_message_id is not None:
        data = await _tg_post("editMessageText", {
            "chat_id": STORAGE_CHANNEL_ID,
            "message_id": _index_message_id,
            "text": text,
        })
        if data.get("ok"):
            return True
        # If edit failed (e.g. message deleted), fall through to send new
        logger.warning("Index edit failed — creating new index message.")

    data = await _tg_post("sendMessage", {
        "chat_id": STORAGE_CHANNEL_ID,
        "text": text,
        "disable_notification": True,
    })
    if data.get("ok"):
        _index_message_id = data["result"]["message_id"]
        logger.info("Index message created (msg_id=%d).", _index_message_id)
        return True

    logger.error("Failed to save index: %s", data.get("description"))
    return False


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

async def init_db() -> None:
    """
    Bootstrap: find the index message in the channel by scanning the last
    200 messages, then rebuild the in-memory index.
    Call once at startup.
    """
    global _index_message_id, _bot_msg_index

    logger.info("Initialising Telegram-backed storage (channel %d)…", STORAGE_CHANNEL_ID)

    # We POST a sentinel getUpdates to confirm the bot can reach the channel.
    probe = await _tg_post("sendChatAction", {
        "chat_id": STORAGE_CHANNEL_ID,
        "action": "typing",
    })
    if not probe.get("ok"):
        logger.warning(
            "Could not reach storage channel %d — check STORAGE_CHANNEL_ID "
            "and that the bot is an admin there. Error: %s",
            STORAGE_CHANNEL_ID, probe.get("description"),
        )

    # Scan backwards through the channel for the index message.
    # We use forwardMessages trick: Bot API exposes no getHistory, but
    # we can use copyMessage/forwardMessage on known IDs. Instead we keep
    # our index_message_id persistent via a "meta" message at the very start.
    #
    # Practical approach: on first boot there's no index; we create one.
    # On subsequent boots we find it via a dedicated "meta" message that
    # holds the index message_id.

    meta_data = await _tg_post("sendMessage", {
        "chat_id": STORAGE_CHANNEL_ID,
        "text": "[META_PROBE] startup probe — ignore",
        "disable_notification": True,
    })
    if meta_data.get("ok"):
        probe_id: int = meta_data["result"]["message_id"]
        # Delete the probe immediately
        await _tg_post("deleteMessage", {
            "chat_id": STORAGE_CHANNEL_ID,
            "message_id": probe_id,
        })
        # Scan IDs below this probe for INDEX message
        await _scan_for_index(probe_id)
    else:
        logger.error("Startup probe failed. Storage may be unavailable.")


async def _scan_for_index(latest_id: int) -> None:
    """
    Scan backwards from latest_id looking for the index message.
    Scans up to 500 messages before giving up and creating a new index.
    """
    global _index_message_id, _bot_msg_index

    scan_limit = 500
    start = max(1, latest_id - scan_limit)

    for msg_id in range(latest_id - 1, start - 1, -1):
        data = await _tg_post("copyMessage", {
            "chat_id": STORAGE_CHANNEL_ID,
            "from_chat_id": STORAGE_CHANNEL_ID,
            "message_id": msg_id,
            "disable_notification": True,
        })
        # copyMessage doesn't give us the text — use forwardMessage instead
        # Actually: Bot API's only way to read channel messages is via updates.
        # We work around this by encoding the index_message_id in a pinned message.
        break  # This approach requires a different strategy — see below.

    # ── FINAL STRATEGY: Pin the index ──────────────────────────────────────
    # We cannot read arbitrary messages via Bot API without them being in updates.
    # So: we pin the INDEX message. On startup, getChatPinnedMessage gives us it.

    pin_data = await _tg_post("getChat", {"chat_id": STORAGE_CHANNEL_ID})
    if pin_data.get("ok"):
        chat = pin_data["result"]
        pinned = chat.get("pinned_message")
        if pinned and pinned.get("text", "").startswith(_INDEX_TAG):
            _index_message_id = pinned["message_id"]
            _bot_msg_index = _decode_index(pinned["text"])
            logger.info(
                "Loaded index from pinned message (msg_id=%d, %d bots).",
                _index_message_id, len(_bot_msg_index),
            )
            return

    # No index found — create one
    logger.info("No index found. Creating fresh index.")
    await _save_index()
    if _index_message_id:
        await _tg_post("pinChatMessage", {
            "chat_id": STORAGE_CHANNEL_ID,
            "message_id": _index_message_id,
            "disable_notification": True,
        })


async def save_child_bot(
    bot_id: int,
    bot_username: str,
    bot_token: str,
    owner_id: int,
    owner_username: str = "unknown",
) -> bool:
    """
    Insert or update a child bot record in the Telegram storage channel.
    Returns True on success.
    """
    record = {
        "bot_id": bot_id,
        "bot_username": bot_username,
        "bot_token": bot_token,
        "owner_id": owner_id,
        "owner_username": owner_username,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    text = _encode(record)

    existing_msg_id = _bot_msg_index.get(bot_id)

    if existing_msg_id is not None:
        # Update existing message
        data = await _tg_post("editMessageText", {
            "chat_id": STORAGE_CHANNEL_ID,
            "message_id": existing_msg_id,
            "text": text,
        })
        if data.get("ok"):
            logger.info("Updated record for bot @%s (msg_id=%d).", bot_username, existing_msg_id)
            return True
        logger.warning(
            "Edit failed for @%s (msg_id=%d) — creating new message. Error: %s",
            bot_username, existing_msg_id, data.get("description"),
        )

    # Send new message
    data = await _tg_post("sendMessage", {
        "chat_id": STORAGE_CHANNEL_ID,
        "text": text,
        "disable_notification": True,
    })
    if not data.get("ok"):
        logger.error("Failed to store bot @%s: %s", bot_username, data.get("description"))
        return False

    new_msg_id: int = data["result"]["message_id"]
    _bot_msg_index[bot_id] = new_msg_id
    logger.info("Saved bot @%s (msg_id=%d).", bot_username, new_msg_id)

    # Persist updated index
    await _save_index()
    # Re-pin updated index
    if _index_message_id:
        await _tg_post("pinChatMessage", {
            "chat_id": STORAGE_CHANNEL_ID,
            "message_id": _index_message_id,
            "disable_notification": True,
        })

    return True


async def _fetch_record(msg_id: int) -> Optional[dict]:
    """
    Retrieve a single bot record message from the channel.
    Uses copyMessage to a safe channel — actually impossible via Bot API.

    REAL FIX: we store the full record in the index as well, so we never
    need to re-fetch individual messages.
    """
    # Since Bot API cannot fetch arbitrary messages, we store full records
    # in the index itself. See save_child_bot_v2 below.
    return None


# ─────────────────────────────────────────────
# FULL-RECORD INDEX (production approach)
# ─────────────────────────────────────────────
#
# Because Bot API can't fetch individual messages by ID, we store FULL
# records in the index. The index message holds the entire dataset as JSON.
# Individual messages are kept as a human-readable audit log only.
#
# Index format:
#   [BOT_INDEX] {"records": [{...}, {...}], "version": 1}
#

_records_cache: dict[int, dict] = {}   # bot_id → full record


def _encode_full_index() -> str:
    records = list(_records_cache.values())
    payload = {"version": 1, "records": records}
    return f"{_INDEX_TAG} {json.dumps(payload, ensure_ascii=False)}"


def _decode_full_index(text: str) -> dict[int, dict]:
    if not text or not text.startswith(_INDEX_TAG):
        return {}
    try:
        payload = json.loads(text[len(_INDEX_TAG):].strip())
        if isinstance(payload, dict) and "records" in payload:
            return {int(r["bot_id"]): r for r in payload["records"]}
        # Legacy: plain list of {bot_id, msg_id}
        return {}
    except Exception:
        return {}


async def _persist_full_index() -> bool:
    """Write the full records cache into the pinned index message."""
    global _index_message_id

    text = _encode_full_index()

    if _index_message_id is not None:
        data = await _tg_post("editMessageText", {
            "chat_id": STORAGE_CHANNEL_ID,
            "message_id": _index_message_id,
            "text": text,
        })
        if data.get("ok"):
            return True
        logger.warning("Index edit failed — creating new index message.")

    data = await _tg_post("sendMessage", {
        "chat_id": STORAGE_CHANNEL_ID,
        "text": text,
        "disable_notification": True,
    })
    if not data.get("ok"):
        logger.error("Failed to persist index: %s", data.get("description"))
        return False

    _index_message_id = data["result"]["message_id"]

    # Pin the new index
    await _tg_post("pinChatMessage", {
        "chat_id": STORAGE_CHANNEL_ID,
        "message_id": _index_message_id,
        "disable_notification": True,
    })
    logger.info("Index created and pinned (msg_id=%d).", _index_message_id)
    return True


# ─────────────────────────────────────────────
# REVISED PUBLIC API (full-record approach)
# ─────────────────────────────────────────────

async def init_storage() -> None:
    """
    Bootstrap storage on startup.
    Reads pinned message from the storage channel to restore in-memory cache.
    Must be awaited once before any other storage function is called.
    """
    global _index_message_id, _records_cache

    logger.info("Initialising Telegram storage (channel=%d)…", STORAGE_CHANNEL_ID)

    # Confirm bot can access the channel
    chat_data = await _tg_post("getChat", {"chat_id": STORAGE_CHANNEL_ID})
    if not chat_data.get("ok"):
        raise RuntimeError(
            f"Cannot access storage channel {STORAGE_CHANNEL_ID}: "
            f"{chat_data.get('description')}. "
            "Check STORAGE_CHANNEL_ID and that the bot is an admin."
        )

    pinned = chat_data["result"].get("pinned_message")
    if pinned:
        text: str = pinned.get("text", "")
        decoded = _decode_full_index(text)
        if decoded:
            _records_cache = decoded
            _index_message_id = pinned["message_id"]
            logger.info(
                "Storage loaded from pinned index (msg_id=%d, %d bots).",
                _index_message_id, len(_records_cache),
            )
            return

    logger.info("No valid pinned index found. Starting with empty storage.")
    await _persist_full_index()


async def upsert_bot(
    bot_id: int,
    bot_username: str,
    bot_token: str,
    owner_id: int,
    owner_username: str = "unknown",
) -> bool:
    """
    Insert or update a child bot record.
    Stores the full record in the pinned index message.
    Also posts a human-readable audit log message to the channel.
    Returns True on success.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Preserve original created_at if record already exists
    existing = _records_cache.get(bot_id)
    created_at = existing["created_at"] if existing else now

    record: dict = {
        "bot_id": bot_id,
        "bot_username": bot_username,
        "bot_token": bot_token,
        "owner_id": owner_id,
        "owner_username": owner_username,
        "created_at": created_at,
        "updated_at": now,
    }

    _records_cache[bot_id] = record

    # Persist index
    ok = await _persist_full_index()
    if not ok:
        # Roll back in-memory state to avoid divergence
        if existing:
            _records_cache[bot_id] = existing
        else:
            _records_cache.pop(bot_id, None)
        return False

    # Audit log message (non-critical — failure here is acceptable)
    audit_text = (
        f"📝 #bot_record\n"
        f"Bot: @{bot_username} (ID: {bot_id})\n"
        f"Owner: @{owner_username} (ID: {owner_id})\n"
        f"Created: {created_at[:19]}Z"
    )
    await _tg_post("sendMessage", {
        "chat_id": STORAGE_CHANNEL_ID,
        "text": audit_text,
        "disable_notification": True,
    })

    logger.info("Upserted bot @%s (owner=%d).", bot_username, owner_id)
    return True


def get_bots_by_owner(owner_id: int) -> list[dict]:
    """Return all child bots belonging to a given owner (synchronous — uses cache)."""
    return sorted(
        [r for r in _records_cache.values() if r["owner_id"] == owner_id],
        key=lambda r: r["created_at"],
        reverse=True,
    )


def get_all_bots() -> list[dict]:
    """Return all child bots across all owners (synchronous — uses cache)."""
    return sorted(
        _records_cache.values(),
        key=lambda r: r["created_at"],
        reverse=True,
    )


async def delete_bot(bot_id: int) -> bool:
    """Remove a child bot record. Returns True if it existed and was removed."""
    if bot_id not in _records_cache:
        return False
    _records_cache.pop(bot_id)
    ok = await _persist_full_index()
    if not ok:
        logger.error("Failed to persist index after deleting bot %d.", bot_id)
    return ok
