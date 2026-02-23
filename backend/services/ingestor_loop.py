import os, sys, time, logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.ingestor.main import run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ingestor] %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("ingestor_loop")

# 5 min between cycles — run_once() internally skips KOLs not due yet
CYCLE_INTERVAL = 300


def run():
    log.info("🐦 Ingestor loop starting…")
    while True:
        try:
            run_once(max_days=3)
        except Exception as e:
            log.error(f"Ingestor cycle error: {e}", exc_info=True)
        log.info(f"💤 Sleeping {CYCLE_INTERVAL}s…")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()