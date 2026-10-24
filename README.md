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

`TMDB_API_KEY` optionally enables TMDb genre enrichment and region-specific provider listings for `/wtw`. A title is matched by name and format with a confidence threshold; uncertain matches are left unresolved. Provider search links are clearly labeled and do not verify availability. TMDb provider listings and its returned link are the source of verified offers. `STREAMING_API_KEY` remains reserved because no authorized provider API adapter is configured. IMDb ratings are not inferred from TMDb.

Set `DEFAULT_REGION=IN` (or another TMDb region code) and optionally configure `DEPLOYMENT_PROVIDER` for the About screen. The default About value is "Not configured."

Genre normalization supports legacy fields such as `genre`, `Genres`, and `genre_names`. Run a safe, repeatable backfill after deployment:

```powershell
python scripts/backfill_genres.py --dry-run
python scripts/backfill_genres.py
```

The backfill only adds/normalizes the anime document's `genres` field; it does not touch watchlist or progress records.

## Current scope and limitations

Implemented: async Telegram command bot, Mongo persistence/indexes, owner-only authorization administration, persisted access requests, private-chat default, catalogue search and manual additions, status-aware watchlist updates, personal favorites, 1–10 ratings, multi-select genre filters, AniList-backed discovery with paginated anime buttons, top unwatched anime by genre, genre-filtered watchlist/completed views, optional cached TMDb provider enrichment, `/wtw`, `/users`, `/pending_users`, Help and About screens, start images with a separate menu message, personal JSON export, statistics, confirmed-title seeding, and Excel dry-run/import command. Genre menus include the requested anime categories and AniList catalogue genres. AniList genre enrichment fills missing catalogue genres when a title can be matched. Persistent reminder scheduling, custom collections, and review text entry are not implemented yet. Commands requiring these features respond honestly rather than fabricating results.

Use `/delete <title>` to remove a manually added catalogue title. The bot asks for confirmation and refuses permanent deletion if another user's watchlist, progress, or activity records reference it.

Add or update catalogue details with `/add <title> | <genres> | <format> | <synopsis>`. For example: `/add Frieren | Adventure, Drama, Fantasy | series | An elf mage reflects on a journey after the heroes' victory.` Supported formats are `movie`, `series`, `OVA`, `ONA`, `special`, `short`, and `franchise` (`film` and `tv` are accepted aliases). Genres, format, and synopsis are optional; keep the separators to leave an earlier field blank, such as `/add My Title | | movie | A short synopsis.` You can also keep using `/add <title>` or `/add <title> | <genres>`. If the title already exists, the supplied details update that catalogue entry.

All handlers that reveal personal or catalogue data check authorization. The owner is always recognized by numeric ID; approved users are stored in `authorized_users`. MongoDB is never dropped or cleared at startup. Keep a least-privilege DB account and encrypted backups.

