from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from anime_bot.config import Settings


class Mongo:
    def __init__(self, settings: Settings):
        self.client = AsyncIOMotorClient(
            settings.mongodb_uri.get_secret_value(),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            maxPoolSize=30,
            retryWrites=True,
        )
        self.db: AsyncIOMotorDatabase = self.client[settings.mongodb_database]

    async def start(self) -> None:
        await self.client.admin.command("ping")
        await ensure_indexes(self.db)

    async def close(self) -> None:
        self.client.close()


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.users.create_index("user_id", unique=True)
    await db.anime.create_index([("title_norm", 1), ("year", 1)])
    await db.anime.create_index([("aliases_norm", 1)])
    await db.watchlist.create_index([("user_id", 1), ("anime_id", 1)], unique=True)
    await db.watchlist.create_index([("user_id", 1), ("status", 1), ("priority", -1)])
    await db.watchlist.create_index([("user_id", 1), ("favourite", 1), ("updated_at", -1)])
    await db.progress.create_index([("user_id", 1), ("anime_id", 1)], unique=True)
    await db.authorized_users.create_index("user_id", unique=True)
    await db.access_requests.create_index("user_id", unique=True)
    await db.audit.create_index([("at", -1)])
    await db.streaming.create_index([("anime_id", 1), ("region", 1)])
    await db.activity.create_index([("user_id", 1), ("at", -1)])

