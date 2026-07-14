# =============================================================================
# tests/test_task_s.py
# =============================================================================
# Task S — security hardening regression guards:
#   item 1: pickle is gone; JSON caches round-trip the real cached shapes and
#           REJECT malformed / pickle-era / hostile files as a cache miss,
#           never executing anything.
#   item 2: the npm-install call resolves the binary and uses shell=False.
#   item 3: dependency pins are exact (no >= floats, no npm caret ranges).
# =============================================================================

import json
import pickle
import re
from dataclasses import dataclass
from pathlib import Path

import json_cache
from maintenance import (DuplicateGroup, JunkFile, SanitizePair,
                         ShowInventory, UnindexedFile)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# JSON round trip of the real cached shapes
# ---------------------------------------------------------------------------

def test_json_roundtrip_preserves_cached_shapes(tmp_path):
    """The exact structures the maintenance cache stores: dataclasses,
    tuples (payloads are isinstance-checked as tuples after reload),
    int-keyed dicts (ShowInventory.seasons), and nesting."""
    payload = {
        "clean_junk": {
            "results": [JunkFile(path="D:/x/sample.mkv", kind="file",
                                 reason="sample video", size_bytes=123)],
            "typed_rows": [
                ("movie", ("", "sample.mkv", "sample video", "123 B"),
                 ("file", "D:/x/sample.mkv")),
            ],
            "cols": ("File / Folder", "Why", "Size"),
            "check_label": "Delete?",
            "default_checked": True,
            "status": "1 junk item(s)",
            "at": "Jul 14 08:00",
        },
        "find_duplicates": {
            "results": [DuplicateGroup(
                normalized_title="tremors", candidates=["a.mkv", "b.mkv"],
                candidate_sizes=[10, 20], total_size_bytes=30)],
            "typed_rows": [
                ("movie", ("", "tremors", "2 copies", "30 B"), None, 0),
                ("movie", ("", "a.mkv", "a.mkv", "10 B"), "a.mkv", 1),
                ("movie", ("", "side", "d", "x"), ("paths", ["a.mkv"]), 1),
            ],
        },
        "sanitize": {
            "results": [SanitizePair(original="a b.mkv",
                                     sanitized="A B.mkv", size_bytes=5)],
            "typed_rows": [("tv", ("", "a", "b", "5 B"), 0)],
        },
        "library_inventory": {
            "results": [ShowInventory(title="Show", media_type="tv",
                                      seasons={1: 10, 2: 8},
                                      total_episodes=18,
                                      total_size_bytes=999)],
        },
        "unindexed": {
            "results": [UnindexedFile(path="p", name="n", size_bytes=1)],
        },
    }
    path = tmp_path / "cache.json"
    assert json_cache.save_json_cache(path, payload, version=3)
    loaded = json_cache.load_json_cache(
        path, version=3,
        dataclass_types=(DuplicateGroup, JunkFile, SanitizePair,
                         ShowInventory, UnindexedFile))
    assert loaded == payload
    # Tuple payloads survive as tuples — apply flows depend on it.
    junk_payload = loaded["clean_junk"]["typed_rows"][0][2]
    assert isinstance(junk_payload, tuple) and junk_payload[0] == "file"
    dupe_side = loaded["find_duplicates"]["typed_rows"][2][2]
    assert isinstance(dupe_side, tuple) and dupe_side[0] == "paths"
    # Int keys came back as ints, not strings.
    seasons = loaded["library_inventory"]["results"][0].seasons
    assert seasons == {1: 10, 2: 8} and all(
        isinstance(k, int) for k in seasons)


def test_garbage_bytes_are_a_cache_miss(tmp_path):
    path = tmp_path / "cache.json"
    path.write_bytes(b"\x80\x05\x00garbage\xff not json at all")
    assert json_cache.load_json_cache(path, version=3) is None


_EXECUTED_SENTINELS: list[str] = []


def _record_execution(tag: str) -> str:
    _EXECUTED_SENTINELS.append(tag)
    return tag


@dataclass
class _Boobytrap:
    """Pickle payload whose deserialization would leave evidence."""
    tag: str

    def __reduce__(self):
        return (_record_execution, ("pickle deserialized!",))


def test_pickle_era_file_is_rejected_not_executed(tmp_path):
    """A planted pickle file at the cache path (the exact attack S1 closes)
    must load as a miss without running anything."""
    path = tmp_path / "cache.json"
    path.write_bytes(pickle.dumps({"version": 3,
                                   "payload": _Boobytrap("boom")}))
    _EXECUTED_SENTINELS.clear()
    assert json_cache.load_json_cache(path, version=3) is None
    assert _EXECUTED_SENTINELS == [], "pickle payload was deserialized!"


def test_version_mismatch_is_a_cache_miss(tmp_path):
    path = tmp_path / "cache.json"
    assert json_cache.save_json_cache(path, {"a": 1}, version=2)
    assert json_cache.load_json_cache(path, version=3) is None
    assert json_cache.load_json_cache(path, version=2) == {"a": 1}


def test_unknown_dataclass_tag_rejects_the_cache(tmp_path):
    """A hostile file can name any class it wants — only the caller's
    allowlist is ever constructed."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "version": 3,
        "payload": {"__dc__": "SystemExit", "fields": {}},
    }), encoding="utf-8")
    assert json_cache.load_json_cache(path, version=3) is None


def test_successful_save_deletes_the_legacy_pickle(tmp_path):
    legacy = tmp_path / "cache.pkl"
    legacy.write_bytes(pickle.dumps({"old": True}))
    path = tmp_path / "cache.json"
    assert json_cache.save_json_cache(path, {"new": True}, version=1,
                                      legacy_paths=[legacy])
    assert not legacy.exists(), "stale .pkl must be removed, not orphaned"
    assert json_cache.load_json_cache(path, version=1) == {"new": True}


def test_unsupported_payload_is_not_written(tmp_path):
    path = tmp_path / "cache.json"
    assert not json_cache.save_json_cache(path, {"bad": object()}, version=1)
    assert not path.exists()


# ---------------------------------------------------------------------------
# Source-level guards: pickle stays dead, npm stays shell-free
# ---------------------------------------------------------------------------

def _source(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def test_no_pickle_usage_remains_in_the_cache_owners():
    pattern = re.compile(r"\bimport pickle\b|pickle\.(loads?|dumps?)\b")
    for name in ("desktop_app.py", "watchlist_tab.py", "json_cache.py"):
        assert not pattern.search(_source(name)), (
            f"{name} still touches pickle — S1 regression")


def test_npm_install_runs_without_a_shell():
    src = _source("desktop_app.py")
    assert "shell=True" not in src
    assert 'shutil.which("npm")' in src


# ---------------------------------------------------------------------------
# Dependency pins
# ---------------------------------------------------------------------------

def test_requirements_are_exact_pins():
    for line in _source("requirements.txt").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert ">=" not in line and "~=" not in line, (
            f"unpinned requirement: {line}")
        assert "==" in line, f"requirement without an exact pin: {line}"


def test_webtorrent_is_pinned_exact_and_matches_the_lockfile():
    manifest = json.loads(_source("torrent_runner/package.json"))
    spec = manifest["dependencies"]["webtorrent"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", spec), (
        f"webtorrent must be an exact version, got {spec!r}")
    lock = json.loads(_source("torrent_runner/package-lock.json"))
    assert lock["packages"][""]["dependencies"]["webtorrent"] == spec
    locked = lock["packages"]["node_modules/webtorrent"]["version"]
    assert locked == spec, "package.json pin and lockfile resolution differ"
