"""Download any missing league emblems and team crests for offline use.

Safe to re-run: existing files are skipped. Copies crest files between league
folders when a team plays in multiple competitions instead of re-downloading.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db_client
import data_importer
from api_client import FREE_TIER_COMPETITIONS

BASE_DIR = Path(__file__).resolve().parent
LEAGUE_LOGOS_DIR = Path(data_importer.LEAGUE_LOGOS_DIR)
TEAM_LOGOS_DIR = Path(data_importer.TEAM_LOGOS_DIR)


def sync_league_emblems() -> tuple[int, int]:
    downloaded = skipped = 0
    for comp in FREE_TIER_COMPETITIONS:
        code, url = comp.get("code"), comp.get("emblem")
        if not code or not url:
            continue
        ext = os.path.splitext(url.split("?")[0])[1] or ".png"
        dest = LEAGUE_LOGOS_DIR / f"{data_importer._safe_filename(code)}{ext}"
        if dest.exists():
            skipped += 1
        elif data_importer.download_image(url, str(dest), "competition", code):
            print(f"  [+] emblem {code} -> {dest.name}")
            downloaded += 1
        else:
            print(f"  [!] emblem {code} FAILED ({url})")
            continue
        with db_client._Cursor() as cur:
            cur.execute(
                "UPDATE competitions SET emblem_local=%s WHERE code=%s",
                (str(dest), code),
            )
    return downloaded, skipped


def _existing_crest_file(team_name: str, crest_url: str) -> Path | None:
    ext = os.path.splitext(crest_url.split("?")[0])[1] or ".png"
    filename = data_importer._safe_filename(team_name) + ext
    if TEAM_LOGOS_DIR.exists():
        for league_dir in TEAM_LOGOS_DIR.iterdir():
            candidate = league_dir / filename
            if candidate.exists():
                return candidate
    return None


def sync_team_crests() -> tuple[int, int, int]:
    with db_client._Cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT t.id, t.name, t.crest_url, c.name AS league_name
            FROM teams t
            JOIN competition_teams ct ON ct.team_id = t.id
            LEFT JOIN competitions c ON c.code = ct.competition_code
            WHERE t.crest_url IS NOT NULL AND t.crest_url <> ''
            ORDER BY t.id
            """
        )
        rows = cur.fetchall()

    downloaded = copied = skipped = 0
    for row in rows:
        team_id = row["id"]
        name = row["name"] or "unknown"
        crest_url = row["crest_url"]
        league_name = row["league_name"] or "Other"
        dest = Path(data_importer._local_logo_path(name, league_name, crest_url))

        if dest.exists():
            skipped += 1
        else:
            source = _existing_crest_file(name, crest_url)
            if source and source.suffix == dest.suffix:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                db_client.register_image(crest_url, str(dest), "team", str(team_id))
                copied += 1
                print(f"  [c] {name} ({league_name}) copied")
            elif data_importer.download_image(crest_url, str(dest), "team", str(team_id)):
                downloaded += 1
                print(f"  [+] {name} ({league_name}) downloaded")
            else:
                print(f"  [!] {name} ({league_name}) FAILED ({crest_url})")
                continue

        with db_client._Cursor() as cur:
            cur.execute(
                "UPDATE teams SET crest_local=%s WHERE id=%s AND (crest_local IS NULL OR crest_local='')",
                (str(dest), team_id),
            )
    return downloaded, copied, skipped


def main() -> int:
    print("== League emblems ==")
    d, s = sync_league_emblems()
    print(f"emblems: {d} downloaded, {s} already local")
    print("== Team crests ==")
    d, c, s = sync_team_crests()
    print(f"crests: {d} downloaded, {c} copied, {s} already local")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
