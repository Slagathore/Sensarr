# =============================================================================
# Task D (Phase 5) — identity-first TV routing (route by show_id, not title
# strings), season-contradiction as a verification failure, requested-season
# fill-in when the file carries no season evidence, and the needs-placement row.
#
# These drive the REAL _post_process with on-disk tmp roots; find_show_folder is
# monkeypatched to explode so any fuzzy fallthrough fails loudly (the gate:
# request-linked TV routes by show_id without a fuzzy call).
# =============================================================================

import hashlib
import json

import pytest

import config
import db
import download_manager
import downloads_store
import queue_store
import shows_store
import torrent_routing
from download_manager import DownloadManager


def _clean():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "selection_runs", "candidate_decisions", "failed_grabs",
                      "grab_deferrals", "blocklist", "download_files",
                      "request_downloads", "needs_placement", "episodes",
                      "tracked_shows", "show_folders", "season_targets"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh():
    _clean()
    yield
    _clean()


def _hash(seed):
    return hashlib.sha1(seed.encode()).hexdigest()


def _write(path, size=3000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _tv_want(*, title, season, source="tvdb", ext="100", countries=()):
    return {
        "schema": 1, "media_type": "tv", "identity_source": source,
        "external_id": ext, "canonical_title": title, "canonical_year": None,
        "origin_countries": list(countries), "aliases": [],
        "search_alias": title, "season": season, "episode": None,
        "size_pref_mb_min": 0, "size_max_rate": 0, "runtime_minutes": None,
    }


def _make_pack_download(*, want, staging, files, show_id, season, request_id=None,
                        seed=None):
    seed = seed or files[0]
    did = downloads_store.create_download(
        title=files[0].rsplit("/", 1)[-1].rsplit(".", 1)[0],
        magnet=f"magnet:?xt=urn:btih:{_hash(seed)}",
        source="tpb", media_type="tv", request_id=request_id,
        staging_dir=str(staging), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=True, auto_move=True,
        show_id=show_id, season=season, episode=None,
        want_json=json.dumps(want))
    downloads_store.set_files(did, files)
    downloads_store.set_status(did, "downloaded", completed=True)
    return did


def _dm(monkeypatch, *, no_fuzzy=True):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    if no_fuzzy:
        def _boom(*a, **k):
            raise AssertionError("find_show_folder (fuzzy) must not be called "
                                 "for a show_id-linked TV download")
        monkeypatch.setattr("download_manager.torrent_routing.find_show_folder",
                            _boom)
    return dm


def _set_tv_root(monkeypatch, root):
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="tv")])
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(root.parent / "staging"))


# ---------------------------------------------------------------------------
# Route by show_id (no fuzzy), existing show
# ---------------------------------------------------------------------------

def test_new_season_routes_by_show_id_into_existing_show(tmp_path, monkeypatch):
    tv_root = tmp_path / "TV"
    show_dir = tv_root / "My Show"
    (show_dir / "Season 01").mkdir(parents=True)
    _set_tv_root(monkeypatch, tv_root)
    show_id = shows_store.upsert_show(title="My Show", media_type="tv",
                                      source="tvdb", external_id="100")
    shows_store.add_show_folder(show_id, str(show_dir))

    staging = tmp_path / "staging"
    e1 = _write(staging / "My.Show.S02E01.1080p.WEB.x264-GRP.mkv")
    e2 = _write(staging / "My.Show.S02E02.1080p.WEB.x264-GRP.mkv")
    want = _tv_want(title="My Show", season=2)
    did = _make_pack_download(want=want, staging=staging,
                              files=[e1.name, e2.name], show_id=show_id, season=2)
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    # Landed under the show's OWN folder / Season 02, from show_id (no fuzzy).
    landed = sorted(p.name for p in show_dir.rglob("*.mkv"))
    assert landed == ["My Show - S02E01.mkv", "My Show - S02E02.mkv"]
    assert (show_dir / "Season 02").is_dir()


def test_new_show_no_folders_routes_by_show_id_creates_folder(tmp_path, monkeypatch):
    tv_root = tmp_path / "TV"
    tv_root.mkdir()
    _set_tv_root(monkeypatch, tv_root)
    # Tracked show exists (upserted at request time) but has NO mapped folders.
    show_id = shows_store.upsert_show(title="Brand New Show", media_type="tv",
                                      source="tvdb", external_id="200")

    staging = tmp_path / "staging"
    e1 = _write(staging / "Brand.New.Show.S01E01.1080p.WEB.x264-GRP.mkv")
    want = _tv_want(title="Brand New Show", season=1, ext="200")
    did = _make_pack_download(want=want, staging=staging, files=[e1.name],
                              show_id=show_id, season=1)
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    created = tv_root / "Brand New Show" / "Season 01"
    assert (created / "Brand New Show - S01E01.mkv").is_file()


# ---------------------------------------------------------------------------
# Season contradiction is a verification failure (Design stance 8)
# ---------------------------------------------------------------------------

def test_season_contradiction_stays_staged_and_blocked(tmp_path, monkeypatch):
    tv_root = tmp_path / "TV"
    show_dir = tv_root / "Contradict Show"
    (show_dir / "Season 01").mkdir(parents=True)
    _set_tv_root(monkeypatch, tv_root)
    show_id = shows_store.upsert_show(title="Contradict Show", media_type="tv",
                                      source="tvdb", external_id="300")
    shows_store.add_show_folder(show_id, str(show_dir))
    req = queue_store.add_request(
        "contradict show", "cole", media_type="tv",
        resolved_title="Contradict Show", external_id="300",
        identity_source="tvdb", season=2, aliases=["Contradict Show"])

    staging = tmp_path / "staging"
    # Want S02, but the payload is S03 — a contradiction.
    bad = _write(staging / "Contradict.Show.S03E01.1080p.WEB.x264-GRP.mkv")
    want = _tv_want(title="Contradict Show", season=2, ext="300")
    did = _make_pack_download(want=want, staging=staging, files=[bad.name],
                              show_id=show_id, season=2,
                              request_id=req.request_id)
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "quarantined" in out
    # No S03 file reached the library root.
    assert not list(show_dir.rglob("*.mkv"))
    assert bad.exists()                              # kept in staging
    # Blocked for THIS season's subject; download archived; request reopened.
    entries = downloads_store.blocklist_entries_for_subject("tvdb:300:s2")
    assert entries
    assert downloads_store.get_download(did).verification_state == "quarantined"
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN


# ---------------------------------------------------------------------------
# No season evidence -> the requested season fills in
# ---------------------------------------------------------------------------

def test_no_season_evidence_gets_requested_season(tmp_path, monkeypatch):
    tv_root = tmp_path / "TV"
    show_dir = tv_root / "Bare Show"
    (show_dir / "Season 01").mkdir(parents=True)
    _set_tv_root(monkeypatch, tv_root)
    show_id = shows_store.upsert_show(title="Bare Show", media_type="tv",
                                      source="tvdb", external_id="400")
    shows_store.add_show_folder(show_id, str(show_dir))

    staging = tmp_path / "staging"
    # A file carrying NO season evidence (a bare absolute episode number, no
    # SxxEyy). Its trailing "05" must NOT read as sequel #5 (verification
    # extension), and the requested season fills in for placement.
    bare = _write(staging / "Bare Show - 05 [1080p].mkv")
    want = _tv_want(title="Bare Show", season=2, ext="400")
    did = _make_pack_download(want=want, staging=staging, files=[bare.name],
                              show_id=show_id, season=2)
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out                        # NOT quarantined as a sequel
    # The requested season (S02) is applied — the file lands under Season 02.
    placed = list(show_dir.rglob("*.mkv"))
    assert placed and placed[0].parent.name == "Season 02"


# ---------------------------------------------------------------------------
# Staging fallback surfaces a needs-placement row + the create-folder action
# ---------------------------------------------------------------------------

def test_staging_fallback_surfaces_needs_placement(tmp_path, monkeypatch):
    # No configured TV root anywhere -> plan_for_season lands in staging.
    monkeypatch.setattr(config, "MEDIA_LIBRARY_PATHS", [])
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path / "staging"))
    show_id = shows_store.upsert_show(title="Homeless Show", media_type="tv",
                                      source="tvdb", external_id="500")
    staging = tmp_path / "staging"
    e1 = _write(staging / "Homeless.Show.S01E01.1080p.WEB.x264-GRP.mkv")
    want = _tv_want(title="Homeless Show", season=1, ext="500")
    did = _make_pack_download(want=want, staging=staging, files=[e1.name],
                              show_id=show_id, season=1)
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "left in staging" in out
    rows = downloads_store.list_needs_placement()
    assert any(r.download_id == did for r in rows)
    assert e1.exists()  # nothing moved


def test_create_placement_folder_places_files(tmp_path, monkeypatch):
    tv_root = tmp_path / "TV"
    tv_root.mkdir()
    monkeypatch.setattr(config, "MEDIA_LIBRARY_PATHS", [])
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path / "staging"))
    show_id = shows_store.upsert_show(title="Place Me", media_type="tv",
                                      source="tvdb", external_id="600")
    staging = tmp_path / "staging"
    e1 = _write(staging / "Place.Me.S01E01.1080p.WEB.x264-GRP.mkv")
    want = _tv_want(title="Place Me", season=1, ext="600")
    did = _make_pack_download(want=want, staging=staging, files=[e1.name],
                              show_id=show_id, season=1)
    dm = _dm(monkeypatch, no_fuzzy=False)
    dm._post_process(did)                            # lands in staging
    assert downloads_store.get_needs_placement(did) is not None

    target = tv_root / "Place Me" / "Season 01"
    out = dm.create_placement_folder(did, dest_dir=str(target))

    assert "created" in out and "moved to" in out
    assert (target / "Place Me - S01E01.mkv").is_file()
    assert downloads_store.get_needs_placement(did) is None   # resolved


# ---------------------------------------------------------------------------
# Identity-first show resolution (upsert by source+external_id, not fuzzy)
# ---------------------------------------------------------------------------

def test_tracked_show_for_request_resolves_by_identity():
    dm = DownloadManager()
    req = queue_store.add_request(
        "some show", "cole", media_type="tv", resolved_title="Some Show",
        external_id="777", identity_source="tvdb", season=1,
        aliases=["Some Show"])
    show = dm._tracked_show_for_request(req)
    assert show is not None
    assert (show.source, show.external_id) == ("tvdb", "777")
    # A second call resolves the SAME row (idempotent upsert, no duplicate).
    again = dm._tracked_show_for_request(req)
    assert again.show_id == show.show_id
    assert len(shows_store.list_shows()) == 1
