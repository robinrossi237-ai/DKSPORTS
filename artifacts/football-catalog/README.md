# DKSPORTS

DKSPORTS is the real runnable application inside this workspace. It is a Flask app backed by PostgreSQL, with optional external football APIs for live data enrichment.

## What The Scan Found

- The real app lives in `artifacts/football-catalog`, not in the TypeScript workspace at the repo root.
- `app.py` is the web entrypoint.
- `db_client.py` is the current source of truth for the PostgreSQL schema and data-access layer.
- `data_importer.py` and `smart_sync.py` populate the database from external APIs.
- The root `lib/db` Drizzle package is still scaffolded and does not contain the DKSPORTS schema yet.

## Current State In This Workspace

- PostgreSQL is wired through `DATABASE_URL` and the schema bootstraps with `python init_db.py`.
- The app boots with `python app.py`.
- A dedicated bulk loader now exists at `full_hydrator.py`.
- The root `lib/db` package is still unrelated to the live DKSPORTS schema.

## Run Path

From `artifacts/football-catalog`:

1. Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and set at least:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
SESSION_SECRET=replace_with_a_real_secret
PORT=5000
```

3. Apply the PostgreSQL schema:

```powershell
python init_db.py
```

4. Start the app:

```powershell
python app.py
```

5. Open `http://localhost:5000`.

If you want DKSPORTS to skip live API calls and immediately serve cached PostgreSQL plus local static assets, set:

```env
OFFLINE_MODE=1
```

You can also toggle that from the Settings page.

## Full Hydration

Use `full_hydrator.py` when you want to backfill the database beyond the lightweight startup sync.

The Settings page now also exposes admin operations for:

- core import by league
- deep hydration by league
- targeted hydration by team
- targeted hydration by cached season
- portable backup and restore

Portable backups are written to:

```text
../dksports-backups/
```

Each archive includes:

- a PostgreSQL custom-format dump of the configured `DATABASE_URL`
- `static/img/` local media
- `static/vendor/` offline Bootstrap/fonts assets
- a small JSON manifest with DB stats and timestamps

Core population only:

```powershell
python -B full_hydrator.py --core-only
```

This imports the reachable football-data.org corpus:

- competitions
- teams
- competition-to-team links
- standings
- scorers
- matches
- squad players

Offline media backfill now happens during squad hydration:

- player assets are written under `static/img/players/_imported/<League>/<Club>/`
- coach assets are written under `static/img/coaches/_imported/<League>/<Club>/`
- when a real portrait provider does not return a usable image, DKSPORTS now creates a local SVG placeholder so the app still works fully offline
- TheSportsDB can be used as a second portrait source; with the free key the app defaults to `THESPORTSDB_API_KEY=123`

Standalone portrait refresh from existing DB squads:

```powershell
python -B sync_sportsdb_media.py --codes PL,PD,SA
```

For the free SportsDB tier, bulk endpoints are truncated. Use a small direct-search budget when you want to progressively fill missing portraits while online:

```powershell
python -B sync_sportsdb_media.py --codes PL --player-search-budget 2 --delay 4.0
```

In practice, the free tier is only giving back a partial bulk slice, so the safest model is:

- bulk sync for the clubs SportsDB exposes
- targeted `--teams` refresh with a small `--player-search-budget` for the club the user is currently viewing

For a single club refresh:

```powershell
python -B sync_sportsdb_media.py --codes PL --teams Arsenal --player-search-budget 3 --delay 4.0
```

Offline vendor assets:

```powershell
python sync_offline_assets.py
```

That mirrors:

- Bootstrap CSS and JS
- Bootstrap Icons CSS plus font files
- Inter and Space Grotesk font files from Google Fonts

Extra RapidAPI archive pass:

```powershell
python sync_extra_rapidapi.py --news-max-pages 4 --delay 0.2
```

Optional extra payload capture:

```powershell
python sync_extra_rapidapi.py --epl-player-ids 173896 --include-laliga2
```

That script:

- archives football-news11 article metadata into `news_articles`
- downloads article images to `static/img/news/`
- stores the provider's extra unused fields in PostgreSQL, including article IDs, aliases, categories, linked leagues, linked teams, and linked countries
- stores raw payloads from the extra ESPN-style RapidAPI endpoints in `external_api_payloads`

Deeper enrichment attempt:

```powershell
python -B full_hydrator.py --include-footapi7
```

That path now resolves and caches best-effort footapi7 provider IDs for teams, matches, and players in PostgreSQL before attempting deeper enrichment. Once a mapping is known, later hydrator runs and lazy-load APIs reuse it instead of guessing again.

That path also tries to fill:

- match events
- match lineups
- match statistics
- match incidents
- match graphs
- match shotmaps
- team transfers
- team form
- player season stats
- news
- top cards
- odds snapshots
- raw odds markets
- H2H records

## Required Vs Optional Env

- Required for PostgreSQL: `DATABASE_URL`
- Required for most live football pages and background sync: `FOOTBALL_DATA_API_KEY`
- Optional app mode:
  - `OFFLINE_MODE=1` forces DB-first / local-asset-first rendering
- Optional enrichments:
  - `API_FOOTBALL_KEY`
  - `ODDS_API_KEY`
  - `RAPIDAPI_FOOTBALL_NEWS_KEY`
  - `RAPIDAPI_FOOTAPI7_KEY`
  - `RAPIDAPI_ESPN_SOCCER_KEY`

Without `FOOTBALL_DATA_API_KEY`, the app can still boot, but the live catalog pages will not have football-data.org content and smart sync will skip.

## Database Notes

- `app.py` already calls `db_client.init_db()` on startup, but it treats failures as non-fatal and only logs them.
- `python init_db.py` is the safest first step because it fails loudly if the database connection or schema bootstrap is broken.
- The schema currently creates 29 tables plus supporting indexes directly from `db_client.py`.
- There is no separate migration framework yet for the Python app; schema evolution is handled inline in `init_db()` with `CREATE TABLE IF NOT EXISTS` and targeted `ALTER TABLE` statements.

## Known Provider Limits

- `EL` and `ECL` are not currently importable from the football-data.org free tier in this project. `EL` returns a free-tier restriction and `ECL` returns not found.
- `footapi7` does not share the same match/team/player IDs as football-data.org. DKSPORTS now includes a persistent mapping layer for those IDs, but actual population still depends on which discovery/data endpoints your current RapidAPI subscription level allows.
- `football-news11`, `apifootball`, and `The Odds API` are wired, but actual data availability still depends on subscription level, endpoint access, and provider coverage.
- The current `football-news11` BASIC plan can hit its daily request quota quickly. DKSPORTS now archives those pages round-robin across competitions so a limited quota produces broader offline league coverage instead of overfilling one league first.
- The provided `english-premiere-league1` RapidAPI host currently exposes the tested `player/bio` route, but not bulk team or player listing routes from the paths we probed, so large-scale player-bio backfill still needs either official endpoint documentation or a source of external player IDs.
- The provided `la-liga2` RapidAPI `GET /matches` path currently returns HTTP `404 Not Found` with the key/path combination tested on May 11, 2026, so DKSPORTS stores a clean error for that feed instead of pretending the dataset is available.
- The current `apifootball` integration does not return reliable bulk player portraits for many teams, so offline player media currently falls back to generated local avatars unless a provider starts returning real portrait URLs.

## Deployment Commands Found In Artifact Metadata

- Development: `python3 app.py`
- Production: `gunicorn -w 2 -b 0.0.0.0:$PORT app:app`

## Recommended Next Step

If you want the TypeScript `lib/db` package to manage the same PostgreSQL schema, the next piece of work is to port the `db_client.py` schema into Drizzle models. Right now, the Python app owns the real schema.
