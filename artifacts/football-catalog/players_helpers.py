"""Helpers for top-scorers and players pages.

Centralises the cross-league aggregation logic so the routes in app.py stay
readable. Every helper degrades gracefully on football-data.org rate-limit
errors and api_football auth issues — partial data is always better than a
crashed page for the catalog UX.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import api_client
import api_football
import db_client
from api_client import FootballDataError, FREE_TIER_COMPETITIONS


# Leagues we surface on the home top-scorers strip and on the /players page.
# Kept short to stay within the football-data.org free-tier rate limit
# (~10 requests/minute) on a cold cache.
TOP_SCORER_LEAGUE_CODES = ["PL", "PD", "SA", "BL1", "FL1"]
PLAYER_LEAGUE_CODES = ["PL", "PD", "SA", "BL1", "FL1", "DED", "PPL"]

# Position mapping (football-data.org returns mixed terminology across sources).
POSITION_GROUPS = {
    "Goalkeeper": "GK",
    "Defence": "DEF",
    "Defender": "DEF",
    "Centre-Back": "DEF",
    "Center-Back": "DEF",
    "Left-Back": "DEF",
    "Right-Back": "DEF",
    "Midfield": "MID",
    "Midfielder": "MID",
    "Defensive Midfield": "MID",
    "Central Midfield": "MID",
    "Attacking Midfield": "MID",
    "Left Midfield": "MID",
    "Right Midfield": "MID",
    "Offence": "FWD",
    "Attacker": "FWD",
    "Forward": "FWD",
    "Centre-Forward": "FWD",
    "Center-Forward": "FWD",
    "Left Winger": "FWD",
    "Right Winger": "FWD",
    "Striker": "FWD",
    "Second Striker": "FWD",
}

POSITION_FILTERS = [
    {"key": "ALL", "label": "All positions"},
    {"key": "GK", "label": "Goalkeepers"},
    {"key": "DEF", "label": "Defenders"},
    {"key": "MID", "label": "Midfielders"},
    {"key": "FWD", "label": "Forwards"},
]


def _league_meta(code: str) -> dict[str, Any]:
    for c in FREE_TIER_COMPETITIONS:
        if c["code"] == code:
            return c
    return {"code": code, "name": code, "country": "", "emblem": ""}


def position_group(raw: str | None) -> str:
    if not raw:
        return "OTHER"
    return POSITION_GROUPS.get(raw.strip(), "OTHER")


# ---------- Top scorers ----------

def _attach_scorer_photo(scorer: dict[str, Any]) -> None:
    """Best-effort photo enrichment for a single scorer dict.

    Looks up the apifootball team blob by team name, then matches the player
    by normalized name. Silent on any failure.
    """
    try:
        if scorer.get("_photo"):
            return
        team = scorer.get("team") or {}
        player = scorer.get("player") or {}
        team_name = team.get("name") or team.get("shortName")
        if not team_name or not player.get("name"):
            return
        api_id = api_football.find_team_id(team_name, (team.get("area") or {}).get("name"))
        if not api_id:
            return
        photos = api_football.get_squad_photo_map(api_id)
        if not photos:
            return
        norm = api_football._normalize_name(player.get("name") or "")
        if norm in photos:
            scorer["_photo"] = photos[norm]
            return
        last = norm.split(" ")[-1]
        for k, v in photos.items():
            if last and last in k.split(" "):
                scorer["_photo"] = v
                return
    except Exception:
        return


_scorers_league_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_SCORERS_LEAGUE_TTL = 60 * 60 * 6  # 6h — top scorers change slowly


def get_top_scorers_by_league(limit_per_league: int = 5) -> list[dict[str, Any]]:
    """Return ``[{league: {...}, scorers: [...]}, ...]`` for the top leagues.

    Each scorer dict is augmented with ``_photo`` (best-effort) and ``_pos``
    (normalized position group). Always returns a list — leagues that error
    out are simply skipped, and successful per-league results are cached for
    6 hours so transient rate-limit failures don't blank the home page.
    """
    out: list[dict[str, Any]] = []
    now = time.time()
    for code in TOP_SCORER_LEAGUE_CODES:
        cached = _scorers_league_cache.get(code)
        if cached and (now - cached[0]) < _SCORERS_LEAGUE_TTL:
            out.append(cached[1])
            continue
        try:
            data = api_client.get_scorers(code, limit=limit_per_league)
        except FootballDataError:
            continue
        scorers = (data or {}).get("scorers") or []
        if not scorers:
            continue
        scorers = scorers[:limit_per_league]
        for s in scorers:
            s["_pos"] = position_group(((s.get("player") or {}).get("position")) or "")
            _attach_scorer_photo(s)
        group = {"league": _league_meta(code), "scorers": scorers}
        _scorers_league_cache[code] = (now, group)
        out.append(group)
    return out


# ---------- Single-leader leaderboards (one row, one entry per league) ----------

def _scorers_for(code: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the cached scorers list for a league, fetching once if needed."""
    now = time.time()
    cached = _scorers_league_cache.get(code)
    if cached and (now - cached[0]) < _SCORERS_LEAGUE_TTL:
        return cached[1].get("scorers", [])
    try:
        data = api_client.get_scorers(code, limit=limit)
    except FootballDataError:
        return []
    scorers = (data or {}).get("scorers") or []
    if not scorers:
        return []
    for s in scorers:
        s["_pos"] = position_group(((s.get("player") or {}).get("position")) or "")
        _attach_scorer_photo(s)
    group = {"league": _league_meta(code), "scorers": scorers}
    _scorers_league_cache[code] = (now, group)
    return scorers


def _entry_from_scorer(scorer: dict[str, Any], code: str, value_key: str) -> dict[str, Any]:
    player = scorer.get("player") or {}
    team = scorer.get("team") or {}
    raw_value = scorer.get(value_key)
    return {
        "league": _league_meta(code),
        "player": {
            "id": player.get("id"),
            "name": player.get("name"),
            "position": player.get("position") or "",
        },
        "team": {
            "id": team.get("id"),
            "name": team.get("name"),
            "shortName": team.get("shortName") or team.get("name"),
            "crest": team.get("crest"),
        },
        "photo": scorer.get("_photo") or "",
        "value": raw_value if raw_value is not None else 0,
        "extra": {
            "goals": scorer.get("goals") or 0,
            "assists": scorer.get("assists") or 0,
            "penalties": scorer.get("penalties") or 0,
            "playedMatches": scorer.get("playedMatches") or 0,
        },
    }


def top_scorers_one_per_league() -> list[dict[str, Any]]:
    """Return one card per league with the league's top scorer (sorted by goals)."""
    out: list[dict[str, Any]] = []
    for code in TOP_SCORER_LEAGUE_CODES:
        scorers = _scorers_for(code, limit=10)
        if not scorers:
            continue
        ranked = sorted(scorers, key=lambda s: -(s.get("goals") or 0))
        if ranked and (ranked[0].get("goals") or 0) > 0:
            out.append(_entry_from_scorer(ranked[0], code, "goals"))
    return out


def top_assists_one_per_league() -> list[dict[str, Any]]:
    """Return one card per league with the league's top assister.

    Reuses the football-data scorers payload (which carries an ``assists``
    field per row) so this is essentially free in terms of API calls.
    """
    out: list[dict[str, Any]] = []
    for code in TOP_SCORER_LEAGUE_CODES:
        scorers = _scorers_for(code, limit=20)
        if not scorers:
            continue
        ranked = sorted(scorers, key=lambda s: -((s.get("assists") or 0)))
        if ranked and (ranked[0].get("assists") or 0) > 0:
            out.append(_entry_from_scorer(ranked[0], code, "assists"))
    return out


def top_cards_one_per_league() -> list[dict[str, Any]]:
    """Return one card per league with the most-booked player.

    Sourced from apifootball.com which exposes a dedicated ``get_topcards``
    action keyed on ``league_id``. Silently skips leagues where the action
    fails or returns nothing.
    """
    out: list[dict[str, Any]] = []
    for code in TOP_SCORER_LEAGUE_CODES:
        lid = api_football.league_id_for_competition(code)
        if not lid:
            continue
        rows = api_football.get_top_cards_for_league(lid, limit=5)
        if not rows:
            continue
        top = rows[0]
        out.append({
            "league": _league_meta(code),
            "player": {"id": None, "name": top.get("name") or "Unknown"},
            "team": {
                "id": None,
                "name": top.get("team_name") or "",
                "shortName": top.get("team_name") or "",
                "crest": "",
            },
            "photo": top.get("photo") or "",
            "value": top.get("total") or 0,
            "extra": {
                "yellow": top.get("yellow") or 0,
                "red": top.get("red") or 0,
            },
        })
    return out


def assists_table_for_league(code: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the league scorers list re-sorted by assists, top ``limit``."""
    scorers = _scorers_for(code, limit=20)
    ranked = sorted(scorers, key=lambda s: (-(s.get("assists") or 0), -(s.get("goals") or 0)))
    return [s for s in ranked if (s.get("assists") or 0) > 0][:limit]


def cards_table_for_league(code: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the apifootball.com top-cards table for the given football-data code."""
    lid = api_football.league_id_for_competition(code)
    if not lid:
        return []
    return api_football.get_top_cards_for_league(lid, limit=limit)


# ---------- Players page ----------

class _PlayerCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: int) -> Any | None:
        with self._lock:
            v = self._data.get(key)
            if not v:
                return None
            ts, val = v
            if time.time() - ts > ttl:
                self._data.pop(key, None)
                return None
            return val

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)


_player_cache = _PlayerCache()
PLAYER_CACHE_TTL = 60 * 30  # 30 min


def get_players_for_league(code: str, with_photos: bool = True) -> list[dict[str, Any]]:
    """Flatten every team's squad in the given competition into player rows.

    Each row: {id, name, position, _pos, dateOfBirth, nationality, shirtNumber,
    team: {id, name, shortName, crest}, league: {code, name, emblem}, _photo}.

    Cached aggressively because building the photo map for a whole league is
    expensive on a cold cache.
    """
    code = (code or "").upper()
    if not code:
        return []
    cache_key = f"players:{code}:{int(with_photos)}"
    cached = _player_cache.get(cache_key, PLAYER_CACHE_TTL)
    if cached is not None:
        return cached

    try:
        data = api_client.get_competition_teams(code)
    except FootballDataError:
        _player_cache.set(cache_key, [])
        return []

    league = _league_meta(code)
    rows: list[dict[str, Any]] = []
    for team in (data.get("teams") or []):
        squad = team.get("squad") or []
        if not squad:
            continue
        team_meta = {
            "id": team.get("id"),
            "name": team.get("name"),
            "shortName": team.get("shortName") or team.get("name"),
            "crest": team.get("crest"),
        }
        # Photo enrichment per team — silent failure.
        if with_photos:
            try:
                api_football.attach_player_photos(team)
            except Exception:
                pass
        for p in squad:
            if not isinstance(p, dict):
                continue
            rows.append({
                "id": p.get("id"),
                "name": p.get("name"),
                "position": p.get("position") or "",
                "_pos": position_group(p.get("position")),
                "dateOfBirth": p.get("dateOfBirth"),
                "nationality": p.get("nationality"),
                "shirtNumber": p.get("shirtNumber"),
                "team": team_meta,
                "league": {
                    "code": code,
                    "name": league.get("name"),
                    "emblem": league.get("emblem"),
                },
                "_photo": p.get("_photo") or "",
            })

    photo_locals = db_client.get_player_photo_locals([
        r["id"] for r in rows if isinstance(r.get("id"), int)
    ])
    for row in rows:
        row["_photo_local"] = photo_locals.get(row.get("id"), "")

    rows.sort(key=lambda r: ((r["team"].get("name") or "").lower(), (r.get("name") or "").lower()))
    _player_cache.set(cache_key, rows)
    return rows


# ---------- Search index ----------

_search_cache = _PlayerCache()
SEARCH_CACHE_TTL = 60 * 30  # 30 min


def _league_search_chunk(code: str) -> dict[str, list[dict[str, Any]]]:
    """Return ``{teams, players}`` for a single league, cached per-league.

    Caching per-league means a rate-limit failure on one league does not
    poison the entire search index — successful leagues still return data.
    """
    cache_key = f"search_chunk:{code}"
    cached = _search_cache.get(cache_key, SEARCH_CACHE_TTL)
    if cached is not None:
        return cached
    try:
        data = api_client.get_competition_teams(code)
    except FootballDataError:
        # Don't cache failures — retry on next request.
        return {"teams": [], "players": []}
    league_meta = _league_meta(code)
    teams: list[dict[str, Any]] = []
    players: list[dict[str, Any]] = []
    for t in (data.get("teams") or []):
        tid = t.get("id")
        team_meta = {
            "id": tid,
            "name": t.get("name"),
            "shortName": t.get("shortName") or t.get("name"),
            "tla": t.get("tla"),
            "crest": t.get("crest"),
            "league": {"code": code, "name": league_meta.get("name")},
        }
        if isinstance(tid, int):
            teams.append(team_meta)
        for p in (t.get("squad") or []):
            pid = p.get("id")
            if not isinstance(pid, int):
                continue
            players.append({
                "id": pid,
                "name": p.get("name"),
                "position": p.get("position") or "",
                "team": {
                    "id": tid,
                    "name": t.get("name"),
                    "shortName": t.get("shortName") or t.get("name"),
                    "crest": t.get("crest"),
                },
                "league": {"code": code, "name": league_meta.get("name")},
            })
    chunk = {"teams": teams, "players": players}
    # Only cache if we actually got data; empty results retry next call.
    if teams or players:
        _search_cache.set(cache_key, chunk)
    return chunk


def _competitions_chunk() -> list[dict[str, Any]]:
    cached = _search_cache.get("comps_chunk", SEARCH_CACHE_TTL)
    if cached is not None:
        return cached
    try:
        all_comps = api_client.list_competitions()
    except FootballDataError:
        return []
    free_codes = {c["code"] for c in FREE_TIER_COMPETITIONS}
    out = [{
        "code": c.get("code"),
        "name": c.get("name"),
        "country": (c.get("area") or {}).get("name"),
        "emblem": c.get("emblem"),
        "free": c.get("code") in free_codes,
    } for c in all_comps]
    if out:
        _search_cache.set("comps_chunk", out)
    return out


def build_search_index() -> dict[str, list[dict[str, Any]]]:
    """Aggregate competitions + per-league chunks into a single index dict."""
    competitions = _competitions_chunk()
    teams: list[dict[str, Any]] = []
    players: list[dict[str, Any]] = []
    seen_team_ids: set[int] = set()
    seen_player_ids: set[int] = set()
    for code in PLAYER_LEAGUE_CODES:
        chunk = _league_search_chunk(code)
        for t in chunk["teams"]:
            tid = t.get("id")
            if isinstance(tid, int) and tid not in seen_team_ids:
                seen_team_ids.add(tid)
                teams.append(t)
        for p in chunk["players"]:
            pid = p.get("id")
            if isinstance(pid, int) and pid not in seen_player_ids:
                seen_player_ids.add(pid)
                players.append(p)
    return {"competitions": competitions, "teams": teams, "players": players}


def search_index(query: str, limit_per_kind: int = 6) -> dict[str, list[dict[str, Any]]]:
    """Run a case-insensitive substring search across the cached index."""
    q = (query or "").strip().lower()
    if not q or len(q) < 2:
        return {"competitions": [], "teams": [], "players": []}
    idx = build_search_index()
    out_c: list[dict[str, Any]] = []
    out_t: list[dict[str, Any]] = []
    out_p: list[dict[str, Any]] = []

    for c in idx["competitions"]:
        if len(out_c) >= limit_per_kind:
            break
        hay = f"{c.get('name') or ''} {c.get('code') or ''} {c.get('country') or ''}".lower()
        if q in hay:
            out_c.append(c)

    for t in idx["teams"]:
        if len(out_t) >= limit_per_kind:
            break
        hay = f"{t.get('name') or ''} {t.get('shortName') or ''} {t.get('tla') or ''}".lower()
        if q in hay:
            out_t.append(t)

    for p in idx["players"]:
        if len(out_p) >= limit_per_kind:
            break
        if q in (p.get("name") or "").lower():
            out_p.append(p)

    return {"competitions": out_c, "teams": out_t, "players": out_p}
