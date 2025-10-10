# Running the API (backend)

This project exposes a FastAPI application that serves sentiment/tracking data from an SQLite database.

Files of interest
- `backend/api.py` - main FastAPI application with multiple `/api/*` read endpoints.
- `run_api.py` - small wrapper to run uvicorn with `backend.api:app`.
- `backend/serve.py` - a small app to serve the static dashboard (`web/index.html`).
- `backend/services/enhanced_price_database.py` - DB helper that creates tables and offers query functions.

Prerequisites
- Python 3.8+ and the project dependencies installed. If you don't have an environment yet, create one and install requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Running the API

Start the API server (default 0.0.0.0:8080):

```bash
python3 run_api.py
# or
uvicorn backend.api:app --host 0.0.0.0 --port 8080 --reload
```

Configuration
- `CRYPTO_DB_PATH` environment variable can be set to point to the SQLite DB file. By default the app uses `data/crypto_tracker.db` via `backend/config.py`.

Listing routes (without starting the server)

You can run a quick script to load the app and print mounted routes (useful to validate imports):

```bash
python3 - <<'PY'
import importlib
mod = importlib.import_module('backend.api')
app = getattr(mod, 'app')
print('\n'.join(sorted(r.path for r in app.router.routes)))
PY
```

Example requests

- Health:

```bash
curl http://127.0.0.1:8080/api/health
```

- Leaderboard:

```bash
curl 'http://127.0.0.1:8080/api/leaderboard?window_h=168&limit=20'
```

Notes and next steps
- The API is already implemented in `backend/api.py` for many common read endpoints. To make it public-safe you may want to add API key authentication or rate limiting.
- Ensure the database file exists and is populated (tables referenced: `tweets`, `performance_horizons`, `leaderboard_cache`, `user_daily_stats`, etc.). Some tables are created by `backend/services/enhanced_price_database.py` but others may be created by the ingestion pipeline.
