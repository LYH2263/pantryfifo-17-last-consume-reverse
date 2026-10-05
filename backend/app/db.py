import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    # WAL + busy timeout so consume / expire-sweep / reverse serialize cleanly
    # under BEGIN IMMEDIATE instead of hitting "database is locked".
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=8000")
    return c
