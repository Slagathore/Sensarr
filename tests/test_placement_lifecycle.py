"""Placement is actionable only while its exact completed payload still needs it."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import db
import download_manager
import downloads_store
import grab_queue
import queue_store
import shows_store
import torrent_routing
from download_manager import DownloadManager

# conftest disables the automatic entry point; this reference lets one test
# exercise its real state-machine wiring with the process sweep stubbed.
_RECOVER_SESSION = DownloadManager._recover_previous_session


@pytest.fixture(autouse=True)
def clean_database():
    # conftest pins APP_DB_PATH to an isolated temporary database before imports.
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history", "download_files",
                      "request_downloads", "needs_placement", "tracked_shows",
                      "show_folders", "season_targets", "episodes"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def make_download(tmp_path, *, names=("Show.S01E01.mkv",), request_id=None,
                  show_id=None, season=1, episode=None):
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    did = downloads_store.create_download(
        title="Show S01", magnet="magnet:?xt=urn:btih:" + "b" * 40,
        source="test", media_type="tv", request_id=request_id,
        staging_dir=str(staging), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=True,
        show_id=show_id, season=season, episode=episode,
        want_json=json.dumps({"media_type": "tv", "canonical_title": "Show",
                              "season": season, "episode": episode}))
    downloads_store.set_files(did, list(names))
    downloads_store.set_status(did, "downloaded", completed=True)
    return did, staging


def marker(did, target):
    downloads_store.record_needs_placement(
        did, show_id=None, season=1, suggested_dir=str(target),
        reason="no confident route")


def marker_audit(did):
    with db.connect() as conn:
        return conn.execute(
            "SELECT reason, resolved_at FROM needs_placement WHERE download_id = ?",
            (did,)).fetchone()


@pytest.fixture
def manager(monkeypatch, tmp_path):
    manager = DownloadManager()
    monkeypatch.setattr(manager, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(manager, "_cleanup_staging_leftovers", lambda row: None)
    monkeypatch.setattr(manager, "_settle_requests_for_show", lambda *a: None)
    monkeypatch.setattr(manager, "_record_quality_labels", lambda *a: None)
    monkeypatch.setattr(download_manager.presence_ledger, "register_download",
                        lambda *a, **k: SimpleNamespace(
                            show_id=None, episodes=(), season=None))
    monkeypatch.setattr(download_manager.presence_ledger, "reap_redundant", lambda *a: None)
    monkeypatch.setattr("staging_sweep.remove_pack_neighbours", lambda row: None)
    monkeypatch.setattr(download_manager, "_library_copy_is_complete", lambda *a: False)
    monkeypatch.setattr(torrent_routing, "plan_route", lambda *a, **k:
                        torrent_routing.RoutePlan(
                            confident=True, dest_dir=str(tmp_path / "library"),
                            reason="exact test destination"))
    return manager


@pytest.mark.parametrize("status", ["queued", "downloading", "moved", "cancelled", "error", "stopped"])
def test_status_transition_resolves_marker_and_preserves_audit(tmp_path, status):
    did, _ = make_download(tmp_path)
    marker(did, tmp_path / "library")
    downloads_store.add_history(did, "grabbed", before=None, after="audit")

    downloads_store.set_status(did, status)

    assert downloads_store.list_needs_placement() == []
    assert marker_audit(did)[0] == "no confident route"
    assert marker_audit(did)[1] is not None
    assert any(h.after_value == "audit" for h in downloads_store.history_for_download(did))
    # A late import callback cannot resurrect a cancelled/completed marker.
    marker(did, tmp_path / "library")
    assert marker_audit(did)[1] is not None


@pytest.mark.parametrize("action", ["archive", "quarantine", "delete"])
def test_removal_resolves_marker_without_deleting_it(tmp_path, action):
    did, _ = make_download(tmp_path)
    marker(did, tmp_path / "library")
    if action == "archive":
        downloads_store.tombstone_download(did, reason="removed")
    elif action == "quarantine":
        downloads_store.set_verification(did, "quarantined")
    else:
        downloads_store.delete_download(did)
    assert downloads_store.get_needs_placement(did) is None
    assert marker_audit(did)[1] is not None


def test_legacy_stale_markers_are_hidden_then_durably_resolved(tmp_path):
    ids = []
    for status in ("downloaded", "moved", "cancelled", "downloaded", "downloaded"):
        did, _ = make_download(tmp_path)
        marker(did, tmp_path / "library")
        ids.append(did)
        with db.connect() as conn:
            conn.execute("UPDATE downloads SET status = ? WHERE id = ?", (status, did))
            conn.commit()
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET removed_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (ids[3],))
        conn.execute("DELETE FROM downloads WHERE id = ?", (ids[4],))
        conn.commit()

    assert [m.download_id for m in downloads_store.list_needs_placement()] == [ids[0]]
    assert grab_queue.status_counts().needs_placement == 1
    assert [r.download_id for r in grab_queue.needs_placement_rows()] == [ids[0]]
    assert downloads_store.reconcile_needs_placement() == 4
    assert downloads_store.reconcile_needs_placement() == 0
    assert all(marker_audit(did)[1] is not None for did in ids[1:])
    assert marker_audit(ids[0])[1] is None


@pytest.mark.parametrize("state", ["moved", "cancelled", "queued", "archived", "quarantined"])
def test_manual_create_rejects_inactive_job_before_filesystem_changes(tmp_path, manager, state):
    did, staging = make_download(tmp_path)
    source = staging / "Show.S01E01.mkv"
    source.write_bytes(b"untouched")
    target = tmp_path / "new destination"
    marker(did, target)
    if state == "archived":
        downloads_store.tombstone_download(did, reason="removed")
    elif state == "quarantined":
        downloads_store.set_verification(did, state)
    else:
        downloads_store.set_status(did, state)
    result = manager.create_placement_folder(did)
    assert "inactive" in result
    assert not target.exists()
    assert source.read_bytes() == b"untouched"


def test_manual_create_keeps_marker_when_import_fails(tmp_path, manager, monkeypatch):
    did, staging = make_download(tmp_path)
    (staging / "Show.S01E01.mkv").write_bytes(b"payload")
    marker(did, tmp_path / "library")

    def broken(*a, **k):
        assert marker_audit(did)[1] is None
        raise OSError("destination locked")

    monkeypatch.setattr(manager, "_post_process", broken)
    assert "import failed: destination locked" in manager.create_placement_folder(did)
    assert downloads_store.get_needs_placement(did) is not None
    assert downloads_store.get_download(did).error == "import failed: destination locked"


def test_ordinary_success_resolves_marker_without_manual_create(tmp_path, manager):
    did, staging = make_download(tmp_path)
    (staging / "Show.S01E01.mkv").write_bytes(b"payload")
    marker(did, tmp_path / "library")
    assert manager.apply_route(did).startswith("moved to")
    assert marker_audit(did)[1] is not None
    receipt = downloads_store.list_download_files(did)[0]
    assert receipt.size_bytes == len(b"payload")
    assert Path(receipt.final_path).read_bytes() == b"payload"


def test_missing_manifest_never_borrows_similarly_named_neighbor(tmp_path, manager):
    did, staging = make_download(tmp_path)
    neighbor = staging / "Show S01"
    neighbor.mkdir()
    (neighbor / "Show.S01E02.mkv").write_bytes(b"another download")
    assert manager._media_files_in_staging(downloads_store.get_download(did)) == []
    manager.apply_route(did)
    assert (neighbor / "Show.S01E02.mkv").read_bytes() == b"another download"
    assert not (tmp_path / "library").exists()


def test_missing_payload_is_held_for_review_without_regrab_or_fake_placement(tmp_path, manager):
    req = queue_store.add_request("Show", "test", media_type="tv", resolved_title="Show",
                                  identity_source="tvdb", external_id="123", season=1)
    queue_store.set_status(req.request_id, queue_store.STATUS_GRABBING)
    did, _ = make_download(tmp_path, request_id=req.request_id, show_id=7)
    marker(did, tmp_path / "library")
    # A planned target's mere existence is NOT a receipt for this payload.
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "Show.S01E01.mkv").write_bytes(b"unattributed")

    assert manager._recover_completed_download(downloads_store.get_download(did)) == "manual"
    row = downloads_store.get_download(did)
    assert row.status == "downloaded" and row.verification_state == "failed"
    assert downloads_store.display_status(row.status, row.progress, error=row.error) == "missing files"
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_NEEDS_ATTENTION
    assert req.request_id in downloads_store.request_ids_with_downloads()
    assert manager._season_pack_in_flight(None, 7, 1) == did
    assert downloads_store.get_needs_placement(did) is None
    assert not any(h.action in ("moved", "reconciled") for h in downloads_store.history_for_download(did))
    before = len(downloads_store.history_for_download(did))
    assert manager._recover_completed_download(row) == "manual"
    assert len(downloads_store.history_for_download(did)) == before


def test_exact_prior_move_recovers_only_all_manifest_members(tmp_path, manager):
    names = ("Show.S01E01.mkv", "Show.S01E02.mkv")
    did, staging = make_download(tmp_path, names=names)
    library = tmp_path / "library"
    library.mkdir()
    first = library / names[0]
    first.write_bytes(b"first placed")
    downloads_store.add_history(did, "moved", before=str(staging / names[0]), after=str(first))
    assert manager._reconcile_prior_move(downloads_store.get_download(did)) == []
    assert downloads_store.get_download(did).status == "downloaded"
    second = library / names[1]
    second.write_bytes(b"second placed")
    downloads_store.add_history(did, "moved", before=str(staging / names[1]), after=str(second))
    assert set(manager._reconcile_prior_move(downloads_store.get_download(did))) == {str(first), str(second)}
    assert downloads_store.get_download(did).verification_state == "verified"


def test_reappeared_source_prevents_historical_receipt_from_completing_job(tmp_path, manager):
    did, staging = make_download(tmp_path)
    source = staging / "Show.S01E01.mkv"
    source.write_bytes(b"different current payload")
    final = tmp_path / "previous.mkv"
    final.write_bytes(b"old placement")
    downloads_store.add_history(did, "moved", before=str(source), after=str(final))
    assert manager._reconcile_prior_move(downloads_store.get_download(did)) == []
    assert source.read_bytes() == b"different current payload"


@pytest.mark.parametrize("receipt_state,size", [("failed", 4), ("verified", 99)])
def test_failed_or_changed_receipt_cannot_fall_back_to_old_move_history(
        tmp_path, manager, receipt_state, size):
    did, staging = make_download(tmp_path)
    source = staging / "Show.S01E01.mkv"
    final = tmp_path / "placed.mkv"
    final.write_bytes(b"four")
    downloads_store.add_history(did, "moved", before=str(source), after=str(final))
    downloads_store.add_download_file(
        did, source_relative_path=source.name, source_absolute_path=str(source),
        final_path=str(final), media_role="episode", size_bytes=size,
        verification_state=receipt_state)
    assert manager._reconcile_prior_move(downloads_store.get_download(did)) == []
    assert downloads_store.get_download(did).status == "downloaded"


def test_startup_reconciles_stale_marker_and_holds_missing_payload(
        tmp_path, manager, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lambda *a: [], NoSuchProcess=ProcessLookupError,
        AccessDenied=PermissionError))
    monkeypatch.setattr(downloads_store, "registered_runners", lambda: [])
    missing, _ = make_download(tmp_path)
    stale, _ = make_download(tmp_path)
    marker(missing, tmp_path / "missing-target")
    marker(stale, tmp_path / "stale-target")
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET status = 'cancelled' WHERE id = ?", (stale,))
        conn.commit()

    _RECOVER_SESSION(manager)

    assert marker_audit(stale)[1] is not None
    assert marker_audit(missing)[1] is not None
    assert downloads_store.get_download(missing).error.startswith("missing staging payload:")
    assert not (tmp_path / "missing-target").exists()


@pytest.mark.parametrize("legacy_moved", [False, True])
def test_partial_pack_retries_remaining_file_and_keeps_first_receipt(tmp_path, manager, legacy_moved):
    names = ("Show.S01E01.mkv", "Show.S01E02.mkv")
    did, staging = make_download(tmp_path, names=names)
    for name in names:
        (staging / name).write_bytes(name.encode() * 100)
    library = tmp_path / "library"
    library.mkdir()
    blocker = library / names[1]
    blocker.write_bytes(b"different existing library file")

    outcome = manager.apply_route(did)
    row = downloads_store.get_download(did)
    assert "partial placement" in outcome
    assert row.status == "downloaded" and row.verification_state == "partial"
    first_receipt = next(r for r in downloads_store.list_download_files(did)
                         if r.source_relative_path == names[0])
    first_hash = hashlib.sha256((library / names[0]).read_bytes()).hexdigest()
    assert first_receipt.size_bytes == len(names[0].encode() * 100)
    assert manager._reconcile_prior_move(row) == []
    assert manager.apply_route(did).startswith("processed (no move)")
    assert downloads_store.get_download(did).verification_state == "partial"
    assert first_receipt in downloads_store.list_download_files(did)
    if legacy_moved:
        downloads_store.set_status(did, "moved")
        assert manager._recover_completed_download(downloads_store.get_download(did)) == "manual"
        assert downloads_store.get_download(did).status == "downloaded"
        # Exercise the direct legacy Apply Route entry point too.
        downloads_store.set_status(did, "moved")
    blocker.unlink()  # Test-only: user has resolved the conflicting destination.

    assert manager.apply_route(did).startswith("moved to")
    row = downloads_store.get_download(did)
    assert row.status == "moved" and row.verification_state == "verified"
    receipts = downloads_store.list_download_files(did)
    assert len(receipts) == 2
    assert all(r.verification_state == "verified" for r in receipts)
    assert first_receipt in receipts
    assert hashlib.sha256((library / names[0]).read_bytes()).hexdigest() == first_hash
    assert (library / names[1]).read_bytes() == names[1].encode() * 100


def test_missing_pack_member_does_not_become_verified_when_one_moves(tmp_path, manager):
    did, staging = make_download(tmp_path, names=("Show.S01E01.mkv", "Show.S01E02.mkv"))
    (staging / "Show.S01E01.mkv").write_bytes(b"only one member remains")
    assert "partial placement (1/2" in manager.apply_route(did)
    row = downloads_store.get_download(did)
    assert row.status == "downloaded" and row.verification_state == "partial"
    assert manager._recover_completed_download(row) == "manual"
    assert downloads_store.get_download(did).verification_state == "partial"


@pytest.mark.parametrize("kind", ["parent", "absolute"])
def test_manifest_and_cleanup_refuse_paths_outside_staging(tmp_path, manager, kind):
    outside_video = tmp_path / "Show.S01E01.mkv"
    outside_junk = tmp_path / "retain.txt"
    outside_video.write_bytes(b"outside media")
    outside_junk.write_bytes(b"outside document")
    entries = (("../Show.S01E01.mkv", "../retain.txt") if kind == "parent"
               else (str(outside_video), str(outside_junk)))
    did, _ = make_download(tmp_path, names=entries)
    row = downloads_store.get_download(did)
    assert manager._media_files_in_staging(row) == []
    DownloadManager._cleanup_staging_leftovers(manager, row)
    manager.apply_route(did)
    assert outside_video.read_bytes() == b"outside media"
    assert outside_junk.read_bytes() == b"outside document"
    assert not (tmp_path / "library").exists()


def test_absolute_legacy_manifest_inside_staging_still_imports(tmp_path, manager):
    did, staging = make_download(tmp_path)
    source = staging / "Show.S01E01.mkv"
    source.write_bytes(b"owned media")
    downloads_store.set_files(did, [str(source)])
    assert manager.apply_route(did).startswith("moved to")
    assert (tmp_path / "library" / source.name).read_bytes() == b"owned media"


@pytest.mark.parametrize("inside", [False, True])
def test_manifest_and_cleanup_reject_directory_links(tmp_path, manager, inside):
    import os
    did, staging = make_download(tmp_path, names=("link/Show.S01E01.mkv", "link/keep.txt"))
    target = (staging if inside else tmp_path) / "real_directory"
    target.mkdir()
    (target / "Show.S01E01.mkv").write_bytes(b"keep video")
    (target / "keep.txt").write_bytes(b"keep document")
    link = staging / "link"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        row = downloads_store.get_download(did)
        assert manager._media_files_in_staging(row) == []
        DownloadManager._cleanup_staging_leftovers(manager, row)
        assert (target / "Show.S01E01.mkv").read_bytes() == b"keep video"
        assert (target / "keep.txt").read_bytes() == b"keep document"
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove only the temporary junction itself.
        else:
            link.unlink()


@pytest.mark.parametrize("guard", ["changed_size", "unknown_size", "new_failed", "new_failed_alias", "removed_receipt", "archived_owner", "source_mismatch"])
def test_other_download_receipt_cannot_falsely_complete_missing_payload(tmp_path, manager, guard):
    missing, staging = make_download(tmp_path)
    owner, _ = make_download(tmp_path)
    final = tmp_path / "placed.mkv"
    final.write_bytes(b"x")
    receipt = downloads_store.add_download_file(
        owner, source_relative_path="Show.S01E01.mkv",
        source_absolute_path=str(staging / ("other.mkv" if guard == "source_mismatch" else "Show.S01E01.mkv")),
        media_role="episode", final_path=str(final), verification_state="verified",
        size_bytes=None if guard == "unknown_size" else 1000 if guard == "changed_size" else 1)
    downloads_store.set_status(owner, "moved", completed=True)
    if guard.startswith("new_failed"):
        downloads_store.add_download_file(
            owner, source_relative_path=("Pack/../Show.S01E01.mkv"
                                         if guard == "new_failed_alias" else "Show.S01E01.mkv"),
            source_absolute_path=str(staging / "Show.S01E01.mkv"),
            media_role="episode", verification_state="failed", size_bytes=1)
    elif guard == "removed_receipt":
        with db.connect() as conn:
            conn.execute("UPDATE download_files SET removed_at=CURRENT_TIMESTAMP WHERE id=?", (receipt,))
            conn.commit()
    elif guard == "archived_owner":
        downloads_store.tombstone_download(owner, reason="removed")
    assert manager._placed_by_another_download(downloads_store.get_download(missing)) == []
    manager.apply_route(missing)
    row = downloads_store.get_download(missing)
    assert row.status == "downloaded" and row.error.startswith("missing staging payload:")
    assert final.read_bytes() == b"x"


def test_removed_latest_own_receipt_cannot_revive_old_history(tmp_path, manager):
    did, staging = make_download(tmp_path)
    source = staging / "Show.S01E01.mkv"
    final = tmp_path / "placed.mkv"
    final.write_bytes(b"x")
    downloads_store.add_history(did, "moved", before=str(source), after=str(final))
    receipt = downloads_store.add_download_file(
        did, source_relative_path=source.name, source_absolute_path=str(source),
        media_role="episode", final_path=str(final), verification_state="verified", size_bytes=1)
    with db.connect() as conn:
        conn.execute("UPDATE download_files SET removed_at=CURRENT_TIMESTAMP WHERE id=?", (receipt,))
        conn.commit()
    assert manager._reconcile_prior_move(downloads_store.get_download(did)) == []


@pytest.mark.parametrize("release", ["cancel", "remove"])
def test_held_pack_blocks_episode_autograb_until_explicit_release(tmp_path, manager, monkeypatch, release):
    from test_fast_follow import _show, _release, _dm
    show, episode = _show(match=False)
    did, _ = make_download(tmp_path, names=("Fast.Show.S01E03.mkv",), show_id=show.show_id)
    manager._hold_missing_payload(downloads_store.get_download(did))
    grabbed = []
    auto = _dm(monkeypatch, [_release(400)], grabbed)
    monkeypatch.setattr(auto, "_torrentio_target_for_episode", lambda *a: None)
    assert auto._grab_one_episode(show, episode, fast_follow=False) == []
    assert grabbed == []
    if release == "cancel":
        downloads_store.set_status(did, "cancelled")
    else:
        downloads_store.tombstone_download(did, reason="removed")
    assert auto._grab_one_episode(show, episode, fast_follow=False) == [999]
    assert len(grabbed) == 1
