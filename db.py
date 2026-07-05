# =============================================================================
# db.py
# =============================================================================
# Shared SQLite connection helper for every module that touches the app
# database (queue_store, library_index, auth_store, maintenance).
#
# Why this exists: the app hits one SQLite file from several threads — the
# Tkinter main thread, Telegram bot executor threads, and ad-hoc worker
# threads. Each module used to call sqlite3.connect() directly with default
# settings (journal_mode=DELETE, 5s busy timeout), which makes
# "database is locked" errors likely under concurrent writes. Routing every
# connection through connect() gives all of them:
#   - WAL journal mode  → readers never block the writer and vice versa
#   - busy_timeout=15s  → writers wait politely instead of raising immediately
#   - synchronous=NORMAL → the recommended pairing with WAL
# =============================================================================

import sqlite3
from pathlib import Path

import config


def db_path() -> Path:
    """Absolute path to the shared application database."""
    path = Path(config.APP_DB_PATH)
    if path.is_absolute():
        return path
    return config.APP_DIR / path


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the app-standard pragmas applied.

    Connections are still short-lived and per-call (never shared across
    threads); this helper only standardises the settings.
    """
    conn = sqlite3.connect(str(path) if path is not None else str(db_path()), timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
