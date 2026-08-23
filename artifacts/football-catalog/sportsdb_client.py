"""Client for TheSportsDB free v1 API.

Used as a more reliable source for player portraits and richer player metadata
than the current apifootball integration.
"""
from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from typing import Any
from urllib.parse import quote

import requests

API_KEY = os.environ.get("THESPORTSDB_API_KEY", "123").strip() or "123"
API_BASE = f"https://www.thesportsdb.com/api/v1/json/{API_KEY}"
DEFAULT_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
TEAM_PLAYER_MATCH_MIN_SCORE = 78
SEARCH_PLAYER_MATCH_MIN_SCORE = 82

COMPETITION_TO_LEAGUE_NAMES: dict[str, list[str]] = {
    "PL": ["English Premier League", "Premier League"],
    "PD": ["Spanish La Liga", "La Liga", "Primera Division"],
    "SA": ["Italian Serie A", "Serie A"],
    "BL1": ["German Bundesliga", "Bundesliga"],
    "FL1": ["French Ligue 1", "Ligue 1"],
    "CL": ["UEFA Champions League"],
    "DED": ["Dutch Eredivisie", "Eredivisie"],
    "PPL": ["Portuguese Primeira Liga", "Primeira Liga"],
    "BSA": ["Brazilian Serie A", "Campeonato Brasileiro Série A"],
    "ELC": ["English League Championship", "Championship"],
    "WC": ["FIFA World Cup"],
    "EC": ["UEFA European Championship", "European Championship", "UEFA Euro"],
}

COMPETITION_TO_COUNTRY: dict[str, str] = {
    "PL": "England",
    "PD": "Spain",
    "SA": "Italy",
    "BL1": "Germany",
    "FL1": "France",
    "DED": "Netherlands",
    "PPL": "Portugal",
    "BSA": "Brazil",
    "ELC": "England",
}

PLAYER_TTL = 60 * 60 * 12
TEAM_TTL = 60 * 60 * 24


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: int) -> Any | None:
        with self._lock:
            value = self._data.get(key)
            if not value:
                return None
            ts, payload = value
            if time.time() - ts > ttl:
                self._data.pop(key, None)
                return None
            return payload

    def set(self, key: str, payload: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), payload)


_cache = _Cache()


def has_sportsdb_api() -> bool:
    return bool(API_KEY)


def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\b(fc|cf|club|de|do|the|afc|sc|ac|fk|sk|sv|ss|ssc)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _request(endpoint: str, ttl: int = TEAM_TTL) -> dict[str, Any]:
    cached = _cache.get(endpoint, ttl)
    if cached is not None:
        return cached
    url = f"{API_BASE}/{endpoint}"
    last_error: Exception | None = None
    data: dict[str, Any] = {}
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": "DKSPORTS/2.0"})
            if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json() or {}
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= MAX_RETRIES - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    if last_error and not data:
        raise last_error
    _cache.set(endpoint, data)
    return data


def _best_team_match(team_name: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    match, _score = _best_team_match_with_score([team_name], items)
    return match


def _score_team_match(target_name: str, item: dict[str, Any]) -> int:
    target = _normalize_name(target_name)
    if not target:
        return 0
    target_tokens = set(target.split())
    names = [
        item.get("strTeam"),
        item.get("strTeamAlternate"),
        item.get("strTeamShort"),
        item.get("strLeague"),
    ]
    best = 0
    for cand in names:
        norm = _normalize_name(cand)
        if not norm:
            continue
        score = 0
        if norm == target:
            score += 100
        elif target in norm or norm in target:
            score += 70
        overlap = len(target_tokens & set(norm.split()))
        score += overlap * 8
        best = max(best, score)
    return best


def _best_team_match_with_score(
    team_names: list[str],
    items: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, int]:
    names = [name for name in team_names if name]
    if not names:
        return None, 0
    best_item: dict[str, Any] | None = None
    best_score = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        score = max((_score_team_match(name, item) for name in names), default=0)
        if score > best_score:
            best_item = item
            best_score = score
    return best_item, best_score


def _best_player_match(player_name: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    match, _score = _best_player_match_with_score(player_name, items)
    return match


def _score_team_affinity(team_name: str | None, item: dict[str, Any]) -> int:
    target = _normalize_name(team_name)
    if not target:
        return 0
    target_tokens = set(target.split())
    best = 0
    for cand in (
        item.get("strTeam"),
        item.get("strTeam2"),
        item.get("strTeamNational"),
    ):
        norm = _normalize_name(cand)
        if not norm:
            continue
        score = 0
        if norm == target:
            score += 35
        elif target in norm or norm in target:
            score += 24
        score += len(target_tokens & set(norm.split())) * 5
        best = max(best, score)
    return best


def _position_affinity(position: str | None, item: dict[str, Any]) -> int:
    target = _normalize_name(position)
    cand = _normalize_name(item.get("strPosition"))
    if not target or not cand:
        return 0
    if target == cand:
        return 10
    if target in cand or cand in target:
        return 6
    return 0


def _shirt_affinity(shirt_number: Any, item: dict[str, Any]) -> int:
    if shirt_number in (None, "", 0):
        return 0
    try:
        target = int(shirt_number)
        cand = int(item.get("strNumber") or 0)
    except (TypeError, ValueError):
        return 0
    return 8 if target == cand else 0


def _best_player_match_with_score(
    player_name: str,
    items: list[dict[str, Any]],
    *,
    team_name: str | None = None,
    position: str | None = None,
    shirt_number: Any = None,
) -> tuple[dict[str, Any] | None, int]:
    target = _normalize_name(player_name)
    if not target:
        return None, 0
    target_tokens = set(target.split())
    target_last = target.split()[-1] if target.split() else ""
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        names = [
            item.get("strPlayer"),
            item.get("strPlayerAlternate"),
        ]
        best = 0
        for cand in names:
            norm = _normalize_name(cand)
            if not norm:
                continue
            score = 0
            if norm == target:
                score += 100
            elif target in norm or norm in target:
                score += 70
            overlap = len(target_tokens & set(norm.split()))
            score += overlap * 7
            if target_last and target_last == norm.split()[-1]:
                score += 10
            score += _score_team_affinity(team_name, item)
            score += _position_affinity(position, item)
            score += _shirt_affinity(shirt_number, item)
            best = max(best, score)
        if best:
            scored.append((best, item))
    if not scored:
        return None, 0
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1], scored[0][0]


def list_teams_for_competition(code: str) -> list[dict[str, Any]]:
    code = (code or "").upper()
    names = COMPETITION_TO_LEAGUE_NAMES.get(code, [])
    seen_team_ids: set[str] = set()
    collected: list[dict[str, Any]] = []
    for league_name in names:
        try:
            endpoint = f"search_all_teams.php?l={quote(league_name.replace(' ', '_'))}"
            data = _request(endpoint, ttl=TEAM_TTL)
        except Exception:
            continue
        teams = data.get("teams") or []
        for team in teams:
            if not isinstance(team, dict):
                continue
            team_id = str(team.get("idTeam") or "")
            if team_id and team_id in seen_team_ids:
                continue
            if team_id:
                seen_team_ids.add(team_id)
            collected.append(team)
    country = COMPETITION_TO_COUNTRY.get(code)
    if country:
        try:
            endpoint = f"search_all_teams.php?s=Soccer&c={quote(country.replace(' ', '_'))}"
            data = _request(endpoint, ttl=TEAM_TTL)
            teams = data.get("teams") or []
            for team in teams:
                if not isinstance(team, dict):
                    continue
                team_id = str(team.get("idTeam") or "")
                if team_id and team_id in seen_team_ids:
                    continue
                if team_id:
                    seen_team_ids.add(team_id)
                collected.append(team)
        except Exception:
            pass
    return collected


def find_team(
    team_name: str,
    competition_code: str,
    alternate_names: list[str] | None = None,
) -> dict[str, Any] | None:
    teams = list_teams_for_competition(competition_code)
    match, score = _best_team_match_with_score(
        [team_name, *(alternate_names or [])],
        teams,
    )
    if score < 24:
        return None
    return match


def list_players_for_team(team_id: int | str) -> list[dict[str, Any]]:
    if not team_id:
        return []
    try:
        data = _request(f"lookup_all_players.php?id={team_id}", ttl=PLAYER_TTL)
    except Exception:
        return []
    return data.get("player") or []


def search_players(player_name: str) -> list[dict[str, Any]]:
    if not player_name:
        return []
    try:
        data = _request(f"searchplayers.php?p={quote(player_name.replace(' ', '_'))}", ttl=PLAYER_TTL)
    except Exception:
        return []
    return data.get("player") or []


def _preferred_player_image(player: dict[str, Any]) -> str:
    return (
        player.get("strThumb")
        or player.get("strCutout")
        or player.get("strRender")
        or player.get("strFanart1")
        or ""
    )


def _is_coach_row(player: dict[str, Any]) -> bool:
    position = (player.get("strPosition") or "").lower()
    status = (player.get("strStatus") or "").lower()
    role = (player.get("strRole") or "").lower()
    return any(word in position for word in ("coach", "manager")) or "coach" in status or "coach" in role


def profile_from_player(player: dict[str, Any]) -> dict[str, Any]:
    return {
        "photo": _preferred_player_image(player),
        "height": player.get("strHeight") or "",
        "weight": player.get("strWeight") or "",
        "age": player.get("dateBorn") or "",
        "country": player.get("strNationality") or "",
        "position": player.get("strPosition") or "",
        "market_value": player.get("strWage") or "",
        "shirt": player.get("strNumber") or "",
        "rating": "",
        "foot": player.get("strSide") or "",
        "raw": player,
    }


def attach_team_people(
    team: dict[str, Any],
    competition_code: str,
    *,
    search_budget: int = 0,
) -> dict[str, int]:
    """Attach real player/coach portraits and rich raw player metadata."""
    if not team or not isinstance(team, dict):
        return {"team_found": 0, "source_players": 0, "search_attempts": 0, "search_hits": 0, "players": 0, "coaches": 0, "profiles": 0}

    search_names = [
        team.get("name") or "",
        team.get("shortName") or "",
        team.get("tla") or "",
    ]
    team_found = 0
    sportsdb_team = find_team(
        team.get("name") or team.get("shortName") or "",
        competition_code,
        alternate_names=search_names[1:],
    )
    players: list[dict[str, Any]] = []
    if sportsdb_team:
        team_found = 1
        team["sportsdb_team_id"] = sportsdb_team.get("idTeam")
        players = list_players_for_team(sportsdb_team.get("idTeam"))

    attached_players = 0
    attached_profiles = 0
    squad = team.get("squad") or []
    team_label = team.get("shortName") or team.get("name") or ""
    if players:
        for player in squad:
            if not isinstance(player, dict):
                continue
            match, _score = _best_player_match_with_score(
                player.get("name") or "",
                players,
                team_name=team_label,
                position=player.get("position"),
                shirt_number=player.get("shirtNumber"),
            )
            if not match or _score < TEAM_PLAYER_MATCH_MIN_SCORE:
                continue
            photo = _preferred_player_image(match)
            if photo:
                player["_photo"] = photo
                attached_players += 1
            player["_sportsdb_id"] = match.get("idPlayer")
            player["_sportsdb_profile"] = match
            attached_profiles += 1

    fallback_attempts = 0
    fallback_hits = 0
    if search_budget > 0:
        remaining_budget = search_budget
        for player in squad:
            if remaining_budget <= 0:
                break
            if not isinstance(player, dict) or player.get("_sportsdb_profile"):
                continue
            remaining_budget -= 1
            fallback_attempts += 1
            results = search_players(player.get("name") or "")
            match, score = _best_player_match_with_score(
                player.get("name") or "",
                results,
                team_name=team_label,
                position=player.get("position"),
                shirt_number=player.get("shirtNumber"),
            )
            if not match or score < SEARCH_PLAYER_MATCH_MIN_SCORE:
                continue
            photo = _preferred_player_image(match)
            if photo:
                player["_photo"] = photo
                attached_players += 1
            player["_sportsdb_id"] = match.get("idPlayer")
            player["_sportsdb_profile"] = match
            attached_profiles += 1
            fallback_hits += 1

    attached_coaches = 0
    coach_name = ((team.get("coach") or {}).get("name")) or ""
    coach_match, coach_score = _best_player_match_with_score(
        coach_name,
        [p for p in players if _is_coach_row(p)],
        team_name=team_label,
    ) if coach_name else (None, 0)
    if coach_match and coach_score < TEAM_PLAYER_MATCH_MIN_SCORE:
        coach_match = None
    if not coach_match:
        for candidate in players:
            if _is_coach_row(candidate):
                coach_match = candidate
                break
    if not coach_match and coach_name and search_budget > 0:
        coach_candidates = search_players(coach_name)
        coach_match, coach_score = _best_player_match_with_score(
            coach_name,
            [p for p in coach_candidates if _is_coach_row(p) or coach_name.lower() in (p.get("strPlayer") or "").lower()],
            team_name=team_label,
        )
        if coach_score < SEARCH_PLAYER_MATCH_MIN_SCORE:
            coach_match = None
    if coach_match:
        coach_photo = _preferred_player_image(coach_match)
        if coach_photo:
            team["_coach_photo"] = coach_photo
            attached_coaches = 1
        team["_coach_sportsdb"] = coach_match

    return {
        "team_found": team_found,
        "source_players": len(players),
        "search_attempts": fallback_attempts,
        "search_hits": fallback_hits,
        "players": attached_players,
        "coaches": attached_coaches,
        "profiles": attached_profiles,
    }
