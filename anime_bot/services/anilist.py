"""Public AniList GraphQL adapter for anime discovery and genre enrichment."""

import html
import json
import time
from difflib import SequenceMatcher
from typing import Any

import httpx

from anime_bot.services.core import normalize
from anime_bot.services.genres import genres_for, normalize_genres

ANILIST_URL = "https://graphql.anilist.co"
MEDIA_FIELDS = """
id
title { romaji english native }
genres
format
averageScore
popularity
siteUrl
startDate { year }
"""


def _titles(row: dict[str, Any]) -> list[str]:
    titles = row.get("title") or {}
    return [str(titles.get(key) or "").strip() for key in ("english", "romaji", "native") if titles.get(key)]


def _title_score(wanted: str, candidates: list[str]) -> float:
    normalized = normalize(wanted)
    if not normalized:
        return 0.0
    return max(
        (1.0 if normalize(candidate) == normalized else SequenceMatcher(None, normalized, normalize(candidate)).ratio())
        for candidate in candidates
    ) if candidates else 0.0


class AniListService:
    """Fetch top-rated anime and fill missing local genre metadata without an API key."""

    def __init__(self, db, client: httpx.AsyncClient | None = None):
        self.db = db
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": "Anime-Vault/2.0"},
        )
        self._owns_client = client is None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_seconds = 1800

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    def _cached(self, key: str):
        item = self._cache.get(key)
        if item and item[0] > time.monotonic():
            return item[1]
        return None

    def _store(self, key: str, value: Any):
        self._cache[key] = (time.monotonic() + self._cache_seconds, value)
        return value

    async def _query(self, query: str) -> dict[str, Any] | None:
        try:
            response = await self.client.post(
                ANILIST_URL,
                json={"query": query},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                return None
            return payload.get("data")
        except (httpx.HTTPError, ValueError):
            return None

    async def genres(self) -> list[str]:
        cached = self._cached("genres")
        if cached is not None:
            return cached
        data = await self._query("{ GenreCollection }")
        return self._store("genres", sorted((data or {}).get("GenreCollection") or [], key=str.casefold))

    async def _ranked_by_genre(self, genre: str, *, series_only: bool, limit: int = 50):
        formats = ", format_in: [TV, OVA, ONA, TV_SHORT]" if series_only else ""
        safe_genre = json.dumps(genre, ensure_ascii=False)
        query = f"""
        {{ Page(page: 1, perPage: {max(1, min(limit, 50))}) {{
          media(type: ANIME, genre: {safe_genre}, sort: [SCORE_DESC, POPULARITY_DESC], isAdult: false{formats}) {{
            {MEDIA_FIELDS}
          }}
        }} }}
        """
        data = await self._query(query)
        return ((data or {}).get("Page") or {}).get("media") or []

    async def top_unwatched_by_genre(self, watched_titles: set[str]) -> list[dict[str, Any]]:
        """Return one highest-scored, not-yet-watched title for every AniList genre."""
        ranked = self._cached("ranked_by_genre")
        if ranked is None:
            genre_names = await self.genres()
            if not genre_names:
                return []
            ranked = []
            # Keep each GraphQL request small enough for AniList's query complexity limits.
            for start in range(0, len(genre_names), 5):
                batch = genre_names[start : start + 5]
                fields = [
                    f'g{i}: Page(page: 1, perPage: 50) {{ media(type: ANIME, genre: {json.dumps(genre, ensure_ascii=False)}, sort: [SCORE_DESC, POPULARITY_DESC], isAdult: false) {{ {MEDIA_FIELDS} }} }}'
                    for i, genre in enumerate(batch)
                ]
                data = await self._query("query { " + " ".join(fields) + " }")
                if data is None:
                    continue
                for i, genre in enumerate(batch):
                    rows = (((data.get(f"g{i}") or {}).get("media")) or [])
                    ranked.append({"_genre": genre, "_ranked": rows})
            self._store("ranked_by_genre", ranked)
        results = []
        for group in ranked:
            pick = next(
                (row for row in group["_ranked"] if not any(normalize(title) in watched_titles for title in _titles(row))),
                None,
            )
            if pick:
                results.append({**pick, "_genre": group["_genre"]})
        return results

    async def series_for_genre(self, genre: str, watched_titles: set[str], limit: int = 25):
        rows = await self._ranked_by_genre(genre, series_only=True, limit=50)
        results = []
        for row in rows:
            if any(normalize(title) in watched_titles for title in _titles(row)):
                continue
            results.append(row)
            if len(results) >= limit:
                break
        return results

    async def enrich_catalogue(self, rows: list[dict[str, Any]]) -> None:
        missing = [row for row in rows if not genres_for(row) and row.get("title")]
        if not missing:
            return
        fields = []
        for i, row in enumerate(missing[:40]):
            fields.append(
                f's{i}: Page(page: 1, perPage: 5) {{ media(type: ANIME, search: {json.dumps(str(row["title"]), ensure_ascii=False)}, sort: [SEARCH_MATCH]) {{ {MEDIA_FIELDS} }} }}'
            )
        data = await self._query("query { " + " ".join(fields) + " }")
        if not data:
            return
        for i, row in enumerate(missing[:40]):
            candidates = (((data.get(f"s{i}") or {}).get("media")) or [])
            wanted = str(row.get("title") or "")
            best = max(candidates, key=lambda item: _title_score(wanted, _titles(item)), default=None)
            if not best or _title_score(wanted, _titles(best)) < 0.82:
                continue
            genres = normalize_genres(best.get("genres"))
            if not genres:
                continue
            row.update({"genres": genres, "anilist_id": best.get("id"), "anilist_url": best.get("siteUrl")})
            if row.get("_id") is not None:
                update: dict[str, Any] = {"genres": genres, "anilist_id": best.get("id"), "anilist_url": best.get("siteUrl")}
                if str(row.get("format", "")).casefold() in {"", "unknown"} and best.get("format"):
                    update["format"] = "series" if best["format"] in {"TV", "TV_SHORT", "OVA", "ONA"} else "movie" if best["format"] == "MOVIE" else best["format"]
                await self.db.anime.update_one({"_id": row["_id"]}, {"$set": update})


async def watched_titles(db, user_id: int) -> set[str]:
    """Collect title aliases for list items already started or completed by this user."""
    rows = await db.watchlist.find({"user_id": user_id}).to_list(5000)
    watched_ids = {
        row["anime_id"]
        for row in rows
        if row.get("status") not in {"plan", "unknown"}
    }
    progress = await db.progress.find({"user_id": user_id, "episodes_completed": {"$gt": 0}}).to_list(5000)
    watched_ids.update(row["anime_id"] for row in progress)
    titles: set[str] = set()
    for anime in await db.anime.find({"_id": {"$in": list(watched_ids)}}).to_list(5000):
        for value in [anime.get("title"), anime.get("aliases"), anime.get("title_aliases")]:
            if isinstance(value, str):
                value = [value]
            if isinstance(value, list):
                titles.update(normalize(str(item)) for item in value if item)
            elif value:
                titles.add(normalize(str(value)))
    return {title for title in titles if title}


def display_title(row: dict[str, Any]) -> str:
    titles = row.get("title") or {}
    return str(titles.get("english") or titles.get("romaji") or titles.get("native") or "Untitled")


def linked_title(row: dict[str, Any]) -> str:
    title = html.escape(display_title(row))
    url = html.escape(str(row.get("siteUrl") or "https://anilist.co"), quote=True)
    return f'<a href="{url}">{title}</a>'
