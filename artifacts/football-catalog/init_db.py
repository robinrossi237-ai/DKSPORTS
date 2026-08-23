"""Bootstrap the DKSPORTS PostgreSQL schema."""
from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    import db_client
except ModuleNotFoundError as exc:
    if exc.name == "psycopg2":
        print(
            "psycopg2 is not installed. Run `python -m pip install -r requirements.txt` first.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print(
            "DATABASE_URL is not set. Add it to .env or export it before running this script.",
            file=sys.stderr,
        )
        return 1

    try:
        db_client.init_db()
        stats = db_client.get_db_stats()
    except Exception as exc:
        print(f"Schema bootstrap failed: {exc}", file=sys.stderr)
        return 1

    total_rows = sum(int(value or 0) for value in stats.values())
    print("Schema bootstrap complete.")
    print(f"Tracked tables: {len(stats)}")
    print(f"Current total rows: {total_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
