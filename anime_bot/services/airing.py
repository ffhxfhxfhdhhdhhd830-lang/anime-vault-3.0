"""Automatic AniList airing notifications for titles in a user's Watching list.

Schedules are estimates supplied by AniList. Notifications are deduplicated per
user/title/episode; progress changes only after the user taps Mark watched.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from bson import ObjectId

from anime_bot.services.core import normalize

log = logging.getLogger(__name__)
ANILIST_URL = "https://graphql.anilist.co"
SEARCH_QUERY = """
query ($search: String!) {
  Media(search: $search, type: ANIME, isAdult: false) {
    id
    title { english romaji }
    airingSchedule(notYetAired: false, page: 1, perPage: 1) {
      nodes { episode airingAt }
    }
  }
}
"""


class AiringService:
    def __init__(self, db, client=None):
        self.db = db
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=4.0),
            headers={"User-Agent": "Anime-Vault/3.0"},
        )
        self._owns_client = client is None
        self._cache = {}

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def schedule_for(self, title):
        key = normalize(title)
        cached = self._cache.get(key)
        now = datetime.now(timezone.utc)
        if cached and cached[0] > now:
            return cached[1]
        try:
            response = await self.client.post(
                ANILIST_URL, json={"query": SEARCH_QUERY, "variables": {"search": title}}
            )
            response.raise_for_status()
            media = (response.json().get("data") or {}).get("Media") or {}
            candidates = [media.get("title", {}).get(k) for k in ("english", "romaji")]
            if not any(normalize(value or "") == key for value in candidates):
                return None
            nodes = (media.get("airingSchedule") or {}).get("nodes") or []
            schedule = {**nodes[0], "anilist_id": media["id"]} if nodes else None
            self._cache[key] = (now + timedelta(minutes=30), schedule)
            return schedule
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.info("AniList airing lookup unavailable for %s: %s", title, type(exc).__name__)
            return None

    async def upcoming(self, user_id, days=7, limit=15):
        rows = await self.db.watchlist.find(
            {"user_id": user_id, "status": "watching"}
        ).to_list(length=250)
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=max(1, min(days, 14)))
        results = []
        for row in rows:
            anime = await self.db.anime.find_one({"_id": row["anime_id"]})
            if not anime or not anime.get("title"):
                continue
            schedule = await self.schedule_for(anime["title"])
            if not schedule:
                continue
            release = datetime.fromtimestamp(int(schedule["airingAt"]), tz=timezone.utc)
            if now - timedelta(hours=6) <= release <= end:
                results.append({
                    "anime_id": str(row["anime_id"]),
                    "title": anime["title"],
                    "episode": int(schedule["episode"]),
                    "release_at": release,
                })
        return sorted(results, key=lambda item: item["release_at"])[:limit]

    async def claim_notification(self, user_id, item):
        """Atomic dedupe: a given user's episode is announced only once."""
        key = {
            "user_id": user_id,
            "anime_id": ObjectId(item["anime_id"]),
            "episode": item["episode"],
        }
        try:
            await self.db.airing_notifications.insert_one({
                **key, "title": item["title"], "release_at": item["release_at"],
                "created_at": datetime.now(timezone.utc),
            })
            return True
        except Exception as exc:
            if type(exc).__name__ == "DuplicateKeyError":
                return False
            raise

    async def due_notifications(self):
        users = await self.db.watchlist.distinct("user_id", {"status": "watching"})
        for user_id in users:
            try:
                for item in await self.upcoming(user_id, days=1, limit=50):
                    if item["release_at"] > datetime.now(timezone.utc) + timedelta(minutes=10):
                        continue
                    if await self.claim_notification(user_id, item):
                        yield user_id, item
            except Exception:
                log.exception("Airing scan failed for user %s", user_id)
