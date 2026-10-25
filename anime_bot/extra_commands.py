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
from collections import deque

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        LOG.warning("LOG_CHANNEL delivery failed (%s)", type(exc).__name__)


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
        return value.translate(small)
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
    await query.answer()
    style = (query.data or "").partition(":")[2]
    original = context.user_data.get("font_original")
    if style not in FONT_STYLES or not original:
        await query.answer("Please run /font again.", show_alert=True)
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
