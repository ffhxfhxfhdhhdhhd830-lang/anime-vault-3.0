"""Restore an archive into a separate database for safe recovery verification."""

import argparse
import subprocess
from pathlib import Path

from anime_bot.config import get_settings


def main():
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--target-database",
        default=f"{settings.mongodb_database}_restore",
        help="Destination database; defaults to a separate *_restore database.",
    )
    args = parser.parse_args()
    settings.validate_runtime()
    if not args.archive.is_file():
        parser.error(f"Archive not found: {args.archive}")
    if args.target_database == settings.mongodb_database:
        parser.error("Restores into the live database are blocked. Choose a separate target database.")
    command = [
        "mongorestore",
        "--uri", settings.mongodb_uri.get_secret_value(),
        "--archive", str(args.archive),
        "--gzip",
        "--nsInclude", f"{settings.mongodb_database}.*",
        "--nsFrom", f"{settings.mongodb_database}.*",
        "--nsTo", f"{args.target_database}.*",
    ]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError:
        parser.error("mongorestore was not found. Install MongoDB Database Tools first.")
    except subprocess.CalledProcessError as exc:
        parser.error(f"mongorestore failed with exit code {exc.returncode}.")
    print(f"Archive restored into separate database: {args.target_database}")


if __name__ == "__main__":
    main()
