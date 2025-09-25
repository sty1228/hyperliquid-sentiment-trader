import os
from dotenv import load_dotenv

_loaded = False
def load_env():
    global _loaded
    if not _loaded:
        load_dotenv() 
        _loaded = True

def env(key: str, default: str | None = None) -> str | None:
    load_env()
    return os.getenv(key, default)

def get_db_path(default: str = "data/crypto_tracker.db") -> str:
    return env("CRYPTO_DB_PATH", default)
