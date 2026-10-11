# =============================================================================
# A wrong movie stamped with a request's identity (the July grabs: 'The Last
# House on the Left (2009)' for 'The Last House' 2026, 'The Boss Baby Christmas
# Bonus (2022)' for 'The Boss Baby' 2017). The watcher's identity join closed
# the request, the startup reconcile reopened it, and the at-grab check kept
# skipping it, from September through October.
# =============================================================================
import hashlib

import db
import downloads_store
import library_identity as li
import maintenance
import queue_store

WRONG = r"I:\Movies\The Last House on the Left (2009)\The Last House on the Left (2009).mp4"
RIGHT = r"I:\Movies\The Last House (2026)\The Last House (2026).mkv"


def _clean():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    li.initialize_library_identity_db()
    import library_index
    library_index.initialize_library_index_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_files",
                      "request_downloads", "library_identity", "library_files",
                      "blocklist"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


def _index(*paths):
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO library_files "
            "(path, name, root_path, search_name, size_bytes, modified_at) "
            "VALUES (?, ?, 'root', ?, 1, 0.0)",
            [(p, p.rsplit("\\", 1)[-1], p.casefold()) for p in paths])
        conn.commit()


def _request():
    return queue_store.add_request(
        "the last house", "cole", media_type="movie",
        resolved_title="The Last House", external_id="1284041",
        identity_source="tmdb", canonical_year=2026)


def _stamp(path, resolved_by=li.RESOLVED_DOWNLOAD):
    li.set_identity(path, media_type="movie", identity_source="tmdb",
                    external_id="1284041", resolved_by=resolved_by,
                    canonical_title="The Last House", canonical_year=2026)


def test_contradiction_check_uses_the_year():
    kw = dict(media_type="movie", identity_source="tmdb", external_id="1284041",
              canonical_title="The Last House", canonical_year=2026)
    assert li.placed_file_contradicts(WRONG, **kw) is True
    assert li.placed_file_contradicts(RIGHT, **kw) is False
    # A one-year drift (festival vs release) is still the same movie.
    assert li.placed_file_contradicts(
        r"I:\Movies\The Last House (2025).mkv", **kw) is False
    # Episodic rows are never second-guessed here.
    assert li.placed_file_contradicts(WRONG, **{**kw, "media_type": "tv"}) is False


def test_record_placement_skips_a_different_movie(monkeypatch):
    _clean()
    req = _request()

    class _Row:
        request_id = req.request_id
        download_id = 579
        season = None
        episode = None
        show_id = None

    monkeypatch.setattr(downloads_store, "list_download_files", lambda did: [])
    assert li.record_placement(_Row(), [WRONG, RIGHT]) == 1
    assert li.get_identity(WRONG) is None
    assert li.get_identity(RIGHT).external_id == "1284041"


def test_backfill_does_not_restamp_a_different_movie(monkeypatch):
    _clean()
    req = _request()

    class _DL:
        download_id = 579

    class _File:
        verification_state = "verified"
        final_path = WRONG
        parsed_season = None
        parsed_episode = None

    monkeypatch.setattr(downloads_store, "downloads_for_request", lambda rid: [_DL()])
    monkeypatch.setattr(downloads_store, "list_download_files", lambda did: [_File()])
    _index(WRONG)
    li.backfill_identities(allow_network=False)
    row = li.get_identity(WRONG)
    assert row is None or row.external_id != "1284041"
    assert req.request_id


def test_purge_drops_only_contradicting_download_rows():
    _clean()
    _stamp(WRONG)
    _stamp(RIGHT)
    batch = r"I:\Movies\Other\Some Other Name (1990).mkv"
    _stamp(batch, resolved_by=li.RESOLVED_BATCH)
    assert li.purge_contradicting_placements("tmdb", "1284041") == 1
    assert li.get_identity(WRONG) is None
    assert li.get_identity(RIGHT) is not None
    # Batch lookups are a separate, weaker source; this purge leaves them be.
    assert li.get_identity(batch) is not None


def test_reopen_clears_the_stamp_so_the_watcher_stops_closing_it(monkeypatch, tmp_path):
    _clean()
    req = _request()
    queue_store.set_status(req.request_id, queue_store.STATUS_FULFILLED)
    magnet = "magnet:?xt=urn:btih:" + hashlib.sha1(b"lh").hexdigest()
    did = downloads_store.create_download(
        title="The Last House On The Left 2009", magnet=magnet, source="tpb",
        media_type="movie", request_id=req.request_id, staging_dir="/tmp",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=True)
    downloads_store.link_request_download(req.request_id, did, "movie")
    wrong = tmp_path / "The Last House on the Left (2009).mp4"
    wrong.write_bytes(b"x")
    downloads_store.add_download_file(
        did, source_relative_path=wrong.name, source_absolute_path="/staging/x",
        final_path=str(wrong), media_role="primary_video",
        verification_state="verified", moved_at="CURRENT")
    _index(str(wrong))
    _stamp(str(wrong))
    monkeypatch.setattr("library_index.search_library", lambda *a, **k: [])

    # Before: the identity join says the request is satisfied.
    assert maintenance.request_present_in_library(
        queue_store.get_request(req.request_id)) is True

    maintenance.daily_library_check()
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN
    assert li.get_identity(str(wrong)) is None

    # After: the watcher no longer finds it, so the request stays open.
    maintenance.reconcile_requests_with_library()
    r = queue_store.get_request(req.request_id)
    assert r.status == queue_store.STATUS_OPEN
    assert maintenance.request_present_in_library(r) is False
    assert wrong.exists()  # the file itself is never touched
