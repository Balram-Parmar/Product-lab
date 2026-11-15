# Deploy to Vercel

The app is Vercel-ready:

- `pyproject.toml` points Vercel at the Flask instance:
  ```toml
  [tool.vercel]
  entrypoint = "web.app:app"
  ```
- `.python-version` pins Python 3.12.
- Static files live in `public/assets`, which Vercel serves from its CDN at
  `/assets/*`; Flask uses the same folder for local development.
- The Flask app opens a PostgreSQL connection per request and closes it on
  teardown, which is serverless-friendly.

## The one thing Vercel cannot do

Vercel Functions cannot reach a PostgreSQL server on `localhost`. The deployed
app needs a hosted database. Use Neon, Vercel Postgres (Neon), Supabase, or any
managed PostgreSQL 13+ instance.

The ingestion script is also not run by Vercel. Load the data from your machine
(or CI) into the hosted database once, then deploy the web app.

## 1. Create the hosted database

For Neon:

1. Create a project at <https://neon.tech>.
2. Copy the **direct** connection string (not the `-pooler` one) for schema setup.
3. Apply the schema from this repository:

   ```bash
   psql "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require" \
     -f db/create_schema.sql
   ```

   Neon supports the `pg_trgm` extension used by the text-search index. If your
   provider blocks it, remove the `CREATE EXTENSION` and trigram index lines from
   `db/create_schema.sql`; search still works, just without the index.

4. Load the mock data into the hosted database:

   ```bash
   DATABASE_URL="postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require" \
     .venv/bin/python ingest/fetch_and_load.py
   ```

5. Copy the **pooled** connection string (the host usually contains `-pooler`).
   Use that one for Vercel so serverless concurrency does not exhaust Postgres
   connections.

## 2. Set Vercel environment variables

In the Vercel project: **Settings → Environment Variables** (Production and
Preview), add:

| Name | Value |
| --- | --- |
| `DATABASE_URL` | pooled hosted Postgres URL, including `sslmode=require` |
| `FLASK_SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `PAGE_SIZE` | `25` (optional) |
| `FLASK_DEBUG` | `0` |

Do not commit `.env`; it is ignored by git.

## 3. Deploy

Push the repository to GitHub and let Vercel build it, or run:

```bash
vercel --prod
```

Vercel installs `requirements.txt`, imports `web.app:app`, and serves
`public/assets` from the CDN.

## 4. Verify

Open the deployment URL. The header should show counts such as:

```
posts 24   authors 8   runs 1
```

If it says **database offline**:

- Check that `DATABASE_URL` is set for the environment you are viewing
  (Production vs Preview).
- Confirm the hosted database allows connections from Vercel; `sslmode=require`
  is usually required.
- Run `psql "$DATABASE_URL" -c "select count(*) from tweets;"` locally.
- Check Vercel Runtime Logs for the underlying `psycopg2` error.

## Notes and limitations

- **Cold starts:** the first request after idle may take a moment while the
  function starts and connects to Postgres.
- **Ingestion:** Vercel does not run `ingest/fetch_and_load.py`; data is loaded
  ahead of time. A future scheduled collector would need Vercel Cron + a
  protected route, or an external worker.
- **Connection pooling:** use the provider's pooled URL for `DATABASE_URL`.
- **Static assets:** keep runtime CSS/fonts in `public/assets`. Do not add them
  back under `web/static`; Vercel only serves `public/**` from the CDN.
- **Bundle size:** `.vercelignore` excludes screenshots, the virtualenv, and
  caches from the function bundle.
