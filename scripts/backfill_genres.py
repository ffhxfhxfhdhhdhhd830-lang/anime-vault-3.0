"""Idempotently canonicalize legacy anime genre fields; never touches watchlists."""

import argparse
import asyncio

from anime_bot.config import get_settings
from anime_bot.database.mongo import Mongo
from anime_bot.services.genres import genres_for


async def run(dry_run: bool = False):
    settings = get_settings()
    settings.validate_runtime()
    mongo = Mongo(settings)
    changed = 0
    try:
        await mongo.start()
        async for anime in mongo.db.anime.find({}):
            canonical = genres_for(anime)
            if not canonical or anime.get("genres") == canonical:
                continue
            changed += 1
            if not dry_run:
                await mongo.db.anime.update_one(
                    {"_id": anime["_id"]}, {"$set": {"genres": canonical}}
                )
        print(f"Anime records needing genre backfill: {changed}; dry run: {dry_run}.")
    finally:
        await mongo.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()

