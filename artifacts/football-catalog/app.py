"""DKSPORTS Flask application."""
from __future__ import annotations

import hashlib
import os
import random
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

# Load .env file (if present) before anything reads env vars
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from flask import (
    Flask, abort, flash, jsonify, make_response,
    redirect, render_template, request, send_file, url_for,
)

import api_client
import odds_client
import news_client
import api_football
import players_helpers
import rapidapi_extra_client
import shotmap_client
import virtual_game as vg_module
import virtual_league as vl_module
import admin_ops
import db_client
import data_importer
import smart_sync
from api_client import FootballDataError, FREE_TIER_COMPETITIONS
from news_client import NewsApiError
from players_helpers import (
    POSITION_FILTERS,
    PLAYER_LEAGUE_CODES,
    TOP_SCORER_LEAGUE_CODES,
)

# Initialise the PostgreSQL database (creates tables if they don't exist)
try:
    db_client.init_db()
except Exception as _db_init_err:
    print(f"[db] init_db error (non-fatal): {_db_init_err}", flush=True)

# Position-based European/relegation qualification per competition.
# Each value: list of (start_pos, end_pos, qual_class). qual_class drives the
# left-border color in the standings table, and feeds the legend.
QUAL_LABELS = {
    "ucl":     ("UEFA Champions League", "#2f5dff"),
    "ucl-q":   ("UCL qualification",     "#4a7bff"),
    "uel":     ("UEFA Europa League",    "#f97316"),
    "uel-q":   ("UEL qualification",     "#fb923c"),
    "uecl":    ("UEFA Conference League","#10b981"),
    "uecl-q":  ("UECL qualification",    "#34d399"),
    "uecl-po": ("UECL playoffs",         "#34d399"),
    "rel-po":  ("Relegation playoff",    "#f59e0b"),
    "rel":     ("Relegation",            "#dc2626"),
    "promo":   ("Promotion",             "#16a34a"),
    "po":      ("Promotion playoff",     "#84cc16"),
    "lib":     ("Copa Libertadores",     "#2f5dff"),
    "sud":     ("Copa Sudamericana",     "#10b981"),
}

EUROPE_QUALIFICATION = {
    "PL":  [(1, 4, "ucl"), (5, 5, "uel"), (6, 6, "uecl"), (18, 20, "rel")],
    "PD":  [(1, 4, "ucl"), (5, 6, "uel"), (7, 7, "uecl"), (18, 20, "rel")],
    "SA":  [(1, 4, "ucl"), (5, 5, "uel"), (6, 7, "uecl"), (18, 20, "rel")],
    "BL1": [(1, 4, "ucl"), (5, 5, "uel"), (6, 6, "uecl"), (16, 16, "rel-po"), (17, 18, "rel")],
    "FL1": [(1, 3, "ucl"), (4, 4, "ucl-q"), (5, 5, "uel"), (6, 6, "uecl-po"), (16, 16, "rel-po"), (17, 18, "rel")],
    "DED": [(1, 1, "ucl"), (2, 2, "ucl-q"), (3, 3, "uel"), (4, 4, "uecl"), (5, 7, "uecl-po"), (17, 18, "rel")],
    "PPL": [(1, 2, "ucl"), (3, 3, "ucl-q"), (4, 4, "uel"), (5, 5, "uecl-q"), (17, 18, "rel")],
    "BSA": [(1, 6, "lib"), (7, 12, "sud"), (17, 20, "rel")],
    "ELC": [(1, 2, "promo"), (3, 6, "po"), (22, 24, "rel")],
    "CL":  [],  # group-stage groups handled separately
    "EC":  [],
    "WC":  [],
}


def annotate_standings_qualification(standings_data: dict, code: str) -> list[str]:
    """Mutate each row in standings_data['standings'][*]['table'] adding `_qual` class.
    Returns the ordered list of unique qual classes that were applied (for the legend).
    """
    rules = EUROPE_QUALIFICATION.get((code or "").upper(), [])
    applied: list[str] = []
    if not standings_data or not isinstance(standings_data, dict):
        return applied
    for st in standings_data.get("standings", []) or []:
        for row in st.get("table", []) or []:
            pos = row.get("position")
            if not pos:
                continue
            row["_qual"] = ""
            for start, end, cls in rules:
                if start <= pos <= end:
                    row["_qual"] = cls
                    if cls not in applied:
                        applied.append(cls)
                    break
    return applied

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.secret_key = os.environ.get("SESSION_SECRET", "dksports-dev-secret-change-me")
OFFLINE_MODE_ENV_KEY = "OFFLINE_MODE"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _offline_mode_enabled() -> bool:
    return _env_flag(OFFLINE_MODE_ENV_KEY, False)


def _startup_sync_codes() -> list[str]:
    codes: list[str] = []
    for comp in FREE_TIER_COMPETITIONS:
        code = comp.get("code") if isinstance(comp, dict) else getattr(comp, "code", None)
        if code:
            codes.append(str(code).upper())
    return codes


def _startup_sync_snapshot() -> dict[str, Any]:
    stats = db_client.get_db_stats()
    status = data_importer.get_status()
    codes = _startup_sync_codes()
    return {
        **status,
        "competition_codes": codes,
        "competition_count": len(codes),
        "cached_matches": stats.get("matches", 0),
        "cached_competitions": stats.get("competitions", 0),
        "offline_mode": _offline_mode_enabled(),
        "db_stats": stats,
    }


def _warm_caches_async() -> None:
    """Pre-fetch top scorers and search index in the background.

    Runs once per process so the first home/players page render isn't blocked
    by football-data.org rate limits.
    """
    import threading
    import time as _time
    def _worker() -> None:
        # Warm standings for the 5 majors first — they're cheap, cached for
        # 30 min by api_client, and unlock the home Top-of-the-table section,
        # the hero PL leader card, and team-position pills.
        print("[warmup] starting standings prefetch", flush=True)
        try:
            _time.sleep(2)
            ok = 0
            for code in ("PL", "PD", "BL1", "SA", "FL1"):
                try:
                    api_client.get_standings(code)
                    ok += 1
                except Exception:
                    pass
                _time.sleep(7)  # ~9 calls/min, safely under the 10/min cap
            print(f"[warmup] standings warmed: {ok}/5 leagues", flush=True)
        except Exception as e:
            print(f"[warmup] standings warmup failed: {e}", flush=True)
        print("[warmup] starting top scorers prefetch", flush=True)
        try:
            ok = 0
            for code in players_helpers.TOP_SCORER_LEAGUE_CODES:
                try:
                    api_client.get_scorers(code, limit=20)
                    ok += 1
                except Exception:
                    pass
                _time.sleep(7)
            # Now hydrate the per-league wrapper used by the home page.
            groups = players_helpers.get_top_scorers_by_league(limit_per_league=5)
            print(
                f"[warmup] top scorers fetched: {ok}/5 raw, {len(groups)} hydrated leagues",
                flush=True,
            )
        except Exception as e:
            print(f"[warmup] top scorers failed: {e}", flush=True)
        try:
            _time.sleep(60)
            print("[warmup] starting search index prefetch", flush=True)
            idx = players_helpers.build_search_index()
            print(
                f"[warmup] search index: comps={len(idx['competitions'])} "
                f"teams={len(idx['teams'])} players={len(idx['players'])}",
                flush=True,
            )
        except Exception as e:
            print(f"[warmup] search index failed: {e}", flush=True)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# Warm caches once per worker process. Under the Werkzeug debug reloader,
# WERKZEUG_RUN_MAIN is "true" only in the child (request-serving) process —
# we skip the parent watcher process. In production (no reloader) the env
# var is unset AND debug is off, so we run unconditionally.
_run_main = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
_in_dev_reloader = "WERKZEUG_RUN_MAIN" in os.environ or app.debug
if _run_main or not _in_dev_reloader:
    if _offline_mode_enabled():
        print("[startup] offline mode enabled; skipping warmup and smart sync", flush=True)
    else:
        _warm_caches_async()
        # Smart incremental DB sync only runs when live providers are allowed.
        smart_sync.start_smart_sync_in_background()


# ---------- Template helpers ----------

@app.template_filter("local_to_url")
def local_to_url_filter(path: str | None) -> str | None:
    """Convert an absolute static-folder path to a Flask static URL."""
    if not path:
        return None
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    try:
        rel = os.path.relpath(os.path.abspath(path), static_dir)
        if rel.startswith(".."):
            return None
        from flask import url_for as _uf
        return _uf("static", filename=rel.replace("\\", "/"))
    except Exception:
        return None


@app.template_global("img_url")
def img_url(local_path: str | None, fallback: str | None = None) -> str | None:
    """Return a local static URL for local_path, falling back to fallback (external URL)."""
    if local_path and str(local_path).startswith("/static/"):
        return str(local_path)
    url = local_to_url_filter(local_path)
    return url or fallback or None


def _browser_asset_url(path: str | None) -> str:
    """Normalize local absolute asset paths into browser-safe /static URLs."""
    if not path:
        return ""
    value = str(path).strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://", "/static/", "data:")):
        return value
    if value.startswith("/"):
        return value
    static_dir = os.path.abspath(app.static_folder or os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
    try:
        abs_path = os.path.abspath(value)
    except Exception:
        return value
    if not abs_path.startswith(static_dir):
        return value
    rel = os.path.relpath(abs_path, static_dir).replace("\\", "/")
    return f"/static/{quote(rel, safe='/')}"


@app.template_filter("fmt_date")
def fmt_date(value: str | None, fmt: str = "%b %d, %Y") -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return value


@app.template_filter("fmt_datetime")
def fmt_datetime(value: str | None, fmt: str = "%b %d, %Y · %H:%M") -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return value


@app.template_filter("fmt_dmy")
def fmt_dmy(value: str | None, fmt: str = "%d/%m/%Y") -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return value


@app.template_filter("fmt_time")
def fmt_time(value: str | None, fmt: str = "%H:%M") -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return value


@app.template_filter("fmt_day")
def fmt_day(value: str | None, fmt: str = "%-d %b") -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return value


@app.template_filter("years_old")
def years_old(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dob = datetime.fromisoformat(value).date()
        today = datetime.now(timezone.utc).date()
        years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        return str(years)
    except Exception:
        return "-"


@app.context_processor
def inject_globals() -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    page_theme = _page_theme_for_request()
    return {
        "site_name": "DKSPORTS",
        "tagline": "Live football catalog",
        "free_tier_competitions": FREE_TIER_COMPETITIONS,
        "startup_sync_count": len(_startup_sync_codes()),
        "current_year": now_utc.year,
        "today_iso": now_utc.strftime("%Y-%m-%d"),
        "offline_mode": _offline_mode_enabled(),
        "has_odds": odds_client.has_odds_api(),
        "has_news": news_client.has_news_api(),
        "has_shotmap": shotmap_client.has_shotmap_api(),
        "qual_labels": QUAL_LABELS,
        "page_theme": page_theme,
        "league_theme_tokens": LEAGUE_THEME_TOKENS,
    }


# ---------- Club color theming ----------

# Friendly-name -> hex map. We match the first non-white token from the
# football-data.org ``clubColors`` field and use that as the team page accent.
_CLUB_COLOR_HEX = {
    "red": "#dc2626", "scarlet": "#dc2626", "crimson": "#dc2626",
    "blue": "#2563eb", "navy": "#1e3a8a", "azure": "#0284c7",
    "sky": "#0284c7", "skyblue": "#0284c7", "royal": "#1d4ed8",
    "yellow": "#eab308", "amber": "#d97706", "gold": "#ca8a04",
    "green": "#16a34a", "emerald": "#059669", "lime": "#65a30d",
    "orange": "#ea580c",
    "purple": "#7c3aed", "violet": "#7c3aed", "magenta": "#c026d3",
    "pink": "#db2777", "rose": "#e11d48",
    "claret": "#7f1d1d", "burgundy": "#7f1d1d", "maroon": "#7f1d1d",
    "brown": "#78350f",
    "black": "#0f172a",
    "grey": "#64748b", "gray": "#64748b", "silver": "#94a3b8",
    "teal": "#0d9488", "cyan": "#06b6d4",
    "white": "",  # never used as accent — too pale
}


def parse_club_accent(colors_str: str | None) -> str | None:
    """Pick a usable accent hex color from the messy ``clubColors`` string.

    The football-data.org field looks like ``"Red / White"`` or
    ``"Sky Blue / White / Navy Blue"``. We tokenize on common separators,
    skip white, and return the first matching color hex.
    """
    if not colors_str:
        return None
    import re as _re
    parts = _re.split(r"[/,\-]+", colors_str)
    for part in parts:
        token = part.strip().lower()
        if not token or token == "white":
            continue
        # Direct hit (e.g. "blue").
        if token in _CLUB_COLOR_HEX and _CLUB_COLOR_HEX[token]:
            return _CLUB_COLOR_HEX[token]
        # Multi-word tokens — strip whitespace and try each word.
        for word in token.split():
            if word in _CLUB_COLOR_HEX and _CLUB_COLOR_HEX[word]:
                return _CLUB_COLOR_HEX[word]
        # Fallback: substring match.
        for k, v in _CLUB_COLOR_HEX.items():
            if v and k in token:
                return v
    return None


DEFAULT_PAGE_THEME: dict[str, Any] = {
    "code": "DEFAULT",
    "name": "DKSPORTS",
    "country": "",
    "primary": "#2f5dff",
    "accent": "#8b5cf6",
    "surface": "#eef4ff",
    "soft": "rgba(47, 93, 255, 0.10)",
    "glow": "rgba(47, 93, 255, 0.22)",
    "contrast": "#ffffff",
    "logo": "",
    "label": "Global",
}


LEAGUE_THEME_TOKENS: dict[str, dict[str, str]] = {
    "PL":  {"primary": "#37003c", "accent": "#9b5cff", "surface": "#f5ebff", "soft": "rgba(55, 0, 60, 0.10)", "glow": "rgba(155, 92, 255, 0.22)", "contrast": "#ffffff"},
    "PD":  {"primary": "#ef3340", "accent": "#ffb703", "surface": "#fff1f2", "soft": "rgba(239, 51, 64, 0.10)", "glow": "rgba(255, 183, 3, 0.24)", "contrast": "#ffffff"},
    "SA":  {"primary": "#0057b8", "accent": "#38bdf8", "surface": "#edf6ff", "soft": "rgba(0, 87, 184, 0.10)", "glow": "rgba(56, 189, 248, 0.22)", "contrast": "#ffffff"},
    "BL1": {"primary": "#d0001b", "accent": "#141414", "surface": "#fff0f2", "soft": "rgba(208, 0, 27, 0.10)", "glow": "rgba(20, 20, 20, 0.18)", "contrast": "#ffffff"},
    "FL1": {"primary": "#0f1b3d", "accent": "#d7ff4a", "surface": "#eef5ff", "soft": "rgba(15, 27, 61, 0.10)", "glow": "rgba(215, 255, 74, 0.24)", "contrast": "#ffffff"},
    "DED": {"primary": "#f97316", "accent": "#111827", "surface": "#fff7ed", "soft": "rgba(249, 115, 22, 0.10)", "glow": "rgba(17, 24, 39, 0.16)", "contrast": "#ffffff"},
    "PPL": {"primary": "#0057b8", "accent": "#f59e0b", "surface": "#eff6ff", "soft": "rgba(0, 87, 184, 0.10)", "glow": "rgba(245, 158, 11, 0.24)", "contrast": "#ffffff"},
    "BSA": {"primary": "#128c3a", "accent": "#f6c000", "surface": "#effdf5", "soft": "rgba(18, 140, 58, 0.10)", "glow": "rgba(246, 192, 0, 0.24)", "contrast": "#ffffff"},
    "ELC": {"primary": "#003da5", "accent": "#d71920", "surface": "#eef4ff", "soft": "rgba(0, 61, 165, 0.10)", "glow": "rgba(215, 25, 32, 0.20)", "contrast": "#ffffff"},
    "CL":  {"primary": "#0b2a6f", "accent": "#00d0ff", "surface": "#eef5ff", "soft": "rgba(11, 42, 111, 0.10)", "glow": "rgba(0, 208, 255, 0.20)", "contrast": "#ffffff"},
    "EL":  {"primary": "#f36c21", "accent": "#1f2937", "surface": "#fff7ed", "soft": "rgba(243, 108, 33, 0.10)", "glow": "rgba(31, 41, 55, 0.18)", "contrast": "#ffffff"},
    "ECL": {"primary": "#00a651", "accent": "#85d943", "surface": "#ecfdf3", "soft": "rgba(0, 166, 81, 0.10)", "glow": "rgba(133, 217, 67, 0.24)", "contrast": "#ffffff"},
    "WC":  {"primary": "#113b92", "accent": "#00c3ff", "surface": "#edf5ff", "soft": "rgba(17, 59, 146, 0.10)", "glow": "rgba(0, 195, 255, 0.20)", "contrast": "#ffffff"},
    "EC":  {"primary": "#004f9f", "accent": "#e10600", "surface": "#eef6ff", "soft": "rgba(0, 79, 159, 0.10)", "glow": "rgba(225, 6, 0, 0.18)", "contrast": "#ffffff"},
}


def _competition_logo_for_code(code: str) -> str:
    payload = _db_competition_payload(code)
    if payload and payload.get("emblem"):
        return payload.get("emblem") or ""
    league = vl_module.LEAGUES.get(code.upper()) if hasattr(vl_module, "LEAGUES") else None
    if league and league.get("logo"):
        return league.get("logo") or ""
    return _free_competition_seed(code).get("emblem") or ""


def _competition_country_for_code(code: str) -> str:
    payload = _db_competition_payload(code)
    if payload:
        return (payload.get("area") or {}).get("name") or payload.get("country") or ""
    league = vl_module.LEAGUES.get(code.upper()) if hasattr(vl_module, "LEAGUES") else None
    if league:
        return league.get("country") or ""
    return _free_competition_seed(code).get("country") or ""


def _page_theme_for_code(code: str | None, *, label: str | None = None) -> dict[str, Any]:
    code = (code or "").strip().upper()
    theme = dict(DEFAULT_PAGE_THEME)
    if not code:
        return theme
    league = vl_module.LEAGUES.get(code, {}) if hasattr(vl_module, "LEAGUES") else {}
    seed = _free_competition_seed(code)
    theme.update(LEAGUE_THEME_TOKENS.get(code, {}))
    theme.update({
        "code": code,
        "name": (league.get("name") or seed.get("name") or code),
        "country": (league.get("country") or _competition_country_for_code(code) or seed.get("country") or ""),
        "logo": (league.get("logo") or _competition_logo_for_code(code) or ""),
        "label": label or (league.get("name") or seed.get("name") or code),
    })
    return theme


def _page_theme_for_team_id(team_id: int | None) -> dict[str, Any]:
    if not team_id:
        return dict(DEFAULT_PAGE_THEME)
    team = _db_team_payload(team_id) or _load_team_payload(team_id) or {}
    running = team.get("runningCompetitions") or []
    for comp in running:
        code = (comp or {}).get("code")
        if code:
            return _page_theme_for_code(code, label=(comp or {}).get("name") or team.get("name") or "Team")
    return dict(DEFAULT_PAGE_THEME)


def _page_theme_for_player_id(player_id: int | None) -> dict[str, Any]:
    if not player_id:
        return dict(DEFAULT_PAGE_THEME)
    person = _db_player_payload(player_id) or _load_player_payload(player_id) or {}
    running = ((person.get("currentTeam") or {}).get("runningCompetitions")) or []
    for comp in running:
        code = (comp or {}).get("code")
        if code:
            return _page_theme_for_code(code, label=(comp or {}).get("name") or person.get("name") or "Player")
    return dict(DEFAULT_PAGE_THEME)


def _comparison_theme(kind: str, a_id: int | None, b_id: int | None) -> dict[str, Any]:
    left = _page_theme_for_player_id(a_id) if kind == "players" else _page_theme_for_team_id(a_id)
    right = _page_theme_for_player_id(b_id) if kind == "players" else _page_theme_for_team_id(b_id)
    if left.get("code") != "DEFAULT" and left.get("code") == right.get("code"):
        return left
    if left.get("code") != "DEFAULT" and right.get("code") != "DEFAULT":
        mixed = dict(DEFAULT_PAGE_THEME)
        mixed.update({
            "code": "COMPARE",
            "name": "Cross-league compare",
            "label": "Cross-league compare",
            "primary": left.get("primary") or DEFAULT_PAGE_THEME["primary"],
            "accent": right.get("primary") or right.get("accent") or DEFAULT_PAGE_THEME["accent"],
            "surface": left.get("surface") or DEFAULT_PAGE_THEME["surface"],
            "soft": left.get("soft") or DEFAULT_PAGE_THEME["soft"],
            "glow": right.get("glow") or DEFAULT_PAGE_THEME["glow"],
        })
        return mixed
    return left if left.get("code") != "DEFAULT" else (right if right.get("code") != "DEFAULT" else dict(DEFAULT_PAGE_THEME))


def _page_theme_for_request() -> dict[str, Any]:
    endpoint = (request.endpoint or "").strip()
    view_args = request.view_args or {}
    if endpoint in {"competition_detail", "matchday_digest"}:
        return _page_theme_for_code(view_args.get("code"))
    if endpoint == "team_detail":
        return _page_theme_for_team_id(view_args.get("team_id"))
    if endpoint == "player_detail":
        return _page_theme_for_player_id(view_args.get("person_id"))
    if endpoint == "news_page":
        return _page_theme_for_code(view_args.get("code"))
    if endpoint == "compare_teams_page":
        return _comparison_theme("teams", request.args.get("a", type=int), request.args.get("b", type=int))
    if endpoint == "compare_players_page":
        return _comparison_theme("players", request.args.get("a", type=int), request.args.get("b", type=int))
    if endpoint == "virtual_league_new_get":
        return _page_theme_for_code(view_args.get("league_code"))
    if endpoint in {"virtual_league_session", "virtual_league_play", "virtual_league_matchday_results", "virtual_league_team_results", "virtual_league_restart"}:
        session_key = view_args.get("session_key")
        if session_key:
            sess = db_client.get_virtual_league_session(session_key)
            if sess:
                return _page_theme_for_code(sess.get("league_code"))
    return dict(DEFAULT_PAGE_THEME)


# ---------- Error handling ----------

@app.errorhandler(FootballDataError)
def handle_api_error(err: FootballDataError):
    code = err.status_code if err.status_code in (403, 404, 429, 500, 502) else 500
    if request.path.startswith("/api/"):
        return jsonify({"error": str(err), "status": code}), code
    return render_template("error.html", message=str(err), code=code), code


@app.errorhandler(404)
def handle_404(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found", "status": 404}), 404
    return render_template("error.html", message="Page not found.", code=404), 404


# ---------- Helpers ----------

def _team_positions_for_codes(codes: list[str]) -> dict[int, dict[str, Any]]:
    """Return ``{team_id: {pos, comp_code, played, points}}`` across the given competitions.

    Reads the free-tier league standings (already cached by api_client) and only
    keeps the TOTAL table (ignores HOME/AWAY splits and CL groups). Best effort:
    failures for individual competitions are silently ignored.
    """
    out: dict[int, dict[str, Any]] = {}
    for code in codes:
        data = _load_standings_payload(code)
        for st in (data.get("standings") or []):
            if (st.get("type") or "").upper() != "TOTAL":
                continue
            for row in (st.get("table") or []):
                team = row.get("team") or {}
                tid = team.get("id")
                if not isinstance(tid, int):
                    continue
                # First standings entry wins (avoids overwriting league with cup group).
                if tid in out:
                    continue
                out[tid] = {
                    "pos": row.get("position"),
                    "comp_code": code,
                    "played": row.get("playedGames"),
                    "points": row.get("points"),
                    "form": row.get("form"),
                }
    return out


def _attach_competition_to_matches(matches: list[dict], competition: dict | None) -> None:
    """Make sure each match has a `.competition` dict with `code` so odds enrichment works."""
    if not competition:
        return
    code = competition.get("code")
    name = competition.get("name")
    for m in matches or []:
        if not m.get("competition"):
            m["competition"] = {"code": code, "name": name}


def _free_competition_seed(code: str) -> dict[str, Any]:
    return next((c for c in FREE_TIER_COMPETITIONS if c["code"] == code.upper()), {"code": code.upper(), "name": code.upper()})


@lru_cache(maxsize=64)
def _db_competition_payload(code: str) -> dict[str, Any] | None:
    code = code.upper()
    row = db_client.get_competition_by_code(code)
    seed = _free_competition_seed(code)
    if not row and not seed:
        return None
    season = db_client.get_season_for_code(code)
    current_md = None
    try:
        matches = db_client.get_matches_for_competition(code)
        future_mds = [m.get("matchday") for m in matches if m.get("status") in ("SCHEDULED", "TIMED", "POSTPONED") and m.get("matchday")]
        played_mds = [m.get("matchday") for m in matches if m.get("matchday")]
        current_md = min(future_mds) if future_mds else (max(played_mds) if played_mds else None)
    except Exception:
        current_md = None
    current_season = None
    if season:
        try:
            end_year = int(season) + 1
            start_date = f"{season}-08-01"
            end_date = f"{end_year}-05-31"
        except Exception:
            start_date = f"{season}-01-01"
            end_date = f"{season}-12-31"
        current_season = {
            "startDate": start_date,
            "endDate": end_date,
            "currentMatchday": current_md,
        }
    emblem = (
        _browser_asset_url((row or {}).get("emblem_local"))
        or (row or {}).get("emblem_url")
        or seed.get("emblem")
        or ""
    )
    country = (row or {}).get("area_name") or seed.get("country") or "International"
    return {
        "id": (row or {}).get("id"),
        "code": code,
        "name": (row or {}).get("name") or seed.get("name") or code,
        "area": {"name": country},
        "country": country,
        "emblem": emblem,
        "type": (row or {}).get("type") or seed.get("type"),
        "plan": (row or {}).get("plan"),
        "currentSeason": current_season,
    }


def _db_competitions_listing() -> list[dict]:
    rows = db_client.get_all_competitions()
    by_code = {str(r.get("code") or "").upper(): r for r in rows if r.get("code")}
    seen: set[str] = set()
    out: list[dict] = []
    for seed in FREE_TIER_COMPETITIONS:
        code = seed["code"].upper()
        seen.add(code)
        payload = _db_competition_payload(code) or {
            "code": code,
            "name": seed.get("name") or code,
            "area": {"name": seed.get("country") or "International"},
            "country": seed.get("country") or "International",
            "emblem": seed.get("emblem") or "",
            "type": seed.get("type"),
            "currentSeason": None,
        }
        payload["_free"] = True
        out.append(payload)
    for code, row in by_code.items():
        if code in seen:
            continue
        payload = _db_competition_payload(code)
        if not payload:
            continue
        payload["_free"] = code in {c["code"] for c in FREE_TIER_COMPETITIONS}
        out.append(payload)
    out.sort(key=lambda c: (not c.get("_free"), (c.get("area") or {}).get("name", ""), c.get("name", "")))
    return out


@lru_cache(maxsize=1024)
def _db_team_summary(team_id: int | None) -> dict[str, Any]:
    if not team_id:
        return {}
    row = db_client.get_team_by_id(int(team_id))
    if not row:
        return {}
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "shortName": row.get("short_name") or row.get("name"),
        "tla": row.get("tla"),
        "crest": _browser_asset_url(row.get("crest_local")) or row.get("crest_url") or "",
        "crest_local": row.get("crest_local"),
        "area": {"name": row.get("area_name") or ""},
    }


def _db_team_payload(team_id: int) -> dict[str, Any] | None:
    row = db_client.get_team_by_id(team_id)
    if not row:
        return None
    competitions = []
    for comp in db_client.get_competitions_for_team(team_id):
        competitions.append({
            "code": comp.get("code"),
            "name": comp.get("name"),
            "emblem": _browser_asset_url(comp.get("emblem_local")) or comp.get("emblem_url") or "",
        })
    squad = []
    for player in db_client.get_players_for_team(team_id):
        squad.append({
            "id": player.get("id"),
            "name": player.get("name"),
            "position": player.get("position"),
            "dateOfBirth": player.get("date_of_birth"),
            "nationality": player.get("nationality"),
            "shirtNumber": player.get("shirt_number"),
            "_photo": player.get("photo_url"),
            "_photo_local": player.get("photo_local"),
        })
    coach_name = row.get("coach_name") or ""
    coach_photo = _browser_asset_url(row.get("coach_photo")) or row.get("coach_photo") or ""
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "shortName": row.get("short_name") or row.get("name"),
        "tla": row.get("tla"),
        "crest": _browser_asset_url(row.get("crest_local")) or row.get("crest_url") or "",
        "crest_local": row.get("crest_local"),
        "area": {"name": row.get("area_name") or ""},
        "founded": row.get("founded"),
        "venue": row.get("venue"),
        "website": row.get("website"),
        "clubColors": row.get("club_colors"),
        "runningCompetitions": competitions,
        "squad": squad,
        "coach": {"name": coach_name} if coach_name else {},
        "coach_photo": coach_photo,
    }


def _db_player_payload(person_id: int) -> dict[str, Any] | None:
    row = db_client.get_player_by_id(person_id)
    if not row:
        return None
    person = row.get("raw_json") or {}
    current_team = _db_team_summary(row.get("current_team_id"))
    if current_team:
        current_team["runningCompetitions"] = [
            {
                "code": comp.get("code"),
                "name": comp.get("name"),
                "emblem": _browser_asset_url(comp.get("emblem_local")) or comp.get("emblem_url") or "",
            }
            for comp in db_client.get_competitions_for_team(int(row.get("current_team_id")))
        ]
    person.update({
        "id": row.get("id"),
        "name": row.get("name"),
        "position": row.get("position"),
        "dateOfBirth": row.get("date_of_birth"),
        "nationality": row.get("nationality"),
        "shirtNumber": row.get("shirt_number"),
        "currentTeam": current_team,
    })
    return person


def _winner_from_scores(home_score: Any, away_score: Any) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "HOME_TEAM"
    if away_score > home_score:
        return "AWAY_TEAM"
    return "DRAW"


SCHEDULED_MATCH_STATUSES = {"SCHEDULED", "TIMED", "POSTPONED"}
LIVE_MATCH_STATUSES = {"IN_PLAY", "PAUSED"}
FINISHED_MATCH_STATUSES = {"FINISHED", "AWARDED"}


def _match_status(match: dict[str, Any] | None) -> str:
    return str(((match or {}).get("status") or "")).strip().upper()


def _is_finished_match_status(status: str) -> bool:
    return status in FINISHED_MATCH_STATUSES


def _is_live_match_status(status: str) -> bool:
    return status in LIVE_MATCH_STATUSES


def _is_scheduled_match_status(status: str) -> bool:
    return status in SCHEDULED_MATCH_STATUSES


def _match_sort_key(match: dict[str, Any]) -> tuple[str, int]:
    raw_id = match.get("id")
    try:
        match_id = int(raw_id)
    except (TypeError, ValueError):
        match_id = 0
    return ((match.get("utcDate") or ""), match_id)


def _sort_matches_chronologically(matches: list[dict[str, Any]], reverse: bool = False) -> list[dict[str, Any]]:
    return sorted(matches or [], key=_match_sort_key, reverse=reverse)


def _partition_matches(
    matches: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    scheduled: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    for match in _sort_matches_chronologically(matches):
        status = _match_status(match)
        if _is_finished_match_status(status):
            finished.append(match)
        elif _is_live_match_status(status):
            live.append(match)
        else:
            scheduled.append(match)
    return scheduled, live, finished


def _build_matchday_archive(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    archive: dict[int, dict[str, Any]] = {}
    for match in _sort_matches_chronologically(matches):
        raw_matchday = match.get("matchday")
        try:
            matchday = int(raw_matchday)
        except (TypeError, ValueError):
            continue
        row = archive.setdefault(matchday, {
            "matchday": matchday,
            "total": 0,
            "finished": 0,
            "live": 0,
            "scheduled": 0,
            "first_date": None,
            "last_date": None,
        })
        row["total"] += 1
        status = _match_status(match)
        if _is_finished_match_status(status):
            row["finished"] += 1
        elif _is_live_match_status(status):
            row["live"] += 1
        else:
            row["scheduled"] += 1
        utc_date = match.get("utcDate") or ""
        if utc_date:
            if not row["first_date"] or utc_date < row["first_date"]:
                row["first_date"] = utc_date
            if not row["last_date"] or utc_date > row["last_date"]:
                row["last_date"] = utc_date

    out: list[dict[str, Any]] = []
    for row in archive.values():
        if row["live"]:
            row["state"] = "live"
        elif row["finished"] == row["total"]:
            row["state"] = "complete"
        elif row["finished"]:
            row["state"] = "mixed"
        else:
            row["state"] = "scheduled"
        out.append(row)
    return sorted(out, key=lambda item: item["matchday"])


def _db_match_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("raw_json") or {}
    competition = payload.get("competition") or _db_competition_payload(row.get("competition_code") or "")
    if competition:
        competition = {
            "code": competition.get("code"),
            "name": competition.get("name"),
            "emblem": competition.get("emblem"),
        }
    home_team = payload.get("homeTeam") or {}
    away_team = payload.get("awayTeam") or {}
    home_summary = _db_team_summary(row.get("home_team_id"))
    away_summary = _db_team_summary(row.get("away_team_id"))
    home_team.update({k: v for k, v in home_summary.items() if v not in (None, "")})
    away_team.update({k: v for k, v in away_summary.items() if v not in (None, "")})

    score = payload.get("score") or {}
    full_time = score.get("fullTime") or {}
    half_time = score.get("halfTime") or {}
    extra_time = score.get("extraTime") or {}
    penalties = score.get("penalties") or {}
    full_time.setdefault("home", row.get("home_score"))
    full_time.setdefault("away", row.get("away_score"))
    half_time.setdefault("home", row.get("half_time_home"))
    half_time.setdefault("away", row.get("half_time_away"))
    extra_time.setdefault("home", row.get("extra_time_home"))
    extra_time.setdefault("away", row.get("extra_time_away"))
    penalties.setdefault("home", row.get("penalty_home"))
    penalties.setdefault("away", row.get("penalty_away"))
    score["fullTime"] = full_time
    score["halfTime"] = half_time
    score["extraTime"] = extra_time
    score["penalties"] = penalties
    score.setdefault("winner", _winner_from_scores(full_time.get("home"), full_time.get("away")))

    return {
        "id": row.get("id"),
        "competition": competition,
        "matchday": row.get("matchday"),
        "stage": row.get("stage"),
        "status": row.get("status"),
        "utcDate": row.get("utc_date"),
        "homeTeam": home_team,
        "awayTeam": away_team,
        "score": score,
        "venue": row.get("venue"),
        "referees": payload.get("referees") or [],
    }


def _db_match_payload(match_id: int) -> dict[str, Any] | None:
    row = db_client.get_match_by_id(match_id)
    if not row:
        return None
    return _db_match_row_to_payload(row)


def _db_matches_for_competition_payload(code: str, status: str | None = None) -> list[dict]:
    return [_db_match_row_to_payload(row) for row in db_client.get_matches_for_competition(code.upper(), status=status)]


def _db_team_matches_payload(team_id: int, status: str | None = None, limit: int | None = None) -> list[dict]:
    return [_db_match_row_to_payload(row) for row in db_client.get_team_matches(team_id, status=status, limit=limit)]


def _db_standings_payload(code: str) -> dict[str, Any]:
    rows = db_client.get_standings_for_competition(code.upper())
    if not rows:
        return {}
    team_map = {team.get("id"): team for team in db_client.get_teams_for_competition(code.upper())}
    table = []
    for row in rows:
        team = team_map.get(row.get("team_id")) or db_client.get_team_by_id(row.get("team_id"))
        table.append({
            "position": row.get("position"),
            "team": {
                "id": row.get("team_id"),
                "name": row.get("team_name") or (team or {}).get("name"),
                "shortName": (team or {}).get("short_name") or row.get("team_name"),
                "crest": _browser_asset_url((team or {}).get("crest_local")) or (team or {}).get("crest_url") or "",
            },
            "playedGames": row.get("played_games"),
            "won": row.get("won"),
            "draw": row.get("draw"),
            "lost": row.get("lost"),
            "goalsFor": row.get("goals_for"),
            "goalsAgainst": row.get("goals_against"),
            "goalDifference": row.get("goal_difference"),
            "points": row.get("points"),
            "form": row.get("form"),
        })
    return {"standings": [{"type": "TOTAL", "table": table}]}


def _db_scorers_payload(code: str, limit: int = 20) -> dict[str, Any]:
    rows = db_client.get_scorers_for_competition(code.upper(), limit=limit)
    if not rows:
        return {}
    team_map = {team.get("id"): team for team in db_client.get_teams_for_competition(code.upper())}
    scorers = []
    for row in rows:
        team = team_map.get(row.get("team_id")) or db_client.get_team_by_id(row.get("team_id"))
        scorers.append({
            "player": {"id": row.get("player_id"), "name": row.get("player_name")},
            "team": {
                "id": row.get("team_id"),
                "name": row.get("team_name") or (team or {}).get("name"),
                "shortName": (team or {}).get("short_name") or row.get("team_name"),
                "crest": _browser_asset_url((team or {}).get("crest_local")) or (team or {}).get("crest_url") or "",
            },
            "goals": row.get("goals"),
            "assists": row.get("assists"),
            "penalties": row.get("penalties"),
        })
    return {"scorers": scorers}


def _db_stat_card_entries(kind: str) -> list[dict]:
    codes = list(TOP_SCORER_LEAGUE_CODES)
    out: list[dict] = []
    for code in codes:
        league = _db_competition_payload(code)
        if kind == "cards":
            row = next(iter(db_client.get_top_cards(code, limit=1)), None)
            team = {}
            if row and row.get("team_name"):
                team = next((t for t in db_client.get_teams_for_competition(code) if (t.get("name") or "").lower() == row.get("team_name", "").lower()), {})
            if not row:
                continue
            out.append({
                "player": {"name": row.get("player_name")},
                "team": {
                    "id": team.get("id"),
                    "name": row.get("team_name") or team.get("name"),
                    "shortName": team.get("short_name") or row.get("team_name"),
                    "crest": _browser_asset_url(team.get("crest_local")) or team.get("crest_url") or "",
                },
                "league": league,
                "value": row.get("total_cards") or 0,
                "extra": {"yellow": row.get("yellow_cards") or 0, "red": row.get("red_cards") or 0},
            })
            continue
        rows = db_client.get_scorers_for_competition(code, limit=50)
        if kind == "assists":
            rows = [r for r in rows if (r.get("assists") or 0) > 0]
            rows.sort(key=lambda r: (r.get("assists") or 0, -(r.get("rank_position") or 9999)), reverse=True)
        row = rows[0] if rows else None
        if not row:
            continue
        team = db_client.get_team_by_id(row.get("team_id")) if row.get("team_id") else {}
        out.append({
            "player": {"id": row.get("player_id"), "name": row.get("player_name")},
            "team": {
                "id": row.get("team_id"),
                "name": row.get("team_name") or (team or {}).get("name"),
                "shortName": (team or {}).get("short_name") or row.get("team_name"),
                "crest": _browser_asset_url((team or {}).get("crest_local")) or (team or {}).get("crest_url") or "",
            },
            "league": league,
            "value": row.get("goals") if kind == "goals" else (row.get("assists") or 0),
            "extra": {"assists": row.get("assists") or 0, "goals": row.get("goals") or 0},
        })
    return out


def _load_competitions_listing() -> list[dict]:
    if _offline_mode_enabled():
        return _db_competitions_listing()
    try:
        return api_client.list_competitions()
    except FootballDataError:
        return _db_competitions_listing()


def _load_competition_payload(code: str) -> dict[str, Any] | None:
    if _offline_mode_enabled():
        return _db_competition_payload(code)
    try:
        return api_client.get_competition(code)
    except FootballDataError:
        return _db_competition_payload(code)


def _load_standings_payload(code: str) -> dict[str, Any]:
    if _offline_mode_enabled():
        return _db_standings_payload(code) or {}
    try:
        return api_client.get_standings(code)
    except FootballDataError:
        return _db_standings_payload(code) or {}


def _load_scorers_payload(code: str, limit: int = 20) -> dict[str, Any]:
    if _offline_mode_enabled():
        return _db_scorers_payload(code, limit=limit) or {}
    try:
        return api_client.get_scorers(code, limit=limit)
    except FootballDataError:
        return _db_scorers_payload(code, limit=limit) or {}


def _load_team_payload(team_id: int) -> dict[str, Any] | None:
    if _offline_mode_enabled():
        return _db_team_payload(team_id)
    try:
        return api_client.get_team(team_id)
    except FootballDataError:
        return _db_team_payload(team_id)


def _load_player_payload(person_id: int) -> dict[str, Any] | None:
    if _offline_mode_enabled():
        return _db_player_payload(person_id)
    try:
        return api_client.get_person(person_id)
    except FootballDataError:
        return _db_player_payload(person_id)


def _load_match_payload(match_id: int) -> dict[str, Any] | None:
    if _offline_mode_enabled():
        return _db_match_payload(match_id)
    try:
        return api_client.get_match(match_id)
    except FootballDataError:
        return _db_match_payload(match_id)


def _load_competition_matches_payload(code: str, **kwargs: Any) -> dict[str, Any]:
    status = kwargs.get("status")
    matchday = kwargs.get("matchday")
    date_from = kwargs.get("date_from")
    date_to = kwargs.get("date_to")
    if _offline_mode_enabled():
        if status == "SCHEDULED":
            matches = [m for m in _db_matches_for_competition_payload(code) if (m.get("status") or "").upper() in ("SCHEDULED", "TIMED", "POSTPONED")]
        elif status == "FINISHED":
            matches = [m for m in _db_matches_for_competition_payload(code) if (m.get("status") or "").upper() in ("FINISHED", "AWARDED")]
        else:
            matches = _db_matches_for_competition_payload(code, status=status)
        if matchday is not None:
            matches = [m for m in matches if m.get("matchday") == matchday]
        if date_from:
            matches = [m for m in matches if (m.get("utcDate") or "")[:10] >= date_from]
        if date_to:
            matches = [m for m in matches if (m.get("utcDate") or "")[:10] <= date_to]
        return {"matches": matches}
    try:
        return api_client.get_competition_matches(code, **kwargs)
    except FootballDataError:
        if status == "SCHEDULED":
            matches = [m for m in _db_matches_for_competition_payload(code) if (m.get("status") or "").upper() in ("SCHEDULED", "TIMED", "POSTPONED")]
        elif status == "FINISHED":
            matches = [m for m in _db_matches_for_competition_payload(code) if (m.get("status") or "").upper() in ("FINISHED", "AWARDED")]
        else:
            matches = _db_matches_for_competition_payload(code, status=status)
        if matchday is not None:
            matches = [m for m in matches if m.get("matchday") == matchday]
        if date_from:
            matches = [m for m in matches if (m.get("utcDate") or "")[:10] >= date_from]
        if date_to:
            matches = [m for m in matches if (m.get("utcDate") or "")[:10] <= date_to]
        return {"matches": matches}


def _load_team_matches_payload(team_id: int, **kwargs: Any) -> dict[str, Any]:
    status = kwargs.get("status")
    limit = kwargs.get("limit")
    if _offline_mode_enabled():
        if status == "SCHEDULED":
            matches = [m for m in _db_team_matches_payload(team_id) if (m.get("status") or "").upper() in ("SCHEDULED", "TIMED", "POSTPONED")]
        elif status == "FINISHED":
            matches = [m for m in _db_team_matches_payload(team_id) if (m.get("status") or "").upper() in ("FINISHED", "AWARDED")]
        else:
            matches = _db_team_matches_payload(team_id, status=status, limit=limit)
        if limit is not None:
            matches = matches[:limit]
        return {"matches": matches}
    try:
        return api_client.get_team_matches(team_id, **kwargs)
    except FootballDataError:
        if status == "SCHEDULED":
            matches = [m for m in _db_team_matches_payload(team_id) if (m.get("status") or "").upper() in ("SCHEDULED", "TIMED", "POSTPONED")]
        elif status == "FINISHED":
            matches = [m for m in _db_team_matches_payload(team_id) if (m.get("status") or "").upper() in ("FINISHED", "AWARDED")]
        else:
            matches = _db_team_matches_payload(team_id, status=status, limit=limit)
        if limit is not None:
            matches = matches[:limit]
        return {"matches": matches}


def _build_cached_odds_lookup_index(rows: list[dict], payload_key: str) -> dict[tuple[str, str], dict[str, Any]]:
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows or []:
        home_key = odds_client._team_key(row.get("home_team"))
        away_key = odds_client._team_key(row.get("away_team"))
        if not (home_key and away_key):
            continue
        pair = (home_key, away_key)
        if pair in idx:
            continue
        idx[pair] = {
            "home_team": row.get("home_team"),
            "away_team": row.get("away_team"),
            payload_key: row,
        }
    return idx


def _find_cached_odds_snapshot(
    competition_code: str | None,
    home_name: str | None,
    away_name: str | None,
    rows_cache: dict[str, list[dict]] | None = None,
    index_cache: dict[str, dict[tuple[str, str], dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    if not (competition_code and home_name and away_name):
        return None
    code = competition_code.upper()
    snap = db_client.get_odds_snapshot(code, home_name, away_name)
    if snap:
        return snap
    rows_cache = rows_cache if rows_cache is not None else {}
    index_cache = index_cache if index_cache is not None else {}
    if code not in index_cache:
        rows = rows_cache.get(code)
        if rows is None:
            rows = db_client.get_odds_snapshots_for_competition(code, limit=500)
            rows_cache[code] = rows
        index_cache[code] = _build_cached_odds_lookup_index(rows, "_snapshot")
    match = odds_client._lookup(index_cache.get(code, {}), home_name, away_name)
    return (match or {}).get("_snapshot")


def _find_cached_odds_market_raw(
    competition_code: str | None,
    home_name: str | None,
    away_name: str | None,
    rows_cache: dict[str, list[dict]] | None = None,
    index_cache: dict[str, dict[tuple[str, str], dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    if not (competition_code and home_name and away_name):
        return None
    code = competition_code.upper()
    cached = db_client.get_odds_market_raw(code, home_name, away_name)
    if cached:
        return cached
    rows_cache = rows_cache if rows_cache is not None else {}
    index_cache = index_cache if index_cache is not None else {}
    if code not in index_cache:
        rows = rows_cache.get(code)
        if rows is None:
            rows = db_client.get_odds_markets_for_competition(code, limit=500)
            rows_cache[code] = rows
        index_cache[code] = _build_cached_odds_lookup_index(rows, "_market_raw")
    match = odds_client._lookup(index_cache.get(code, {}), home_name, away_name)
    return (match or {}).get("_market_raw")


def _attach_cached_odds_to_matches(matches: list[dict]) -> None:
    rows_cache: dict[str, list[dict]] = {}
    index_cache: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for match in matches or []:
        if match.get("_odds"):
            continue
        competition_code = ((match.get("competition") or {}).get("code") or "").upper()
        home_name = (match.get("homeTeam") or {}).get("name")
        away_name = (match.get("awayTeam") or {}).get("name")
        if not (competition_code and home_name and away_name):
            continue
        snap = _find_cached_odds_snapshot(
            competition_code,
            home_name,
            away_name,
            rows_cache=rows_cache,
            index_cache=index_cache,
        )
        if not snap:
            continue
        match["_odds"] = {
            "home": snap.get("odds_home"),
            "draw": snap.get("odds_draw"),
            "away": snap.get("odds_away"),
            "bookmaker": snap.get("bookmaker"),
        }


def _enrich_matches_with_available_odds(matches: list[dict]) -> None:
    candidates = [m for m in (matches or []) if isinstance(m, dict)]
    if not candidates:
        return
    if not _offline_mode_enabled():
        live_candidates = [m for m in candidates if not _is_finished_match_status(_match_status(m))]
        if live_candidates:
            try:
                odds_client.enrich_matches_with_odds(live_candidates)
            except Exception:
                pass
    _attach_cached_odds_to_matches(candidates)


def _cached_markets_for_match(competition_code: str | None, home_name: str | None, away_name: str | None) -> dict[str, Any] | None:
    if not (competition_code and home_name and away_name):
        return None
    cached = _find_cached_odds_market_raw(competition_code.upper(), home_name, away_name)
    if not cached:
        return None
    event = cached.get("raw_json") or {}
    have: set[str] = set()
    bookmakers: set[str] = set()
    for bk in event.get("bookmakers", []) or []:
        if bk.get("title"):
            bookmakers.add(bk["title"])
        for market in bk.get("markets", []) or []:
            key = market.get("key")
            if key in odds_client.MARKET_LABELS:
                have.add(key)
    out = {
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "commence_time": event.get("commence_time"),
        "bookmakers": sorted(bookmakers),
        "available_markets": [k for k in ("h2h", "totals", "spreads", "btts") if k in have],
        "labels": odds_client.MARKET_LABELS,
    }
    if "h2h" in have:
        out["h2h"] = odds_client._organize_h2h(event)
    if "totals" in have:
        out["totals"] = odds_client._organize_totals(event)
    if "spreads" in have:
        out["spreads"] = odds_client._organize_spreads(event)
    if "btts" in have:
        out["btts"] = odds_client._organize_btts(event)
    return out


# ---------- Routes ----------

@app.route("/")
def home():
    from datetime import timedelta

    competitions = FREE_TIER_COMPETITIONS
    comp_emblem: dict[str, str | None] = {c["code"]: c.get("emblem") for c in FREE_TIER_COMPETITIONS}
    comp_name: dict[str, str | None] = {c["code"]: c.get("name") for c in FREE_TIER_COMPETITIONS}
    comp_lookup = {c["code"]: c for c in FREE_TIER_COMPETITIONS}
    top_codes = tuple(c["code"] for c in FREE_TIER_COMPETITIONS)
    qual_codes = ["PL", "PD", "BL1", "SA", "FL1"]
    yesterday_codes = ("PL", "PD", "SA", "BL1", "FL1")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    y_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    # ── Sort helper ────────────────────────────────────────────────────────────
    def _sort_key(m: dict) -> tuple:
        order = {"IN_PLAY": 0, "PAUSED": 0, "LIVE": 0, "TIMED": 1, "SCHEDULED": 1, "FINISHED": 2}
        return (order.get(m.get("status", ""), 3), m.get("utcDate") or "")

    # ── Parallel fetches ───────────────────────────────────────────────────────
    # All independent data sources are dispatched at once; we collect results
    # as they arrive so even slow sources don't hold up fast ones.

    def _fetch_today(code: str) -> tuple[str, list]:
        data = _load_competition_matches_payload(code, date_from=today_str, date_to=today_str)
        ms = data.get("matches") or []
        _attach_competition_to_matches(ms, {"code": code, "name": comp_name.get(code), "emblem": comp_emblem.get(code)})
        return (code, ms)

    def _fetch_scheduled(code: str) -> tuple[str, list]:
        data = _load_competition_matches_payload(code, status="SCHEDULED")
        ms = data.get("matches") or []
        _attach_competition_to_matches(ms, {"code": code, "name": comp_name.get(code), "emblem": comp_emblem.get(code)})
        return (code, ms)

    def _fetch_yesterday(code: str) -> tuple[str, list]:
        data = _load_competition_matches_payload(code, date_from=y_date, date_to=y_date)
        ms = [m for m in (data.get("matches") or []) if (m.get("status") or "").upper() in ("FINISHED", "AWARDED")]
        _attach_competition_to_matches(ms, {"code": code, "name": comp_name.get(code), "emblem": comp_emblem.get(code)})
        return (code, ms)

    def _fetch_standing(code: str) -> tuple[str, dict | None]:
        return (code, _load_standings_payload(code) or None)

    def _fetch_cl() -> dict | None:
        cl_data = _load_competition_matches_payload("CL", status="SCHEDULED")
        cl_ms = cl_data.get("matches") or []
        if cl_ms:
            m = cl_ms[0]
            _attach_competition_to_matches([m], {"code": "CL", "name": comp_name.get("CL"), "emblem": comp_emblem.get("CL")})
            return m
        return None

    def _fetch_all_comps() -> list:
        return _load_competitions_listing()

    def _fetch_news() -> tuple[list, str | None, str | None]:
        if _offline_mode_enabled():
            return (_decorate_news_articles(db_client.get_recent_news(limit=8)), None, None)
        try:
            articles = _decorate_news_articles(news_client.fetch_home_news(limit=8))
            if articles:
                _persist_news_articles(articles)
                return (articles, None, None)
        except NewsApiError as exc:
            cached = _decorate_news_articles(db_client.get_recent_news(limit=8))
            return (cached, str(exc), exc.kind)
        except Exception:
            pass
        return (_decorate_news_articles(db_client.get_recent_news(limit=8)), None, None)

    def _fetch_scorers() -> list:
        if _offline_mode_enabled():
            return _db_stat_card_entries("goals")
        try:
            return players_helpers.top_scorers_one_per_league()
        except Exception:
            return _db_stat_card_entries("goals")

    def _fetch_assists() -> list:
        if _offline_mode_enabled():
            return _db_stat_card_entries("assists")
        try:
            return players_helpers.top_assists_one_per_league()
        except Exception:
            return _db_stat_card_entries("assists")

    def _fetch_cards() -> list:
        if _offline_mode_enabled():
            return _db_stat_card_entries("cards")
        try:
            return players_helpers.top_cards_one_per_league()
        except Exception:
            return _db_stat_card_entries("cards")

    def _db_matches_for_codes(
        codes: tuple[str, ...] | list[str],
        *,
        date_str: str | None = None,
        finished_only: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        out: list[dict] = []
        for code in codes:
            ms = _db_matches_for_competition_payload(code)
            if finished_only:
                ms = [
                    m for m in ms
                    if (m.get("status") or "").upper() in ("FINISHED", "AWARDED")
                ]
            if date_str:
                ms = [m for m in ms if (m.get("utcDate") or "")[:10] == date_str]
            _attach_competition_to_matches(ms, {"code": code, "name": comp_name.get(code), "emblem": comp_emblem.get(code)})
            out.extend(ms)
        out.sort(key=lambda m: m.get("utcDate") or "", reverse=finished_only or bool(date_str))
        return out[:limit] if limit is not None else out

    # Dispatch everything in parallel; collect with a hard 8-second wall-clock
    # deadline so one slow API call never blocks the whole page render.
    import concurrent.futures as _cf

    _WALL = 8.0  # seconds before we give up on any remaining futures

    with ThreadPoolExecutor(max_workers=20) as pool:
        f_comps      = pool.submit(_fetch_all_comps)
        f_cl         = pool.submit(_fetch_cl)
        f_news       = pool.submit(_fetch_news)
        f_scorers    = pool.submit(_fetch_scorers)
        f_assists    = pool.submit(_fetch_assists)
        f_cards      = pool.submit(_fetch_cards)
        f_todays     = [pool.submit(_fetch_today, c) for c in top_codes]
        f_standings  = [pool.submit(_fetch_standing, c) for c in qual_codes]
        f_yesterdays = [pool.submit(_fetch_yesterday, c) for c in yesterday_codes]

        all_futures = (
            [f_comps, f_cl, f_news, f_scorers, f_assists, f_cards]
            + f_todays + f_standings + f_yesterdays
        )
        _cf.wait(all_futures, timeout=_WALL)

        # Safely read any future — return default if still running / failed
        def _get(fut, default):
            try:
                return fut.result(timeout=0)
            except Exception:
                return default

        # Collect today's matches
        todays: list[dict] = []
        for fut in f_todays:
            _, ms = _get(fut, ("", []))
            todays.extend(ms)

        # Collect standings
        standings_by_code: dict[str, dict] = {}
        for fut in f_standings:
            result = _get(fut, (None, None))
            code, data = result if result and len(result) == 2 else (None, None)
            if code and data:
                standings_by_code[code] = data

        # Collect yesterday's results
        yesterdays_list: list[dict] = []
        for fut in f_yesterdays:
            _, ms = _get(fut, ("", []))
            yesterdays_list.extend(ms)

        all_comps_list     = _get(f_comps,   [])
        cl_match           = _get(f_cl,      None)
        news_result        = _get(f_news,    ([], None, None))
        top_scorers_single = _get(f_scorers, [])
        top_assists_single = _get(f_assists, [])
        top_cards_single   = _get(f_cards,   [])

    # Inject form from DB for all qual standings (API often returns null form)
    try:
        for _code, _data in standings_by_code.items():
            _db_form = db_client.get_team_form_from_matches(_code, last_n=5)
            for _st in (_data.get("standings") or []):
                for _row in (_st.get("table") or []):
                    if not _row.get("form"):
                        _tid = (_row.get("team") or {}).get("id")
                        if _tid and _tid in _db_form:
                            _row["form"] = _db_form[_tid]
    except Exception:
        pass

    # ── Enrich competition list ────────────────────────────────────────────────
    if not all_comps_list:
        all_comps_list = _db_competitions_listing()

    for code in qual_codes:
        if code not in standings_by_code:
            cached = _db_standings_payload(code)
            if cached:
                standings_by_code[code] = cached

    if not top_scorers_single:
        top_scorers_single = _db_stat_card_entries("goals")
    if not top_assists_single:
        top_assists_single = _db_stat_card_entries("assists")
    if not top_cards_single:
        top_cards_single = _db_stat_card_entries("cards")

    if not cl_match:
        cached_cl = _db_matches_for_codes(["CL"], limit=1)
        cl_match = cached_cl[0] if cached_cl else None

    home_news, home_news_error, home_news_kind = news_result
    if not home_news:
        home_news = _decorate_news_articles(db_client.get_recent_news(limit=8))

    enriched: list[dict] = []
    by_code = {c.get("code"): c for c in all_comps_list if c.get("code")}
    for c in competitions:
        full = by_code.get(c["code"], {})
        enriched.append({
            **c,
            "id": full.get("id"),
            "currentSeason": full.get("currentSeason"),
            "area": full.get("area", {}),
            "emblem": full.get("emblem") or c.get("emblem"),
        })

    # ── Featured matches ───────────────────────────────────────────────────────
    featured: list[dict] = []
    is_today_view = False
    if todays:
        todays.sort(key=_sort_key)
        featured = todays[:12]
        is_today_view = True

    MIN_FEATURED = 5
    if len(featured) < MIN_FEATURED:
        seen_ids = {m.get("id") for m in featured}
        # Fetch scheduled matches in parallel for the fallback
        with ThreadPoolExecutor(max_workers=8) as pool2:
            f_sched = {pool2.submit(_fetch_scheduled, c): c for c in top_codes}
            upcoming: list[dict] = []
            for fut in as_completed(f_sched):
                _, ms = fut.result()
                upcoming.extend(ms)
        upcoming.sort(key=_sort_key)
        for m in upcoming:
            if len(featured) >= max(MIN_FEATURED, 8):
                break
            if m.get("id") in seen_ids:
                continue
            featured.append(m)
            seen_ids.add(m.get("id"))

    _enrich_matches_with_available_odds(featured)
    if cl_match is not None:
        _enrich_matches_with_available_odds([cl_match])

    # ── Team positions ─────────────────────────────────────────────────────────
    needed_codes: set[str] = set()
    for m in featured + ([cl_match] if cl_match else []):
        c = (m.get("competition") or {}).get("code")
        if c:
            needed_codes.add(c)
    team_positions = _team_positions_for_codes(sorted(needed_codes)) if needed_codes else {}

    # ── News ──────────────────────────────────────────────────────────────────
    home_news, home_news_error, home_news_kind = news_result

    # ── Stats hub ─────────────────────────────────────────────────────────────
    stats_league_codes: list[str] = []
    seen_stat_codes: set[str] = set()
    for lst in (top_scorers_single, top_assists_single, top_cards_single):
        for entry in lst:
            code = (entry.get("league") or {}).get("code")
            if code and code not in seen_stat_codes:
                seen_stat_codes.add(code)
                stats_league_codes.append(code)
    stats_leagues = [
        next((c for c in FREE_TIER_COMPETITIONS if c["code"] == code), {"code": code, "name": code, "emblem": ""})
        for code in stats_league_codes
    ]

    # ── Qual standings carousel ────────────────────────────────────────────────
    qual_standings: list[dict] = []
    for code in qual_codes:
        data = standings_by_code.get(code)
        if not data:
            continue
        applied = annotate_standings_qualification(data, code)
        rows: list[dict] = []
        for st in (data.get("standings") or []):
            if (st.get("type") or "").upper() != "TOTAL":
                continue
            for row in (st.get("table") or [])[:10]:
                rows.append(row)
            break
        if rows:
            meta = comp_lookup.get(code, {"code": code, "name": code, "emblem": ""})
            qual_standings.append({
                "code": code,
                "name": meta.get("name") or code,
                "emblem": meta.get("emblem") or "",
                "country": meta.get("country") or "",
                "rows": rows,
                "legend": applied,
            })

    # ── Hero cards ─────────────────────────────────────────────────────────────
    hero_top_scorer: dict | None = None
    if top_scorers_single:
        try:
            hero_top_scorer = max(top_scorers_single, key=lambda e: (e.get("value") or 0))
        except ValueError:
            hero_top_scorer = top_scorers_single[0]

    hero_pl_leader: dict | None = None
    pl_block = next((q for q in qual_standings if q["code"] == "PL"), None)
    pl_top_row = pl_block["rows"][0] if (pl_block and pl_block["rows"]) else None
    if pl_top_row is not None:
        hero_pl_leader = {
            "team": pl_top_row.get("team") or {},
            "points": pl_top_row.get("points") or 0,
            "played": pl_top_row.get("playedGames") or 0,
            "league_name": "Premier League",
        }

    # ── Yesterday's results ────────────────────────────────────────────────────
    yesterday_results_mode = "yesterday"
    if not yesterdays_list:
        yesterdays_list = _db_matches_for_codes(list(yesterday_codes), date_str=y_date, finished_only=True, limit=12)
    if not yesterdays_list:
        yesterdays_list = _db_matches_for_codes(list(yesterday_codes), finished_only=True, limit=12)
        if yesterdays_list:
            yesterday_results_mode = "recent"
    yesterdays_list.sort(key=lambda m: m.get("utcDate") or "", reverse=True)
    yesterday_matches = yesterdays_list[:12]

    return render_template(
        "home.html",
        competitions=enriched,
        featured_matches=featured,
        featured_is_today=is_today_view,
        hero_match=cl_match,
        hero_top_scorer=hero_top_scorer,
        hero_pl_leader=hero_pl_leader,
        qual_standings=qual_standings,
        yesterday_matches=yesterday_matches,
        yesterday_results_mode=yesterday_results_mode,
        home_news=home_news,
        home_news_error=home_news_error,
        home_news_kind=home_news_kind,
        team_positions=team_positions,
        top_scorers_single=top_scorers_single,
        top_assists_single=top_assists_single,
        top_cards_single=top_cards_single,
        stats_leagues=stats_leagues,
    )


@app.route("/fixtures")
def fixtures_page():
    """Aggregate scheduled fixtures across the free-tier competitions, with
    optional team and date filters applied client-side after fetching."""
    from datetime import datetime, timezone, timedelta

    selected_team = (request.args.get("team") or "").strip()
    selected_competition = (request.args.get("competition") or "").strip().upper()
    date_from_str = (request.args.get("date_from") or "").strip()
    date_to_str = (request.args.get("date_to") or "").strip()

    # Pull a competition emblem map for nice rendering on each card.
    comp_emblem: dict[str, str] = {c["code"]: c.get("emblem") for c in FREE_TIER_COMPETITIONS}
    comp_name: dict[str, str] = {c["code"]: c.get("name") for c in FREE_TIER_COMPETITIONS}
    for c in _load_competitions_listing():
        code = c.get("code")
        if code:
            comp_emblem[code] = c.get("emblem") or comp_emblem.get(code)
            comp_name[code] = c.get("name") or comp_name.get(code)

    all_matches: list[dict] = []
    teams_index: dict[int, dict] = {}

    codes_to_fetch = [selected_competition] if selected_competition else [c["code"] for c in FREE_TIER_COMPETITIONS]
    for code in codes_to_fetch:
        data = _load_competition_matches_payload(code, status="SCHEDULED")
        ms = data.get("matches", []) or []
        _attach_competition_to_matches(
            ms,
            {"code": code, "name": comp_name.get(code), "emblem": comp_emblem.get(code)},
        )
        for m in ms:
            for side in ("homeTeam", "awayTeam"):
                t = m.get(side) or {}
                if t.get("id"):
                    teams_index.setdefault(t["id"], {
                        "id": t["id"],
                        "name": t.get("name") or t.get("shortName") or "",
                        "shortName": t.get("shortName") or t.get("name") or "",
                        "crest": t.get("crest"),
                    })
        all_matches.extend(ms)

    # Date filtering
    def parse_date(s: str) -> datetime | None:
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    df = parse_date(date_from_str)
    dt = parse_date(date_to_str)
    if dt:
        dt = dt + timedelta(days=1)  # inclusive end-of-day

    filtered: list[dict] = []
    team_id_int: int | None = None
    if selected_team:
        try:
            team_id_int = int(selected_team)
        except ValueError:
            team_id_int = None

    for m in all_matches:
        if team_id_int is not None:
            home_id = (m.get("homeTeam") or {}).get("id")
            away_id = (m.get("awayTeam") or {}).get("id")
            if home_id != team_id_int and away_id != team_id_int:
                continue
        if df or dt:
            try:
                d = datetime.fromisoformat((m.get("utcDate") or "").replace("Z", "+00:00"))
            except Exception:
                d = None
            if d is None:
                continue
            if df and d < df:
                continue
            if dt and d >= dt:
                continue
        filtered.append(m)

    # Sort + group by day (YYYY-MM-DD)
    filtered.sort(key=lambda x: x.get("utcDate") or "")
    grouped: list[dict] = []
    current_day: str | None = None
    bucket: list[dict] = []
    for m in filtered:
        day = (m.get("utcDate") or "")[:10]
        if day != current_day:
            if bucket:
                grouped.append({"day": current_day, "matches": bucket})
            current_day = day
            bucket = []
        bucket.append(m)
    if bucket:
        grouped.append({"day": current_day, "matches": bucket})

    teams_list = sorted(teams_index.values(), key=lambda t: (t.get("name") or "").lower())
    _enrich_matches_with_available_odds(filtered)

    return render_template(
        "fixtures.html",
        groups=grouped,
        total=len(filtered),
        teams=teams_list,
        competitions=FREE_TIER_COMPETITIONS,
        selected_team=selected_team,
        selected_competition=selected_competition,
        date_from=date_from_str,
        date_to=date_to_str,
    )


@app.route("/competitions")
def competitions_page():
    comps = _load_competitions_listing()

    free_codes = {c["code"] for c in FREE_TIER_COMPETITIONS}
    for c in comps:
        c["_free"] = c.get("code") in free_codes

    comps.sort(key=lambda c: (not c.get("_free"), c.get("area", {}).get("name", ""), c.get("name", "")))
    return render_template("competitions.html", competitions=comps)


def _decorate_news_articles(articles: list[dict], default_code: str | None = None) -> list[dict]:
    out: list[dict] = []
    for article in articles or []:
        if not isinstance(article, dict):
            continue
        image_url = article.get("image") or article.get("image_url") or ""
        image_local = article.get("image_local") or ""
        if not image_local and image_url:
            try:
                image_local = db_client.get_local_image(image_url) or ""
            except Exception:
                image_local = ""
        code = (default_code or article.get("competition_code") or "").upper() or None
        if code:
            meta = news_client.competition_meta_for_code(code)
            out.append({
                **article,
                "image_local": image_local,
                "competition_code": meta.get("code"),
                "competition_name": meta.get("name"),
                "competition_emblem": meta.get("emblem"),
            })
        else:
            out.append({
                **article,
                "image_local": image_local,
            })
    return out


def _persist_news_articles(articles: list[dict]) -> None:
    for article in articles or []:
        if not isinstance(article, dict):
            continue
        code = (article.get("competition_code") or "").upper()
        if not code:
            continue
        try:
            db_client.upsert_news_article(
                article,
                code,
                league_id=news_client.league_id_for_competition(code),
            )
        except Exception:
            pass


def _db_search_index(query: str, limit_per_kind: int = 6) -> dict[str, list[dict]]:
    q = (query or "").strip()
    if len(q) < 2:
        return {"competitions": [], "teams": [], "players": []}

    competitions = [{
        "code": row.get("code"),
        "name": row.get("name"),
        "country": row.get("area_name") or "",
        "emblem": _browser_asset_url(row.get("emblem_local")) or row.get("emblem_url") or "",
    } for row in db_client.search_competitions(q, limit=limit_per_kind)]

    teams = [{
        "id": row.get("id"),
        "name": row.get("name"),
        "shortName": row.get("short_name") or row.get("name"),
        "tla": row.get("tla"),
        "crest": _browser_asset_url(row.get("crest_local")) or row.get("crest_url") or "",
        "league": {
            "code": row.get("competition_code"),
            "name": row.get("competition_name") or row.get("competition_code") or "",
        },
        "_competition": row.get("competition_name") or row.get("competition_code") or "",
    } for row in db_client.search_teams(q, limit=limit_per_kind)]

    players = [{
        "id": row.get("id"),
        "name": row.get("name"),
        "position": row.get("position") or "",
        "_photo": row.get("photo_url") or "",
        "_photo_local": _browser_asset_url(row.get("photo_local")) or "",
        "team": {
            "id": row.get("current_team_id"),
            "name": row.get("team_name") or row.get("current_team_name") or "",
            "shortName": row.get("team_short_name") or row.get("team_name") or row.get("current_team_name") or "",
            "crest": _browser_asset_url(row.get("crest_local")) or row.get("crest_url") or "",
        },
        "league": {
            "code": row.get("competition_code"),
            "name": row.get("competition_name") or row.get("competition_code") or "",
            "emblem": _browser_asset_url(row.get("competition_emblem_local")) or row.get("competition_emblem_url") or "",
        },
    } for row in db_client.search_players(q, limit=limit_per_kind)]

    return {"competitions": competitions, "teams": teams, "players": players}


def _search_index_payload(query: str, limit_per_kind: int = 6) -> dict[str, list[dict]]:
    cached = _db_search_index(query, limit_per_kind=limit_per_kind)
    total = sum(len(cached.get(kind, [])) for kind in ("competitions", "teams", "players"))
    if total or _offline_mode_enabled():
        return cached
    live = players_helpers.search_index(query, limit_per_kind=limit_per_kind)
    for team in live.get("teams", []):
        team["_competition"] = (team.get("league") or {}).get("name") or ""
    return live


@app.route("/competition/<code>")
def competition_detail(code: str):
    code = code.upper()
    competition = _load_competition_payload(code)
    if not competition:
        abort(404)

    standings_data: dict = {}
    scorers_data: dict = {}
    teams_data: dict = {}
    matches_scheduled: list = []
    matches_finished: list = []

    standings_data = _load_standings_payload(code)
    if not standings_data:
        standings_data = {"_error": "No cached standings available."}

    scorers_data = _load_scorers_payload(code, limit=20)
    if not scorers_data:
        scorers_data = {"_error": "No cached scorers available."}

    if _offline_mode_enabled():
        teams = db_client.get_teams_for_competition(code)
        if teams:
            teams_data = {
                "competition": {"code": code, "name": competition.get("name"), "emblem": competition.get("emblem")},
                "teams": [
                    {
                        "id": t.get("id"),
                        "name": t.get("name"),
                        "shortName": t.get("short_name") or t.get("name"),
                        "crest": _browser_asset_url(t.get("crest_local")) or t.get("crest_url") or "",
                        "tla": t.get("tla"),
                        "area": {"name": t.get("area_name") or ""},
                    }
                    for t in teams
                ],
            }
        else:
            teams_data = {"_error": "No cached teams available."}
    else:
        try:
            teams_data = api_client.get_competition_teams(code)
        except FootballDataError as exc:
            teams = db_client.get_teams_for_competition(code)
            if teams:
                teams_data = {
                    "competition": {"code": code, "name": competition.get("name"), "emblem": competition.get("emblem")},
                    "teams": [
                        {
                            "id": t.get("id"),
                            "name": t.get("name"),
                            "shortName": t.get("short_name") or t.get("name"),
                            "crest": _browser_asset_url(t.get("crest_local")) or t.get("crest_url") or "",
                            "tla": t.get("tla"),
                            "area": {"name": t.get("area_name") or ""},
                        }
                        for t in teams
                    ],
                }
            else:
                teams_data = {"_error": str(exc)}

    all_matches = _load_competition_matches_payload(code).get("matches", []) or []
    all_matches = _sort_matches_chronologically(all_matches)
    open_matches = [m for m in all_matches if not _is_finished_match_status(_match_status(m))]
    finished_matches_all = [m for m in all_matches if _is_finished_match_status(_match_status(m))]
    matches_scheduled = open_matches[:30]
    matches_finished = list(reversed(finished_matches_all[-30:]))
    matchday_archive = _build_matchday_archive(all_matches)

    _attach_competition_to_matches(matches_scheduled, {"code": code, "name": competition.get("name")})
    _attach_competition_to_matches(matches_finished, {"code": code, "name": competition.get("name")})
    _enrich_matches_with_available_odds(matches_scheduled)

    # Annotate standings rows with their European/relegation qualification class
    qual_legend = annotate_standings_qualification(standings_data, code)

    # Inject form from DB match results when the API returns null form values
    try:
        db_form = db_client.get_team_form_from_matches(code, last_n=5)
        for st in (standings_data.get("standings") or []):
            for row in (st.get("table") or []):
                if not row.get("form"):
                    tid = (row.get("team") or {}).get("id")
                    if tid and tid in db_form:
                        row["form"] = db_form[tid]
    except Exception:
        pass

    # Pull a couple of news articles for the competition (best-effort).
    news_articles: list = []
    news_error: str | None = None
    news_error_kind: str | None = None
    try:
        if _offline_mode_enabled():
            raise NewsApiError("Offline mode enabled.", kind="network")
        news_articles = _decorate_news_articles(news_client.fetch_news_for_competition(code)[:6], code)
        if news_articles:
            _persist_news_articles(news_articles)
    except NewsApiError as exc:
        news_error = str(exc)
        news_error_kind = exc.kind
        news_articles = _decorate_news_articles(db_client.get_news_for_competition(code, limit=6), code)
        if news_articles:
            news_error = None
            news_error_kind = None

    # Build the assists-leaderboard from the same scorers payload (free).
    try:
        assists_table = players_helpers.assists_table_for_league(code, limit=20)
    except Exception:
        assists_table = []
    if not assists_table and (scorers_data.get("scorers") or []):
        assists_table = sorted(
            [s for s in scorers_data.get("scorers", []) if (s.get("assists") or 0) > 0],
            key=lambda s: (s.get("assists") or 0, s.get("goals") or 0),
            reverse=True,
        )[:20]
    # Most-cards leaderboard via apifootball.com (best-effort).
    try:
        cards_table = players_helpers.cards_table_for_league(code, limit=20)
    except Exception:
        cards_table = []
    if not cards_table:
        cards_table = [
            {
                "player": {"name": row.get("player_name")},
                "team": {"name": row.get("team_name")},
                "yellow": row.get("yellow_cards") or 0,
                "red": row.get("red_cards") or 0,
                "total": row.get("total_cards") or 0,
            }
            for row in db_client.get_top_cards(code, limit=20)
        ]

    return render_template(
        "competition.html",
        competition=competition,
        standings_data=standings_data,
        scorers_data=scorers_data,
        teams_data=teams_data,
        matches_scheduled=matches_scheduled,
        matches_finished=matches_finished,
        all_matches_total=len(all_matches),
        finished_matches_total=len(finished_matches_all),
        scheduled_matches_total=len(open_matches),
        matchday_archive=matchday_archive,
        qual_legend=qual_legend,
        news_articles=news_articles,
        news_error=news_error,
        news_error_kind=news_error_kind,
        assists_table=assists_table,
        cards_table=cards_table,
    )


@app.route("/news")
@app.route("/news/<code>")
def news_page(code: str | None = None):
    code = code.upper() if code else None
    news_competitions = [c for c in FREE_TIER_COMPETITIONS if news_client.league_id_for_competition(c["code"])]
    articles: list = []
    fetched_articles: list = []
    error: str | None = None
    error_kind: str | None = None
    try:
        if _offline_mode_enabled():
            raise NewsApiError("Offline mode enabled.", kind="network")
        if code:
            fetched_articles = _decorate_news_articles(news_client.fetch_news_for_competition(code, page=1), code)
        else:
            fetched_articles = _decorate_news_articles(news_client.fetch_catalog_news(
                codes=[c["code"] for c in news_competitions],
                per_competition=12,
                limit=240,
            ))
        if fetched_articles:
            _persist_news_articles(fetched_articles)
    except NewsApiError as exc:
        error = str(exc)
        error_kind = exc.kind
    if code:
        articles = _decorate_news_articles(db_client.get_news_for_competition(code, limit=None), code)
    else:
        articles = _decorate_news_articles(db_client.get_recent_news(limit=None))
    if articles:
        error = None
        error_kind = None
    elif fetched_articles:
        articles = fetched_articles

    # Download article images locally in background (best effort, non-blocking).
    def _cache_news_images(arts: list) -> None:
        for art in arts:
            img = art.get("image") or art.get("image_url")
            article_key = news_client.article_external_id(art)
            article_code = (art.get("competition_code") or "news").upper()
            article_id = f"{article_code}_{article_key}" if article_key else ""
            if img and article_id:
                try:
                    local = data_importer.download_news_image(str(article_id), img)
                    if local:
                        art["image_local"] = local
                        db_client.set_news_image_local(art.get("url") or "", local)
                except Exception:
                    pass

    cache_targets = fetched_articles or [art for art in articles if not art.get("image_local")][:60]
    if cache_targets and not _offline_mode_enabled():
        import threading as _threading
        _threading.Thread(target=_cache_news_images, args=(list(cache_targets),), daemon=True).start()
        _cache_news_images(articles)

    return render_template(
        "news.html",
        articles=articles,
        active_code=code,
        news_competitions=news_competitions,
        error=error,
        error_kind=error_kind,
    )


@app.route("/team/<int:team_id>")
def team_detail(team_id: int):
    team = _load_team_payload(team_id)
    if not team:
        abort(404)
    cached_team_matches = _db_team_matches_payload(team_id)
    team_match_archive_source = "db" if cached_team_matches else "live-preview"
    if cached_team_matches:
        all_team_matches = _sort_matches_chronologically(cached_team_matches)
    else:
        preview_upcoming = (_load_team_matches_payload(team_id, status="SCHEDULED", limit=10).get("matches", []) or [])[:10]
        preview_recent = (_load_team_matches_payload(team_id, status="FINISHED", limit=10).get("matches", []) or [])
        merged: dict[str, dict[str, Any]] = {}
        for match in preview_upcoming + preview_recent:
            key = str(match.get("id") or f"{match.get('utcDate')}:{(match.get('homeTeam') or {}).get('id')}:{(match.get('awayTeam') or {}).get('id')}")
            merged[key] = match
        all_team_matches = _sort_matches_chronologically(list(merged.values()))

    upcoming = [m for m in all_team_matches if not _is_finished_match_status(_match_status(m))]
    recent = [m for m in all_team_matches if _is_finished_match_status(_match_status(m))]
    recent = list(reversed(recent))
    _enrich_matches_with_available_odds(upcoming + recent)
    team_match_summary = {
        "source": team_match_archive_source,
        "total": len(all_team_matches),
        "played": len(recent),
        "upcoming": len(upcoming),
        "competitions": len({
            ((m.get("competition") or {}).get("code") or "").upper()
            for m in all_team_matches
            if (m.get("competition") or {}).get("code")
        }),
        "matchdays": len({m.get("matchday") for m in all_team_matches if m.get("matchday") is not None}),
    }

    # League-table position(s) for this team across its tracked competitions,
    # plus home/away splits where the standings call returns them.
    positions: list[dict[str, Any]] = []
    home_away_form: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for c in (team.get("runningCompetitions") or []):
        code = c.get("code")
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        standings_data = _load_standings_payload(code)
        haf_entry: dict[str, Any] = {"competition": {"code": code, "name": c.get("name"), "emblem": c.get("emblem")}}
        for st in (standings_data.get("standings") or []):
            stype = (st.get("type") or "").upper()
            for row in (st.get("table") or []):
                if (row.get("team") or {}).get("id") != team_id:
                    continue
                payload = {
                    "position": row.get("position"),
                    "played": row.get("playedGames"),
                    "won": row.get("won"),
                    "draw": row.get("draw"),
                    "lost": row.get("lost"),
                    "points": row.get("points"),
                    "goalsFor": row.get("goalsFor"),
                    "goalsAgainst": row.get("goalsAgainst"),
                    "goalDifference": row.get("goalDifference"),
                    "form": row.get("form"),
                }
                if stype == "TOTAL":
                    positions.append({
                        "competition": {"code": code, "name": c.get("name"), "emblem": c.get("emblem")},
                        "group": st.get("group"),
                        **payload,
                    })
                elif stype == "HOME":
                    haf_entry["home"] = payload
                elif stype == "AWAY":
                    haf_entry["away"] = payload
                break
        if "home" in haf_entry or "away" in haf_entry:
            home_away_form.append(haf_entry)
    if not positions:
        for c in (team.get("runningCompetitions") or []):
            code = c.get("code")
            if not code:
                continue
            for row in db_client.get_standings_for_competition(code):
                if row.get("team_id") != team_id:
                    continue
                positions.append({
                    "competition": {"code": code, "name": c.get("name"), "emblem": c.get("emblem")},
                    "group": None,
                    "position": row.get("position"),
                    "played": row.get("played_games"),
                    "won": row.get("won"),
                    "draw": row.get("draw"),
                    "lost": row.get("lost"),
                    "points": row.get("points"),
                    "goalsFor": row.get("goals_for"),
                    "goalsAgainst": row.get("goals_against"),
                    "goalDifference": row.get("goal_difference"),
                    "form": row.get("form"),
                })
                break
    if not home_away_form:
        cached_form = db_client.get_team_form_db(team_id)
        if cached_form and team.get("runningCompetitions"):
            home_rows = [r for r in cached_form if (r.get("venue") or "").lower() == "home"]
            away_rows = [r for r in cached_form if (r.get("venue") or "").lower() == "away"]
            if home_rows or away_rows:
                def _form_block(rows: list[dict]) -> dict[str, Any] | None:
                    if not rows:
                        return None
                    won = sum(1 for r in rows if (r.get("result") or "").upper().startswith("W"))
                    draw = sum(1 for r in rows if (r.get("result") or "").upper().startswith("D"))
                    lost = sum(1 for r in rows if (r.get("result") or "").upper().startswith("L"))
                    gf = sum(int(r.get("goals_for") or 0) for r in rows)
                    ga = sum(int(r.get("goals_against") or 0) for r in rows)
                    return {
                        "played": len(rows),
                        "won": won,
                        "draw": draw,
                        "lost": lost,
                        "goalsFor": gf,
                        "goalsAgainst": ga,
                        "points": won * 3 + draw,
                    }
                home_away_form.append({
                    "competition": team.get("runningCompetitions")[0],
                    "home": _form_block(home_rows),
                    "away": _form_block(away_rows),
                })

    # Build a position lookup for the opponents on the upcoming/recent fixtures.
    opp_codes: set[str] = set()
    for m in upcoming + recent:
        c = (m.get("competition") or {}).get("code")
        if c:
            opp_codes.add(c)
    team_positions = _team_positions_for_codes(sorted(opp_codes)) if opp_codes else {}

    # Best-effort photo enrichment via API-Football. Silently degrades when the
    # key is missing or the user has not subscribed yet on RapidAPI.
    if not _offline_mode_enabled():
        try:
            api_football.attach_coach_photo(team)
            api_football.attach_player_photos(team)
        except Exception:
            pass

    # Merge locally-cached crest path from DB (for static serving).
    try:
        local_assets = db_client.get_team_local_assets(team_id)
        team.update(local_assets)
    except Exception:
        pass

    try:
        player_locals = db_client.get_player_photo_locals([
            p.get("id") for p in (team.get("squad") or []) if isinstance(p.get("id"), int)
        ])
        for player in (team.get("squad") or []):
            if isinstance(player, dict):
                player["_photo_local"] = player_locals.get(player.get("id"), "")
    except Exception:
        pass

    # Club color accent — drives the --team-accent CSS variable on team.html.
    team["_accent"] = parse_club_accent(team.get("clubColors"))

    # Head-to-head vs the next scheduled opponent (best effort).
    next_match = next((m for m in upcoming if _is_scheduled_match_status(_match_status(m))), None)
    if not next_match and upcoming:
        next_match = upcoming[0]
    h2h: list[dict[str, Any]] = []
    next_opponent: dict[str, Any] | None = None
    if next_match:
        home_t = next_match.get("homeTeam") or {}
        away_t = next_match.get("awayTeam") or {}
        is_home = (home_t.get("id") == team_id)
        opp = away_t if is_home else home_t
        if opp.get("name"):
            next_opponent = {
                "id": opp.get("id"),
                "name": opp.get("name"),
                "shortName": opp.get("shortName") or opp.get("name"),
                "crest": opp.get("crest"),
                "is_home_for_team": is_home,
                "match": next_match,
            }
            try:
                h2h = api_football.get_h2h(team.get("name") or "", opp.get("name"), limit=6)
            except Exception:
                h2h = db_client.get_h2h_records(team.get("name") or "", opp.get("name") or "", limit=6)

    # Team news — best effort, only fetched if the news provider has a key.
    team_news: list[dict[str, Any]] = []
    if news_client.has_news_api() and not _offline_mode_enabled():
        try:
            team_news = news_client.fetch_news_by_team(team.get("name") or "")[:12]
        except Exception:
            team_news = []
    if not team_news:
        comp_codes = [c.get("code") for c in (team.get("runningCompetitions") or []) if c.get("code")]
        for code in comp_codes:
            cached_articles = _decorate_news_articles(db_client.get_news_for_competition(code, limit=18), code)
            for article in cached_articles:
                hay = f"{article.get('title', '')} {article.get('summary', '')}".lower()
                if (team.get("name") or "").lower() in hay:
                    team_news.append(article)
        team_news = team_news[:12]

    # Transfer news — filter team news for transfer-related stories.
    _TRANSFER_KW = {
        "transfer", "signing", "sign", "deal", "bid", "linked", "loan",
        "fee", "move", "joins", "bought", "sold", "sell", "buy", "target",
        "want", "interested", "negotiate", "agreement", "window",
    }
    transfer_news = [
        n for n in team_news
        if any(kw in (n.get("title") or "").lower() + (n.get("description") or "").lower()
               for kw in _TRANSFER_KW)
    ]

    return render_template(
        "team.html",
        team=team,
        upcoming=upcoming,
        recent=recent,
        positions=positions,
        team_positions=team_positions,
        home_away_form=home_away_form,
        team_match_summary=team_match_summary,
        next_opponent=next_opponent,
        h2h=h2h,
        team_news=team_news[:8],
        transfer_news=transfer_news[:8],
    )


@app.route("/player/<int:person_id>")
def player_detail(person_id: int):
    """Standalone profile page for a single player."""
    try:
        person = _load_player_payload(person_id)
    except FootballDataError as exc:
        person = _db_player_payload(person_id)
        if not person:
            return render_template("player.html", person=None, error=str(exc), profile={}, news=[]), 404
    if not person:
        abort(404)

    # Enrich with apifootball lookup (photo, height, market value, age, country).
    current_team = person.get("currentTeam") or {}
    profile: dict[str, Any] = {}
    try:
        profile = db_client.get_player_profile(person.get("name") or "", current_team.get("name")) or {}
    except Exception:
        profile = {}
    if not profile:
        cached_player = db_client.get_player_by_id(person_id)
        if cached_player:
            profile = {
                "photo": cached_player.get("photo_url"),
                "photo_local": cached_player.get("photo_local"),
                "country": cached_player.get("nationality"),
                "position": cached_player.get("position"),
                "shirt_number": cached_player.get("shirt_number"),
                "shirt": cached_player.get("shirt_number"),
            }
    if not _offline_mode_enabled():
        try:
            live_profile = api_football.find_player_profile(person.get("name") or "", current_team.get("name"))
            if live_profile:
                profile.update({k: v for k, v in live_profile.items() if v not in (None, "")})
        except Exception:
            pass

    try:
        local_photo = db_client.get_player_photo_locals([person_id]).get(person_id)
        if local_photo:
            profile["photo_local"] = local_photo
    except Exception:
        pass

    if not _offline_mode_enabled() and not profile.get("photo_local") and profile.get("photo"):
        try:
            local_photo = db_client.get_local_image(profile["photo"])
            if not local_photo:
                local_photo = data_importer.download_player_photo(
                    person.get("name") or "player",
                    person_id,
                    profile["photo"],
                    league_name=((current_team.get("runningCompetitions") or [{}])[0].get("name") or ""),
                    team_name=current_team.get("name") or current_team.get("shortName") or "",
                )
            if local_photo:
                profile["photo_local"] = local_photo
        except Exception:
            pass

    # Player news feed (best effort).
    news: list[dict[str, Any]] = []
    if news_client.has_news_api() and not _offline_mode_enabled():
        try:
            news = news_client.fetch_news_by_player(person.get("name") or "")[:8]
        except Exception:
            news = []
    if not news:
        for comp in (current_team.get("runningCompetitions") or []):
            code = comp.get("code")
            if not code:
                continue
            for article in _decorate_news_articles(db_client.get_news_for_competition(code, limit=18), code):
                hay = f"{article.get('title', '')} {article.get('summary', '')}".lower()
                if (person.get("name") or "").lower() in hay:
                    news.append(article)
        news = news[:8]

    # Compute age from dateOfBirth if apifootball didn't return one.
    age = profile.get("age") or ""
    if not age and person.get("dateOfBirth"):
        try:
            dob = datetime.fromisoformat(person["dateOfBirth"].replace("Z", "+00:00"))
            today = datetime.now(timezone.utc)
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        except Exception:
            age = ""

    return render_template(
        "player.html",
        person=person,
        profile=profile,
        news=news,
        age=age,
        error=None,
        now_year=datetime.now(timezone.utc).year,
    )


@app.route("/match/<int:match_id>")
def match_detail(match_id: int):
    data = _load_match_payload(match_id)
    if not data:
        abort(404)
    odds = None
    markets = None
    h2h_rows: list = []
    h2h_tally = {"home": 0, "draw": 0, "away": 0}
    home_name = (data.get("homeTeam") or {}).get("name") or ""
    away_name = (data.get("awayTeam") or {}).get("name") or ""
    try:
        comp_code = (data.get("competition") or {}).get("code")
        status = (data.get("status") or "").upper()
        if not _offline_mode_enabled():
            # Only fetch deeper markets for matches that haven't started yet —
            # bookmakers stop offering pre-match odds after kickoff.
            if status in ("SCHEDULED", "TIMED", "POSTPONED"):
                try:
                    markets = odds_client.full_markets_for_match(comp_code, home_name, away_name)
                except Exception:
                    markets = _cached_markets_for_match(comp_code, home_name, away_name)
            odds = odds_client.odds_for_match(comp_code, home_name, away_name)
    except Exception:
        odds = None
    if odds is None:
        snap = _find_cached_odds_snapshot((data.get("competition") or {}).get("code") or "", home_name, away_name)
        if snap:
            odds = {
                "home": snap.get("odds_home"),
                "draw": snap.get("odds_draw"),
                "away": snap.get("odds_away"),
                "bookmaker": snap.get("bookmaker"),
            }
    if markets is None:
        markets = _cached_markets_for_match((data.get("competition") or {}).get("code"), home_name, away_name)

    # Head-to-head — last 5 meetings between these two clubs (best effort).
    if _offline_mode_enabled():
        h2h_rows = db_client.get_h2h_records(home_name, away_name, limit=5)
    else:
        try:
            h2h_rows = api_football.get_h2h(home_name, away_name, limit=5) or []
        except Exception:
            h2h_rows = db_client.get_h2h_records(home_name, away_name, limit=5)
    home_norm = (home_name or "").lower()
    for row in h2h_rows:
        winner = (row.get("winner") or "").upper()
        if winner == "D":
            h2h_tally["draw"] += 1
        elif winner == "H":
            # The H/A is relative to the historical match, not this fixture —
            # work out who actually won by comparing names.
            if (row.get("home") or "").lower() == home_norm:
                h2h_tally["home"] += 1
            else:
                h2h_tally["away"] += 1
        elif winner == "A":
            if (row.get("away") or "").lower() == home_norm:
                h2h_tally["home"] += 1
            else:
                h2h_tally["away"] += 1

    # Shotmap — fetch if API key is configured and match is FINISHED
    shotmap_data = None
    load_sm = request.args.get("load_shotmap") == "1"
    status_val = (data.get("status") or "").upper()
    if load_sm or status_val == "FINISHED":
        try:
            raw_sm = db_client.get_match_shotmap(match_id)
            if raw_sm is None and shotmap_client.has_shotmap_api() and not _offline_mode_enabled():
                home_external_id = shotmap_client.resolve_team_id((data.get("homeTeam") or {}), competition_code=((data.get("competition") or {}).get("code")))
                away_external_id = shotmap_client.resolve_team_id((data.get("awayTeam") or {}), competition_code=((data.get("competition") or {}).get("code")))
                external_match_id = shotmap_client.resolve_match_id(
                    data,
                    home_external_id=home_external_id,
                    away_external_id=away_external_id,
                    competition_code=((data.get("competition") or {}).get("code")),
                )
                if external_match_id:
                    raw_sm = shotmap_client.get_shotmap(external_match_id)
                if raw_sm:
                    db_client.upsert_match_shotmap(match_id, raw_sm)
            if raw_sm:
                shotmap_data = shotmap_client.parse_shotmap(raw_sm)
        except Exception:
            shotmap_data = None

    return render_template(
        "match.html",
        match=data,
        odds=odds,
        markets=markets,
        h2h_rows=h2h_rows,
        h2h_tally=h2h_tally,
        shotmap_data=shotmap_data,
    )


@app.route("/search")
def search():
    query = (request.args.get("q") or "").strip()
    results_comps: list = []
    results_teams: list = []
    results_players: list = []
    if query:
        idx = _search_index_payload(query, limit_per_kind=30)
        results_comps = idx["competitions"]
        results_teams = idx["teams"]
        results_players = idx["players"]

    return render_template(
        "search.html",
        query=query,
        results_comps=results_comps,
        results_teams=results_teams,
        results_players=results_players,
    )


@app.route("/players")
def players_page():
    code = (request.args.get("league") or "PL").upper()
    pos = (request.args.get("position") or "ALL").upper()
    q = (request.args.get("q") or "").strip().lower()

    leagues = [
        {"code": c, **{k: v for k, v in players_helpers._league_meta(c).items() if k != "code"}}
        for c in PLAYER_LEAGUE_CODES
    ]

    rows: list = []
    error: str | None = None
    if not _offline_mode_enabled():
        try:
            rows = players_helpers.get_players_for_league(code, with_photos=True)
        except FootballDataError as exc:
            error = str(exc)
            rows = []
    if not rows:
        for player in db_client.get_players_for_competition(code):
            team = _db_team_summary(player.get("current_team_id"))
            rows.append({
                "id": player.get("id"),
                "name": player.get("name"),
                "position": player.get("position"),
                "dateOfBirth": player.get("date_of_birth"),
                "nationality": player.get("nationality"),
                "_pos": player.get("position") or "Other",
                "_photo": player.get("photo_url"),
                "_photo_local": player.get("photo_local"),
                "team": team,
            })

    # Server-side filtering keeps the page usable even before the JS hooks in.
    filtered = rows
    if pos and pos != "ALL":
        filtered = [r for r in filtered if r.get("_pos") == pos]
    if q:
        filtered = [
            r for r in filtered
            if q in (r.get("name") or "").lower()
            or q in ((r.get("team") or {}).get("name") or "").lower()
        ]

    league_meta = players_helpers._league_meta(code)

    return render_template(
        "players.html",
        leagues=leagues,
        positions=POSITION_FILTERS,
        active_league=code,
        active_position=pos,
        query=q,
        league_meta=league_meta,
        players=filtered,
        total=len(rows),
        shown=len(filtered),
        error=error,
    )


@app.route("/about")
def about():
    return render_template("about.html")


# ---------- Comparison ----------

def _team_compact_stats(team_id: int) -> dict:
    """Build a compact stats blob for a team for the comparison page.
    Returns {team, recent: [matches], record: {p,w,d,l,gf,ga,gd,pts,form_str},
    upcoming: [next_match]}.
    """
    out: dict = {}
    team = _db_team_payload(team_id) or _load_team_payload(team_id)
    if not team:
        return out
    out["team"] = team
    # Recent results — last 8 finished
    recent = _db_team_matches_payload(team_id, status="FINISHED", limit=8)
    if not recent:
        m = _load_team_matches_payload(team_id, status="FINISHED", limit=8)
        recent = (m or {}).get("matches", []) or []
    record = {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "gd": 0, "pts": 0, "form": []}
    for mm in recent[:6]:
        ht = (mm.get("homeTeam") or {}).get("id")
        at = (mm.get("awayTeam") or {}).get("id")
        sft = ((mm.get("score") or {}).get("fullTime") or {})
        h, a = sft.get("home"), sft.get("away")
        if h is None or a is None:
            continue
        record["p"] += 1
        is_home = (ht == team_id)
        gf, ga = (h, a) if is_home else (a, h)
        record["gf"] += gf
        record["ga"] += ga
        if gf > ga:
            record["w"] += 1; record["pts"] += 3; record["form"].append("W")
        elif gf == ga:
            record["d"] += 1; record["pts"] += 1; record["form"].append("D")
        else:
            record["l"] += 1; record["form"].append("L")
    record["gd"] = record["gf"] - record["ga"]
    out["record"] = record
    out["recent"] = recent[:5]
    # Next fixture
    upcoming = _db_team_matches_payload(team_id, status="SCHEDULED", limit=1)
    if not upcoming:
        upc = _load_team_matches_payload(team_id, status="SCHEDULED", limit=1)
        upcoming = (upc or {}).get("matches", []) or []
    out["next"] = upcoming[0] if upcoming else None
    return out


@app.route("/compare/teams")
def compare_teams_page():
    a_id = request.args.get("a", type=int)
    b_id = request.args.get("b", type=int)
    side_a = _team_compact_stats(a_id) if a_id else {}
    side_b = _team_compact_stats(b_id) if b_id else {}
    return render_template(
        "compare_teams.html",
        a=side_a, b=side_b,
        a_id=a_id, b_id=b_id,
        a_label=((side_a.get("team") or {}).get("name") or ""),
        b_label=((side_b.get("team") or {}).get("name") or ""),
    )


@app.route("/compare/players")
def compare_players_page():
    a_id = request.args.get("a", type=int)
    b_id = request.args.get("b", type=int)
    def _player_blob(pid: int) -> dict:
        person = _db_player_payload(pid) or _load_player_payload(pid) or {}
        agg = {"goals": 0, "assists": 0, "penalties": 0, "matches": 0, "team": "", "league": "", "shirt": person.get("shirtNumber"), "photo": "", "photo_local": "", "crest": "", "form": []}
        for row in db_client.get_scorer_rows_for_player(pid):
            raw = row.get("raw_json") or {}
            agg["goals"] = max(agg["goals"], row.get("goals") or 0)
            agg["assists"] = max(agg["assists"], row.get("assists") or 0)
            agg["penalties"] = max(agg["penalties"], row.get("penalties") or 0)
            agg["matches"] = max(agg["matches"], raw.get("playedMatches") or 0)
            if not agg["team"]:
                agg["team"] = row.get("team_name") or ""
            if not agg["league"]:
                comp = _db_competition_payload(row.get("competition_code") or "")
                agg["league"] = (comp or {}).get("name") or row.get("competition_code") or ""
        if not agg["team"]:
            agg["team"] = ((person.get("currentTeam") or {}).get("name")) or ""
        if not agg["league"]:
            running = ((person.get("currentTeam") or {}).get("runningCompetitions")) or []
            if running:
                agg["league"] = (running[0] or {}).get("name") or (running[0] or {}).get("code") or ""
        current_team = person.get("currentTeam") or {}
        agg["crest"] = current_team.get("crest") or ""
        if not agg["shirt"] and person.get("name"):
            profile = db_client.get_player_profile(person.get("name") or "", agg["team"] or None)
            if profile:
                agg["shirt"] = profile.get("shirt")
                agg["photo"] = profile.get("photo") or ""
                agg["photo_local"] = profile.get("photo_local") or ""
        if not agg["photo_local"]:
            agg["photo_local"] = db_client.get_player_photo_locals([pid]).get(pid) or ""
        if not agg["photo"] and not agg["photo_local"]:
            cached = db_client.get_player_by_id(pid) or {}
            agg["photo"] = cached.get("photo_url") or ""
            agg["photo_local"] = agg["photo_local"] or cached.get("photo_local") or ""
        recent_matches = _db_team_matches_payload(current_team.get("id"), status="FINISHED", limit=5) if current_team.get("id") else []
        if recent_matches:
            form: list[str] = []
            for mm in recent_matches[:5]:
                home = (mm.get("homeTeam") or {}).get("id")
                away = (mm.get("awayTeam") or {}).get("id")
                full = ((mm.get("score") or {}).get("fullTime") or {})
                h = full.get("home")
                a = full.get("away")
                if h is None or a is None:
                    continue
                if home == current_team.get("id"):
                    form.append("W" if h > a else ("D" if h == a else "L"))
                elif away == current_team.get("id"):
                    form.append("W" if a > h else ("D" if a == h else "L"))
            agg["form"] = form[:5]
        return {"person": person, "stats": agg}

    side_a = _player_blob(a_id) if a_id else {}
    side_b = _player_blob(b_id) if b_id else {}
    return render_template(
        "compare_players.html",
        a=side_a, b=side_b, a_id=a_id, b_id=b_id,
        a_label=((side_a.get("person") or {}).get("name") or ""),
        b_label=((side_b.get("person") or {}).get("name") or ""),
    )


# ---------- JSON API ----------

@app.route("/competition/<code>/matchday/<int:md>")
def matchday_digest(code: str, md: int):
    """Full results & scorers digest for a single matchday."""
    code = code.upper()
    competition = _load_competition_payload(code)
    if not competition:
        abort(404)
    all_matches = _load_competition_matches_payload(code).get("matches", []) or []
    all_matches = _sort_matches_chronologically(all_matches)
    matchday_archive = _build_matchday_archive(all_matches)
    current_md = (competition.get("currentSeason") or {}).get("currentMatchday") or md
    max_matchday = matchday_archive[-1]["matchday"] if matchday_archive else current_md

    matches = [match for match in all_matches if match.get("matchday") == md]

    # Build a map of team positions for context chips on each card.
    opp_codes: set[str] = {(m.get("competition") or {}).get("code") or code for m in matches}
    team_positions = _team_positions_for_codes(sorted(opp_codes)) if opp_codes else {}

    total_goals = sum(
        (m.get("score", {}).get("fullTime", {}).get("home") or 0) +
        (m.get("score", {}).get("fullTime", {}).get("away") or 0)
        for m in matches if _is_finished_match_status(_match_status(m))
    )

    return render_template(
        "matchday.html",
        competition=competition,
        matches=matches,
        matchday=md,
        current_matchday=current_md,
        max_matchday=max_matchday,
        team_positions=team_positions,
        total_goals=total_goals,
    )


@app.route("/api/standings/<code>")
def api_standings(code: str):
    code = code.upper()
    cached = _load_standings_payload(code)
    if cached:
        return jsonify(cached)
    raise FootballDataError("No cached standings available.")


@app.route("/api/scorers/<code>")
def api_scorers(code: str):
    limit = int(request.args.get("limit", 20))
    code = code.upper()
    cached = _load_scorers_payload(code, limit=limit)
    if cached:
        return jsonify(cached)
    raise FootballDataError("No cached scorers available.")


@app.route("/api/team/<int:team_id>")
def api_team(team_id: int):
    cached = _load_team_payload(team_id)
    if cached:
        return jsonify(cached)
    raise FootballDataError("No cached team available.")


@app.route("/api/odds/<code>")
def api_odds(code: str):
    sport = odds_client._sport_for_competition(code.upper())
    if not sport:
        return jsonify({"error": "No odds mapping for this competition", "status": 404}), 404
    if _offline_mode_enabled():
        return jsonify({"competition_code": code.upper(), "odds": db_client.get_odds_snapshots_for_competition(code.upper(), limit=100), "source": "db"})
    return jsonify(odds_client.fetch_odds_for_sport(sport))


@app.route("/api/search")
def api_search():
    q = request.args.get("q") or ""
    limit = int(request.args.get("limit", 6))
    kind = (request.args.get("kind") or "").strip().lower()
    payload = _search_index_payload(q, limit_per_kind=limit)
    if kind in {"team", "teams"}:
        payload = {"competitions": [], "teams": payload.get("teams", []), "players": []}
    elif kind in {"player", "players"}:
        payload = {"competitions": [], "teams": [], "players": payload.get("players", [])}
    elif kind in {"competition", "competitions", "league", "leagues"}:
        payload = {"competitions": payload.get("competitions", []), "teams": [], "players": []}
    return jsonify(payload)


@app.route("/api/healthz")
def healthz():
    return jsonify({"status": "ok", "odds_api": odds_client.has_odds_api(), "offline_mode": _offline_mode_enabled()})


def _footapi7_team_payload(team_id: int) -> dict[str, Any] | None:
    return _load_team_payload(team_id)


def _footapi7_match_payload(match_id: int) -> dict[str, Any] | None:
    return _load_match_payload(match_id)


def _footapi7_player_payload(player_id: int) -> dict[str, Any] | None:
    return _load_player_payload(player_id)


# ============================================================
# Virtual Game routes
# ============================================================

@app.route("/virtual-game")
def virtual_game():
    """Two-player football betting simulation page."""
    all_leagues = vg_module.get_teams_from_logos()
    league_logos = vg_module.get_league_logo_map(all_leagues)
    return render_template(
        "virtual_game.html",
        all_leagues=all_leagues,
        league_logos=league_logos,
        bet_types=vg_module.BET_TYPES,
        house_cut_pct=int(vg_module.HOUSE_CUT * 100),
        min_bet=int(vg_module.MIN_BET),
        max_bet=int(vg_module.MAX_BET),
        countdown=vg_module.COUNTDOWN,
    )


@app.route("/virtual-game/play", methods=["POST"])
def virtual_game_play():
    """Simulate a match and return the full result as JSON."""
    data = request.get_json(force=True) or {}
    try:
        p1_amount = float(data.get("p1_amount", 0))
        p2_amount = float(data.get("p2_amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid bet amounts"}), 400

    if p1_amount != p2_amount:
        return jsonify({"error": "Both players must bet the same amount"}), 400
    if p1_amount < vg_module.MIN_BET or p1_amount > vg_module.MAX_BET:
        return jsonify({"error": f"Bet must be between {vg_module.MIN_BET} and {vg_module.MAX_BET}"}), 400
    if not data.get("p1_bet_type") or not data.get("p2_bet_type"):
        return jsonify({"error": "Both players must select a bet type"}), 400
    if not data.get("home_team") or not data.get("away_team"):
        return jsonify({"error": "Both players must select a team"}), 400
    if data.get("home_team") == data.get("away_team"):
        return jsonify({"error": "Each player must pick a different team"}), 400

    result = vg_module.resolve_game(
        home_team=data.get("home_team", ""),
        away_team=data.get("away_team", ""),
        home_league=data.get("home_league", ""),
        away_league=data.get("away_league", ""),
        home_logo=data.get("home_logo", ""),
        away_logo=data.get("away_logo", ""),
        p1_bet_type=data.get("p1_bet_type", ""),
        p1_amount=p1_amount,
        p2_bet_type=data.get("p2_bet_type", ""),
        p2_amount=p2_amount,
    )

    # Save to DB (non-blocking, best-effort)
    try:
        db_client.save_virtual_game(result)
    except Exception:
        pass

    return jsonify(result)


@app.route("/virtual-game/history")
def virtual_game_history():
    """Return recent virtual game results from DB."""
    limit = int(request.args.get("limit", 50))
    return jsonify(db_client.get_virtual_game_history(limit))


def _build_match_archive_coverage() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    free_codes = {comp["code"] for comp in FREE_TIER_COMPETITIONS}

    for comp in FREE_TIER_COMPETITIONS + db_client.get_all_competitions():
        code = (comp.get("code") or "").upper()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)

        db_comp = db_client.get_competition_by_code(code) or {}
        match_rows = db_client.get_matches_for_competition(code)
        matchday_archive = _build_matchday_archive([
            {
                "matchday": match.get("matchday"),
                "status": match.get("status"),
                "utcDate": match.get("utc_date"),
            }
            for match in match_rows
        ])
        current_matchday = next(
            (row["matchday"] for row in matchday_archive if row.get("scheduled") or row.get("live")),
            matchday_archive[-1]["matchday"] if matchday_archive else None,
        )
        season = db_client.get_season_for_code(code)
        teams = db_client.get_teams_for_competition(code, season=season) if season else db_client.get_teams_for_competition(code)

        rows.append({
            "code": code,
            "name": db_comp.get("name") or comp.get("name") or code,
            "country": db_comp.get("area_name") or comp.get("country") or comp.get("area_name") or "",
            "season": season,
            "team_count": len(teams),
            "matches_total": len(match_rows),
            "matches_finished": sum(1 for match in match_rows if _is_finished_match_status(str(match.get("status") or "").upper())),
            "matches_pending": sum(1 for match in match_rows if not _is_finished_match_status(str(match.get("status") or "").upper())),
            "matchdays_count": len(matchday_archive),
            "matchday_range": (
                f"{matchday_archive[0]['matchday']}-{matchday_archive[-1]['matchday']}"
                if matchday_archive else "—"
            ),
            "current_matchday": current_matchday,
            "last_match_import": db_client.get_last_import(code, "matches"),
            "is_free_tier": code in free_codes,
        })

    rows.sort(key=lambda row: (not row["is_free_tier"], row["country"], row["name"]))
    return rows


# ============================================================
# Settings routes
# ============================================================

@app.route("/settings")
def settings_page():
    """API key management and data import settings page."""
    current_keys = {
        "FOOTBALL_DATA_API_KEY": bool(os.environ.get("FOOTBALL_DATA_API_KEY")),
        "API_FOOTBALL_KEY":      bool(os.environ.get("API_FOOTBALL_KEY")),
        "ODDS_API_KEY":          bool(os.environ.get("ODDS_API_KEY")),
        "RAPIDAPI_FOOTBALL_NEWS_KEY": bool(os.environ.get("RAPIDAPI_FOOTBALL_NEWS_KEY")),
        "RAPIDAPI_FOOTAPI7_KEY": bool(os.environ.get("RAPIDAPI_FOOTAPI7_KEY")),
        "RAPIDAPI_ESPN_SOCCER_KEY": bool(os.environ.get("RAPIDAPI_ESPN_SOCCER_KEY")),
    }
    app_flags = {
        "offline_mode": _offline_mode_enabled(),
    }
    admin_competitions = []
    seen_codes: set[str] = set()
    for comp in FREE_TIER_COMPETITIONS + db_client.get_all_competitions():
        code = (comp.get("code") or "").upper()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        admin_competitions.append({
            "code": code,
            "name": comp.get("name") or code,
            "country": comp.get("country") or comp.get("area_name") or "",
        })
    admin_competitions.sort(key=lambda item: (item.get("country") or "", item.get("name") or ""))
    admin_teams = db_client.get_compareable_teams()
    known_seasons = db_client.get_known_seasons(limit_per_competition=10)
    all_known_seasons = sorted({season for seasons in known_seasons.values() for season in seasons}, reverse=True)
    env_vals = {
        "VIRTUAL_GAME_HOUSE_CUT": os.environ.get("VIRTUAL_GAME_HOUSE_CUT", "20"),
        "VIRTUAL_GAME_MIN_BET":   os.environ.get("VIRTUAL_GAME_MIN_BET", "25"),
        "VIRTUAL_GAME_MAX_BET":   os.environ.get("VIRTUAL_GAME_MAX_BET", "500"),
        "VIRTUAL_GAME_COUNTDOWN": os.environ.get("VIRTUAL_GAME_COUNTDOWN", "15"),
    }
    return render_template(
        "settings.html",
        current_keys=current_keys,
        app_flags=app_flags,
        env_vals=env_vals,
        import_log=db_client.get_import_log(limit=30),
        import_failures=db_client.get_import_failures(limit=20),
        db_stats=db_client.get_db_stats(),
        admin_competitions=admin_competitions,
        admin_teams=admin_teams,
        known_seasons=known_seasons,
        all_known_seasons=all_known_seasons,
        admin_status=admin_ops.get_status(),
        admin_backups=admin_ops.list_backups(limit=20),
        match_archive_coverage=_build_match_archive_coverage(),
    )


@app.route("/settings/save", methods=["POST"])
def settings_save():
    """Persist API keys and virtual-game config to .env file."""
    form_type = request.form.get("form_type", "api_keys")
    env_path = os.path.join(os.path.dirname(__file__), ".env")

    # Read existing .env lines
    existing_lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            existing_lines = f.readlines()

    def _set_env_key(lines: list[str], key: str, value: str) -> list[str]:
        """Update or append a KEY=value line."""
        found = False
        for i, line in enumerate(lines):
            if line.startswith(key + "=") or line.startswith(f'export {key}='):
                lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}\n")
        return lines

    if form_type == "api_keys":
        key_names = [
            "FOOTBALL_DATA_API_KEY", "API_FOOTBALL_KEY", "ODDS_API_KEY",
            "RAPIDAPI_FOOTBALL_NEWS_KEY", "RAPIDAPI_FOOTAPI7_KEY",
            "RAPIDAPI_ESPN_SOCCER_KEY",
        ]
        updated = 0
        for key in key_names:
            val = request.form.get(key, "").strip()
            if val:
                existing_lines = _set_env_key(existing_lines, key, val)
                os.environ[key] = val
                updated += 1
        with open(env_path, "w") as f:
            f.writelines(existing_lines)
        flash(f"Saved {updated} API key(s) to .env", "success")

    elif form_type == "offline_mode":
        offline_enabled = request.form.get("OFFLINE_MODE") == "1"
        existing_lines = _set_env_key(existing_lines, OFFLINE_MODE_ENV_KEY, "1" if offline_enabled else "0")
        os.environ[OFFLINE_MODE_ENV_KEY] = "1" if offline_enabled else "0"
        with open(env_path, "w") as f:
            f.writelines(existing_lines)
        mode_label = "enabled" if offline_enabled else "disabled"
        flash(f"Offline mode {mode_label}. Restart the server to apply startup sync behavior.", "success")

    elif form_type == "virtual_game":
        vg_keys = [
            "VIRTUAL_GAME_HOUSE_CUT", "VIRTUAL_GAME_MIN_BET",
            "VIRTUAL_GAME_MAX_BET", "VIRTUAL_GAME_COUNTDOWN",
        ]
        for key in vg_keys:
            val = request.form.get(key, "").strip()
            if val:
                existing_lines = _set_env_key(existing_lines, key, val)
                os.environ[key] = val
        with open(env_path, "w") as f:
            f.writelines(existing_lines)
        flash("Virtual game settings saved. Restart the server to apply.", "success")

    return redirect(url_for("settings_page"))


@app.route("/settings/test-keys")
def settings_test_keys():
    """Quick connectivity test for all configured APIs. Returns JSON."""
    results: dict[str, Any] = {}

    # Test football-data.org
    try:
        comp = api_client.get_competition("PL")
        results["football_data"] = {"ok": True, "configured": bool(os.environ.get("FOOTBALL_DATA_API_KEY")), "name": comp.get("name", "ok")}
    except Exception as e:
        results["football_data"] = {"ok": False, "configured": bool(os.environ.get("FOOTBALL_DATA_API_KEY")), "error": str(e)}

    # Test odds API
    try:
        results["odds_api"] = odds_client.probe_api()
    except Exception as e:
        results["odds_api"] = {"ok": False, "configured": bool(os.environ.get("ODDS_API_KEY")), "error": str(e)}

    # Test news API
    results["news_api"] = {"ok": news_client.has_news_api(), "configured": bool(os.environ.get("RAPIDAPI_FOOTBALL_NEWS_KEY"))}

    # Test footapi7
    results["footapi7"] = {"ok": shotmap_client.has_shotmap_api(), "configured": bool(os.environ.get("RAPIDAPI_FOOTAPI7_KEY"))}
    results["espn_style_rapidapi"] = {"ok": rapidapi_extra_client.has_espn_soccer_api(), "configured": bool(os.environ.get("RAPIDAPI_ESPN_SOCCER_KEY"))}
    results["offline_mode"] = {"ok": True, "enabled": _offline_mode_enabled()}

    return jsonify(results)


@app.route("/settings/import/start", methods=["POST"])
def settings_import_start():
    """Start a full data import in the background."""
    started = data_importer.start_import_in_background()
    if started:
        return jsonify({"started": True, "message": "Import started in background."})
    else:
        return jsonify({"started": False, "running": True, "message": "Import is already running."})


@app.route("/settings/import/status")
def settings_import_status():
    """Return current import status as JSON."""
    return jsonify(data_importer.get_status())


@app.route("/startup/sync/start", methods=["POST"])
def startup_sync_start():
    """Start a startup-triggered full archive refresh for all tracked leagues."""
    codes = _startup_sync_codes()
    started = data_importer.start_import_in_background(codes=codes)
    snapshot = _startup_sync_snapshot()
    snapshot["started"] = started
    snapshot["message"] = (
        "Sync started. DKSPORTS is archiving previous matchweeks, scores, standings, teams, and scorers for your tracked leagues."
        if started else
        "A sync is already running. DKSPORTS is still refreshing your local archive in the background."
    )
    return jsonify(snapshot)


@app.route("/startup/sync/status")
def startup_sync_status():
    """Return the startup archive-sync status plus cached match counts."""
    snapshot = _startup_sync_snapshot()
    if snapshot.get("running"):
        snapshot["message"] = "Sync is running. Historical matchweeks and scores are being stored locally."
    elif snapshot.get("last_run"):
        snapshot["message"] = "Your local archive is ready. You can keep browsing offline or run a fresh sync."
    else:
        snapshot["message"] = "Choose whether to refresh from the API now or open your local archive immediately."
    return jsonify(snapshot)


@app.route("/settings/sync-status")
def settings_sync_status():
    """Return the smart background sync status plus live DB row counts as JSON."""
    status = smart_sync.get_sync_status()
    status["db_stats"] = db_client.get_db_stats()
    status["offline_mode"] = _offline_mode_enabled()
    return jsonify(status)


@app.route("/settings/admin/status")
def settings_admin_status():
    status = admin_ops.get_status()
    status["backups"] = admin_ops.list_backups(limit=20)
    status["failures"] = db_client.get_import_failures(limit=12)
    return jsonify(status)


@app.route("/settings/admin/start", methods=["POST"])
def settings_admin_start():
    operation = (request.form.get("operation") or "").strip().lower()

    def _bool(name: str) -> bool:
        return (request.form.get(name) or "").strip().lower() in {"1", "true", "on", "yes"}

    def _int(name: str) -> int | None:
        raw = (request.form.get(name) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _float(name: str) -> float | None:
        raw = (request.form.get(name) or "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    try:
        started = False
        if operation == "core_import_league":
            code = (request.form.get("competition_code") or "").upper()
            if not code:
                raise ValueError("Competition code is required.")
            started = admin_ops.start_core_import(code, delay=_float("fd_delay") or 8.0)
        elif operation == "hydrate_league":
            code = (request.form.get("competition_code") or "").upper()
            if not code:
                raise ValueError("Competition code is required.")
            started = admin_ops.start_league_hydration(
                code,
                core_only=_bool("core_only"),
                include_footapi7=_bool("include_footapi7"),
                match_limit=_int("match_limit"),
                player_limit=_int("player_limit"),
                h2h_limit=_int("h2h_limit"),
                news_max_pages=_int("news_max_pages") or 4,
                fd_delay=_float("fd_delay") or 7.5,
                aux_delay=_float("aux_delay") or 0.75,
            )
        elif operation == "hydrate_team":
            team_id = _int("team_id")
            if not team_id:
                raise ValueError("Team ID is required.")
            started = admin_ops.start_team_hydration(
                team_id,
                competition_code=(request.form.get("competition_code") or "").upper() or None,
                season=(request.form.get("season") or "").strip() or None,
                include_footapi7=_bool("include_footapi7"),
                match_limit=_int("match_limit") or 30,
                player_limit=_int("player_limit"),
                aux_delay=_float("aux_delay") or 0.75,
            )
        elif operation == "hydrate_season":
            code = (request.form.get("competition_code") or "").upper()
            season = (request.form.get("season") or "").strip()
            if not code or not season:
                raise ValueError("Competition code and season are required.")
            started = admin_ops.start_season_hydration(
                code,
                season,
                include_footapi7=_bool("include_footapi7"),
                match_limit=_int("match_limit"),
                player_limit=_int("player_limit"),
                h2h_limit=_int("h2h_limit"),
                news_max_pages=_int("news_max_pages") or 4,
                aux_delay=_float("aux_delay") or 0.75,
            )
        elif operation == "create_backup":
            started = admin_ops.start_backup((request.form.get("backup_name") or "").strip() or None)
        elif operation == "restore_backup":
            backup_name = (request.form.get("backup_name") or "").strip()
            confirm_text = request.form.get("confirm_restore") or ""
            if not backup_name:
                raise ValueError("Choose a backup to restore.")
            started = admin_ops.start_restore(backup_name, confirm_text=confirm_text)
        else:
            raise ValueError("Unknown admin operation.")
    except Exception as exc:
        return jsonify({"started": False, "running": False, "message": str(exc)}), 400

    if not started:
        status = admin_ops.get_status()
        return jsonify({
            "started": False,
            "running": True,
            "message": "Another admin operation is already running.",
            "status": status,
        }), 409

    return jsonify({"started": True, "message": "Admin operation started.", "status": admin_ops.get_status()})


@app.route("/settings/check-updates")
def settings_check_updates():
    """Compare live API data with DB and return a list of detected changes."""
    if _offline_mode_enabled():
        return jsonify({"updates": [], "offline_mode": True, "message": "Offline mode is enabled, so live update checks are skipped."})
    codes = request.args.getlist("codes") or ["PL", "PD", "BL1", "SA", "FL1"]
    results = []
    for code in codes[:5]:  # limit to avoid rate limiting
        try:
            summary = data_importer.check_for_updates(code)
            results.append(summary)
        except Exception as e:
            results.append({"code": code, "error": str(e)})
    return jsonify({"updates": results})


# ============================================================
# Shotmap API proxy
# ============================================================

@app.route("/api/match/<int:match_id>/shotmap")
def api_shotmap(match_id: int):
    """Proxy the footapi7 shotmap endpoint with parsing and caching."""
    cached = db_client.get_match_shotmap(match_id)
    if cached:
        return jsonify(shotmap_client.parse_shotmap(cached))
    if _offline_mode_enabled():
        return jsonify({"error": "Offline mode is enabled and no cached shotmap is available.", "status": 503}), 503
    if not shotmap_client.has_shotmap_api():
        return jsonify({"error": "footapi7 API key not configured", "status": 503}), 503
    match = _footapi7_match_payload(match_id)
    if not match:
        return jsonify({"error": "Match not found", "status": 404}), 404
    home_external_id = shotmap_client.resolve_team_id((match.get("homeTeam") or {}), competition_code=((match.get("competition") or {}).get("code")))
    away_external_id = shotmap_client.resolve_team_id((match.get("awayTeam") or {}), competition_code=((match.get("competition") or {}).get("code")))
    external_match_id = shotmap_client.resolve_match_id(
        match,
        home_external_id=home_external_id,
        away_external_id=away_external_id,
        competition_code=((match.get("competition") or {}).get("code")),
    )
    if not external_match_id:
        return jsonify({"error": "No footapi7 mapping found for this match", "status": 424}), 424
    raw = shotmap_client.get_shotmap(external_match_id)
    if raw is None:
        return jsonify({"error": "Shotmap not available", "status": 404}), 404
    db_client.upsert_match_shotmap(match_id, raw)
    parsed = shotmap_client.parse_shotmap(raw)
    return jsonify(parsed)


@app.route("/api/match/<int:match_id>/statistics")
def api_match_statistics(match_id: int):
    """Proxy footapi7 match statistics endpoint."""
    cached = db_client.get_match_statistics(match_id)
    if cached:
        return jsonify({"statistics": cached, "raw_count": len(cached), "source": "db"})
    if _offline_mode_enabled():
        return jsonify({"error": "Offline mode is enabled and no cached statistics are available.", "status": 503}), 503
    if not shotmap_client.has_shotmap_api():
        return jsonify({"error": "footapi7 API key not configured", "status": 503}), 503
    match = _footapi7_match_payload(match_id)
    if not match:
        return jsonify({"error": "Match not found", "status": 404}), 404
    home_external_id = shotmap_client.resolve_team_id((match.get("homeTeam") or {}), competition_code=((match.get("competition") or {}).get("code")))
    away_external_id = shotmap_client.resolve_team_id((match.get("awayTeam") or {}), competition_code=((match.get("competition") or {}).get("code")))
    external_match_id = shotmap_client.resolve_match_id(
        match,
        home_external_id=home_external_id,
        away_external_id=away_external_id,
        competition_code=((match.get("competition") or {}).get("code")),
    )
    if not external_match_id:
        return jsonify({"error": "No footapi7 mapping found for this match", "status": 424}), 424
    raw = shotmap_client.get_match_statistics(external_match_id)
    if raw is None:
        return jsonify({"error": "Statistics not available", "status": 404}), 404
    parsed = shotmap_client.parse_statistics(raw)
    if parsed:
        db_client.upsert_match_statistics(match_id, parsed)
    return jsonify({"statistics": parsed, "raw_count": len(raw.get("statistics") or [])})


# ============================================================
# Match momentum graph
# ============================================================

@app.route("/api/match/<int:match_id>/graph")
def api_match_graph(match_id: int):
    """Fetch and cache match momentum/pressure graph from footapi7."""
    cached = db_client.get_match_graph(match_id)
    if cached:
        return jsonify(shotmap_client.parse_match_graph(cached))
    if _offline_mode_enabled():
        return jsonify({"error": "Offline mode is enabled and no cached graph is available."}), 503
    if not shotmap_client.has_shotmap_api():
        return jsonify({"error": "footapi7 API key not configured"}), 503
    match = _footapi7_match_payload(match_id)
    if not match:
        return jsonify({"error": "Match not found"}), 404
    home_external_id = shotmap_client.resolve_team_id((match.get("homeTeam") or {}), competition_code=((match.get("competition") or {}).get("code")))
    away_external_id = shotmap_client.resolve_team_id((match.get("awayTeam") or {}), competition_code=((match.get("competition") or {}).get("code")))
    external_match_id = shotmap_client.resolve_match_id(
        match,
        home_external_id=home_external_id,
        away_external_id=away_external_id,
        competition_code=((match.get("competition") or {}).get("code")),
    )
    if not external_match_id:
        return jsonify({"error": "No footapi7 mapping found for this match"}), 424
    raw = shotmap_client.get_match_graph(external_match_id)
    if raw is None:
        return jsonify({"error": "Graph data not available"}), 404
    db_client.upsert_match_graph(match_id, raw)
    return jsonify(shotmap_client.parse_match_graph(raw))


# ============================================================
# Team transfers + form
# ============================================================

@app.route("/api/team/<int:team_id>/transfers")
def api_team_transfers(team_id: int):
    """Fetch team transfer data. DB-first, falls back to footapi7."""
    cached = db_client.get_team_transfers(team_id)
    if cached:
        return jsonify({"transfers": cached, "source": "db"})
    if _offline_mode_enabled():
        return jsonify({"transfers": [], "error": "Offline mode is enabled and no cached transfer data is available."}), 503
    if not shotmap_client.has_shotmap_api():
        return jsonify({"transfers": [], "error": "footapi7 API key not configured"}), 503
    team = _footapi7_team_payload(team_id)
    if not team:
        return jsonify({"transfers": [], "error": "Team not found"}), 404
    external_team_id = shotmap_client.resolve_team_id(team, competition_code=((team.get("runningCompetitions") or [{}])[0].get("code")))
    if not external_team_id:
        return jsonify({"transfers": [], "error": "No footapi7 mapping found for this team"}), 424
    raw = shotmap_client.get_team_transfers(external_team_id)
    if raw is None:
        return jsonify({"transfers": [], "error": "Transfer data not available"}), 404
    transfers = shotmap_client.parse_team_transfers(raw)
    if transfers:
        db_client.upsert_team_transfers(team_id, transfers)
    return jsonify({"transfers": transfers, "source": "api"})


@app.route("/api/team/<int:team_id>/form")
def api_team_form(team_id: int):
    """Fetch team recent form. DB-first, falls back to footapi7."""
    cached = db_client.get_team_form_db(team_id)
    if cached:
        return jsonify({"form": cached, "source": "db"})
    if _offline_mode_enabled():
        return jsonify({"form": [], "error": "Offline mode is enabled and no cached team form is available."}), 503
    if not shotmap_client.has_shotmap_api():
        return jsonify({"form": [], "error": "footapi7 API key not configured"}), 503
    team = _footapi7_team_payload(team_id)
    if not team:
        return jsonify({"form": [], "error": "Team not found"}), 404
    external_team_id = shotmap_client.resolve_team_id(team, competition_code=((team.get("runningCompetitions") or [{}])[0].get("code")))
    if not external_team_id:
        return jsonify({"form": [], "error": "No footapi7 mapping found for this team"}), 424
    raw = shotmap_client.get_team_form(external_team_id)
    if raw is None:
        return jsonify({"form": [], "error": "Form data not available"}), 404
    form_items = _parse_team_form_raw(raw)
    if form_items:
        db_client.upsert_team_form(team_id, form_items)
    return jsonify({"form": form_items, "source": "api"})


def _parse_team_form_raw(raw: dict) -> list[dict]:
    """Parse footapi7 form data into structured list."""
    items = raw.get("form") or raw.get("events") or []
    result = []
    for m in items:
        if not isinstance(m, dict):
            continue
        home = m.get("homeTeam") or {}
        away = m.get("awayTeam") or {}
        score = m.get("homeScore") or {}
        result.append({
            "match_date": m.get("startTimestamp") or m.get("date") or "",
            "opponent": away.get("name") or home.get("name") or "",
            "result": m.get("result") or "",
            "goals_for": score.get("current") or 0,
            "goals_against": (m.get("awayScore") or {}).get("current") or 0,
            "venue": "home" if m.get("isHome") else "away",
            "competition": (m.get("tournament") or {}).get("name") or "",
        })
    return result


# ============================================================
# Player season stats
# ============================================================

@app.route("/api/player/<player_id>/stats/<season_year>")
def api_player_season_stats(player_id: str, season_year: str):
    """Fetch player season stats. DB-first, falls back to footapi7."""
    cached = db_client.get_player_season_stats(player_id, season_year)
    if cached:
        return jsonify({"stats": cached[0], "source": "db"})
    if _offline_mode_enabled():
        return jsonify({"error": "Offline mode is enabled and no cached player stats are available."}), 503
    if not shotmap_client.has_shotmap_api():
        return jsonify({"error": "footapi7 API key not configured"}), 503
    person = _footapi7_player_payload(int(player_id)) if str(player_id).isdigit() else None
    if not person:
        return jsonify({"error": "Player not found"}), 404
    current_team = person.get("currentTeam") or {}
    external_team_id = None
    if current_team.get("id"):
        external_team_id = shotmap_client.resolve_team_id(
            _footapi7_team_payload(int(current_team["id"])) or current_team,
            competition_code=((current_team.get("runningCompetitions") or [{}])[0].get("code")),
        )
    external_player_id = shotmap_client.resolve_player_id(
        person,
        team_external_id=external_team_id,
        competition_code=((current_team.get("runningCompetitions") or [{}])[0].get("code")),
    )
    if not external_player_id:
        return jsonify({"error": "No footapi7 mapping found for this player"}), 424
    raw = shotmap_client.get_player_season_stats(external_player_id, season_year)
    if raw is None:
        return jsonify({"error": "Stats not available for this player/season"}), 404
    stats = shotmap_client.parse_player_season_stats(raw)
    if stats:
        db_client.upsert_player_season_stats(str(player_id), str(season_year), stats)
    return jsonify({"stats": stats, "source": "api"})


# ============================================================
# Scan page — DB deep scan vs live API
# ============================================================

@app.route("/scan")
def scan_page():
    return render_template("scan.html", free_tier_competitions=api_client.FREE_TIER_COMPETITIONS)


@app.route("/dkapi/scan/db-stats")
def api_scan_db_stats():
    """Return row counts for all DB tables."""
    try:
        return jsonify(db_client.get_db_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dkapi/scan/compare/<entity_type>/<code>")
def api_scan_compare(entity_type: str, code: str):
    """Compare DB state vs live API for a competition."""
    result: dict = {"code": code, "entity_type": entity_type, "changes": [], "error": None}
    if _offline_mode_enabled():
        result["error"] = "Offline mode is enabled, so live comparison is unavailable."
        return jsonify(result), 409
    try:
        if entity_type == "standings":
            db_rows = db_client.get_standings_simple(code)
            db_map = {r["team_id"]: r for r in db_rows}
            live_data = api_client.get_standings(code)
            live_rows = []
            for group in (live_data.get("standings") or []):
                for row in (group.get("table") or []):
                    live_rows.append(row)
            for row in live_rows:
                tid = (row.get("team") or {}).get("id")
                tname = (row.get("team") or {}).get("name", "")
                live_pts = row.get("points", 0)
                live_pos = row.get("position", 0)
                if tid in db_map:
                    db_row = db_map[tid]
                    if db_row.get("points") != live_pts or db_row.get("position") != live_pos:
                        result["changes"].append({
                            "team": tname, "team_id": tid,
                            "type": "points_changed",
                            "old_pts": db_row.get("points"), "new_pts": live_pts,
                            "old_pos": db_row.get("position"), "new_pos": live_pos,
                        })
                else:
                    result["changes"].append({"team": tname, "team_id": tid, "type": "new_team"})
        else:
            result["error"] = f"entity_type '{entity_type}' not yet supported"
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


@app.route("/dkapi/teams")
def api_teams_list():
    """Return all teams in DB as JSON."""
    try:
        teams = db_client.get_all_teams()
        return jsonify({"teams": teams, "count": len(teams)})
    except Exception as e:
        return jsonify({"error": str(e), "teams": []}), 500


@app.route("/dkapi/competitions-list")
def api_competitions_list():
    """Return all competitions in DB as JSON."""
    try:
        comps = db_client.get_all_competitions()
        return jsonify({"competitions": comps, "count": len(comps)})
    except Exception as e:
        return jsonify({"error": str(e), "competitions": []}), 500


# ============================================================
# Virtual Games Catalog
# ============================================================

@app.route("/virtual-games")
def virtual_games_catalog():
    leagues = vl_module.LEAGUES
    # Count active sessions per league
    all_sessions = db_client.list_virtual_league_sessions(limit=100)
    session_counts: dict[str, int] = {}
    for s in all_sessions:
        lc = s.get("league_code", "")
        session_counts[lc] = session_counts.get(lc, 0) + 1
    active_sessions = sorted((_decorate_virtual_session(s) for s in all_sessions), key=lambda s: s.get("created_at", ""), reverse=True)[:12]
    # Recent betting games from history
    try:
        recent_bets = db_client.get_virtual_game_history(limit=8)
    except Exception:
        recent_bets = []
    return render_template(
        "virtual_game_catalog.html",
        leagues=leagues,
        session_counts=session_counts,
        active_sessions=active_sessions,
        recent_bets=recent_bets,
    )


def _virtual_season_name(session: dict[str, Any], fallback_league_name: str = "") -> str:
    season_name = (session.get("season_name") or "").strip()
    if season_name:
        return season_name
    league_name = (session.get("league_name") or fallback_league_name or "Virtual League").strip()
    return f"{league_name} Season"


def _decorate_virtual_session(session: dict[str, Any], fallback_league_name: str = "") -> dict[str, Any]:
    total_matchdays = int(session.get("total_matchdays") or 0)
    current_matchday = int(session.get("current_matchday") or 1)
    played_matchdays = min(max(current_matchday - 1, 0), total_matchdays) if total_matchdays else 0
    session["display_name"] = _virtual_season_name(session, fallback_league_name)
    session["played_matchdays"] = played_matchdays
    session["progress_pct"] = int((played_matchdays / total_matchdays) * 100) if total_matchdays else 0
    return session


def _normalize_virtual_team_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    normalized = normalized.replace("&", " and ").lower()
    normalized = re.sub(r"\b(fc|cf|afc|ac|as|ssc|rc|rcd|ca|cd|club|sporting|calcio)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _virtual_league_team_crests(league_code: str, league: dict[str, Any]) -> dict[str, str]:
    team_crests: dict[str, str] = {}
    try:
        teams_by_league = db_client.get_teams_by_league()
        normalized_lookup: dict[str, str] = {}
        for team in teams_by_league.get(league_code, []):
            name = team.get("name") or ""
            crest = _browser_asset_url(team.get("crest_local") or team.get("crest_url") or "")
            if name and crest:
                team_crests[name] = crest
                normalized_lookup[_normalize_virtual_team_key(name)] = crest
        for team_name in league.get("teams", []):
            if team_crests.get(team_name):
                continue
            normalized_name = _normalize_virtual_team_key(team_name)
            if normalized_name and normalized_lookup.get(normalized_name):
                team_crests[team_name] = normalized_lookup[normalized_name]
    except Exception:
        pass
    for team_name in league.get("teams", []):
        if not team_crests.get(team_name):
            local_logo = _browser_asset_url(vl_module.get_local_team_logo(team_name, league_code))
            if local_logo:
                team_crests[team_name] = local_logo
    return team_crests


def _virtual_team_crest_for(team_name: str, team_crests: dict[str, str]) -> str:
    direct = _browser_asset_url(team_crests.get(team_name, ""))
    if direct:
        return direct
    normalized_name = _normalize_virtual_team_key(team_name)
    if not normalized_name:
        return ""
    for key, crest in team_crests.items():
        if _normalize_virtual_team_key(key) == normalized_name and crest:
            return _browser_asset_url(crest)
    return ""


def _virtual_scorer_preview(scorers: list[dict[str, Any]], limit: int = 2) -> str:
    preview: list[str] = []
    for scorer in scorers[:limit]:
        player = (scorer.get("player") or scorer.get("player_name") or "").strip()
        minute = scorer.get("minute")
        if player and minute is not None:
            preview.append(f"{player} {minute}'")
        elif player:
            preview.append(player)
    return ", ".join(preview)


def _virtual_seed(*parts: Any) -> int:
    raw = "||".join(str(part) for part in parts)
    return int(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16], 16)


def _fallback_virtual_scorers(team_name: str, goals: int, seed: int) -> list[dict[str, Any]]:
    if goals <= 0:
        return []
    rng = random.Random(seed)
    pool = list(getattr(vl_module, "TEAM_SCORERS", {}).get(team_name) or getattr(vl_module, "_FALLBACK_PLAYERS", ["Forward"]))
    scorers: list[dict[str, Any]] = []
    used_minutes: set[int] = set()
    for _ in range(goals):
        player = rng.choice(pool)
        minute = rng.randint(3, 90)
        while minute in used_minutes:
            minute = rng.randint(3, 90)
        used_minutes.add(minute)
        assist = None
        if len(pool) > 1 and rng.random() > 0.35:
            candidates = [name for name in pool if name != player]
            if candidates:
                assist = rng.choice(candidates)
        scorers.append({"player": player, "minute": minute, "assist": assist})
    scorers.sort(key=lambda scorer: scorer["minute"])
    return scorers


def _fallback_virtual_stats(
    home_team: str,
    away_team: str,
    home_score: int,
    away_score: int,
    matchday: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rng = random.Random(_virtual_seed(home_team, away_team, home_score, away_score, matchday or 0))
    home_possession = max(36, min(64, 50 + ((home_score - away_score) * 4) + rng.randint(-8, 8)))

    def build(goals_for: int, goals_against: int, possession: int) -> dict[str, Any]:
        goal_gap = goals_for - goals_against
        control_bonus = max(possession - 50, 0) // 10
        momentum_bonus = max(goal_gap, 0)
        shots_on_target = max(goals_for, 1 + goals_for + control_bonus + momentum_bonus + rng.randint(0, 2))
        total_shots = max(shots_on_target + rng.randint(3, 8), shots_on_target + 2)
        corners_floor = max(1, total_shots // 4)
        corners_ceiling = max(corners_floor + 1, total_shots // 2 + 2)
        fouls_floor = 7 + max(0, 52 - possession) // 7
        fouls_ceiling = fouls_floor + 7
        yellow_cap = 4 if fouls_ceiling >= 12 else 3
        return {
            "possession": possession,
            "shots": total_shots,
            "shots_on_target": shots_on_target,
            "corners": rng.randint(corners_floor, corners_ceiling),
            "fouls": rng.randint(fouls_floor, fouls_ceiling),
            "yellow_cards": rng.randint(0, yellow_cap),
        }

    home_stats = build(home_score, away_score, home_possession)
    away_stats = build(away_score, home_score, 100 - home_possession)

    if home_score > away_score and home_stats["shots"] < away_stats["shots"]:
        home_stats["shots"] = away_stats["shots"] + rng.randint(0, 3)
        home_stats["shots_on_target"] = min(home_stats["shots"], max(home_stats["shots_on_target"], away_stats["shots_on_target"]))
    elif away_score > home_score and away_stats["shots"] < home_stats["shots"]:
        away_stats["shots"] = home_stats["shots"] + rng.randint(0, 3)
        away_stats["shots_on_target"] = min(away_stats["shots"], max(away_stats["shots_on_target"], home_stats["shots_on_target"]))
    return home_stats, away_stats


def _ensure_virtual_match_details(item: dict[str, Any], league_code: str, matchday: int | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    played = (item.get("status") == "played") or (
        item.get("home_score") is not None and item.get("away_score") is not None
    )
    home_scorers = list(item.get("home_scorers") or [])
    away_scorers = list(item.get("away_scorers") or [])
    home_stats = dict(item.get("home_stats") or {})
    away_stats = dict(item.get("away_stats") or {})
    if not played:
        return home_scorers, away_scorers, home_stats, away_stats

    home_team = item.get("home_team") or ""
    away_team = item.get("away_team") or ""
    home_score = int(item.get("home_score") or 0)
    away_score = int(item.get("away_score") or 0)
    matchday_value = item.get("matchday") or matchday

    if home_score > 0 and not home_scorers:
        home_scorers = _fallback_virtual_scorers(home_team, home_score, _virtual_seed(league_code, home_team, away_team, "home", home_score, away_score, matchday_value or 0))
    if away_score > 0 and not away_scorers:
        away_scorers = _fallback_virtual_scorers(away_team, away_score, _virtual_seed(league_code, away_team, home_team, "away", away_score, home_score, matchday_value or 0))
    if not home_stats or not away_stats:
        fallback_home_stats, fallback_away_stats = _fallback_virtual_stats(home_team, away_team, home_score, away_score, matchday_value)
        home_stats = home_stats or fallback_home_stats
        away_stats = away_stats or fallback_away_stats
    return home_scorers, away_scorers, home_stats, away_stats


def _shape_virtual_match(
    item: dict[str, Any],
    league_code: str,
    league: dict[str, Any],
    team_crests: dict[str, str],
    matchday: int | None = None,
) -> dict[str, Any]:
    home_team = item.get("home_team") or ""
    away_team = item.get("away_team") or ""
    stats = item.get("stats") or {}
    home_scorers, away_scorers, home_stats, away_stats = _ensure_virtual_match_details(
        {
            **item,
            "home_stats": item.get("home_stats") or stats.get("home") or {},
            "away_stats": item.get("away_stats") or stats.get("away") or {},
        },
        league_code,
        matchday,
    )
    shaped = {
        "home_team": home_team,
        "away_team": away_team,
        "home_score": item.get("home_score"),
        "away_score": item.get("away_score"),
        "home_scorers": home_scorers,
        "away_scorers": away_scorers,
        "home_stats": home_stats,
        "away_stats": away_stats,
        "home_preview": _virtual_scorer_preview(home_scorers),
        "away_preview": _virtual_scorer_preview(away_scorers),
        "home_crest": _virtual_team_crest_for(home_team, team_crests),
        "away_crest": _virtual_team_crest_for(away_team, team_crests),
        "matchday": item.get("matchday") or matchday,
        "status": item.get("status") or ("played" if item.get("home_score") is not None else "scheduled"),
        "competition": {
            "code": league_code,
            "name": league.get("name") or league_code,
            "emblem": league.get("logo") or "",
        },
    }
    shaped["detail_available"] = bool(home_scorers or away_scorers or home_stats or away_stats)
    return shaped


def _build_virtual_team_payload(
    session_id: int,
    session_key: str,
    league_code: str,
    league: dict[str, Any],
    team_name: str,
    team_crests: dict[str, str],
    standings: list[dict[str, Any]],
    scorer_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    standing_row = next((row for row in standings if row.get("team") == team_name), None)
    team_scorers = [row for row in vl_module.compute_top_scorers(scorer_rows, limit=200) if row.get("team_name") == team_name][:5]
    fixtures = []
    for row in db_client.get_virtual_league_fixtures(session_id):
        if row.get("status") != "played":
            continue
        if team_name not in (row.get("home_team"), row.get("away_team")):
            continue
        fixtures.append(_shape_virtual_match(row, league_code, league, team_crests, row.get("matchday")))
    fixtures.sort(key=lambda match: (match.get("matchday") or 0, match.get("home_team") or "", match.get("away_team") or ""))
    return {
        "team_name": team_name,
        "session_key": session_key,
        "crest": _virtual_team_crest_for(team_name, team_crests),
        "summary": {
            "position": standing_row.get("position") if standing_row else None,
            "points": standing_row.get("points", 0) if standing_row else 0,
            "played": standing_row.get("played", 0) if standing_row else 0,
            "goal_diff": standing_row.get("goal_diff", 0) if standing_row else 0,
            "goals_for": standing_row.get("goals_for", 0) if standing_row else 0,
            "goals_against": standing_row.get("goals_against", 0) if standing_row else 0,
            "won": standing_row.get("won", 0) if standing_row else 0,
            "drawn": standing_row.get("drawn", 0) if standing_row else 0,
            "lost": standing_row.get("lost", 0) if standing_row else 0,
            "form": standing_row.get("form", []) if standing_row else [],
        },
        "top_scorers": team_scorers,
        "matches": fixtures,
    }


# ============================================================
# Virtual League — new session
# ============================================================

@app.route("/virtual-league/new/<league_code>", methods=["GET", "POST"])
def virtual_league_new_get(league_code: str):
    league = vl_module.LEAGUES.get(league_code)
    if not league:
        return redirect(url_for("virtual_games_catalog"))
    suggested_season_name = f"{league['name']} Offline Season"
    existing_sessions = [
        _decorate_virtual_session(s, league["name"])
        for s in db_client.list_virtual_league_sessions(league_code=league_code, limit=6)
    ]
    if request.method == "GET":
        return render_template(
            "virtual_league_new.html",
            league=league,
            league_code=league_code,
            existing_sessions=existing_sessions,
            suggested_season_name=suggested_season_name,
        )

    season_name = (request.form.get("season_name") or "").strip() or suggested_season_name
    try:
        session_key = db_client.create_virtual_league_session(
            league_code,
            league["name"],
            league["matchdays"],
            season_name=season_name,
        )
        # Get the session to retrieve its numeric id
        sess = db_client.get_virtual_league_session(session_key)
        if not sess:
            raise RuntimeError("session not found after creation")
        teams = list(league["teams"])
        all_matchdays = vl_module.generate_fixtures(teams)
        db_client.save_virtual_league_fixtures(sess["id"], all_matchdays)
    except Exception as e:
        return f"Error creating session: {e}", 500
    return redirect(url_for("virtual_league_session", session_key=session_key))


# ============================================================
# Virtual League — session view
# ============================================================

@app.route("/virtual-league/<session_key>")
def virtual_league_session(session_key: str):
    sess = db_client.get_virtual_league_session(session_key)
    if not sess:
        return redirect(url_for("virtual_games_catalog"))
    league = vl_module.LEAGUES.get(sess["league_code"], {})
    sess = _decorate_virtual_session(sess, league.get("name", sess.get("league_name", "")))
    current_md = sess["current_matchday"]
    total_md = sess["total_matchdays"]
    played_matchdays = sess["played_matchdays"]
    progress_pct = sess["progress_pct"]
    team_crests = _virtual_league_team_crests(sess["league_code"], league)
    # Fixtures for today (next to simulate)
    fixtures_today = []
    if sess["status"] != "completed":
        raw = db_client.get_virtual_league_fixtures(sess["id"], matchday=current_md)
        fixtures_today = [
            _shape_virtual_match(r, sess["league_code"], league, team_crests, current_md)
            for r in raw if r.get("status") == "scheduled"
        ]
    # Last matchday results
    last_matchday_results = []
    if current_md > 1:
        last_raw = db_client.get_virtual_league_fixtures(sess["id"], matchday=current_md - 1)
        last_matchday_results = [
            _shape_virtual_match(r, sess["league_code"], league, team_crests, current_md - 1)
            for r in last_raw if r.get("status") == "played"
        ]
    # Standings from all played results
    all_played = db_client.get_all_played_results(sess["id"])
    standings = vl_module.compute_standings(all_played, list(league.get("teams", [])))
    # Top scorers
    scorer_rows = db_client.get_virtual_league_scorers(sess["id"], limit=500)
    top_scorers = vl_module.compute_top_scorers(scorer_rows)
    initial_team_name = standings[0]["team"] if standings else next(iter(league.get("teams", [])), "")
    initial_team_detail = None
    if initial_team_name:
        initial_team_detail = _build_virtual_team_payload(
            sess["id"],
            session_key,
            sess["league_code"],
            league,
            initial_team_name,
            team_crests,
            standings,
            scorer_rows,
        )
    return render_template(
        "virtual_league_session.html",
        session=sess,
        league=league,
        current_matchday=current_md,
        played_matchdays=played_matchdays,
        progress_pct=progress_pct,
        fixtures_today=fixtures_today,
        last_matchday_results=last_matchday_results,
        standings=standings,
        top_scorers=top_scorers,
        get_zone=vl_module.get_position_zone,
        qual_zones=vl_module.QUAL_ZONES,
        team_crests=team_crests,
        initial_team_name=initial_team_name,
        initial_team_detail=initial_team_detail,
    )


# ============================================================
# Virtual League — play next matchday (AJAX / JSON)
# ============================================================

@app.route("/virtual-league/<session_key>/play", methods=["POST"])
def virtual_league_play(session_key: str):
    sess = db_client.get_virtual_league_session(session_key)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    if sess["status"] == "completed":
        return jsonify({"error": "Season already complete"}), 400
    current_md = sess["current_matchday"]
    total_md = sess["total_matchdays"]
    league = vl_module.LEAGUES.get(sess["league_code"], {})
    team_crests = _virtual_league_team_crests(sess["league_code"], league)
    # Fetch scheduled fixtures for this matchday
    raw_fixtures = db_client.get_virtual_league_fixtures(sess["id"], matchday=current_md)
    fixtures = [(r["home_team"], r["away_team"]) for r in raw_fixtures if r.get("status") == "scheduled"]
    if not fixtures:
        return jsonify({"error": f"No scheduled fixtures for matchday {current_md}"}), 400
    # Simulate
    results = vl_module.simulate_matchday(fixtures)
    # Persist
    db_client.save_matchday_results(sess["id"], current_md, results)
    next_md = current_md + 1
    db_client.advance_virtual_league_matchday(session_key, next_md, total_md)
    season_complete = next_md > total_md
    # Recompute standings + scorers for response
    all_played = db_client.get_all_played_results(sess["id"])
    standings = vl_module.compute_standings(all_played, list(league.get("teams", [])))
    scorer_rows = db_client.get_virtual_league_scorers(sess["id"], limit=30)
    top_scorers = vl_module.compute_top_scorers(scorer_rows)
    upcoming_fixtures: list[dict[str, Any]] = []
    if not season_complete:
        next_raw = db_client.get_virtual_league_fixtures(sess["id"], matchday=next_md)
        upcoming_fixtures = [
            _shape_virtual_match(r, sess["league_code"], league, team_crests, next_md)
            for r in next_raw if r.get("status") == "scheduled"
        ]
    return jsonify({
        "matchday": current_md,
        "next_matchday": next_md,
        "season_complete": season_complete,
        "results": [
            _shape_virtual_match(r, sess["league_code"], league, team_crests, current_md)
            for r in results
        ],
        "upcoming_fixtures": upcoming_fixtures,
        "played_matchdays": min(current_md, total_md),
        "progress_pct": int((min(current_md, total_md) / total_md) * 100) if total_md else 0,
        "standings": standings,
        "top_scorers": top_scorers,
    })


# ============================================================
# Virtual League — matchday results (AJAX)
# ============================================================

@app.route("/virtual-league/<session_key>/matchday/<int:md>")
def virtual_league_matchday_results(session_key: str, md: int):
    """Return played fixture results for a specific matchday (JSON)."""
    sess = db_client.get_virtual_league_session(session_key)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    league = vl_module.LEAGUES.get(sess["league_code"], {})
    team_crests = _virtual_league_team_crests(sess["league_code"], league)
    raw = db_client.get_virtual_league_fixtures(sess["id"], matchday=md)
    results = [
        _shape_virtual_match(r, sess["league_code"], league, team_crests, md)
        for r in raw if r.get("status") == "played"
    ]
    return jsonify({"matchday": md, "results": results})


@app.route("/virtual-league/<session_key>/team")
def virtual_league_team_results(session_key: str):
    sess = db_client.get_virtual_league_session(session_key)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    team_name = (request.args.get("team") or "").strip()
    if not team_name:
        return jsonify({"error": "Team name is required"}), 400
    league = vl_module.LEAGUES.get(sess["league_code"], {})
    team_crests = _virtual_league_team_crests(sess["league_code"], league)
    standings = vl_module.compute_standings(db_client.get_all_played_results(sess["id"]), list(league.get("teams", [])))
    scorer_rows = db_client.get_virtual_league_scorers(sess["id"], limit=500)
    payload = _build_virtual_team_payload(
        sess["id"],
        session_key,
        sess["league_code"],
        league,
        team_name,
        team_crests,
        standings,
        scorer_rows,
    )
    return jsonify(payload)


# ============================================================
# Virtual League — restart season
# ============================================================

@app.route("/virtual-league/<session_key>/restart", methods=["POST"])
def virtual_league_restart(session_key: str):
    sess = db_client.get_virtual_league_session(session_key)
    if not sess:
        return redirect(url_for("virtual_games_catalog"))
    league_code = sess["league_code"]
    league = vl_module.LEAGUES.get(league_code, {})
    try:
        db_client.delete_virtual_league_session(session_key)
        new_key = db_client.create_virtual_league_session(
            league_code,
            league.get("name", league_code),
            league.get("matchdays", 38),
            season_name=sess.get("season_name") or f"{league.get('name', league_code)} Season",
        )
        new_sess = db_client.get_virtual_league_session(new_key)
        if new_sess:
            teams = list(league.get("teams", []))
            all_matchdays = vl_module.generate_fixtures(teams)
            db_client.save_virtual_league_fixtures(new_sess["id"], all_matchdays)
        return redirect(url_for("virtual_league_session", session_key=new_key))
    except Exception as e:
        return f"Error restarting: {e}", 500


# ============================================================
# .env template download
# ============================================================

@app.route("/download/env-template")
def download_env_template():
    """Serve the .env.example template as a file download."""
    template_path = os.path.join(os.path.dirname(__file__), ".env.example")
    if not os.path.exists(template_path):
        return "Template not found", 404
    response = make_response(open(template_path).read())
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename=".env.example"'
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False, threaded=True)
