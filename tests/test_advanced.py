from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId

from anime_bot.services.advanced import ReminderService, ReviewService


class FakeCollection:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    async def find_one(self, query, *_args):
        self.calls.append(("find_one", query))
        return self.result

    async def update_one(self, query, update, **kwargs):
        self.calls.append(("update_one", query, update, kwargs))
        return SimpleNamespace(modified_count=1, upserted_id=None)

    async def insert_one(self, document):
        self.calls.append(("insert_one", document))
        return SimpleNamespace(inserted_id=ObjectId())


@pytest.mark.asyncio
async def test_review_saves_scores_and_updates_recommendation_rating():
    anime_id = ObjectId()
    anime = FakeCollection({"_id": anime_id})
    reviews = FakeCollection()
    watchlist = FakeCollection()
    activity = FakeCollection()
    db = SimpleNamespace(anime=anime, reviews=reviews, watchlist=watchlist, activity=activity)

    result = await ReviewService(db).save(
        123, str(anime_id),
        {"story": 9, "animation": 10, "music": 8, "overall": 9},
        "Great atmosphere.",
    )

    assert result["overall"] == 9
    watch_update = next(call for call in watchlist.calls if call[0] == "update_one")
    assert watch_update[2]["$set"]["rating"] == 9
    assert reviews.calls[0][2]["$set"]["review"] == "Great atmosphere."


@pytest.mark.asyncio
async def test_review_rejects_out_of_range_scores():
    anime_id = ObjectId()
    db = SimpleNamespace(anime=FakeCollection({"_id": anime_id}))
    with pytest.raises(ValueError, match="1 and 10"):
        await ReviewService(db).save(
            123, str(anime_id),
            {"story": 11, "animation": 10, "music": 8, "overall": 9},
        )


@pytest.mark.asyncio
async def test_reminder_requires_future_timezone_aware_datetime():
    db = SimpleNamespace(reminders=FakeCollection())
    service = ReminderService(db)
    with pytest.raises(ValueError, match="future"):
        await service.create(123, datetime.now(timezone.utc) - timedelta(minutes=1), "Watch anime")
    with pytest.raises(ValueError, match="timezone"):
        await service.create(123, datetime.now(), "Watch anime")


@pytest.mark.asyncio
async def test_reminder_creation_persists_user_and_due_time():
    reminders = FakeCollection()
    service = ReminderService(SimpleNamespace(reminders=reminders))
    due = datetime.now(timezone.utc) + timedelta(hours=1)
    reminder_id = await service.create(123, due, "New episode")
    saved = reminders.calls[0][1]
    assert reminder_id
    assert saved["user_id"] == 123
    assert saved["title"] == "New episode"
    assert saved["sent_at"] is None
