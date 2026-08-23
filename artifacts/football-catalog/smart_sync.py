"""Smart incremental sync for DKSPORTS.

Runs on every app startup in a background thread.
- Checks each competition's last-updated timestamps per entity type.
- Only hits the API for data that is missing or older than the staleness threshold.
- Uses INSERT ... ON CONFLICT / upsert so only changed rows are modified in PostgreSQL.
- Respects the football-data.org free-tier rate limit (~10 req/min).
- Also syncs: match events/lineups, match stats (footapi7), news (news11),
  top cards (apifootball), player profiles (apifootball), odds snapshots.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import api_client
import db_client
import data_importer
import news_client
import api_football
import odds_client
import shotmap_client
from api_client import FREE_TIER_COMPETITIONS

# -------------------------------------------------------------------
# Staleness thresholds (hours).
# -------------------------------------------------------------------
THRESHOLDS: dict[str, float] = {
    "competition":      48.0,
    "teams":            24.0,
    "standings":         4.0,
    "scorers":           4.0,
    "matches":           1.0,
    "match_events":     24.0,   # once FINISHED, events never change
    "match_statistics": 24.0,   # once FINISHED, stats never change
    "news":              2.0,   # refresh news every 2 hours
    "top_cards":         6.0,
    "odds":              0.25,  # 15 min — odds change frequently
    "player_profiles":  48.0,
}

API_DELAY = 7.0  # seconds between football-data.org calls

_sync_lock = threading.Lock()
_sync_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "current": "",
    "synced": [],
    "skipped": [],
    "errors": [],
}


def get_sync_status() -> dict:
    with _sync_lock:
        return dict(_sync_status)


def _update(**kwargs: Any) -> None:
    with _sync_lock:
        _sync_status.update(kwargs)


def _log(msg: str, level: str = "info") -> None:
    print(f"[smart_sync:{level}] {msg}", flush=True)


def _last_updated(competition_code: str, entity_type: str) -> datetime | None:
    try:
        raw = db_client.get_last_import(competition_code, entity_type)
        if raw:
            try:
                return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.fromisoformat(raw)
    except Exception:
        pass
    return None


def _is_stale(competition_code: str, entity_type: str) -> bool:
    last = _last_updated(competition_code, entity_type)
    if last is None:
        return True
    age = datetime.now(timezone.utc) - last
    return age > timedelta(hours=THRESHOLDS.get(entity_type, 6.0))


def _has_key() -> bool:
    return bool(os.environ.get("FOOTBALL_DATA_API_KEY"))


def _current_season() -> str:
    return str(datetime.now(timezone.utc).year)


def _season_for_code(code: str) -> str:
    try:
        season = db_client.get_season_for_code(code)
        return season if season else _current_season()
    except Exception:
        return _current_season()


# -------------------------------------------------------------------
# football-data.org syncs
# -------------------------------------------------------------------

def _sync_competition_meta(code: str) -> bool:
    if not _is_stale(code, "competition"):
        return False
    try:
        comp_data = api_client.get_competition(code)
        db_client.upsert_competition(comp_data)
        db_client.log_import("competition", code, 1, "ok")
        _log(f"  [{code}] competition metadata refreshed")
        return True
    except Exception as e:
        db_client.log_import("competition", code, 0, "error", str(e))
        _log(f"  [{code}] competition metadata error: {e}", "warn")
        return False


def _sync_teams(code: str, season: str) -> int:
    if not _is_stale(code, "teams"):
        return 0
    try:
        teams_data = api_client.get_competition_teams(code)
        teams = teams_data.get("teams") or []
        league_name = (teams_data.get("competition") or {}).get("name") or code
        for team in teams:
            db_client.upsert_team(team, code, season)
            crest = team.get("crest")
            if crest:
                dest = data_importer._local_logo_path(team.get("name", "unknown"), league_name, crest)
                data_importer.download_image(crest, dest, "team", str(team.get("id", "")))
        db_client.log_import("teams", code, len(teams), "ok")
        _log(f"  [{code}] teams synced: {len(teams)}")
        return len(teams)
    except Exception as e:
        db_client.log_import("teams", code, 0, "error", str(e))
        _log(f"  [{code}] teams error: {e}", "warn")
        return 0


def _sync_standings(code: str, season: str) -> int:
    if not _is_stale(code, "standings"):
        return 0
    try:
        data = api_client.get_standings(code)
        count = 0
        for group in (data.get("standings") or []):
            for row in (group.get("table") or []):
                db_client.upsert_standing(row, code, season)
                count += 1
        db_client.log_import("standings", code, count, "ok")
        _log(f"  [{code}] standings synced: {count} rows")
        return count
    except Exception as e:
        db_client.log_import("standings", code, 0, "error", str(e))
        _log(f"  [{code}] standings error: {e}", "warn")
        return 0


def _sync_scorers(code: str, season: str) -> int:
    if not _is_stale(code, "scorers"):
        return 0
    try:
        data = api_client.get_scorers(code, limit=20)
        scorers = data.get("scorers") or []
        for i, scorer in enumerate(scorers):
            db_client.upsert_scorer(scorer, code, season, i + 1)
        db_client.log_import("scorers", code, len(scorers), "ok")
        _log(f"  [{code}] scorers synced: {len(scorers)}")
        return len(scorers)
    except Exception as e:
        db_client.log_import("scorers", code, 0, "error", str(e))
        _log(f"  [{code}] scorers error: {e}", "warn")
        return 0


def _sync_matches(code: str, season: str) -> int:
    if not _is_stale(code, "matches"):
        return 0
    try:
        data = api_client.get_competition_matches(code)
        matches = data.get("matches") or []
        for match in matches:
            db_client.upsert_match(match, code, season)
        db_client.log_import("matches", code, len(matches), "ok")
        _log(f"  [{code}] matches synced: {len(matches)}")
        return len(matches)
    except Exception as e:
        db_client.log_import("matches", code, 0, "error", str(e))
        _log(f"  [{code}] matches error: {e}", "warn")
        return 0


def _sync_match_detail_events(code: str, limit: int = 5) -> int:
    """Fetch events + lineups for finished matches that don't have events yet."""
    if not _is_stale(code, "match_events"):
        return 0
    try:
        match_ids = db_client.get_finished_matches_without_events(code, limit)
    except Exception:
        return 0

    if not match_ids:
        db_client.log_import("match_events", code, 0, "ok")
        return 0

    count = 0
    for mid in match_ids:
        try:
            mdata = api_client.get_match(mid)
            db_client.upsert_match_events(mid, mdata)
            db_client.upsert_match_lineups(mid, mdata)
            count += 1
            time.sleep(API_DELAY)
        except Exception as e:
            _log(f"  [{code}] match {mid} events error: {e}", "warn")

    db_client.log_import("match_events", code, count, "ok")
    _log(f"  [{code}] match events/lineups synced for {count} matches")
    return count


# -------------------------------------------------------------------
# footapi7 syncs (match statistics + incidents)
# -------------------------------------------------------------------

def _sync_match_statistics(code: str, limit: int = 5) -> int:
    """Fetch footapi7 statistics for finished matches without stats yet."""
    if not shotmap_client.has_shotmap_api():
        return 0
    if not _is_stale(code, "match_statistics"):
        return 0
    try:
        match_ids = db_client.get_finished_matches_without_stats(code, limit)
    except Exception:
        return 0

    if not match_ids:
        db_client.log_import("match_statistics", code, 0, "ok")
        return 0

    count = 0
    for mid in match_ids:
        try:
            raw = shotmap_client.get_match_statistics(mid)
            if raw:
                stats = shotmap_client.parse_statistics(raw)
                db_client.upsert_match_statistics(mid, stats)
            raw_inc = shotmap_client.get_match_incidents(mid)
            if raw_inc:
                db_client.upsert_match_incidents(mid, raw_inc.get("incidents") or [])
            count += 1
            time.sleep(1.0)
        except Exception as e:
            _log(f"  [{code}] match {mid} stats error: {e}", "warn")

    db_client.log_import("match_statistics", code, count, "ok")
    _log(f"  [{code}] match statistics synced for {count} matches")
    return count


# -------------------------------------------------------------------
# news_client syncs
# -------------------------------------------------------------------

def _sync_news(code: str) -> int:
    if not news_client.has_news_api():
        return 0
    if not _is_stale(code, "news"):
        return 0
    try:
        articles = news_client.fetch_news_for_competition(code, lang="en", page=1)
        lid = news_client.league_id_for_competition(code)
        count = 0
        for art in articles:
            if not art.get("url"):
                continue
            db_client.upsert_news_article(art, code, lid)
            count += 1
        db_client.log_import("news", code, count, "ok")
        _log(f"  [{code}] news synced: {count} articles")
        return count
    except Exception as e:
        db_client.log_import("news", code, 0, "error", str(e))
        _log(f"  [{code}] news error: {e}", "warn")
        return 0


# -------------------------------------------------------------------
# apifootball.com syncs
# -------------------------------------------------------------------

def _sync_top_cards(code: str) -> int:
    if not api_football.has_api_football():
        return 0
    if not _is_stale(code, "top_cards"):
        return 0
    league_id = api_football.league_id_for_competition(code)
    if not league_id:
        return 0
    try:
        cards = api_football.get_top_cards_for_league(league_id, limit=20)
        for card in cards:
            db_client.upsert_top_card(card, code)
        db_client.log_import("top_cards", code, len(cards), "ok")
        _log(f"  [{code}] top cards synced: {len(cards)}")
        return len(cards)
    except Exception as e:
        db_client.log_import("top_cards", code, 0, "error", str(e))
        _log(f"  [{code}] top cards error: {e}", "warn")
        return 0


def _sync_player_profiles(code: str) -> int:
    """Sync player photos/profiles for top scorers in this competition."""
    if not api_football.has_api_football():
        return 0
    if not _is_stale(code, "player_profiles"):
        return 0
    try:
        scorers = db_client.get_scorers_for_sync(code, limit=10)
    except Exception:
        return 0

    count = 0
    for row in scorers:
        pname = row["player_name"]
        tname = row["team_name"]
        if not pname:
            continue
        try:
            profile = api_football.find_player_profile(pname, tname)
            if profile:
                db_client.upsert_player_profile(pname, tname, row["team_id"], profile)
                count += 1
        except Exception:
            pass

    if count:
        db_client.log_import("player_profiles", code, count, "ok")
        _log(f"  [{code}] player profiles synced: {count}")
    return count


# -------------------------------------------------------------------
# Odds snapshot sync
# -------------------------------------------------------------------

def _sync_odds_snapshot(code: str) -> int:
    """Cache a snapshot of current h2h odds for upcoming matches."""
    if not odds_client.has_odds_api():
        return 0
    if not _is_stale(code, "odds"):
        return 0
    sport = odds_client.COMPETITION_TO_SPORT.get(code.upper())
    if not sport:
        return 0
    try:
        events = odds_client.fetch_odds_for_sport(sport, ttl=900)
        count = 0
        for ev in events:
            home = ev.get("home_team")
            away = ev.get("away_team")
            if not home or not away:
                continue
            match_key = f"{code}:{home}:{away}"
            odds_h2h = odds_client._best_h2h(ev)
            if odds_h2h:
                db_client.upsert_odds_snapshot(match_key, code, home, away, odds_h2h)
                count += 1
        db_client.log_import("odds", code, count, "ok")
        _log(f"  [{code}] odds snapshots synced: {count}")
        return count
    except Exception as e:
        db_client.log_import("odds", code, 0, "error", str(e))
        _log(f"  [{code}] odds error: {e}", "warn")
        return 0


# -------------------------------------------------------------------
# Per-competition incremental sync
# -------------------------------------------------------------------

def sync_one(code: str) -> dict[str, Any]:
    """Incrementally sync a single competition across all data types."""
    result: dict[str, Any] = {"code": code, "refreshed": [], "skipped": [], "errors": []}
    _update(current=code)
    season = _season_for_code(code)

    def _run(label: str, fn, *args):
        time.sleep(API_DELAY)
        try:
            n = fn(*args)
            (result["refreshed"] if n else result["skipped"]).append(f"{label}({n})")
        except Exception as e:
            result["errors"].append({"entity": label, "error": str(e)})
            _log(f"  [{code}] {label} unexpected error: {e}", "error")

    def _run_fast(label: str, fn, *args):
        """Run without the football-data.org delay (for other APIs)."""
        try:
            n = fn(*args)
            (result["refreshed"] if n else result["skipped"]).append(f"{label}({n})")
        except Exception as e:
            result["errors"].append({"entity": label, "error": str(e)})

    # --- football-data.org (rate-limited) ---
    _run("competition", _sync_competition_meta, code)
    _run("teams", _sync_teams, code, season)
    _run("standings", _sync_standings, code, season)
    _run("scorers", _sync_scorers, code, season)
    _run("matches", _sync_matches, code, season)
    _run("match_events", _sync_match_detail_events, code)

    # --- footapi7 (separate rate limit) ---
    _run_fast("match_statistics", _sync_match_statistics, code)

    # --- news (quota-limited: ~100 calls/day) ---
    _run_fast("news", _sync_news, code)

    # --- apifootball.com ---
    _run_fast("top_cards", _sync_top_cards, code)
    _run_fast("player_profiles", _sync_player_profiles, code)

    # --- The Odds API ---
    _run_fast("odds", _sync_odds_snapshot, code)

    return result


# -------------------------------------------------------------------
# Full background sync
# -------------------------------------------------------------------

def run_smart_sync(codes: list[str] | None = None) -> None:
    if not _has_key():
        _log("No FOOTBALL_DATA_API_KEY set — skipping sync", "warn")
        return

    target = codes or [c["code"] for c in FREE_TIER_COMPETITIONS if c.get("code")]
    _update(
        running=True,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        current="",
        synced=[],
        skipped=[],
        errors=[],
    )
    _log(f"Smart sync started: {len(target)} competitions to check")

    for code in target:
        with _sync_lock:
            if not _sync_status["running"]:
                _log("Sync cancelled")
                break
        try:
            result = sync_one(code)
            if result["refreshed"]:
                _log(f"[{code}] refreshed: {result['refreshed']}")
                with _sync_lock:
                    _sync_status["synced"].append(code)
            else:
                _log(f"[{code}] all data fresh — skipped")
                with _sync_lock:
                    _sync_status["skipped"].append(code)
        except Exception as e:
            _log(f"[{code}] unexpected error: {e}", "error")
            with _sync_lock:
                _sync_status["errors"].append({"code": code, "error": str(e)})

    _update(
        running=False,
        current="",
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    stats = db_client.get_db_stats()
    _log(
        f"Smart sync complete — "
        f"teams={stats.get('teams', 0)}, standings={stats.get('standings', 0)}, "
        f"matches={stats.get('matches', 0)}, scorers={stats.get('scorers', 0)}, "
        f"events={stats.get('match_events', 0)}, stats={stats.get('match_statistics', 0)}, "
        f"news={stats.get('news_articles', 0)}, cards={stats.get('top_cards', 0)}"
    )


_sync_thread: threading.Thread | None = None


def start_smart_sync_in_background(codes: list[str] | None = None) -> bool:
    global _sync_thread
    with _sync_lock:
        if _sync_status["running"]:
            return False
    _sync_thread = threading.Thread(
        target=run_smart_sync, args=(codes,), daemon=True, name="dksports-smart-sync"
    )
    _sync_thread.start()
    return True
