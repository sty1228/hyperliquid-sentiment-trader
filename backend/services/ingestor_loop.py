import os, sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.ingestor.main import run_daemon

if __name__ == "__main__":
    # run_daemon() handles everything:
    # - adaptive polling (1h–24h per KOL)
    # - since_id incremental tweet fetch
    # - profile refresh every 7 days
    # - graceful SIGTERM shutdown
    # - exponential backoff + circuit breaker
    # - force_first_cycle=True: first cycle polls all KOLs regardless of schedule
    run_daemon(max_days=7, force_first_cycle=True)