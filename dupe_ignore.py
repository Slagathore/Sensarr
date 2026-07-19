# =============================================================================
# dupe_ignore.py
# =============================================================================
# "Not a duplicate" verdicts for the Maintenance tab's Find Duplicates tool
# (Task G item 2). Before this module, Keep/Delete were the only verbs a
# dismissed group had no memory: every rescan re-flagged the exact same
# false positive forever.
#
# Verdicts persist in their OWN small JSON file under app_paths' DURABLE data
# dir (never maintenance_cache.json, which Re-run deliberately wipes and
# which is expendable-cache territory, not a place for user decisions to
# live). Two verdict shapes, matching the two things a user can mark in the
# UI:
#   - folder pair  — "everything between these two folders is not a dupe"
#     (the whole-folder-pair fold the dupes view already collapses groups
#     into). Keyed by the sorted pair of parent-folder paths.
#   - file pair    — "this one specific group of files is not a dupe".
#     Keyed by the sorted tuple of every candidate path in the group (a
#     DuplicateGroup is a "pair" in the common 2-copy case, but the key
#     generalizes to N candidates so a 3-way tie can be dismissed as one
#     unit too).
#
# This module is UI-free and has no dependency on maintenance.py (the
# opposite import happens: maintenance.find_duplicates() filters through
# ignored_folder_pair_set()/ignored_file_pair_set() before it emits groups).
# =============================================================================

import datetime
import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_FILE_NAME = "dupe_ignore.json"
_VERSION = 1

_lock = threading.Lock()


def _data_dir() -> Path:
    """Indirection point so tests can redirect storage without fighting the
    frozen AppPaths dataclass — patch this function, not app_paths.PATHS."""
    import app_paths
    return app_paths.PATHS.data_dir


def _path() -> Path:
    return _data_dir() / _FILE_NAME


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def _empty() -> dict:
    return {"version": _VERSION, "folder_pairs": [], "file_pairs": []}


def _load() -> dict:
    p = _path()
    try:
        if not p.is_file():
            return _empty()
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            logger.warning("%s has an unrecognised shape/version -- "
                           "treating as empty (nothing already ignored is "
                           "lost from disk, just not honoured this run).", p)
            return _empty()
        raw.setdefault("folder_pairs", [])
        raw.setdefault("file_pairs", [])
        if not isinstance(raw["folder_pairs"], list):
            raw["folder_pairs"] = []
        if not isinstance(raw["file_pairs"], list):
            raw["file_pairs"] = []
        return raw
    except Exception:
        logger.warning("%s unreadable -- treating as empty this run.", p,
                       exc_info=True)
        return _empty()


def _save(data: dict) -> bool:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        logger.exception("Could not save %s -- verdict not persisted.", p)
        return False


def _norm(path: str) -> str:
    """Best-effort absolute-path normalisation. Falls back to the raw string
    on any OS error (a deleted file must still be un-ignorable)."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path))


def folder_pair_key(a: str, b: str) -> tuple[str, str]:
    norm = sorted((_norm(a), _norm(b)))
    return (norm[0], norm[1])


def file_pair_key(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(_norm(p) for p in paths))


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def ignore_folder_pair(a: str, b: str) -> None:
    key = list(folder_pair_key(a, b))
    with _lock:
        data = _load()
        if not any(sorted(e.get("paths") or []) == key
                  for e in data["folder_pairs"]):
            data["folder_pairs"].append({"paths": key, "ignored_at": _utcnow()})
            _save(data)


def ignore_file_pair(paths: Iterable[str]) -> None:
    key = list(file_pair_key(paths))
    if len(key) < 2:
        return
    with _lock:
        data = _load()
        if not any(sorted(e.get("paths") or []) == key
                  for e in data["file_pairs"]):
            data["file_pairs"].append({"paths": key, "ignored_at": _utcnow()})
            _save(data)


def unignore_folder_pair(a: str, b: str) -> bool:
    key = list(folder_pair_key(a, b))
    with _lock:
        data = _load()
        before = len(data["folder_pairs"])
        data["folder_pairs"] = [
            e for e in data["folder_pairs"] if sorted(e.get("paths") or []) != key
        ]
        if len(data["folder_pairs"]) == before:
            return False
        return _save(data)


def unignore_file_pair(paths: Iterable[str]) -> bool:
    key = list(file_pair_key(paths))
    with _lock:
        data = _load()
        before = len(data["file_pairs"])
        data["file_pairs"] = [
            e for e in data["file_pairs"] if sorted(e.get("paths") or []) != key
        ]
        if len(data["file_pairs"]) == before:
            return False
        return _save(data)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_ignored_folder_pairs() -> list[dict]:
    return list(_load()["folder_pairs"])


def list_ignored_file_pairs() -> list[dict]:
    return list(_load()["file_pairs"])


def ignored_folder_pair_set() -> set[tuple[str, str]]:
    """(sorted-path-a, sorted-path-b) tuples for every ignored folder pair —
    the shape find_duplicates() checks membership against on every walk."""
    out: set[tuple[str, str]] = set()
    for e in _load()["folder_pairs"]:
        paths = e.get("paths") or []
        if len(paths) == 2:
            out.add(tuple(sorted(paths)))
    return out


def ignored_file_pair_set() -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for e in _load()["file_pairs"]:
        paths = e.get("paths") or []
        if len(paths) >= 2:
            out.add(tuple(sorted(paths)))
    return out
