"""Clients for extra RapidAPI feeds used for offline caching experiments."""
from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_TIMEOUT = 18
EPL1_BASE = "https://english-premiere-league1.p.rapidapi.com"
EPL1_HOST = "english-premiere-league1.p.rapidapi.com"
LALIGA2_BASE = "https://la-liga2.p.rapidapi.com"
LALIGA2_HOST = "la-liga2.p.rapidapi.com"
RAPIDAPI_ESPN_SOCCER_KEY = os.environ.get("RAPIDAPI_ESPN_SOCCER_KEY", "")


class RapidApiExtraError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def has_espn_soccer_api() -> bool:
    return bool(RAPIDAPI_ESPN_SOCCER_KEY)


def _headers(host: str) -> dict[str, str]:
    return {
        "x-rapidapi-host": host,
        "x-rapidapi-key": RAPIDAPI_ESPN_SOCCER_KEY,
        "Content-Type": "application/json",
    }


def _request(base_url: str, host: str, path: str, params: dict[str, Any] | None = None) -> Any:
    if not RAPIDAPI_ESPN_SOCCER_KEY:
        raise RapidApiExtraError("RAPIDAPI_ESPN_SOCCER_KEY is not configured.")
    url = f"{base_url}{path}"
    try:
        resp = requests.get(url, headers=_headers(host), params=params or {}, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise RapidApiExtraError(f"Network error: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        snippet = (resp.text or "")[:160]
        raise RapidApiExtraError(
            f"Bad response from {host}{path} (status {resp.status_code}): {snippet}",
            status_code=resp.status_code,
        )

    if resp.status_code >= 400:
        msg = data.get("message") if isinstance(data, dict) else None
        raise RapidApiExtraError(msg or f"HTTP {resp.status_code}", status_code=resp.status_code)
    return data


def fetch_epl_player_bio(player_id: int | str) -> dict[str, Any]:
    data = _request(EPL1_BASE, EPL1_HOST, "/player/bio", {"playerId": str(player_id)})
    return data if isinstance(data, dict) else {}


def fetch_la_liga2_matches_raw() -> Any:
    return _request(LALIGA2_BASE, LALIGA2_HOST, "/matches")
