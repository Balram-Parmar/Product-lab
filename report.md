# Sift — one-page report

## What was built

A three-stage pipeline for X posts: an X API v2-shaped JSON payload
(`mock.json`) is parsed and normalized into PostgreSQL, then queried through a
Flask page. The ingestion path is deliberately offline: the bundled payload is
a saved `GET /2/tweets/search/recent` response, so the same parser and upserts
run without network access or API credits. The web app exposes combinable
filters for text, hashtag, author, language, date range, and ingestion run, with
pagination, result counts, and human-readable filter chips.

## Schema design

Five tables in third normal form:

- `users` — one row per X account, keyed by the X `user_id`. Stores the mutable
  profile fields (`username`, `display_name`, `followers_count`, `verified`) and
  `fetched_at`.
- `tweets` — keyed by `tweet_id`, with `user_id` as a foreign key, the post text,
  `created_at`, `lang`, and the four public metrics. Metrics are mutable and are
  refreshed on conflict.
- `hashtags` — `(tweet_id, tag)` composite primary key. Tags are extracted from
  `entities.hashtags`, not by parsing text, and deduplicated case-insensitively
  during ingestion. This keeps tag filters exact and avoids a many-to-many table
  for a value that is naturally scoped to one post.
- `topics` — one row per ingestion run (`query`, `run_at`), an append-only audit
  trail rather than a mutable “current query” field.
- `tweet_topics` — junction table linking runs to tweets. Deleting a run removes
  its links (`ON DELETE CASCADE`) but not the archived posts, so a tweet can
  survive across multiple runs.

The natural X identifiers are the primary keys, which makes re-ingestion
idempotent: `ON CONFLICT ... DO UPDATE` refreshes users, posts, and metrics, while
`ON CONFLICT DO NOTHING` protects hashtags and run links. `fetched_at` records
the last refresh. Indexes support newest-first browsing (`created_at DESC`),
per-author timelines, language filters, case-insensitive username/hashtag
lookups, and substring text search through a `pg_trgm` GIN index on
`tweets.text`. The schema script is idempotent and wrapped in a transaction.

## Ingestion and API shape

The payload mirrors the X API v2 recent-search endpoint:

```
GET https://api.x.com/2/tweets/search/recent
query=generative ai lang:en -is:retweet
max_results=100
tweet.fields=created_at,public_metrics,lang,entities,author_id
user.fields=created_at,public_metrics,username,name,verified
expansions=author_id
```

`ingest/fetch_and_load.py` validates `data` and `includes.users`, inserts a
`topics` row, upserts authors and tweets, extracts and deduplicates hashtags,
links each tweet to the run, and commits once per file. It reports new versus
updated rows using `RETURNING (xmax = 0)`. A tweet whose expanded author is
missing is skipped rather than assigned a fabricated user, preserving the
foreign-key guarantee. The loader also accepts `--file` and `--max` for
repeatable test runs.

To use the live endpoint, a collector would add the bearer header, paginate on
`meta.next_token` until the requested maximum, and pass the same JSON to
`load_payload`. No other stage changes.

## Rate-limit handling

A live collector must treat limits as normal operation:

- **429 Too Many Requests** — read `x-rate-limit-reset` (epoch seconds), sleep
  until that time plus a small margin, then retry the same request. Cap both the
  wait and the retry count so a bad reset header cannot stall a job forever.
- **5xx and timeouts** — exponential backoff with jitter (for example 1, 2, 4,
  8 seconds), then fail the run with a clear message.
- **401** — invalid or expired bearer token; do not retry.
- **402** — credits depleted. This is why the submitted build loads `mock.json`
  instead of calling X.
- **403** — the account's access level does not include recent search.

The design persists per-page progress (tweets, hashtags, and run links are
committed together), so a retry after a partial failure is safe: the natural
primary keys make the repeated page an upsert rather than a duplicate.

## Web query safety and UX

The Flask app builds a fixed `WHERE` clause from constant fragments and binds
every user value as a parameter. There is no input concatenation. `ILIKE`
patterns escape `\`, `%`, and `_` with an explicit `ESCAPE` clause, so a search
for `100%` is literal. `ORDER BY` is fixed; `LIMIT` and `OFFSET` are bound
integers, and the page number is clamped to the result set. Date-only “to”
values are converted to an exclusive next-midnight boundary in UTC, which makes
the range inclusive of the selected day. Jinja autoescaping and an
escape-then-mark highlight filter protect rendered post text.

The UI shows the effective filters as chips, the total count, query time, and
pagination. The landing page doubles as an archive view with the eight most
recent posts. Ingestion runs are selectable, which demonstrates the
`tweet_topics` relationship rather than leaving it unused.

## Limitations

- The dataset is a 24-post mock payload; it is not live X data.
- No authentication on the web app; it is a local assignment tool.
- Metrics refresh only when a post is re-ingested.
- Full-text ranking is out of scope; text search is substring matching backed by
  a trigram index, not `tsvector` relevance.
