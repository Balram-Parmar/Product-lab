"""PostgreSQL connection helpers shared by the CLI tools and the web app."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class DatabaseNotConfigured(RuntimeError):
    """Raised when DATABASE_URL is missing."""


def get_conn(**kwargs: Any) -> psycopg2.extensions.connection:
    """Open a connection to DATABASE_URL with a predictable UTC session."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    conn = psycopg2.connect(dsn, connect_timeout=5, **kwargs)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn


def get_dict_cursor(conn: psycopg2.extensions.connection):
    """Return a cursor that yields RealDictRow objects."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
