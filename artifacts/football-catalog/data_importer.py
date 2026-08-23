"""Data importer: archives tracked football data into PostgreSQL for offline use.

Call run_full_import() to fetch all tracked leagues, or import_competition(code)
for one league. The importer is designed to run in a background thread so the UI
stays responsive while full season results, matchweeks, standings, scorers,
teams, and media are cached locally.
"""
from __future__ import annotations

import hashlib
import html
import os
import threading
import time
from typing import Any

import requests

import api_client
import db_client
from api_client import FootballDataError, FREE_TIER_COMPETITIONS

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
IMG_DIR = os.path.join(STATIC_DIR, "img")
TEAM_LOGOS_DIR = os.path.join(IMG_DIR, "team-logos", "_imported")
LEAGUE_LOGOS_DIR = os.path.join(IMG_DIR, "league-logos")
PLAYERS_DIR = os.path.join(IMG_DIR, "players")
PLAYERS_IMPORTED_DIR = os.path.join(PLAYERS_DIR, "_imported")
COACHES_DIR = os.path.join(IMG_DIR, "coaches")
COACHES_IMPORTED_DIR = os.path.join(COACHES_DIR, "_imported")
NEWS_DIR = os.path.join(IMG_DIR, "news")

for _d in (TEAM_LOGOS_DIR, LEAGUE_LOGOS_DIR, PLAYERS_DIR, PLAYERS_IMPORTED_DIR, COACHES_DIR, COACHES_IMPORTED_DIR, NEWS_DIR):
    os.makedirs(_d, exist_ok=True)

DEFAULT_TIMEOUT = 15
_import_status: dict[str, Any] = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_task": "",
    "log": [],
    "last_run": None,
    "errors": 0,
}
_status_lock = threading.Lock()


def get_status() -> dict:
    with _status_lock:
        return dict(_import_status)


def _update_status(**kwargs: Any) -> None:
    with _status_lock:
        _import_status.update(kwargs)


def _log(msg: str, level: str = "info") -> None:
    entry = {"msg": msg, "level": level, "ts": time.strftime("%H:%M:%S")}
    with _status_lock:
        _import_status["log"].append(entry)
        if len(_import_status["log"]) > 200:
            _import_status["log"] = _import_status["log"][-200:]
    print(f"[importer:{level}] {msg}", flush=True)


def _safe_filename(name: str) -> str:
    """Convert a name to a safe filename."""
    return "".join(c if c.isalnum() or c in " .-_()" else "_" for c in name).strip()


def download_image(url: str, dest_path: str, entity_type: str = "", entity_id: str = "") -> bool:
    """Download an image URL to dest_path. Returns True on success."""
    if not url:
        return False
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.exists(dest_path):
        db_client.register_image(url, dest_path, entity_type, entity_id)
        return True
    try:
        r = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": "DKSPORTS/2.0"})
        if r.status_code == 200 and len(r.content) > 100:
            with open(dest_path, "wb") as f:
                f.write(r.content)
            db_client.register_image(url, dest_path, entity_type, entity_id)
            return True
    except Exception as e:
        _log(f"Image download failed for {url}: {e}", "warn")
    return False


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:8]


def _local_logo_path(team_name: str, league_name: str, url: str) -> str:
    league_safe = _safe_filename(league_name)
    ext = os.path.splitext(url.split("?")[0])[1] or ".png"
    filename = _safe_filename(team_name) + ext
    return os.path.join(TEAM_LOGOS_DIR, league_safe, filename)


def _structured_people_dir(base_dir: str, league_name: str | None = None, team_name: str | None = None) -> str:
    if league_name and team_name:
        return os.path.join(base_dir, _safe_filename(league_name), _safe_filename(team_name))
    return base_dir


def _local_player_path(
    player_name: str,
    player_id: str | int,
    url: str,
    league_name: str | None = None,
    team_name: str | None = None,
) -> str:
    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
    filename = f"{player_id}_{_safe_filename(player_name)}{ext}"
    return os.path.join(_structured_people_dir(PLAYERS_IMPORTED_DIR, league_name, team_name), filename)


def _local_coach_path(
    coach_name: str,
    team_id: str | int,
    ext: str = ".jpg",
    league_name: str | None = None,
    team_name: str | None = None,
) -> str:
    filename = f"{team_id}_{_safe_filename(coach_name or 'coach')}{ext}"
    return os.path.join(_structured_people_dir(COACHES_IMPORTED_DIR, league_name, team_name), filename)


def _local_news_path(article_id: str, url: str) -> str:
    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
    filename = f"{_safe_filename(str(article_id))}{ext}"
    return os.path.join(NEWS_DIR, filename)


def download_player_photo(
    player_name: str,
    player_id: str | int,
    photo_url: str,
    league_name: str | None = None,
    team_name: str | None = None,
) -> str | None:
    """Download a player photo to static/img/players/_imported/<league>/<club>/."""
    if not photo_url:
        return None
    dest = _local_player_path(player_name, player_id, photo_url, league_name=league_name, team_name=team_name)
    ok = download_image(photo_url, dest, "player", str(player_id))
    return dest if ok else None


def download_coach_photo(
    coach_name: str,
    team_id: str | int,
    photo_url: str,
    league_name: str | None = None,
    team_name: str | None = None,
) -> str | None:
    """Download a coach photo to static/img/coaches/_imported/<league>/<club>/."""
    if not photo_url:
        return None
    ext = os.path.splitext(photo_url.split("?")[0])[1] or ".jpg"
    dest = _local_coach_path(coach_name, team_id, ext=ext, league_name=league_name, team_name=team_name)
    ok = download_image(photo_url, dest, "coach", str(team_id))
    return dest if ok else None


def ensure_coach_placeholder(
    coach_name: str,
    team_id: str | int,
    league_name: str | None = None,
    team_name: str | None = None,
) -> str:
    """Create a simple local SVG placeholder for coaches when no portrait exists."""
    dest = _local_coach_path(coach_name, team_id, ext=".svg", league_name=league_name, team_name=team_name)
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        initials = "".join(part[:1].upper() for part in (coach_name or "Coach").split()[:2]) or "C"
        label = html.escape(coach_name or "Coach")
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320">
<rect width="320" height="320" rx="32" fill="#dbeafe"/>
<circle cx="160" cy="126" r="56" fill="#93c5fd"/>
<text x="160" y="148" text-anchor="middle" font-family="Arial, sans-serif" font-size="36" font-weight="700" fill="#0f172a">{html.escape(initials)}</text>
<rect x="46" y="212" width="228" height="18" rx="9" fill="#bfdbfe"/>
<text x="160" y="270" text-anchor="middle" font-family="Arial, sans-serif" font-size="20" fill="#1e3a8a">{label}</text>
</svg>"""
        with open(dest, "w", encoding="utf-8") as f:
            f.write(svg)
    db_client.register_image(f"placeholder://coach/{team_id}", dest, "coach", str(team_id))
    return dest


def ensure_player_placeholder(
    player_name: str,
    player_id: str | int,
    league_name: str | None = None,
    team_name: str | None = None,
) -> str:
    """Create a simple local SVG placeholder for players when no portrait exists."""
    dest = _local_player_path(player_name, player_id, "placeholder.svg", league_name=league_name, team_name=team_name)
    dest = os.path.splitext(dest)[0] + ".svg"
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        initials = "".join(part[:1].upper() for part in (player_name or "Player").split()[:2]) or "P"
        label = html.escape(player_name or "Player")
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320">
<rect width="320" height="320" rx="32" fill="#ecfccb"/>
<circle cx="160" cy="126" r="56" fill="#bef264"/>
<text x="160" y="148" text-anchor="middle" font-family="Arial, sans-serif" font-size="36" font-weight="700" fill="#1a2e05">{html.escape(initials)}</text>
<rect x="46" y="212" width="228" height="18" rx="9" fill="#d9f99d"/>
<text x="160" y="270" text-anchor="middle" font-family="Arial, sans-serif" font-size="20" fill="#3f6212">{label}</text>
</svg>"""
        with open(dest, "w", encoding="utf-8") as f:
            f.write(svg)
    db_client.register_image(f"placeholder://player/{player_id}", dest, "player", str(player_id))
    return dest


def download_news_image(article_id: str, image_url: str) -> str | None:
    """Download a news article image to static/img/news/. Returns local path or None."""
    if not image_url:
        return None
    dest = _local_news_path(article_id, image_url)
    ok = download_image(image_url, dest, "news", article_id)
    return dest if ok else None


def cache_team_people_assets(team: dict[str, Any], league_name: str | None = None) -> dict[str, int]:
    """Download player photos and create a coach asset for offline use."""
    if not team or not isinstance(team, dict):
        return {"players": 0, "coaches": 0}

    team_name = team.get("name") or team.get("shortName") or "unknown-team"
    team_id = team.get("id")
    players_count = 0
    coaches_count = 0

    coach_name = (
        ((team.get("coach") or {}).get("name"))
        or ((team.get("_coach_meta") or {}).get("name"))
        or ""
    )
    coach_photo = team.get("_coach_photo") or ((team.get("_coach_meta") or {}).get("photo")) or ""
    if team_id and coach_name:
        local_coach = download_coach_photo(
            coach_name,
            team_id,
            coach_photo,
            league_name=league_name,
            team_name=team_name,
        ) if coach_photo else ensure_coach_placeholder(
            coach_name,
            team_id,
            league_name=league_name,
            team_name=team_name,
        )
        if local_coach:
            team["_coach_photo_local"] = local_coach
            coaches_count += 1

    for player in team.get("squad") or []:
        if not isinstance(player, dict):
            continue
        player_id = player.get("id")
        photo_url = player.get("_photo") or player.get("photo") or player.get("photo_url")
        if not player_id:
            continue
        local_player = None
        if photo_url:
            local_player = download_player_photo(
                player.get("name") or "player",
                player_id,
                photo_url,
                league_name=league_name,
                team_name=team_name,
            )
        if not local_player:
            local_player = ensure_player_placeholder(
                player.get("name") or "player",
                player_id,
                league_name=league_name,
                team_name=team_name,
            )
        if local_player:
            player["_photo_local"] = local_player
            players_count += 1

    return {"players": players_count, "coaches": coaches_count}


def local_path_to_static_url(path: str | None) -> str | None:
    """Convert an absolute local file path under static/ to a web-accessible URL fragment.

    Returns the relative path from static/ root (e.g. 'img/team-logos/...') so callers
    can build a Flask url_for('static', filename=...) URL.  Returns None if path is
    outside STATIC_DIR or is falsy.
    """
    if not path:
        return None
    try:
        rel = os.path.relpath(os.path.abspath(path), STATIC_DIR)
        if rel.startswith(".."):
            return None
        return rel.replace("\\", "/")
    except Exception:
        return None


def import_competition(code: str, delay: float = 1.0) -> dict:
    """Import one competition with its offline-friendly league archive."""
    result: dict[str, Any] = {"code": code, "teams": 0, "standings": 0, "scorers": 0, "matches": 0, "errors": 0}

    _log(f"Importing competition: {code}")
    _update_status(current_task=f"Syncing {code} archive")

    # --- Competition metadata ---
    try:
        comp_data = api_client.get_competition(code)
        db_client.upsert_competition(comp_data)
        season = ((comp_data.get("currentSeason") or {}).get("startDate") or "")[:4]
        _log(f"  Competition: {comp_data.get('name')} (season {season})")

        # Download league emblem
        emblem_url = comp_data.get("emblem")
        if emblem_url:
            safe_code = _safe_filename(code)
            ext = os.path.splitext(emblem_url.split("?")[0])[1] or ".png"
            dest = os.path.join(LEAGUE_LOGOS_DIR, f"{safe_code}{ext}")
            download_image(emblem_url, dest, "competition", code)
    except FootballDataError as e:
        _log(f"  Failed to get competition {code}: {e}", "error")
        result["errors"] += 1
        db_client.log_import("competition", code, 0, "error", str(e))
        return result

    time.sleep(delay)

    # --- Teams ---
    try:
        teams_data = api_client.get_competition_teams(code)
        teams = teams_data.get("teams") or []
        season = ((teams_data.get("season") or {}).get("startDate") or season or "")[:4]
        league_name = (teams_data.get("competition") or {}).get("name") or code

        for team in teams:
            db_client.upsert_team(team, code, season)

            crest_url = team.get("crest")
            if crest_url:
                dest = _local_logo_path(team.get("name", "unknown"), league_name, crest_url)
                ok = download_image(crest_url, dest, "team", str(team.get("id", "")))
                if ok:
                    db_client.upsert_team({**team, "_crest_local": dest}, code, season)

            result["teams"] += 1

        _log(f"  Teams imported: {result['teams']}")
        db_client.log_import("teams", code, result["teams"], "ok")
    except FootballDataError as e:
        _log(f"  Failed teams for {code}: {e}", "error")
        result["errors"] += 1
        db_client.log_import("teams", code, 0, "error", str(e))

    time.sleep(delay)

    # --- Standings ---
    try:
        standings_data = api_client.get_standings(code)
        for standings_group in (standings_data.get("standings") or []):
            for row in (standings_group.get("table") or []):
                db_client.upsert_standing(row, code, season)
                result["standings"] += 1
        _log(f"  Standings rows: {result['standings']}")
        db_client.log_import("standings", code, result["standings"], "ok")
    except FootballDataError as e:
        _log(f"  Failed standings for {code}: {e}", "warn")
        result["errors"] += 1
        db_client.log_import("standings", code, 0, "error", str(e))

    time.sleep(delay)

    # --- Scorers ---
    try:
        scorers_data = api_client.get_scorers(code, limit=20)
        for i, scorer in enumerate(scorers_data.get("scorers") or []):
            db_client.upsert_scorer(scorer, code, season, i + 1)
            result["scorers"] += 1
        _log(f"  Scorers: {result['scorers']}")
        db_client.log_import("scorers", code, result["scorers"], "ok")
    except FootballDataError as e:
        _log(f"  Failed scorers for {code}: {e}", "warn")
        result["errors"] += 1
        db_client.log_import("scorers", code, 0, "error", str(e))

    time.sleep(delay)

    # --- Matches (full competition schedule + archived results) ---
    try:
        matches_data = api_client.get_competition_matches(code)
        matches = matches_data.get("matches") or []
        matchdays = sorted({
            int(match.get("matchday"))
            for match in matches
            if isinstance(match, dict) and match.get("matchday") is not None
        })
        for match in matches:
            db_client.upsert_match(match, code, season)
            result["matches"] += 1
        if matchdays:
            _log(
                f"  Matches: {result['matches']} cached across matchdays "
                f"{matchdays[0]}-{matchdays[-1]}"
            )
        else:
            _log(f"  Matches: {result['matches']} cached")
        db_client.log_import("matches", code, result["matches"], "ok")
    except FootballDataError as e:
        _log(f"  Failed matches for {code}: {e}", "warn")
        result["errors"] += 1
        db_client.log_import("matches", code, 0, "error", str(e))

    return result


_import_thread: threading.Thread | None = None


def run_full_import(codes: list[str] | None = None, delay: float = 8.0) -> None:
    """Run a full import for all (or specified) competitions. Blocks."""
    db_client.init_db()
    target_codes = codes or [c["code"] for c in FREE_TIER_COMPETITIONS if c.get("code")]

    _update_status(
        running=True, progress=0, total=len(target_codes),
        log=[], errors=0, current_task="Starting import…"
    )
    _log(f"Starting full import for {len(target_codes)} competitions")

    total_errors = 0
    for i, code in enumerate(target_codes):
        with _status_lock:
            _import_status["progress"] = i
        result = import_competition(code, delay=delay)
        total_errors += result.get("errors", 0)
        time.sleep(delay)

    _update_status(
        running=False, progress=len(target_codes),
        errors=total_errors, current_task="Done",
        last_run=time.strftime("%Y-%m-%d %H:%M:%S")
    )
    _log(f"Import complete. Total errors: {total_errors}")


def start_import_in_background(codes: list[str] | None = None) -> bool:
    """Start import in a background thread. Returns False if already running."""
    global _import_thread
    with _status_lock:
        if _import_status["running"]:
            return False
    _import_thread = threading.Thread(target=run_full_import, args=(codes,), daemon=True)
    _import_thread.start()
    return True


def check_for_updates(code: str) -> dict:
    """Compare live API data with DB to find changes. Returns summary dict."""
    summary: dict[str, Any] = {"code": code, "changes": [], "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        live_standings = api_client.get_standings(code)
        db_teams = {r["team_id"]: r for r in _get_db_standings(code)}
        for group in (live_standings.get("standings") or []):
            for row in (group.get("table") or []):
                team = row.get("team") or {}
                tid = team.get("id")
                if not tid:
                    continue
                db_row = db_teams.get(tid)
                if not db_row:
                    summary["changes"].append({"type": "new_team", "team": team.get("name"), "position": row.get("position")})
                elif db_row.get("points") != row.get("points"):
                    summary["changes"].append({
                        "type": "points_changed", "team": team.get("name"),
                        "old_pts": db_row.get("points"), "new_pts": row.get("points"),
                        "old_pos": db_row.get("position"), "new_pos": row.get("position"),
                    })
    except Exception as e:
        summary["error"] = str(e)
    return summary


def _get_db_standings(code: str) -> list[dict]:
    try:
        import db_client as _db
        return _db.get_standings_simple(code)
    except Exception:
        return []
