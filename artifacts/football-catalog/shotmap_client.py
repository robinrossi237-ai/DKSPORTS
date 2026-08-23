"""Client for footapi7 RapidAPI — shotmap and match stats endpoints."""
from __future__ import annotations

import difflib
import os
import re
import time
import threading
import unicodedata
from typing import Any
from urllib.parse import quote

import requests
import db_client

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_FOOTAPI7_KEY", "")
RAPIDAPI_HOST = "footapi7.p.rapidapi.com"
BASE_URL = f"https://{RAPIDAPI_HOST}/api"
DEFAULT_TIMEOUT = 10
PROVIDER = "footapi7"
DISCOVERY_TIMEOUT = 4

_cache: dict[str, tuple[float, int, Any]] = {}
_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes
DISCOVERY_TTL = 86400  # 24 hours
_discovery_blocked_until = 0.0

TEAM_SEARCH_PATHS = (
    "/team/search/{query}",
    "/search/teams/{query}",
    "/search/team/{query}",
    "/search/{query}",
)
PLAYER_SEARCH_PATHS = (
    "/player/search/{query}",
    "/search/players/{query}",
    "/search/player/{query}",
    "/search/{query}",
)
TEAM_ROSTER_PATHS = (
    "/team/{team_id}/players",
    "/team/{team_id}/squad",
    "/team/{team_id}/roster",
)
TEAM_EVENT_PATHS = (
    "/team/{team_id}/events/last/0",
    "/team/{team_id}/events/next/0",
    "/team/{team_id}/matches",
    "/team/{team_id}/form",
)


def has_shotmap_api() -> bool:
    return bool(RAPIDAPI_KEY)


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }


def _request_json(
    path_or_url: str,
    ttl: int = CACHE_TTL,
    cache_errors: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, Any | None]:
    url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}/{path_or_url.lstrip('/')}"
    with _lock:
        if url in _cache:
            ts, status, val = _cache[url]
            if time.time() - ts < ttl:
                return status, val
    try:
        r = requests.get(url, headers=_headers(), timeout=timeout)
        status = r.status_code
        data = r.json() if r.content else None
    except Exception:
        return 0, None
    if status == 200 or cache_errors:
        with _lock:
            _cache[url] = (time.time(), status, data)
    return status, data


def _cached_get(path_or_url: str, ttl: int = CACHE_TTL) -> Any | None:
    status, data = _request_json(path_or_url, ttl=ttl)
    return data if status == 200 else None


def _discovery_request(path_or_url: str) -> tuple[int, Any | None]:
    global _discovery_blocked_until
    now = time.time()
    if now < _discovery_blocked_until:
        return 0, None
    status, data = _request_json(
        path_or_url,
        ttl=DISCOVERY_TTL,
        cache_errors=True,
        timeout=DISCOVERY_TIMEOUT,
    )
    if status == 0:
        _discovery_blocked_until = time.time() + 300
    return status, data


def _norm_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    stop = {"fc", "cf", "sc", "club", "the", "de", "ac", "afc", "cfc"}
    return " ".join(part for part in value.split() if part and part not in stop)


def _score_name(target: str | None, *candidates: str | None) -> float:
    target_norm = _norm_text(target)
    if not target_norm:
        return 0.0
    best = 0.0
    for candidate in candidates:
        cand_norm = _norm_text(candidate)
        if not cand_norm:
            continue
        if cand_norm == target_norm:
            return 1.0
        if cand_norm in target_norm or target_norm in cand_norm:
            best = max(best, 0.92)
        ratio = difflib.SequenceMatcher(None, target_norm, cand_norm).ratio()
        best = max(best, ratio)
        target_tokens = set(target_norm.split())
        cand_tokens = set(cand_norm.split())
        if target_tokens and target_tokens.issubset(cand_tokens):
            best = max(best, 0.9)
    return best


def _walk_objects(node: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, dict):
        out.append(node)
        for value in node.values():
            out.extend(_walk_objects(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_objects(item))
    return out


def _candidate_names(obj: dict) -> list[str]:
    names: list[str] = []
    for key in ("name", "fullName", "shortName", "displayName", "slug"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    nested = obj.get("team")
    if isinstance(nested, dict):
        names.extend(_candidate_names(nested))
    return list(dict.fromkeys(names))


def _extract_search_candidates(raw: dict | list | None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for obj in _walk_objects(raw):
        obj_id = obj.get("id")
        names = _candidate_names(obj)
        if obj_id in (None, "") or not names:
            continue
        key = f"{obj_id}:{_norm_text(names[0])}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": str(obj_id), "name": names[0], "names": names, "raw": obj})
    return out


def _string_blob(obj: dict) -> str:
    parts: list[str] = []
    for key, value in obj.items():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            parts.extend(v for v in value.values() if isinstance(v, str))
    return " ".join(parts)


def _search_candidates(path_templates: tuple[str, ...], query: str) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    quoted = quote(query, safe="")
    for template in path_templates:
        status, data = _discovery_request(template.format(query=quoted))
        if status != 200 or data is None:
            continue
        candidates = _extract_search_candidates(data)
        if candidates:
            return candidates
    return []


def _day_key(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(float(value)))
        except Exception:
            return ""
    text = str(value)
    if text.isdigit():
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(float(text)))
        except Exception:
            return ""
    return text[:10]


def _probe_direct_team(external_id: int | str) -> bool:
    for path in (f"/team/{external_id}/image", f"/team/{external_id}/form"):
        status, _ = _discovery_request(path)
        if status == 200:
            return True
    return False


def _probe_direct_player(external_id: int | str) -> bool:
    for path in (f"/player/{external_id}/image",):
        status, _ = _discovery_request(path)
        if status == 200:
            return True
    return False


def _probe_direct_match(external_id: int | str) -> bool:
    for path in (
        f"/match/{external_id}/statistics",
        f"/match/{external_id}/graph",
        f"/match/{external_id}/lineups",
        f"/match/{external_id}/shotmap",
    ):
        status, _ = _discovery_request(path)
        if status == 200:
            return True
    return False


def _extract_event_candidates(raw: dict | list | None) -> list[dict]:
    events: list[dict] = []
    seen: set[str] = set()
    for obj in _walk_objects(raw):
        obj_id = obj.get("id")
        home = obj.get("homeTeam") or obj.get("home") or {}
        away = obj.get("awayTeam") or obj.get("away") or {}
        home_name = home.get("name") if isinstance(home, dict) else home if isinstance(home, str) else ""
        away_name = away.get("name") if isinstance(away, dict) else away if isinstance(away, str) else ""
        if obj_id in (None, "") or not (home_name and away_name):
            continue
        key = f"{obj_id}:{_norm_text(home_name)}:{_norm_text(away_name)}:{_day_key(obj.get('startTimestamp') or obj.get('utcDate') or obj.get('date'))}"
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "id": str(obj_id),
            "home_name": home_name,
            "away_name": away_name,
            "date": obj.get("startTimestamp") or obj.get("utcDate") or obj.get("date"),
            "raw": obj,
        })
    return events


def _extract_team_roster_candidates(raw: dict | list | None) -> list[dict]:
    players: list[dict] = []
    seen: set[str] = set()
    for obj in _walk_objects(raw):
        obj_id = obj.get("id")
        names = _candidate_names(obj)
        if obj_id in (None, "") or not names:
            continue
        if not any(key in obj for key in ("position", "player", "jerseyNumber", "shirtNumber", "nationality")):
            continue
        key = f"{obj_id}:{_norm_text(names[0])}"
        if key in seen:
            continue
        seen.add(key)
        players.append({"id": str(obj_id), "name": names[0], "names": names, "raw": obj})
    return players


def fetch_team_event_candidates(external_team_id: int | str) -> list[dict]:
    for template in TEAM_EVENT_PATHS:
        status, data = _discovery_request(template.format(team_id=external_team_id))
        if status != 200 or data is None:
            continue
        events = _extract_event_candidates(data)
        if events:
            return events
    return []


def fetch_team_player_candidates(external_team_id: int | str) -> list[dict]:
    for template in TEAM_ROSTER_PATHS:
        status, data = _discovery_request(template.format(team_id=external_team_id))
        if status != 200 or data is None:
            continue
        players = _extract_team_roster_candidates(data)
        if players:
            return players
    return []


def search_team_candidates(query: str) -> list[dict]:
    return _search_candidates(TEAM_SEARCH_PATHS, query)


def search_player_candidates(query: str) -> list[dict]:
    return _search_candidates(PLAYER_SEARCH_PATHS, query)


def resolve_team_mapping(team: dict, competition_code: str | None = None, force_refresh: bool = False) -> dict | None:
    internal_id = team.get("id")
    if internal_id in (None, ""):
        return None
    if not force_refresh:
        cached = db_client.get_provider_mapping(PROVIDER, "team", internal_id)
        if cached:
            return cached
    if _probe_direct_team(internal_id):
        db_client.upsert_provider_mapping(
            PROVIDER,
            "team",
            internal_id,
            internal_id,
            entity_name=team.get("name") or team.get("shortName"),
            competition_code=competition_code,
            confidence=0.78,
            source="direct-probe",
            raw_json={"probe": "direct-id"},
        )
        return db_client.get_provider_mapping(PROVIDER, "team", internal_id)

    best: dict | None = None
    best_score = 0.0
    for query in (team.get("name"), team.get("shortName"), team.get("tla")):
        for candidate in search_team_candidates(query or ""):
            score = max(
                _score_name(team.get("name"), *candidate["names"]),
                _score_name(team.get("shortName"), *candidate["names"]),
            )
            country = (team.get("area") or {}).get("name") or team.get("area_name")
            if country and _norm_text(country) and _norm_text(country) in _norm_text(_string_blob(candidate["raw"])):
                score = min(1.0, score + 0.05)
            if score > best_score:
                best = candidate
                best_score = score
    if best and best_score >= 0.84:
        db_client.upsert_provider_mapping(
            PROVIDER,
            "team",
            internal_id,
            best["id"],
            entity_name=best["name"],
            competition_code=competition_code,
            confidence=best_score,
            source="search",
            raw_json=best["raw"],
        )
        return db_client.get_provider_mapping(PROVIDER, "team", internal_id)
    return None


def resolve_player_mapping(
    player: dict,
    *,
    team_external_id: int | str | None = None,
    competition_code: str | None = None,
    force_refresh: bool = False,
) -> dict | None:
    internal_id = player.get("id")
    if internal_id in (None, ""):
        return None
    if not force_refresh:
        cached = db_client.get_provider_mapping(PROVIDER, "player", internal_id)
        if cached:
            return cached
    if _probe_direct_player(internal_id):
        db_client.upsert_provider_mapping(
            PROVIDER,
            "player",
            internal_id,
            internal_id,
            entity_name=player.get("name"),
            competition_code=competition_code,
            confidence=0.75,
            source="direct-probe",
            raw_json={"probe": "direct-id"},
        )
        return db_client.get_provider_mapping(PROVIDER, "player", internal_id)

    best: dict | None = None
    best_score = 0.0
    if team_external_id:
        for candidate in fetch_team_player_candidates(team_external_id):
            score = max(
                _score_name(player.get("name"), *candidate["names"]),
                _score_name(player.get("shortName"), *candidate["names"]),
            )
            if score > best_score:
                best = candidate
                best_score = score
    if best is None:
        for candidate in search_player_candidates(player.get("name") or ""):
            score = _score_name(player.get("name"), *candidate["names"])
            if score > best_score:
                best = candidate
                best_score = score
    if best and best_score >= 0.88:
        db_client.upsert_provider_mapping(
            PROVIDER,
            "player",
            internal_id,
            best["id"],
            entity_name=best["name"],
            competition_code=competition_code,
            confidence=best_score,
            source="team-roster" if team_external_id else "search",
            raw_json=best["raw"],
        )
        return db_client.get_provider_mapping(PROVIDER, "player", internal_id)
    return None


def resolve_match_mapping(
    match: dict,
    *,
    home_external_id: int | str | None = None,
    away_external_id: int | str | None = None,
    competition_code: str | None = None,
    force_refresh: bool = False,
) -> dict | None:
    internal_id = match.get("id")
    if internal_id in (None, ""):
        return None
    if not force_refresh:
        cached = db_client.get_provider_mapping(PROVIDER, "match", internal_id)
        if cached:
            return cached
    if _probe_direct_match(internal_id):
        db_client.upsert_provider_mapping(
            PROVIDER,
            "match",
            internal_id,
            internal_id,
            entity_name=f"{((match.get('homeTeam') or {}).get('name') or '')} vs {((match.get('awayTeam') or {}).get('name') or '')}".strip(),
            competition_code=competition_code,
            confidence=0.74,
            source="direct-probe",
            raw_json={"probe": "direct-id"},
        )
        return db_client.get_provider_mapping(PROVIDER, "match", internal_id)

    home_name = (match.get("homeTeam") or {}).get("name") or ""
    away_name = (match.get("awayTeam") or {}).get("name") or ""
    target_day = _day_key(match.get("utcDate"))
    event_candidates: list[dict] = []
    for external_team_id in (home_external_id, away_external_id):
        if not external_team_id:
            continue
        event_candidates = fetch_team_event_candidates(external_team_id)
        if event_candidates:
            break

    best: dict | None = None
    best_score = 0.0
    for candidate in event_candidates:
        score = 0.6 * _score_name(home_name, candidate.get("home_name")) + 0.4 * _score_name(away_name, candidate.get("away_name"))
        if target_day and target_day == _day_key(candidate.get("date")):
            score += 0.2
        score = min(score, 1.0)
        if score > best_score:
            best = candidate
            best_score = score
    if best and best_score >= 0.9:
        db_client.upsert_provider_mapping(
            PROVIDER,
            "match",
            internal_id,
            best["id"],
            entity_name=f"{best.get('home_name')} vs {best.get('away_name')}",
            competition_code=competition_code,
            confidence=best_score,
            source="team-events",
            raw_json=best["raw"],
        )
        return db_client.get_provider_mapping(PROVIDER, "match", internal_id)
    return None


def resolve_team_id(team: dict, competition_code: str | None = None, force_refresh: bool = False) -> str | None:
    mapping = resolve_team_mapping(team, competition_code=competition_code, force_refresh=force_refresh)
    return str(mapping["external_id"]) if mapping else None


def resolve_player_id(
    player: dict,
    *,
    team_external_id: int | str | None = None,
    competition_code: str | None = None,
    force_refresh: bool = False,
) -> str | None:
    mapping = resolve_player_mapping(
        player,
        team_external_id=team_external_id,
        competition_code=competition_code,
        force_refresh=force_refresh,
    )
    return str(mapping["external_id"]) if mapping else None


def resolve_match_id(
    match: dict,
    *,
    home_external_id: int | str | None = None,
    away_external_id: int | str | None = None,
    competition_code: str | None = None,
    force_refresh: bool = False,
) -> str | None:
    mapping = resolve_match_mapping(
        match,
        home_external_id=home_external_id,
        away_external_id=away_external_id,
        competition_code=competition_code,
        force_refresh=force_refresh,
    )
    return str(mapping["external_id"]) if mapping else None


def get_shotmap(match_id: int | str) -> dict | None:
    """Fetch shot map data for a match."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/match/{match_id}/shotmap")


def get_match_statistics(match_id: int | str) -> dict | None:
    """Fetch match statistics (possession, passes, etc.)."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/match/{match_id}/statistics")


def get_match_incidents(match_id: int | str) -> dict | None:
    """Fetch match incidents (goals, cards, substitutions)."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/match/{match_id}/incidents")


def get_match_lineups(match_id: int | str) -> dict | None:
    """Fetch match lineups."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/match/{match_id}/lineups")


def parse_match_incidents_to_events(raw: dict) -> dict:
    """Convert footapi7 incidents into the db_client match-event shape."""
    incidents = raw.get("incidents") or []
    goals: list[dict] = []
    bookings: list[dict] = []
    substitutions: list[dict] = []

    for inc in incidents:
        if not isinstance(inc, dict):
            continue
        incident_type = (inc.get("incidentType") or inc.get("type") or "").lower()
        minute = inc.get("time") or inc.get("minute") or 0
        team = inc.get("team") or {}
        player = inc.get("player") or {}

        if "goal" in incident_type:
            goals.append({
                "minute": minute,
                "team": team,
                "scorer": player,
                "assist": inc.get("assist") or inc.get("assist1") or {},
                "type": inc.get("incidentClass") or inc.get("detail") or incident_type,
            })
            continue

        if "card" in incident_type or incident_type in ("yellow", "red"):
            bookings.append({
                "minute": minute,
                "team": team,
                "player": player,
                "card": incident_type.upper().replace("-", "_"),
            })
            continue

        if "substitution" in incident_type or incident_type == "sub":
            substitutions.append({
                "minute": minute,
                "team": team,
                "playerOut": inc.get("playerOut") or player,
                "playerIn": inc.get("playerIn") or inc.get("substitute") or {},
            })

    return {
        "goals": goals,
        "bookings": bookings,
        "substitutions": substitutions,
    }


def parse_lineups(raw: dict) -> dict:
    """Normalize a footapi7 lineups payload into db_client's expected shape."""
    out = {"lineups": []}
    for side in ("home", "away"):
        block = raw.get(side) or {}
        if not isinstance(block, dict):
            continue

        team = block.get("team") or {}
        formation = block.get("formation")
        starters: list[dict] = []
        substitutes: list[dict] = []

        player_lists = []
        for key in ("players", "startXI", "starters", "lineup"):
            value = block.get(key)
            if isinstance(value, list):
                player_lists.extend(value)

        for entry in player_lists:
            if not isinstance(entry, dict):
                continue
            player = entry.get("player") if isinstance(entry.get("player"), dict) else entry
            normalized = {
                "id": player.get("id"),
                "name": player.get("name"),
                "shirtNumber": entry.get("shirtNumber") or player.get("shirtNumber") or player.get("jerseyNumber"),
                "position": entry.get("position") or player.get("position"),
            }
            if entry.get("substitute") or entry.get("isSubstitute") or entry.get("bench"):
                substitutes.append(normalized)
            else:
                starters.append(normalized)

        for key in ("substitutes", "bench"):
            value = block.get(key)
            if not isinstance(value, list):
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                player = entry.get("player") if isinstance(entry.get("player"), dict) else entry
                substitutes.append({
                    "id": player.get("id"),
                    "name": player.get("name"),
                    "shirtNumber": entry.get("shirtNumber") or player.get("shirtNumber") or player.get("jerseyNumber"),
                    "position": entry.get("position") or player.get("position"),
                })

        if starters or substitutes:
            out["lineups"].append({
                "team": team,
                "formation": formation,
                "startXI": starters,
                "substitutes": substitutes,
            })

    return out


def parse_shotmap(raw: dict) -> dict:
    """Parse raw shotmap API response into structured frontend-ready data."""
    shots_raw = raw.get("shotmap") or []
    home_shots: list[dict] = []
    away_shots: list[dict] = []

    for s in shots_raw:
        player_info = s.get("player") or {}
        draw = s.get("draw") or {}
        shot: dict[str, Any] = {
            "id": s.get("id"),
            "player": player_info.get("name", "Unknown"),
            "player_short": player_info.get("shortName", ""),
            "jersey": player_info.get("jerseyNumber", ""),
            "position": player_info.get("position", ""),
            "type": s.get("shotType", "miss"),
            "goal_type": s.get("goalType"),
            "situation": s.get("situation", "regular"),
            "body_part": s.get("bodyPart", "right-foot"),
            "goal_mouth": s.get("goalMouthLocation", ""),
            "time": s.get("time", 0),
            "added_time": s.get("addedTime"),
            "period_time": s.get("periodTimeSeconds"),
            "coords": s.get("playerCoordinates") or {},
            "goal_mouth_coords": s.get("goalMouthCoordinates") or {},
            "block_coords": s.get("blockCoordinates"),
            "draw_start": draw.get("start") or {},
            "draw_end": draw.get("end") or {},
            "draw_block": draw.get("block"),
            "draw_goal": draw.get("goal") or {},
            "is_home": s.get("isHome", True),
        }
        if shot["is_home"]:
            home_shots.append(shot)
        else:
            away_shots.append(shot)

    all_shots = home_shots + away_shots

    def _count(lst: list, key: str, val: str) -> int:
        return sum(1 for s in lst if s.get(key) == val)

    stats = {
        "total": len(all_shots),
        "home_total": len(home_shots),
        "away_total": len(away_shots),
        "goals": _count(all_shots, "type", "goal"),
        "home_goals": _count(home_shots, "type", "goal"),
        "away_goals": _count(away_shots, "type", "goal"),
        "saves": _count(all_shots, "type", "save"),
        "misses": _count(all_shots, "type", "miss"),
        "blocks": _count(all_shots, "type", "block"),
        "posts": _count(all_shots, "type", "post"),
        "right_foot": _count(all_shots, "body_part", "right-foot"),
        "left_foot": _count(all_shots, "body_part", "left-foot"),
        "headers": _count(all_shots, "body_part", "head"),
        "from_open_play": _count(all_shots, "situation", "regular") + _count(all_shots, "situation", "assisted") + _count(all_shots, "situation", "fast-break"),
        "from_set_piece": _count(all_shots, "situation", "set-piece") + _count(all_shots, "situation", "free-kick") + _count(all_shots, "situation", "corner"),
        "penalties": _count(all_shots, "situation", "penalty"),
    }

    return {
        "shots": all_shots,
        "home_shots": home_shots,
        "away_shots": away_shots,
        "stats": stats,
    }


def get_match_graph(match_id: int | str) -> dict | None:
    """Fetch match momentum/pressure graph data."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/match/{match_id}/graph", ttl=3600)


def get_player_image(player_id: int | str) -> dict | None:
    """Fetch player image data from footapi7."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/player/{player_id}/image", ttl=86400)


def get_team_image(team_id: int | str) -> dict | None:
    """Fetch team image/logo from footapi7."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/team/{team_id}/image", ttl=86400)


def get_team_transfers(team_id: int | str) -> dict | None:
    """Fetch team transfer data (arrivals and departures)."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/team/{team_id}/transfers", ttl=14400)


def get_player_season_stats(player_id: int | str, season_year: int | str) -> dict | None:
    """Fetch player season statistics for a specific year."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/player/{player_id}/statistics/season/{season_year}", ttl=14400)


def get_team_form(team_id: int | str) -> dict | None:
    """Fetch team recent form (last 5 matches)."""
    if not RAPIDAPI_KEY:
        return None
    return _cached_get(f"{BASE_URL}/team/{team_id}/form", ttl=3600)


def parse_match_graph(raw: dict) -> dict:
    """Parse match graph/momentum data into frontend-ready format."""
    graph_points = raw.get("graphPoints") or raw.get("points") or []
    momentum: list[dict] = []
    for pt in graph_points:
        momentum.append({
            "minute": pt.get("minute") or pt.get("time") or 0,
            "value": pt.get("value") or pt.get("homeValue") or 0,
            "away_value": pt.get("awayValue") or 0,
            "home": pt.get("homeValue") or pt.get("value") or 0,
            "away": pt.get("awayValue") or 0,
        })
    return {
        "momentum": momentum,
        "total_points": len(momentum),
        "raw": raw,
    }


def parse_team_transfers(raw: dict) -> list[dict]:
    """Parse team transfer raw data into structured list."""
    transfers: list[dict] = []
    for section in (raw.get("transferIn") or [], raw.get("transferOut") or []):
        for t in section:
            if not isinstance(t, dict):
                continue
            player = t.get("player") or {}
            from_team = t.get("from") or t.get("fromTeam") or {}
            to_team = t.get("to") or t.get("toTeam") or {}
            transfers.append({
                "player_name": player.get("name") or t.get("name") or t.get("playerName", ""),
                "player_image": player.get("photo") or player.get("image") or "",
                "transfer_type": "in" if section is raw.get("transferIn") else "out",
                "from_team": from_team.get("name") or from_team if isinstance(from_team, str) else "",
                "to_team": to_team.get("name") or to_team if isinstance(to_team, str) else "",
                "transfer_fee": t.get("fee") or t.get("transferFee") or "—",
                "transfer_date": t.get("date") or t.get("transferDate") or "",
            })
    return sorted(transfers, key=lambda x: x.get("transfer_date") or "", reverse=True)


def parse_player_season_stats(raw: dict) -> dict:
    """Parse player season stats API response."""
    stats = raw.get("statistics") or raw.get("stats") or {}
    if isinstance(stats, list):
        stats = stats[0] if stats else {}
    goals = stats.get("goals") or {}
    passes = stats.get("passes") or {}
    shots = stats.get("shots") or {}
    tackles = stats.get("tackles") or {}
    dribbles = stats.get("dribbles") or {}
    cards = stats.get("cards") or {}
    games = stats.get("games") or {}
    return {
        "team_name": (stats.get("team") or {}).get("name") or "",
        "goals": goals.get("total") or goals if isinstance(goals, int) else 0,
        "assists": stats.get("goals", {}).get("assists") or 0,
        "appearances": games.get("appearences") or games.get("appearances") or 0,
        "minutes_played": games.get("minutes") or 0,
        "rating": _safe_float(games.get("rating")),
        "yellow_cards": cards.get("yellow") or 0,
        "red_cards": cards.get("red") or 0,
        "shots_total": shots.get("total") or 0,
        "shots_on_target": shots.get("on") or 0,
        "dribbles_success": dribbles.get("success") or 0,
        "pass_accuracy": _safe_float(passes.get("accuracy")),
        "key_passes": passes.get("key") or 0,
        "tackles_total": tackles.get("total") or 0,
        "interceptions": tackles.get("interceptions") or 0,
    }


def _safe_float(val) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def parse_statistics(raw: dict) -> list[dict]:
    """Parse match statistics into a structured list of stat categories."""
    groups = raw.get("statistics") or []
    out: list[dict] = []
    for group in groups:
        period = group.get("period", "ALL")
        for stat in (group.get("groups") or []):
            category = stat.get("groupName", "")
            for item in (stat.get("statisticsItems") or []):
                out.append({
                    "period": period,
                    "category": category,
                    "name": item.get("name", ""),
                    "home": item.get("home"),
                    "away": item.get("away"),
                    "home_value": item.get("homeValue"),
                    "away_value": item.get("awayValue"),
                    "render_type": item.get("renderType"),
                    "key": item.get("key", ""),
                })
    return out
