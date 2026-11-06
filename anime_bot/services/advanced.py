"""Episode history, structured reviews, and persistent reminder services."""

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from anime_bot.services.core import normalize


SERIES_FORMATS = {"series", "tv", "tv series", "tv anime", "anime series", "television"}


class TrackingService:
    def __init__(self, db: Any):
        self.db = db

    async def log_episode(self, user_id: int, anime_id: str, episode: int) -> tuple[bool, int]:
        if not ObjectId.is_valid(anime_id) or isinstance(episode, bool) or episode < 1:
            raise ValueError("Choose a valid series and episode number (1 or higher).")
        aid = ObjectId(anime_id)
        anime = await self.db.anime.find_one({"_id": aid})
        if not anime or normalize(str(anime.get("format", ""))) not in SERIES_FORMATS:
            raise ValueError("Episode tracking is available for series, not movies.")
        total = anime.get("episodes")
        if isinstance(total, int) and total > 0 and episode > total:
            raise ValueError(f"This series has {total} episodes.")
        key = {"user_id": user_id, "anime_id": aid}
        now = datetime.now(timezone.utc)
        result = await self.db.progress.update_one(
            key,
            {
                "$addToSet": {"watched_episodes": episode},
                "$set": {"updated_at": now},
                "$setOnInsert": {"user_id": user_id, "anime_id": aid},
            },
            upsert=True,
        )
        row = await self.db.progress.find_one(key) or {}
        watched = sorted(set(row.get("watched_episodes", [])))
        count = len(watched)
        await self.db.progress.update_one(
            key,
            {"$set": {
                "episodes_completed": count,
                "last_watched_episode": max(watched) if watched else None,
                "progress_percent": round(100 * count / total, 1) if isinstance(total, int) and total > 0 else None,
                "updated_at": now,
            }},
        )
        if result.modified_count:
            await self.db.episode_events.insert_one({
                "user_id": user_id, "anime_id": aid, "episode": episode,
                "action": "watched", "active": True, "at": now,
            })
            await self.db.activity.insert_one({
                "user_id": user_id, "anime_id": aid, "kind": "episode",
                "value": episode, "at": now,
            })
            return True, count
        return False, count

    async def undo_last_episode(self, user_id: int, anime_id: str) -> tuple[int, int] | None:
        if not ObjectId.is_valid(anime_id):
            raise ValueError("Invalid anime ID.")
        aid = ObjectId(anime_id)
        event = await self.db.episode_events.find_one_and_update(
            {"user_id": user_id, "anime_id": aid, "action": "watched", "active": True},
            {"$set": {"active": False, "undone_at": datetime.now(timezone.utc)}},
            sort=[("at", -1)],
            return_document=ReturnDocument.AFTER,
        )
        if not event:
            return None
        episode = int(event["episode"])
        key = {"user_id": user_id, "anime_id": aid}
        await self.db.progress.update_one(key, {"$pull": {"watched_episodes": episode}})
        row = await self.db.progress.find_one(key) or {}
        watched = sorted(set(row.get("watched_episodes", [])))
        count = len(watched)
        anime = await self.db.anime.find_one({"_id": aid}) or {}
        total = anime.get("episodes")
        await self.db.progress.update_one(
            key,
            {"$set": {
                "episodes_completed": count,
                "last_watched_episode": max(watched) if watched else None,
                "progress_percent": round(100 * count / total, 1) if isinstance(total, int) and total > 0 else None,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        await self.db.activity.insert_one({
            "user_id": user_id, "anime_id": aid, "kind": "episode_undo",
            "value": episode, "at": datetime.now(timezone.utc),
        })
        return episode, count


class ReviewService:
    SCORE_FIELDS = ("story", "animation", "music", "overall")

    def __init__(self, db: Any):
        self.db = db

    async def save(self, user_id: int, anime_id: str, scores: dict[str, int], text: str = "") -> dict:
        if not ObjectId.is_valid(anime_id):
            raise ValueError("Invalid anime ID.")
        if set(scores) != set(self.SCORE_FIELDS):
            raise ValueError("Provide story, animation, music, and overall scores.")
        if any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10 for v in scores.values()):
            raise ValueError("Each review score must be a whole number from 1 to 10.")
        text = text.strip()
        if len(text) > 1500:
            raise ValueError("Review text must be 1,500 characters or fewer.")
        aid = ObjectId(anime_id)
        if not await self.db.anime.find_one({"_id": aid}, {"_id": 1}):
            raise ValueError("Anime not found.")
        now = datetime.now(timezone.utc)
        row = {"user_id": user_id, "anime_id": aid, **scores, "review": text, "updated_at": now}
        await self.db.reviews.update_one(
            {"user_id": user_id, "anime_id": aid},
            {"$set": row, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        await self.db.activity.insert_one({
            "user_id": user_id, "anime_id": aid, "kind": "review",
            "value": scores["overall"], "at": now,
        })
        return row


class ReminderService:
    def __init__(self, db: Any):
        self.db = db

    async def create(self, user_id: int, due_at: datetime, title: str, note: str = "") -> str:
        if due_at.tzinfo is None or due_at <= datetime.now(timezone.utc):
            raise ValueError("Reminder time must be in the future and include a timezone.")
        title, note = title.strip(), note.strip()
        if not title or len(title) > 120 or len(note) > 500:
            raise ValueError("Title is required (max 120 characters); note limit is 500.")
        result = await self.db.reminders.insert_one({
            "user_id": user_id, "due_at": due_at.astimezone(timezone.utc),
            "title": title, "note": note, "sent_at": None,
            "cancelled_at": None, "created_at": datetime.now(timezone.utc),
        })
        return str(result.inserted_id)

    async def list(self, user_id: int, limit: int = 20) -> list[dict]:
        return await self.db.reminders.find({
            "user_id": user_id, "sent_at": None, "cancelled_at": None,
        }).sort("due_at", 1).limit(limit).to_list(limit)

    async def cancel(self, user_id: int, reminder_id: str) -> bool:
        if not ObjectId.is_valid(reminder_id):
            return False
        result = await self.db.reminders.update_one(
            {"_id": ObjectId(reminder_id), "user_id": user_id, "sent_at": None, "cancelled_at": None},
            {"$set": {"cancelled_at": datetime.now(timezone.utc)}},
        )
        return bool(result.modified_count)

    async def claim_due(self, now: datetime) -> dict | None:
        return await self.db.reminders.find_one_and_update(
            {"due_at": {"$lte": now}, "sent_at": None, "cancelled_at": None},
            {"$set": {"sent_at": now}},
            sort=[("due_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    async def release_claim(self, reminder_id: ObjectId) -> None:
        await self.db.reminders.update_one(
            {"_id": reminder_id}, {"$set": {"sent_at": None}}
        )
