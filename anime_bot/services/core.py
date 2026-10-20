import re
from datetime import datetime, timezone
from typing import Any
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from anime_bot.services.genres import genres_for

STATUSES = (
    "plan",
    "watching",
    "completed",
    "on_hold",
    "dropped",
    "rewatching",
    "not_interested",
    "unknown",
)
FORMATS = ("movie", "series", "OVA", "ONA", "special", "short", "franchise")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


class AnimeService:
    def __init__(self, db: Any):
        self.db = db

    async def search(self, query: str, limit: int = 8) -> list[dict]:
        q = normalize(query)
        if not q:
            return []
        # Exact normalized matches and Mongo text matching; no fuzzy auto-merges.
        docs = (
            await self.db.anime.find(
                {
                    "$or": [
                        {"title_norm": {"$regex": re.escape(q), "$options": "i"}},
                        {"aliases_norm": {"$regex": re.escape(q), "$options": "i"}},
                        {"title": {"$regex": re.escape(query), "$options": "i"}},
                    ]
                }
            )
            .limit(limit)
            .to_list(limit)
        )
        return docs

    async def get(self, anime_id: str) -> dict | None:
        if not ObjectId.is_valid(anime_id):
            return None
        return await self.db.anime.find_one({"_id": ObjectId(anime_id)})


class WatchlistService:
    def __init__(self, db: Any):
        self.db = db

    async def set_status(self, user_id: int, anime_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError("Unsupported watch status")
        anime = await self.db.anime.find_one({"_id": ObjectId(anime_id)}, {"_id": 1})
        if not anime:
            raise ValueError("Anime not found")
        now = datetime.now(timezone.utc)
        patch: dict[str, Any] = {"status": status, "updated_at": now}
        if status == "completed":
            patch["completed_at"] = now
        key = {"user_id": user_id, "anime_id": ObjectId(anime_id)}
        current = await self.db.watchlist.find_one(key)
        if current and current.get("status") == status:
            return False
        if current:
            # The conditional update makes repeated Telegram callback delivery idempotent.
            result = await self.db.watchlist.update_one(
                {**key, "status": {"$ne": status}}, {"$set": patch}
            )
            if not result.modified_count:
                return False
        else:
            try:
                result = await self.db.watchlist.update_one(
                    {**key, "status": {"$ne": status}},
                    {
                        "$set": patch,
                        "$setOnInsert": {
                            "user_id": user_id,
                            "anime_id": ObjectId(anime_id),
                            "added_at": now,
                            "rating": None,
                            "favourite": False,
                            "priority": 0,
                            "notes": "",
                            "review": "",
                            "rewatch_count": 0,
                        },
                    },
                    upsert=True,
                )
                if not result.modified_count and result.upserted_id is None:
                    return False
            except DuplicateKeyError:
                # A concurrent first update won the unique (user_id, anime_id) race.
                result = await self.db.watchlist.update_one(
                    {**key, "status": {"$ne": status}}, {"$set": patch}
                )
                if not result.modified_count:
                    return False
        await self.db.activity.insert_one(
            {
                "user_id": user_id,
                "anime_id": ObjectId(anime_id),
                "kind": "status",
                "value": status,
                "at": now,
            }
        )
        return True

    async def list(
        self, user_id: int, status: str | None = None, limit: int = 25
    ) -> list[dict]:
        match: dict[str, Any] = {"user_id": user_id}
        if status:
            match["status"] = status
        rows = (
            await self.db.watchlist.find(match)
            .sort("updated_at", -1)
            .limit(limit)
            .to_list(limit)
        )
        out = []
        for row in rows:
            anime = await self.db.anime.find_one({"_id": row["anime_id"]})
            if anime:
                out.append({**anime, "watch": row})
        return out

    async def progress(
        self, user_id: int, anime_id: str, increment: int, total: int | None = None
    ) -> int:
        if increment == 0:
            raise ValueError("Progress increment cannot be zero")
        aid = ObjectId(anime_id)
        anime = await self.db.anime.find_one({"_id": aid})
        series_formats = {
            "series",
            "tv",
            "tv series",
            "tv anime",
            "anime series",
            "television",
        }
        if not anime or normalize(str(anime.get("format", ""))) not in series_formats:
            raise ValueError("Episode progress is only available for series")
        row = await self.db.progress.find_one(
            {"user_id": user_id, "anime_id": aid}
        ) or {"episodes_completed": 0}
        new = max(0, row["episodes_completed"] + increment)
        cap = total or anime.get("episodes")
        if cap and new > cap:
            raise ValueError(f"Episode count cannot exceed {cap}")
        await self.db.progress.update_one(
            {"user_id": user_id, "anime_id": aid},
            {
                "$set": {
                    "episodes_completed": new,
                    "last_watched_episode": new or None,
                    "updated_at": datetime.now(timezone.utc),
                    "progress_percent": round(100 * new / cap, 1) if cap else None,
                },
                "$setOnInsert": {"user_id": user_id, "anime_id": aid},
            },
            upsert=True,
        )
        await self.db.activity.insert_one(
            {
                "user_id": user_id,
                "anime_id": aid,
                "kind": "progress",
                "value": new,
                "at": datetime.now(timezone.utc),
            }
        )
        return new


class RecommendationService:
    def __init__(self, db: Any):
        self.db = db

    async def recommend(
        self, user_id: int, limit: int = 5, genre: str | None = None
    ) -> list[tuple[dict, str]]:
        user_rows = await self.db.watchlist.find({"user_id": user_id}).to_list(2000)
        excluded = {str(x["anime_id"]) for x in user_rows}
        liked_genres: set[str] = set()
        for row in user_rows:
            if (
                row.get("status") == "completed"
                or row.get("favourite")
                or (row.get("rating") or 0) >= 8
            ):
                anime = await self.db.anime.find_one({"_id": row["anime_id"]})
                liked_genres.update(g.casefold() for g in genres_for(anime or {}))
        candidates = await self.db.anime.find({}).limit(5000).to_list(5000)
        scored = []
        for anime in candidates:
            if str(anime["_id"]) in excluded:
                continue
            normalized_genres = {g.casefold() for g in genres_for(anime)}
            if genre and genre.casefold() not in normalized_genres:
                continue
            overlap = liked_genres.intersection(normalized_genres)
            score = len(overlap)
            scored.append((score, anime, bool(overlap)))
        scored.sort(key=lambda x: (-x[0], x[1].get("title", "").casefold()))
        return [
            (
                a,
                "Matches your preferred genres"
                if matched
                else (
                    "Genre match; suggestions are not personalized yet"
                    if not liked_genres
                    else "Selected genre match"
                ),
            )
            for _, a, matched in scored[:limit]
        ]


async def seed_confirmed_titles(db: Any, owner_id: int) -> None:
    watched = [
        "Your Name",
        "I Want to Eat Your Pancreas",
        "Weathering with You",
        "A Silent Voice",
        "The Tunnel to Summer, the Exit of Goodbyes",
        "5 Centimeters per Second",
    ]
    planned = [
        "Josee, the Tiger and the Fish",
        "The Girl Who Leapt Through Time",
        "Words Bubble Up Like Soda Pop",
        "Tamako Love Story",
    ]
    unknown = [
        "HELLO WORLD",
        "To Every You I've Loved Before",
        "To Me, the One Who Loved You",
        "The Anthem of the Heart",
        "The Last: Naruto the Movie",
        "Ride Your Wave",
        "The Stranger by the Shore",
        "Fortune Favors Lady Nikuko",
    ]
    for title, status in (
        [(t, "completed") for t in watched]
        + [(t, "plan") for t in planned]
        + [(t, "unknown") for t in unknown]
    ):
        norm = normalize(title)
        await db.anime.update_one(
            {"title_norm": norm},
            {
                "$setOnInsert": {
                    "title": title,
                    "title_norm": norm,
                    "format": "movie",
                    "genres": [],
                    "metadata_source": "user-provided seed",
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        anime = await db.anime.find_one({"title_norm": norm})
        # Seed does not overwrite any pre-existing personal record.
        await db.watchlist.update_one(
            {"user_id": owner_id, "anime_id": anime["_id"]},
            {
                "$setOnInsert": {
                    "user_id": owner_id,
                    "anime_id": anime["_id"],
                    "status": status,
                    "rating": None,
                    "favourite": False,
                    "priority": 0,
                    "notes": "",
                    "review": "",
                    "added_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

