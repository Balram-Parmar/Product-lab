<p align="center">
  <img src="screenshots/logo.png" alt="Sift logo" width="120">
</p>

# X posts → PostgreSQL → web search

A small, complete data pipeline: an X API v2-shaped payload is normalized into
PostgreSQL, then searched through a Flask page with filters for text, hashtag,
author, language, date range, and ingestion run.

This build intentionally **does not call the X API**. It loads the bundled
`mock.json`, which has the same shape as a `GET /2/tweets/search/recent`
response with `expansions=author_id`. The parser, upserts, schema, and web app
are the same ones a live collector would use.

## Stack

- Python 3.10+ (tested on 3.14)
- Flask 3 + Jinja
- PostgreSQL 13+ (tested on 18)
- `psycopg2` with parameterized SQL
- `python-dotenv`

## Project layout

```
.
├── check.py                     # DB + payload pre-flight check
├── mock.json                    # X API v2-shaped dataset (24 posts, 8 authors)
├── requirements.txt
├── pyproject.toml               # Vercel entrypoint + ruff config
├── .python-version              # Python 3.12 for Vercel
├── .env.example
├── DEPLOY.md                    # Vercel + hosted Postgres steps
├── db/
│   ├── create_schema.sql        # tables, indexes, pg_trgm
│   ├── connect.py               # DATABASE_URL connection helper
│   └── setup_local.sh           # one-time local role/db/auth setup
├── ingest/
│   └── fetch_and_load.py        # parse mock.json → upsert into Postgres
├── public/
│   └── assets/                  # served by Vercel's CDN at /assets/*
│       ├── fonts.css
│       ├── fonts/
│       └── styles.css
├── web/
│   ├── app.py                   # Flask routes, filters, safe SQL
│   └── templates/
│       ├── base.html
│       └── index.html
├── screenshots/
├── report.md                    # one-page design/API/rate-limit report
└── README.md
```

## Setup

### 1. PostgreSQL

On Fedora/RHEL:

```bash
sudo dnf install -y postgresql-server postgresql-contrib
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql
```

Then create the role, database, and schema from the `DATABASE_URL` in `.env`:

```bash
sudo bash db/setup_local.sh
```

The script creates the `tweets` role and `tweetsdb` database, switches the two
loopback `pg_hba.conf` lines from `ident` to `scram-sha-256` (after saving a
timestamped backup), reloads PostgreSQL, and applies `db/create_schema.sql`.
It is safe to re-run.

Prefer doing it by hand? Create a role and database, edit `pg_hba.conf` so
`127.0.0.1/32` and `::1/128` use `scram-sha-256`, reload PostgreSQL, then:

```bash
psql "postgresql://tweets:YOUR_PASSWORD@localhost:5432/tweetsdb" \
  -f db/create_schema.sql
```

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment

For a fresh clone, copy the template (this workspace already has a configured
`.env`; do not overwrite it):

```bash
[ -f .env ] || cp .env.example .env
```

Fill in `DATABASE_URL` and `FLASK_SECRET_KEY`. `.env` is ignored by git. The
local `.env` is already wired to the role created by `setup_local.sh`.

`X_BEARER_TOKEN` is kept for reference only; the current ingestion path reads
`mock.json`. The legacy `TOKEN` name is ignored.

### 4. Verify

```bash
python check.py
```

Expected:

```
mock.json
  OK    24 tweets, 8 authors
database
  OK    PostgreSQL 18...
  OK    schema ready: 0 tweets, 0 users, 0 hashtags, 0 runs
```

### 5. Load the mock payload

```bash
python ingest/fetch_and_load.py
```

Options:

```bash
python ingest/fetch_and_load.py --query "generative ai" --file mock.json
python ingest/fetch_and_load.py --max 10      # load the first 10 posts
```

Each run creates one `topics` row, so re-running adds a new ingestion run
while the users/tweets/hashtags are upserted in place.

### 6. Run the web app

```bash
python web/app.py
# open http://127.0.0.1:5000
```

Or with the Flask CLI:

```bash
flask --app web.app run --debug
```

## Deploy to Vercel

The repository is Vercel-ready: `pyproject.toml` exports `web.app:app`, Python
3.12 is pinned in `.python-version`, and static assets live in `public/assets`
for the Vercel CDN.

Vercel cannot reach a PostgreSQL server on `localhost`, so the deployment needs
a hosted database (Neon, Vercel Postgres, Supabase, or any managed PostgreSQL).
Set `DATABASE_URL` and `FLASK_SECRET_KEY` in the Vercel project, apply
`db/create_schema.sql` to the hosted database, and load `mock.json` once from
your machine. The full walkthrough, including pooled connection strings and
verification, is in [DEPLOY.md](DEPLOY.md).

## Using the search page

All fields are optional and combinable:

| Field | Behaviour |
| --- | --- |
| Text contains | Case-insensitive substring match (`ILIKE`), with `%`/`_` treated literally |
| Hashtag | Exact case-insensitive tag match; the leading `#` is optional |
| Author | Case-insensitive substring on `users.username`; `@` is optional |
| From / To | Date range in UTC; a date-only “To” includes the whole day |
| Language | Exact match on the X language code (`en`, `ja`, …) |
| Ingestion run | Restricts to tweets linked through `tweet_topics` |

Results show 25 per page with `LIMIT`/`OFFSET`, a count, query time, and
human-readable chips for the active filters. Matching text is highlighted.
The landing page shows the eight most recent posts when no filters are set.

## Data model

```
users(user_id PK, username, display_name, created_at, followers_count,
      verified, fetched_at)
  └── tweets(tweet_id PK, user_id FK, text, created_at, lang,
             like_count, retweet_count, reply_count, quote_count, fetched_at)
        ├── hashtags(tweet_id FK, tag, PK(tweet_id, tag))
        └── tweet_topics(topic_id FK, tweet_id FK, PK(topic_id, tweet_id))

topics(topic_id PK, query, run_at)
```

Design notes:

- **Surrogate-free IDs** from X (`user_id`, `tweet_id`) are natural primary keys,
  so re-ingesting is idempotent.
- **Normalized hashtags** allow exact tag filters and counts without scanning
  post text. Hashtags come from `entities.hashtags`, not from parsing the text.
- **`tweet_topics`** is a junction table: one tweet can belong to several
  ingestion runs, and deleting a run does not delete the tweets.
- **`fetched_at`** records when the archive last refreshed a row; metrics are
  mutable and are updated on conflict.
- Indexes cover newest-first browsing (`created_at DESC`), author timelines,
  language filters, case-insensitive username/hashtag lookups, and substring
  text search (`pg_trgm` GIN).

## Ingestion behaviour

`ingest/fetch_and_load.py`:

1. Validates `data`, `includes.users`, and required tweet fields.
2. Inserts a `topics` row for the run.
3. Upserts each author with `ON CONFLICT (user_id) DO UPDATE`.
4. Upserts each tweet with `ON CONFLICT (tweet_id) DO UPDATE`; metrics and text
   refresh, `created_at` is preserved if the payload omits it.
5. Deduplicates hashtags case-insensitively and inserts them with
   `ON CONFLICT DO NOTHING`.
6. Links each tweet to the run with `ON CONFLICT DO NOTHING`.
7. Commits once per file, in a single transaction, and prints new/updated counts.

The loader uses `RETURNING (xmax = 0)` to distinguish inserts from updates.

## Safe SQL

`web/app.py` builds a fixed `WHERE` clause from a list of fragments; every
user value is a bound parameter. There is no string interpolation of input.
The only interpolated fragments are constant SQL and the chosen date operator
(`<=` or `<`), which is selected from two literals.

- `ILIKE` patterns escape `\`, `%`, and `_`, with `ESCAPE '\'`.
- `ORDER BY` is fixed; sort direction is not user-controlled.
- `LIMIT`/`OFFSET` are bound integers, and the page number is clamped.
- Jinja autoescaping plus an escaping `highlight` filter protect rendered post
  text; no user text is marked safe before escaping.

## Live X API (reference)

The mock payload mirrors:

```
GET https://api.x.com/2/tweets/search/recent
Authorization: Bearer $X_BEARER_TOKEN

query=generative ai lang:en -is:retweet
max_results=100
tweet.fields=created_at,public_metrics,lang,entities,author_id
user.fields=created_at,public_metrics,username,name,verified
expansions=author_id
```

A live collector would paginate with `meta.next_token` and handle:

- `429` — read `x-rate-limit-reset` (epoch seconds), sleep until it plus a small
  margin, then retry; cap the wait and retry count.
- `5xx`/timeouts — exponential backoff with jitter.
- `401` — invalid/expired bearer token.
- `402` — credits depleted (the reason this build uses `mock.json`).
- `403` — the app's access level does not include recent search.

Rate-limit handling is described in `report.md`. Because this build does not
make network calls, none of that retry loop is active code.

## Screenshots

**Archive home** — live counts, the search form, and the eight most recent posts.

![Sift home page](screenshots/screenshot.png)

**Searching for “AI”** — seven matches, the active filter chip, and highlighted text.

![Search for AI](screenshots/Screenshot_20261115_184246.png)

**Searching for “python”** — form state and the first rows.

![Search for python](screenshots/Screenshot_20261115_184354.png)

**Result table** — highlighted matches, hashtag chips, and metrics.

![Python result table](screenshots/Screenshot_20261115_184401.png)

**More results** — the table scrolled to the last rows.

![More python results](screenshots/Screenshot_20261115_184408.png)

<details>
<summary>Flask request log</summary>

The server log while the screenshots were taken:

![Flask request log](screenshots/Screenshot_20261115_184417.png)

</details>

## Troubleshooting

**`FATAL: Ident authentication failed`**
Run `sudo bash db/setup_local.sh`. It switches loopback TCP auth to
`scram-sha-256` and backs up `pg_hba.conf`.

**`role "tweets" does not exist` / `database "tweetsdb" does not exist`**
Run the setup script, or create them manually and update `DATABASE_URL`.

**`psycopg2.OperationalError: connection refused`**
`sudo systemctl start postgresql` and confirm `pg_isready`.

**`permission denied to create extension "pg_trgm"`**
Connect as the database owner (the setup script makes `tweets` the owner), or
ask an administrator to run `CREATE EXTENSION pg_trgm;`.

**The page says the archive is empty**
Load the payload: `python ingest/fetch_and_load.py`, then reload.

**Port 5000 is busy**
Set `FLASK_PORT=5001` in `.env`.
