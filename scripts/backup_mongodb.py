"""Create a compressed MongoDB archive without exposing credentials in output."""

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from anime_bot.config import get_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("backups"))
    args = parser.parse_args()
    settings = get_settings()
    settings.validate_runtime()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = args.output_dir / f"{settings.mongodb_database}-{stamp}.archive.gz"
    command = [
        "mongodump",
        "--uri", settings.mongodb_uri.get_secret_value(),
        "--db", settings.mongodb_database,
        "--archive", str(target),
        "--gzip",
    ]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError:
        parser.error("mongodump was not found. Install MongoDB Database Tools first.")
    except subprocess.CalledProcessError as exc:
        parser.error(f"mongodump failed with exit code {exc.returncode}.")
    print(f"Backup created: {target}")


if __name__ == "__main__":
    main()
