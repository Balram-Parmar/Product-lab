#!/usr/bin/env python3
"""Flask UI for searching the PostgreSQL post archive."""

from __future__ import annotations

import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2  # noqa: E402
from flask import Flask, g, redirect, render_template, request, url_for  # noqa: E402
from markupsafe import Markup, escape  # noqa: E402

from db.connect import DatabaseNotConfigured, get_conn, get_dict_cursor  # noqa: E402

LATEST_LIMIT = 8
MAX_TEXT_FILTER = 200
LANG_RE = re.compile(r"^[a-z]{2,3}$")
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LANGUAGES = [
    ("", "Any language"),
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("pt", "Portuguese"),
    ("ja", "Japanese"),
    ("ar", "Arabic"),
    ("hi", "Hindi"),
    ("ko", "Korean"),
    ("it", "Italian"),
    ("nl", "Dutch"),
    ("tr", "Turkish"),
    ("ru", "Russian"),
    ("id", "Indonesian"),
    ("und", "Undetermined"),
]

RESULT_COLUMNS = """
    t.tweet_id,
    t.created_at,
    t.text,
    t.lang,
    t.like_count,
    t.retweet_count,
    t.reply_count,
    t.quote_count,
    u.username,
    u.display_name,
    u.verified,
    COALESCE(h.tags, ARRAY[]::text[]) AS hashtags
"""

RESULT_FROM = """
    FROM tweets t
    JOIN users u ON u.user_id = t.user_id
    LEFT JOIN LATERAL (
        SELECT array_agg(hg.tag ORDER BY lower(hg.tag)) AS tags
        FROM hashtags hg
        WHERE hg.tweet_id = t.tweet_id
    ) h ON TRUE
"""


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def get_db():
    """Return one connection per request."""
    if "db" not in g:
        g.db = get_conn()
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _clean(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _parse_start(raw: str, errors: list[str]) -> datetime | None:
    if not raw:
        return None
    try:
        if DATE_ONLY_RE.fullmatch(raw):
            day = date.fromisoformat(raw)
            return datetime.combine(day, dt_time.min, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        errors.append("Start date is not a valid date.")
        return None


def _parse_end(raw: str, errors: list[str]) -> tuple[datetime | None, bool]:
    """Return (boundary, inclusive). A date-only end covers the whole day."""
    if not raw:
        return None, True
    try:
        if DATE_ONLY_RE.fullmatch(raw):
            day = date.fromisoformat(raw) + timedelta(days=1)
            return datetime.combine(day, dt_time.min, tzinfo=timezone.utc), False
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed, True
    except ValueError:
        errors.append("End date is not a valid date.")
        return None, True


def read_filters(args) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "q": _clean(args.get("q"), MAX_TEXT_FILTER),
        "hashtag": _clean(args.get("hashtag"), 80).lstrip("#"),
        "username": _clean(args.get("username"), 80).lstrip("@"),
        "lang": _clean(args.get("lang"), 8).lower(),
        "topic_id": _clean(args.get("topic_id"), 12),
        "start_date": _clean(args.get("start_date"), 32),
        "end_date": _clean(args.get("end_date"), 32),
        "errors": [],
        "chips": [],
        "active": False,
        "start_dt": None,
        "end_dt": None,
        "end_inclusive": True,
        "page": 1,
        "topic_label": "",
    }

    raw_q = args.get("q") or ""
    if len(raw_q.strip()) > MAX_TEXT_FILTER:
        filters["errors"].append(f"Text search is limited to {MAX_TEXT_FILTER} characters.")

    if filters["lang"] and not LANG_RE.fullmatch(filters["lang"]):
        filters["errors"].append("Language must be a two- or three-letter code.")
        filters["lang"] = ""

    if filters["topic_id"] and not filters["topic_id"].isdigit():
        filters["errors"].append("Ingestion run must be a number.")
        filters["topic_id"] = ""

    try:
        filters["page"] = max(1, int(args.get("page", 1)))
    except (TypeError, ValueError):
        filters["page"] = 1

    filters["start_dt"] = _parse_start(filters["start_date"], filters["errors"])
    filters["end_dt"], filters["end_inclusive"] = _parse_end(
        filters["end_date"], filters["errors"]
    )
    if filters["start_dt"] and filters["end_dt"] and filters["end_dt"] < filters["start_dt"]:
        filters["errors"].append("End date is before the start date.")

    return filters


def build_chips(filters: dict[str, Any]) -> list[dict[str, str]]:
    chips: list[dict[str, str]] = []
    if filters["q"]:
        chips.append({"label": "Text", "value": filters["q"]})
    if filters["hashtag"]:
        chips.append({"label": "Hashtag", "value": f"#{filters['hashtag']}"})
    if filters["username"]:
        chips.append({"label": "Author", "value": f"@{filters['username']}"})
    if filters["start_date"]:
        chips.append({"label": "From", "value": filters["start_date"]})
    if filters["end_date"]:
        chips.append({"label": "To", "value": filters["end_date"]})
    if filters["lang"]:
        language = dict(LANGUAGES).get(filters["lang"], filters["lang"])
        chips.append({"label": "Language", "value": language})
    if filters["topic_label"]:
        chips.append({"label": "Ingestion run", "value": filters["topic_label"]})
    return chips


def build_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    """Compose the WHERE clause from fixed fragments and bound parameters."""
    clauses = ["1 = 1"]
    params: list[Any] = []

    if filters["q"]:
        clauses.append(r"t.text ILIKE %s ESCAPE '\'")
        params.append(_like(filters["q"]))

    if filters["hashtag"]:
        clauses.append(
            """
            EXISTS (
                SELECT 1 FROM hashtags hf
                WHERE hf.tweet_id = t.tweet_id
                  AND lower(hf.tag) = lower(%s)
            )
            """
        )
        params.append(filters["hashtag"])

    if filters["username"]:
        clauses.append(r"u.username ILIKE %s ESCAPE '\'")
        params.append(_like(filters["username"]))

    if filters["start_dt"]:
        clauses.append("t.created_at >= %s")
        params.append(filters["start_dt"])

    if filters["end_dt"]:
        operator = "<=" if filters["end_inclusive"] else "<"
        clauses.append(f"t.created_at {operator} %s")
        params.append(filters["end_dt"])

    if filters["lang"]:
        clauses.append("lower(t.lang) = %s")
        params.append(filters["lang"])

    if filters["topic_id"]:
        clauses.append(
            """
            EXISTS (
                SELECT 1 FROM tweet_topics tt
                WHERE tt.tweet_id = t.tweet_id
                  AND tt.topic_id = %s
            )
            """
        )
        params.append(int(filters["topic_id"]))

    return " AND ".join(clauses), params


def _like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def fetch_stats(db) -> dict[str, int]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM tweets)  AS tweets,
                (SELECT count(*) FROM users)   AS users,
                (SELECT count(*) FROM topics)  AS topics
            """
        )
        row = cur.fetchone()
    return {"tweets": row[0], "users": row[1], "topics": row[2]}


def fetch_topics(db) -> list[dict[str, Any]]:
    with get_dict_cursor(db) as cur:
        cur.execute(
            """
            SELECT t.topic_id, t.query, t.run_at,
                   (SELECT count(*) FROM tweet_topics tt
                    WHERE tt.topic_id = t.topic_id) AS tweet_count
            FROM topics t
            ORDER BY t.run_at DESC, t.topic_id DESC
            LIMIT 100
            """
        )
        return cur.fetchall()


def fetch_latest(db) -> list[dict[str, Any]]:
    with get_dict_cursor(db) as cur:
        cur.execute(
            f"""
            SELECT {RESULT_COLUMNS}
            {RESULT_FROM}
            ORDER BY t.created_at DESC NULLS LAST, t.tweet_id DESC
            LIMIT %s
            """,
            (LATEST_LIMIT,),
        )
        return cur.fetchall()


def run_search(
    db, filters: dict[str, Any], per_page: int
) -> tuple[list[dict[str, Any]], int, float, int, int]:
    where, params = build_where(filters)
    started = time.perf_counter()

    with db.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM tweets t JOIN users u ON u.user_id = t.user_id "
            f"WHERE {where}",
            params,
        )
        total = cur.fetchone()[0]

    pages = max(1, math.ceil(total / per_page))
    page = min(filters["page"], pages)
    offset = (page - 1) * per_page

    with get_dict_cursor(db) as cur:
        cur.execute(
            f"""
            SELECT {RESULT_COLUMNS}
            {RESULT_FROM}
            WHERE {where}
            ORDER BY t.created_at DESC NULLS LAST, t.tweet_id DESC
            LIMIT %s OFFSET %s
            """,
            [*params, per_page, offset],
        )
        rows = cur.fetchall()

    elapsed_ms = (time.perf_counter() - started) * 1000
    return rows, total, elapsed_ms, page, pages


def page_numbers(current: int, pages: int) -> list[int | None]:
    """A small page window with None marking an ellipsis."""
    visible = {1, pages}
    visible.update(range(max(1, current - 2), min(pages, current + 2) + 1))
    numbers: list[int | None] = []
    previous = 0
    for number in sorted(visible):
        if previous and number - previous > 1:
            numbers.append(None)
        numbers.append(number)
        previous = number
    return numbers


def page_url(number: int) -> str:
    args = request.args.to_dict(flat=True)
    args.pop("page", None)
    if number > 1:
        args["page"] = str(number)
    return url_for("search", **args)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "dev-only-change-me"),
        PER_PAGE=_env_int("PAGE_SIZE", 25),
        MAX_CONTENT_LENGTH=64 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.teardown_appcontext(close_db)

    @app.template_filter("num")
    def format_number(value: Any) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "0"

    @app.template_filter("date")
    def format_date(value: datetime | None) -> str:
        if not value:
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%d %b %Y")

    @app.template_filter("time")
    def format_time(value: datetime | None) -> str:
        if not value:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%H:%M UTC")

    @app.template_filter("highlight")
    def highlight(text: str, term: str) -> Markup:
        """Escape the post text, then wrap literal matches in <mark>."""
        text = text or ""
        term = (term or "").strip()
        if not term:
            return escape(text)

        parts: list[Markup] = []
        cursor = 0
        for match in re.finditer(re.escape(term), text, re.IGNORECASE):
            parts.append(escape(text[cursor : match.start()]))
            parts.append(Markup("<mark>") + escape(match.group(0)) + Markup("</mark>"))
            cursor = match.end()
        parts.append(escape(text[cursor:]))
        return Markup("").join(parts)

    def render_page():
        filters = read_filters(request.args)
        stats = None
        topics: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        total = 0
        elapsed_ms = None
        pager = None
        latest = False
        db_error = None

        try:
            db = get_db()
            stats = fetch_stats(db)
            topics = fetch_topics(db)

            if filters["topic_id"]:
                match = next(
                    (t for t in topics if str(t["topic_id"]) == filters["topic_id"]),
                    None,
                )
                if match is None:
                    filters["errors"].append("That ingestion run does not exist.")
                    filters["topic_id"] = ""
                else:
                    filters["topic_label"] = match["query"]

            filters["active"] = any(
                filters[key]
                for key in (
                    "q",
                    "hashtag",
                    "username",
                    "lang",
                    "topic_id",
                    "start_date",
                    "end_date",
                )
            )
            filters["chips"] = build_chips(filters)

            if filters["active"]:
                results, total, elapsed_ms, page, pages = run_search(
                    db, filters, app.config["PER_PAGE"]
                )
                pager = {
                    "page": page,
                    "pages": pages,
                    "prev": page_url(page - 1) if page > 1 else None,
                    "next": page_url(page + 1) if page < pages else None,
                    "numbers": page_numbers(page, pages),
                }
            else:
                latest = True
                results = fetch_latest(db)
        except DatabaseNotConfigured as exc:
            db_error = str(exc)
            app.logger.error("Database not configured: %s", exc)
        except psycopg2.Error:
            app.logger.exception("Database query failed")
            db_error = (
                "The archive database could not run this request. "
                "Check that PostgreSQL is running and the schema is loaded."
            )
        finally:
            filters["chips"] = build_chips(filters)

        return render_template(
            "index.html",
            filters=filters,
            stats=stats,
            topics=topics,
            results=results,
            total=total,
            elapsed_ms=elapsed_ms,
            pager=pager,
            latest=latest,
            db_error=db_error,
            languages=LANGUAGES,
            page_size=app.config["PER_PAGE"],
            page_url=page_url,
        )

    @app.get("/")
    def index():
        return render_page()

    @app.route("/search", methods=["GET", "POST"])
    def search():
        if request.method == "POST":
            values = {
                key: value.strip()
                for key, value in request.form.items()
                if value and value.strip()
            }
            return redirect(url_for("search", **values), code=303)
        return render_page()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=_env_int("FLASK_PORT", 5000),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
