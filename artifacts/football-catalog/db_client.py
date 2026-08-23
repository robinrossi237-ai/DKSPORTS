"""PostgreSQL database client for DKSPORTS — replaces the SQLite version.

All public function signatures are unchanged. Uses psycopg2 connection pool.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                url = DATABASE_URL
                if not url:
                    raise RuntimeError("DATABASE_URL not set — cannot connect to PostgreSQL.")
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, url)
    return _pool


def get_connection():
    return _get_pool().getconn()


def _release(conn) -> None:
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


class _Cursor:
    """Context manager that borrows a connection, yields a RealDictCursor, auto-commits."""
    def __init__(self):
        self.conn = None

    def __enter__(self):
        self.conn = get_connection()
        self.cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self.cur

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.cur.close()
        _release(self.conn)
        return False


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_array(value: Any) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except Exception:
        return []
    return loaded if isinstance(loaded, list) else []


def init_db() -> None:
    """Create all tables if they don't exist."""
    with _Cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS competitions (
            id BIGINT PRIMARY KEY,
            code TEXT UNIQUE,
            name TEXT,
            area_name TEXT,
            emblem_url TEXT,
            emblem_local TEXT,
            type TEXT,
            plan TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS teams (
            id BIGINT PRIMARY KEY,
            name TEXT,
            short_name TEXT,
            tla TEXT,
            crest_url TEXT,
            crest_local TEXT,
            area_name TEXT,
            founded INTEGER,
            venue TEXT,
            website TEXT,
            club_colors TEXT,
            coach_name TEXT,
            coach_photo TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS competition_teams (
            competition_code TEXT,
            team_id BIGINT,
            season TEXT,
            PRIMARY KEY (competition_code, team_id, season)
        );

        CREATE TABLE IF NOT EXISTS players (
            id BIGINT PRIMARY KEY,
            name TEXT,
            first_name TEXT,
            last_name TEXT,
            position TEXT,
            date_of_birth TEXT,
            nationality TEXT,
            shirt_number INTEGER,
            current_team_id BIGINT,
            current_team_name TEXT,
            photo_url TEXT,
            photo_local TEXT,
            raw_json JSONB,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS competition_players (
            competition_code TEXT,
            season TEXT,
            team_id BIGINT,
            player_id BIGINT,
            PRIMARY KEY (competition_code, season, team_id, player_id)
        );

        CREATE TABLE IF NOT EXISTS matches (
            id BIGINT PRIMARY KEY,
            competition_code TEXT,
            season TEXT,
            matchday INTEGER,
            stage TEXT,
            status TEXT,
            utc_date TEXT,
            home_team_id BIGINT,
            away_team_id BIGINT,
            home_score INTEGER,
            away_score INTEGER,
            half_time_home INTEGER,
            half_time_away INTEGER,
            extra_time_home INTEGER,
            extra_time_away INTEGER,
            penalty_home INTEGER,
            penalty_away INTEGER,
            venue TEXT,
            raw_json JSONB,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS standings (
            competition_code TEXT,
            season TEXT,
            team_id BIGINT,
            team_name TEXT,
            position INTEGER,
            played_games INTEGER,
            won INTEGER,
            draw INTEGER,
            lost INTEGER,
            goals_for INTEGER,
            goals_against INTEGER,
            goal_difference INTEGER,
            points INTEGER,
            form TEXT,
            raw_json JSONB,
            updated_at TEXT,
            PRIMARY KEY (competition_code, season, team_id)
        );

        CREATE TABLE IF NOT EXISTS scorers (
            id BIGSERIAL PRIMARY KEY,
            competition_code TEXT,
            season TEXT,
            rank_position INTEGER,
            player_id BIGINT,
            player_name TEXT,
            team_id BIGINT,
            team_name TEXT,
            goals INTEGER,
            assists INTEGER,
            penalties INTEGER,
            raw_json JSONB,
            updated_at TEXT,
            UNIQUE (competition_code, season, player_id)
        );

        CREATE TABLE IF NOT EXISTS match_events (
            id BIGSERIAL PRIMARY KEY,
            match_id BIGINT,
            minute INTEGER,
            added_time INTEGER,
            type TEXT,
            team_id BIGINT,
            player_id BIGINT,
            player_name TEXT,
            assist_player_id BIGINT,
            assist_player_name TEXT,
            detail TEXT,
            extra_info TEXT,
            raw_json JSONB,
            UNIQUE (match_id, minute, type, player_id)
        );

        CREATE TABLE IF NOT EXISTS match_lineups (
            id BIGSERIAL PRIMARY KEY,
            match_id BIGINT,
            team_id BIGINT,
            team_name TEXT,
            formation TEXT,
            player_id BIGINT,
            player_name TEXT,
            shirt_number INTEGER,
            position TEXT,
            is_starter BOOLEAN,
            raw_json JSONB,
            UNIQUE (match_id, team_id, player_id)
        );

        CREATE TABLE IF NOT EXISTS match_statistics (
            id BIGSERIAL PRIMARY KEY,
            match_id BIGINT,
            period TEXT,
            category TEXT,
            name TEXT,
            home_value TEXT,
            away_value TEXT,
            render_type TEXT,
            stat_key TEXT,
            UNIQUE (match_id, period, stat_key)
        );

        CREATE TABLE IF NOT EXISTS match_incidents (
            id BIGSERIAL PRIMARY KEY,
            match_id BIGINT,
            incident_type TEXT,
            minute INTEGER,
            added_time INTEGER,
            is_home BOOLEAN,
            player_name TEXT,
            player2_name TEXT,
            description TEXT,
            raw_json JSONB,
            UNIQUE (match_id, incident_type, minute, player_name)
        );

        CREATE TABLE IF NOT EXISTS match_graphs (
            id BIGSERIAL PRIMARY KEY,
            match_id BIGINT UNIQUE,
            graph_data JSONB,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS match_shotmaps (
            id BIGSERIAL PRIMARY KEY,
            match_id BIGINT UNIQUE,
            shotmap_data JSONB,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS news_articles (
            id BIGSERIAL PRIMARY KEY,
            article_id TEXT,
            url TEXT UNIQUE,
            competition_code TEXT,
            league_id INTEGER,
            title TEXT,
            summary TEXT,
            image_url TEXT,
            image_local TEXT,
            source TEXT,
            source_url TEXT,
            published_at TEXT,
            category TEXT,
            alias TEXT,
            lang TEXT,
            categories JSONB,
            teams JSONB,
            leagues JSONB,
            countries JSONB,
            raw_json JSONB,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS player_profiles (
            id BIGSERIAL PRIMARY KEY,
            player_name TEXT,
            team_name TEXT,
            team_id BIGINT,
            photo_url TEXT,
            photo_local TEXT,
            height TEXT,
            weight TEXT,
            age TEXT,
            country TEXT,
            position TEXT,
            market_value TEXT,
            shirt_number TEXT,
            rating TEXT,
            foot TEXT,
            raw_json JSONB,
            updated_at TEXT,
            UNIQUE (player_name, team_name)
        );

        CREATE TABLE IF NOT EXISTS player_season_stats (
            id BIGSERIAL PRIMARY KEY,
            player_id TEXT,
            season_year TEXT,
            team_name TEXT,
            goals INTEGER,
            assists INTEGER,
            appearances INTEGER,
            minutes_played INTEGER,
            rating NUMERIC(5,2),
            yellow_cards INTEGER,
            red_cards INTEGER,
            shots_total INTEGER,
            shots_on_target INTEGER,
            dribbles_success INTEGER,
            pass_accuracy NUMERIC(5,2),
            raw_json JSONB,
            updated_at TEXT,
            UNIQUE (player_id, season_year)
        );

        CREATE TABLE IF NOT EXISTS team_transfers (
            id BIGSERIAL PRIMARY KEY,
            team_id BIGINT,
            player_name TEXT,
            transfer_type TEXT,
            from_team TEXT,
            to_team TEXT,
            transfer_fee TEXT,
            transfer_date TEXT,
            player_image TEXT,
            raw_json JSONB,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS team_form (
            id BIGSERIAL PRIMARY KEY,
            team_id BIGINT,
            match_date TEXT,
            opponent TEXT,
            result TEXT,
            goals_for INTEGER,
            goals_against INTEGER,
            venue TEXT,
            competition TEXT,
            raw_json JSONB,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS provider_id_mappings (
            id BIGSERIAL PRIMARY KEY,
            provider TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            internal_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            entity_name TEXT,
            competition_code TEXT,
            confidence NUMERIC(5,2),
            source TEXT,
            raw_json JSONB,
            updated_at TEXT,
            UNIQUE (provider, entity_type, internal_id)
        );

        CREATE TABLE IF NOT EXISTS external_api_payloads (
            id BIGSERIAL PRIMARY KEY,
            provider TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            internal_player_id BIGINT,
            internal_team_id BIGINT,
            competition_code TEXT,
            entity_name TEXT,
            image_url TEXT,
            image_local TEXT,
            raw_json JSONB,
            updated_at TEXT,
            UNIQUE (provider, resource_type, external_id)
        );

        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id BIGSERIAL PRIMARY KEY,
            match_key TEXT UNIQUE,
            competition_code TEXT,
            home_team TEXT,
            away_team TEXT,
            odds_home NUMERIC(8,3),
            odds_draw NUMERIC(8,3),
            odds_away NUMERIC(8,3),
            bookmaker TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS odds_markets_raw (
            id BIGSERIAL PRIMARY KEY,
            match_key TEXT UNIQUE,
            event_id TEXT,
            sport_key TEXT,
            competition_code TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            raw_json JSONB,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS h2h_records (
            id BIGSERIAL PRIMARY KEY,
            team1 TEXT,
            team2 TEXT,
            match_date TEXT,
            home_team TEXT,
            away_team TEXT,
            score_home INTEGER,
            score_away INTEGER,
            winner TEXT,
            raw_json JSONB,
            UNIQUE (team1, team2, match_date)
        );

        CREATE TABLE IF NOT EXISTS top_cards (
            id BIGSERIAL PRIMARY KEY,
            competition_code TEXT,
            player_name TEXT,
            team_name TEXT,
            yellow_cards INTEGER,
            red_cards INTEGER,
            total_cards INTEGER,
            raw_json JSONB,
            updated_at TEXT,
            UNIQUE (competition_code, player_name)
        );

        CREATE TABLE IF NOT EXISTS images (
            id BIGSERIAL PRIMARY KEY,
            url TEXT UNIQUE,
            local_path TEXT,
            entity_type TEXT,
            entity_id TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS virtual_game_history (
            id BIGSERIAL PRIMARY KEY,
            home_team TEXT,
            away_team TEXT,
            home_score INTEGER,
            away_score INTEGER,
            home_bet NUMERIC(10,2),
            away_bet NUMERIC(10,2),
            winner TEXT,
            payout NUMERIC(10,2),
            house_cut NUMERIC(10,2),
            simulation_data JSONB,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS import_log (
            id BIGSERIAL PRIMARY KEY,
            entity_type TEXT,
            entity_code TEXT,
            records INTEGER,
            status TEXT,
            error_message TEXT,
            imported_at TEXT
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id BIGSERIAL PRIMARY KEY,
            key_name TEXT UNIQUE,
            key_value TEXT,
            endpoint TEXT,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_matches_comp_date ON matches (competition_code, utc_date DESC);
        CREATE INDEX IF NOT EXISTS idx_matches_status ON matches (status);
        CREATE INDEX IF NOT EXISTS idx_players_team ON players (current_team_id);
        CREATE INDEX IF NOT EXISTS idx_comp_players_comp ON competition_players (competition_code, season);
        CREATE INDEX IF NOT EXISTS idx_comp_players_player ON competition_players (player_id);
        CREATE INDEX IF NOT EXISTS idx_standings_comp ON standings (competition_code, season);
        CREATE INDEX IF NOT EXISTS idx_scorers_comp ON scorers (competition_code, season, rank_position);
        CREATE INDEX IF NOT EXISTS idx_news_comp ON news_articles (competition_code, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_import_log ON import_log (entity_type, entity_code, imported_at DESC);
        CREATE INDEX IF NOT EXISTS idx_match_events_match ON match_events (match_id);
        CREATE INDEX IF NOT EXISTS idx_match_stats_match ON match_statistics (match_id, period);
        CREATE INDEX IF NOT EXISTS idx_match_shotmaps_match ON match_shotmaps (match_id);
        CREATE INDEX IF NOT EXISTS idx_odds_markets_comp ON odds_markets_raw (competition_code, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_provider_maps_lookup ON provider_id_mappings (provider, entity_type, competition_code);

        -- Virtual League tables
        CREATE TABLE IF NOT EXISTS virtual_league_sessions (
            id BIGSERIAL PRIMARY KEY,
            session_key TEXT UNIQUE NOT NULL,
            league_code TEXT NOT NULL,
            league_name TEXT,
            season_name TEXT,
            current_matchday INTEGER DEFAULT 1,
            total_matchdays INTEGER DEFAULT 38,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS virtual_league_fixtures (
            id BIGSERIAL PRIMARY KEY,
            session_id BIGINT NOT NULL,
            matchday INTEGER NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            home_scorers JSONB DEFAULT '[]',
            away_scorers JSONB DEFAULT '[]',
            home_stats JSONB DEFAULT '{}',
            away_stats JSONB DEFAULT '{}',
            status TEXT DEFAULT 'scheduled',
            played_at TEXT,
            UNIQUE (session_id, matchday, home_team, away_team)
        );

        CREATE TABLE IF NOT EXISTS virtual_league_scorers (
            id BIGSERIAL PRIMARY KEY,
            session_id BIGINT NOT NULL,
            player_name TEXT NOT NULL,
            team_name TEXT NOT NULL,
            goals INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            UNIQUE (session_id, player_name, team_name)
        );

        CREATE INDEX IF NOT EXISTS idx_vl_sessions_key ON virtual_league_sessions (session_key);
        CREATE INDEX IF NOT EXISTS idx_vl_fixtures_session ON virtual_league_fixtures (session_id, matchday);
        CREATE INDEX IF NOT EXISTS idx_vl_scorers_session ON virtual_league_scorers (session_id, goals DESC);
        """)

    # --- Migrations: add columns to existing tables that pre-date this schema ---
    migrations = [
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS extra_time_home INTEGER",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS extra_time_away INTEGER",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS penalty_home INTEGER",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS penalty_away INTEGER",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS raw_json JSONB",
        "ALTER TABLE standings ADD COLUMN IF NOT EXISTS raw_json JSONB",
        "ALTER TABLE scorers ADD COLUMN IF NOT EXISTS raw_json JSONB",
        "ALTER TABLE h2h_records ADD COLUMN IF NOT EXISTS raw_json JSONB",
        "ALTER TABLE top_cards ADD COLUMN IF NOT EXISTS raw_json JSONB",
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS photo_local TEXT",
        "ALTER TABLE player_profiles ADD COLUMN IF NOT EXISTS photo_local TEXT",
        "ALTER TABLE virtual_league_sessions ADD COLUMN IF NOT EXISTS season_name TEXT",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS article_id TEXT",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS image_local TEXT",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS source_url TEXT",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS alias TEXT",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS lang TEXT",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS categories JSONB",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS teams JSONB",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS leagues JSONB",
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS countries JSONB",
    ]
    with _Cursor() as cur:
        for sql in migrations:
            try:
                cur.execute(sql)
            except Exception:
                pass
    # Unique indexes for team_transfers and team_form upsert support
    with _Cursor() as cur:
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_team_transfers_upsert
                ON team_transfers (team_id, player_name, COALESCE(transfer_date,''), COALESCE(transfer_type,''))
            """)
        except Exception:
            pass
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_team_form_upsert
                ON team_form (team_id, COALESCE(match_date,''), COALESCE(opponent,''))
            """)
        except Exception:
            pass
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_news_article_provider_id
                ON news_articles (article_id)
                WHERE article_id IS NOT NULL
            """)
        except Exception:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---- Teams ----

def upsert_team(team: dict, competition_code: str | None = None, season: str | None = None) -> None:
    coach = team.get("coach") or {}
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO teams (id, name, short_name, tla, crest_url, crest_local,
                area_name, founded, venue, website, club_colors, coach_name, coach_photo, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, short_name=EXCLUDED.short_name, tla=EXCLUDED.tla,
                crest_url=EXCLUDED.crest_url,
                crest_local=COALESCE(EXCLUDED.crest_local, teams.crest_local),
                area_name=EXCLUDED.area_name, founded=EXCLUDED.founded,
                venue=EXCLUDED.venue, website=EXCLUDED.website,
                club_colors=EXCLUDED.club_colors, coach_name=EXCLUDED.coach_name,
                coach_photo=COALESCE(EXCLUDED.coach_photo, teams.coach_photo),
                updated_at=EXCLUDED.updated_at
        """, (
            team.get("id"), team.get("name"), team.get("shortName"), team.get("tla"),
            team.get("crest") or team.get("crest_url"),
            team.get("_crest_local") or team.get("crest_local"),
            (team.get("area") or {}).get("name"), team.get("founded"),
            team.get("venue"), team.get("website"), team.get("clubColors"),
            coach.get("name"), team.get("_coach_photo"), _now(),
        ))
        if competition_code and season and team.get("id"):
            cur.execute("""
                INSERT INTO competition_teams (competition_code, team_id, season)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
            """, (competition_code, team["id"], season))


def get_all_teams() -> list[dict]:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM teams ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


def get_team_by_id(team_id: int) -> dict | None:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM teams WHERE id=%s LIMIT 1", (team_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_team_local_assets(team_id: int) -> dict:
    """Return locally cached asset paths for a team (crest_local, coach_photo, etc.)."""
    with _Cursor() as cur:
        cur.execute("SELECT crest_local, coach_photo FROM teams WHERE id=%s", (team_id,))
        row = cur.fetchone()
        if row:
            return {
                "crest_local": row["crest_local"],
                "coach_photo": row["coach_photo"],
            }
        return {}


def get_teams_by_league() -> dict[str, list[dict]]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT ct.competition_code, t.* FROM teams t
            JOIN competition_teams ct ON ct.team_id=t.id
            ORDER BY ct.competition_code, t.name
        """)
        result: dict[str, list[dict]] = {}
        for row in cur.fetchall():
            r = dict(row)
            code = r.pop("competition_code")
            result.setdefault(code, []).append(r)
        return result


def get_teams_for_competition(competition_code: str, season: str | None = None) -> list[dict]:
    season = season or get_season_for_code(competition_code)
    with _Cursor() as cur:
        if season:
            cur.execute("""
                SELECT t.* FROM teams t
                JOIN competition_teams ct ON ct.team_id=t.id
                WHERE ct.competition_code=%s AND ct.season=%s
                ORDER BY t.name
            """, (competition_code, season))
        else:
            cur.execute("""
                SELECT DISTINCT t.* FROM teams t
                JOIN competition_teams ct ON ct.team_id=t.id
                WHERE ct.competition_code=%s
                ORDER BY t.name
            """, (competition_code,))
        return [dict(r) for r in cur.fetchall()]


def get_competitions_for_team(team_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT DISTINCT c.*
            FROM competitions c
            JOIN competition_teams ct ON ct.competition_code=c.code
            WHERE ct.team_id=%s
            ORDER BY c.name
        """, (team_id,))
        return [dict(r) for r in cur.fetchall()]


# ---- Players ----

def upsert_player(
    player: dict,
    team: dict | None = None,
    competition_code: str | None = None,
    season: str | None = None,
) -> None:
    if not player or not player.get("id"):
        return
    team = team or {}
    name = (player.get("name") or "").strip()
    first_name = player.get("firstName") or (name.split(" ", 1)[0] if name else "")
    last_name = player.get("lastName")
    if not last_name and " " in name:
        last_name = name.split(" ", 1)[1]
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO players (
                id, name, first_name, last_name, position, date_of_birth, nationality,
                shirt_number, current_team_id, current_team_name, photo_url, photo_local, raw_json, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name,
                first_name=COALESCE(EXCLUDED.first_name, players.first_name),
                last_name=COALESCE(EXCLUDED.last_name, players.last_name),
                position=COALESCE(EXCLUDED.position, players.position),
                date_of_birth=COALESCE(EXCLUDED.date_of_birth, players.date_of_birth),
                nationality=COALESCE(EXCLUDED.nationality, players.nationality),
                shirt_number=COALESCE(EXCLUDED.shirt_number, players.shirt_number),
                current_team_id=COALESCE(EXCLUDED.current_team_id, players.current_team_id),
                current_team_name=COALESCE(EXCLUDED.current_team_name, players.current_team_name),
                photo_url=COALESCE(EXCLUDED.photo_url, players.photo_url),
                photo_local=COALESCE(EXCLUDED.photo_local, players.photo_local),
                raw_json=EXCLUDED.raw_json,
                updated_at=EXCLUDED.updated_at
        """, (
            player.get("id"),
            name,
            first_name or None,
            last_name or None,
            player.get("position"),
            player.get("dateOfBirth"),
            player.get("nationality"),
            player.get("shirtNumber"),
            team.get("id"),
            team.get("name") or team.get("shortName"),
            player.get("_photo") or player.get("photo") or player.get("photo_url"),
            player.get("_photo_local") or player.get("photo_local"),
            json.dumps(player),
            _now(),
        ))
        if competition_code and season and team.get("id"):
            cur.execute("""
                INSERT INTO competition_players (competition_code, season, team_id, player_id)
                VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (competition_code, season, team.get("id"), player.get("id")))


def get_all_players(limit: int | None = None) -> list[dict]:
    with _Cursor() as cur:
        if limit is None:
            cur.execute("SELECT * FROM players ORDER BY current_team_name NULLS LAST, name")
        else:
            cur.execute("SELECT * FROM players ORDER BY current_team_name NULLS LAST, name LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_player_by_id(player_id: int) -> dict | None:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM players WHERE id=%s LIMIT 1", (player_id,))
        row = cur.fetchone()
        if not row:
            return None
        out = dict(row)
        out["raw_json"] = _json_object(out.get("raw_json"))
        return out


def get_players_for_team(team_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT *
            FROM players
            WHERE current_team_id=%s
            ORDER BY
                CASE position
                    WHEN 'Goalkeeper' THEN 1
                    WHEN 'Defence' THEN 2
                    WHEN 'Midfield' THEN 3
                    WHEN 'Offence' THEN 4
                    ELSE 5
                END,
                shirt_number NULLS LAST,
                name
        """, (team_id,))
        rows = []
        for row in cur.fetchall():
            out = dict(row)
            out["raw_json"] = _json_object(out.get("raw_json"))
            rows.append(out)
        return rows


def get_players_for_competition(competition_code: str, season: str | None = None) -> list[dict]:
    season = season or get_season_for_code(competition_code)
    with _Cursor() as cur:
        if season:
            cur.execute("""
                SELECT p.*, cp.competition_code, cp.season, cp.team_id
                FROM players p
                JOIN competition_players cp ON cp.player_id=p.id
                WHERE cp.competition_code=%s AND cp.season=%s
                ORDER BY p.current_team_name NULLS LAST, p.name
            """, (competition_code, season))
        else:
            cur.execute("""
                SELECT DISTINCT p.*, cp.competition_code, cp.season, cp.team_id
                FROM players p
                JOIN competition_players cp ON cp.player_id=p.id
                WHERE cp.competition_code=%s
                ORDER BY p.current_team_name NULLS LAST, p.name
            """, (competition_code,))
        return [dict(r) for r in cur.fetchall()]


def search_competitions(query: str, limit: int = 20) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    pattern = f"%{q}%"
    prefix = f"{q}%"
    with _Cursor() as cur:
        cur.execute("""
            SELECT *
            FROM competitions
            WHERE name ILIKE %s
               OR code ILIKE %s
               OR COALESCE(area_name, '') ILIKE %s
            ORDER BY
                CASE
                    WHEN name ILIKE %s THEN 0
                    WHEN code ILIKE %s THEN 1
                    ELSE 2
                END,
                name,
                code
            LIMIT %s
        """, (pattern, pattern, pattern, prefix, prefix, limit))
        return [dict(r) for r in cur.fetchall()]


def search_teams(query: str, limit: int = 30) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    pattern = f"%{q}%"
    prefix = f"{q}%"
    with _Cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (t.id)
                t.*,
                ct.competition_code,
                c.name AS competition_name,
                c.emblem_url AS competition_emblem_url,
                c.emblem_local AS competition_emblem_local
            FROM teams t
            LEFT JOIN competition_teams ct ON ct.team_id=t.id
            LEFT JOIN competitions c ON c.code=ct.competition_code
            WHERE t.name ILIKE %s
               OR COALESCE(t.short_name, '') ILIKE %s
               OR COALESCE(t.tla, '') ILIKE %s
            ORDER BY
                t.id,
                CASE
                    WHEN t.name ILIKE %s THEN 0
                    WHEN COALESCE(t.short_name, '') ILIKE %s THEN 1
                    WHEN COALESCE(t.tla, '') ILIKE %s THEN 2
                    ELSE 3
                END,
                CASE WHEN ct.season ~ '^[0-9]+$' THEN CAST(ct.season AS INTEGER) ELSE 0 END DESC,
                ct.season DESC NULLS LAST,
                c.name NULLS LAST
            LIMIT %s
        """, (pattern, pattern, pattern, prefix, prefix, prefix, limit))
        return [dict(r) for r in cur.fetchall()]


def search_players(query: str, limit: int = 30) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    pattern = f"%{q}%"
    prefix = f"{q}%"
    with _Cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (p.id)
                p.*,
                t.name AS team_name,
                t.short_name AS team_short_name,
                t.crest_url,
                t.crest_local,
                cp.competition_code,
                c.name AS competition_name,
                c.emblem_url AS competition_emblem_url,
                c.emblem_local AS competition_emblem_local
            FROM players p
            LEFT JOIN teams t ON t.id=p.current_team_id
            LEFT JOIN competition_players cp ON cp.player_id=p.id
            LEFT JOIN competitions c ON c.code=cp.competition_code
            WHERE p.name ILIKE %s
               OR COALESCE(p.current_team_name, '') ILIKE %s
               OR COALESCE(p.position, '') ILIKE %s
            ORDER BY
                p.id,
                CASE
                    WHEN p.name ILIKE %s THEN 0
                    WHEN COALESCE(p.current_team_name, '') ILIKE %s THEN 1
                    ELSE 2
                END,
                CASE WHEN cp.season ~ '^[0-9]+$' THEN CAST(cp.season AS INTEGER) ELSE 0 END DESC,
                cp.season DESC NULLS LAST,
                c.name NULLS LAST
            LIMIT %s
        """, (pattern, pattern, pattern, prefix, prefix, limit))
        return [dict(r) for r in cur.fetchall()]


def get_compareable_teams(limit: int | None = None) -> list[dict]:
    query = """
        SELECT
            t.id,
            t.name,
            t.short_name,
            t.tla,
            t.crest_url,
            t.crest_local,
            t.area_name,
            STRING_AGG(DISTINCT COALESCE(c.name, ct.competition_code), ', ' ORDER BY COALESCE(c.name, ct.competition_code)) AS leagues
        FROM teams t
        LEFT JOIN competition_teams ct ON ct.team_id=t.id
        LEFT JOIN competitions c ON c.code=ct.competition_code
        GROUP BY t.id, t.name, t.short_name, t.tla, t.crest_url, t.crest_local, t.area_name
        ORDER BY t.name
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def get_compareable_players(limit: int | None = None) -> list[dict]:
    query = """
        SELECT
            p.id,
            p.name,
            p.position,
            p.nationality,
            p.date_of_birth,
            p.shirt_number,
            p.photo_url,
            p.photo_local,
            p.current_team_id,
            COALESCE(t.name, p.current_team_name) AS team_name,
            COALESCE(t.short_name, p.current_team_name) AS team_short_name,
            t.crest_url,
            t.crest_local,
            STRING_AGG(DISTINCT COALESCE(c.name, cp.competition_code), ', ' ORDER BY COALESCE(c.name, cp.competition_code)) AS leagues,
            MAX(COALESCE(s.goals, 0)) AS goals,
            MAX(COALESCE(s.assists, 0)) AS assists,
            MAX(COALESCE(s.penalties, 0)) AS penalties,
            MAX(COALESCE(NULLIF(s.raw_json->>'playedMatches', '')::INTEGER, 0)) AS played_matches
        FROM players p
        LEFT JOIN teams t ON t.id=p.current_team_id
        LEFT JOIN competition_players cp ON cp.player_id=p.id
        LEFT JOIN competitions c ON c.code=cp.competition_code
        LEFT JOIN scorers s ON s.player_id=p.id
        GROUP BY
            p.id, p.name, p.position, p.nationality, p.date_of_birth, p.shirt_number,
            p.photo_url, p.photo_local, p.current_team_id, p.current_team_name,
            t.name, t.short_name, t.crest_url, t.crest_local
        ORDER BY
            MAX(COALESCE(s.goals, 0)) DESC,
            MAX(COALESCE(s.assists, 0)) DESC,
            p.name
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def get_player_photo_locals(player_ids: list[int | str]) -> dict[int, str]:
    if not player_ids:
        return {}
    ids = [int(pid) for pid in player_ids if str(pid).isdigit()]
    if not ids:
        return {}
    with _Cursor() as cur:
        cur.execute("""
            SELECT id, photo_local FROM players
            WHERE id = ANY(%s) AND photo_local IS NOT NULL
        """, (ids,))
        out: dict[int, str] = {}
        for row in cur.fetchall():
            local_path = row["photo_local"]
            if local_path and os.path.exists(local_path):
                out[int(row["id"])] = local_path
        return out


# ---- Competitions ----

def upsert_competition(comp: dict) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO competitions (id, code, name, area_name, emblem_url, emblem_local, type, plan, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                code=EXCLUDED.code, name=EXCLUDED.name, area_name=EXCLUDED.area_name,
                emblem_url=EXCLUDED.emblem_url,
                emblem_local=COALESCE(EXCLUDED.emblem_local, competitions.emblem_local),
                type=EXCLUDED.type, plan=EXCLUDED.plan, updated_at=EXCLUDED.updated_at
        """, (
            comp.get("id"), comp.get("code"), comp.get("name"),
            (comp.get("area") or {}).get("name"),
            comp.get("emblem"), comp.get("emblem_local"),
            comp.get("type"), comp.get("plan"), _now(),
        ))


def get_all_competitions() -> list[dict]:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM competitions ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


def get_competition_by_code(code: str) -> dict | None:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM competitions WHERE code=%s LIMIT 1", (code,))
        row = cur.fetchone()
        return dict(row) if row else None


# ---- Matches ----

def upsert_match(match: dict, competition_code: str, season: str) -> None:
    score = match.get("score") or {}
    ft = score.get("fullTime") or {}
    ht = score.get("halfTime") or {}
    et = score.get("extraTime") or {}
    pen = score.get("penalties") or {}
    home_team = match.get("homeTeam") or {}
    away_team = match.get("awayTeam") or {}
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO matches (id, competition_code, season, matchday, stage, status, utc_date,
                home_team_id, away_team_id, home_score, away_score,
                half_time_home, half_time_away,
                extra_time_home, extra_time_away, penalty_home, penalty_away,
                venue, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                status=EXCLUDED.status,
                home_score=EXCLUDED.home_score, away_score=EXCLUDED.away_score,
                half_time_home=EXCLUDED.half_time_home, half_time_away=EXCLUDED.half_time_away,
                extra_time_home=EXCLUDED.extra_time_home, extra_time_away=EXCLUDED.extra_time_away,
                penalty_home=EXCLUDED.penalty_home, penalty_away=EXCLUDED.penalty_away,
                raw_json=EXCLUDED.raw_json, updated_at=EXCLUDED.updated_at
        """, (
            match.get("id"), competition_code, season, match.get("matchday"),
            match.get("stage"), match.get("status"), match.get("utcDate"),
            home_team.get("id"), away_team.get("id"),
            ft.get("home"), ft.get("away"), ht.get("home"), ht.get("away"),
            et.get("home"), et.get("away"), pen.get("home"), pen.get("away"),
            match.get("venue"), json.dumps(match), _now(),
        ))


# ---- Standings ----

def upsert_standing(row: dict, competition_code: str, season: str) -> None:
    team = row.get("team") or {}
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO standings (competition_code, season, team_id, team_name, position,
                played_games, won, draw, lost, goals_for, goals_against, goal_difference,
                points, form, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (competition_code, season, team_id) DO UPDATE SET
                position=EXCLUDED.position, played_games=EXCLUDED.played_games,
                won=EXCLUDED.won, draw=EXCLUDED.draw, lost=EXCLUDED.lost,
                goals_for=EXCLUDED.goals_for, goals_against=EXCLUDED.goals_against,
                goal_difference=EXCLUDED.goal_difference, points=EXCLUDED.points,
                form=EXCLUDED.form, raw_json=EXCLUDED.raw_json, updated_at=EXCLUDED.updated_at
        """, (
            competition_code, season, team.get("id"), team.get("name"),
            row.get("position"), row.get("playedGames"), row.get("won"),
            row.get("draw"), row.get("lost"), row.get("goalsFor"),
            row.get("goalsAgainst"), row.get("goalDifference"), row.get("points"),
            row.get("form"), json.dumps(row), _now(),
        ))


def get_standings_simple(competition_code: str) -> list[dict]:
    """Return minimal standings rows for change detection."""
    with _Cursor() as cur:
        cur.execute("""
            SELECT team_id, points, position FROM standings
            WHERE competition_code=%s ORDER BY position
        """, (competition_code,))
        return [dict(r) for r in cur.fetchall()]


def get_team_form_from_matches(competition_code: str, last_n: int = 5) -> dict[int, str]:
    """Compute last-N form string per team from finished matches in the DB.

    Returns a dict mapping team_id -> comma-separated form string, e.g. {64: "W,W,D,L,W"}.
    Results are ordered most-recent first so the last entry in the split is the most recent.
    """
    with _Cursor() as cur:
        cur.execute("""
            SELECT home_team_id, away_team_id, home_score, away_score, utc_date
            FROM matches
            WHERE competition_code = %s AND status = 'FINISHED'
              AND home_score IS NOT NULL AND away_score IS NOT NULL
            ORDER BY utc_date DESC
            LIMIT 500
        """, (competition_code,))
        rows = cur.fetchall()

    # Build per-team result lists (most-recent first already from ORDER BY DESC)
    team_results: dict[int, list[str]] = {}
    for row in rows:
        ht, at, hs, as_ = row["home_team_id"], row["away_team_id"], row["home_score"], row["away_score"]
        if ht is None or at is None:
            continue
        if hs > as_:
            hr, ar = "W", "L"
        elif hs < as_:
            hr, ar = "L", "W"
        else:
            hr = ar = "D"
        team_results.setdefault(ht, [])
        team_results.setdefault(at, [])
        if len(team_results[ht]) < last_n:
            team_results[ht].append(hr)
        if len(team_results[at]) < last_n:
            team_results[at].append(ar)

    # Reverse so that the string reads oldest→newest (matches pill display expectation)
    return {tid: ",".join(reversed(res)) for tid, res in team_results.items()}


# ---- Scorers ----

def upsert_scorer(scorer: dict, competition_code: str, season: str, rank: int) -> None:
    player = scorer.get("player") or {}
    team = scorer.get("team") or {}
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO scorers (competition_code, season, rank_position, player_id, player_name,
                team_id, team_name, goals, assists, penalties, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (competition_code, season, player_id) DO UPDATE SET
                rank_position=EXCLUDED.rank_position, goals=EXCLUDED.goals,
                assists=EXCLUDED.assists, penalties=EXCLUDED.penalties,
                raw_json=EXCLUDED.raw_json, updated_at=EXCLUDED.updated_at
        """, (
            competition_code, season, rank, player.get("id"), player.get("name"),
            team.get("id"), team.get("name"),
            scorer.get("goals"), scorer.get("assists"), scorer.get("penalties"),
            json.dumps(scorer), _now(),
        ))


# ---- Import log helpers ----

def get_last_import(entity_code: str, entity_type: str) -> str | None:
    """Return the ISO timestamp of the last successful import, or None."""
    with _Cursor() as cur:
        cur.execute("""
            SELECT imported_at FROM import_log
            WHERE entity_code=%s AND entity_type=%s AND status='ok'
            ORDER BY imported_at DESC LIMIT 1
        """, (entity_code, entity_type))
        row = cur.fetchone()
        return row["imported_at"] if row else None


def get_season_for_code(competition_code: str) -> str | None:
    with _Cursor() as cur:
        cur.execute("""
            SELECT season
            FROM matches
            WHERE competition_code=%s AND COALESCE(season, '') <> ''
            GROUP BY season
            ORDER BY COUNT(*) DESC,
                CASE WHEN season ~ '^[0-9]+$' THEN CAST(season AS INTEGER) ELSE 0 END DESC,
                season DESC
            LIMIT 1
        """, (competition_code,))
        row = cur.fetchone()
        if row and row.get("season"):
            return row["season"]

    with _Cursor() as cur:
        cur.execute("""
            SELECT season
            FROM competition_teams
            WHERE competition_code=%s AND COALESCE(season, '') <> ''
            GROUP BY season
            ORDER BY COUNT(*) DESC,
                CASE WHEN season ~ '^[0-9]+$' THEN CAST(season AS INTEGER) ELSE 0 END DESC,
                season DESC
            LIMIT 1
        """, (competition_code,))
        row = cur.fetchone()
        return row["season"] if row else None


def get_finished_matches_without_events(competition_code: str, limit: int = 5) -> list[int]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT m.id FROM matches m
            LEFT JOIN match_events e ON e.match_id=m.id
            WHERE m.competition_code=%s AND m.status='FINISHED' AND e.id IS NULL
            ORDER BY m.utc_date DESC LIMIT %s
        """, (competition_code, limit))
        return [r["id"] for r in cur.fetchall()]


def get_finished_matches_without_stats(competition_code: str, limit: int = 5) -> list[int]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT m.id FROM matches m
            LEFT JOIN match_statistics s ON s.match_id=m.id
            WHERE m.competition_code=%s AND m.status='FINISHED' AND s.match_id IS NULL
            ORDER BY m.utc_date DESC LIMIT %s
        """, (competition_code, limit))
        return [r["id"] for r in cur.fetchall()]


def get_matches_for_competition(competition_code: str, status: str | None = None) -> list[dict]:
    with _Cursor() as cur:
        if status:
            cur.execute("""
                SELECT m.*,
                       ht.name AS home_team_name,
                       at.name AS away_team_name
                FROM matches m
                LEFT JOIN teams ht ON ht.id=m.home_team_id
                LEFT JOIN teams at ON at.id=m.away_team_id
                WHERE m.competition_code=%s AND m.status=%s
                ORDER BY m.utc_date
            """, (competition_code, status))
        else:
            cur.execute("""
                SELECT m.*,
                       ht.name AS home_team_name,
                       at.name AS away_team_name
                FROM matches m
                LEFT JOIN teams ht ON ht.id=m.home_team_id
                LEFT JOIN teams at ON at.id=m.away_team_id
                WHERE m.competition_code=%s
                ORDER BY m.utc_date
            """, (competition_code,))
        return [dict(r) for r in cur.fetchall()]


def get_match_by_id(match_id: int) -> dict | None:
    with _Cursor() as cur:
        cur.execute("""
            SELECT m.*,
                   ht.name AS home_team_name,
                   at.name AS away_team_name
            FROM matches m
            LEFT JOIN teams ht ON ht.id=m.home_team_id
            LEFT JOIN teams at ON at.id=m.away_team_id
            WHERE m.id=%s
            LIMIT 1
        """, (match_id,))
        row = cur.fetchone()
        if not row:
            return None
        out = dict(row)
        out["raw_json"] = _json_object(out.get("raw_json"))
        return out


def get_team_matches(team_id: int, status: str | None = None, limit: int | None = None) -> list[dict]:
    query = """
        SELECT m.*,
               ht.name AS home_team_name,
               at.name AS away_team_name
        FROM matches m
        LEFT JOIN teams ht ON ht.id=m.home_team_id
        LEFT JOIN teams at ON at.id=m.away_team_id
        WHERE (m.home_team_id=%s OR m.away_team_id=%s)
    """
    params: list[Any] = [team_id, team_id]
    if status:
        query += " AND m.status=%s"
        params.append(status)
    query += " ORDER BY m.utc_date"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        rows = []
        for row in cur.fetchall():
            out = dict(row)
            out["raw_json"] = _json_object(out.get("raw_json"))
            rows.append(out)
        return rows


def get_scorers_for_sync(competition_code: str, limit: int = 10) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT player_name, team_name, team_id FROM scorers
            WHERE competition_code=%s ORDER BY rank_position LIMIT %s
        """, (competition_code, limit))
        return [dict(r) for r in cur.fetchall()]


def get_standings_for_competition(competition_code: str, season: str | None = None) -> list[dict]:
    season = season or get_season_for_code(competition_code)
    with _Cursor() as cur:
        if season:
            cur.execute("""
                SELECT *
                FROM standings
                WHERE competition_code=%s AND season=%s
                ORDER BY position
            """, (competition_code, season))
        else:
            cur.execute("""
                SELECT *
                FROM standings
                WHERE competition_code=%s
                ORDER BY season DESC NULLS LAST, position
            """, (competition_code,))
        rows = []
        for row in cur.fetchall():
            out = dict(row)
            out["raw_json"] = _json_object(out.get("raw_json"))
            rows.append(out)
        return rows


def get_scorers_for_competition(competition_code: str, season: str | None = None, limit: int | None = 20) -> list[dict]:
    season = season or get_season_for_code(competition_code)
    query = """
        SELECT *
        FROM scorers
        WHERE competition_code=%s
    """
    params: list[Any] = [competition_code]
    if season:
        query += " AND season=%s"
        params.append(season)
    query += " ORDER BY rank_position"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        rows = []
        for row in cur.fetchall():
            out = dict(row)
            out["raw_json"] = _json_object(out.get("raw_json"))
            rows.append(out)
        return rows


def get_scorer_rows_for_player(player_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT *
            FROM scorers
            WHERE player_id=%s
            ORDER BY goals DESC NULLS LAST, assists DESC NULLS LAST, competition_code, season DESC NULLS LAST
        """, (player_id,))
        rows = []
        for row in cur.fetchall():
            out = dict(row)
            out["raw_json"] = _json_object(out.get("raw_json"))
            rows.append(out)
        return rows


# ---- Images ----

def register_image(url: str, local_path: str, entity_type: str, entity_id: str) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO images (url, local_path, entity_type, entity_id, created_at)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (url) DO UPDATE SET local_path=EXCLUDED.local_path
        """, (url, local_path, entity_type, entity_id, _now()))


def get_local_image(url: str) -> str | None:
    with _Cursor() as cur:
        cur.execute("SELECT local_path FROM images WHERE url=%s", (url,))
        row = cur.fetchone()
        if row and os.path.exists(row["local_path"]):
            return row["local_path"]
    return None


# ---- External provider mappings ----

def upsert_provider_mapping(
    provider: str,
    entity_type: str,
    internal_id: int | str,
    external_id: int | str,
    *,
    entity_name: str | None = None,
    competition_code: str | None = None,
    confidence: float | None = None,
    source: str | None = None,
    raw_json: dict | None = None,
) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO provider_id_mappings
                (provider, entity_type, internal_id, external_id, entity_name,
                 competition_code, confidence, source, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (provider, entity_type, internal_id) DO UPDATE SET
                external_id=EXCLUDED.external_id,
                entity_name=COALESCE(EXCLUDED.entity_name, provider_id_mappings.entity_name),
                competition_code=COALESCE(EXCLUDED.competition_code, provider_id_mappings.competition_code),
                confidence=COALESCE(EXCLUDED.confidence, provider_id_mappings.confidence),
                source=COALESCE(EXCLUDED.source, provider_id_mappings.source),
                raw_json=COALESCE(EXCLUDED.raw_json, provider_id_mappings.raw_json),
                updated_at=EXCLUDED.updated_at
        """, (
            provider,
            entity_type,
            str(internal_id),
            str(external_id),
            entity_name,
            competition_code,
            confidence,
            source,
            json.dumps(raw_json) if raw_json is not None else None,
            _now(),
        ))


def get_provider_mapping(provider: str, entity_type: str, internal_id: int | str) -> dict | None:
    with _Cursor() as cur:
        cur.execute("""
            SELECT *
            FROM provider_id_mappings
            WHERE provider=%s AND entity_type=%s AND internal_id=%s
            LIMIT 1
        """, (provider, entity_type, str(internal_id)))
        row = cur.fetchone()
        if not row:
            return None
        out = dict(row)
        out["raw_json"] = _json_object(out.get("raw_json"))
        if out.get("confidence") is not None:
            try:
                out["confidence"] = float(out["confidence"])
            except Exception:
                pass
        return out


def get_provider_mappings(provider: str, entity_type: str | None = None, competition_code: str | None = None) -> list[dict]:
    query = """
        SELECT *
        FROM provider_id_mappings
        WHERE provider=%s
    """
    params: list[Any] = [provider]
    if entity_type:
        query += " AND entity_type=%s"
        params.append(entity_type)
    if competition_code:
        query += " AND competition_code=%s"
        params.append(competition_code)
    query += " ORDER BY entity_type, internal_id"
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        rows = []
        for row in cur.fetchall():
            out = dict(row)
            out["raw_json"] = _json_object(out.get("raw_json"))
            if out.get("confidence") is not None:
                try:
                    out["confidence"] = float(out["confidence"])
                except Exception:
                    pass
            rows.append(out)
        return rows


def upsert_external_api_payload(
    provider: str,
    resource_type: str,
    external_id: str | int,
    raw_json: dict | list,
    *,
    internal_player_id: int | None = None,
    internal_team_id: int | None = None,
    competition_code: str | None = None,
    entity_name: str | None = None,
    image_url: str | None = None,
    image_local: str | None = None,
) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO external_api_payloads
                (provider, resource_type, external_id, internal_player_id, internal_team_id,
                 competition_code, entity_name, image_url, image_local, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (provider, resource_type, external_id) DO UPDATE SET
                internal_player_id=COALESCE(EXCLUDED.internal_player_id, external_api_payloads.internal_player_id),
                internal_team_id=COALESCE(EXCLUDED.internal_team_id, external_api_payloads.internal_team_id),
                competition_code=COALESCE(EXCLUDED.competition_code, external_api_payloads.competition_code),
                entity_name=COALESCE(EXCLUDED.entity_name, external_api_payloads.entity_name),
                image_url=COALESCE(EXCLUDED.image_url, external_api_payloads.image_url),
                image_local=COALESCE(EXCLUDED.image_local, external_api_payloads.image_local),
                raw_json=EXCLUDED.raw_json,
                updated_at=EXCLUDED.updated_at
        """, (
            provider,
            resource_type,
            str(external_id),
            internal_player_id,
            internal_team_id,
            competition_code,
            entity_name,
            image_url,
            image_local,
            json.dumps(raw_json),
            _now(),
        ))


def get_external_api_payloads(
    provider: str,
    resource_type: str | None = None,
    *,
    competition_code: str | None = None,
    limit: int = 200,
) -> list[dict]:
    query = """
        SELECT *
        FROM external_api_payloads
        WHERE provider=%s
    """
    params: list[Any] = [provider]
    if resource_type:
        query += " AND resource_type=%s"
        params.append(resource_type)
    if competition_code:
        query += " AND competition_code=%s"
        params.append(competition_code)
    query += " ORDER BY updated_at DESC NULLS LAST LIMIT %s"
    params.append(limit)
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        rows = []
        for row in cur.fetchall():
            out = dict(row)
            raw = out.get("raw_json")
            if isinstance(raw, (dict, list)):
                out["raw_json"] = raw
            else:
                try:
                    out["raw_json"] = json.loads(raw)
                except Exception:
                    out["raw_json"] = {}
            rows.append(out)
        return rows


# ---- Match graphs ----

def upsert_match_graph(match_id: int, graph_data: dict) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO match_graphs (match_id, graph_data, updated_at)
            VALUES (%s,%s,%s)
            ON CONFLICT (match_id) DO UPDATE SET graph_data=EXCLUDED.graph_data, updated_at=EXCLUDED.updated_at
        """, (match_id, json.dumps(graph_data), _now()))


def get_match_graph(match_id: int) -> dict | None:
    with _Cursor() as cur:
        cur.execute("SELECT graph_data FROM match_graphs WHERE match_id=%s", (match_id,))
        row = cur.fetchone()
        if row:
            gd = row["graph_data"]
            return gd if isinstance(gd, dict) else json.loads(gd)
    return None


def upsert_match_shotmap(match_id: int, shotmap_data: dict) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO match_shotmaps (match_id, shotmap_data, updated_at)
            VALUES (%s,%s,%s)
            ON CONFLICT (match_id) DO UPDATE SET
                shotmap_data=EXCLUDED.shotmap_data,
                updated_at=EXCLUDED.updated_at
        """, (match_id, json.dumps(shotmap_data), _now()))


def get_match_shotmap(match_id: int) -> dict | None:
    with _Cursor() as cur:
        cur.execute("SELECT shotmap_data FROM match_shotmaps WHERE match_id=%s", (match_id,))
        row = cur.fetchone()
        if row:
            data = row["shotmap_data"]
            return data if isinstance(data, dict) else json.loads(data)
    return None


# ---- Team transfers ----

def upsert_team_transfers(team_id: int, transfers: list[dict]) -> None:
    """Atomically replace all transfers for a team (DELETE + batch INSERT in one transaction)."""
    with _Cursor() as cur:
        cur.execute("DELETE FROM team_transfers WHERE team_id=%s", (team_id,))
        for t in transfers:
            cur.execute("""
                INSERT INTO team_transfers
                    (team_id, player_name, transfer_type, from_team, to_team,
                     transfer_fee, transfer_date, player_image, raw_json, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                team_id, t.get("player_name"), t.get("transfer_type"),
                t.get("from_team"), t.get("to_team"), t.get("transfer_fee"),
                t.get("transfer_date"), t.get("player_image"),
                json.dumps(t), _now(),
            ))


def get_team_transfers(team_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM team_transfers WHERE team_id=%s ORDER BY transfer_date DESC LIMIT 30
        """, (team_id,))
        return [dict(r) for r in cur.fetchall()]


# ---- Player season stats ----

def upsert_player_season_stats(player_id: str, season_year: str, stats: dict) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO player_season_stats
                (player_id, season_year, team_name, goals, assists, appearances, minutes_played,
                 rating, yellow_cards, red_cards, shots_total, shots_on_target,
                 dribbles_success, pass_accuracy, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (player_id, season_year) DO UPDATE SET
                team_name=EXCLUDED.team_name, goals=EXCLUDED.goals, assists=EXCLUDED.assists,
                appearances=EXCLUDED.appearances, minutes_played=EXCLUDED.minutes_played,
                rating=EXCLUDED.rating, yellow_cards=EXCLUDED.yellow_cards,
                red_cards=EXCLUDED.red_cards, shots_total=EXCLUDED.shots_total,
                shots_on_target=EXCLUDED.shots_on_target,
                dribbles_success=EXCLUDED.dribbles_success, pass_accuracy=EXCLUDED.pass_accuracy,
                raw_json=EXCLUDED.raw_json, updated_at=EXCLUDED.updated_at
        """, (
            player_id, season_year, stats.get("team_name"),
            stats.get("goals"), stats.get("assists"), stats.get("appearances"),
            stats.get("minutes_played"), stats.get("rating"),
            stats.get("yellow_cards"), stats.get("red_cards"),
            stats.get("shots_total"), stats.get("shots_on_target"),
            stats.get("dribbles_success"), stats.get("pass_accuracy"),
            json.dumps(stats), _now(),
        ))


def get_player_season_stats(player_id: str, season_year: str | None = None) -> list[dict]:
    with _Cursor() as cur:
        if season_year:
            cur.execute("""
                SELECT * FROM player_season_stats WHERE player_id=%s AND season_year=%s
            """, (player_id, season_year))
        else:
            cur.execute("""
                SELECT * FROM player_season_stats WHERE player_id=%s ORDER BY season_year DESC LIMIT 5
            """, (player_id,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("rating", "pass_accuracy"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            rows.append(d)
        return rows


# ---- Team form ----

def upsert_team_form(team_id: int, form_data: list[dict]) -> None:
    """Atomically replace all form records for a team (DELETE + batch INSERT in one transaction)."""
    with _Cursor() as cur:
        cur.execute("DELETE FROM team_form WHERE team_id=%s", (team_id,))
        for f in form_data:
            cur.execute("""
                INSERT INTO team_form
                    (team_id, match_date, opponent, result, goals_for, goals_against, venue, competition, raw_json, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                team_id, f.get("match_date"), f.get("opponent"), f.get("result"),
                f.get("goals_for"), f.get("goals_against"), f.get("venue"),
                f.get("competition"), json.dumps(f), _now(),
            ))


def get_team_form_db(team_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM team_form WHERE team_id=%s ORDER BY match_date DESC LIMIT 10
        """, (team_id,))
        return [dict(r) for r in cur.fetchall()]


# ---- Virtual game ----

def save_virtual_game(data: dict) -> int:
    sim = data.get("simulation") or {}
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO virtual_game_history
                (home_team, away_team, home_score, away_score, home_bet, away_bet,
                 winner, payout, house_cut, simulation_data, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get("home_team"), data.get("away_team"),
            data.get("home_score"), data.get("away_score"),
            data.get("home_bet"), data.get("away_bet"),
            data.get("winner"), data.get("payout"), data.get("house_cut"),
            json.dumps(sim), _now(),
        ))
        return cur.fetchone()["id"]


def get_virtual_game_history(limit: int = 100) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT id, home_team, away_team, home_score, away_score,
                   home_bet, away_bet, winner, payout, house_cut, created_at
            FROM virtual_game_history ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("home_bet", "away_bet", "payout", "house_cut"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            rows.append(d)
        return rows


# ---- Import log ----

def log_import(entity_type: str, entity_code: str, records: int, status: str, error: str = "") -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO import_log (entity_type, entity_code, records, status, error_message, imported_at)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (entity_type, entity_code, records, status, error, _now()))


def get_import_log(limit: int = 100) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM import_log ORDER BY imported_at DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_import_failures(limit: int = 50) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT *
            FROM import_log
            WHERE status <> 'ok' OR COALESCE(error_message, '') <> ''
            ORDER BY imported_at DESC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_known_seasons(limit_per_competition: int = 8) -> dict[str, list[str]]:
    with _Cursor() as cur:
        cur.execute("""
            WITH raw_seasons AS (
                SELECT competition_code, season FROM matches
                WHERE COALESCE(season, '') <> ''
                UNION
                SELECT competition_code, season FROM competition_teams
                WHERE COALESCE(season, '') <> ''
                UNION
                SELECT competition_code, season FROM competition_players
                WHERE COALESCE(season, '') <> ''
                UNION
                SELECT competition_code, season FROM scorers
                WHERE COALESCE(season, '') <> ''
            ),
            ranked AS (
                SELECT
                    competition_code,
                    season,
                    ROW_NUMBER() OVER (
                        PARTITION BY competition_code
                        ORDER BY
                            CASE WHEN season ~ '^[0-9]+$' THEN CAST(season AS INTEGER) ELSE 0 END DESC,
                            season DESC
                    ) AS rn
                FROM raw_seasons
            )
            SELECT competition_code, season
            FROM ranked
            WHERE rn <= %s
            ORDER BY competition_code, rn
        """, (limit_per_competition,))
        seasons: dict[str, list[str]] = {}
        for row in cur.fetchall():
            seasons.setdefault(row["competition_code"], []).append(row["season"])
        return seasons


# ---- DB stats ----

def get_db_stats() -> dict:
    tables = [
        "teams", "competitions", "competition_teams", "players", "competition_players",
        "matches", "standings", "scorers",
        "match_events", "match_statistics", "match_incidents", "match_graphs",
        "match_shotmaps",
        "news_articles", "player_profiles", "top_cards",
        "player_season_stats", "team_transfers", "team_form",
        "provider_id_mappings", "external_api_payloads",
        "odds_snapshots", "odds_markets_raw", "h2h_records",
        "virtual_game_history", "import_log",
        "images", "api_keys",
        "virtual_league_sessions", "virtual_league_fixtures", "virtual_league_scorers",
    ]
    stats: dict[str, int] = {}
    for table in tables:
        try:
            with _Cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
                row = cur.fetchone()
                stats[table] = row["cnt"] if row else 0
        except Exception:
            stats[table] = -1
    return stats


# ---- Match events ----

def upsert_match_events(match_id: int, match_data: dict) -> int:
    goals = match_data.get("goals") or []
    bookings = match_data.get("bookings") or []
    subs = match_data.get("substitutions") or []
    count = 0
    with _Cursor() as cur:
        for ev in goals:
            minute = (ev.get("minute") or {}).get("regular") if isinstance(ev.get("minute"), dict) else ev.get("minute")
            scorer = ev.get("scorer") or {}
            assist = ev.get("assist") or {}
            team = ev.get("team") or {}
            try:
                cur.execute("""
                    INSERT INTO match_events (match_id, minute, type, team_id, player_id, player_name,
                        assist_player_id, assist_player_name, detail, raw_json)
                    VALUES (%s,%s,'GOAL',%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (match_id, minute, type, player_id) DO NOTHING
                """, (
                    match_id, minute, team.get("id"), scorer.get("id"), scorer.get("name"),
                    assist.get("id"), assist.get("name"), ev.get("type"), json.dumps(ev),
                ))
                count += 1
            except Exception:
                pass
        for ev in bookings:
            minute = (ev.get("minute") or {}).get("regular") if isinstance(ev.get("minute"), dict) else ev.get("minute")
            player = ev.get("player") or {}
            team = ev.get("team") or {}
            try:
                cur.execute("""
                    INSERT INTO match_events (match_id, minute, type, team_id, player_id, player_name, detail, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (match_id, minute, type, player_id) DO NOTHING
                """, (
                    match_id, minute, ev.get("card"), team.get("id"),
                    player.get("id"), player.get("name"), ev.get("card"), json.dumps(ev),
                ))
                count += 1
            except Exception:
                pass
        for ev in subs:
            minute = (ev.get("minute") or {}).get("regular") if isinstance(ev.get("minute"), dict) else ev.get("minute")
            player_out = ev.get("playerOut") or {}
            player_in = ev.get("playerIn") or {}
            team = ev.get("team") or {}
            try:
                cur.execute("""
                    INSERT INTO match_events (match_id, minute, type, team_id, player_id, player_name,
                        assist_player_id, assist_player_name, detail, raw_json)
                    VALUES (%s,%s,'SUBSTITUTION',%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (match_id, minute, type, player_id) DO NOTHING
                """, (
                    match_id, minute, team.get("id"), player_out.get("id"), player_out.get("name"),
                    player_in.get("id"), player_in.get("name"), "substitution", json.dumps(ev),
                ))
                count += 1
            except Exception:
                pass
    return count


def get_match_events(match_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM match_events WHERE match_id=%s ORDER BY minute, id", (match_id,))
        return [dict(r) for r in cur.fetchall()]


def upsert_match_lineups(match_id: int, match_data: dict) -> int:
    lineups = match_data.get("lineups") or []
    count = 0
    with _Cursor() as cur:
        for lineup in lineups:
            team = lineup.get("team") or {}
            formation = lineup.get("formation")
            for player_entry in (lineup.get("startXI") or []):
                p = player_entry.get("player") or player_entry
                try:
                    cur.execute("""
                        INSERT INTO match_lineups (match_id, team_id, team_name, formation,
                            player_id, player_name, shirt_number, position, is_starter, raw_json)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (match_id, team_id, player_id) DO NOTHING
                    """, (
                        match_id, team.get("id"), team.get("name"), formation,
                        p.get("id"), p.get("name"), p.get("number") or p.get("shirtNumber"),
                        p.get("pos") or p.get("position"), True, json.dumps(p),
                    ))
                    count += 1
                except Exception:
                    pass
            for player_entry in (lineup.get("substitutes") or []):
                p = player_entry.get("player") or player_entry
                try:
                    cur.execute("""
                        INSERT INTO match_lineups (match_id, team_id, team_name, formation,
                            player_id, player_name, shirt_number, position, is_starter, raw_json)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (match_id, team_id, player_id) DO NOTHING
                    """, (
                        match_id, team.get("id"), team.get("name"), formation,
                        p.get("id"), p.get("name"), p.get("number") or p.get("shirtNumber"),
                        p.get("pos") or p.get("position"), False, json.dumps(p),
                    ))
                    count += 1
                except Exception:
                    pass
    return count


def get_match_lineups(match_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM match_lineups WHERE match_id=%s ORDER BY team_id, is_starter DESC, shirt_number
        """, (match_id,))
        return [dict(r) for r in cur.fetchall()]


def upsert_match_statistics(match_id: int, stats: list[dict]) -> int:
    count = 0
    with _Cursor() as cur:
        for s in stats:
            try:
                cur.execute("""
                    INSERT INTO match_statistics
                        (match_id, period, category, name, home_value, away_value, render_type, stat_key)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (match_id, period, stat_key) DO UPDATE SET
                        home_value=EXCLUDED.home_value, away_value=EXCLUDED.away_value
                """, (
                    match_id, s.get("period", "ALL"), s.get("category", ""),
                    s.get("name", ""),
                    str(s.get("home") or s.get("home_value") or ""),
                    str(s.get("away") or s.get("away_value") or ""),
                    s.get("render_type", ""), s.get("key", s.get("stat_key", "")),
                ))
                count += 1
            except Exception:
                pass
    return count


def get_match_statistics(match_id: int, period: str = "ALL") -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM match_statistics WHERE match_id=%s AND period=%s ORDER BY category, name
        """, (match_id, period))
        return [dict(r) for r in cur.fetchall()]


def upsert_match_incidents(match_id: int, incidents: list[dict]) -> int:
    count = 0
    with _Cursor() as cur:
        for inc in incidents:
            try:
                cur.execute("""
                    INSERT INTO match_incidents
                        (match_id, incident_type, minute, added_time, is_home,
                         player_name, player2_name, description, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (match_id, incident_type, minute, player_name) DO NOTHING
                """, (
                    match_id, inc.get("incidentType") or inc.get("incident_type"),
                    inc.get("time"), inc.get("addedTime"),
                    inc.get("isHome"),
                    inc.get("player_name") or (inc.get("player") or {}).get("name", ""),
                    inc.get("player2_name") or (inc.get("playerIn") or {}).get("name", ""),
                    inc.get("description") or inc.get("text", ""),
                    json.dumps(inc),
                ))
                count += 1
            except Exception:
                pass
    return count


def get_match_incidents(match_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM match_incidents WHERE match_id=%s ORDER BY minute, id", (match_id,))
        return [dict(r) for r in cur.fetchall()]


# ---- News ----

def upsert_news_article(article: dict, competition_code: str, league_id: int | None = None) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO news_articles
                (article_id, url, competition_code, league_id, title, summary, image_url,
                 image_local, source, source_url, published_at, category, alias, lang,
                 categories, teams, leagues, countries, raw_json, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (url) DO UPDATE SET
                title=EXCLUDED.title, summary=EXCLUDED.summary,
                image_url=COALESCE(EXCLUDED.image_url, news_articles.image_url),
                image_local=COALESCE(EXCLUDED.image_local, news_articles.image_local),
                source=COALESCE(EXCLUDED.source, news_articles.source),
                source_url=COALESCE(EXCLUDED.source_url, news_articles.source_url),
                published_at=EXCLUDED.published_at,
                alias=COALESCE(EXCLUDED.alias, news_articles.alias),
                lang=COALESCE(EXCLUDED.lang, news_articles.lang),
                categories=COALESCE(EXCLUDED.categories, news_articles.categories),
                teams=COALESCE(EXCLUDED.teams, news_articles.teams),
                leagues=COALESCE(EXCLUDED.leagues, news_articles.leagues),
                countries=COALESCE(EXCLUDED.countries, news_articles.countries),
                raw_json=EXCLUDED.raw_json
        """, (
            article.get("id"), article.get("url"), competition_code, league_id,
            article.get("title"),
            article.get("summary") or article.get("description"),
            article.get("image") or article.get("image_url"),
            article.get("image_local"),
            article.get("source") or article.get("publisher"),
            article.get("source_url"),
            article.get("published") or article.get("published_at"),
            article.get("category"),
            article.get("alias"),
            article.get("lang"),
            json.dumps(article.get("categories") or []),
            json.dumps(article.get("teams") or []),
            json.dumps(article.get("leagues") or []),
            json.dumps(article.get("countries") or []),
            json.dumps(article), _now(),
        ))


def set_news_image_local(url: str, local_path: str) -> None:
    if not url or not local_path:
        return
    with _Cursor() as cur:
        cur.execute("""
            UPDATE news_articles
            SET image_local=%s
            WHERE url=%s
        """, (local_path, url))


def get_news_for_competition(competition_code: str, limit: int | None = 20) -> list[dict]:
    query = """
        SELECT * FROM news_articles WHERE competition_code=%s
        ORDER BY published_at DESC NULLS LAST
    """
    params: list[Any] = [competition_code]
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            item["categories"] = _json_array(item.get("categories"))
            item["teams"] = _json_array(item.get("teams"))
            item["leagues"] = _json_array(item.get("leagues"))
            item["countries"] = _json_array(item.get("countries"))
            rows.append(item)
        return rows


def get_recent_news(limit: int | None = 20) -> list[dict]:
    query = "SELECT * FROM news_articles ORDER BY published_at DESC NULLS LAST"
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with _Cursor() as cur:
        cur.execute(query, tuple(params))
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            item["categories"] = _json_array(item.get("categories"))
            item["teams"] = _json_array(item.get("teams"))
            item["leagues"] = _json_array(item.get("leagues"))
            item["countries"] = _json_array(item.get("countries"))
            rows.append(item)
        return rows


# ---- Player profiles ----

def upsert_player_profile(player_name: str, team_name: str, team_id: int | None, profile: dict) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO player_profiles
                (player_name, team_name, team_id, photo_url, photo_local, height, weight, age, country, position,
                 market_value, shirt_number, rating, foot, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (player_name, team_name) DO UPDATE SET
                photo_url=COALESCE(EXCLUDED.photo_url, player_profiles.photo_url),
                photo_local=COALESCE(EXCLUDED.photo_local, player_profiles.photo_local),
                height=COALESCE(EXCLUDED.height, player_profiles.height),
                weight=COALESCE(EXCLUDED.weight, player_profiles.weight),
                age=EXCLUDED.age, country=EXCLUDED.country, position=EXCLUDED.position,
                market_value=COALESCE(EXCLUDED.market_value, player_profiles.market_value),
                shirt_number=COALESCE(EXCLUDED.shirt_number, player_profiles.shirt_number),
                rating=COALESCE(EXCLUDED.rating, player_profiles.rating),
                foot=COALESCE(EXCLUDED.foot, player_profiles.foot),
                raw_json=EXCLUDED.raw_json, updated_at=EXCLUDED.updated_at
        """, (
            player_name, team_name, team_id,
            profile.get("photo"), profile.get("photo_local"), profile.get("height"), profile.get("weight"),
            profile.get("age"), profile.get("country"), profile.get("position"),
            profile.get("market_value"), profile.get("shirt"), profile.get("rating"),
            profile.get("foot"), json.dumps(profile), _now(),
        ))


def get_player_profile(player_name: str, team_name: str | None = None) -> dict | None:
    with _Cursor() as cur:
        if team_name:
            cur.execute("""
                SELECT * FROM player_profiles WHERE player_name=%s AND team_name=%s LIMIT 1
            """, (player_name, team_name))
        else:
            cur.execute("""
                SELECT * FROM player_profiles WHERE player_name=%s ORDER BY updated_at DESC LIMIT 1
            """, (player_name,))
        row = cur.fetchone()
        if row:
            d = dict(row)
            d.setdefault("photo", d.pop("photo_url", None))
            d.setdefault("photo_local", d.get("photo_local"))
            d.setdefault("shirt", d.pop("shirt_number", None))
            return d
    return None


# ---- Odds snapshots ----

def upsert_odds_snapshot(match_key: str, competition_code: str, home: str, away: str, odds: dict) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO odds_snapshots (match_key, competition_code, home_team, away_team,
                odds_home, odds_draw, odds_away, bookmaker, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (match_key) DO UPDATE SET
                odds_home=EXCLUDED.odds_home, odds_draw=EXCLUDED.odds_draw,
                odds_away=EXCLUDED.odds_away, bookmaker=EXCLUDED.bookmaker,
                updated_at=EXCLUDED.updated_at
        """, (
            match_key, competition_code, home, away,
            odds.get("home"), odds.get("draw"), odds.get("away"),
            odds.get("bookmaker"), _now(),
        ))


def get_odds_snapshot(competition_code: str, home: str, away: str) -> dict | None:
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM odds_snapshots
            WHERE competition_code=%s AND home_team=%s AND away_team=%s
            ORDER BY updated_at DESC LIMIT 1
        """, (competition_code, home, away))
        row = cur.fetchone()
        if row:
            d = dict(row)
            for k in ("odds_home", "odds_draw", "odds_away"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            return d
    return None


def get_odds_snapshots_for_competition(competition_code: str, limit: int = 100) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT *
            FROM odds_snapshots
            WHERE competition_code=%s
            ORDER BY updated_at DESC NULLS LAST, id DESC
            LIMIT %s
        """, (competition_code, limit))
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            for k in ("odds_home", "odds_draw", "odds_away"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            rows.append(d)
        return rows


def upsert_odds_market_raw(
    match_key: str,
    event_id: str | None,
    sport_key: str | None,
    competition_code: str,
    home: str,
    away: str,
    commence_time: str | None,
    raw_data: dict,
) -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO odds_markets_raw (
                match_key, event_id, sport_key, competition_code,
                home_team, away_team, commence_time, raw_json, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (match_key) DO UPDATE SET
                event_id=EXCLUDED.event_id,
                sport_key=EXCLUDED.sport_key,
                competition_code=EXCLUDED.competition_code,
                home_team=EXCLUDED.home_team,
                away_team=EXCLUDED.away_team,
                commence_time=EXCLUDED.commence_time,
                raw_json=EXCLUDED.raw_json,
                updated_at=EXCLUDED.updated_at
        """, (
            match_key, event_id, sport_key, competition_code,
            home, away, commence_time, json.dumps(raw_data), _now(),
        ))


def get_odds_market_raw(competition_code: str, home: str, away: str) -> dict | None:
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM odds_markets_raw
            WHERE competition_code=%s AND home_team=%s AND away_team=%s
            ORDER BY updated_at DESC LIMIT 1
        """, (competition_code, home, away))
        row = cur.fetchone()
        return dict(row) if row else None


def get_odds_markets_for_competition(competition_code: str, limit: int = 100) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT *
            FROM odds_markets_raw
            WHERE competition_code=%s
            ORDER BY updated_at DESC NULLS LAST, id DESC
            LIMIT %s
        """, (competition_code, limit))
        return [dict(row) for row in cur.fetchall()]


# ---- H2H records ----

def upsert_h2h_records(first_team: str, second_team: str, records: list[dict]) -> int:
    t1, t2 = sorted([first_team, second_team])
    count = 0
    with _Cursor() as cur:
        for r in records:
            try:
                cur.execute("""
                    INSERT INTO h2h_records
                        (team1, team2, match_date, home_team, away_team, score_home, score_away, winner, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (team1, team2, match_date) DO UPDATE SET
                        score_home=EXCLUDED.score_home, score_away=EXCLUDED.score_away,
                        winner=EXCLUDED.winner, raw_json=EXCLUDED.raw_json
                """, (t1, t2, r.get("date"), r.get("home"), r.get("away"),
                      r.get("score_home"), r.get("score_away"), r.get("winner"),
                      json.dumps(r)))
                count += 1
            except Exception:
                pass
    return count


def get_h2h_records(first_team: str, second_team: str, limit: int = 10) -> list[dict]:
    t1, t2 = sorted([first_team, second_team])
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM h2h_records WHERE team1=%s AND team2=%s ORDER BY match_date DESC LIMIT %s
        """, (t1, t2, limit))
        return [dict(r) for r in cur.fetchall()]


# ---- Top cards ----

def upsert_top_card(card: dict, competition_code: str) -> None:
    yc = card.get("yellow_cards") or 0
    rc = card.get("red_cards") or 0
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO top_cards
                (competition_code, player_name, team_name, yellow_cards, red_cards, total_cards, raw_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (competition_code, player_name) DO UPDATE SET
                yellow_cards=EXCLUDED.yellow_cards, red_cards=EXCLUDED.red_cards,
                total_cards=EXCLUDED.total_cards, raw_json=EXCLUDED.raw_json,
                updated_at=EXCLUDED.updated_at
        """, (
            competition_code, card.get("player_name") or card.get("name"),
            card.get("team_name") or card.get("team"),
            yc, rc, yc + rc, json.dumps(card), _now(),
        ))


def get_top_cards(competition_code: str, limit: int = 10) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT * FROM top_cards WHERE competition_code=%s ORDER BY total_cards DESC LIMIT %s
        """, (competition_code, limit))
        return [dict(r) for r in cur.fetchall()]


# ---- Virtual League ----

def create_virtual_league_session(
    league_code: str,
    league_name: str,
    total_matchdays: int,
    season_name: str | None = None,
) -> str:
    """Create a new virtual league session. Returns the session key."""
    import uuid
    session_key = str(uuid.uuid4()).replace("-", "")[:12]
    label = (season_name or "").strip() or f"{league_name} Season"
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO virtual_league_sessions
                (session_key, league_code, league_name, season_name, current_matchday, total_matchdays, status, created_at, updated_at)
            VALUES (%s,%s,%s,%s,1,%s,'active',%s,%s)
        """, (session_key, league_code, league_name, label, total_matchdays, _now(), _now()))
    return session_key


def get_virtual_league_session(session_key: str) -> dict | None:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM virtual_league_sessions WHERE session_key=%s", (session_key,))
        row = cur.fetchone()
        return dict(row) if row else None


def save_virtual_league_fixtures(session_id: int, all_matchdays: list[list[tuple[str, str]]]) -> None:
    """Save all pre-generated fixtures for a session."""
    with _Cursor() as cur:
        for md_num, fixtures in enumerate(all_matchdays, 1):
            for home, away in fixtures:
                cur.execute("""
                    INSERT INTO virtual_league_fixtures
                        (session_id, matchday, home_team, away_team, status)
                    VALUES (%s,%s,%s,%s,'scheduled')
                    ON CONFLICT (session_id, matchday, home_team, away_team) DO NOTHING
                """, (session_id, md_num, home, away))


def get_virtual_league_fixtures(session_id: int, matchday: int | None = None) -> list[dict]:
    with _Cursor() as cur:
        if matchday is not None:
            cur.execute("""
                SELECT * FROM virtual_league_fixtures
                WHERE session_id=%s AND matchday=%s ORDER BY id
            """, (session_id, matchday))
        else:
            cur.execute("""
                SELECT * FROM virtual_league_fixtures
                WHERE session_id=%s ORDER BY matchday, id
            """, (session_id,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("home_scorers", "away_scorers", "home_stats", "away_stats"):
                v = d.get(k)
                if v is not None and isinstance(v, str):
                    try:
                        d[k] = json.loads(v)
                    except Exception:
                        d[k] = {} if k.endswith("_stats") else []
                elif v is None:
                    d[k] = {} if k.endswith("_stats") else []
            rows.append(d)
        return rows


def get_all_played_results(session_id: int) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT home_team, away_team, home_score, away_score
            FROM virtual_league_fixtures
            WHERE session_id=%s AND status='played'
            ORDER BY matchday
        """, (session_id,))
        return [dict(r) for r in cur.fetchall()]


def save_matchday_results(session_id: int, matchday: int, results: list[dict]) -> None:
    """Persist simulation results for a matchday and update scorer totals."""
    with _Cursor() as cur:
        for r in results:
            cur.execute("""
                UPDATE virtual_league_fixtures
                SET home_score=%s, away_score=%s,
                    home_scorers=%s, away_scorers=%s,
                    home_stats=%s, away_stats=%s,
                    status='played', played_at=%s
                WHERE session_id=%s AND matchday=%s
                  AND home_team=%s AND away_team=%s
            """, (
                r["home_score"], r["away_score"],
                json.dumps(r.get("home_scorers", [])),
                json.dumps(r.get("away_scorers", [])),
                json.dumps(r.get("stats", {}).get("home", {})),
                json.dumps(r.get("stats", {}).get("away", {})),
                _now(),
                session_id, matchday,
                r["home_team"], r["away_team"],
            ))
        # Update scorer totals
        for r in results:
            for side in ("home", "away"):
                team = r[f"{side}_team"]
                for scorer in r.get(f"{side}_scorers", []):
                    player = scorer.get("player")
                    assist = scorer.get("assist")
                    if not player:
                        continue
                    cur.execute("""
                        INSERT INTO virtual_league_scorers (session_id, player_name, team_name, goals, assists)
                        VALUES (%s,%s,%s,1,%s)
                        ON CONFLICT (session_id, player_name, team_name) DO UPDATE SET
                            goals = virtual_league_scorers.goals + 1,
                            assists = virtual_league_scorers.assists + EXCLUDED.assists
                    """, (session_id, player, team, 1 if assist else 0))
                    if assist:
                        cur.execute("""
                            INSERT INTO virtual_league_scorers (session_id, player_name, team_name, goals, assists)
                            VALUES (%s,%s,%s,0,1)
                            ON CONFLICT (session_id, player_name, team_name) DO UPDATE SET
                                assists = virtual_league_scorers.assists + 1
                        """, (session_id, assist, team))


def advance_virtual_league_matchday(session_key: str, next_matchday: int, total_matchdays: int) -> None:
    status = "completed" if next_matchday > total_matchdays else "active"
    with _Cursor() as cur:
        cur.execute("""
            UPDATE virtual_league_sessions
            SET current_matchday=%s, status=%s, updated_at=%s
            WHERE session_key=%s
        """, (min(next_matchday, total_matchdays + 1), status, _now(), session_key))


def get_virtual_league_scorers(session_id: int, limit: int = 30) -> list[dict]:
    with _Cursor() as cur:
        cur.execute("""
            SELECT player_name, team_name, goals, assists
            FROM virtual_league_scorers
            WHERE session_id=%s
            ORDER BY goals DESC, assists DESC LIMIT %s
        """, (session_id, limit))
        return [dict(r) for r in cur.fetchall()]


def list_virtual_league_sessions(league_code: str | None = None, limit: int = 20) -> list[dict]:
    with _Cursor() as cur:
        if league_code:
            cur.execute("""
                SELECT * FROM virtual_league_sessions
                WHERE league_code=%s ORDER BY created_at DESC LIMIT %s
            """, (league_code, limit))
        else:
            cur.execute("""
                SELECT * FROM virtual_league_sessions ORDER BY created_at DESC LIMIT %s
            """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def delete_virtual_league_session(session_key: str) -> None:
    with _Cursor() as cur:
        cur.execute("""
            DELETE FROM virtual_league_fixtures WHERE session_id IN (
                SELECT id FROM virtual_league_sessions WHERE session_key=%s
            )
        """, (session_key,))
        cur.execute("""
            DELETE FROM virtual_league_scorers WHERE session_id IN (
                SELECT id FROM virtual_league_sessions WHERE session_key=%s
            )
        """, (session_key,))
        cur.execute("DELETE FROM virtual_league_sessions WHERE session_key=%s", (session_key,))


# ---- API keys ----

def save_api_key(key_name: str, key_value: str, endpoint: str = "") -> None:
    with _Cursor() as cur:
        cur.execute("""
            INSERT INTO api_keys (key_name, key_value, endpoint, updated_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (key_name) DO UPDATE SET key_value=EXCLUDED.key_value, updated_at=EXCLUDED.updated_at
        """, (key_name, key_value, endpoint, _now()))


def get_api_keys() -> dict[str, dict]:
    with _Cursor() as cur:
        cur.execute("SELECT * FROM api_keys ORDER BY key_name")
        return {r["key_name"]: dict(r) for r in cur.fetchall()}
