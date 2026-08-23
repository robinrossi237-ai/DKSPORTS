"""Archive extra RapidAPI datasets for offline use."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import api_client
import data_importer
import db_client
import news_client
import rapidapi_extra_client


def _news_article_key(article: dict[str, Any]) -> str:
    return str(article.get("url") or article.get("id") or article.get("alias") or "").strip()


def archive_news_for_competition(code: str, *, max_pages: int, lang: str, delay: float) -> dict[str, int]:
    code = code.upper()
    league_id = news_client.league_id_for_competition(code)
    if not league_id:
        return {"articles": 0, "images": 0}

    seen: set[str] = set()
    article_count = 0
    image_count = 0

    for page in range(1, max(1, max_pages) + 1):
        articles = news_client.fetch_news_for_competition(code, lang=lang, page=page)
        if not articles:
            break
        page_new = 0
        for article in articles:
            key = _news_article_key(article)
            if not key or key in seen:
                continue
            seen.add(key)
            image_url = article.get("image") or article.get("image_url") or ""
            article_id = news_client.article_external_id(article)
            if image_url and article_id:
                local = data_importer.download_news_image(f"{code}_{article_id}", image_url)
                if local:
                    article["image_local"] = local
                    image_count += 1
            db_client.upsert_news_article(article, code, league_id)
            article_count += 1
            page_new += 1
            if delay > 0:
                time.sleep(delay)
        if page_new == 0:
            break

    return {"articles": article_count, "images": image_count}


def archive_all_news(codes: list[str], *, max_pages: int, lang: str, delay: float) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {code: {"articles": 0, "images": 0} for code in codes}
    seen_by_code: dict[str, set[str]] = {code: set() for code in codes}
    done: set[str] = set()
    quota_hit = False

    for page in range(1, max(1, max_pages) + 1):
        made_progress = False
        for code in codes:
            if code in done:
                continue
            league_id = news_client.league_id_for_competition(code)
            if not league_id:
                done.add(code)
                continue
            try:
                articles = news_client.fetch_news_for_competition(code, lang=lang, page=page)
            except Exception as exc:
                summary[code]["error"] = str(exc)  # type: ignore[index]
                if "DAILY quota" in str(exc) or "quota" in str(exc).lower():
                    quota_hit = True
                done.add(code)
                continue
            if not articles:
                done.add(code)
                continue

            page_new = 0
            for article in articles:
                key = _news_article_key(article)
                if not key or key in seen_by_code[code]:
                    continue
                seen_by_code[code].add(key)
                image_url = article.get("image") or article.get("image_url") or ""
                article_id = news_client.article_external_id(article)
                if image_url and article_id:
                    local = data_importer.download_news_image(f"{code}_{article_id}", image_url)
                    if local:
                        article["image_local"] = local
                        summary[code]["images"] += 1
                db_client.upsert_news_article(article, code, league_id)
                summary[code]["articles"] += 1
                page_new += 1
                made_progress = True
                if delay > 0:
                    time.sleep(delay)
            if page_new == 0:
                done.add(code)

        if quota_hit or len(done) == len(codes) or not made_progress:
            break
    return summary


def archive_epl_player_bios(player_ids: list[str], *, delay: float) -> dict[str, int]:
    stored = 0
    for player_id in player_ids:
        raw = rapidapi_extra_client.fetch_epl_player_bio(player_id)
        team_history = raw.get("teamHistory") if isinstance(raw, dict) else []
        first_team = team_history[0] if isinstance(team_history, list) and team_history else {}
        image_url = first_team.get("logo") if isinstance(first_team, dict) else None
        entity_name = f"EPL player {player_id}"
        db_client.upsert_external_api_payload(
            "english-premiere-league1",
            "player_bio",
            player_id,
            raw,
            competition_code="PL",
            entity_name=entity_name,
            image_url=image_url,
        )
        stored += 1
        if delay > 0:
            time.sleep(delay)
    return {"player_bios": stored}


def archive_la_liga2_matches() -> dict[str, int | str]:
    raw = rapidapi_extra_client.fetch_la_liga2_matches_raw()
    db_client.upsert_external_api_payload(
        "la-liga2",
        "matches_raw",
        "matches",
        raw,
        competition_code="PD2",
        entity_name="La Liga 2 matches feed",
    )
    return {"matches_raw": 1}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive extra RapidAPI feeds for offline DKSPORTS use.")
    parser.add_argument("--codes", help="Comma-separated competition codes for news. Default: all mapped news competitions.")
    parser.add_argument("--news-max-pages", type=int, default=4, help="How many news pages to archive per competition.")
    parser.add_argument("--news-lang", default="en", help="News language code.")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between provider calls.")
    parser.add_argument("--epl-player-ids", default="", help="Comma-separated english-premiere-league1 player IDs to archive.")
    parser.add_argument("--include-laliga2", action="store_true", help="Attempt the provided la-liga2 matches endpoint and cache the raw payload.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is required.", file=sys.stderr)
        return 1

    db_client.init_db()

    if args.codes:
        codes = [code.strip().upper() for code in args.codes.split(",") if code.strip()]
    else:
        codes = [
            c["code"]
            for c in api_client.FREE_TIER_COMPETITIONS
            if news_client.league_id_for_competition(c["code"])
        ]

    output: dict[str, Any] = {}
    if news_client.has_news_api():
        output["news"] = archive_all_news(
            codes,
            max_pages=args.news_max_pages,
            lang=args.news_lang,
            delay=max(0.0, args.delay),
        )
    else:
        output["news"] = {"error": "RAPIDAPI_FOOTBALL_NEWS_KEY is not configured."}

    player_ids = [pid.strip() for pid in args.epl_player_ids.split(",") if pid.strip()]
    if player_ids:
        output["epl_player_bios"] = archive_epl_player_bios(player_ids, delay=max(0.0, args.delay))

    if args.include_laliga2:
        try:
            output["la_liga2"] = archive_la_liga2_matches()
        except Exception as exc:
            output["la_liga2"] = {"error": str(exc)}

    output["db_stats"] = db_client.get_db_stats()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
