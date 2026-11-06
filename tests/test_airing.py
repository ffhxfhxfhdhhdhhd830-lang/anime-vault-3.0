from datetime import datetime, timezone

import pytest

from anime_bot.services.airing import AiringService


class FakeCollection:
    def __init__(self):
        self.rows = []
    async def insert_one(self, row):
        key = (row["user_id"], row["anime_id"], row["episode"])
        if any((r["user_id"], r["anime_id"], r["episode"]) == key for r in self.rows):
            from pymongo.errors import DuplicateKeyError
            raise DuplicateKeyError("duplicate")
        self.rows.append(row)


@pytest.mark.asyncio
async def test_notification_claim_is_deduplicated():
    class DB:
        def __init__(self):
            self.airing_notifications = FakeCollection()
    service = AiringService(DB())
    item = {"anime_id": "507f1f77bcf86cd799439011", "episode": 4,
            "title": "Example", "release_at": datetime.now(timezone.utc)}
    assert await service.claim_notification(42, item)
    assert not await service.claim_notification(42, item)


@pytest.mark.asyncio
async def test_schedule_lookup_requires_exact_title_match():
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"Media": {
                "id": 10, "title": {"english": "Different Show", "romaji": "Different Show"},
                "airingSchedule": {"nodes": [{"episode": 2, "airingAt": 1800000000}]}
            }}}
    class Client:
        async def post(self, *args, **kwargs): return Response()
    service = AiringService(object(), client=Client())
    assert await service.schedule_for("My Show") is None
