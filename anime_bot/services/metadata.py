"""Optional, source-labelled TMDb metadata and watch-provider adapter."""

from difflib import SequenceMatcher
from typing import Any

import httpx

from anime_bot.services.genres import genres_for, normalize_genres
from anime_bot.services.core import normalize

TMDB_BASE = "https://api.themoviedb.org/3"


def _media_types(anime: dict[str, Any]) -> tuple[str, ...]:
    fmt = normalize(str(anime.get("format", "")))
    if fmt in {"movie", "movies", "film", "feature film", "anime movie"}:
        return ("movie",)
    if fmt in {"series", "tv", "tv series", "tv anime", "anime series", "television"}:
        return ("tv",)
    return ("movie", "tv")


def _score(query: str, *titles: str) -> float:
    wanted = normalize(query)
    if not wanted:
        return 0.0
    best = 0.0
    wanted_tokens = set(wanted.split())
    for title in titles:
        candidate = normalize(title)
        if not candidate:
            continue
        if candidate == wanted:
            return 1.0
        token_overlap = len(wanted_tokens & set(candidate.split())) / max(
            1, len(wanted_tokens | set(candidate.split()))
        )
        best = max(
            best, SequenceMatcher(None, wanted, candidate).ratio(), token_overlap
        )
    return best


class MetadataService:
    def __init__(self, db, api_key: str, client: httpx.AsyncClient | None = None):
        self.db = db
        self.api_key = api_key.strip()
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0)
        )
        self._owns_client = client is None

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, path: str, **params):
        if not self.api_key:
            return None
        try:
            response = await self.client.get(
                f"{TMDB_BASE}{path}", params={"api_key": self.api_key, **params}
            )
            if response.status_code in (401, 403, 404, 429):
                return None
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def _resolve(self, anime: dict[str, Any]):
        media_type = anime.get("tmdb_media_type")
        tmdb_id = anime.get("tmdb_id")
        if tmdb_id and media_type in {"movie", "tv"}:
            return media_type, int(tmdb_id)
        title = str(anime.get("title") or "").strip()
        if not title or not self.api_key:
            return None
        candidates = []
        for kind in _media_types(anime):
            body = await self._get(
                f"/search/{kind}", query=title, include_adult="false"
            )
            for result in (body or {}).get("results", []):
                name = result.get("title" if kind == "movie" else "name", "")
                original = result.get(
                    "original_title" if kind == "movie" else "original_name", ""
                )
                score = _score(title, name, original)
                candidates.append((score, kind, result))
        candidates.sort(key=lambda item: (-item[0], item[1], int(item[2].get("id", 0))))
        if not candidates or candidates[0][0] < 0.82:
            return None
        # Do not select between two near-equal candidates automatically.
        if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.025:
            top_ids = {
                (item[1], item[2].get("id"))
                for item in candidates
                if candidates[0][0] - item[0] < 0.025
            }
            if len(top_ids) > 1:
                return None
        kind, result = candidates[0][1], candidates[0][2]
        return kind, int(result["id"])

    async def enrich_genres(self, anime: dict[str, Any]) -> list[str]:
        existing = genres_for(anime)
        cached = normalize_genres(anime.get("tmdb_genres"))
        if cached:
            return normalize_genres(existing, cached)
        if not self.api_key:
            return existing
        resolved = await self._resolve(anime)
        if not resolved:
            return existing
        kind, tmdb_id = resolved
        details = await self._get(f"/{kind}/{tmdb_id}", language="en-US")
        fetched = normalize_genres((details or {}).get("genres"))
        if not fetched:
            return existing
        merged = normalize_genres(existing, fetched)
        if anime.get("_id") is not None:
            await self.db.anime.update_one(
                {"_id": anime["_id"]},
                {
                    "$set": {
                        "genres": merged,
                        "tmdb_id": tmdb_id,
                        "tmdb_media_type": kind,
                        "tmdb_genres": fetched,
                    }
                },
            )
        anime.update(
            {
                "genres": merged,
                "tmdb_id": tmdb_id,
                "tmdb_media_type": kind,
                "tmdb_genres": fetched,
            }
        )
        return merged

    async def enrich_many(self, anime_rows: list[dict[str, Any]], limit: int = 20):
        if not self.api_key:
            return
        import asyncio

        semaphore = asyncio.Semaphore(4)

        async def one(anime):
            if genres_for(anime) or anime.get("tmdb_genres"):
                return
            async with semaphore:
                await self.enrich_genres(anime)

        await asyncio.gather(
            *(one(a) for a in anime_rows[:limit]), return_exceptions=True
        )

    async def watch_offers(self, anime: dict[str, Any], region: str):
        resolved = await self._resolve(anime)
        if not resolved:
            return {"region": region.upper(), "offers": [], "available": False}
        kind, tmdb_id = resolved
        if anime.get("_id") is not None:
            await self.db.anime.update_one(
                {"_id": anime["_id"]},
                {"$set": {"tmdb_id": tmdb_id, "tmdb_media_type": kind}},
            )
        body = await self._get(f"/{kind}/{tmdb_id}/watch/providers")
        region_data = (body or {}).get("results", {}).get(region.upper(), {})
        offers = []
        for access_type in ("flatrate", "free", "ads", "rent", "buy"):
            for provider in region_data.get(access_type, []):
                offers.append(
                    {
                        "name": str(
                            provider.get("provider_name") or "Unknown provider"
                        ),
                        "type": access_type,
                    }
                )
        return {
            "region": region.upper(),
            "offers": offers,
            "link": region_data.get("link"),
            "available": bool(offers),
        }

