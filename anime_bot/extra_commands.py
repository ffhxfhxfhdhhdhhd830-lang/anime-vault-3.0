"""Owner-safe utility commands adapted for the Anime Vault bot.

Media upload uses the envs.sh public upload endpoint, matching the reference bot's
Telegraph-style behavior. Never place credentials in logs or log messages.
"""
from __future__ import annotations

import html
import io
import logging
import os
import re
import sys
import asyncio
from datetime import datetime, timezone
from collections import deque
from urllib.parse import quote_plus

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

LOG = logging.getLogger("anime_bot")
RECENT_LOGS: deque[str] = deque(maxlen=120)


class RecentLogHandler(logging.Handler):
    """Keep a small in-memory diagnostic tail; no log file or secrets required."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            message = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", message)
            message = re.sub(r"(?i)(token|api[_ -]?key|password|secret)(\s*[=:]\s*)\S+", r"\1\2[REDACTED]", message)
            RECENT_LOGS.append(message[:900])
        except Exception:
            self.handleError(record)


_recent_handler = RecentLogHandler()
_recent_handler.setLevel(logging.WARNING)
_recent_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_recent_handler)


async def _has_access(update: Update, context: ContextTypes.DEFAULT_TYPE, *, owner_only: bool = False) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False
    if update.effective_chat and update.effective_chat.type != "private":
        await message.reply_text("Please use this command in a private chat.")
        return False
    settings = context.application.bot_data["settings"]
    is_owner = user.id == settings.owner_telegram_id
    if owner_only and not is_owner:
        await message.reply_text("Owner only.")
        return False
    if not is_owner:
        db = context.application.bot_data["db"]
        authorized = await db.authorized_users.find_one({"user_id": user.id, "blocked": {"$ne": True}})
        if not authorized:
            await message.reply_text("Access denied. Use /request_access to ask the owner.")
            return False
    await _log_event(context, f"🧭 <b>Command used</b>\nCommand: <code>{html.escape(message.text or '/media')}</code>\nUser: {html.escape(user.full_name or 'Unknown')} (<code>{user.id}</code>)")
    return True


async def _log_event(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    settings = context.application.bot_data.get("settings")
    channel_id = settings.log_channel_id if settings else None
    if channel_id is None:
        return
    try:
        await context.bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramError as exc:
        LOG.warning("LOG_CHANNEL delivery failed (%s): %s", type(exc).__name__, exc)


async def _record_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user and message:
        await _log_event(
            context,
            "🧭 <b>Command used</b>\n"
            f"Command: <code>{html.escape(message.text or '')}</code>\n"
            f"User: {html.escape(user.full_name or 'Unknown')} "
            f"(<code>{user.id}</code>)",
        )


async def _is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user or chat.type not in {"group", "supergroup"}:
        return False
    if user.id == context.application.bot_data["settings"].owner_telegram_id:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


async def _owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return await _has_access(update, context, owner_only=True)


def _font_text(value: str, style: str) -> str:
    # Unicode mathematical alphabets. Characters without a matching Unicode
    # glyph are retained unchanged.
    starts = {
        "bold": (0x1D400, 0x1D41A),
        "italic": (0x1D434, 0x1D44E),
        "bold_italic": (0x1D468, 0x1D482),
        "script": (0x1D4D0, 0x1D4EA),
        "fraktur": (0x1D504, 0x1D51E),
        "double": (0x1D538, 0x1D552),
        "sans": (0x1D5A0, 0x1D5BA),
        "sans_bold": (0x1D5D4, 0x1D5EE),
        "mono": (0x1D670, 0x1D68A),
    }
    if style == "smallcaps":
        small = str.maketrans({
            **dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ")),
        })
        return value.lower().translate(small)
    upper, lower = starts.get(style, starts["bold"])
    result = []
    for char in value:
        if "A" <= char <= "Z":
            code = upper + ord(char) - ord("A")
            # Some mathematical alphabets have Unicode holes for legacy symbols.
            if code in {0x1D455, 0x1D49D, 0x1D4A0, 0x1D4A1, 0x1D4A3, 0x1D4A4, 0x1D4A7, 0x1D4A8, 0x1D4AD, 0x1D4BA, 0x1D4BC, 0x1D4C4, 0x1D506, 0x1D50B, 0x1D50C, 0x1D515, 0x1D51D, 0x1D53A, 0x1D53F, 0x1D545, 0x1D547, 0x1D548, 0x1D549, 0x1D551}:
                result.append(char)
            else:
                result.append(chr(code))
        elif "a" <= char <= "z":
            result.append(chr(lower + ord(char) - ord("a")))
        elif style == "mono" and "0" <= char <= "9":
            result.append(chr(0x1D7F6 + ord(char) - ord("0")))
        elif style in {"bold", "sans_bold"} and "0" <= char <= "9":
            result.append(chr((0x1D7CE if style == "bold" else 0x1D7EC) + ord(char) - ord("0")))
        else:
            result.append(char)
    return "".join(result)


FONT_STYLES = {
    "bold": "𝐁𝐨𝐥𝐝",
    "italic": "𝘐𝘵𝘢𝘭𝘪𝘤",
    "bold_italic": "𝑩𝒐𝒍𝒅 𝑰𝒕𝒂𝒍𝒊𝒄",
    "script": "𝓢𝓬𝓻𝓲𝓹𝓽",
    "fraktur": "𝔉𝔯𝔞𝔨𝔱𝔲𝔯",
    "double": "𝔻𝕠𝕦𝕓𝕝𝕖",
    "sans": "𝖲𝖺𝗇𝗌",
    "sans_bold": "𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱",
    "mono": "𝙼𝚘𝚗𝚘",
    "smallcaps": "Sᴍᴀʟʟ Cᴀᴘs",
}


async def font_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context):
        return
    message = update.effective_message
    original = " ".join(context.args or []).strip()
    if not original and message.reply_to_message:
        original = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not original:
        await message.reply_text("Usage: /font <text>\nExample: /font Hello Darling")
        return
    if len(original) > 700:
        await message.reply_text("Please keep font text under 700 characters.")
        return
    context.user_data["font_original"] = original
    rows = []
    keys = list(FONT_STYLES)
    for i in range(0, len(keys), 2):
        rows.append([InlineKeyboardButton(FONT_STYLES[k], callback_data=f"font:{k}") for k in keys[i:i+2]])
    await message.reply_text(
        f"Choose a style for:\n<code>{html.escape(original)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def font_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await _has_access(update, context):
        return
    await query.answer()
    style = (query.data or "").partition(":")[2]
    original = context.user_data.get("font_original")
    if style not in FONT_STYLES or not original:
        await query.edit_message_text("Please run /font <text> again.")
        return
    converted = _font_text(original, style)
    await query.edit_message_text(
        f"<code>{html.escape(converted)}</code>\n\nTap and hold to copy.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Styles", callback_data="font:back")]]),
    )
    context.user_data["font_original"] = original


async def font_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await _has_access(update, context):
        return
    await query.answer()
    original = context.user_data.get("font_original")
    if not original:
        await query.edit_message_text("Please run /font <text> again.")
        return
    rows = []
    keys = list(FONT_STYLES)
    for i in range(0, len(keys), 2):
        rows.append([InlineKeyboardButton(FONT_STYLES[k], callback_data=f"font:{k}") for k in keys[i:i+2]])
    await query.edit_message_text(
        f"Choose a style for:\n<code>{html.escape(original)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def stickerid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context):
        return
    message = update.effective_message
    target = message.reply_to_message
    if target and target.sticker:
        await _send_sticker_id(target, message)
        return
    context.user_data["awaiting_sticker"] = True
    await message.reply_text("Send me a sticker, or reply to a sticker with /stickerid.")


async def _send_sticker_id(sticker_message, reply_to) -> None:
    sticker = sticker_message.sticker
    await reply_to.reply_text(
        "Sticker ID:\n<code>" + html.escape(sticker.file_id) + "</code>\n\nUnique ID:\n<code>" +
        html.escape(sticker.file_unique_id) + "</code>",
        parse_mode="HTML",
    )


async def telegraph_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context):
        return
    message = update.effective_message
    target = message.reply_to_message
    if target and (target.photo or target.video or (target.document and (target.document.mime_type or "").startswith(("image/", "video/")))):
        await _upload_media(target, context)
        return
    context.user_data["awaiting_telegraph"] = True
    await message.reply_text("Send or forward a photo/video under 5 MB. I'll return a public media link.")


async def pending_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not update.effective_user:
        return
    if context.user_data.get("awaiting_sticker"):
        if message.sticker:
            context.user_data.pop("awaiting_sticker", None)
            await _send_sticker_id(message, message)
        else:
            await message.reply_text("That isn't a sticker. Send a sticker or use /cancel.")
        return
    if context.user_data.get("awaiting_telegraph"):
        if message.photo or message.video or (message.document and (message.document.mime_type or "").startswith(("image/", "video/"))):
            context.user_data.pop("awaiting_telegraph", None)
            await _upload_media(message, context)
        else:
            await message.reply_text("Only photos and videos are supported. Send one or use /cancel.")


async def repo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        return await update.effective_message.reply_text("Usage: /repo <search terms>")
    status = await update.effective_message.reply_text("Searching GitHub…")
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "per_page": 1},
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            data = response.json()
        items = data.get("items") or []
        if not items:
            return await status.edit_text("No matching GitHub repositories found.")
        item = items[0]
        description = html.escape(str(item.get("description") or "No description"))
        name = html.escape(str(item.get("full_name") or item.get("name") or "Repository"))
        language = html.escape(str(item.get("language") or "Unknown"))
        text = (
            f"<b>{name}</b>\n{description}\n\n"
            f"⭐ {item.get('stargazers_count', 0)} · "
            f"🍴 {item.get('forks_count', 0)} · "
            f"Open issues: {item.get('open_issues_count', 0)}\n"
            f"Language: {language}\n"
            f"<a href=\"{html.escape(str(item.get('html_url', '')), quote=True)}\">Open repository</a>"
        )
        await status.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except (httpx.HTTPError, ValueError) as exc:
        LOG.warning("GitHub repository search failed (%s)", type(exc).__name__)
        await status.edit_text("GitHub search is unavailable right now. Try again later.")


async def imdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context):
        return
    title = " ".join(context.args or []).strip()
    if not title:
        return await update.effective_message.reply_text("Usage: /imdb <anime title>")
    db = context.application.bot_data["db"]
    # Show verified catalogue data, and link directly to IMDb's title search.
    # We do not invent IMDb scores when the catalogue has no verified IMDb ID/rating.
    from anime_bot.services.core import AnimeService

    rows = await AnimeService(db).search(title, limit=5)
    query_url = "https://www.imdb.com/find/?q=" + quote_plus(title)
    if not rows:
        await update.effective_message.reply_text(
            "No exact catalogue match. Search IMDb for the title here:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Search IMDb", url=query_url)]]
            ),
        )
        return
    anime = rows[0]
    name = html.escape(str(anime.get("title") or title))
    genres = html.escape(", ".join(anime.get("genres") or []) or "Not listed")
    fmt = html.escape(str(anime.get("format") or "Unknown"))
    synopsis = html.escape(str(anime.get("synopsis") or "Synopsis unavailable"))
    try:
        rating = float(anime.get("imdb_rating"))
        rating_text = f"IMDb rating: {rating:g}/10\n" if 0 <= rating <= 10 else ""
    except (TypeError, ValueError):
        rating_text = ""
    imdb_id = str(anime.get("imdb_id") or "")
    imdb_url = f"https://www.imdb.com/title/{imdb_id}/" if re.fullmatch(r"tt\d+", imdb_id) else query_url
    await update.effective_message.reply_text(
        f"<b>{name}</b>\nFormat: {fmt}\nGenres: {genres}\n{rating_text}\n{synopsis}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open IMDb", url=imdb_url)]]),
        disable_web_page_preview=True,
    )


async def all_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner(update, context):
        return
    db = context.application.bot_data["db"]
    rows = await db.users.find({}).sort("user_id", 1).limit(100).to_list(100)
    if not rows:
        return await update.effective_message.reply_text("No users are registered yet.")
    lines = [f"<code>{row.get('user_id')}</code>  {html.escape(str(row.get('first_name') or 'Unknown'))}" for row in rows]
    await update.effective_message.reply_text(
        "<b>Registered users (first 100)</b>\n" + "\n".join(lines), parse_mode="HTML"
    )


async def chats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner(update, context):
        return
    db = context.application.bot_data["db"]
    rows = await db.chats.find({}).sort("title", 1).limit(100).to_list(100)
    if not rows:
        return await update.effective_message.reply_text("No groups or channels have been recorded yet.")
    lines = [
        f"{html.escape(str(row.get('title') or 'Untitled'))} · "
        f"{html.escape(str(row.get('chat_type') or 'group'))} · "
        f"<code>{row.get('chat_id')}</code>"
        for row in rows
    ]
    await update.effective_message.reply_text(
        f"<b>Known chats ({len(rows)})</b>\n" + "\n".join(lines), parse_mode="HTML"
    )


async def channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner(update, context):
        return
    db = context.application.bot_data["db"]
    rows = await db.chats.find({"chat_type": "channel", "bot_status": {"$ne": "left"}}).limit(100).to_list(100)
    if not rows:
        return await update.effective_message.reply_text("No channels are connected yet.")
    lines = [f"{html.escape(str(row.get('title') or 'Untitled channel'))} · <code>{row.get('chat_id')}</code>" for row in rows]
    await update.effective_message.reply_text(
        f"<b>Connected channels: {len(rows)}</b>\n" + "\n".join(lines), parse_mode="HTML"
    )


async def logtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner(update, context):
        return
    settings = context.application.bot_data["settings"]
    if settings.log_channel_id is None:
        return await update.effective_message.reply_text("LOG_CHANNEL is empty in this process's environment.")
    try:
        sent = await context.bot.send_message(
            chat_id=settings.log_channel_id,
            text=f"✅ LOG_CHANNEL test passed. Bot ID: <code>{context.bot.id}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        LOG.exception("LOG_CHANNEL test delivery failed")
        return await update.effective_message.reply_text(
            f"Could not post to LOG_CHANNEL ({type(exc).__name__}). Check the bot's channel permissions and the numeric channel ID."
        )
    await update.effective_message.reply_text(f"Test message delivered (Telegram message ID {sent.message_id}).")


async def _connection_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    try:
        chat = await context.bot.get_chat(chat_id)
        if chat.type not in {"group", "supergroup"}:
            await update.effective_message.reply_text("Connections are for groups and supergroups.")
            return False
        member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            await update.effective_message.reply_text("You must be an administrator of that group.")
            return False
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            await update.effective_message.reply_text("Add the bot as a group administrator first.")
            return False
    except TelegramError:
        await update.effective_message.reply_text("I can’t access that group. Check its ID and add me there as an administrator.")
        return False
    return True


async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type in {"group", "supergroup"}:
        target_id = chat.id
    elif context.args and re.fullmatch(r"-?\d+", context.args[0]):
        target_id = int(context.args[0])
    else:
        return await update.effective_message.reply_text("Usage in private chat: /connect <group_id>\nIn the group, send /connect.")
    if not await _connection_admin(update, context, target_id):
        return
    db = context.application.bot_data["db"]
    group = await context.bot.get_chat(target_id)
    await db.connections.update_one(
        {"user_id": update.effective_user.id, "chat_id": target_id},
        {"$setOnInsert": {"user_id": update.effective_user.id, "chat_id": target_id, "title": group.title, "connected_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await db.chats.update_one(
        {"chat_id": target_id},
        {"$set": {"chat_id": target_id, "title": group.title, "chat_type": group.type, "bot_status": "administrator", "last_seen_at": datetime.now(timezone.utc)}, "$setOnInsert": {"registered_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await update.effective_message.reply_text(f"Connected to {group.title or target_id}.")


async def connections_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context):
        return
    db = context.application.bot_data["db"]
    rows = await db.connections.find({"user_id": update.effective_user.id}).sort("connected_at", -1).to_list(100)
    if not rows:
        return await update.effective_message.reply_text("No connected groups. Use /connect <group_id> in private chat or /connect in a group where you are an admin.")
    lines = [f"{html.escape(str(row.get('title') or 'Group'))} · <code>{row['chat_id']}</code>" for row in rows]
    await update.effective_message.reply_text("<b>Your connected groups</b>\n" + "\n".join(lines), parse_mode="HTML")


async def disconnect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type in {"group", "supergroup"}:
        target_id = chat.id
    elif context.args and re.fullmatch(r"-?\d+", context.args[0]):
        target_id = int(context.args[0])
    else:
        return await update.effective_message.reply_text("Usage: /disconnect <group_id> in private chat, or /disconnect in the group.")
    if chat and chat.type in {"group", "supergroup"}:
        if not await _is_chat_admin(update, context):
            return await update.effective_message.reply_text("Group administrators only.")
    db = context.application.bot_data["db"]
    result = await db.connections.delete_one({"user_id": update.effective_user.id, "chat_id": target_id})
    await update.effective_message.reply_text("Disconnected." if result.deleted_count else "That group is not connected to your account.")


async def _toggle_group(update: Update, context: ContextTypes.DEFAULT_TYPE, disabled: bool) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in {"group", "supergroup"}:
        return await update.effective_message.reply_text("Use this command inside the group.")
    if not await _is_chat_admin(update, context):
        return await update.effective_message.reply_text("Group administrators only.")
    db = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    await db.chats.update_one(
        {"chat_id": chat.id},
        {"$set": {"chat_id": chat.id, "title": chat.title, "chat_type": chat.type, "disabled": disabled, "last_seen_at": now}, "$setOnInsert": {"registered_at": now}},
        upsert=True,
    )
    await update.effective_message.reply_text("Bot commands disabled in this group." if disabled else "Bot commands enabled in this group.")


async def disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_group(update, context, True)


async def enable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_group(update, context, False)


async def leave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in {"group", "supergroup"}:
        return await update.effective_message.reply_text("Use /leave inside the group you want me to leave.")
    if not await _is_chat_admin(update, context):
        return await update.effective_message.reply_text("Group administrators only.")
    await update.effective_message.reply_text("Leaving this group.")
    await context.bot.leave_chat(chat.id)
    await context.application.bot_data["db"].chats.update_one(
        {"chat_id": chat.id}, {"$set": {"bot_status": "left", "last_seen_at": datetime.now(timezone.utc)}}
    )


async def chat_membership_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member_update = update.my_chat_member
    if not member_update:
        return
    chat = member_update.chat
    new_status = member_update.new_chat_member.status
    old_status = member_update.old_chat_member.status
    now = datetime.now(timezone.utc)
    db = context.application.bot_data["db"]
    chat_doc = {
        "chat_id": chat.id,
        "title": chat.title or chat.full_name or "Untitled chat",
        "chat_type": chat.type,
        "bot_status": new_status.value,
        "last_seen_at": now,
    }
    await db.chats.update_one(
        {"chat_id": chat.id},
        {"$set": chat_doc, "$setOnInsert": {"registered_at": now}},
        upsert=True,
    )
    if old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED} and new_status not in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
        await _log_event(
            context,
            "➕ <b>Anime Vault added to a chat</b>\n"
            f"Name: {html.escape(chat_doc['title'])}\nType: {chat.type}\n"
            f"Chat ID: <code>{chat.id}</code>",
        )
    elif new_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
        await _log_event(
            context,
            f"➖ <b>Anime Vault removed from a chat</b>\nChat ID: <code>{chat.id}</code>",
        )


async def _broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, *, groups: bool) -> None:
    if not await _owner(update, context):
        return
    message = update.effective_message
    args = context.args or []
    if not args or args[0].upper() != "CONFIRM":
        name = "groups" if groups else "users"
        return await message.reply_text(
            f"This sends a message to all registered {name}. To confirm, reply to the message you want to send and use /{'grp_broadcast' if groups else 'broadcast'} CONFIRM, or send /{'grp_broadcast' if groups else 'broadcast'} CONFIRM <text>."
        )
    text = " ".join(args[1:]).strip()
    source = message.reply_to_message
    if not text and not source:
        return await message.reply_text("Reply to a message or add text after CONFIRM.")
    db = context.application.bot_data["db"]
    if groups:
        rows = await db.chats.find({"chat_type": {"$in": ["group", "supergroup"]}, "bot_status": {"$nin": ["left", "kicked"]}}).to_list(5000)
        targets = [int(row["chat_id"]) for row in rows if row.get("chat_id")]
    else:
        rows = await db.users.find({}).to_list(10000)
        targets = [int(row["user_id"]) for row in rows if row.get("user_id")]
    status = await message.reply_text(f"Sending to {len(targets)} {'groups' if groups else 'users'}…")
    success = failed = 0
    for target in targets:
        try:
            if source:
                await context.bot.copy_message(
                    chat_id=target,
                    from_chat_id=source.chat_id,
                    message_id=source.message_id,
                )
            else:
                await context.bot.send_message(chat_id=target, text=text)
            success += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"Broadcast finished. Sent: {success}; failed: {failed}.")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _broadcast(update, context, groups=False)


async def grp_broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _broadcast(update, context, groups=True)


async def _upload_media(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await message.reply_text("Uploading media…")
    try:
        if message.photo:
            attachment = message.photo[-1]
            filename, mime = "photo.jpg", "image/jpeg"
        elif message.video:
            attachment = message.video
            filename, mime = message.video.file_name or "video.mp4", message.video.mime_type or "video/mp4"
        elif message.document and (message.document.mime_type or "").startswith(("image/", "video/")):
            attachment = message.document
            filename, mime = message.document.file_name or "media.bin", message.document.mime_type
        else:
            await status.edit_text("Only photos and videos are supported.")
            return
        size = getattr(attachment, "file_size", None)
        if size is not None and size > 5 * 1024 * 1024:
            await status.edit_text("This file is over 5 MB. Please send a smaller photo or video.")
            return
        telegram_file = await context.bot.get_file(attachment.file_id)
        data = io.BytesIO()
        await telegram_file.download_to_memory(out=data)
        payload = data.getvalue()
        if len(payload) > 5 * 1024 * 1024:
            await status.edit_text("This file is over 5 MB. Please send a smaller photo or video.")
            return
        async with httpx.AsyncClient(timeout=40.0, follow_redirects=True) as client:
            response = await client.post("https://envs.sh", files={"file": (filename, payload, mime)})
            response.raise_for_status()
        url = response.text.strip()
        if not re.match(r"^https?://[^\s<>]+$", url):
            raise ValueError("Upload service returned an invalid link.")
        await status.edit_text(
            f"Link:\n<code>{html.escape(url)}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open link", url=url)]]),
            disable_web_page_preview=True,
        )
        await _log_event(context, f"📤 <b>Media link created</b>\nUser ID: <code>{message.from_user.id if message.from_user else 'unknown'}</code>")
    except (httpx.HTTPError, TelegramError, ValueError) as exc:
        LOG.warning("Media upload failed (%s)", type(exc).__name__)
        await status.edit_text("Upload failed. The upload service may be unavailable; please try again later.")


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context, owner_only=True):
        return
    lines = list(RECENT_LOGS)
    if not lines:
        await update.effective_message.reply_text("No warnings or errors have been recorded since startup.")
        return
    output = "\n".join(lines[-60:])
    # Telegram text messages are limited to 4096 chars.
    output = output[-3800:]
    await update.effective_message.reply_text("<pre>" + html.escape(output) + "</pre>", parse_mode="HTML")


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _has_access(update, context, owner_only=True):
        return
    await update.effective_message.reply_text("Restarting Anime Vault now…")
    await _log_event(context, f"🔄 <b>Bot restart requested</b> by owner <code>{update.effective_user.id}</code>")
    # Re-exec the same package entry point, preserving the current interpreter and
    # environment. The process manager should supervise the process in production.
    os.execv(sys.executable, [sys.executable, "-m", "anime_bot.main"])


async def cancel_utility_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_sticker", None)
    context.user_data.pop("awaiting_telegraph", None)
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled the pending utility action.")
