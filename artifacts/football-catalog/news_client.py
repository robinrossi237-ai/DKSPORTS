"""Client for the football-news11 RapidAPI endpoint.

Fetches league-scoped football news. Uses simple in-memory TTL cache to stay
within typical RapidAPI free-tier monthly quotas.
"""
from __future__ import annotations

import os
import time
import threading
from typing import Any

import requests

from api_client import FREE_TIER_COMPETITIONS

NEWS_BASE = "https://football-news11.p.rapidapi.com/api"
NEWS_HOST = "football-news11.p.rapidapi.com"
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_FOOTBALL_NEWS_KEY", "")

DEFAULT_TIMEOUT = 12

# football-data.org competition code -> football-news11 league_id.
# These mappings are best-effort defaults; tweak after confirming via the
# /api/leagues endpoint of the provider. Using 52 as a safe default for PL
# because that is what the user verified in their original request.
# Confirmed: league_id=52 returns data (this was the example the user verified).
# Other IDs are best-guess defaults for the same provider; users can adjust as
# more mappings are confirmed. None means "no news for this competition".
COMPETITION_TO_NEWS_LEAGUE = {
    "PL": 52,    # Premier League (confirmed)
    "PD": 87,    # La Liga
    "SA": 135,   # Serie A
    "BL1": 78,   # Bundesliga
    "FL1": 61,   # Ligue 1
    "CL": 2,     # UEFA Champions League
    "EL": 3,     # UEFA Europa League
    "DED": 88,   # Eredivisie
    "PPL": 94,   # Primeira Liga
    "BSA": 71,   # Brasileirão
    "ELC": 40,   # Championship
    "WC": 1,     # World Cup
    "EC": 4,     # Euros
}

# A "general" feed for the home page. Rotated through across requests to stay
# within the BASIC RapidAPI plan (100 calls / day). With NEWS_TTL=1h, one
# league per request means at most ~24 calls/day on the home feed.
DEFAULT_HOME_LEAGUE_IDS = [52, 87, 2, 78]  # Premier League, La Liga, UCL, Bundesliga
NEWS_COMPETITION_META = {
    c["code"]: {
        "code": c["code"],
        "name": c["name"],
        "emblem": c.get("emblem") or "",
    }
    for c in FREE_TIER_COMPETITIONS
}
NEWS_LEAGUE_TO_COMPETITION = {
    league_id: code
    for code, league_id in COMPETITION_TO_NEWS_LEAGUE.items()
    if league_id
}


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
NEWS_TTL = 3600  # 1 hour — keeps daily call count well under the BASIC quota
_HOME_ROTATION_LOCK = threading.Lock()
_HOME_ROTATION_IDX = 0


def _domain_from_url(url: str) -> str:
    """Return a friendly source label from a URL, e.g. 'football-espana.net'."""
    if not url or not isinstance(url, str):
        return ""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc or url
    except Exception:
        host = url
    host = host.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host.split("/")[0] or ""


class NewsApiError(Exception):
    """Wraps any error returned by the news API so callers can show a friendly message."""

    def __init__(self, message: str, status_code: int | None = None, kind: str = "error"):
        super().__init__(message)
        self.status_code = status_code
        # kind: "error" | "subscription" | "ratelimit" | "no_key" | "network"
        self.kind = kind


def has_news_api() -> bool:
    return bool(RAPIDAPI_KEY)


def _headers() -> dict[str, str]:
    return {
        "x-rapidapi-host": NEWS_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
        "Content-Type": "application/json",
    }


def _request(path: str, params: dict[str, Any]) -> Any:
    if not RAPIDAPI_KEY:
        raise NewsApiError("News API key not configured.", kind="no_key")
    url = f"{NEWS_BASE}/{path.lstrip('/')}"
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise NewsApiError(f"Network error: {exc}", kind="network") from exc

    # Try to parse JSON regardless of status — RapidAPI returns JSON for errors too.
    try:
        data = r.json()
    except ValueError:
        raise NewsApiError(f"Bad response (status {r.status_code}).", status_code=r.status_code)

    is_error_payload = (
        isinstance(data, dict)
        and data.get("message")
        and not (data.get("result") or data.get("data") or data.get("news") or data.get("articles"))
    )
    if r.status_code >= 400 or is_error_payload:
        msg = (isinstance(data, dict) and (data.get("message") or data.get("error"))) or f"HTTP {r.status_code}"
        msg_l = str(msg).lower()
        kind = "error"
        if "not subscribed" in msg_l:
            kind = "subscription"
        elif "exceeded" in msg_l and ("quota" in msg_l or "limit" in msg_l) or r.status_code == 429:
            kind = "ratelimit"
        elif r.status_code == 401 or ("key" in msg_l and "invalid" in msg_l):
            kind = "no_key"
        raise NewsApiError(str(msg), status_code=r.status_code, kind=kind)

    return data


def _normalize_articles(raw: Any) -> list[dict[str, Any]]:
    """The API may return a list, or a dict with `data`/`news`/`articles`. Normalize."""
    if not raw:
        return []
    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("result", "data", "news", "articles", "results"):
            v = raw.get(key)
            if isinstance(v, list):
                items = v
                break

    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (
            it.get("title")
            or it.get("headline")
            or it.get("name")
            or ""
        )
        url = (
            it.get("url")
            or it.get("original_url")
            or it.get("link")
            or it.get("source_url")
            or ""
        )
        image = (
            it.get("image")
            or it.get("thumbnail")
            or it.get("img")
            or (it.get("images") or [None])[0]
            or ""
        )
        if isinstance(image, dict):
            image = image.get("url") or image.get("src") or ""
        raw_source = it.get("source") or it.get("publisher") or it.get("provider") or ""
        source = ""
        source_url = ""
        if isinstance(raw_source, dict):
            source = (
                raw_source.get("name")
                or raw_source.get("title")
                or raw_source.get("displayName")
                or _domain_from_url(raw_source.get("main_url") or raw_source.get("url") or raw_source.get("website") or "")
                or ""
            )
            source_url = (
                raw_source.get("main_url")
                or raw_source.get("url")
                or raw_source.get("website")
                or ""
            )
        elif isinstance(raw_source, str):
            source = raw_source
        if not source:
            source = _domain_from_url(url)
        published = (
            it.get("published_at")
            or it.get("publishedAt")
            or it.get("date")
            or it.get("pub_date")
            or it.get("created_at")
            or ""
        )
        description = (
            it.get("description")
            or it.get("excerpt")
            or it.get("summary")
            or it.get("snippet")
            or ""
        )
        out.append({
            "id": it.get("id"),
            "alias": it.get("alias") or "",
            "lang": it.get("lang") or "",
            "title": title,
            "url": url,
            "image": image,
            "source": source,
            "source_url": source_url,
            "published_at": published,
            "description": description,
            "categories": it.get("categories") if isinstance(it.get("categories"), list) else [],
            "teams": it.get("teams") if isinstance(it.get("teams"), list) else [],
            "leagues": it.get("leagues") if isinstance(it.get("leagues"), list) else [],
            "countries": it.get("countries") if isinstance(it.get("countries"), list) else [],
        })
    return out


def article_external_id(article: dict[str, Any]) -> str:
    value = (
        article.get("article_id")
        or article.get("id")
        or article.get("alias")
        or article.get("url")
        or article.get("title")
        or ""
    )
    return str(value).strip()


def fetch_news_by_league(league_id: int, lang: str = "en", page: int = 1, ttl: int = NEWS_TTL) -> list[dict[str, Any]]:
    """Return a normalized list of articles for the given news-API league_id."""
    cache_key = f"news:{league_id}:{lang}:{page}"
    cached = _cache.get(cache_key, ttl)
    if cached is not None:
        return cached
    raw = _request("news-by-league", {"league_id": league_id, "lang": lang, "page": page})
    articles = _normalize_articles(raw)
    _cache.set(cache_key, articles)
    return articles


def league_id_for_competition(code: str) -> int | None:
    return COMPETITION_TO_NEWS_LEAGUE.get((code or "").upper())


def competition_meta_for_code(code: str) -> dict[str, str]:
    return NEWS_COMPETITION_META.get((code or "").upper(), {
        "code": (code or "").upper(),
        "name": (code or "").upper(),
        "emblem": "",
    })


def _tag_articles(articles: list[dict[str, Any]], competition_code: str | None) -> list[dict[str, Any]]:
    if not competition_code:
        return articles
    meta = competition_meta_for_code(competition_code)
    tagged: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        tagged.append({
            **article,
            "competition_code": meta["code"],
            "competition_name": meta["name"],
            "competition_emblem": meta["emblem"],
        })
    return tagged


def _article_key(article: dict[str, Any]) -> str:
    return str(article.get("url") or article.get("title") or "").strip()


def fetch_news_for_competition(code: str, lang: str = "en", page: int = 1) -> list[dict[str, Any]]:
    lid = league_id_for_competition(code)
    if not lid:
        return []
    return _tag_articles(fetch_news_by_league(lid, lang=lang, page=page), code)


def fetch_catalog_news(
    codes: list[str] | None = None,
    *,
    per_competition: int = 4,
    limit: int = 36,
    lang: str = "en",
) -> list[dict[str, Any]]:
    """Build an interleaved all-leagues news feed.

    The output is intentionally interleaved round-robin so the catalog page
    shows representation from many leagues instead of one league dominating the
    first screenful.
    """
    selected_codes = [
        (code or "").upper()
        for code in (codes or NEWS_COMPETITION_META.keys())
        if league_id_for_competition(code)
    ]
    buckets: list[list[dict[str, Any]]] = []
    last_err: NewsApiError | None = None

    for code in selected_codes:
        try:
            articles = fetch_news_for_competition(code, lang=lang, page=1)[:max(1, per_competition)]
        except NewsApiError as exc:
            last_err = last_err or exc
            if exc.kind in ("ratelimit", "subscription", "no_key"):
                break
            continue
        if articles:
            buckets.append(articles)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_len = max((len(bucket) for bucket in buckets), default=0)
    for idx in range(max_len):
        for bucket in buckets:
            if idx >= len(bucket):
                continue
            article = bucket[idx]
            key = _article_key(article)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(article)
            if len(out) >= limit:
                return out

    if not out and last_err is not None:
        raise last_err
    return out


def fetch_news_by_team(team_name: str, lang: str = "en", page: int = 1, ttl: int = NEWS_TTL) -> list[dict[str, Any]]:
    """Best-effort fetch of news articles for a single team.

    Falls back to an empty list when the provider doesn't support the action
    or the daily quota is exhausted, so the team page never errors out.
    """
    if not team_name:
        return []
    cache_key = f"news_team:{team_name.lower()}:{lang}:{page}"
    cached = _cache.get(cache_key, ttl)
    if cached is not None:
        return cached
    try:
        raw = _request("news-by-team", {"team_name": team_name, "lang": lang, "page": page})
    except NewsApiError:
        # Silent failure — we don't want missing team news to render an error
        # banner over the full page; cache an empty list for a short window so
        # we don't hammer the provider.
        _cache.set(cache_key, [])
        return []
    articles = _normalize_articles(raw)
    _cache.set(cache_key, articles)
    return articles


def fetch_news_by_player(player_name: str, lang: str = "en", page: int = 1, ttl: int = NEWS_TTL) -> list[dict[str, Any]]:
    """Best-effort fetch of news articles for a single player."""
    if not player_name:
        return []
    cache_key = f"news_player:{player_name.lower()}:{lang}:{page}"
    cached = _cache.get(cache_key, ttl)
    if cached is not None:
        return cached
    try:
        raw = _request("news-by-player", {"player_name": player_name, "lang": lang, "page": page})
    except NewsApiError:
        _cache.set(cache_key, [])
        return []
    articles = _normalize_articles(raw)
    _cache.set(cache_key, articles)
    return articles


def fetch_home_news(limit: int = 9, lang: str = "en") -> list[dict[str, Any]]:
    """Aggregate a few top leagues for the home page. Returns up to ``limit`` articles.

    Strategy designed to respect the BASIC RapidAPI quota (100 reqs/day):
      1. Always serve any cached league results first (free).
      2. If we still need more articles, fetch ONE additional league per call,
         rotating through DEFAULT_HOME_LEAGUE_IDS so each request only spends
         at most one network call on the news provider.
      3. On 429 (quota exceeded) we still return whatever cached articles we
         have, so the section stays populated for the rest of the day.
    """
    global _HOME_ROTATION_IDX
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _push(arts: list[dict[str, Any]]) -> bool:
        for a in arts:
            key = _article_key(a)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(a)
            if len(out) >= limit:
                return True
        return False

    # Pass 1 — cache only (no network hits).
    for lid in DEFAULT_HOME_LEAGUE_IDS:
        cached = _cache.get(f"news:{lid}:{lang}:1", NEWS_TTL)
        if cached and _push(_tag_articles(cached, NEWS_LEAGUE_TO_COMPETITION.get(lid))):
            return out

    if len(out) >= limit:
        return out

    # Pass 2 — fetch one league per call, rotating across requests.
    with _HOME_ROTATION_LOCK:
        start = _HOME_ROTATION_IDX
        _HOME_ROTATION_IDX = (start + 1) % max(1, len(DEFAULT_HOME_LEAGUE_IDS))
    n = len(DEFAULT_HOME_LEAGUE_IDS)
    last_err: NewsApiError | None = None
    for offset in range(n):
        lid = DEFAULT_HOME_LEAGUE_IDS[(start + offset) % n]
        # Skip if already loaded from cache above.
        if _cache.get(f"news:{lid}:{lang}:1", NEWS_TTL):
            continue
        try:
            arts = fetch_news_by_league(lid, lang=lang, page=1)
        except NewsApiError as exc:
            last_err = exc
            # On quota / subscription errors just stop — the cached articles
            # we already collected (if any) are still served.
            if exc.kind in ("ratelimit", "subscription", "no_key"):
                break
            continue
        if _push(_tag_articles(arts, NEWS_LEAGUE_TO_COMPETITION.get(lid))):
            return out
        # Only one network call per home request.
        break

    if not out and last_err is not None:
        # Re-raise so the caller can show a helpful message in the UI.
        raise last_err
    return out
