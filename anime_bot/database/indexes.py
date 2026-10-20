"""Safe, additive MongoDB index initialization command."""

import asyncio
from anime_bot.config import get_settings
from anime_bot.database.mongo import Mongo


async def main():
    settings = get_settings()
    mongo = Mongo(settings)
    try:
        await mongo.start()
        print(f"Indexes initialized for database {settings.mongodb_database}.")
    finally:
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
