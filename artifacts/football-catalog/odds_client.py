"""Client for The Odds API (https://the-odds-api.com) with simple TTL caching.

Used to enrich football fixtures with live moneyline (h2h) odds: 1, X, 2.
"""
from __future__ import annotations

import difflib
import os
import re
import threading
import time
import unicodedata
from typing import Any, Optional

import requests

ODDS_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

DEFAULT_TIMEOUT = 10

# Map football-data.org competition codes -> The Odds API sport keys.
COMPETITION_TO_SPORT = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "ECL": "soccer_uefa_europa_conference_league",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "BSA": "soccer_brazil_campeonato",
    "ELC": "soccer_efl_champ",
    "WC": "soccer_fifa_world_cup",
    "EC": "soccer_uefa_european_championship",
}

# Reverse — which sport_keys we know cover football
SOCCER_SPORT_KEYS = list(set(COMPETITION_TO_SPORT.values()))

CLUB_STOPWORDS = {
    "fc", "cf", "sc", "ac", "afc", "club", "calcio", "de", "do", "the",
    "fk", "sk", "sv", "ss", "ssc", "spa", "ev", "bv", "cfc",
}

PHRASE_ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "paris sg": "paris saint germain",
    "paris st germain": "paris saint germain",
    "ath madrid": "atletico madrid",
    "ath bilbao": "athletic bilbao",
}

TOKEN_ALIASES = {
    "utd": "united",
    "munchen": "munich",
    "muenchen": "munich",
    "internazionale": "inter",
    "milano": "milan",
    "st": "saint",
    "psg": "paris",
    "spurs": "tottenham",
    "wolves": "wolverhampton",
    "gladbach": "monchengladbach",
}


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


def _team_key(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&", " and ")
    for src, dst in PHRASE_ALIASES.items():
        s = re.sub(rf"\b{re.escape(src)}\b", dst, s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens: list[str] = []
    for raw_token in s.split():
        token = TOKEN_ALIASES.get(raw_token, raw_token)
        if not token or token in CLUB_STOPWORDS or token.isdigit():
            continue
        tokens.append(token)
    return " ".join(tokens)


def _norm(name: str | None) -> str:
    """Normalize team names so 'Bayern Munich' ~ 'FC Bayern München'."""
    if not name:
        return ""
    s = name.lower()
    s = (
        s.replace("ü", "u").replace("ö", "o").replace("ä", "a")
        .replace("é", "e").replace("è", "e").replace("ê", "e")
        .replace("á", "a").replace("à", "a").replace("â", "a")
        .replace("í", "i").replace("ó", "o").replace("ñ", "n")
        .replace("ç", "c")
    )
    # strip common club tokens
    for token in [" fc", "fc ", " cf", "cf ", " sc", "sc ", " ac ", " ssc ",
                  " bv", " bvb", "1.", "1. ", " 04 ", " 05 ", " 1899 ",
                  " e.v.", " ev ", " club", " calcio", " ssd ", " spa "]:
        s = s.replace(token, " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sport_for_competition(code: str | None) -> Optional[str]:
    if not code:
        return None
    return COMPETITION_TO_SPORT.get(code.upper())


def fetch_odds_for_sport(sport_key: str, ttl: int = 600, markets: str = "h2h") -> list[dict]:
    """Fetch raw odds events for a sport. Returns [] silently if no key/error.

    `markets` defaults to "h2h" to keep the global enrichment cheap; pass a
    comma-separated list ("h2h,totals,spreads,btts") to fetch deeper markets.
    """
    if not ODDS_API_KEY:
        return []
    cache_key = f"odds:{sport_key}:{markets}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"{ODDS_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk",
        "markets": markets,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            _cache.set(cache_key, [], 60)  # short cache to avoid hammering on errors
            return []
        data = resp.json() or []
    except requests.RequestException:
        return []

    _cache.set(cache_key, data, ttl)
    return data


def fetch_event_odds(sport_key: str, event_id: str, markets: str = "h2h,totals,spreads,btts", ttl: int = 600) -> Optional[dict]:
    """Fetch the full multi-market odds for ONE event id.

    The Odds API charges 1 credit per market per region, so we keep this to a
    single call (eu region) and cache for 10 min by default. Returns the
    raw event dict (with bookmakers/markets) or None on any failure.
    """
    if not ODDS_API_KEY or not event_id:
        return None
    cache_key = f"odds:event:{sport_key}:{event_id}:{markets}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"{ODDS_BASE}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk",
        "markets": markets,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            _cache.set(cache_key, None, 60)
            return None
        data = resp.json() or None
    except requests.RequestException:
        return None

    _cache.set(cache_key, data, ttl)
    return data


def _best_h2h(event: dict) -> Optional[dict]:
    """Pick best (highest) decimal odds from any bookmaker for h2h market."""
    best = {"home": None, "draw": None, "away": None, "bookmaker": None}
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    for bk in event.get("bookmakers", []) or []:
        for mk in bk.get("markets", []) or []:
            if mk.get("key") != "h2h":
                continue
            for o in mk.get("outcomes", []) or []:
                price = o.get("price")
                name = o.get("name")
                if price is None:
                    continue
                if name == home_team:
                    if best["home"] is None or price > best["home"]:
                        best["home"] = price
                        best["bookmaker"] = bk.get("title")
                elif name == away_team:
                    if best["away"] is None or price > best["away"]:
                        best["away"] = price
                elif name and name.lower() == "draw":
                    if best["draw"] is None or price > best["draw"]:
                        best["draw"] = price
    if best["home"] is None and best["draw"] is None and best["away"] is None:
        return None
    return best


def _index_events(events: list[dict]) -> dict[tuple[str, str], dict]:
    idx: dict[tuple[str, str], dict] = {}
    for ev in events:
        h = _team_key(ev.get("home_team"))
        a = _team_key(ev.get("away_team"))
        if h and a and (h, a) not in idx:
            idx[(h, a)] = ev
    return idx


def _name_match_score(target: str, candidate: str) -> float:
    if not target or not candidate:
        return 0.0
    if target == candidate:
        return 1.0
    score = 0.0
    if target in candidate or candidate in target:
        score = 0.93
    target_tokens = set(target.split())
    candidate_tokens = set(candidate.split())
    if target_tokens and candidate_tokens:
        overlap = len(target_tokens & candidate_tokens)
        if overlap:
            score = max(score, overlap / max(len(target_tokens), len(candidate_tokens)))
        if target_tokens.issubset(candidate_tokens) or candidate_tokens.issubset(target_tokens):
            score = max(score, 0.9)
        if target.split()[0] == candidate.split()[0]:
            score = max(score, 0.82)
    score = max(score, difflib.SequenceMatcher(None, target, candidate).ratio())
    return score


def _lookup(idx: dict[tuple[str, str], dict], home_name: str | None, away_name: str | None) -> Optional[dict]:
    h = _team_key(home_name)
    a = _team_key(away_name)
    if not h or not a:
        return None
    if (h, a) in idx:
        return idx[(h, a)]
    best_event: Optional[dict] = None
    best_score = 0.0
    for (eh, ea), ev in idx.items():
        home_score = _name_match_score(h, eh)
        away_score = _name_match_score(a, ea)
        if home_score < 0.58 or away_score < 0.58:
            continue
        pair_score = home_score + away_score
        if pair_score > best_score:
            best_score = pair_score
            best_event = ev
    return best_event if best_score >= 1.45 else None


def odds_for_match(competition_code: str | None, home_name: str | None, away_name: str | None) -> Optional[dict]:
    """Return dict with home/draw/away decimal odds for a single fixture, or None."""
    sport = _sport_for_competition(competition_code)
    if not sport:
        return None
    events = fetch_odds_for_sport(sport)
    if not events:
        return None
    idx = _index_events(events)
    ev = _lookup(idx, home_name, away_name)
    if not ev:
        return None
    return _best_h2h(ev)


# ---------- Multi-market betting panel for the match-detail page ----------

MARKET_LABELS = {
    "h2h": "Match result",
    "totals": "Goals over/under",
    "spreads": "Asian handicap",
    "btts": "Both teams to score",
}


def _organize_h2h(event: dict) -> dict:
    """Per-bookmaker home/draw/away rows + best (highest) prices."""
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    rows: list[dict] = []
    best = {"home": None, "draw": None, "away": None,
            "books": {"home": None, "draw": None, "away": None}}
    for bk in event.get("bookmakers", []) or []:
        bk_title = bk.get("title")
        row = {"bookmaker": bk_title, "home": None, "draw": None, "away": None}
        for mk in bk.get("markets", []) or []:
            if mk.get("key") != "h2h":
                continue
            for o in mk.get("outcomes", []) or []:
                price = o.get("price")
                name = o.get("name")
                if price is None:
                    continue
                if name == home_team:
                    row["home"] = price
                    if best["home"] is None or price > best["home"]:
                        best["home"] = price
                        best["books"]["home"] = bk_title
                elif name == away_team:
                    row["away"] = price
                    if best["away"] is None or price > best["away"]:
                        best["away"] = price
                        best["books"]["away"] = bk_title
                elif name and name.lower() == "draw":
                    row["draw"] = price
                    if best["draw"] is None or price > best["draw"]:
                        best["draw"] = price
                        best["books"]["draw"] = bk_title
        if any(v is not None for v in (row["home"], row["draw"], row["away"])):
            rows.append(row)
    rows.sort(key=lambda r: r["bookmaker"] or "")
    return {"best": best, "rows": rows}


def _organize_totals(event: dict) -> dict:
    """Group Over/Under by goal-line. Returns lines sorted by point asc."""
    lines: dict[float, dict] = {}
    for bk in event.get("bookmakers", []) or []:
        bk_title = bk.get("title")
        for mk in bk.get("markets", []) or []:
            if mk.get("key") != "totals":
                continue
            for o in mk.get("outcomes", []) or []:
                point = o.get("point")
                price = o.get("price")
                name = (o.get("name") or "").lower()
                if point is None or price is None or name not in ("over", "under"):
                    continue
                bucket = lines.setdefault(point, {
                    "point": point,
                    "best": {"over": None, "under": None,
                             "books": {"over": None, "under": None}},
                    "rows": {},
                })
                row = bucket["rows"].setdefault(bk_title, {
                    "bookmaker": bk_title, "over": None, "under": None,
                })
                row[name] = price
                if bucket["best"][name] is None or price > bucket["best"][name]:
                    bucket["best"][name] = price
                    bucket["best"]["books"][name] = bk_title
    out = []
    for point, b in sorted(lines.items()):
        b["rows"] = sorted(b["rows"].values(), key=lambda r: r["bookmaker"] or "")
        out.append(b)
    return {"lines": out}


def _organize_spreads(event: dict) -> dict:
    """Group Asian handicap by point (positive = home gets +N, negative = home -N)."""
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    lines: dict[float, dict] = {}
    for bk in event.get("bookmakers", []) or []:
        bk_title = bk.get("title")
        for mk in bk.get("markets", []) or []:
            if mk.get("key") != "spreads":
                continue
            outcomes = mk.get("outcomes", []) or []
            # Each market has 2 outcomes (home & away) at the same |point|.
            home_point = None
            for o in outcomes:
                if o.get("name") == home_team and o.get("point") is not None:
                    home_point = o.get("point")
                    break
            if home_point is None:
                continue
            bucket = lines.setdefault(home_point, {
                "point": home_point,
                "best": {"home": None, "away": None,
                         "books": {"home": None, "away": None}},
                "rows": {},
            })
            row = bucket["rows"].setdefault(bk_title, {
                "bookmaker": bk_title, "home": None, "away": None,
            })
            for o in outcomes:
                price = o.get("price")
                name = o.get("name")
                if price is None:
                    continue
                if name == home_team:
                    row["home"] = price
                    if bucket["best"]["home"] is None or price > bucket["best"]["home"]:
                        bucket["best"]["home"] = price
                        bucket["best"]["books"]["home"] = bk_title
                elif name == away_team:
                    row["away"] = price
                    if bucket["best"]["away"] is None or price > bucket["best"]["away"]:
                        bucket["best"]["away"] = price
                        bucket["best"]["books"]["away"] = bk_title
    out = []
    for point, b in sorted(lines.items()):
        b["rows"] = sorted(b["rows"].values(), key=lambda r: r["bookmaker"] or "")
        out.append(b)
    return {"lines": out}


def _organize_btts(event: dict) -> dict:
    rows: list[dict] = []
    best = {"yes": None, "no": None, "books": {"yes": None, "no": None}}
    for bk in event.get("bookmakers", []) or []:
        bk_title = bk.get("title")
        row = {"bookmaker": bk_title, "yes": None, "no": None}
        for mk in bk.get("markets", []) or []:
            if mk.get("key") != "btts":
                continue
            for o in mk.get("outcomes", []) or []:
                name = (o.get("name") or "").lower()
                price = o.get("price")
                if price is None or name not in ("yes", "no"):
                    continue
                row[name] = price
                if best[name] is None or price > best[name]:
                    best[name] = price
                    best["books"][name] = bk_title
        if row["yes"] is not None or row["no"] is not None:
            rows.append(row)
    rows.sort(key=lambda r: r["bookmaker"] or "")
    return {"best": best, "rows": rows}


def full_markets_for_match(competition_code: str | None, home_name: str | None, away_name: str | None) -> Optional[dict]:
    """Return organized multi-market betting data for the match-detail page.

    Strategy: use the cached sport-wide h2h list to find the event id, then
    make ONE per-event call for the deeper markets (h2h+totals+spreads+btts).
    """
    sport = _sport_for_competition(competition_code)
    if not sport or not ODDS_API_KEY:
        return None
    events = fetch_odds_for_sport(sport)
    if not events:
        return None
    idx = _index_events(events)
    ev_lite = _lookup(idx, home_name, away_name)
    if not ev_lite:
        return None
    event_id = ev_lite.get("id")
    if not event_id:
        return None

    ev = fetch_event_odds(sport, event_id, markets="h2h,totals,spreads,btts") or ev_lite

    # Discover which markets are actually present
    have: set[str] = set()
    bookmakers: set[str] = set()
    for bk in ev.get("bookmakers", []) or []:
        if bk.get("title"):
            bookmakers.add(bk["title"])
        for mk in bk.get("markets", []) or []:
            if mk.get("key") in MARKET_LABELS:
                have.add(mk["key"])

    out = {
        "home_team": ev.get("home_team"),
        "away_team": ev.get("away_team"),
        "commence_time": ev.get("commence_time"),
        "bookmakers": sorted(bookmakers),
        "available_markets": [k for k in ("h2h", "totals", "spreads", "btts") if k in have],
        "labels": MARKET_LABELS,
    }
    if "h2h" in have:
        out["h2h"] = _organize_h2h(ev)
    if "totals" in have:
        out["totals"] = _organize_totals(ev)
    if "spreads" in have:
        out["spreads"] = _organize_spreads(ev)
    if "btts" in have:
        out["btts"] = _organize_btts(ev)
    return out


def enrich_matches_with_odds(matches: list[dict]) -> list[dict]:
    """Attach `_odds` dict (home/draw/away) to each match in-place. Returns list."""
    if not matches or not ODDS_API_KEY:
        return matches
    # Group matches by sport_key to fetch each sport once
    by_sport: dict[str, list[dict]] = {}
    for m in matches:
        code = (m.get("competition") or {}).get("code")
        sport = _sport_for_competition(code)
        if sport:
            by_sport.setdefault(sport, []).append(m)

    # Fetch all sport odds and index them once
    sport_index: dict[str, dict] = {}
    for sport in by_sport.keys():
        sport_index[sport] = _index_events(fetch_odds_for_sport(sport))

    for m in matches:
        code = (m.get("competition") or {}).get("code")
        sport = _sport_for_competition(code)
        if not sport:
            continue
        idx = sport_index.get(sport, {})
        ev = _lookup(
            idx,
            (m.get("homeTeam") or {}).get("name") or (m.get("homeTeam") or {}).get("shortName"),
            (m.get("awayTeam") or {}).get("name") or (m.get("awayTeam") or {}).get("shortName"),
        )
        if ev:
            best = _best_h2h(ev)
            if best:
                m["_odds"] = best
    return matches


def has_odds_api() -> bool:
    return bool(ODDS_API_KEY)


def probe_api(sport_key: str = "soccer_epl") -> dict[str, Any]:
    """Run a lightweight provider health check with request quota metadata."""
    configured = bool(ODDS_API_KEY)
    if not configured:
        return {"ok": False, "configured": False, "error": "ODDS_API_KEY is not set."}

    url = f"{ODDS_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "uk",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        return {"ok": False, "configured": True, "sport": sport_key, "error": str(exc)}

    quota_meta = {
        "requests_remaining": resp.headers.get("x-requests-remaining"),
        "requests_used": resp.headers.get("x-requests-used"),
        "requests_last": resp.headers.get("x-requests-last"),
    }
    if resp.status_code != 200:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "")
        return {
            "ok": False,
            "configured": True,
            "sport": sport_key,
            "status_code": resp.status_code,
            "error": message or resp.text[:200] or "Unexpected Odds API response.",
            **quota_meta,
        }

    try:
        data = resp.json() or []
    except ValueError:
        data = []
    return {
        "ok": True,
        "configured": True,
        "sport": sport_key,
        "event_count": len(data) if isinstance(data, list) else 0,
        **quota_meta,
    }
