"""Backfill real player and coach media from TheSportsDB using existing DB squads."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import db_client
import data_importer
import sportsdb_client
from api_client import FREE_TIER_COMPETITIONS


def _log(message: str) -> None:
    safe = str(message).encode("cp1252", errors="replace").decode("cp1252", errors="replace")
    print(f"[sportsdb-sync] {safe}", flush=True)


def _competition_name(code: str) -> str:
    for comp in FREE_TIER_COMPETITIONS:
        if comp["code"] == code:
            return comp["name"]
    return code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill real player and coach media from TheSportsDB.")
    parser.add_argument("--codes", help="Comma-separated competition codes. Default: all imported competitions.")
    parser.add_argument(
        "--teams",
        help="Comma-separated team name filters. Matches against team name and short name.",
    )
    parser.add_argument("--delay", type=float, default=2.2, help="Delay between team-level TheSportsDB calls.")
    parser.add_argument(
        "--player-search-budget",
        type=int,
        default=0,
        help="Extra direct player-name searches to spend per team for missing portraits.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is required.", file=sys.stderr)
        return 1

    db_client.init_db()
    args = parse_args(argv or sys.argv[1:])

    if args.codes:
        codes = [code.strip().upper() for code in args.codes.split(",") if code.strip()]
    else:
        codes = [row["code"] for row in db_client.get_all_competitions() if row.get("code")]
    team_filters = [value.strip().lower() for value in (args.teams or "").split(",") if value.strip()]

    for code in codes:
        teams = db_client.get_teams_for_competition(code)
        if team_filters:
            teams = [
                team for team in teams
                if any(
                    filt in ((team.get("name") or "").lower())
                    or filt in ((team.get("short_name") or "").lower())
                    for filt in team_filters
                )
            ]
        players = db_client.get_players_for_competition(code)
        players_by_team: dict[int, list[dict]] = {}
        for player in players:
            team_id = player.get("current_team_id")
            if isinstance(team_id, int):
                players_by_team.setdefault(team_id, []).append(player)

        competition_name = _competition_name(code)
        _log(f"[{code}] syncing {len(teams)} teams")

        for team in teams:
            team_id = team.get("id")
            if not isinstance(team_id, int):
                continue
            squad_rows = players_by_team.get(team_id, [])
            if not squad_rows:
                continue
            team_payload = {
                "id": team_id,
                "name": team.get("name"),
                "shortName": team.get("short_name") or team.get("name"),
                "tla": team.get("tla"),
                "coach": {"name": team.get("coach_name") or ""},
                "squad": [
                    {
                        "id": row.get("id"),
                        "name": row.get("name"),
                        "position": row.get("position"),
                        "nationality": row.get("nationality"),
                        "shirtNumber": row.get("shirt_number"),
                    }
                    for row in squad_rows
                ],
            }

            try:
                enrich = sportsdb_client.attach_team_people(
                    team_payload,
                    code,
                    search_budget=max(0, args.player_search_budget),
                )
                cache = data_importer.cache_team_people_assets(team_payload, competition_name)
                if team_payload.get("_coach_photo_local"):
                    db_client.upsert_team(
                        {
                            "id": team_id,
                            "name": team.get("name"),
                            "shortName": team.get("short_name"),
                            "tla": team.get("tla"),
                            "crest": team.get("crest_url"),
                            "area": {"name": team.get("area_name")},
                            "founded": team.get("founded"),
                            "venue": team.get("venue"),
                            "website": team.get("website"),
                            "clubColors": team.get("club_colors"),
                            "coach": {"name": team.get("coach_name")},
                            "_coach_photo": team_payload.get("_coach_photo_local"),
                        },
                        code,
                        db_client.get_season_for_code(code),
                    )

                local_map = {
                    p.get("id"): p.get("_photo_local")
                    for p in team_payload.get("squad") or []
                    if p.get("_photo_local")
                }
                sportsdb_map = {
                    p.get("id"): p
                    for p in team_payload.get("squad") or []
                    if p.get("id")
                }
                for row in squad_rows:
                    pid = row.get("id")
                    if not isinstance(pid, int):
                        continue
                    payload = {
                        "id": pid,
                        "name": row.get("name"),
                        "firstName": row.get("first_name"),
                        "lastName": row.get("last_name"),
                        "position": row.get("position"),
                        "dateOfBirth": row.get("date_of_birth"),
                        "nationality": row.get("nationality"),
                        "shirtNumber": row.get("shirt_number"),
                        "photo_url": row.get("photo_url"),
                        "photo_local": local_map.get(pid),
                    }
                    if pid in sportsdb_map:
                        payload.update({
                            "_photo": sportsdb_map[pid].get("_photo"),
                            "_photo_local": sportsdb_map[pid].get("_photo_local"),
                        })
                    db_client.upsert_player(
                        payload,
                        team={
                            "id": team_id,
                            "name": team.get("name"),
                            "shortName": team.get("short_name"),
                        },
                    )
                    sportsdb_profile = sportsdb_map.get(pid, {}).get("_sportsdb_profile")
                    if isinstance(sportsdb_profile, dict):
                        db_client.upsert_player_profile(
                            row.get("name") or "",
                            team.get("name") or "",
                            team_id,
                            {
                                **sportsdb_client.profile_from_player(sportsdb_profile),
                                "photo_local": local_map.get(pid),
                            },
                        )

                _log(
                    f"[{code}] {team.get('name')}: "
                    f"team_found={enrich.get('team_found', 0)} "
                    f"source_players={enrich.get('source_players', 0)} "
                    f"search_attempts={enrich.get('search_attempts', 0)} "
                    f"search_hits={enrich.get('search_hits', 0)} "
                    f"sportsdb_players={enrich.get('players', 0)} "
                    f"profiles={enrich.get('profiles', 0)} "
                    f"coach={enrich.get('coaches', 0)} "
                    f"cached_players={cache.get('players', 0)}"
                )
            except Exception as exc:
                _log(f"[{code}] {team.get('name')} error: {exc}")
            time.sleep(max(0.0, args.delay))

    _log(f"complete: {db_client.get_db_stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
