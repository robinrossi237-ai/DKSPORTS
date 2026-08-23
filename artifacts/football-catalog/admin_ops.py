from __future__ import annotations

import copy
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from urllib.parse import urlparse, unquote

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import api_client
import api_football
import data_importer
import db_client
import full_hydrator
import shotmap_client
import sportsdb_client

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
WORKSPACE_DIR = BASE_DIR.parent.parent
BACKUPS_DIR = WORKSPACE_DIR / "dksports-backups"

PORTABLE_DIRS = [
    STATIC_DIR / "img",
    STATIC_DIR / "vendor",
]

_status_lock = threading.Lock()
_job_thread: threading.Thread | None = None
_status: dict[str, Any] = {
    "running": False,
    "operation": "",
    "label": "",
    "progress": 0,
    "total": 0,
    "current_task": "",
    "started_at": None,
    "finished_at": None,
    "error": "",
    "result": {},
    "log": [],
    "history": [],
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ensure_backup_dir() -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUPS_DIR


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", (value or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned.strip("._ ") or time.strftime("backup_%Y%m%d_%H%M%S")


def _deepcopy_status() -> dict[str, Any]:
    with _status_lock:
        return copy.deepcopy(_status)


def get_status() -> dict[str, Any]:
    return _deepcopy_status()


def _reset_status(operation: str, label: str, total: int = 1) -> None:
    with _status_lock:
        _status.update({
            "running": True,
            "operation": operation,
            "label": label,
            "progress": 0,
            "total": max(1, total),
            "current_task": "",
            "started_at": _now_iso(),
            "finished_at": None,
            "error": "",
            "result": {},
            "log": [],
        })


def _update_status(**kwargs: Any) -> None:
    with _status_lock:
        _status.update(kwargs)


def _append_history(entry: dict[str, Any]) -> None:
    with _status_lock:
        history = list(_status.get("history") or [])
        history.insert(0, entry)
        _status["history"] = history[:20]


def _log(message: str, level: str = "info") -> None:
    entry = {"ts": time.strftime("%H:%M:%S"), "level": level, "msg": message}
    with _status_lock:
        logs = list(_status.get("log") or [])
        logs.append(entry)
        _status["log"] = logs[-300:]
    print(f"[admin-ops:{level}] {message}", flush=True)


def _set_step(step: int, total: int, current_task: str) -> None:
    _update_status(progress=max(0, step), total=max(1, total), current_task=current_task)


def _finish(ok: bool, result: dict[str, Any] | None = None, error: str = "") -> None:
    finished = _now_iso()
    entry = {
        "operation": _status.get("operation"),
        "label": _status.get("label"),
        "started_at": _status.get("started_at"),
        "finished_at": finished,
        "ok": ok,
        "error": error,
        "result": result or {},
    }
    _append_history(entry)
    _update_status(
        running=False,
        finished_at=finished,
        error=error,
        result=result or {},
        current_task="Complete" if ok else "Failed",
        progress=_status.get("total") or 1,
    )


def _run_in_background(operation: str, label: str, target: Callable[..., dict[str, Any]], *args: Any, total: int = 1, **kwargs: Any) -> bool:
    global _job_thread
    with _status_lock:
        if _status.get("running"):
            return False
        _reset_status(operation, label, total=total)

    def _runner() -> None:
        try:
            result = target(*args, **kwargs) or {}
        except Exception as exc:
            _log(str(exc), "error")
            try:
                db_client.log_import("admin_operation", operation, 0, "error", str(exc))
            except Exception:
                pass
            _finish(False, error=str(exc))
            return
        try:
            db_client.log_import("admin_operation", operation, 1, "ok")
        except Exception:
            pass
        _finish(True, result=result)

    _job_thread = threading.Thread(target=_runner, name=f"admin-{operation}", daemon=True)
    _job_thread.start()
    return True


def list_backups(limit: int = 20) -> list[dict[str, Any]]:
    backup_dir = _ensure_backup_dir()
    backups: list[dict[str, Any]] = []
    for path in sorted(backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        backups.append({
            "name": path.name,
            "path": str(path),
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return backups[:limit]


def _find_pg_binary(name: str) -> str:
    candidate = shutil.which(name)
    if candidate:
        return candidate
    patterns = [
        rf"C:\Program Files\PostgreSQL\*\bin\{name}.exe",
        rf"C:\Program Files (x86)\PostgreSQL\*\bin\{name}.exe",
    ]
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(glob.glob(pattern))
    if matches:
        matches.sort(reverse=True)
        return matches[0]
    raise RuntimeError(f"{name} executable was not found. Install PostgreSQL client tools or add them to PATH.")


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured.")
    return value


def _pg_connection_env() -> tuple[dict[str, str], str]:
    url = _database_url()
    parsed = urlparse(url)
    dbname = (parsed.path or "").lstrip("/")
    if not dbname:
        raise RuntimeError("DATABASE_URL is missing the database name.")
    env = os.environ.copy()
    env["PGHOST"] = parsed.hostname or "localhost"
    env["PGPORT"] = str(parsed.port or 5432)
    env["PGUSER"] = unquote(parsed.username or "")
    env["PGDATABASE"] = dbname
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    return env, dbname


def _run_subprocess(command: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    _log(" ".join(command))
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if stdout:
        _log(stdout[-4000:])
    if completed.returncode != 0:
        if stderr:
            _log(stderr[-4000:], "error")
        raise RuntimeError(stderr or stdout or f"Command failed with exit code {completed.returncode}")
    if stderr:
        _log(stderr[-4000:], "warn")
    return stdout


def _competition_label(code: str) -> str:
    code = (code or "").upper()
    for comp in api_client.FREE_TIER_COMPETITIONS:
        if comp.get("code") == code:
            return comp.get("name") or code
    row = next((c for c in db_client.get_all_competitions() if c.get("code") == code), None)
    return (row or {}).get("name") or code


def run_core_import_for_league(code: str, delay: float = 8.0) -> dict[str, Any]:
    code = (code or "").upper()
    total = 3
    _set_step(0, total, f"Preparing core import for {code}")
    db_client.init_db()
    _set_step(1, total, f"Running core import for {code}")
    result = data_importer.import_competition(code, delay=delay)
    _set_step(2, total, f"Collecting DB stats for {code}")
    stats = db_client.get_db_stats()
    _set_step(3, total, f"Core import complete for {code}")
    return {"mode": "core_import", "code": code, "summary": result, "db_stats": stats}


def run_hydration_for_league(
    code: str,
    *,
    core_only: bool = False,
    include_footapi7: bool = False,
    match_limit: int | None = None,
    player_limit: int | None = None,
    h2h_limit: int | None = None,
    news_max_pages: int = 4,
    fd_delay: float = 7.5,
    aux_delay: float = 0.75,
) -> dict[str, Any]:
    code = (code or "").upper()
    total = 4
    _set_step(0, total, f"Preparing hydrator for {code}")
    db_client.init_db()
    _set_step(1, total, f"Hydrating league {code}")
    summary = full_hydrator.hydrate_competition(
        code,
        fd_delay=fd_delay,
        aux_delay=aux_delay,
        match_limit=match_limit,
        player_limit=player_limit,
        h2h_limit=h2h_limit,
        news_max_pages=news_max_pages,
        include_footapi7=include_footapi7,
        core_only=core_only,
    )
    _set_step(2, total, f"Loading recent failures for {code}")
    failures = [row for row in db_client.get_import_failures(limit=40) if row.get("entity_code") == code][:20]
    _set_step(3, total, f"Collecting DB stats for {code}")
    stats = db_client.get_db_stats()
    _set_step(4, total, f"Hydration complete for {code}")
    return {"mode": "hydrate_league", "code": code, "summary": summary, "failures": failures, "db_stats": stats}


def _download_team_crest(team: dict[str, Any], league_name: str) -> str | None:
    crest_url = team.get("crest")
    if not crest_url:
        return None
    dest = data_importer._local_logo_path(team.get("name", "unknown"), league_name, crest_url)
    ok = data_importer.download_image(crest_url, dest, "team", str(team.get("id") or ""))
    return dest if ok else None


def run_hydration_for_team(
    team_id: int,
    *,
    competition_code: str | None = None,
    season: str | None = None,
    include_footapi7: bool = False,
    match_limit: int | None = 30,
    player_limit: int | None = None,
    aux_delay: float = 0.75,
) -> dict[str, Any]:
    db_client.init_db()
    team_id = int(team_id)
    existing_competitions = db_client.get_competitions_for_team(team_id)
    if not competition_code and existing_competitions:
        competition_code = (existing_competitions[0] or {}).get("code")

    total = 6 if include_footapi7 else 4
    _set_step(0, total, f"Loading live team {team_id}")
    team = api_client.get_team(team_id)
    competition_code = (competition_code or "").upper() or None
    if not competition_code:
        running = team.get("runningCompetitions") or []
        if running:
            competition_code = (running[0] or {}).get("code")
    if not competition_code:
        raise RuntimeError(f"No competition code found for team {team_id}.")

    competition_name = _competition_label(competition_code)
    season = season or db_client.get_season_for_code(competition_code) or str(time.gmtime().tm_year)

    _set_step(1, total, f"Caching team profile for {team.get('name')}")
    try:
        sportsdb_client.attach_team_people(team, competition_code)
    except Exception as exc:
        _log(f"SportsDB enrichment failed for {team.get('name')}: {exc}", "warn")
    if api_football.has_api_football():
        try:
            api_football.attach_coach_photo(team)
            if not any((p.get("_photo") for p in (team.get("squad") or []))):
                api_football.attach_player_photos(team)
        except Exception as exc:
            _log(f"API Football enrichment failed for {team.get('name')}: {exc}", "warn")

    crest_local = _download_team_crest(team, competition_name)
    if crest_local:
        team["_crest_local"] = crest_local
    media_counts = data_importer.cache_team_people_assets(team, competition_name)
    db_client.upsert_team(team, competition_code, season)

    squad = list(team.get("squad") or [])
    players_imported = 0
    player_profiles = 0
    _set_step(2, total, f"Saving squad for {team.get('name')}")
    for player in full_hydrator._slice(squad, player_limit):
        db_client.upsert_player(player, team=team, competition_code=competition_code, season=season)
        players_imported += 1
        sportsdb_profile = player.get("_sportsdb_profile")
        if isinstance(sportsdb_profile, dict):
            db_client.upsert_player_profile(
                player.get("name") or "",
                team.get("name") or "",
                team.get("id"),
                {
                    **sportsdb_client.profile_from_player(sportsdb_profile),
                    "photo_local": player.get("_photo_local"),
                },
            )
            player_profiles += 1

    matches_imported = 0
    _set_step(3, total, f"Saving matches for {team.get('name')}")
    for status_name, limit_value in (("FINISHED", match_limit or 30), ("SCHEDULED", min(match_limit or 20, 20))):
        try:
            payload = api_client.get_team_matches(team_id, status=status_name, limit=limit_value)
        except Exception as exc:
            _log(f"Team matches fetch failed ({status_name}) for {team.get('name')}: {exc}", "warn")
            continue
        for match in payload.get("matches") or []:
            match_code = (((match.get("competition") or {}).get("code")) or competition_code).upper()
            match_season = (((match.get("season") or {}).get("startDate")) or "")[:4] or season
            db_client.upsert_match(match, match_code, match_season)
            matches_imported += 1

    deep_counts: dict[str, Any] = {}
    if include_footapi7:
        _set_step(4, total, f"Running deep team enrichments for {team.get('name')}")
        deep_counts["team_enrichments"] = full_hydrator.hydrate_team_enrichments_for_competition(
            competition_code,
            aux_delay,
            True,
            team_ids={team_id},
        )
        _set_step(5, total, f"Running team match/player hydration for {team.get('name')}")
        deep_counts["match_details"] = full_hydrator.hydrate_match_details_for_competition(
            competition_code,
            match_limit,
            aux_delay,
            True,
            season=season,
            team_ids={team_id},
        )
        deep_counts["player_season_stats"] = full_hydrator.hydrate_player_season_stats_for_competition(
            competition_code,
            player_limit,
            aux_delay,
            True,
            season_override=season,
            team_ids={team_id},
        )
    _set_step(total, total, f"Team hydration complete for {team.get('name')}")
    return {
        "mode": "hydrate_team",
        "team_id": team_id,
        "competition_code": competition_code,
        "season": season,
        "summary": {
            "team_name": team.get("name"),
            "players_imported": players_imported,
            "player_profiles": player_profiles,
            "matches_imported": matches_imported,
            "media": media_counts,
            **deep_counts,
        },
        "db_stats": db_client.get_db_stats(),
    }


def run_hydration_for_season(
    code: str,
    season: str,
    *,
    include_footapi7: bool = False,
    match_limit: int | None = None,
    player_limit: int | None = None,
    h2h_limit: int | None = None,
    news_max_pages: int = 4,
    aux_delay: float = 0.75,
) -> dict[str, Any]:
    code = (code or "").upper()
    season = (season or "").strip()
    if not season:
        raise RuntimeError("Season is required.")
    db_client.init_db()
    existing = set(db_client.get_known_seasons(limit_per_competition=20).get(code, []))
    if season not in existing:
        raise RuntimeError(f"Season {season} is not currently cached for {code}. Import the base competition first.")

    total = 5
    _set_step(0, total, f"Preparing season hydration for {code} {season}")
    summary: dict[str, Any] = {"code": code, "season": season}

    _log(
        f"Historical core import is not wired in this project; season hydration for {code} {season} will enrich already cached rows.",
        "warn",
    )
    summary["core_note"] = "Deep hydration ran against cached season rows; core historical import is not available in this project."

    _set_step(1, total, f"Hydrating match details for {code} {season}")
    summary["match_details"] = full_hydrator.hydrate_match_details_for_competition(
        code,
        match_limit,
        aux_delay,
        include_footapi7,
        season=season,
    )
    _set_step(2, total, f"Hydrating player season stats for {code} {season}")
    summary["player_season_stats"] = full_hydrator.hydrate_player_season_stats_for_competition(
        code,
        player_limit,
        aux_delay,
        include_footapi7,
        season_override=season,
    )
    _set_step(3, total, f"Refreshing competition-level enrichments for {code}")
    summary["team_enrichments"] = full_hydrator.hydrate_team_enrichments_for_competition(
        code,
        aux_delay,
        include_footapi7,
    )
    summary["news_articles"] = full_hydrator.hydrate_news_for_competition(code, aux_delay, max_pages=news_max_pages)
    summary["top_cards"] = full_hydrator.hydrate_top_cards_for_competition(code)
    summary["odds"] = full_hydrator.hydrate_odds_for_competition(code, aux_delay)
    summary["h2h_records"] = full_hydrator.hydrate_h2h_for_competition(code, h2h_limit, aux_delay)
    _set_step(4, total, f"Collecting failures for {code} {season}")
    failures = [
        row for row in db_client.get_import_failures(limit=40)
        if row.get("entity_code") in {code, f"{code}:{season}"}
    ]
    _set_step(5, total, f"Season hydration complete for {code} {season}")
    return {
        "mode": "hydrate_season",
        "code": code,
        "season": season,
        "summary": summary,
        "failures": failures[:20],
        "db_stats": db_client.get_db_stats(),
    }


def create_portable_backup(backup_name: str | None = None) -> dict[str, Any]:
    backup_dir = _ensure_backup_dir()
    env, dbname = _pg_connection_env()
    pg_dump = _find_pg_binary("pg_dump")
    total = 4
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_name(backup_name or f"dksports_{dbname}_{stamp}")
    archive_path = backup_dir / f"{safe_name}.zip"
    if archive_path.exists():
        safe_name = _safe_name(f"{safe_name}_{stamp}")
        archive_path = backup_dir / f"{safe_name}.zip"

    with TemporaryDirectory(prefix=f"{safe_name}_", dir=str(backup_dir)) as tmp_dir:
        staging = Path(tmp_dir)
        dump_path = staging / "database.dump"
        media_root = staging / "media"
        manifest_path = staging / "manifest.json"

        _set_step(1, total, "Dumping PostgreSQL database")
        _run_subprocess(
            [
                pg_dump,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(dump_path),
                dbname,
            ],
            env=env,
        )

        _set_step(2, total, "Collecting local media and offline vendor assets")
        for folder in PORTABLE_DIRS:
            if folder.exists():
                target = media_root / folder.relative_to(BASE_DIR)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(folder, target, dirs_exist_ok=True)

        _set_step(3, total, "Writing backup manifest")
        manifest = {
            "name": safe_name,
            "created_at": _now_iso(),
            "database_name": dbname,
            "db_stats": db_client.get_db_stats(),
            "portable_dirs": [str(path.relative_to(BASE_DIR)).replace("\\", "/") for path in PORTABLE_DIRS if path.exists()],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        _set_step(4, total, "Packing backup archive")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in staging.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(staging))

    return {
        "mode": "backup",
        "name": safe_name,
        "archive_path": str(archive_path),
        "size_mb": round(archive_path.stat().st_size / (1024 * 1024), 2),
        "backups": list_backups(),
    }


def restore_portable_backup(backup_name: str, *, confirm_text: str = "") -> dict[str, Any]:
    backup_dir = _ensure_backup_dir()
    if (confirm_text or "").strip().upper() != "RESTORE":
        raise RuntimeError('Type "RESTORE" to confirm the restore operation.')
    backup_path = backup_dir / backup_name
    if not backup_path.exists():
        raise RuntimeError(f"Backup not found: {backup_name}")

    env, dbname = _pg_connection_env()
    pg_restore = _find_pg_binary("pg_restore")
    total = 4

    with TemporaryDirectory(prefix="restore_", dir=str(backup_dir)) as tmp_dir:
        extract_root = Path(tmp_dir)
        _set_step(1, total, f"Extracting {backup_name}")
        with zipfile.ZipFile(backup_path, "r") as zf:
            zf.extractall(extract_root)

        dump_path = extract_root / "database.dump"
        if not dump_path.exists():
            raise RuntimeError("The selected backup archive does not contain database.dump.")

        _set_step(2, total, f"Restoring PostgreSQL database {dbname}")
        _run_subprocess(
            [
                pg_restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                dbname,
                str(dump_path),
            ],
            env=env,
        )

        _set_step(3, total, "Restoring local media and vendor assets")
        media_root = extract_root / "media"
        if media_root.exists():
            for child in media_root.rglob("*"):
                if child.is_dir():
                    continue
                relative = child.relative_to(media_root)
                target = BASE_DIR / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)

        _set_step(4, total, "Re-initializing database metadata")
        db_client.init_db()

    return {
        "mode": "restore",
        "backup_name": backup_name,
        "db_stats": db_client.get_db_stats(),
        "backups": list_backups(),
    }


def start_core_import(code: str, *, delay: float = 8.0) -> bool:
    return _run_in_background("core_import", f"Core import • {code.upper()}", run_core_import_for_league, code, delay=delay, total=3)


def start_league_hydration(code: str, **kwargs: Any) -> bool:
    return _run_in_background("hydrate_league", f"Hydrate league • {code.upper()}", run_hydration_for_league, code, total=4, **kwargs)


def start_team_hydration(team_id: int, **kwargs: Any) -> bool:
    return _run_in_background("hydrate_team", f"Hydrate team • {team_id}", run_hydration_for_team, team_id, total=6 if kwargs.get("include_footapi7") else 4, **kwargs)


def start_season_hydration(code: str, season: str, **kwargs: Any) -> bool:
    return _run_in_background("hydrate_season", f"Hydrate season • {code.upper()} {season}", run_hydration_for_season, code, season, total=5, **kwargs)


def start_backup(backup_name: str | None = None) -> bool:
    return _run_in_background("backup", "Create portable backup", create_portable_backup, backup_name, total=4)


def start_restore(backup_name: str, *, confirm_text: str) -> bool:
    return _run_in_background("restore", f"Restore backup • {backup_name}", restore_portable_backup, backup_name, confirm_text=confirm_text, total=4)
