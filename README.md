# Anime Command Center

A private, MongoDB-backed Telegram tracker. The project works without external metadata credentials; provider-backed ratings and streaming offers are shown only when verified data exists. The Excel workbook was not included in the workspace, so no workbook import has been claimed. On first startup, the bot seeds only the 18 titles and statuses explicitly given in the brief. This is idempotent and never replaces an existing watchlist status.

## Windows setup

1. Install Python 3.12 from python.org and MongoDB locally or use MongoDB Atlas. In Atlas, create a database user with read/write access only to the chosen database, enable TLS, and allow your machine's IP.
2. Create a bot with `@BotFather`. Send `/whoami` to this bot after it starts to get your numeric Telegram ID (or obtain it from a trusted ID bot). Do not publish it.
3. Copy `.env.example` to `.env`, then set the token, owner ID and MongoDB URI. Keep `.env` private; it is excluded from Git.
4. From PowerShell in this directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env with a text editor, then initialize indexes:
python -m anime_bot.database.indexes
python -m anime_bot.main
```

The bot also initializes indexes safely at startup. To import the Excel file, first preview it:

```powershell
python scripts/import_workbook.py .\anime_master_watchlist.xlsx --dry-run
python scripts/import_workbook.py .\anime_master_watchlist.xlsx
```

The importer accepts a path, reads title/year/genre/status columns when recognized, and inserts imported statuses only for users without an existing record. It does not merge similar names automatically. Review the dry run before committing.

Run checks with `python -m pytest` and `ruff check .`. Stop locally with Ctrl+C; the bot closes MongoDB cleanly. For backups, use `mongodump --uri "$env:MONGODB_URI" --db $env:MONGODB_DATABASE --out .\backup` and protect the backup as sensitive personal data.

## Metadata and streaming providers

IMDb's official data API access is governed through AWS Data Exchange and requires a valid subscription, dataset/revision/asset and its authorized endpoint/authentication setup. A generic API key is insufficient. This build does not pretend to implement that gated integration. `TMDB_API_KEY` is reserved for a future authorized adapter; IMDb ratings are not inferred from TMDb. Streaming availability needs an authorized provider such as a JustWatch partner API token; no public partner credential is assumed, and this build does not report provider offers without it. The bot can offer IMDb title search links, which are search links rather than evidence of rating or availability.

## Current scope and limitations

Implemented: async Telegram command bot, Mongo persistence/indexes, owner-only authorization administration, generic access requests, private-chat default, catalogue search and manual additions, status callbacks, user watchlists, episode progress service, personal JSON export, explainable genre-based recommendations, statistics, confirmed-title seeding, and Excel dry-run/import command. External metadata enrichment, verified live streaming, persistent reminder scheduling, custom collections, interactive rating/review flows, and complete inline pagination are not implemented yet. Commands requiring these features respond honestly rather than fabricating results.

All handlers that reveal personal or catalogue data check authorization. The owner is always recognized by numeric ID; approved users are stored in `authorized_users`. MongoDB is never dropped or cleared at startup. Keep a least-privilege DB account and encrypted backups.
