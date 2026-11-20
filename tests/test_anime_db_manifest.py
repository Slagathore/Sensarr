"""Weekly manifest distribution (Task I): manifest gating, sha256 verify,
schema-mismatch fallback, untouched-on-failure, atomic swap, and the local
publish -> client refresh round trip. Everything here is headless — no real
network (conftest's socket guard blocks it); anime_db._download is monkeypatched
to serve bytes from an in-memory url->bytes map instead.
"""
import gzip
import hashlib
import json
import sqlite3
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import anime_db
import config
import publish_anime_db

SAMPLE_MANAMI = {"data": [
    {
        "sources": ["https://anidb.net/anime/17478",
                    "https://myanimelist.net/anime/52211"],
        "title": "Mashle", "type": "TV", "episodes": 12, "status": "FINISHED",
        "animeSeason": {"season": "SPRING", "year": 2023},
        "synonyms": ["Mashle: Magic and Muscles"],
        "tags": ["comedy"], "score": {"arithmeticMean": 7.5},
    },
]}
SAMPLE_FRIBB = [
    {"anidb_id": 17478, "anilist_id": 151801, "mal_id": 52211,
     "tvdb_id": 421737, "themoviedb_id": {"tv": 204832},
     "imdb_id": ["tt21209804"], "season": {"tvdb": 1}},
]
SAMPLE_XML = ET.fromstring(
    '<anime-list><anime anidbid="17478" tvdbid="421737"'
    ' defaulttvdbseason="1"/></anime-list>')


@pytest.fixture()
def db_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "anime_meta.sqlite"
    monkeypatch.setattr(anime_db, "_db_path", lambda: path)
    monkeypatch.setattr(config, "ANIME_DB_MANIFEST_ENABLED", True)
    return path


def _gz_of(manami: dict, tmp_path: Path, *, fribb: list | None = None,
          xml_root=None, name: str = "artifact.sqlite") -> tuple[bytes, str]:
    """Build an artifact — manami-only by default, or with bundled mappings
    when fribb/xml_root are given — and return (gz_bytes, sha256_hex)."""
    dest = tmp_path / name
    anime_db._build_database(manami, fribb or [], xml_root, dest)
    raw = dest.read_bytes()
    gz = gzip.compress(raw, compresslevel=9, mtime=0)
    return gz, hashlib.sha256(gz).hexdigest()


def _manifest_for(gz: bytes, sha: str, *, built: str = "2026-07-19T00:00:00Z",
                  schema: int | None = None) -> dict:
    return {
        "schema_version": anime_db._SCHEMA_VERSION if schema is None else schema,
        "built": built,
        "sha256": sha,
        "url": "https://example.invalid/anime-db.sqlite.gz",
        "bytes": len(gz),
    }


def _mock_downloads(monkeypatch, url_map: dict[str, bytes]) -> None:
    def fake_download(url, timeout=180):
        if url not in url_map:
            raise RuntimeError(f"no mock registered for {url}")
        payload = url_map[url]
        if isinstance(payload, Exception):
            raise payload
        return payload
    monkeypatch.setattr(anime_db, "_download", fake_download)


@pytest.mark.parametrize(("primary_result", "expected_requests"), [
    (504, 2),
    (404, 1),
    (b'{"data":[]}', 1),
])
def test_manami_falls_back_to_dated_release(monkeypatch, primary_result, expected_requests):
    primary, fallback = anime_db._MANAMI_URLS
    calls = []
    waits = []

    def download(url, timeout):
        calls.append(url)
        if url == fallback:
            return json.dumps(SAMPLE_MANAMI).encode()
        if isinstance(primary_result, int):
            raise urllib.error.HTTPError(url, primary_result, "error", None, None)
        return primary_result

    monkeypatch.setattr(anime_db, "_download", download)
    monkeypatch.setattr(anime_db.time, "sleep", waits.append)

    assert anime_db.fetch_manami() == SAMPLE_MANAMI
    assert calls == [primary] * expected_requests + [fallback]
    assert waits == [2] * (expected_requests - 1)


# ---------------------------------------------------------------------------
# Manifest compare/gating logic
# ---------------------------------------------------------------------------

def test_manifest_newer_when_no_local_db(db_path, tmp_path, monkeypatch):
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    manifest = _manifest_for(gz, sha)
    state, schema = anime_db._manifest_state(manifest)
    assert state == anime_db._MANIFEST_NEWER and schema == anime_db._SCHEMA_VERSION


def test_manifest_not_newer_when_local_already_current(db_path, tmp_path):
    # Local DB built "now" — its built_at will be >= a manifest claiming an
    # older timestamp.
    anime_db.build_manami_artifact(SAMPLE_MANAMI, db_path)
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    manifest = _manifest_for(gz, sha, built="2020-01-01T00:00:00Z")
    state, schema = anime_db._manifest_state(manifest)
    assert state == anime_db._MANIFEST_UP_TO_DATE and schema == anime_db._SCHEMA_VERSION


def test_manifest_schema_mismatch_refused_either_direction(db_path, tmp_path):
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    older = _manifest_for(gz, sha, schema=anime_db._SCHEMA_VERSION - 1)
    newer_schema = _manifest_for(gz, sha, schema=anime_db._SCHEMA_VERSION + 1)
    for manifest in (older, newer_schema):
        state, schema = anime_db._manifest_state(manifest)
        assert state == anime_db._MANIFEST_SCHEMA_MISMATCH
        assert schema == manifest["schema_version"]


def test_check_for_manifest_update_respects_disable_switch(db_path, tmp_path, monkeypatch):
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    manifest = _manifest_for(gz, sha)
    _mock_downloads(monkeypatch, {anime_db.MANIFEST_URL: json.dumps(manifest).encode()})
    assert anime_db.check_for_manifest_update() is True
    monkeypatch.setattr(config, "ANIME_DB_MANIFEST_ENABLED", False)
    assert anime_db.check_for_manifest_update() is False


def test_check_for_manifest_update_false_on_unreachable_manifest(db_path, monkeypatch):
    _mock_downloads(monkeypatch, {})  # every URL raises "no mock registered"
    assert anime_db.check_for_manifest_update() is False


# ---------------------------------------------------------------------------
# sha256 verification
# ---------------------------------------------------------------------------

def test_corrupt_gz_rejected_and_db_untouched(db_path, tmp_path, monkeypatch):
    # Seed an "existing" local DB with known bytes so we can prove it's
    # byte-identical afterwards.
    anime_db.build_manami_artifact(SAMPLE_MANAMI, db_path)
    before = db_path.read_bytes()

    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    corrupt_gz = gz[:-5] + b"\x00\x00\x00\x00\x00"  # tail-truncated + mangled
    manifest = _manifest_for(corrupt_gz, sha, built="2099-01-01T00:00:00Z")
    _mock_downloads(monkeypatch, {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: corrupt_gz,
    })

    result = anime_db._refresh_from_manifest()
    assert result is None
    assert db_path.read_bytes() == before, "sha256 mismatch must leave the DB untouched"


def test_valid_gz_with_lying_manifest_sha256_is_rejected(db_path, tmp_path, monkeypatch):
    """Isolates the sha256 gate itself: the gz payload is perfectly valid
    (gzip's own CRC passes, the DB inside is sound), only the manifest's
    sha256 field is wrong. Nothing but the explicit hash comparison can
    reject this, so the test fails if that check is ever removed."""
    anime_db.build_manami_artifact(SAMPLE_MANAMI, db_path)
    before = db_path.read_bytes()

    gz, _ = _gz_of(SAMPLE_MANAMI, tmp_path)
    wrong_sha = hashlib.sha256(b"something else entirely").hexdigest()
    manifest = _manifest_for(gz, wrong_sha, built="2099-01-01T00:00:00Z")
    _mock_downloads(monkeypatch, {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: gz,
    })

    result = anime_db._refresh_from_manifest()
    assert result is None
    assert db_path.read_bytes() == before, "lying sha256 must leave the DB untouched"


def test_sha256_ok_but_not_actually_sqlite_is_rejected(db_path, tmp_path, monkeypatch):
    """Defends the post-decompress schema sanity check: a manifest whose
    sha256 matches garbage bytes (e.g. a manifest/gz mix-up) must still be
    refused rather than swapped in as a broken 'database'."""
    anime_db.build_manami_artifact(SAMPLE_MANAMI, db_path)
    before = db_path.read_bytes()

    garbage = b"not a real gzip payload at all"
    garbage_gz = gzip.compress(garbage, mtime=0)
    sha = hashlib.sha256(garbage_gz).hexdigest()
    manifest = _manifest_for(garbage_gz, sha, built="2099-01-01T00:00:00Z")
    _mock_downloads(monkeypatch, {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: garbage_gz,
    })

    result = anime_db._refresh_from_manifest()
    assert result is None
    assert db_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Failed download leaves the existing DB untouched
# ---------------------------------------------------------------------------

def test_gz_download_failure_leaves_db_untouched(db_path, tmp_path, monkeypatch):
    anime_db.build_manami_artifact(SAMPLE_MANAMI, db_path)
    before = db_path.read_bytes()

    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    manifest = _manifest_for(gz, sha, built="2099-01-01T00:00:00Z")
    _mock_downloads(monkeypatch, {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: RuntimeError("simulated network failure"),
    })

    result = anime_db._refresh_from_manifest()
    assert result is None
    assert db_path.read_bytes() == before


def test_manifest_unreachable_leaves_db_untouched(db_path, tmp_path, monkeypatch):
    anime_db.build_manami_artifact(SAMPLE_MANAMI, db_path)
    before = db_path.read_bytes()
    _mock_downloads(monkeypatch, {
        anime_db.MANIFEST_URL: RuntimeError("simulated network failure"),
    })
    result = anime_db._refresh_from_manifest()
    assert result is None
    assert db_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Schema mismatch refuses the artifact and falls back to a full local build
# ---------------------------------------------------------------------------

def test_refresh_falls_back_to_local_build_on_schema_mismatch(db_path, monkeypatch):
    gz = gzip.compress(b"irrelevant, never downloaded", mtime=0)
    sha = hashlib.sha256(gz).hexdigest()
    manifest = _manifest_for(gz, sha, schema=anime_db._SCHEMA_VERSION + 1,
                             built="2099-01-01T00:00:00Z")

    url_map = {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        anime_db._MANAMI_URLS[0]: json.dumps(SAMPLE_MANAMI).encode(),
        anime_db._FRIBB_URL: json.dumps(SAMPLE_FRIBB).encode(),
        anime_db._ANIME_LISTS_XML_URL: ET.tostring(SAMPLE_XML),
    }
    _mock_downloads(monkeypatch, url_map)

    summary = anime_db.refresh(force=True)
    assert "rebuilt" in summary  # the full local-build path, not the artifact path
    assert db_path.is_file()
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(row[0]) == anime_db._SCHEMA_VERSION
        # Local build merges Fribb/XML too — mappings should be populated,
        # unlike a manifest-artifact swap (which starts with an empty table).
        mapping_row = conn.execute(
            "SELECT tvdb_id FROM mappings WHERE anidb_id=17478").fetchone()
        assert mapping_row and mapping_row[0] == 421737
    finally:
        conn.close()


def test_refresh_disabled_manifest_skips_straight_to_local_build(db_path, monkeypatch):
    monkeypatch.setattr(config, "ANIME_DB_MANIFEST_ENABLED", False)
    calls = []

    def fake_download(url, timeout=180):
        calls.append(url)
        if url == anime_db._MANAMI_URLS[0]:
            return json.dumps(SAMPLE_MANAMI).encode()
        if url == anime_db._FRIBB_URL:
            return json.dumps(SAMPLE_FRIBB).encode()
        if url == anime_db._ANIME_LISTS_XML_URL:
            return ET.tostring(SAMPLE_XML)
        raise AssertionError(f"manifest fetching must be skipped entirely, got {url}")

    monkeypatch.setattr(anime_db, "_download", fake_download)
    summary = anime_db.refresh(force=True)
    assert "rebuilt" in summary
    assert anime_db.MANIFEST_URL not in calls


# ---------------------------------------------------------------------------
# Atomic swap
# ---------------------------------------------------------------------------

def test_atomic_swap_replaces_dest_and_drops_stale_sidecars(tmp_path):
    dest = tmp_path / "anime_meta.sqlite"
    dest.write_bytes(b"old contents")
    (tmp_path / "anime_meta.sqlite-wal").write_bytes(b"stale wal")
    (tmp_path / "anime_meta.sqlite-shm").write_bytes(b"stale shm")
    tmp = tmp_path / "anime_meta.manifest-tmp"
    tmp.write_bytes(b"new contents")

    anime_db._atomic_swap(tmp, dest)

    assert dest.read_bytes() == b"new contents"
    assert not tmp.exists()
    assert not (tmp_path / "anime_meta.sqlite-wal").exists()
    assert not (tmp_path / "anime_meta.sqlite-shm").exists()


# ---------------------------------------------------------------------------
# Full success path: manifest -> verified swap -> local Fribb/XML merge
# ---------------------------------------------------------------------------

def test_successful_manifest_refresh_swaps_and_merges_locally(db_path, tmp_path, monkeypatch):
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    manifest = _manifest_for(gz, sha, built="2099-01-01T00:00:00Z")
    url_map = {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: gz,
        anime_db._FRIBB_URL: json.dumps(SAMPLE_FRIBB).encode(),
        anime_db._ANIME_LISTS_XML_URL: ET.tostring(SAMPLE_XML),
    }
    _mock_downloads(monkeypatch, url_map)

    summary = anime_db.refresh(force=True)
    assert "published artifact" in summary
    assert db_path.is_file()

    hits = anime_db.search("Mashle")
    assert hits and hits[0].anidb_id == 17478
    mapping = anime_db.mapping_for_anidb(17478)
    assert mapping is not None and mapping["tvdb_id"] == 421737


def test_merge_failure_after_swap_does_not_undo_swap(db_path, tmp_path, monkeypatch):
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)
    manifest = _manifest_for(gz, sha, built="2099-01-01T00:00:00Z")
    _mock_downloads(monkeypatch, {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: gz,
        # Fribb/XML both unreachable — merge should fail silently, swap stays.
    })
    summary = anime_db.refresh(force=True)
    assert "published artifact" in summary
    assert db_path.is_file()
    # manami tables made it in even though the id-mapping merge failed.
    hits = anime_db.search("Mashle")
    assert hits and hits[0].anidb_id == 17478
    assert anime_db.mapping_for_anidb(17478) is None


# ---------------------------------------------------------------------------
# publish_anime_db.py: local round-trip proof (no network)
# ---------------------------------------------------------------------------

def test_publish_script_package_artifact_round_trip(tmp_path):
    """Normal publish: Cole's 2026-07-19 decision bundles all three sources,
    so a successful build with both mapping sources available must ship
    with `mappings` populated, not empty."""
    out_dir = tmp_path / "dist-anime-db"
    manifest = publish_anime_db.package_artifact(
        SAMPLE_MANAMI, out_dir, fribb=SAMPLE_FRIBB, xml_root=SAMPLE_XML)

    gz_path = out_dir / publish_anime_db.GZ_NAME
    db_path_out = out_dir / publish_anime_db.ARTIFACT_NAME
    manifest_path = out_dir / publish_anime_db.MANIFEST_NAME
    notice_path = out_dir / publish_anime_db.NOTICE_NAME
    for p in (gz_path, db_path_out, manifest_path, notice_path):
        assert p.is_file(), f"missing {p}"

    gz_bytes = gz_path.read_bytes()
    assert hashlib.sha256(gz_bytes).hexdigest() == manifest["sha256"]
    assert manifest["bytes"] == len(gz_bytes)
    assert manifest["schema_version"] == anime_db._SCHEMA_VERSION
    assert manifest["url"].endswith(publish_anime_db.GZ_NAME)

    # No WAL/SHM sidecars — the shipped file must be single-file-clean.
    assert not (out_dir / (publish_anime_db.ARTIFACT_NAME + "-wal")).exists()
    assert not (out_dir / (publish_anime_db.ARTIFACT_NAME + "-shm")).exists()

    decompressed = gzip.decompress(gz_bytes)
    tmp_db = tmp_path / "roundtrip.sqlite"
    tmp_db.write_bytes(decompressed)
    conn = sqlite3.connect(str(tmp_db))
    try:
        schema_row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(schema_row[0]) == anime_db._SCHEMA_VERSION
        entries_row = conn.execute(
            "SELECT value FROM meta WHERE key='entries'").fetchone()
        assert int(entries_row[0]) == len(SAMPLE_MANAMI["data"])
        title_row = conn.execute(
            "SELECT title FROM anime WHERE title='Mashle'").fetchone()
        assert title_row is not None
        # Bundled: Fribb + Anime-Lists mappings ship IN the artifact now.
        mapping_row = conn.execute(
            "SELECT tvdb_id FROM mappings WHERE anidb_id=17478").fetchone()
        assert mapping_row and mapping_row[0] == 421737
    finally:
        conn.close()

    notice = notice_path.read_text(encoding="utf-8")
    assert "ODbL" in notice
    assert "Fribb/anime-lists" in notice
    assert "Anime-Lists/anime-lists" in notice
    # Wraps across a line in NOTICE_TEXT — check both halves rather than one
    # contiguous phrase.
    assert "do not declare a" in notice
    assert "license for their data" in notice


def test_publish_script_degrades_to_manami_only_when_mapping_sources_unavailable(
        tmp_path, capsys):
    """Graceful degradation: an explicitly empty/absent Fribb/Anime-Lists
    source (simulating "unreachable at build time") must still produce a
    valid, publishable artifact — manami tables alone are enough — and the
    degradation must be visible in the script's own output, never a hard
    failure of the whole weekly build."""
    out_dir = tmp_path / "dist-anime-db"
    manifest = publish_anime_db.package_artifact(
        SAMPLE_MANAMI, out_dir, fribb=[], xml_root=None)
    assert manifest["schema_version"] == anime_db._SCHEMA_VERSION

    captured = capsys.readouterr()
    assert "Fribb id mappings unavailable" in captured.out
    assert "Anime-Lists XML unavailable" in captured.out

    gz_bytes = (out_dir / publish_anime_db.GZ_NAME).read_bytes()
    decompressed = gzip.decompress(gz_bytes)
    tmp_db = tmp_path / "degraded.sqlite"
    tmp_db.write_bytes(decompressed)
    conn = sqlite3.connect(str(tmp_db))
    try:
        title_row = conn.execute(
            "SELECT title FROM anime WHERE title='Mashle'").fetchone()
        assert title_row is not None
        mapping_count = conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]
        assert mapping_count == 0
    finally:
        conn.close()


def test_publish_script_default_fetch_degrades_when_network_down(
        tmp_path, monkeypatch, capsys):
    """package_artifact's DEFAULT path (no fribb/xml_root override) fetches
    live via anime_db._fetch_fribb/_fetch_anime_lists_xml. Both already
    degrade to empty/None internally rather than raise — this proves the
    publish script's actual default code path inherits that degradation and
    still finishes successfully, not just the explicit-empty-args case
    above."""
    def always_fails(url, timeout=180):
        raise RuntimeError("simulated network failure")
    monkeypatch.setattr(anime_db, "_download", always_fails)

    out_dir = tmp_path / "dist-anime-db"
    manifest = publish_anime_db.package_artifact(SAMPLE_MANAMI, out_dir)
    assert manifest["schema_version"] == anime_db._SCHEMA_VERSION

    captured = capsys.readouterr()
    assert "Fribb id mappings unavailable" in captured.out
    assert "Anime-Lists XML unavailable" in captured.out


def test_publish_then_client_refresh_end_to_end(db_path, tmp_path, monkeypatch):
    """The closing gate's local round-trip proof: publish_anime_db builds the
    FULL artifact (bundled mappings) exactly like CI would, and
    anime_db._refresh_from_manifest() consumes it exactly like a real client
    would — entirely offline. Fribb/Anime-Lists URLs are deliberately NOT
    registered in the mock: a bundled artifact must never re-fetch them."""
    out_dir = tmp_path / "dist-anime-db"
    manifest = publish_anime_db.package_artifact(
        SAMPLE_MANAMI, out_dir, fribb=SAMPLE_FRIBB, xml_root=SAMPLE_XML)
    gz_bytes = (out_dir / publish_anime_db.GZ_NAME).read_bytes()

    url_map = {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: gz_bytes,
    }
    _mock_downloads(monkeypatch, url_map)

    summary = anime_db.refresh(force=True)
    assert "published artifact" in summary
    hits = anime_db.search("Mashle")
    assert hits and hits[0].anidb_id == 17478
    mapping = anime_db.mapping_for_anidb(17478)
    assert mapping is not None and mapping["tvdb_id"] == 421737


# ---------------------------------------------------------------------------
# Client-side redundant-merge skip (Task I follow-up, 2026-07-19): a bundled
# artifact must not re-fetch Fribb/Anime-Lists; a mappings-empty artifact
# (manami-only — an older publish, or a degraded CI build) must still fall
# back to the local merge.
# ---------------------------------------------------------------------------

def test_refresh_skips_redundant_merge_when_artifact_bundles_mappings(
        db_path, tmp_path, monkeypatch):
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path, fribb=SAMPLE_FRIBB, xml_root=SAMPLE_XML)
    manifest = _manifest_for(gz, sha, built="2099-01-01T00:00:00Z")

    def trap(url, timeout=180):
        if url in (anime_db._FRIBB_URL, anime_db._ANIME_LISTS_XML_URL):
            raise AssertionError(
                f"redundant fetch of {url} — a bundled artifact must skip this")
        if url == anime_db.MANIFEST_URL:
            return json.dumps(manifest).encode()
        if url == manifest["url"]:
            return gz
        raise RuntimeError(f"unexpected url {url}")

    monkeypatch.setattr(anime_db, "_download", trap)

    summary = anime_db.refresh(force=True)
    assert "published artifact" in summary
    mapping = anime_db.mapping_for_anidb(17478)
    assert mapping is not None and mapping["tvdb_id"] == 421737


def test_refresh_still_merges_locally_when_artifact_has_no_mappings(
        db_path, tmp_path, monkeypatch):
    gz, sha = _gz_of(SAMPLE_MANAMI, tmp_path)  # manami-only: empty mappings
    manifest = _manifest_for(gz, sha, built="2099-01-01T00:00:00Z")
    url_map = {
        anime_db.MANIFEST_URL: json.dumps(manifest).encode(),
        manifest["url"]: gz,
        anime_db._FRIBB_URL: json.dumps(SAMPLE_FRIBB).encode(),
        anime_db._ANIME_LISTS_XML_URL: ET.tostring(SAMPLE_XML),
    }
    _mock_downloads(monkeypatch, url_map)

    summary = anime_db.refresh(force=True)
    assert "published artifact" in summary
    mapping = anime_db.mapping_for_anidb(17478)
    assert mapping is not None and mapping["tvdb_id"] == 421737
