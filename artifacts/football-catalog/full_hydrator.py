"""Full DKSPORTS data hydrator.

This script goes beyond the lightweight importer and attempts to populate every
DB table the current project can reasonably fill from the configured APIs.

Usage examples:

    python full_hydrator.py
    python full_hydrator.py --codes PL,PD --match-limit 10 --player-limit 50 --h2h-limit 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, TypeVar

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import api_client
import api_football
import data_importer
import db_client
import news_client
import odds_client
import shotmap_client
import sportsdb_client

T = TypeVar("T")


def _log(message: str) -> None:
    print(f"[full-hydrator] {message}", flush=True)


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _slice(items: list[T], limit: int | None) -> list[T]:
    if limit is None:
        return items
    return items[:limit]


def _safe_stage(
    entity_type: str,
    code: str,
    fn,
    *args,
    default: Any = 0,
) -> Any:
    try:
        result = fn(*args)
        if isinstance(result, int):
            db_client.log_import(entity_type, code, result, "ok")
        elif isinstance(result, list):
            db_client.log_import(entity_type, code, len(result), "ok")
        return result
    except Exception as exc:
        db_client.log_import(entity_type, code, 0, "error", str(exc))
        _log(f"[{code}] {entity_type} error: {exc}")
        return default


def _team_payload_from_row(row: dict | None) -> dict:
    row = row or {}
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "shortName": row.get("short_name") or row.get("name"),
        "tla": row.get("tla"),
        "area": {"name": row.get("area_name") or ""},
    }


def _player_payload_from_row(row: dict | None) -> dict:
    row = row or {}
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "shortName": row.get("name"),
        "position": row.get("position"),
        "nationality": row.get("nationality"),
        "currentTeam": {"id": row.get("current_team_id"), "name": row.get("current_team_name")},
    }


def _match_payload_from_row(row: dict, teams_by_id: dict[int, dict]) -> dict:
    return {
        "id": row.get("id"),
        "utcDate": row.get("utc_date"),
        "competition": {"code": row.get("competition_code")},
        "homeTeam": _team_payload_from_row(teams_by_id.get(row.get("home_team_id"))),
        "awayTeam": _team_payload_from_row(teams_by_id.get(row.get("away_team_id"))),
    }


def hydrate_players_for_competition(
    code: str,
    player_limit: int | None,
    aux_delay: float,
) -> dict[str, int]:
    teams_data = api_client.get_competition_teams(code)
    teams = teams_data.get("teams") or []
    season = ((teams_data.get("season") or {}).get("startDate") or "")[:4]
    competition_name = (teams_data.get("competition") or {}).get("name") or code

    players_seen: set[int] = set()
    players_count = 0
    profiles_count = 0

    for team in teams:
        squad = list(team.get("squad") or [])
        if not squad:
            continue
        team_profiles_added = 0

        try:
            sportsdb_client.attach_team_people(team, code)
        except Exception as exc:
            _log(f"[{code}] sportsdb enrich failed for {team.get('name')}: {exc}")

        if api_football.has_api_football():
            try:
                api_football.attach_coach_photo(team)
                if not any((p.get("_photo") for p in squad)):
                    api_football.attach_player_photos(team)
            except Exception:
                pass
        try:
            data_importer.cache_team_people_assets(team, competition_name)
            if team.get("_coach_photo_local"):
                db_client.upsert_team(
                    {**team, "_coach_photo": team.get("_coach_photo_local")},
                    code,
                    season,
                )
        except Exception as exc:
            _log(f"[{code}] offline media cache failed for {team.get('name')}: {exc}")

        for player in squad:
            pid = player.get("id")
            if not isinstance(pid, int) or pid in players_seen:
                continue
            db_client.upsert_player(player, team=team, competition_code=code, season=season)
            sportsdb_profile = player.get("_sportsdb_profile")
            if isinstance(sportsdb_profile, dict):
                local_photo = player.get("_photo_local")
                db_client.upsert_player_profile(
                    player.get("name") or "",
                    team.get("name") or "",
                    team.get("id"),
                    {
                        **sportsdb_client.profile_from_player(sportsdb_profile),
                        "photo_local": local_photo,
                    },
                )
                profiles_count += 1
                team_profiles_added += 1
            players_seen.add(pid)
            players_count += 1
            if player_limit is not None and players_count >= player_limit:
                break

        if api_football.has_api_football() and team_profiles_added < len(squad):
            try:
                api_team_id = api_football.find_team_id(
                    team.get("name") or "",
                    (team.get("area") or {}).get("name"),
                )
                if api_team_id:
                    for profile in api_football.get_squad_profiles(api_team_id):
                        player_name = profile.get("player_name") or ""
                        if not player_name:
                            continue
                        player_id = None
                        for squad_player in squad:
                            if (squad_player.get("name") or "").strip().lower() == player_name.strip().lower():
                                player_id = squad_player.get("id")
                                break
                        photo_local = None
                        if player_id and profile.get("photo"):
                            photo_local = data_importer.download_player_photo(
                                player_name,
                                player_id,
                                profile.get("photo"),
                                league_name=competition_name,
                                team_name=team.get("name") or "",
                            )
                        db_client.upsert_player_profile(
                            player_name,
                            team.get("name") or "",
                            team.get("id"),
                            {
                                "photo": profile.get("photo"),
                                "photo_local": photo_local,
                                "height": profile.get("height"),
                                "weight": profile.get("weight"),
                                "age": profile.get("age"),
                                "country": profile.get("country"),
                                "position": profile.get("position"),
                                "market_value": profile.get("market_value"),
                                "shirt": profile.get("shirt"),
                                "rating": profile.get("rating"),
                                "foot": profile.get("foot"),
                                "raw": profile.get("raw"),
                            },
                        )
                        profiles_count += 1
            except Exception as exc:
                _log(f"[{code}] squad profile enrich failed for {team.get('name')}: {exc}")

        if player_limit is not None and players_count >= player_limit:
            break

        _sleep(aux_delay)

    return {"players": players_count, "player_profiles": profiles_count}


def hydrate_match_details_for_competition(
    code: str,
    match_limit: int | None,
    aux_delay: float,
    include_footapi7: bool,
    season: str | None = None,
    team_ids: set[int] | None = None,
) -> dict[str, int]:
    finished_matches = db_client.get_matches_for_competition(code, status="FINISHED")
    if season:
        finished_matches = [match for match in finished_matches if (match.get("season") or "") == season]
    if team_ids:
        finished_matches = [
            match for match in finished_matches
            if match.get("home_team_id") in team_ids or match.get("away_team_id") in team_ids
        ]
    finished_matches = _slice(finished_matches, match_limit)
    team_ids = {
        int(tid) for match in finished_matches
        for tid in (match.get("home_team_id"), match.get("away_team_id"))
        if tid is not None
    }
    teams_by_id = {team_id: (db_client.get_team_by_id(team_id) or {}) for team_id in team_ids}
    team_maps = {
        team_id: shotmap_client.resolve_team_id(_team_payload_from_row(team_row), competition_code=code)
        for team_id, team_row in teams_by_id.items()
        if team_row
    }

    counts = {
        "team_mappings": len([v for v in team_maps.values() if v]),
        "match_mappings": 0,
        "match_events": 0,
        "match_lineups": 0,
        "match_statistics": 0,
        "match_incidents": 0,
        "match_graphs": 0,
        "match_shotmaps": 0,
    }

    if not include_footapi7 or not shotmap_client.has_shotmap_api():
        return counts

    for match in finished_matches:
        match_id = match.get("id")
        if not match_id:
            continue
        match_payload = _match_payload_from_row(match, teams_by_id)
        external_match_id = shotmap_client.resolve_match_id(
            match_payload,
            home_external_id=team_maps.get(match.get("home_team_id")),
            away_external_id=team_maps.get(match.get("away_team_id")),
            competition_code=code,
        )
        if not external_match_id:
            continue
        counts["match_mappings"] += 1

        try:
            raw_incidents = shotmap_client.get_match_incidents(external_match_id)
            if raw_incidents:
                counts["match_incidents"] += db_client.upsert_match_incidents(
                    match_id,
                    raw_incidents.get("incidents") or [],
                )
                normalized_events = shotmap_client.parse_match_incidents_to_events(raw_incidents)
                counts["match_events"] += db_client.upsert_match_events(match_id, normalized_events)
        except Exception as exc:
            _log(f"[{code}] match incidents failed for {match_id}->{external_match_id}: {exc}")

        try:
            raw_lineups = shotmap_client.get_match_lineups(external_match_id)
            if raw_lineups:
                normalized_lineups = shotmap_client.parse_lineups(raw_lineups)
                counts["match_lineups"] += db_client.upsert_match_lineups(match_id, normalized_lineups)
        except Exception as exc:
            _log(f"[{code}] match lineups failed for {match_id}->{external_match_id}: {exc}")

        try:
            raw_stats = shotmap_client.get_match_statistics(external_match_id)
            if raw_stats:
                parsed_stats = shotmap_client.parse_statistics(raw_stats)
                counts["match_statistics"] += db_client.upsert_match_statistics(match_id, parsed_stats)
        except Exception as exc:
            _log(f"[{code}] match stats failed for {match_id}->{external_match_id}: {exc}")

        try:
            raw_graph = shotmap_client.get_match_graph(external_match_id)
            if raw_graph:
                db_client.upsert_match_graph(match_id, raw_graph)
                counts["match_graphs"] += 1
        except Exception as exc:
            _log(f"[{code}] match graph failed for {match_id}->{external_match_id}: {exc}")

        try:
            raw_shotmap = shotmap_client.get_shotmap(external_match_id)
            if raw_shotmap:
                db_client.upsert_match_shotmap(match_id, raw_shotmap)
                counts["match_shotmaps"] += 1
        except Exception as exc:
            _log(f"[{code}] shotmap failed for {match_id}->{external_match_id}: {exc}")

        _sleep(aux_delay)

    return counts


def hydrate_team_enrichments_for_competition(
    code: str,
    aux_delay: float,
    include_footapi7: bool,
    team_ids: set[int] | None = None,
) -> dict[str, int]:
    teams = db_client.get_teams_for_competition(code)
    if team_ids:
        teams = [team for team in teams if team.get("id") in team_ids]
    counts = {"team_mappings": 0, "team_transfers": 0, "team_form": 0}

    if not include_footapi7 or not shotmap_client.has_shotmap_api():
        return counts

    for team in teams:
        team_id = team.get("id")
        if not team_id:
            continue
        external_team_id = shotmap_client.resolve_team_id(_team_payload_from_row(team), competition_code=code)
        if not external_team_id:
            continue
        counts["team_mappings"] += 1

        try:
            raw_transfers = shotmap_client.get_team_transfers(external_team_id)
            if raw_transfers:
                transfers = shotmap_client.parse_team_transfers(raw_transfers)
                if transfers:
                    db_client.upsert_team_transfers(team_id, transfers)
                    counts["team_transfers"] += len(transfers)
        except Exception as exc:
            _log(f"[{code}] team transfers failed for {team.get('name')} ({team_id}->{external_team_id}): {exc}")

        try:
            raw_form = shotmap_client.get_team_form(external_team_id)
            if raw_form:
                form_items = _parse_team_form_raw(raw_form)
                if form_items:
                    db_client.upsert_team_form(team_id, form_items)
                    counts["team_form"] += len(form_items)
        except Exception as exc:
            _log(f"[{code}] team form failed for {team.get('name')} ({team_id}->{external_team_id}): {exc}")

        _sleep(aux_delay)

    return counts


def hydrate_player_season_stats_for_competition(
    code: str,
    player_limit: int | None,
    aux_delay: float,
    include_footapi7: bool,
    season_override: str | None = None,
    team_ids: set[int] | None = None,
) -> int:
    if not include_footapi7 or not shotmap_client.has_shotmap_api():
        return 0

    season = season_override or db_client.get_season_for_code(code) or str(time.gmtime().tm_year)
    players = db_client.get_players_for_competition(code, season=season)
    if team_ids:
        players = [
            player for player in players
            if player.get("current_team_id") in team_ids or player.get("team_id") in team_ids
        ]
    players = _slice(players, player_limit)
    team_maps = {
        team_id: shotmap_client.resolve_team_id(_team_payload_from_row(db_client.get_team_by_id(int(team_id))), competition_code=code)
        for team_id in {player.get("current_team_id") for player in players if player.get("current_team_id") is not None}
    }
    count = 0

    for player in players:
        player_id = player.get("id")
        if not player_id:
            continue
        external_player_id = shotmap_client.resolve_player_id(
            _player_payload_from_row(player),
            team_external_id=team_maps.get(player.get("current_team_id")),
            competition_code=code,
        )
        if not external_player_id:
            continue
        try:
            raw = shotmap_client.get_player_season_stats(external_player_id, season)
            if not raw:
                continue
            stats = shotmap_client.parse_player_season_stats(raw)
            if stats:
                db_client.upsert_player_season_stats(str(player_id), str(season), stats)
                count += 1
        except Exception as exc:
            _log(f"[{code}] player season stats failed for {player.get('name')} ({player_id}->{external_player_id}): {exc}")
        _sleep(aux_delay)

    return count


def hydrate_news_for_competition(code: str, aux_delay: float, max_pages: int = 4) -> int:
    if not news_client.has_news_api():
        return 0
    league_id = news_client.league_id_for_competition(code)
    seen_urls: set[str] = set()
    count = 0
    for page in range(1, max(1, max_pages) + 1):
        articles = news_client.fetch_news_for_competition(code, lang="en", page=page)
        if not articles:
            break
        page_new = 0
        for idx, article in enumerate(articles, start=1):
            url = article.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            image_url = article.get("image") or article.get("image_url")
            if image_url:
                try:
                    article_id = news_client.article_external_id(article) or f"{page}_{idx}"
                    local = data_importer.download_news_image(f"{code}_{article_id}", image_url)
                    if local:
                        article["image_local"] = local
                except Exception:
                    pass
            db_client.upsert_news_article(article, code, league_id)
            count += 1
            page_new += 1
            _sleep(aux_delay / 4)
        if page_new == 0:
            break
        _sleep(aux_delay)
    return count


def hydrate_top_cards_for_competition(code: str) -> int:
    if not api_football.has_api_football():
        return 0
    league_id = api_football.league_id_for_competition(code)
    if not league_id:
        return 0
    cards = api_football.get_top_cards_for_league(league_id, limit=50)
    for card in cards:
        db_client.upsert_top_card(card, code)
    return len(cards)


def hydrate_odds_for_competition(code: str, aux_delay: float) -> dict[str, int]:
    sport = odds_client.COMPETITION_TO_SPORT.get(code.upper())
    if not sport or not odds_client.has_odds_api():
        return {"odds_snapshots": 0, "odds_markets_raw": 0}

    events = odds_client.fetch_odds_for_sport(sport, ttl=900)
    snapshot_count = 0
    raw_count = 0

    for event in events:
        home = event.get("home_team")
        away = event.get("away_team")
        event_id = event.get("id")
        if not home or not away:
            continue
        match_key = f"{code}:{home}:{away}"
        best_h2h = odds_client._best_h2h(event)
        if best_h2h:
            db_client.upsert_odds_snapshot(match_key, code, home, away, best_h2h)
            snapshot_count += 1
        try:
            full_event = odds_client.fetch_event_odds(
                sport,
                event_id,
                markets="h2h,totals,spreads,btts",
            ) or event
            db_client.upsert_odds_market_raw(
                match_key,
                event_id,
                sport,
                code,
                home,
                away,
                event.get("commence_time"),
                full_event,
            )
            raw_count += 1
        except Exception as exc:
            _log(f"[{code}] odds markets failed for {home} vs {away}: {exc}")
        _sleep(aux_delay)

    return {"odds_snapshots": snapshot_count, "odds_markets_raw": raw_count}


def hydrate_h2h_for_competition(code: str, h2h_limit: int | None, aux_delay: float) -> int:
    if not api_football.has_api_football():
        return 0
    matches = db_client.get_matches_for_competition(code)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        home = (match.get("home_team_name") or "").strip()
        away = (match.get("away_team_name") or "").strip()
        if not home or not away:
            continue
        key = tuple(sorted((home, away)))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((home, away))
    pairs = _slice(pairs, h2h_limit)

    count = 0
    for home, away in pairs:
        try:
            rows = api_football.get_h2h(home, away, limit=10)
            if rows:
                count += db_client.upsert_h2h_records(home, away, rows)
        except Exception as exc:
            _log(f"[{code}] h2h failed for {home} vs {away}: {exc}")
        _sleep(aux_delay)
    return count


def _parse_team_form_raw(raw: dict) -> list[dict]:
    items = raw.get("form") or raw.get("events") or []
    result = []
    for match in items:
        if not isinstance(match, dict):
            continue
        home = match.get("homeTeam") or {}
        away = match.get("awayTeam") or {}
        score = match.get("homeScore") or {}
        result.append({
            "match_date": match.get("startTimestamp") or match.get("date") or "",
            "opponent": away.get("name") or home.get("name") or "",
            "result": match.get("result") or "",
            "goals_for": score.get("current") or 0,
            "goals_against": (match.get("awayScore") or {}).get("current") or 0,
            "venue": "home" if match.get("isHome") else "away",
            "competition": (match.get("tournament") or {}).get("name") or "",
        })
    return result


def hydrate_competition(
    code: str,
    fd_delay: float,
    aux_delay: float,
    match_limit: int | None,
    player_limit: int | None,
    h2h_limit: int | None,
    news_max_pages: int,
    include_footapi7: bool,
    core_only: bool,
) -> dict[str, Any]:
    code = code.upper()
    summary: dict[str, Any] = {"code": code}

    _log(f"[{code}] core import starting")
    summary["core"] = data_importer.import_competition(code, delay=fd_delay)

    _log(f"[{code}] squad hydration starting")
    summary["players"] = _safe_stage(
        "players",
        code,
        hydrate_players_for_competition,
        code,
        player_limit,
        aux_delay,
        default={"players": 0, "player_profiles": 0},
    )
    if isinstance(summary["players"], dict):
        db_client.log_import("players", code, int(summary["players"].get("players", 0)), "ok")
        db_client.log_import("player_profiles", code, int(summary["players"].get("player_profiles", 0)), "ok")

    if core_only:
        return summary

    _log(f"[{code}] finished-match hydration starting")
    summary["match_details"] = _safe_stage(
        "match_details",
        code,
        hydrate_match_details_for_competition,
        code,
        match_limit,
        aux_delay,
        include_footapi7,
        default={},
    )
    if isinstance(summary["match_details"], dict):
        for entity in ("match_events", "match_lineups", "match_statistics", "match_incidents", "match_graphs", "match_shotmaps"):
            db_client.log_import(entity, code, int(summary["match_details"].get(entity, 0)), "ok")

    _log(f"[{code}] team enrichments starting")
    summary["team_enrichments"] = _safe_stage(
        "team_enrichments",
        code,
        hydrate_team_enrichments_for_competition,
        code,
        aux_delay,
        include_footapi7,
        default={},
    )
    if isinstance(summary["team_enrichments"], dict):
        for entity in ("team_transfers", "team_form"):
            db_client.log_import(entity, code, int(summary["team_enrichments"].get(entity, 0)), "ok")

    _log(f"[{code}] player season stats starting")
    summary["player_season_stats"] = _safe_stage(
        "player_season_stats",
        code,
        hydrate_player_season_stats_for_competition,
        code,
        player_limit,
        aux_delay,
        include_footapi7,
        default=0,
    )

    _log(f"[{code}] news starting")
    summary["news_articles"] = _safe_stage(
        "news",
        code,
        hydrate_news_for_competition,
        code,
        aux_delay,
        news_max_pages,
        default=0,
    )

    _log(f"[{code}] top cards starting")
    summary["top_cards"] = _safe_stage("top_cards", code, hydrate_top_cards_for_competition, code, default=0)

    _log(f"[{code}] odds starting")
    summary["odds"] = _safe_stage(
        "odds",
        code,
        hydrate_odds_for_competition,
        code,
        aux_delay,
        default={"odds_snapshots": 0, "odds_markets_raw": 0},
    )
    if isinstance(summary["odds"], dict):
        db_client.log_import("odds", code, int(summary["odds"].get("odds_snapshots", 0)), "ok")
        db_client.log_import("odds_markets_raw", code, int(summary["odds"].get("odds_markets_raw", 0)), "ok")

    _log(f"[{code}] h2h starting")
    summary["h2h_records"] = _safe_stage(
        "h2h_records",
        code,
        hydrate_h2h_for_competition,
        code,
        h2h_limit,
        aux_delay,
        default=0,
    )

    return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full DKSPORTS data hydrator.")
    parser.add_argument("--codes", help="Comma-separated competition codes. Default: all free-tier competitions.")
    parser.add_argument("--match-limit", type=int, default=None, help="Optional cap on finished matches per competition.")
    parser.add_argument("--player-limit", type=int, default=None, help="Optional cap on squad players per competition.")
    parser.add_argument("--h2h-limit", type=int, default=None, help="Optional cap on unique H2H pairs per competition.")
    parser.add_argument("--news-max-pages", type=int, default=4, help="How many football-news11 pages to archive per competition.")
    parser.add_argument("--fd-delay", type=float, default=7.5, help="Delay between football-data.org calls.")
    parser.add_argument("--aux-delay", type=float, default=0.75, help="Delay between auxiliary API calls.")
    parser.add_argument(
        "--core-only",
        action="store_true",
        help="Only import competitions, teams, standings, scorers, matches, and squad players.",
    )
    parser.add_argument(
        "--include-footapi7",
        action="store_true",
        help="Attempt footapi7-based enrichments. Disabled by default because the current project does not yet have a reliable footapi7 ID mapping layer.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is required.", file=sys.stderr)
        return 1
    if not os.environ.get("FOOTBALL_DATA_API_KEY"):
        print("FOOTBALL_DATA_API_KEY is required for full hydration.", file=sys.stderr)
        return 1

    db_client.init_db()

    if args.codes:
        codes = [code.strip().upper() for code in args.codes.split(",") if code.strip()]
    else:
        codes = [c["code"] for c in api_client.FREE_TIER_COMPETITIONS]

    _log(f"starting full hydrator for: {', '.join(codes)}")
    _log(
        f"limits: match_limit={args.match_limit}, player_limit={args.player_limit}, "
        f"h2h_limit={args.h2h_limit}, news_max_pages={args.news_max_pages}, "
        f"fd_delay={args.fd_delay}, aux_delay={args.aux_delay}, "
        f"include_footapi7={args.include_footapi7}, core_only={args.core_only}"
    )

    summaries: list[dict[str, Any]] = []
    for code in codes:
        summaries.append(
            hydrate_competition(
                code,
                fd_delay=args.fd_delay,
                aux_delay=args.aux_delay,
                match_limit=args.match_limit,
                player_limit=args.player_limit,
                h2h_limit=args.h2h_limit,
                news_max_pages=args.news_max_pages,
                include_footapi7=args.include_footapi7,
                core_only=args.core_only,
            )
        )

    final_stats = db_client.get_db_stats()
    _log("hydration complete")
    print(json.dumps({"summaries": summaries, "db_stats": final_stats}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
