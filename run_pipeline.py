

import os
import logging
import sqlite3

from backend.config import load_env, env
from backend.ingestor import main as ingestor
from backend.services.enhanced_price_database import EnhancedPriceDatabase
from backend.services.bybit_price_tracker import PriceTracker

from recompute_metrics_24h import recompute_user_summary_24h, export_user_summary_csv

load_env()

LOG_DIR = env("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "pipeline.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8"),
    ],
    force=True,
)
logging.info(f"Pipeline logging to {LOG_PATH}")


SKIP_STEP_1 = env("SKIP_STEP_1", "0") == "1"   # skip scraping
SKIP_STEP_2 = env("SKIP_STEP_2", "0") == "1"   # skip OpenAI processing

def _get_user_list():
    users_csv = env("SCRAPE_USERS", "")
    users = [u.strip() for u in users_csv.split(",") if u.strip()]
    if users:
        return users
    return getattr(ingestor, "DEFAULT_USERS", [])

def _get_sqlite_conn_fallback(db_obj: EnhancedPriceDatabase) -> sqlite3.Connection:

    conn = getattr(db_obj, "conn", None) or getattr(db_obj, "_conn", None)
    if conn:
        return conn
    db_path = env("DB_PATH", "data/crypto_tracker.db")
    logging.warning(f"EnhancedPriceDatabase 未暴露连接，改用 env DB_PATH: {db_path}")
    return sqlite3.connect(db_path)


def run_pipeline():
    print("run_pipeline started (use SKIP_STEP_1/2 to skip steps 1 & 2)")
    logging.info("Starting pipeline run")

    if not SKIP_STEP_1:
        logging.info("Step 1: Scraping tweets (CSV fallback if fail)...")
        try:
            users = _get_user_list()
            logging.info(f"Users to scrape: {len(users)} (override with SCRAPE_USERS in .env)")

            driver = ingestor.create_driver()
            try:
                if not ingestor.twitter_login(driver, ingestor.TWITTER_USER, ingestor.TWITTER_PASS):
                    raise RuntimeError("Twitter login failed")

                ingestor.scrape_multiple_users(
                    users,
                    driver=driver,
                    output_csv=ingestor.INPUT_CSV_PATH,
                    max_days=int(env("SCRAPE_MAX_DAYS", "3")),
                    max_scrolls=int(env("SCRAPE_MAX_SCROLLS", "20")),
                )
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        except Exception as e:
            logging.error(f"Scraping failed: {e}", exc_info=True)
            logging.info("Proceeding with CSV fallback (if data/twitter_scraping_results.csv exists).")
    else:
        logging.info("⏭️ Skip Step 1 (scrape)")

    # -------- Step 2: process tweets with OpenAI --------
    if not SKIP_STEP_2:
        logging.info("Step 2: Processing tweets with OpenAI (ticker + sentiment)...")
        try:
            processed_df = ingestor.process_tweets_complete()
            if processed_df is not None:
                logging.info(f"Processed {len(processed_df)} rows")
            else:
                logging.info("No processed dataframe returned (check input CSV availability).")
        except Exception as e:
            logging.error(f"Processing failed: {e}", exc_info=True)
    else:
        logging.info("⏭️ Skip Step 2 (process tweets)")

    # -------- Step 3: DB + price updates --------
    logging.info("Step 3: Loading into DB and updating prices...")
    try:
        tracker = PriceTracker()
        tracker.process_new_tweets()         
        tracker.update_all_prices()         
        tracker.print_performance_summary(hours=24)

        db = EnhancedPriceDatabase()
        conn = _get_sqlite_conn_fallback(db)
        try:
            recompute_user_summary_24h(conn)
            logging.info("Recomputed 24h UserSummary (user_window_stats updated)")

            export_user_summary_csv(
                conn,
                out_dir=env("DATA_DIR", "data"),
                filename="user_summary_24h.csv",
                also_timestamped=True,
            )
            logging.info("Exported CSV to data/user_summary_24h*.csv")
        finally:
            pass

    except Exception as e:
        logging.error(f"Price update / recompute failed: {e}", exc_info=True)

    try:
        db2 = EnhancedPriceDatabase()
        stats = db2.get_database_stats()
        logging.info(f"DB stats: {stats}")
    except Exception as e:
        logging.error(f"Failed to read DB stats: {e}", exc_info=True)

    logging.info("Pipeline run complete")

if __name__ == "__main__":
    run_pipeline()
