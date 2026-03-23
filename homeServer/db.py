from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_DIR = Path.home() / "weatherstation-home" / "weatherstation"
DB_PATH = BASE_DIR / "weatherstation.db"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn
