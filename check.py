#!/usr/bin/env python3
"""Pre-flight check: mock payload, database connection, and schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2  # noqa: E402

from db.connect import DatabaseNotConfigured, get_conn  # noqa: E402

REQUIRED_TABLES = ("users", "tweets", "hashtags", "topics", "tweet_topics")
MOCK_FILE = PROJECT_ROOT / "mock.json"


def check_mock() -> bool:
    print("mock.json")
    if not MOCK_FILE.exists():
        print(f"  FAIL  missing {MOCK_FILE.name}")
        return False

    try:
        payload = json.loads(MOCK_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"  FAIL  invalid JSON: {exc}")
        return False

    tweets = payload.get("data")
    users = (payload.get("includes") or {}).get("users")
    if not isinstance(tweets, list) or not tweets:
        print("  FAIL  no 'data' array with tweets")
        return False
    if not isinstance(users, list) or not users:
        print("  FAIL  no 'includes.users' array (needed for author data)")
        return False

    user_ids = {str(user.get("id")) for user in users}
    missing_author = [t.get("id") for t in tweets if str(t.get("author_id")) not in user_ids]
    missing_fields = [
        t.get("id") for t in tweets if not all(t.get(k) for k in ("id", "text", "author_id"))
    ]
    if missing_author or missing_fields:
        print(f"  FAIL  {len(set(missing_author + missing_fields))} tweet(s) missing author or fields")
        return False

    print(f"  OK    {len(tweets)} tweets, {len(users)} authors")
    return True


def check_database() -> bool:
    print("database")
    try:
        conn = get_conn()
    except DatabaseNotConfigured as exc:
        print(f"  FAIL  {exc}")
        return False
    except psycopg2.OperationalError as exc:
        print(f"  FAIL  cannot connect: {exc}")
        return False

    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0].split(",")[0]
            print(f"  OK    {version}")

            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            present = {row[0] for row in cur.fetchall()}
            missing = [name for name in REQUIRED_TABLES if name not in present]
            if missing:
                print(f"  FAIL  missing tables: {', '.join(missing)}")
                print("        run: psql \"$DATABASE_URL\" -f db/create_schema.sql")
                return False

            cur.execute(
                """
                SELECT
                    (SELECT count(*) FROM tweets),
                    (SELECT count(*) FROM users),
                    (SELECT count(*) FROM hashtags),
                    (SELECT count(*) FROM topics)
                """
            )
            tweets, users, tags, topics = cur.fetchone()
            print(f"  OK    schema ready: {tweets} tweets, {users} users, {tags} hashtags, {topics} runs")
    except psycopg2.Error as exc:
        print(f"  FAIL  query error: {exc}")
        return False
    finally:
        conn.close()

    return True


def main() -> int:
    mock_ok = check_mock()
    print()
    db_ok = check_database()
    print()
    if mock_ok and db_ok:
        print("Ready. Run: python ingest/fetch_and_load.py")
        return 0
    print("Not ready. Fix the failures above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
