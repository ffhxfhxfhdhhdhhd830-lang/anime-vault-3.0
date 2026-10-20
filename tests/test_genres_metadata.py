import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from anime_bot.main import LOADING_FRAMES, _animate_loading, welcome_text
from anime_bot.services.genres import genres_for, normalize_genres
from anime_bot.services.metadata import MetadataService


def test_genres_normalize_aliases_provider_objects_and_legacy_fields():
    assert normalize_genres("Science Fiction, slice-of-life", [{"name": "Action"}]) == [
        "Sci-Fi",
        "Slice of Life",
        "Action",
    ]
    assert genres_for(
        {"Genres": "Shonen | Super Powers", "genre_names": ["Drama"]}
    ) == [
        "Drama",
        "Shounen",
        "Super Power",
    ]


class _AnimeCollection:
    def __init__(self):
        self.update_one = AsyncMock()


class _DB:
    def __init__(self):
        self.anime = _AnimeCollection()


@pytest.mark.asyncio
async def test_metadata_enrichment_caches_confident_match_and_preserves_existing_genres():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/search/movie"):
            return httpx.Response(
                200, json={"results": [{"id": 42, "title": "My Anime"}]}
            )
        if request.url.path.endswith("/movie/42"):
            return httpx.Response(
                200,
                json={
                    "genres": [
                        {"id": 1, "name": "Science Fiction"},
                        {"id": 2, "name": "Drama"},
                    ]
                },
            )
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _DB()
    service = MetadataService(db, "test-key", client)
    anime = {
        "_id": "anime-id",
        "title": "My Anime",
        "format": "movie",
        "genres": ["Action"],
    }
    assert await service.enrich_genres(anime) == ["Action", "Sci-Fi", "Drama"]
    assert anime["tmdb_id"] == 42
    db.anime.update_one.assert_awaited_once()
    assert await service.enrich_genres(anime) == ["Action", "Sci-Fi", "Drama"]
    assert calls == ["/3/search/movie", "/3/movie/42"]
    await client.aclose()


@pytest.mark.asyncio
async def test_metadata_does_not_guess_when_credentials_are_missing():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: pytest.fail("unexpected request"))
    )
    db = _DB()
    service = MetadataService(db, "", client)
    anime = {"_id": "anime-id", "title": "Unknown", "genre": "Mystery"}
    assert await service.enrich_genres(anime) == ["Mystery"]
    db.anime.update_one.assert_not_awaited()
    await client.aclose()


@pytest.mark.asyncio
async def test_where_to_watch_can_lookup_external_titles_and_returns_real_provider_names():
    async def handler(request):
        if request.url.path.endswith("/search/movie"):
            return httpx.Response(
                200, json={"results": [{"id": 18, "title": "External Anime"}]}
            )
        if request.url.path.endswith("/search/tv"):
            return httpx.Response(200, json={"results": []})
        if request.url.path.endswith("/movie/18/watch/providers"):
            return httpx.Response(
                200,
                json={
                    "results": {
                        "IN": {
                            "flatrate": [{"provider_name": "Crunchyroll"}],
                            "link": "https://www.themoviedb.org/movie/18",
                        }
                    }
                },
            )
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _DB()
    service = MetadataService(db, "test-key", client)
    result = await service.watch_offers(
        {"title": "External Anime", "format": "unknown"}, "in"
    )
    assert result == {
        "region": "IN",
        "offers": [{"name": "Crunchyroll", "type": "flatrate"}],
        "link": "https://www.themoviedb.org/movie/18",
        "available": True,
    }
    db.anime.update_one.assert_not_awaited()
    await client.aclose()


@pytest.mark.asyncio
async def test_loading_animation_cycles_exact_frames_and_stops_cleanly():
    stop = asyncio.Event()
    frames = []
    target = SimpleNamespace()

    async def edit(text):
        frames.append(text)
        if len(frames) >= 3:
            stop.set()

    target.edit_text = AsyncMock(side_effect=edit)
    await _animate_loading(target, stop, "Working", interval=0.001)
    rendered = [text.splitlines()[0] for text in frames]
    assert LOADING_FRAMES == ("???", "???", "???")
    assert rendered == ["???", "???", "???"]


def test_welcome_escapes_user_html():
    assert "&lt;User&gt;" in welcome_text("<User>")

