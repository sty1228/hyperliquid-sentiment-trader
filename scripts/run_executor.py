
from __future__ import annotations

import os
import sys
import time
import logging

_THIS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from execution.schema import ensure_schema
from execution.executor import Executor


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    ensure_schema()
    exe = Executor(daily_limit=None) 
    logging.info("Executor started (poll interval = 2s)")

    try:
        while True:
            try:
                exe.process_created_plans()
                exe.sl_daemon_tick()
            except Exception as e:
                logging.exception("[executor] error")
            time.sleep(2) 
    except KeyboardInterrupt:
        logging.info("Executor stopped by user")


if __name__ == "__main__":
    main()
