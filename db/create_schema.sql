-- Schema for the X post archive.
-- Apply with: psql "$DATABASE_URL" -f db/create_schema.sql
-- The script is idempotent: safe to run more than once.

BEGIN;

-- Trigram index support for ILIKE text search (trusted extension, PG 13+).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    username         TEXT NOT NULL,
    display_name     TEXT,
    created_at       TIMESTAMPTZ,
    followers_count  INTEGER,
    verified         BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tweets (
    tweet_id       TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    text           TEXT NOT NULL,
    created_at     TIMESTAMPTZ,
    lang           TEXT,
    like_count     INTEGER NOT NULL DEFAULT 0,
    retweet_count  INTEGER NOT NULL DEFAULT 0,
    reply_count    INTEGER NOT NULL DEFAULT 0,
    quote_count    INTEGER NOT NULL DEFAULT 0,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hashtags (
    tweet_id  TEXT NOT NULL REFERENCES tweets (tweet_id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    PRIMARY KEY (tweet_id, tag)
);

CREATE TABLE IF NOT EXISTS topics (
    topic_id  SERIAL PRIMARY KEY,
    query     TEXT NOT NULL,
    run_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tweet_topics (
    topic_id  INTEGER NOT NULL REFERENCES topics (topic_id) ON DELETE CASCADE,
    tweet_id  TEXT NOT NULL REFERENCES tweets (tweet_id) ON DELETE CASCADE,
    PRIMARY KEY (topic_id, tweet_id)
);

-- Newest-first browsing and date-range filters.
CREATE INDEX IF NOT EXISTS tweets_created_at_idx
    ON tweets (created_at DESC);
CREATE INDEX IF NOT EXISTS tweets_user_created_idx
    ON tweets (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tweets_lang_idx
    ON tweets (lang);

-- Case-insensitive username and hashtag lookups.
CREATE INDEX IF NOT EXISTS users_username_lower_idx
    ON users (lower(username));
CREATE INDEX IF NOT EXISTS hashtags_tag_lower_idx
    ON hashtags (lower(tag));

-- Substring search on post text.
CREATE INDEX IF NOT EXISTS tweets_text_trgm_idx
    ON tweets USING GIN (text gin_trgm_ops);

CREATE INDEX IF NOT EXISTS tweet_topics_tweet_id_idx
    ON tweet_topics (tweet_id);
CREATE INDEX IF NOT EXISTS topics_run_at_idx
    ON topics (run_at DESC);

COMMIT;
