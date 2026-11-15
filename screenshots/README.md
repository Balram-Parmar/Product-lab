# Screenshots

Two screenshots are required by the assignment. Capture them after the app is
running with data loaded.

1. **Ingestion** — in a terminal at the project root:

   ```bash
   source .venv/bin/activate
   python ingest/fetch_and_load.py
   ```

   Capture the full output, including the topic id, new/updated counts, and
   hashtag/link totals. Save as `screenshots/ingest.png`.

2. **Search** — start `python web/app.py`, open <http://127.0.0.1:5000>, and
   search for something that returns several rows, for example:

   - Text contains: `postgres`
   - Hashtag: `RAG`
   - Language: `English`

   Capture the filter chips, result count, and the table. Save as
   `screenshots/search.png`.

Optional third shot: `python check.py` showing the schema ready and row counts.
