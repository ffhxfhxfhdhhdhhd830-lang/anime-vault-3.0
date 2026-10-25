from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from telegram import InlineKeyboardMarkup

from anime_bot.main import (
    _catalogue,
    _format_kind,
    _list_titles,
    _render_screen,
    _title_keyboard,
    callbacks,
    current_ui,
    home_markup,
    render,
)
from anime_bot.services.core import RecommendationService, WatchlistService


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_format_kind_normalizes_common_movie_and_series_values():
    assert _format_kind("Feature Film") == "movie"
    assert _format_kind("TV Anime") == "series"
    assert _format_kind("OVA") == "ova"


def test_home_has_no_duplicate_discover_route_and_has_new_browse_routes():
    markup = home_markup()
    callbacks_ = [button.callback_data for button in _buttons(markup)]
    assert "v1:discover" not in callbacks_
    assert "v1:genres:recommend" in callbacks_
    assert "v1:genres:top" in callbacks_
    assert "v1:genres:movie" in callbacks_
    assert "v1:genres:series" in callbacks_


def test_title_buttons_are_paginated_and_callback_data_is_compact():
    anime = [{"_id": ObjectId(), "title": f"Anime {n}"} for n in range(12)]
    markup = _title_keyboard(anime, 0, "movie")
    buttons = _buttons(markup)
    title_buttons = [
        button for button in buttons if button.callback_data.startswith("v1:anime:")
    ]
    assert len(title_buttons) == 8
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert any(button.callback_data == "v1:page:1" for button in buttons)


class _FakeCollection:
    def __init__(self, find_one=None):
        self._find_one = find_one
        self.find_one = AsyncMock(side_effect=find_one)


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def limit(self, _limit):
        return self

    def sort(self, *_args):
        return self

    async def to_list(self, length=None):
        return self.rows[:length] if length else self.rows


class _DataCollection(_FakeCollection):
    def __init__(self, rows, find_one=None):
        super().__init__(find_one)
        self.rows = rows

    def find(self, _query):
        return _Cursor(self.rows)


class _FakeDB:
    def __init__(self, anime=None, watch=None):
        self.anime = _FakeCollection(
            lambda query, *args: anime if query.get("_id") == anime.get("_id") else None
        )
        self.watchlist = _FakeCollection(
            lambda query, *args: watch if query.get("user_id") == 7 else None
        )
        self.progress = _FakeCollection(lambda *args: None)
        self.authorized_users = _FakeCollection(lambda query, *args: {"user_id": 7})


class _CatalogDB:
    def __init__(self, anime, watchlist=()):
        self.anime_rows = anime
        self.watchlist_rows = list(watchlist)
        self.anime = _DataCollection(anime, self._find_anime)
        self.watchlist = _DataCollection(self.watchlist_rows, self._find_watch)
        self.authorized_users = _FakeCollection(lambda query, *args: {"user_id": 7})
        self.progress = _FakeCollection(lambda *args: None)

    def _find_anime(self, query, *_args):
        return next(
            (a for a in self.anime_rows if a.get("_id") == query.get("_id")), None
        )

    def _find_watch(self, query, *_args):
        return next(
            (
                w
                for w in self.watchlist_rows
                if w.get("user_id") == query.get("user_id")
                and w.get("anime_id") == query.get("anime_id")
            ),
            None,
        )


class _Update:
    def __init__(self, query=None):
        self.callback_query = query
        self.effective_user = SimpleNamespace(id=7)
        self.effective_chat = SimpleNamespace(type="private")
        self.effective_message = query.message if query else None


class _Context:
    def __init__(self, db, settings=None):
        self.application = SimpleNamespace(
            bot_data={
                "db": db,
                "settings": settings
                or SimpleNamespace(owner_telegram_id=99, allow_groups=False),
            }
        )
        self.user_data = {}


@pytest.mark.asyncio
async def test_callback_render_edits_existing_message():
    edit = AsyncMock()
    reply = AsyncMock()
    query = SimpleNamespace(
        edit_message_text=edit, message=SimpleNamespace(reply_text=reply)
    )
    update = _Update(query)
    await render(update, "screen", InlineKeyboardMarkup([]))
    edit.assert_awaited_once()
    reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_detail_screen_shows_change_status_instead_of_contradictory_status_buttons():
    aid = ObjectId()
    anime = {"_id": aid, "title": "Example", "format": "movie", "genres": ["Drama"]}
    db = _FakeDB(anime, {"user_id": 7, "anime_id": aid, "status": "completed"})
    edit = AsyncMock()
    query = SimpleNamespace(
        edit_message_text=edit, message=SimpleNamespace(reply_text=AsyncMock())
    )
    update = _Update(query)
    context = _Context(db)
    current_ui(context)["current"] = {"kind": "detail", "anime_id": str(aid)}
    await _render_screen(update, context)
    markup = edit.await_args.kwargs["reply_markup"]
    labels = [button.text for button in _buttons(markup)]
    assert any("Change status" in label for label in labels)
    assert not any("Completed" in label for label in labels)
    assert "Completed" in edit.await_args.args[0]


@pytest.mark.asyncio
async def test_status_screen_excludes_current_status_from_change_options():
    aid = ObjectId()
    anime = {"_id": aid, "title": "Example", "format": "movie", "genres": []}
    db = _FakeDB(anime, {"user_id": 7, "anime_id": aid, "status": "completed"})
    edit = AsyncMock()
    query = SimpleNamespace(
        edit_message_text=edit, message=SimpleNamespace(reply_text=AsyncMock())
    )
    update = _Update(query)
    context = _Context(db)
    current_ui(context)["current"] = {"kind": "status", "anime_id": str(aid)}
    await _render_screen(update, context)
    labels = [
        button.text for button in _buttons(edit.await_args.kwargs["reply_markup"])
    ]
    assert all("Completed" not in label for label in labels)
    assert any("Watching" in label for label in labels)


@pytest.mark.asyncio
async def test_callback_answers_promptly_and_home_edits_the_menu():
    db = _FakeDB()
    edit = AsyncMock()
    query = SimpleNamespace(
        data="v1:home",
        answer=AsyncMock(),
        edit_message_text=edit,
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = _Update(query)
    context = _Context(db)
    await callbacks(update, context)
    query.answer.assert_awaited_once()
    edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_unauthorized_callback_is_rejected_without_a_second_answer():
    db = _FakeDB()
    db.authorized_users.find_one = AsyncMock(return_value=None)
    answer = AsyncMock()
    edit = AsyncMock()
    query = SimpleNamespace(
        data="v1:home",
        answer=answer,
        edit_message_text=edit,
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = _Update(query)
    context = _Context(db)
    await callbacks(update, context)
    assert answer.await_count == 1
    edit.assert_awaited_once()
    assert "Access denied" in edit.await_args.args[0]


@pytest.mark.asyncio
async def test_genre_to_titles_to_details_and_back_uses_same_message():
    aid = ObjectId()
    anime = {
        "_id": aid,
        "title": "A Romance",
        "format": "Feature Film",
        "genres": ["Romance"],
    }
    db = _CatalogDB([anime])
    context = _Context(db)
    context.user_data["anime_ui"] = {
        "current": {"kind": "genres", "mode": "movie"},
        "back": [],
    }
    for payload in ("v1:genre:5", "v1:show", f"v1:anime:{aid}", "v1:back"):
        edit = AsyncMock()
        query = SimpleNamespace(
            data=payload,
            answer=AsyncMock(),
            edit_message_text=edit,
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = _Update(query)
        await callbacks(update, context)
        query.answer.assert_awaited_once()
        edit.assert_awaited_once()
    assert context.user_data["anime_ui"]["current"] == {
        "kind": "titles",
        "mode": "movie",
        "genres": ["Romance"],
        "page": 0,
    }


@pytest.mark.asyncio
async def test_movie_and_series_lists_filter_formats_and_dedupe_titles():
    movie_id, duplicate_id, series_id = ObjectId(), ObjectId(), ObjectId()
    catalogue = [
        {
            "_id": movie_id,
            "title": "Film",
            "year": 2020,
            "format": "Feature Film",
            "genres": ["Drama"],
        },
        {
            "_id": duplicate_id,
            "title": "Film",
            "year": 2020,
            "format": "movie",
            "genres": ["Drama"],
        },
        {"_id": series_id, "title": "Show", "format": "TV Anime", "genres": ["Drama"]},
    ]
    db = _CatalogDB(catalogue)
    movies, _ = await _list_titles(db, 7, {"mode": "movie", "genre": "Drama"})
    series, _ = await _list_titles(db, 7, {"mode": "series", "genre": "Drama"})
    assert [a["title"] for a in movies] == ["Film"]
    assert [a["title"] for a in series] == ["Show"]


@pytest.mark.asyncio
async def test_recommendations_filter_genre_and_do_not_claim_personalization_without_history():
    one, two = ObjectId(), ObjectId()
    db = _CatalogDB(
        [
            {"_id": one, "title": "Romance Pick", "genres": ["Romance"]},
            {"_id": two, "title": "Action Pick", "genres": ["Action"]},
        ]
    )
    values = await RecommendationService(db).recommend(7, limit=10, genre="romance")
    assert [anime["title"] for anime, _ in values] == ["Romance Pick"]
    assert "not personalized" in values[0][1]


@pytest.mark.asyncio
async def test_top_uses_personal_ratings_and_does_not_assign_missing_scores_zero():
    high, unranked = ObjectId(), ObjectId()
    db = _CatalogDB(
        [
            {"_id": high, "title": "Rated", "genres": ["Drama"]},
            {"_id": unranked, "title": "Unrated", "genres": ["Drama"]},
        ],
        [{"user_id": 7, "anime_id": high, "rating": 9}],
    )
    values, heading = await _list_titles(db, 7, {"mode": "top", "genre": "Drama"})
    assert heading.startswith("Your top rated")
    assert [anime["title"] for anime in values] == ["Rated"]
    assert values[0]["_display_rating"] == 9


@pytest.mark.asyncio
async def test_top_ignores_catalogue_ratings_without_explicit_source():
    unverified, verified = ObjectId(), ObjectId()
    db = _CatalogDB(
        [
            {
                "_id": unverified,
                "title": "Unverified",
                "genres": ["Drama"],
                "imdb_rating": 9.9,
                "metadata_source": "user entry",
            },
            {
                "_id": verified,
                "title": "Verified",
                "genres": ["Drama"],
                "imdb_rating": 8.5,
                "metadata_source": "IMDb API",
            },
        ]
    )
    values, heading = await _list_titles(db, 7, {"mode": "top", "genre": "Drama"})
    assert heading.startswith("Verified IMDb")
    assert [anime["title"] for anime in values] == ["Verified"]


@pytest.mark.asyncio
async def test_empty_catalogue_and_invalid_callback_data_are_safe():
    db = _CatalogDB([])
    values = await _catalogue(db)
    assert values == []
    query = SimpleNamespace(
        data="v1:" + "x" * 80,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    await callbacks(_Update(query), _Context(db))
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_change_updates_existing_watchlist_row_once():
    aid = ObjectId()
    row = {"user_id": 7, "anime_id": aid, "status": "watching"}
    anime = {"_id": aid}

    class WatchCollection:
        def __init__(self):
            self.activity_count = 0

        async def find_one(self, _query, *_args):
            return row

        async def update_one(self, query, update, **_kwargs):
            assert query["user_id"] == 7 and query["anime_id"] == aid
            row.update(update["$set"])
            return SimpleNamespace(modified_count=1)

    class ActivityCollection:
        async def insert_one(self, _doc):
            watch.activity_count += 1

    watch = WatchCollection()
    db = SimpleNamespace(
        anime=SimpleNamespace(find_one=AsyncMock(return_value=anime)),
        watchlist=watch,
        activity=ActivityCollection(),
    )
    assert await WatchlistService(db).set_status(7, str(aid), "completed") is True
    assert await WatchlistService(db).set_status(7, str(aid), "completed") is False
    assert row["status"] == "completed"
    assert watch.activity_count == 1

