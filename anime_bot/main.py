import html
import logging
from datetime import datetime, timezone
from urllib.parse import quote_plus

from bson import ObjectId
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
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

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
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
                except BadRequest:
                    pass
        elif update.effective_message:
            await update.effective_message.reply_text(
                "Access denied. Use /request_access to ask the owner."
            )
        return False
    if owner and not is_owner:
        if update.effective_message:
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
            log.debug("Could not edit callback message, sending replacement: %s", exc)
    message = update.effective_message
    if message:
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


def home_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎌 Search", callback_data="v1:search"),
                InlineKeyboardButton("📚 Watchlist", callback_data="v1:watchlist"),
            ],
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
    genres = anime.get("genres") or []
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.replace(",", ";").split(";")]
    return [str(g).strip() for g in genres if str(g).strip()]


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


async def screen_genres(db, screen, user_id):
    mode = screen["mode"]
    catalogue = await _catalogue(db)
    if mode in {"movie", "series"}:
        catalogue = [a for a in catalogue if _format_kind(a.get("format")) == mode]
    if mode == "recommend":
        watched = await _watch_map(db, user_id)
        catalogue = [a for a in catalogue if str(a["_id"]) not in watched]
    genre_to_ids = {}
    for anime in catalogue:
        for genre in _genre_values(anime):
            key = genre.casefold()
            genre_to_ids.setdefault(key, genre)
    return [genre_to_ids[key] for key in sorted(genre_to_ids)]


async def _list_titles(db, user_id, screen):
    mode = screen["mode"]
    genre = screen.get("genre")
    if mode == "watchlist" or mode == "statuslist":
        rows = await WatchlistService(db).list(
            user_id, screen.get("status"), limit=MAX_CATALOGUE
        )
        return rows, "Your watchlist" if mode == "watchlist" else STATUS_LABELS.get(
            screen.get("status"), "Watchlist"
        )
    if mode == "search":
        rows = await AnimeService(db).search(screen.get("query", ""), limit=100)
        return _dedupe_titles(
            rows
        ), f"Search results for <b>{html.escape(screen.get('query', ''))}</b>"
    catalogue = await _catalogue(db)
    if mode in {"movie", "series"}:
        catalogue = [a for a in catalogue if _format_kind(a.get("format")) == mode]
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


async def _render_screen(update, context):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    screen = current_ui(context)["current"]
    kind = screen["kind"]
    if kind == "home":
        return await render(
            update,
            "🎌 <b>Anime Command Center</b>\nYour private anime tracker. Choose an action:",
            home_markup(),
        )
    if kind == "genres":
        genres = await screen_genres(db, screen, uid)
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
            update, f"🎭 <b>{title}</b>", InlineKeyboardMarkup(keyboard)
        )
    if kind == "titles":
        rows, heading = await _list_titles(db, uid, screen)
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
                f"<b>{heading}</b>\n{message}",
                InlineKeyboardMarkup(navigation_row()),
            )
        page = max(0, min(int(screen.get("page", 0)), (len(rows) - 1) // PAGE_SIZE))
        screen["page"] = page
        keyboard = _title_keyboard(rows, page, screen["mode"])
        return await render(
            update,
            f"<b>{heading}</b> · {len(rows)} title(s)\nChoose a title:",
            keyboard,
        )
    if kind == "detail":
        anime = await db.anime.find_one({"_id": ObjectId(screen["anime_id"])})
        if not anime:
            await _set_current(context, {"kind": "home"}, push=False, reset=True)
            return await render(
                update, "That catalogue title is no longer available.", home_markup()
            )
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
            f"🎌 <b>{html.escape(str(anime.get('title', 'Untitled')))}</b>"
            + (f" ({html.escape(str(anime['year']))})" if anime.get("year") else ""),
            f"🎬 Format: {html.escape(str(anime.get('format', 'Unknown')))}",
            f"🎭 Genres: {html.escape(genres)}",
            f"📌 Status: {status}",
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
            f"<b>{html.escape(anime.get('title', 'Untitled'))}</b>\nCurrent status: {STATUS_LABELS.get(current, STATUS_LABELS['unknown'])}\nChoose the new status:",
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
            f"📊 <b>Your stats</b>\nCatalogue: {total}\nCompleted: {counts['completed']}\nWatching: {counts['watching']}\n"
            f"Plan to watch: {counts['plan']}\nOn hold: {counts['on_hold']}\nDropped: {counts['dropped']}\nEpisodes watched: {prog[0]['episodes'] if prog else 0}"
        )
        return await render(update, text, InlineKeyboardMarkup(navigation_row()))
    return await render(update, "That screen is unavailable.", home_markup())


async def _open(update, context, screen, reset=False):
    _set_current(context, screen, push=not reset, reset=reset)
    await _render_screen(update, context)


async def start(update, context):
    if await allowed(update, context):
        await _open(update, context, {"kind": "home"}, reset=True)


async def whoami(update, context):
    await update.effective_message.reply_text(
        f"Your Telegram numeric ID: {update.effective_user.id}"
    )


async def help_cmd(update, context):
    if not await allowed(update, context):
        return
    await update.effective_message.reply_text(
        "Commands: /start /menu /search <title> /add <title> /watchlist /watching /completed /plan /onhold /dropped /random /recommend /recommendations /movies /series /progress /stats /top /favourites /continue /streaming <title> /settings /export /import <file.xlsx> /cancel /whoami\nOwner: /allow /deny /users /pending_users /approve /reject /block /unblock /health"
    )


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
    title = " ".join(context.args).strip()
    if not title:
        return await update.effective_message.reply_text("Use /add <title>")
    db, anime_svc, _, _ = services(context)
    results = await anime_svc.search(title, limit=5)
    exact = next((x for x in results if x.get("title_norm") == normalize(title)), None)
    if exact:
        return await show_anime(update, context, exact)
    await db.anime.update_one(
        {"title_norm": normalize(title)},
        {
            "$setOnInsert": {
                "title": title,
                "title_norm": normalize(title),
                "format": "unknown",
                "genres": [],
                "metadata_source": "user entry",
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    anime = await db.anime.find_one({"title_norm": normalize(title)})
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
    if not await allowed(update, context, callback_answered=True):
        return
    data = query.data or ""
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
            return await _render_screen(update, context)
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
            return await _render_screen(update, context)
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
        if action == "anime" and len(parts) == 3 and ObjectId.is_valid(parts[2]):
            if ui["current"].get("kind") != "titles":
                raise ValueError("Title selection is no longer active")
            anime = await db.anime.find_one({"_id": ObjectId(parts[2])})
            if not anime:
                raise ValueError("Title no longer exists")
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
            return await _render_screen(update, context)
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
            return await _render_screen(update, context)
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
            return await _render_screen(update, context)
        if action == "stats":
            return await _open(update, context, {"kind": "stats"})
        if action == "search":
            return await render(
                update,
                "Use /search &lt;title&gt; to find a catalogue entry.",
                InlineKeyboardMarkup(navigation_row(back=False)),
            )
        raise ValueError("Unknown action")
    except Exception as exc:
        log.info("Callback rejected (%s)", type(exc).__name__)
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
            "$setOnInsert": {
                "user_id": user.id,
                "name": user.full_name,
                "at": datetime.now(timezone.utc),
                "status": "pending",
            }
        },
        upsert=True,
    )
    await update.message.reply_text("Access request recorded. The owner can review it.")


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

    rows = (
        await context.application.bot_data["db"]
        .watchlist.find({"user_id": update.effective_user.id})
        .to_list(5000)
    )
    payload = []
    for row in rows:
        anime = await context.application.bot_data["db"].anime.find_one(
            {"_id": row["anime_id"]}
        )
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
    from io import BytesIO

    doc = BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode())
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
        "stats": stats,
        "movies": genre_command("movie"),
        "series": genre_command("series"),
        "top": genre_command("top"),
        "favourites": lambda u, c: list_cmd(u, c, None),
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
        "users",
        "pending_users",
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
