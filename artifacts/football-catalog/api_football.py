"""apifootball.com client.

Used to enrich team and squad pages with coach and player photos that the
football-data.org free tier does not provide. Designed to fail gracefully when
the key is missing or invalid — every public function returns ``None`` or an
empty list rather than raising on a missing/invalid key.

This module talks to https://apifootball.com (NOT the api-football-v1 RapidAPI
service). Auth is via the ``APIkey`` query parameter, and every endpoint is
selected through the ``action`` query parameter.

The public interface (``find_team_id``, ``get_coach``, ``get_squad_photo_map``,
``attach_player_photos``, ``attach_coach_photo``, ``has_api_football``) is
unchanged so the rest of the app keeps working.
"""
from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from typing import Any

import requests

API_BASE = "https://apiv3.apifootball.com/"
API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
DEFAULT_TIMEOUT = 12

# Long TTLs because team/coach/squad data rarely change and the free tier is
# rate-limited.
TEAM_TTL = 60 * 60 * 24 * 7   # 1 week
SQUAD_TTL = 60 * 60 * 24      # 1 day

# After the first auth-failure response, flip this to False so that every
# subsequent call short-circuits without hitting the network.
_auth_ok: bool | None = None
_no_key = not bool(API_KEY)


class _Cache:
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


_cache = _Cache()


def has_api_football() -> bool:
    """True iff a key is configured AND the most recent call did not fail auth."""
    if _no_key:
        return False
    return _auth_ok is not False


def _request(action: str, params: dict[str, Any] | None = None) -> Any | None:
    """Hit apifootball.com and return the parsed JSON body, or None on failure.

    apifootball.com signals invalid key as ``{"error": 404, "message": "Authentification failed!"}``
    with HTTP 200 — we have to inspect the body to detect that case and flip
    ``_auth_ok`` to False so future calls skip the network.
    """
    global _auth_ok
    if _no_key or _auth_ok is False:
        return None
    q: dict[str, Any] = {"action": action, "APIkey": API_KEY}
    if params:
        q.update(params)
    try:
        r = requests.get(API_BASE, params=q, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code >= 400:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    # Auth failure / quota errors come back as a dict with "error" key.
    if isinstance(data, dict) and "error" in data:
        msg = (data.get("message") or "").lower()
        if "authentification" in msg or "auth" in msg or data.get("error") in (404, 403, 401):
            _auth_ok = False
        return None
    _auth_ok = True
    return data


def _normalize_name(name: str) -> str:
    """Lowercase, strip accents, collapse whitespace and drop common noise words."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"\b(fc|cf|club|de|do|the|afc)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _get_team_blob(team_id: int) -> dict[str, Any] | None:
    """Return the full apifootball team blob (incl. ``players`` and ``coaches``)."""
    if not team_id or not has_api_football():
        return None
    cache_key = f"team_blob:{team_id}"
    cached = _cache.get(cache_key, SQUAD_TTL)
    if cached is not None:
        return cached or None
    data = _request("get_teams", {"team_id": team_id})
    blob: dict[str, Any] | None = None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            blob = first
    _cache.set(cache_key, blob or {})
    return blob


def find_team_id(name: str, country: str | None = None) -> int | None:
    """Return the apifootball ``team_key`` for the given team name, or None."""
    if not name or not has_api_football():
        return None
    cache_key = f"team:{(name or '').lower()}|{(country or '').lower()}"
    cached = _cache.get(cache_key, TEAM_TTL)
    if cached is not None:
        # Cached `0` means "looked up and not found" — treat as None.
        return cached or None

    data = _request("get_teams", {"team_name": name[:40]})
    found_id: int | None = None
    if isinstance(data, list) and data:
        target = _normalize_name(name)
        scored: list[tuple[int, int]] = []
        for it in data:
            if not isinstance(it, dict):
                continue
            try:
                tid = int(it.get("team_key") or 0)
            except (TypeError, ValueError):
                tid = 0
            if not tid:
                continue
            tname = _normalize_name(it.get("team_name") or "")
            tcountry = (it.get("team_country") or "").lower()
            score = 0
            if tname == target:
                score += 100
            elif target and (target in tname or tname in target):
                score += 60
            if country and tcountry and country.lower() in tcountry:
                score += 20
            scored.append((score, tid))
        if scored:
            scored.sort(reverse=True)
            found_id = scored[0][1]
    _cache.set(cache_key, found_id or 0)
    return found_id


def get_coach(api_team_id: int) -> dict[str, Any] | None:
    """Return ``{name, photo, age, nationality}`` for the current coach, or None.

    apifootball.com does not provide coach photos, but it does return the
    coach's name, country and age inside the ``coaches`` array of the team
    blob. We surface what we have and leave ``photo`` empty so the UI can
    decide to show initials instead of a portrait.
    """
    if not api_team_id or not has_api_football():
        return None
    blob = _get_team_blob(api_team_id)
    if not blob:
        return None
    coaches = blob.get("coaches") or []
    if not coaches:
        return None
    chosen = coaches[0] if isinstance(coaches[0], dict) else None
    if not chosen:
        return None
    full_name = (chosen.get("coach_name") or "").strip()
    parts = full_name.split(" ", 1)
    return {
        "name": full_name,
        "firstname": parts[0] if parts else "",
        "lastname": parts[1] if len(parts) > 1 else "",
        "photo": "",  # not provided by apifootball.com
        "age": chosen.get("coach_age") or None,
        "nationality": chosen.get("coach_country") or "",
        "birth": "",
    }


def get_squad_photo_map(api_team_id: int) -> dict[str, str]:
    """Return ``{normalized_name: photo_url}`` for the team squad."""
    if not api_team_id or not has_api_football():
        return {}
    blob = _get_team_blob(api_team_id)
    if not blob:
        return {}
    out: dict[str, str] = {}
    for p in (blob.get("players") or []):
        if not isinstance(p, dict):
            continue
        name = p.get("player_name") or ""
        photo = p.get("player_image") or ""
        if name and photo:
            out[_normalize_name(name)] = photo
    return out


def attach_player_photos(team: dict[str, Any]) -> None:
    """Attach a ``_photo`` URL to each entry of ``team['squad']`` (mutates in place)."""
    if not team or not isinstance(team, dict):
        return
    squad = team.get("squad") or []
    if not squad:
        return
    api_id = find_team_id(team.get("name") or "", (team.get("area") or {}).get("name"))
    if not api_id:
        return
    photos = get_squad_photo_map(api_id)
    if not photos:
        return
    for p in squad:
        norm = _normalize_name(p.get("name") or "")
        if not norm:
            continue
        if norm in photos:
            p["_photo"] = photos[norm]
            continue
        # Fallback: surname-only match (e.g. "Mbappé" vs "Kylian Mbappe Lottin")
        last = norm.split(" ")[-1]
        for k, v in photos.items():
            if last and last in k.split(" "):
                p["_photo"] = v
                break


def attach_coach_photo(team: dict[str, Any]) -> None:
    """Attach ``_coach_photo`` and ``_coach_meta`` to the team dict (mutates).

    apifootball.com does not provide coach portraits, so ``_coach_photo``
    will be left unset; the UI is expected to fall back to initials. We still
    attach ``_coach_meta`` so the page can show name + nationality + age.
    """
    if not team or not isinstance(team, dict):
        return
    api_id = find_team_id(team.get("name") or "", (team.get("area") or {}).get("name"))
    if not api_id:
        return
    coach = get_coach(api_id)
    if not coach:
        return
    if coach.get("photo"):
        team["_coach_photo"] = coach["photo"]
    team["_coach_meta"] = coach


def get_squad_profiles(api_team_id: int) -> list[dict[str, Any]]:
    """Return best-effort player profiles for an entire squad from one team blob."""
    if not api_team_id or not has_api_football():
        return []
    blob = _get_team_blob(api_team_id)
    if not blob:
        return []
    out: list[dict[str, Any]] = []
    for p in (blob.get("players") or []):
        if not isinstance(p, dict):
            continue
        out.append({
            "player_name": p.get("player_name") or "",
            "photo": p.get("player_image") or "",
            "height": p.get("player_height") or "",
            "weight": p.get("player_weight") or "",
            "age": p.get("player_age") or "",
            "country": p.get("player_country") or "",
            "position": p.get("player_type") or "",
            "market_value": p.get("player_market_value") or "",
            "shirt": p.get("player_number") or "",
            "rating": p.get("player_rating") or "",
            "foot": p.get("player_foot") or "",
            "raw": p,
        })
    return out


# ---------------------------------------------------------------------------
# Extended endpoints used by the stats hub, head-to-head and player profile.
# Every call below is best-effort and returns an empty / None value on any
# error so the UI degrades gracefully when the free tier of apifootball.com
# does not expose a particular action.
# ---------------------------------------------------------------------------

# Mapping from football-data.org competition codes -> apifootball.com
# league_id values. Verified league IDs for the major European competitions.
APIFOOTBALL_LEAGUE_IDS: dict[str, int] = {
    "PL": 152,    # Premier League
    "PD": 302,    # La Liga
    "SA": 207,    # Serie A
    "BL1": 175,   # Bundesliga
    "FL1": 168,   # Ligue 1
    "CL": 3,      # UEFA Champions League
    "DED": 184,   # Eredivisie
    "PPL": 266,   # Primeira Liga
    "ELC": 153,   # Championship
}


def league_id_for_competition(code: str) -> int | None:
    """Return the apifootball.com league_id for a football-data competition."""
    return APIFOOTBALL_LEAGUE_IDS.get((code or "").upper())


def _player_photo_for_team(player_name: str, api_team_id: int) -> str:
    """Look up a player photo URL inside the given apifootball team blob."""
    if not player_name or not api_team_id:
        return ""
    photos = get_squad_photo_map(api_team_id)
    if not photos:
        return ""
    norm = _normalize_name(player_name)
    if norm in photos:
        return photos[norm]
    last = norm.split(" ")[-1] if norm else ""
    for k, v in photos.items():
        if last and last in k.split(" "):
            return v
    return ""


def get_top_cards_for_league(league_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Return ``[{name, team, yellow, red, total, photo}, ...]`` ordered by total cards.

    Tries the apifootball.com ``get_topcards`` action first; degrades to an
    empty list on any failure (the action may not be available for every
    league or every plan tier).
    """
    if not league_id or not has_api_football():
        return []
    cache_key = f"topcards:{league_id}"
    cached = _cache.get(cache_key, SQUAD_TTL)
    if cached is not None:
        return cached or []
    data = _request("get_topcards", {"league_id": league_id})
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for it in data:
            if not isinstance(it, dict):
                continue
            try:
                yellow = int(it.get("player_yellow_cards") or it.get("yellow") or 0)
            except (TypeError, ValueError):
                yellow = 0
            try:
                red = int(it.get("player_red_cards") or it.get("red") or 0)
            except (TypeError, ValueError):
                red = 0
            total = yellow + red
            if total <= 0:
                continue
            out.append({
                "name": it.get("player_name") or "",
                "team_name": it.get("team_name") or "",
                "yellow": yellow,
                "red": red,
                "total": total,
                "photo": it.get("player_image") or "",
            })
    out.sort(key=lambda r: (-r["total"], -r["red"], -r["yellow"]))
    out = out[:limit]
    _cache.set(cache_key, out)
    return out


def get_h2h(first_team: str, second_team: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return last few head-to-head matches between two teams (best effort).

    Each row: {date, home, away, score_home, score_away, winner_side}.
    Winner side is one of ``"H"``, ``"A"``, ``"D"`` (or empty when unknown).
    """
    if not first_team or not second_team or not has_api_football():
        return []
    cache_key = f"h2h:{first_team.lower()}|{second_team.lower()}"
    cached = _cache.get(cache_key, SQUAD_TTL)
    if cached is not None:
        return cached or []
    data = _request("get_H2H", {"firstTeam": first_team, "secondTeam": second_team})
    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        # The provider returns several arrays; the cross-team H2H is keyed
        # ``firstTeam_VS_secondTeam`` (or sometimes a generic ``H2H`` list).
        candidates = []
        for k, v in data.items():
            if isinstance(v, list) and ("vs" in k.lower() or "h2h" in k.lower()):
                candidates = v
                break
        if not candidates:
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "match_hometeam_name" in v[0]:
                    candidates = v
                    break
        for it in candidates[:limit]:
            if not isinstance(it, dict):
                continue
            home = it.get("match_hometeam_name") or ""
            away = it.get("match_awayteam_name") or ""
            try:
                sh = int(it.get("match_hometeam_score") or 0)
            except (TypeError, ValueError):
                sh = 0
            try:
                sa = int(it.get("match_awayteam_score") or 0)
            except (TypeError, ValueError):
                sa = 0
            winner = "H" if sh > sa else ("A" if sa > sh else "D")
            rows.append({
                "date": it.get("match_date") or "",
                "home": home,
                "away": away,
                "score_home": sh,
                "score_away": sa,
                "winner": winner,
            })
    _cache.set(cache_key, rows)
    return rows


def find_player_profile(player_name: str, team_name: str | None = None) -> dict[str, Any]:
    """Return ``{photo, height, age, market_value, country, position, ...}``.

    Falls back to the team blob lookup so it benefits from the same cache as
    ``attach_player_photos`` — keeps API quota usage low.
    """
    if not player_name or not has_api_football():
        return {}
    api_id = None
    if team_name:
        api_id = find_team_id(team_name)
    cache_key = f"player_profile:{player_name.lower()}|{(team_name or '').lower()}"
    cached = _cache.get(cache_key, SQUAD_TTL)
    if cached is not None:
        return cached or {}
    profile: dict[str, Any] = {}
    blob = _get_team_blob(api_id) if api_id else None
    if blob:
        norm = _normalize_name(player_name)
        for p in (blob.get("players") or []):
            if not isinstance(p, dict):
                continue
            cand = _normalize_name(p.get("player_name") or "")
            if cand == norm or (norm and cand and (norm in cand or cand in norm)):
                profile = {
                    "photo": p.get("player_image") or "",
                    "height": p.get("player_height") or "",
                    "weight": p.get("player_weight") or "",
                    "age": p.get("player_age") or "",
                    "country": p.get("player_country") or "",
                    "position": p.get("player_type") or "",
                    "market_value": p.get("player_market_value") or "",
                    "shirt": p.get("player_number") or "",
                    "rating": p.get("player_rating") or "",
                }
                break
    if not profile:
        # Fall back to a direct player search.
        data = _request("get_players", {"player_name": player_name[:40]})
        if isinstance(data, list):
            for it in data:
                if not isinstance(it, dict):
                    continue
                profile = {
                    "photo": it.get("player_image") or "",
                    "height": it.get("height") or "",
                    "weight": it.get("weight") or "",
                    "age": it.get("age") or "",
                    "country": it.get("player_country") or "",
                    "position": it.get("player_type") or "",
                    "market_value": it.get("player_market_value") or "",
                    "shirt": it.get("player_number") or "",
                    "rating": it.get("player_rating") or "",
                    "team": it.get("team_name") or "",
                }
                break
    _cache.set(cache_key, profile or {})
    return profile
