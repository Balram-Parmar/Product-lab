#!/usr/bin/env python3
"""Load an X API v2-shaped JSON file into PostgreSQL.

This project runs against a bundled mock payload (``mock.json``) instead of
calling the live X API. The payload keeps the exact shape returned by
``GET /2/tweets/search/recent`` with ``expansions=author_id``, so the parsing
and upsert logic would work unchanged against a real response.

Usage:
    python ingest/fetch_and_load.py
    python ingest/fetch_and_load.py --query "generative ai" --file mock.json
    python ingest/fetch_and_load.py --max 10
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow `python ingest/fetch_and_load.py` from the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2  # noqa: E402

from db.connect import DatabaseNotConfigured, get_conn  # noqa: E402

DEFAULT_FILE = PROJECT_ROOT / "mock.json"
DEFAULT_QUERY = "generative ai"


@dataclass
class LoadStats:
    """Counters for one ingestion run."""

    topic_id: int = 0
    tweets_seen: int = 0
    tweets_new: int = 0
    tweets_updated: int = 0
    users_new: int = 0
    users_updated: int = 0
    hashtags_added: int = 0
    links_added: int = 0
    skipped: int = 0

    def summary(self) -> str:
        return (
            f"topic #{self.topic_id}: {self.tweets_seen} tweets seen, "
            f"{self.tweets_new} new, {self.tweets_updated} updated, "
            f"{self.skipped} skipped\n"
            f"  users:    {self.users_new} new, {self.users_updated} updated\n"
            f"  hashtags: {self.hashtags_added} new rows\n"
            f"  links:    {self.links_added} new topic/tweet links"
        )


def _as_int(value: Any) -> int:
    """Coerce API metric values to a non-negative int."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _hashtags_for(tweet: dict[str, Any]) -> list[str]:
    """Return unique hashtags for a tweet, preserving first-seen casing."""
    entities = tweet.get("entities") or {}
    seen: set[str] = set()
    tags: list[str] = []
    for item in entities.get("hashtags") or []:
        tag = (item.get("tag") or "").strip().lstrip("#")
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            tags.append(tag)
    return tags


def _upsert_user(cur, user: dict[str, Any]) -> bool:
    """Insert or refresh a user. Returns True when the row was new."""
    metrics = user.get("public_metrics") or {}
    cur.execute(
        """
        INSERT INTO users (
            user_id, username, display_name, created_at,
            followers_count, verified, fetched_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (user_id) DO UPDATE SET
            username        = EXCLUDED.username,
            display_name    = EXCLUDED.display_name,
            created_at      = COALESCE(EXCLUDED.created_at, users.created_at),
            followers_count = EXCLUDED.followers_count,
            verified        = EXCLUDED.verified,
            fetched_at      = now()
        RETURNING (xmax = 0) AS inserted
        """,
        (
            user["id"],
            user.get("username") or f"user_{user['id']}",
            user.get("name"),
            user.get("created_at"),
            _as_int(metrics.get("followers_count")),
            bool(user.get("verified", False)),
        ),
    )
    row = cur.fetchone()
    return bool(row and row[0])


def _upsert_tweet(cur, tweet: dict[str, Any]) -> bool:
    """Insert or refresh a tweet. Returns True when the row was new."""
    metrics = tweet.get("public_metrics") or {}
    cur.execute(
        """
        INSERT INTO tweets (
            tweet_id, user_id, text, created_at, lang,
            like_count, retweet_count, reply_count, quote_count, fetched_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (tweet_id) DO UPDATE SET
            user_id       = EXCLUDED.user_id,
            text          = EXCLUDED.text,
            created_at    = COALESCE(EXCLUDED.created_at, tweets.created_at),
            lang          = EXCLUDED.lang,
            like_count    = EXCLUDED.like_count,
            retweet_count = EXCLUDED.retweet_count,
            reply_count   = EXCLUDED.reply_count,
            quote_count   = EXCLUDED.quote_count,
            fetched_at    = now()
        RETURNING (xmax = 0) AS inserted
        """,
        (
            tweet["id"],
            tweet["author_id"],
            tweet["text"],
            tweet.get("created_at"),
            tweet.get("lang"),
            _as_int(metrics.get("like_count")),
            _as_int(metrics.get("retweet_count")),
            _as_int(metrics.get("reply_count")),
            _as_int(metrics.get("quote_count")),
        ),
    )
    row = cur.fetchone()
    return bool(row and row[0])


def _insert_hashtags(cur, tweet_id: str, tags: list[str]) -> int:
    added = 0
    for tag in tags:
        cur.execute(
            """
            INSERT INTO hashtags (tweet_id, tag)
            VALUES (%s, %s)
            ON CONFLICT (tweet_id, tag) DO NOTHING
            """,
            (tweet_id, tag),
        )
        added += cur.rowcount
    return added


def load_payload(
    conn,
    payload: dict[str, Any],
    topic_query: str,
    *,
    max_tweets: int | None = None,
) -> LoadStats:
    """Parse an X API v2 payload and upsert it into PostgreSQL."""
    tweets = payload.get("data") or []
    if not isinstance(tweets, list):
        raise ValueError("Payload field 'data' must be a list of tweets.")

    users = {
        str(user["id"]): user
        for user in (payload.get("includes") or {}).get("users") or []
        if user.get("id")
    }

    stats = LoadStats()
    seen_users: set[str] = set()
    if max_tweets:
        tweets = tweets[:max_tweets]

    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO topics (query, run_at) VALUES (%s, now()) RETURNING topic_id",
            (topic_query,),
        )
        stats.topic_id = cur.fetchone()[0]

        for tweet in tweets:
            tweet_id = tweet.get("id")
            author_id = tweet.get("author_id")
            text = tweet.get("text")
            if not tweet_id or not author_id or not text:
                stats.skipped += 1
                continue

            user = users.get(str(author_id))
            if user is None:
                # A tweet without its expanded author cannot satisfy the
                # users FK. Count it instead of inventing an author.
                stats.skipped += 1
                continue

            stats.tweets_seen += 1
            if str(author_id) not in seen_users:
                if _upsert_user(cur, user):
                    stats.users_new += 1
                else:
                    stats.users_updated += 1
                seen_users.add(str(author_id))

            if _upsert_tweet(cur, tweet):
                stats.tweets_new += 1
            else:
                stats.tweets_updated += 1

            stats.hashtags_added += _insert_hashtags(cur, tweet_id, _hashtags_for(tweet))

            cur.execute(
                """
                INSERT INTO tweet_topics (topic_id, tweet_id)
                VALUES (%s, %s)
                ON CONFLICT (topic_id, tweet_id) DO NOTHING
                """,
                (stats.topic_id, tweet_id),
            )
            stats.links_added += cur.rowcount

    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load an X API v2 JSON payload (mock.json) into PostgreSQL."
    )
    parser.add_argument(
        "--file",
        default=str(DEFAULT_FILE),
        help=f"JSON payload to load (default: {DEFAULT_FILE.name})",
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"Topic label stored in topics.query (default: {DEFAULT_QUERY!r})",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Load at most N tweets from the file (default: all)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(args.file).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()

    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        print(f"error: payload not found: {path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {path}: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, dict):
        print("error: payload root must be a JSON object", file=sys.stderr)
        return 2

    try:
        conn = get_conn()
    except DatabaseNotConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except psycopg2.OperationalError as exc:
        print(f"error: cannot connect to PostgreSQL: {exc}", file=sys.stderr)
        return 2

    try:
        stats = load_payload(
            conn,
            payload,
            args.query,
            max_tweets=args.max if args.max > 0 else None,
        )
    except (KeyError, ValueError) as exc:
        print(f"error: malformed payload: {exc}", file=sys.stderr)
        return 2
    except psycopg2.Error as exc:
        print(f"error: database write failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"Loaded {path.name} -> {args.query!r}")
    print(stats.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
