# =============================================================================
# media_quality.py  —  persistent quality labels by identity (Task F)
# =============================================================================
# CAM knowledge lives in the DATABASE, not filenames (Design stance 10). Every
# verified move writes/updates one media_quality row keyed by the QUALIFIED
# identity — movies by (media_type, identity_source, external_id), episodes by
# the internal (show_id, season, episode) coordinates — so the label survives
# the sanitize/rename cycle: renames only update file_path (through the ONE
# central helper update_file_path); the label rides the identity.
#
# A write with no resolvable identity (the manual replacement path before it
# learns a MediaIdentity) is keyed by the file path and marked
# source='manual-unresolved' — it never claims an identity-keyed update.
#
# media_quality_history is a real append table (old_label, new_label, cause,
# at) — every label change is recorded, nothing is overwritten silently.
# =============================================================================

import threading
from dataclasses import dataclass
from pathlib import Path

import db

_MQ_LOCK = threading.Lock()

SOURCE_PARSED = "parsed"
SOURCE_MANUAL = "manual"
SOURCE_MANUAL_UNRESOLVED = "manual-unresolved"

# NOTE: table/column identifiers below are static literals — never interpolate
# user-supplied values into these CREATE/ALTER statements.
_SCHEMA_QUALITY = """
CREATE TABLE IF NOT EXISTS media_quality (
    identity_key    TEXT PRIMARY KEY,
    media_type      TEXT,
    identity_source TEXT,
    external_id     TEXT,
    show_id         INTEGER,
    season          INTEGER,
    episode         INTEGER,
    quality_label   TEXT,
    source          TEXT NOT NULL DEFAULT 'parsed',
    file_path       TEXT,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_SCHEMA_HISTORY = """
CREATE TABLE IF NOT EXISTS media_quality_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    old_label    TEXT,
    new_label    TEXT,
    cause        TEXT,
    at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@dataclass(frozen=True)
class MediaQualityRow:
    identity_key: str
    media_type: str | None
    identity_source: str | None
    external_id: str | None
    show_id: int | None
    season: int | None
    episode: int | None
    quality_label: str | None
    source: str
    file_path: str | None
    updated_at: str


@dataclass(frozen=True)
class QualityHistoryRow:
    history_id: int
    identity_key: str
    old_label: str | None
    new_label: str | None
    cause: str | None
    at: str


def initialize_media_quality_db() -> None:
    with _MQ_LOCK, db.connect() as conn:
        conn.execute(_SCHEMA_QUALITY)
        conn.execute(_SCHEMA_HISTORY)
        conn.commit()


# ---------------------------------------------------------------------------
# Identity keys
# ---------------------------------------------------------------------------

def movie_identity_key(identity_source: str, external_id: str) -> str:
    return f"movie:{identity_source}:{external_id}"


def episode_identity_key(show_id: int, season: int, episode: int) -> str:
    return f"ep:{int(show_id)}:s{int(season)}:e{int(episode)}"


def path_identity_key(file_path: str) -> str:
    """Fallback key for writes with NO resolvable identity (manual-unresolved).
    Normalized so the same file always maps to the same key on this OS."""
    return f"path:{str(Path(file_path)).casefold()}"


_COLS = ("identity_key, media_type, identity_source, external_id, show_id, "
         "season, episode, quality_label, source, file_path, updated_at")


def _row(r) -> MediaQualityRow:
    return MediaQualityRow(
        identity_key=r[0], media_type=r[1], identity_source=r[2],
        external_id=r[3], show_id=r[4], season=r[5], episode=r[6],
        quality_label=r[7], source=r[8], file_path=r[9], updated_at=r[10])


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def record_quality(identity_key: str, *, quality_label: str | None,
                   file_path: str | None, media_type: str | None = None,
                   identity_source: str | None = None,
                   external_id: str | None = None, show_id: int | None = None,
                   season: int | None = None, episode: int | None = None,
                   source: str = SOURCE_PARSED,
                   cause: str = "verified_move") -> None:
    """Upsert the quality row for one identity. A LABEL CHANGE (including the
    first label) appends a media_quality_history row with its cause; a
    same-label update only refreshes file_path/updated_at."""
    initialize_media_quality_db()
    with _MQ_LOCK, db.connect() as conn:
        prior = conn.execute(
            "SELECT quality_label FROM media_quality WHERE identity_key = ?",
            (identity_key,)).fetchone()
        old_label = prior[0] if prior is not None else None
        conn.execute(
            """
            INSERT INTO media_quality
                (identity_key, media_type, identity_source, external_id,
                 show_id, season, episode, quality_label, source, file_path,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(identity_key) DO UPDATE SET
                media_type = excluded.media_type,
                identity_source = excluded.identity_source,
                external_id = excluded.external_id,
                show_id = excluded.show_id,
                season = excluded.season,
                episode = excluded.episode,
                quality_label = excluded.quality_label,
                source = excluded.source,
                file_path = excluded.file_path,
                updated_at = CURRENT_TIMESTAMP
            """,
            (identity_key, media_type, identity_source, external_id, show_id,
             season, episode, quality_label, source, file_path))
        if old_label != quality_label:
            conn.execute(
                "INSERT INTO media_quality_history"
                " (identity_key, old_label, new_label, cause)"
                " VALUES (?, ?, ?, ?)",
                (identity_key, old_label, quality_label, cause))
        conn.commit()


def update_file_path(old_path: str, new_path: str) -> int:
    """THE central rename hook (Task F item 2): every code path that renames or
    moves a LIBRARY file routes its media_quality file_path update through
    here, so the label keeps following the file. Path-keyed (unresolved) rows
    get their identity_key re-derived from the new path as well. Returns how
    many rows were updated."""
    initialize_media_quality_db()
    updated = 0
    with _MQ_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM media_quality WHERE file_path = ?",
            (old_path,)).fetchall()
        for r in rows:
            row = _row(r)
            new_key = row.identity_key
            if row.identity_key.startswith("path:"):
                new_key = path_identity_key(new_path)
                # A row already sitting at the new key would be shadowed —
                # replace it (same file, newer knowledge).
                if new_key != row.identity_key:
                    conn.execute(
                        "DELETE FROM media_quality WHERE identity_key = ?",
                        (new_key,))
            conn.execute(
                "UPDATE media_quality SET file_path = ?, identity_key = ?,"
                " updated_at = CURRENT_TIMESTAMP WHERE identity_key = ?",
                (new_path, new_key, row.identity_key))
            updated += 1
        conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_quality(identity_key: str) -> MediaQualityRow | None:
    initialize_media_quality_db()
    with _MQ_LOCK, db.connect() as conn:
        r = conn.execute(
            f"SELECT {_COLS} FROM media_quality WHERE identity_key = ?",
            (identity_key,)).fetchone()
    return _row(r) if r else None


def label_for_path(file_path: str) -> str | None:
    initialize_media_quality_db()
    with _MQ_LOCK, db.connect() as conn:
        r = conn.execute(
            "SELECT quality_label FROM media_quality WHERE file_path = ?",
            (file_path,)).fetchone()
    return r[0] if r else None


def labels_by_path() -> dict[str, str]:
    """file_path -> quality_label for every labeled row (one query; the
    low-quality scan reads this FIRST, name regex second)."""
    initialize_media_quality_db()
    with _MQ_LOCK, db.connect() as conn:
        rows = conn.execute(
            "SELECT file_path, quality_label FROM media_quality"
            " WHERE file_path IS NOT NULL AND quality_label IS NOT NULL"
        ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def list_history(identity_key: str) -> list[QualityHistoryRow]:
    initialize_media_quality_db()
    with _MQ_LOCK, db.connect() as conn:
        rows = conn.execute(
            "SELECT id, identity_key, old_label, new_label, cause, at"
            " FROM media_quality_history WHERE identity_key = ? ORDER BY id",
            (identity_key,)).fetchall()
    return [QualityHistoryRow(*r) for r in rows]
