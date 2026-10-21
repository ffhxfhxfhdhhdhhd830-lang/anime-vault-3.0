import html
import logging
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

from bson import ObjectId
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from anime_bot.config import get_settings
from anime_bot.database.mongo import Mongo
from anime_bot.services.core import (
    AnimeService,
    RecommendationService,
    WatchlistService,
    normalize,
    seed_confirmed_titles,
)
from anime_bot.services.genres import genres_for, normalize_genres
from anime_bot.services.metadata import MetadataService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
# HTTPX INFO logs include full Telegram API URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("anime_bot")
STATUS_LABELS = {
    "plan": "📝 Plan to Watch",
    "watching": "▶️ Watching",
    "completed": "✅ Completed",
    "on_hold": "⏸ On Hold",
    "dropped": "❌ Dropped",
    "rewatching": "🔁 Rewatching",
    "not_interested": "🚫 Not Interested",
    "unknown": "❔ Unknown",
}
STATUS_CHOICES = tuple(k for k in STATUS_LABELS if k != "unknown")
PAGE_SIZE = 8
MAX_CATALOGUE = 5000
START_IMAGES = (
    "https://graph.org/file/dc7fcd70a16a81e6da48f-b633fcf31d25c21088.jpg",
    "https://graph.org/file/9cb5e9c3285fe51ef4b0f-7294b1a2290fdd2c38.jpg",
)
LOADING_FRAMES = ("▣☐☐", "☐▣☐", "☐☐▣")
LOADING_MESSAGES = {}


def services(context):
    db = context.application.bot_data["db"]
    return db, AnimeService(db), WatchlistService(db), RecommendationService(db)


async def allowed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner: bool = False,
    callback_answered: bool = False,
) -> bool:
    settings = context.application.bot_data["settings"]
    user = update.effective_user
    if not user:
        return False
    if (
        update.effective_chat
        and update.effective_chat.type != "private"
        and not settings.allow_groups
    ):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Please use me in a private chat."
            )
        return False
    db = context.application.bot_data["db"]
    is_owner = user.id == settings.owner_telegram_id
    authorized = is_owner or await db.authorized_users.find_one(
        {"user_id": user.id, "blocked": {"$ne": True}}
    )
    if not authorized:
        if update.callback_query:
            if not callback_answered:
                await update.callback_query.answer("Access denied", show_alert=True)
            else:
                try:
                    await update.callback_query.edit_message_text(
                        "Access denied. Use /request_access to ask the owner."
                    )
                except BadRequest as exc:
                    log.warning("Could not edit access-denied response: %s", exc)
                    if update.effective_message:
                        await update.effective_message.reply_text(
                            "Access denied. Use /request_access to ask the owner."
                        )
        elif update.effective_message:
            await update.effective_message.reply_text(
                "Access denied. Use /request_access to ask the owner."
            )
        return False
    if hasattr(db, "users"):
        now = datetime.now(timezone.utc)
        await db.users.update_one(
            {"user_id": user.id},
            {
                "$set": {
                    "user_id": user.id,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "username": user.username,
                    "last_seen_at": now,
                },
                "$setOnInsert": {"registered_at": now},
            },
            upsert=True,
        )
    if owner and not is_owner:
        if update.callback_query and callback_answered:
            try:
                await update.callback_query.edit_message_text("Owner only.")
            except BadRequest as exc:
                log.warning("Could not edit owner-only response: %s", exc)
                if update.effective_message:
                    await update.effective_message.reply_text("Owner only.")
        elif update.effective_message:
            await update.effective_message.reply_text("Owner only.")
        return False
    return True


def current_ui(context):
    return context.user_data.setdefault(
        "anime_ui", {"current": {"kind": "home"}, "back": []}
    )


def _set_current(context, screen, *, push=True, reset=False):
    ui = current_ui(context)
    if reset:
        ui["back"] = []
    elif push:
        ui["back"].append(ui["current"])
        ui["back"] = ui["back"][-20:]
    ui["current"] = screen


async def render(update, text, markup=None):
    """Render every callback screen in-place; commands start one new screen message."""
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                return
            # For old messages that Telegram will not edit, fall back to one fresh screen.
            log.warning("Could not edit callback screen: %s", exc)
    key = _message_key(update)
    target = LOADING_MESSAGES.pop(key, None)
    if target:
        try:
            await target.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                return
            try:
                await target.delete()
            except TelegramError:
                pass
            log.debug("Could not replace loading message: %s", exc)
    message = update.effective_message
    if message:
        try:
            await message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            log.warning("Could not send formatted screen: %s", exc)
            plain_text = html.unescape(re.sub(r"<[^>]*>", "", text))
            # Preserve a visible response even if Telegram rejects rich text/markup.
            try:
                await message.reply_text(plain_text, disable_web_page_preview=True)
            except TelegramError as fallback_exc:
                log.error("Plain-text fallback failed: %s", fallback_exc)


def _message_key(update):
    chat = update.effective_chat
    user = update.effective_user
    return (chat.id if chat else 0, user.id if user else 0)


async def _animate_loading(target, stop, label, interval=1.1):
    frame_index = 1
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
        text = f"{LOADING_FRAMES[frame_index]}\n{label}"
        frame_index = (frame_index + 1) % len(LOADING_FRAMES)
        try:
            await target.edit_text(text)
        except RetryAfter as exc:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=float(exc.retry_after) + 0.1
                )
            except asyncio.TimeoutError:
                continue
        except BadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                continue
            return
        except TelegramError:
            return


async def run_with_loading(update, context, operation, label="Loading…", threshold=0.8):
    """Run an awaitable and animate only after it exceeds the short-operation threshold."""
    lock = context.user_data.setdefault("_loading_lock", asyncio.Lock())
    async with lock:
        task = asyncio.create_task(operation())
        target = None
        stop = asyncio.Event()
        animator = None
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=threshold)
            except asyncio.TimeoutError:
                if task.done():
                    return task.result()
            frame = f"{LOADING_FRAMES[0]}\n{label}"
            query = update.callback_query
            if query:
                target = query.message
                try:
                    await query.edit_message_text(frame)
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).casefold():
                        log.debug("Could not show callback loading frame: %s", exc)
            elif update.effective_message:
                target = await update.effective_message.reply_text(frame)
                LOADING_MESSAGES[_message_key(update)] = target
            if target:
                animator = asyncio.create_task(_animate_loading(target, stop, label))
            return await task
        except asyncio.CancelledError:
            task.cancel()
            if target:
                if update.callback_query:
                    try:
                        await target.edit_text("Operation cancelled.")
                    except TelegramError:
                        pass
                else:
                    LOADING_MESSAGES.pop(_message_key(update), None)
                    try:
                        await target.delete()
                    except TelegramError:
                        pass
            raise
        finally:
            stop.set()
            if animator:
                animator.cancel()
                await asyncio.gather(animator, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def _render_current(update, context):
    try:
        return await run_with_loading(
            update, context, lambda: _render_screen(update, context), "Loading…"
        )
    except Exception as exc:
        log.warning("Could not render screen: %s", exc)
        return await render(
            update,
            "<b><blockquote>⚠️ ᴜɴᴀʙʟᴇ ᴛᴏ ʟᴏᴀᴅ</blockquote></b>\nPlease try again.",
            home_markup(),
        )


def home_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎌 Search", callback_data="v1:search"),
                InlineKeyboardButton("📚 Watchlist", callback_data="v1:watchlist"),
            ],
            [InlineKeyboardButton("⭐ Favorites", callback_data="v1:favorites")],
            [
                InlineKeyboardButton(
                    "▶️ Watching", callback_data="v1:statuslist:watching"
                ),
                InlineKeyboardButton(
                    "✅ Completed", callback_data="v1:statuslist:completed"
                ),
            ],
            [
                InlineKeyboardButton("📝 Plan", callback_data="v1:statuslist:plan"),
                InlineKeyboardButton(
                    "⏸ On Hold", callback_data="v1:statuslist:on_hold"
                ),
            ],
            [
                InlineKeyboardButton(
                    "✨ Recommendations", callback_data="v1:genres:recommend"
                ),
                InlineKeyboardButton("⭐ Top by genre", callback_data="v1:genres:top"),
            ],
            [
                InlineKeyboardButton("🎬 Movies", callback_data="v1:genres:movie"),
                InlineKeyboardButton("📺 Series", callback_data="v1:genres:series"),
            ],
            [
                InlineKeyboardButton("❔ Help", callback_data="v1:help"),
                InlineKeyboardButton("ℹ️ About", callback_data="v1:about"),
            ],
            [InlineKeyboardButton("📊 Statistics", callback_data="v1:stats")],
        ]
    )


def navigation_row(*, back=True, home=True):
    row = []
    if back:
        row.append(InlineKeyboardButton("⬅️ Back", callback_data="v1:back"))
    if home:
        row.append(InlineKeyboardButton("🏠 Home", callback_data="v1:home"))
    return [row] if row else []


def _genre_values(anime):
    return genres_for(anime)


def _format_kind(value):
    value = normalize(str(value or ""))
    if value in {"movie", "movies", "film", "feature film", "anime movie"}:
        return "movie"
    if value in {"series", "tv", "tv series", "tv anime", "anime series", "television"}:
        return "series"
    return value


def _verified_catalogue_rating(anime):
    source = str(
        anime.get("rating_source") or anime.get("metadata_source") or ""
    ).casefold()
    if not any(name in source for name in ("imdb", "omdb")):
        return None
    try:
        value = float(anime.get("imdb_rating"))
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 10:
        return None
    return value


async def _catalogue(db):
    rows = await db.anime.find({}).limit(MAX_CATALOGUE).to_list(MAX_CATALOGUE)
    return _dedupe_titles(rows)


def _dedupe_titles(rows):
    unique = {}
    for anime in rows:
        key = (normalize(str(anime.get("title", ""))), anime.get("year"))
        unique.setdefault(key, anime)
    return sorted(
        unique.values(),
        key=lambda a: (normalize(str(a.get("title", ""))), str(a.get("year", ""))),
    )


async def _watch_map(db, user_id):
    rows = await db.watchlist.find({"user_id": user_id}).to_list(MAX_CATALOGUE)
    return {str(row["anime_id"]): row for row in rows}


async def _registered_users(db):
    """Merge the current user collection with legacy authorization/request records."""
    merged = {}
    for collection_name, query in (
        ("users", {}),
        ("authorized_users", {}),
        ("access_requests", {}),
    ):
        collection = getattr(db, collection_name, None)
        if collection is None:
            continue
        for row in await collection.find(query).to_list(10000):
            if "user_id" not in row:
                continue
            user_id = int(row["user_id"])
            merged.setdefault(user_id, {}).update(
                {key: value for key, value in row.items() if value is not None}
            )
    return [merged[user_id] for user_id in sorted(merged)]


async def screen_genres(db, screen, user_id, metadata=None):
    mode = screen["mode"]
    catalogue = await _catalogue(db)
    if mode in {"movie", "series"}:
        catalogue = [a for a in catalogue if _format_kind(a.get("format")) == mode]
    if mode == "recommend":
        watched = await _watch_map(db, user_id)
        catalogue = [a for a in catalogue if str(a["_id"]) not in watched]
    if metadata:
        await metadata.enrich_many(catalogue, limit=20)
    genre_to_ids = {}
    for anime in catalogue:
        for genre in _genre_values(anime):
            key = genre.casefold()
            genre_to_ids.setdefault(key, genre)
    return [genre_to_ids[key] for key in sorted(genre_to_ids)]


async def _list_titles(db, user_id, screen, metadata=None):
    mode = screen["mode"]
    genre = screen.get("genre")
    if mode in {"watchlist", "statuslist", "favorites"}:
        rows = await WatchlistService(db).list(
            user_id,
            screen.get("status"),
            limit=MAX_CATALOGUE,
            favourite=True if mode == "favorites" else None,
        )
        if mode == "favorites":
            return rows, "Your favorites"
        return rows, "Your watchlist" if mode == "watchlist" else STATUS_LABELS.get(screen.get("status"), "Watchlist")
    if mode in {"search", "wtw"}:
        rows = await AnimeService(db).search(screen.get("query", ""), limit=100)
        heading = (
            "Choose a title for Where to Watch"
            if mode == "wtw"
            else f"Search results for <b>{html.escape(screen.get('query', ''))}</b>"
        )
        return _dedupe_titles(rows), heading
    catalogue = await _catalogue(db)
    if mode in {"movie", "series"}:
        catalogue = [a for a in catalogue if _format_kind(a.get("format")) == mode]
    if metadata:
        await metadata.enrich_many(catalogue, limit=20)
    if genre:
        catalogue = [
            a
            for a in catalogue
            if any(g.casefold() == genre.casefold() for g in _genre_values(a))
        ]
    if mode == "recommend":
        rec = RecommendationService(db)
        values = await rec.recommend(user_id, limit=MAX_CATALOGUE, genre=genre)
        return [
            a for a, _ in values
        ], f"Recommendations · {html.escape(genre or 'all genres')}"
    if mode == "top":
        watches = await _watch_map(db, user_id)
        rated = []
        for anime in catalogue:
            row = watches.get(str(anime["_id"]))
            rating = (row or {}).get("rating")
            if rating is not None:
                try:
                    anime["_display_rating"] = float(rating)
                    rated.append((float(rating), anime))
                except (ValueError, TypeError):
                    continue
        # If there are no personal ratings in this genre, use only catalogue
        # ratings with an explicit IMDb/OMDb source marker.
        if not rated:
            for anime in catalogue:
                rating = _verified_catalogue_rating(anime)
                if rating is not None:
                    anime["_display_rating"] = rating
                    rated.append((rating, anime))
        rated.sort(key=lambda item: (-item[0], item[1].get("title", "").casefold()))
        label = (
            "Your top rated"
            if any(
                (watches.get(str(a["_id"]), {}).get("rating") is not None)
                for a in catalogue
            )
            else "Verified IMDb ratings"
        )
        return [a for _, a in rated], f"{label} · {html.escape(genre or '')}"
    catalogue.sort(key=lambda a: str(a.get("title", "")).casefold())
    heading = {"movie": "Movies", "series": "Series"}.get(mode, "Anime")
    return catalogue, f"{heading} · {html.escape(genre or 'all genres')}"


def _title_keyboard(rows, page, mode):
    page_count = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    buttons = []
    for anime in rows[start : start + PAGE_SIZE]:
        title = str(anime.get("title", "Untitled"))
        year = anime.get("year")
        label = f"{title} ({year})" if year else title
        if anime.get("_display_rating") is not None:
            label += f" · ⭐ {anime['_display_rating']:g}/10"
        button = InlineKeyboardButton(
            label[:60], callback_data=f"v1:anime:{anime['_id']}"
        )
        if len(label) > 30:
            buttons.append(None)
            buttons.append(button)
        else:
            buttons.append(button)
    keyboard = []
    pending = []
    for button in buttons:
        if button is None:
            if pending:
                keyboard.append(pending)
                pending = []
        elif len(button.text) > 30:
            if pending:
                keyboard.append(pending)
                pending = []
            keyboard.append([button])
        else:
            pending.append(button)
            if len(pending) == 2:
                keyboard.append(pending)
                pending = []
    if pending:
        keyboard.append(pending)
    if page_count > 1:
        pages = []
        if page > 0:
            pages.append(
                InlineKeyboardButton("⬅️ Previous", callback_data=f"v1:page:{page - 1}")
            )
        pages.append(
            InlineKeyboardButton(
                f"{page + 1}/{page_count}", callback_data="v1:page:noop"
            )
        )
        if page + 1 < page_count:
            pages.append(
                InlineKeyboardButton("Next ➡️", callback_data=f"v1:page:{page + 1}")
            )
        keyboard.append(pages)
    keyboard.extend(navigation_row())
    return InlineKeyboardMarkup(keyboard)


def _wtw_screen(title, offers):
    lines = [
        "<b><blockquote>📺 ᴡʜᴇʀᴇ ᴛᴏ ᴡᴀᴛᴄʜ</blockquote></b>",
        f"{html.escape(str(title or 'Untitled'))} · Region {html.escape(offers['region'])}",
    ]
    buttons = []
    if offers["available"]:
        lines.append("<b>Verified provider listings</b>")
        lines.extend(
            f"• {html.escape(item['name'])} · {html.escape(item['type'].title())}"
            for item in offers["offers"]
        )
        if offers.get("link"):
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Open TMDb provider listings", url=offers["link"]
                    )
                ]
            )
    else:
        lines.append(
            "No verified streaming offers could be retrieved for this title and region."
        )
    query = quote_plus(str(title or ""))
    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    "Search Netflix", url=f"https://www.netflix.com/search?q={query}"
                ),
                InlineKeyboardButton(
                    "Search Crunchyroll",
                    url=f"https://www.crunchyroll.com/search?q={query}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Search Prime Video",
                    url=f"https://www.primevideo.com/search?phrase={query}",
                )
            ],
        ]
    )
    lines.append(
        "Search buttons open the service’s title search; they do not confirm availability."
    )
    buttons.extend(navigation_row())
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def _render_screen(update, context):
    db = context.application.bot_data["db"]
    metadata = context.application.bot_data.get("metadata")
    uid = update.effective_user.id
    screen = current_ui(context)["current"]
    kind = screen["kind"]
    if kind == "home":
        return await render(
            update,
            "🎌 <b>Anime Command Center</b>\nYour private anime tracker. Choose an action:",
            home_markup(),
        )
    if kind == "help":
        return await render(
            update,
            help_text(),
            InlineKeyboardMarkup(navigation_row()),
        )
    if kind == "about":
        return await render(
            update,
            await about_text(context),
            InlineKeyboardMarkup(navigation_row()),
        )
    if kind == "wtw_search_empty":
        return await render(
            update,
            f"No local catalogue match for <b>{html.escape(str(screen.get('query', '')))}</b>. Add the title with /add, then try /wtw again.",
            InlineKeyboardMarkup(navigation_row()),
        )
    if kind == "wtw_external":
        title = str(screen.get("title", ""))
        region = context.application.bot_data["settings"].default_region
        offers = (
            await metadata.watch_offers({"title": title, "format": "unknown"}, region)
            if metadata
            else {"region": region, "offers": [], "available": False}
        )
        text, markup = _wtw_screen(title, offers)
        return await render(update, text, markup)
    if kind == "genres":
        genres = await screen_genres(db, screen, uid, metadata)
        if not genres:
            return await render(
                update,
                "No genres are available in the matching catalogue entries yet.",
                InlineKeyboardMarkup(navigation_row()),
            )
        page = max(0, min(int(screen.get("page", 0)), (len(genres) - 1) // PAGE_SIZE))
        screen["page"] = page
        first = page * PAGE_SIZE
        keyboard = [
            [InlineKeyboardButton(g[:60], callback_data=f"v1:genre:{i}")]
            for i, g in enumerate(genres[first : first + PAGE_SIZE], start=first)
        ]
        if len(genres) > PAGE_SIZE:
            pages = []
            if page > 0:
                pages.append(
                    InlineKeyboardButton(
                        "⬅️ Previous", callback_data=f"v1:gpage:{page - 1}"
                    )
                )
            pages.append(
                InlineKeyboardButton(
                    f"{page + 1}/{(len(genres) + PAGE_SIZE - 1) // PAGE_SIZE}",
                    callback_data="v1:gpage:noop",
                )
            )
            if first + PAGE_SIZE < len(genres):
                pages.append(
                    InlineKeyboardButton("Next ➡️", callback_data=f"v1:gpage:{page + 1}")
                )
            keyboard.append(pages)
        keyboard.extend(navigation_row())
        title = {
            "recommend": "Choose a genre for recommendations",
            "top": "Choose a genre to see your highest personal ratings",
            "movie": "Choose a movie genre",
            "series": "Choose a series genre",
        }.get(screen["mode"], "Choose a genre")
        return await render(
            update,
            f"<b><blockquote>🎭 {title}</blockquote></b>",
            InlineKeyboardMarkup(keyboard),
        )
    if kind == "titles":
        rows, heading = await _list_titles(db, uid, screen, metadata)
        rows = _dedupe_titles(rows)
        if not rows:
            if screen["mode"] == "top":
                message = "No personal or verified catalogue ratings are available for this genre yet. Rate a title in your watchlist to build your ranking."
            elif screen["mode"] == "recommend":
                message = "No recommendations are available in this genre yet. Add catalogue titles or update your watch history."
            else:
                message = "No titles found here yet."
            return await render(
                update,
                f"<b><blockquote>{heading}</blockquote></b>\n{message}",
                InlineKeyboardMarkup(navigation_row()),
            )
        page = max(0, min(int(screen.get("page", 0)), (len(rows) - 1) // PAGE_SIZE))
        screen["page"] = page
        keyboard = _title_keyboard(rows, page, screen["mode"])
        return await render(
            update,
            f"<b><blockquote>{heading}</blockquote></b> · {len(rows)} title(s)\nChoose a title:",
            keyboard,
        )
    if kind == "detail":
        anime = await db.anime.find_one({"_id": ObjectId(screen["anime_id"])})
        if not anime:
            await _set_current(context, {"kind": "home"}, push=False, reset=True)
            return await render(
                update, "That catalogue title is no longer available.", home_markup()
            )
        if metadata:
            await metadata.enrich_genres(anime)
        entry = await db.watchlist.find_one({"user_id": uid, "anime_id": anime["_id"]})
        status = STATUS_LABELS.get(
            (entry or {}).get("status", "unknown"), STATUS_LABELS["unknown"]
        )
        rating = (entry or {}).get("rating")
        progress = await db.progress.find_one(
            {"user_id": uid, "anime_id": anime["_id"]}
        )
        genres = ", ".join(_genre_values(anime)) or "Not available"
        lines = [
            f"<b><blockquote>🎌 {html.escape(str(anime.get('title', 'Untitled')))}"
            + (f" ({html.escape(str(anime['year']))})" if anime.get("year") else ""),
            "</blockquote></b>",
            f"🎬 ꜰᴏʀᴍᴀᴛ: {html.escape(str(anime.get('format', 'Unknown')))}",
            f"🎭 ɢᴇɴʀᴇs: {html.escape(genres)}",
            f"📌 sᴛᴀᴛᴜs: {status}",
        ]
        if rating is not None:
            lines.append(f"⭐ My rating: {html.escape(str(rating))}/10")
        elif _verified_catalogue_rating(anime) is not None:
            lines.append(f"⭐ IMDb: {_verified_catalogue_rating(anime):g}/10")
        if progress:
            lines.append(
                f"📺 Episodes watched: {progress.get('episodes_completed', 0)}"
            )
        lines.append(
            html.escape(str(anime.get("synopsis") or "Synopsis unavailable.")[:700])
        )
        buttons = [
            [InlineKeyboardButton(
                "★ Remove favorite" if (entry or {}).get("favourite") else "☆ Add to favorites",
                callback_data=f"v1:favorite:{anime['_id']}:{0 if (entry or {}).get('favourite') else 1}",
            )],
            [InlineKeyboardButton("⭐ Set my rating", callback_data=f"v1:rating:{anime['_id']}")],
            [InlineKeyboardButton("📺 Where to watch", callback_data=f"v1:where:{anime['_id']}")],
            [
                InlineKeyboardButton(
                    "🔄 Change status", callback_data=f"v1:status:{anime['_id']}"
                )
            ]
        ]
        if _format_kind(anime.get("format")) == "series":
            buttons.insert(
                0,
                [
                    InlineKeyboardButton(
                        "➕ Episode", callback_data=f"v1:progress:{anime['_id']}:1"
                    ),
                    InlineKeyboardButton(
                        "➖ Episode", callback_data=f"v1:progress:{anime['_id']}:-1"
                    ),
                ],
            )
        imdb_id = str(anime.get("imdb_id", ""))
        url = (
            f"https://www.imdb.com/title/{imdb_id}/"
            if imdb_id.startswith("tt")
            else "https://www.imdb.com/find/?q="
            + quote_plus(str(anime.get("title", "")))
        )
        buttons.append([InlineKeyboardButton("🌐 IMDb", url=url)])
        buttons.extend(navigation_row())
        return await render(update, "\n".join(lines), InlineKeyboardMarkup(buttons))
    if kind == "rating":
        anime = await db.anime.find_one({"_id": ObjectId(screen["anime_id"])})
        if not anime:
            return await render(update, "That catalogue title is no longer available.", home_markup())
        entry = await db.watchlist.find_one({"user_id": uid, "anime_id": anime["_id"]})
        current_rating = (entry or {}).get("rating")
        keyboard = [
            [InlineKeyboardButton(f"{'✓ ' if current_rating == rating else ''}{rating}", callback_data=f"v1:setrating:{anime['_id']}:{rating}") for rating in range(1, 6)],
            [InlineKeyboardButton(f"{'✓ ' if current_rating == rating else ''}{rating}", callback_data=f"v1:setrating:{anime['_id']}:{rating}") for rating in range(6, 11)],
            [InlineKeyboardButton("Clear rating", callback_data=f"v1:setrating:{anime['_id']}:0")],
        ]
        keyboard.extend(navigation_row())
        return await render(update, f"Rate <b>{html.escape(anime.get('title', 'Untitled'))}</b> from 1 to 10:", InlineKeyboardMarkup(keyboard))
    if kind == "wtw_offer":
        anime = await db.anime.find_one({"_id": ObjectId(screen["anime_id"])})
        if not anime:
            return await render(
                update,
                "That catalogue title is no longer available.",
                InlineKeyboardMarkup(navigation_row()),
            )
        region = context.application.bot_data["settings"].default_region
        offers = (
            await metadata.watch_offers(anime, region)
            if metadata
            else {"region": region, "offers": [], "available": False}
        )
        title = html.escape(str(anime.get("title", "Untitled")))
        lines = [
            "<b><blockquote>📺 ᴡʜᴇʀᴇ ᴛᴏ ᴡᴀᴛᴄʜ</blockquote></b>",
            f"{title} · Region {html.escape(offers['region'])}",
        ]
        buttons = []
        if offers["available"]:
            lines.append("<b>Verified provider listings</b>")
            lines.extend(
                f"• {html.escape(item['name'])} · {html.escape(item['type'].title())}"
                for item in offers["offers"]
            )
            if offers.get("link"):
                buttons.append(
                    [
                        InlineKeyboardButton(
                            "Open TMDb provider listings", url=offers["link"]
                        )
                    ]
                )
        else:
            lines.append(
                "No verified streaming offers could be retrieved for this title and region."
            )
        query = quote_plus(str(anime.get("title", "")))
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        "Search Netflix",
                        url=f"https://www.netflix.com/search?q={query}",
                    ),
                    InlineKeyboardButton(
                        "Search Crunchyroll",
                        url=f"https://www.crunchyroll.com/search?q={query}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Search Prime Video",
                        url=f"https://www.primevideo.com/search?phrase={query}",
                    )
                ],
            ]
        )
        lines.append(
            "Search buttons open the service’s title search; they do not confirm availability."
        )
        buttons.extend(navigation_row())
        return await render(update, "\n".join(lines), InlineKeyboardMarkup(buttons))
    if kind == "status":
        anime = await db.anime.find_one({"_id": ObjectId(screen["anime_id"])})
        if not anime:
            return await render(
                update,
                "That catalogue title is no longer available.",
                InlineKeyboardMarkup(navigation_row()),
            )
        entry = await db.watchlist.find_one({"user_id": uid, "anime_id": anime["_id"]})
        current = (entry or {}).get("status", "unknown")
        keyboard = [
            [
                InlineKeyboardButton(
                    ("✓ " if current == status else "") + STATUS_LABELS[status],
                    callback_data=f"v1:set:{status}:{anime['_id']}",
                )
            ]
            for status in STATUS_CHOICES
            if status != current
        ]
        keyboard.extend(navigation_row())
        return await render(
            update,
            f"<b><blockquote>{html.escape(anime.get('title', 'Untitled'))}</blockquote></b>\nCurrent status: {STATUS_LABELS.get(current, STATUS_LABELS['unknown'])}\nChoose the new status:",
            InlineKeyboardMarkup(keyboard),
        )
    if kind == "stats":
        counts = {
            s: await db.watchlist.count_documents({"user_id": uid, "status": s})
            for s in ("completed", "watching", "plan", "on_hold", "dropped")
        }
        prog = await db.progress.aggregate(
            [
                {"$match": {"user_id": uid}},
                {"$group": {"_id": None, "episodes": {"$sum": "$episodes_completed"}}},
            ]
        ).to_list(1)
        total = await db.anime.count_documents({})
        text = (
            f"<b><blockquote>📊 ʏᴏᴜʀ sᴛᴀᴛs</blockquote></b>\nCatalogue: {total}\nCompleted: {counts['completed']}\nWatching: {counts['watching']}\n"
            f"Plan to watch: {counts['plan']}\nOn hold: {counts['on_hold']}\nDropped: {counts['dropped']}\nEpisodes watched: {prog[0]['episodes'] if prog else 0}"
        )
        return await render(update, text, InlineKeyboardMarkup(navigation_row()))
    if kind in {"users", "pending_users"}:
        settings = context.application.bot_data["settings"]
        if uid != settings.owner_telegram_id:
            return await render(
                update, "<b><blockquote>Owner only.</blockquote></b>", home_markup()
            )
        if kind == "pending_users":
            rows = (
                await db.access_requests.find({"status": "pending"})
                .sort("at", 1)
                .to_list(5000)
            )
            title = "⏳ Pending access requests"
        else:
            rows = await _registered_users(db)
            authorized_count = await db.authorized_users.count_documents(
                {"blocked": {"$ne": True}}
            )
            title = f"👥 Registered users: {len(rows)} · Authorized: {authorized_count}"
        if not rows:
            return await render(
                update,
                f"<b><blockquote>{html.escape(title)}</blockquote></b>\nNo records found.",
                InlineKeyboardMarkup(navigation_row()),
            )
        page_size = 6
        page = max(0, min(int(screen.get("page", 0)), (len(rows) - 1) // page_size))
        screen["page"] = page
        start = page * page_size
        keyboard = []
        lines = [f"<b><blockquote>{html.escape(title)}</blockquote></b>"]
        for row in rows[start : start + page_size]:
            user_id = int(row["user_id"])
            display_name = " ".join(
                filter(
                    None, [row.get("first_name"), row.get("last_name"), row.get("name")]
                )
            )
            username = row.get("username")
            label = html.escape(display_name or "Name unavailable")
            if username:
                label += " · @" + html.escape(str(username))
            lines.append(f"• <code>{user_id}</code> · {label}")
            if kind == "pending_users":
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "✅ Approve", callback_data=f"v1:request:approve:{user_id}"
                        ),
                        InlineKeyboardButton(
                            "❌ Reject", callback_data=f"v1:request:reject:{user_id}"
                        ),
                    ]
                )
        if len(rows) > page_size:
            pages = []
            if page:
                pages.append(
                    InlineKeyboardButton(
                        "⬅️ Previous", callback_data=f"v1:adminpage:{kind}:{page - 1}"
                    )
                )
            pages.append(
                InlineKeyboardButton(
                    f"{page + 1}/{(len(rows) + page_size - 1) // page_size}",
                    callback_data="v1:adminpage:noop",
                )
            )
            if start + page_size < len(rows):
                pages.append(
                    InlineKeyboardButton(
                        "Next ➡️", callback_data=f"v1:adminpage:{kind}:{page + 1}"
                    )
                )
            keyboard.append(pages)
        keyboard.extend(navigation_row())
        return await render(update, "\n".join(lines), InlineKeyboardMarkup(keyboard))
    return await render(update, "That screen is unavailable.", home_markup())


async def _open(update, context, screen, reset=False):
    _set_current(context, screen, push=not reset, reset=reset)
    await _render_current(update, context)


def welcome_text(first_name):
    name = html.escape(first_name or "darling", quote=True)
    return (
        "<b><blockquote>ʜᴇʟʟᴏ ᴍʏ ᴅᴀʀʟɪɴɢ "
        f"{name} 👋,</blockquote>\n"
        "   ɪ'ᴍ ᴀ ᴘᴇʀꜱᴏɴᴀʟ ᴀɴɪᴍᴇ ᴠᴀᴜʟᴛ ᴏꜰ ᴍʏ "
        '<a href="https://t.me/Nobita_MUI">ʜᴜʙʙʏ</a> , ᴀɴᴅ ꜱᴏʀʀʏ ɪ ᴄᴀɴᴛ ʜᴇʟᴘ ʙᴜᴛ ɪꜰ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ʀᴇᴀʟʟʏ ᴜꜱᴇ ᴛʜᴇɴ ʏᴏᴜ ᴄᴀɴ ʀᴇQᴜᴇꜱᴛ ᴍʏ '
        '<a href="https://t.me/Nobita_MUI">ʜᴜʙʙʏ</a> , ʙʏ ꜱᴇɴᴅɪɴɢ ᴍᴇ /request_access !</b>'
    )


def help_text():
    return (
        "<b><blockquote>❔ ʜᴇʟᴘ &amp; ᴄᴏᴍᴍᴀɴᴅs</blockquote></b>\n"
        "<b>ᴛʀᴀᴄᴋɪɴɢ</b>\n"
        "/start · /help · /about · /request_access\n"
        "/search &lt;title&gt; · /add &lt;title&gt; [| genres]\n"
        "/watchlist · /favorites · /watching · /completed · /plan · /onhold · /dropped\n"
        "/recommendations · /top · /movies · /series\n"
        "/wtw &lt;title&gt; · /progress · /continue · /stats · /export\n\n"
        "<b>ᴏᴡɴᴇʀ ᴏɴʟʏ</b>\n"
        "/users · /pending_users · /allow &lt;id&gt; · /deny &lt;id&gt;\n"
        "/approve &lt;id&gt; · /reject &lt;id&gt; · /block &lt;id&gt; · /unblock &lt;id&gt; · /health"
    )


async def about_text(context):
    try:
        bot = await context.bot.get_me()
        username = bot.username
        bot_name = bot.first_name or "Anime Command Center"
    except TelegramError:
        username = None
        bot_name = "Anime Command Center"
    safe_name = html.escape(str(bot_name), quote=True)
    safe_username = html.escape(str(username or ""), quote=True)
    linked_name = (
        f'<a href="https://t.me/{safe_username}">{safe_name}</a>'
        if username
        else safe_name
    )
    provider = html.escape(
        context.application.bot_data["settings"].deployment_provider, quote=True
    )
    return (
        "<b><blockquote>⍟───[ ᴍʏ ᴅᴇᴛᴀɪʟꜱ ]───⍟</blockquote>\n\n"
        f"‣ ᴍʏ ɴᴀᴍᴇ : {linked_name}\n"
        '‣ ᴍʏ ᴅᴀʀʟɪɴɢ : <a href="tg://settings">ᴍʏ ᴅᴀʀʟɪɴɢ</a>\n'
        '‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href="https://t.me/Nobita_MUI">ʜᴜʙʙʏ</a>\n'
        '‣ ʟɪʙʀᴀʀʏ : <a href="https://docs.python-telegram-bot.org/en/v22.5/">python-telegram-bot 22.5</a>\n'
        '‣ ʟᴀɴɢᴜᴀɢᴇ : <a href="https://www.python.org/download/releases/3.0/">ᴘʏᴛʜᴏɴ 3</a>\n'
        '‣ ᴅᴀᴛᴀ ʙᴀsᴇ : <a href="https://www.mongodb.com/">ᴍᴏɴɢᴏ ᴅʙ</a>\n'
        f"‣ ʙᴏᴛ sᴇʀᴠᴇʀ : {provider}\n"
        "‣ ʙᴜɪʟᴅ sᴛᴀᴛᴜs : ᴠ2.7.1 [sᴛᴀʙʟᴇ]</b>"
    )


async def start(update, context):
    user = update.effective_user
    chat = update.effective_chat
    settings = context.application.bot_data["settings"]
    if not user or not chat:
        return
    if chat.type != "private" and not settings.allow_groups:
        return await update.effective_message.reply_text(
            "Please use me in a private chat."
        )
    db = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    await db.users.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "user_id": user.id,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "username": user.username,
                "last_seen_at": now,
            },
            "$setOnInsert": {"registered_at": now},
        },
        upsert=True,
    )
    existing = await db.users.find_one({"user_id": user.id}) or {}
    welcome = welcome_text(user.first_name)
    old_message_id = existing.get("start_message_id")
    if old_message_id and existing.get("start_chat_id") == chat.id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id,
                message_id=old_message_id,
                text=welcome,
                parse_mode=ParseMode.HTML,
                reply_markup=home_markup(),
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                return
    if not existing.get("start_album_attempted"):
        album_sent = False
        try:
            await context.bot.send_media_group(
                chat_id=chat.id,
                media=[InputMediaPhoto(media=url) for url in START_IMAGES],
            )
            album_sent = True
        except TelegramError as exc:
            log.warning("Start image album was unavailable (%s)", type(exc).__name__)
            # Telegram may reject an album if one URL is unavailable; try each URL
            # independently so one working image can still be displayed.
            for url in START_IMAGES:
                try:
                    await context.bot.send_photo(chat_id=chat.id, photo=url)
                    album_sent = True
                except TelegramError:
                    continue
        await db.users.update_one(
            {"user_id": user.id},
            {"$set": {"start_album_attempted": True, "start_album_sent": album_sent}},
        )
    message = await update.effective_message.reply_text(
        welcome,
        parse_mode=ParseMode.HTML,
        reply_markup=home_markup(),
        disable_web_page_preview=True,
    )
    await db.users.update_one(
        {"user_id": user.id},
        {"$set": {"start_chat_id": chat.id, "start_message_id": message.message_id}},
    )


async def whoami(update, context):
    await update.effective_message.reply_text(
        f"Your Telegram numeric ID: {update.effective_user.id}"
    )


async def help_cmd(update, context):
    await _open(update, context, {"kind": "help"}, reset=True)


async def about_cmd(update, context):
    await _open(update, context, {"kind": "about"}, reset=True)


async def menu(update, context):
    if await allowed(update, context):
        await _open(update, context, {"kind": "home"}, reset=True)


async def show_anime(update, context, anime, edit=False):
    # Kept as a small compatibility wrapper for command handlers.
    await _open(
        update,
        context,
        {"kind": "detail", "anime_id": str(anime["_id"])},
        reset=not edit,
    )


async def search(update, context):
    if not await allowed(update, context):
        return
    query = " ".join(context.args).strip()
    if not query:
        return await update.effective_message.reply_text("Use /search <title>")
    _, anime_svc, _, _ = services(context)
    results = await anime_svc.search(query, limit=100)
    if not results:
        return await update.effective_message.reply_text(
            "No matching catalogue entries. Add one with /add <title>."
        )
    await _open(
        update,
        context,
        {"kind": "titles", "mode": "search", "query": query, "page": 0},
        reset=True,
    )


async def add(update, context):
    if not await allowed(update, context):
        return
    raw_title = " ".join(context.args).strip()
    title, separator, raw_genres = raw_title.partition("|")
    title = title.strip()
    manual_genres = normalize_genres(raw_genres) if separator else []
    if not title:
        return await update.effective_message.reply_text("Use /add <title>")
    db, anime_svc, _, _ = services(context)
    results = await anime_svc.search(title, limit=5)
    exact = next((x for x in results if x.get("title_norm") == normalize(title)), None)
    if exact:
        if manual_genres:
            merged = normalize_genres(genres_for(exact), manual_genres)
            if merged != genres_for(exact):
                await db.anime.update_one(
                    {"_id": exact["_id"]}, {"$set": {"genres": merged}}
                )
        return await show_anime(update, context, exact)
    genres = manual_genres
    await db.anime.update_one(
        {"title_norm": normalize(title)},
        {
            "$setOnInsert": {
                "title": title,
                "title_norm": normalize(title),
                "format": "unknown",
                "genres": genres,
                "metadata_source": "user entry",
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    anime = await db.anime.find_one({"title_norm": normalize(title)})
    if genres:
        merged = normalize_genres(genres_for(anime or {}), genres)
        if merged != genres_for(anime or {}):
            await db.anime.update_one(
                {"_id": anime["_id"]}, {"$set": {"genres": merged}}
            )
    await _open(
        update, context, {"kind": "detail", "anime_id": str(anime["_id"])}, reset=True
    )


async def list_cmd(update, context, status=None):
    if not await allowed(update, context):
        return
    mode = "watchlist" if status is None else "statuslist"
    await _open(
        update,
        context,
        {"kind": "titles", "mode": mode, "status": status, "page": 0},
        reset=True,
    )


async def favorites_cmd(update, context):
    if await allowed(update, context):
        await _open(update, context, {"kind": "titles", "mode": "favorites", "page": 0}, reset=True)


async def recommendations(update, context):
    if not await allowed(update, context):
        return
    await _open(update, context, {"kind": "genres", "mode": "recommend"}, reset=True)


def genre_command(mode):
    async def handler(update, context):
        if await allowed(update, context):
            await _open(update, context, {"kind": "genres", "mode": mode}, reset=True)

    return handler


async def stats(update, context):
    if await allowed(update, context):
        await _open(update, context, {"kind": "stats"}, reset=True)


async def callbacks(update, context):
    query = update.callback_query
    # Answer promptly so Telegram clears its loading indicator before DB work.
    await query.answer()
    data = query.data or ""
    if data == "v1:help":
        return await _open(update, context, {"kind": "help"})
    if data == "v1:about":
        return await _open(update, context, {"kind": "about"})
    if not await allowed(update, context, callback_answered=True):
        return
    if len(data.encode("utf-8")) > 64 or not data.startswith("v1:"):
        return await render(
            update, "This action expired. Open the menu and try again.", home_markup()
        )
    db = context.application.bot_data["db"]
    ui = current_ui(context)
    parts = data.split(":")
    try:
        action = parts[1]
        if action == "home":
            return await _open(update, context, {"kind": "home"}, reset=True)
        if action == "back":
            ui["current"] = ui["back"].pop() if ui["back"] else {"kind": "home"}
            return await _render_current(update, context)
        if (
            action == "genres"
            and len(parts) == 3
            and parts[2] in {"recommend", "top", "movie", "series"}
        ):
            return await _open(update, context, {"kind": "genres", "mode": parts[2]})
        if (
            action == "genre"
            and len(parts) == 3
            and ui["current"].get("kind") == "genres"
        ):
            genres = await screen_genres(db, ui["current"], update.effective_user.id)
            index = int(parts[2])
            if index < 0 or index >= len(genres):
                raise ValueError("Invalid genre selection")
            mode = ui["current"]["mode"]
            return await _open(
                update,
                context,
                {"kind": "titles", "mode": mode, "genre": genres[index], "page": 0},
            )
        if (
            action == "gpage"
            and len(parts) == 3
            and ui["current"].get("kind") == "genres"
        ):
            if parts[2] == "noop":
                return
            screen = dict(ui["current"])
            screen["page"] = max(0, int(parts[2]))
            _set_current(context, screen, push=False)
            return await _render_current(update, context)
        if action in {"watchlist", "statuslist"}:
            if action == "watchlist":
                return await _open(
                    update, context, {"kind": "titles", "mode": "watchlist", "page": 0}
                )
            status = parts[2]
            if status not in STATUS_LABELS:
                raise ValueError("Invalid status")
            return await _open(
                update,
                context,
                {"kind": "titles", "mode": "statuslist", "status": status, "page": 0},
            )
        if action == "favorites":
            return await _open(update, context, {"kind": "titles", "mode": "favorites", "page": 0})
        if action == "anime" and len(parts) == 3 and ObjectId.is_valid(parts[2]):
            if ui["current"].get("kind") != "titles":
                raise ValueError("Title selection is no longer active")
            anime = await db.anime.find_one({"_id": ObjectId(parts[2])})
            if not anime:
                raise ValueError("Title no longer exists")
            if ui["current"].get("mode") == "wtw":
                return await _open(
                    update,
                    context,
                    {"kind": "wtw_offer", "anime_id": str(anime["_id"])},
                )
            return await _open(
                update, context, {"kind": "detail", "anime_id": str(anime["_id"])}
            )
        if action == "status" and len(parts) == 3 and ObjectId.is_valid(parts[2]):
            if (
                ui["current"].get("kind") != "detail"
                or ui["current"].get("anime_id") != parts[2]
            ):
                raise ValueError("Status action is no longer active")
            return await _open(
                update, context, {"kind": "status", "anime_id": parts[2]}
            )
        if action in {"rating", "where"} and len(parts) == 3 and ObjectId.is_valid(parts[2]):
            aid = parts[2]
            if ui["current"].get("kind") != "detail" or ui["current"].get("anime_id") != aid:
                raise ValueError("Title action is no longer active")
            if action == "rating":
                return await _open(update, context, {"kind": "rating", "anime_id": aid})
            return await _open(update, context, {"kind": "wtw_offer", "anime_id": aid})
        if action == "favorite" and len(parts) == 4 and ObjectId.is_valid(parts[2]):
            aid = parts[2]
            if ui["current"].get("kind") != "detail" or ui["current"].get("anime_id") != aid or parts[3] not in {"0", "1"}:
                raise ValueError("Favorite action is no longer active")
            await WatchlistService(db).set_favourite(update.effective_user.id, aid, parts[3] == "1")
            return await _render_current(update, context)
        if action == "setrating" and len(parts) == 4 and ObjectId.is_valid(parts[2]):
            aid = parts[2]
            if ui["current"].get("kind") != "rating" or ui["current"].get("anime_id") != aid:
                raise ValueError("Rating action is no longer active")
            value = int(parts[3])
            if value not in range(0, 11):
                raise ValueError("Rating must be between 1 and 10")
            await WatchlistService(db).set_rating(update.effective_user.id, aid, value or None)
            ui["current"] = ui["back"].pop() if ui["back"] else {"kind": "detail", "anime_id": aid}
            return await _render_current(update, context)
        if action == "progress" and len(parts) == 4 and ObjectId.is_valid(parts[2]):
            aid = parts[2]
            increment = int(parts[3])
            if (
                increment not in {-1, 1}
                or ui["current"].get("kind") != "detail"
                or ui["current"].get("anime_id") != aid
            ):
                raise ValueError("Progress action is no longer active")
            anime = await db.anime.find_one({"_id": ObjectId(aid)})
            if not anime or _format_kind(anime.get("format")) != "series":
                raise ValueError("Episode progress is only available for series")
            await WatchlistService(db).progress(
                update.effective_user.id, aid, increment, anime.get("episodes")
            )
            return await _render_current(update, context)
        if action == "set" and len(parts) == 4 and ObjectId.is_valid(parts[3]):
            status, aid = parts[2], parts[3]
            if (
                status not in STATUS_CHOICES
                or ui["current"].get("kind") != "status"
                or ui["current"].get("anime_id") != aid
            ):
                raise ValueError("Status action is no longer active")
            await WatchlistService(db).set_status(update.effective_user.id, aid, status)
            ui["current"] = (
                ui["back"].pop() if ui["back"] else {"kind": "detail", "anime_id": aid}
            )
            return await _render_current(update, context)
        if (
            action == "page"
            and len(parts) == 3
            and ui["current"].get("kind") == "titles"
        ):
            if parts[2] == "noop":
                return
            screen = dict(ui["current"])
            screen["page"] = max(0, int(parts[2]))
            _set_current(context, screen, push=False)
            return await _render_current(update, context)
        if action == "stats":
            return await _open(update, context, {"kind": "stats"})
        if action in {"users", "pending_users"}:
            if not await allowed(update, context, owner=True, callback_answered=True):
                return
            return await _open(update, context, {"kind": action, "page": 0})
        if action == "adminpage" and len(parts) == 4:
            if not await allowed(update, context, owner=True, callback_answered=True):
                return
            if parts[2] == "noop":
                return
            kind = parts[2]
            if kind not in {"users", "pending_users"}:
                raise ValueError("Invalid admin screen")
            _set_current(
                context, {"kind": kind, "page": max(0, int(parts[3]))}, push=False
            )
            return await _render_current(update, context)
        if action == "request" and len(parts) == 4:
            if not await allowed(update, context, owner=True, callback_answered=True):
                return
            choice, target_id = parts[2], int(parts[3])
            if (
                choice not in {"approve", "reject"}
                or ui["current"].get("kind") != "pending_users"
            ):
                raise ValueError("Request action is no longer active")
            await _apply_request(db, target_id, choice, update.effective_user.id)
            return await _render_current(update, context)
        if action == "search":
            return await render(
                update,
                "Use /search &lt;title&gt; to find a catalogue entry.",
                InlineKeyboardMarkup(navigation_row(back=False)),
            )
        raise ValueError("Unknown action")
    except Exception as exc:
        log.warning("Callback failed: %s", exc)
        await render(
            update,
            "That action expired or could not be completed. Reopen the menu and try again.",
            home_markup(),
        )


async def request_access(update, context):
    db = context.application.bot_data["db"]
    user = update.effective_user
    await db.access_requests.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "first_name": user.first_name,
                "last_name": user.last_name,
                "username": user.username,
                "name": user.full_name,
            },
            "$setOnInsert": {
                "user_id": user.id,
                "at": datetime.now(timezone.utc),
                "status": "pending",
            },
        },
        upsert=True,
    )
    await update.message.reply_text("Access request recorded. The owner can review it.")


async def _apply_request(db, uid, choice, actor):
    now = datetime.now(timezone.utc)
    if choice == "approve":
        await db.authorized_users.update_one(
            {"user_id": uid},
            {
                "$set": {
                    "user_id": uid,
                    "blocked": False,
                    "authorized_at": now,
                    "authorized_by": actor,
                }
            },
            upsert=True,
        )
        status = "approved"
    else:
        status = "rejected"
    await db.access_requests.update_one(
        {"user_id": uid},
        {"$set": {"status": status, "reviewed_at": now, "reviewed_by": actor}},
    )
    await db.audit.insert_one(
        {"actor_id": actor, "action": status, "target_id": uid, "at": now}
    )


async def users_cmd(update, context):
    if await allowed(update, context, owner=True):
        await _open(update, context, {"kind": "users", "page": 0}, reset=True)


async def pending_users_cmd(update, context):
    if await allowed(update, context, owner=True):
        await _open(update, context, {"kind": "pending_users", "page": 0}, reset=True)


async def wtw_cmd(update, context):
    if not await allowed(update, context):
        return
    title = " ".join(context.args).strip()
    if not title:
        return await update.effective_message.reply_text("Use /wtw <title>")
    anime_svc = services(context)[1]

    async def operation():
        rows = _dedupe_titles(await anime_svc.search(title, limit=100))
        if not rows:
            _set_current(context, {"kind": "wtw_external", "title": title}, reset=True)
        elif len(rows) == 1:
            _set_current(
                context,
                {"kind": "wtw_offer", "anime_id": str(rows[0]["_id"])},
                reset=True,
            )
        else:
            _set_current(
                context,
                {"kind": "titles", "mode": "wtw", "query": title, "page": 0},
                reset=True,
            )
        return await _render_screen(update, context)

    await run_with_loading(update, context, operation, "Searching provider listings…")


async def admin(update, context):
    if not await allowed(update, context, owner=True):
        return
    cmd = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
    db = context.application.bot_data["db"]
    args = context.args
    owner = context.effective_user.id
    now = datetime.now(timezone.utc)
    if cmd == "users":
        users = await db.authorized_users.find({"blocked": {"$ne": True}}).to_list(100)
        text = "Authorized users: " + (
            ", ".join(str(x["user_id"]) for x in users) or "none"
        )
    elif cmd == "pending_users":
        users = await db.access_requests.find({"status": "pending"}).to_list(100)
        text = "Pending: " + (", ".join(str(x["user_id"]) for x in users) or "none")
    elif cmd in {"allow", "approve", "deny", "reject", "block", "unblock"}:
        if not args or not args[0].isdigit():
            return await update.message.reply_text(f"Usage: /{cmd} <telegram_user_id>")
        uid = int(args[0])
        if uid == owner and cmd in {"deny", "reject", "block"}:
            return await update.message.reply_text("Owner access cannot be removed.")
        if cmd in {"allow", "approve", "unblock"}:
            await db.authorized_users.update_one(
                {"user_id": uid},
                {
                    "$set": {
                        "user_id": uid,
                        "blocked": False,
                        "authorized_at": now,
                        "authorized_by": owner,
                    }
                },
                upsert=True,
            )
            await db.access_requests.update_one(
                {"user_id": uid}, {"$set": {"status": "approved"}}
            )
        else:
            await db.authorized_users.update_one(
                {"user_id": uid},
                {"$set": {"blocked": True, "blocked_at": now, "blocked_by": owner}},
                upsert=True,
            )
            await db.access_requests.update_one(
                {"user_id": uid}, {"$set": {"status": "rejected"}}
            )
        await db.audit.insert_one(
            {"actor_id": owner, "action": cmd, "target_id": uid, "at": now}
        )
        text = f"{cmd.title()}d user {uid}."
    elif cmd == "health":
        try:
            await db.client.admin.command("ping")
            mongo = "connected"
        except Exception:
            mongo = "unavailable"
        text = f"Bot process: running\nMongoDB: {mongo}\nTMDb configured: {bool(context.application.bot_data['settings'].tmdb_api_key.get_secret_value())}\nStreaming availability: provider not configured"
    else:
        text = "Use /whoami for your numeric ID."
    await update.message.reply_text(text)


async def export_cmd(update, context):
    if not await allowed(update, context):
        return
    import json

    db = context.application.bot_data["db"]

    async def make_export():
        rows = await db.watchlist.find({"user_id": update.effective_user.id}).to_list(
            5000
        )
        payload = []
        for row in rows:
            anime = await db.anime.find_one({"_id": row["anime_id"]})
            payload.append(
                {
                    "title": (anime or {}).get("title"),
                    "status": row.get("status"),
                    "rating": row.get("rating"),
                    "favourite": row.get("favourite"),
                    "notes": row.get("notes"),
                    "review": row.get("review"),
                }
            )
        return json.dumps(payload, ensure_ascii=False, indent=2).encode()

    try:
        content = await run_with_loading(
            update, context, make_export, "Preparing your export…"
        )
    except Exception as exc:
        log.warning("Export failed (%s)", type(exc).__name__)
        await render(update, "⚠️ Could not prepare your export. Please try again.")
        return
    placeholder = LOADING_MESSAGES.pop(_message_key(update), None)
    if placeholder:
        try:
            await placeholder.delete()
        except TelegramError:
            pass
    from io import BytesIO

    doc = BytesIO(content)
    doc.name = "anime_watchlist.json"
    await update.message.reply_document(doc, caption="Your personal watchlist export")


async def import_cmd(update, context):
    if await allowed(update, context):
        await update.message.reply_text(
            "Use scripts/import_workbook.py <path.xlsx> --dry-run to preview, then rerun without --dry-run to import. Existing watch statuses are never overwritten."
        )


async def post_init(app):
    settings = app.bot_data["settings"]
    mongo = Mongo(settings)
    await mongo.start()
    app.bot_data["mongo"] = mongo
    app.bot_data["db"] = mongo.db
    settings = app.bot_data["settings"]
    app.bot_data["metadata"] = MetadataService(
        mongo.db, settings.tmdb_api_key.get_secret_value()
    )
    await mongo.db.authorized_users.update_one(
        {"user_id": settings.owner_telegram_id},
        {
            "$setOnInsert": {
                "user_id": settings.owner_telegram_id,
                "owner": True,
                "blocked": False,
            }
        },
        upsert=True,
    )
    await seed_confirmed_titles(mongo.db, settings.owner_telegram_id)


async def post_shutdown(app):
    if app.bot_data.get("metadata"):
        await app.bot_data["metadata"].close()
    if app.bot_data.get("mongo"):
        await app.bot_data["mongo"].close()


async def _settings_cmd(update, context):
    if await allowed(update, context):
        await update.message.reply_text(
            "Default region: " + context.application.bot_data["settings"].default_region
        )


async def _streaming_cmd(update, context):
    if await allowed(update, context):
        await update.message.reply_text(
            "Live availability requires authorized provider access. Availability is not inferred."
        )


async def _reminders_cmd(update, context):
    if await allowed(update, context):
        await update.message.reply_text("Reminders are disabled until configured.")


async def _cancel_cmd(update, context):
    if await allowed(update, context):
        await update.message.reply_text("No active workflow.")


def build_app():
    settings = get_settings()
    settings.validate_runtime()
    app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings
    commands = {
        "start": start,
        "help": help_cmd,
        "about": about_cmd,
        "menu": menu,
        "whoami": whoami,
        "search": search,
        "add": add,
        "watchlist": list_cmd,
        "watching": lambda u, c: list_cmd(u, c, "watching"),
        "completed": lambda u, c: list_cmd(u, c, "completed"),
        "plan": lambda u, c: list_cmd(u, c, "plan"),
        "onhold": lambda u, c: list_cmd(u, c, "on_hold"),
        "dropped": lambda u, c: list_cmd(u, c, "dropped"),
        "recommend": recommendations,
        "recommendations": recommendations,
        "wtw": wtw_cmd,
        "users": users_cmd,
        "pending_users": pending_users_cmd,
        "stats": stats,
        "movies": genre_command("movie"),
        "series": genre_command("series"),
        "top": genre_command("top"),
        "favorites": favorites_cmd,
        "favourites": favorites_cmd,
        "progress": lambda u, c: list_cmd(u, c, "watching"),
        "continue": lambda u, c: list_cmd(u, c, "watching"),
        "streaming": _streaming_cmd,
        "reminders": _reminders_cmd,
        "settings": _settings_cmd,
        "export": export_cmd,
        "import": import_cmd,
        "cancel": _cancel_cmd,
        "request_access": request_access,
    }
    for name, fn in commands.items():
        app.add_handler(CommandHandler(name, fn))
    for name in (
        "allow",
        "deny",
        "approve",
        "reject",
        "block",
        "unblock",
        "health",
    ):
        app.add_handler(CommandHandler(name, admin))
    app.add_handler(CallbackQueryHandler(callbacks))
    return app


def main():
    build_app().run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=False
    )


if __name__ == "__main__":
    main()

