"""Client for football-data.org v4 API with simple in-memory TTL caching."""
from __future__ import annotations

import os
import time
import threading
from typing import Any, Optional

import requests

API_BASE = "https://api.football-data.org/v4"
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")

DEFAULT_TIMEOUT = 15


class _Cache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            expires, value = entry
            if expires < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl, value)


_cache = _Cache()


class FootballDataError(Exception):
    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


def _request(path: str, params: Optional[dict] = None, ttl: int = 120) -> dict:
    if not API_KEY:
        raise FootballDataError("FOOTBALL_DATA_API_KEY is not configured.", 500)

    cache_key = f"{path}?{sorted((params or {}).items())}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"{API_BASE}{path}"
    headers = {"X-Auth-Token": API_KEY}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise FootballDataError(f"Network error: {exc}", 502) from exc

    if resp.status_code == 429:
        raise FootballDataError(
            "Rate limit exceeded for football-data.org (free tier allows ~10 requests/minute). Please wait a moment and try again.",
            429,
        )
    if resp.status_code == 403:
        raise FootballDataError(
            "This resource is not available on the free football-data.org tier.",
            403,
        )
    if resp.status_code == 404:
        raise FootballDataError("Resource not found.", 404)
    if resp.status_code >= 400:
        try:
            data = resp.json()
            msg = data.get("message") or data.get("error") or resp.text
        except Exception:
            msg = resp.text
        raise FootballDataError(f"API error ({resp.status_code}): {msg}", resp.status_code)

    data = resp.json()
    _cache.set(cache_key, data, ttl)
    return data


# ---------- Competitions ----------

def list_competitions() -> list[dict]:
    data = _request("/competitions", ttl=3600)
    return data.get("competitions", [])


def get_competition(code_or_id: str) -> dict:
    return _request(f"/competitions/{code_or_id}", ttl=1800)


def get_standings(code_or_id: str) -> dict:
    # Standings change at most a few times per match-day; 30 min keeps
    # the home page snappy once warmed without going stale.
    return _request(f"/competitions/{code_or_id}/standings", ttl=1800)


def get_scorers(code_or_id: str, limit: int = 20) -> dict:
    return _request(f"/competitions/{code_or_id}/scorers", params={"limit": limit}, ttl=1800)


def get_competition_teams(code_or_id: str) -> dict:
    return _request(f"/competitions/{code_or_id}/teams", ttl=1800)


def get_competition_matches(
    code_or_id: str,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    matchday: Optional[int] = None,
) -> dict:
    from datetime import datetime as _dt, timezone as _tz
    params: dict = {}
    if status:
        params["status"] = status
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    if matchday is not None:
        params["matchday"] = matchday
    # A query for a past-only date window can be cached aggressively because
    # the scoreline never changes once a match is in the books.
    today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    is_historical = bool(date_to) and date_to < today
    ttl = 6 * 3600 if is_historical else 180
    return _request(
        f"/competitions/{code_or_id}/matches",
        params=params or None,
        ttl=ttl,
    )


# ---------- Teams ----------

def get_team(team_id: int | str) -> dict:
    return _request(f"/teams/{team_id}", ttl=1800)


def get_team_matches(team_id: int | str, status: Optional[str] = None, limit: int = 20) -> dict:
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    return _request(f"/teams/{team_id}/matches", params=params, ttl=300)


# ---------- Matches ----------

def get_match(match_id: int | str) -> dict:
    return _request(f"/matches/{match_id}", ttl=120)


def list_matches_today() -> dict:
    return _request("/matches", ttl=120)


# ---------- Persons (players) ----------

def get_person(person_id: int | str) -> dict:
    return _request(f"/persons/{person_id}", ttl=1800)


# ---------- Helpers ----------

# Football-data.org free tier supports a limited list of competitions.
# We surface these prominently and use them as defaults.
FREE_TIER_COMPETITIONS = [
    {"code": "PL", "name": "Premier League", "country": "England", "emblem": "https://crests.football-data.org/PL.png"},
    {"code": "PD", "name": "La Liga", "country": "Spain", "emblem": "https://crests.football-data.org/PD.png"},
    {"code": "SA", "name": "Serie A", "country": "Italy", "emblem": "https://crests.football-data.org/SA.png"},
    {"code": "BL1", "name": "Bundesliga", "country": "Germany", "emblem": "https://crests.football-data.org/BL1.png"},
    {"code": "FL1", "name": "Ligue 1", "country": "France", "emblem": "https://crests.football-data.org/FL1.png"},
    {"code": "CL", "name": "UEFA Champions League", "country": "Europe", "emblem": "https://crests.football-data.org/CL.png"},
    {"code": "EL", "name": "UEFA Europa League", "country": "Europe", "emblem": "https://crests.football-data.org/EL.png"},
    {"code": "ECL", "name": "UEFA Conference League", "country": "Europe", "emblem": "https://crests.football-data.org/ECL.png"},
    {"code": "DED", "name": "Eredivisie", "country": "Netherlands", "emblem": "https://crests.football-data.org/DED.png"},
    {"code": "PPL", "name": "Primeira Liga", "country": "Portugal", "emblem": "https://crests.football-data.org/PPL.png"},
    {"code": "BSA", "name": "Brasileirão", "country": "Brazil", "emblem": "https://crests.football-data.org/BSA.png"},
    {"code": "ELC", "name": "Championship", "country": "England", "emblem": "https://crests.football-data.org/ELC.png"},
    {"code": "WC", "name": "FIFA World Cup", "country": "International", "emblem": "https://crests.football-data.org/WC.png"},
    {"code": "EC", "name": "European Championship", "country": "Europe", "emblem": "https://crests.football-data.org/EC.png"},
]
