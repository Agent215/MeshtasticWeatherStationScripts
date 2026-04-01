from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path.home() / "weatherstation-home" / "weatherstation"
DEFAULT_DB_PATH = BASE_DIR / "weatherstation.db"


def get_db_path() -> Path:
    override = os.environ.get("WEATHERSTATION_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_DB_PATH


def get_conn(*, timeout_sec: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)};")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn
