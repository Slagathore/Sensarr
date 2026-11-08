"""Preview/apply parity and deferred cleanup retain the ownership boundary."""
import pytest

import db
import downloads_store
import staging_sweep
from test_staging_sweep import (
    _isolated_rows, staging, _make, _pinned, _touch, _sweep,
)


@pytest.mark.parametrize("dry_run", [False, True])
def test_neighbor_changed_during_owner_refresh_is_preserved(staging, monkeypatch, dry_run):
    neighbor_name = "Pack/S01/E02.mkv"
    row = _pinned(staging, ["Pack/S01/E01.mkv"], age_days=10,
                  neighbours=[neighbor_name])
    downloads_store.set_verification(row.download_id, "verified")
    neighbor = _touch(staging, neighbor_name, days_old=10)
    original_list = downloads_store.list_downloads
    calls = []

    def write_during_current_owner_query(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            neighbor.write_bytes(b"new writer data")
        return original_list(*args, **kwargs)

    monkeypatch.setattr(downloads_store, "list_downloads", write_during_current_owner_query)
    report = _sweep(staging, dry_run=dry_run)
    assert len(calls) >= 2
    assert neighbor.read_bytes() == b"new writer data"
    assert report.files_deleted == 0
    assert report.planned == []


def completed(staging, *, manifest=("Pack/E02.mkv",), neighbours=("Pack/E01.mkv",)):
    row = _pinned(staging, list(manifest), neighbours=list(neighbours), age_days=10)
    downloads_store.set_verification(row.download_id, "verified")
    return downloads_store.get_download(row.download_id)


@pytest.mark.parametrize("status", ["error", "moved"])
def test_neighbor_preview_matches_apply_paths_and_bytes(staging, status):
    if status == "moved":
        row = completed(staging)
    else:
        row = _pinned(staging, ["Pack/E02.mkv"], status="error", age_days=10,
                      neighbours=["Pack/E01.mkv"])
    payload = _touch(staging, "Pack/E02.mkv", days_old=10)
    neighbour = _touch(staging, "Pack/E01.mkv", days_old=10)
    stranger = _touch(staging, "Unowned.mkv", days_old=10)

    preview = _sweep(staging, dry_run=True)
    expected = {str(neighbour.resolve())}
    if status == "error":
        expected.add(str(payload.resolve()))
    assert {path for _did, path, _size in preview.planned} == expected
    assert payload.exists() and neighbour.exists() and stranger.exists()
    assert not [entry for entry in downloads_store.history_for_download(row.download_id)
                if entry.action == "swept"]

    applied = _sweep(staging)
    assert applied.planned == preview.planned
    assert applied.files_deleted == len(expected)
    assert applied.bytes_freed == sum(size for _did, _path, size in preview.planned)
    assert applied.rows_swept == preview.rows_swept == 1
    assert payload.exists() == (status == "moved"), "completed jobs never sweep their own manifest"
    assert not neighbour.exists()
    assert stranger.exists()
    assert _sweep(staging).planned == []


def test_completed_neighbor_cleanup_retries_after_live_sibling_finishes(staging):
    completed(staging)
    sibling = _pinned(staging, ["Pack/E03.mkv"], status="downloading")
    neighbour = _touch(staging, "Pack/E01.mkv", days_old=10)
    assert _sweep(staging, dry_run=True).planned == []
    assert _sweep(staging).files_deleted == 0
    assert neighbour.exists()
    downloads_store.set_status(sibling.download_id, "moved", completed=True)
    downloads_store.set_verification(sibling.download_id, "verified")
    preview = _sweep(staging, dry_run=True)
    assert len(preview.planned) == 1
    assert _sweep(staging).planned == preview.planned
    assert not neighbour.exists()


@pytest.mark.parametrize("guard", [
    "moved_manifest", "removed_manifest", "queued_hash", "stopped_hash",
    "downloaded_hash", "unknown_writer", "recent", "partial_job",
    "removed_job", "quarantined_job",
])
def test_completed_retry_keeps_all_existing_ownership_guards(staging, guard):
    row = completed(staging)
    neighbour = _touch(staging, "Pack/E01.mkv", days_old=0 if guard == "recent" else 10)
    if guard in ("moved_manifest", "removed_manifest"):
        _make(staging, ["Pack/E01.mkv"], status="moved", write=False,
              removed_reason="removed" if guard == "removed_manifest" else None)
    elif guard.endswith("_hash"):
        _pinned(staging, ["Pack/E03.mkv"], status=guard.removesuffix("_hash"))
    elif guard == "unknown_writer":
        other = _make(staging, [], status="downloading", write=False)
        with db.connect() as conn:
            conn.execute("UPDATE downloads SET magnet = ?, files_json = NULL WHERE id = ?",
                         ("magnet:?xt=urn:btih:" + "2d" * 20, other))
    elif guard == "partial_job":
        downloads_store.set_verification(row.download_id, "partial")
    elif guard in ("removed_job", "quarantined_job"):
        downloads_store.tombstone_download(
            row.download_id, reason="quarantine:keep" if guard == "quarantined_job" else "removed")
    assert _sweep(staging, dry_run=True).planned == []
    assert _sweep(staging).files_deleted == 0
    assert neighbour.exists()


def test_completed_retry_refuses_escape_and_unreported_files(staging, tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"retain")
    completed(staging, neighbours=("../outside.mkv", str(outside)))
    unreported = _touch(staging, "Pack/not-reported.mkv", days_old=10)
    assert _sweep(staging, dry_run=True).planned == []
    assert _sweep(staging).files_deleted == 0
    assert outside.read_bytes() == b"retain" and unreported.exists()


def test_neighbor_is_planned_and_counted_once_across_completed_jobs(staging):
    completed(staging, neighbours=("Pack/E01.mkv", "Pack/E01.mkv"))
    completed(staging, manifest=("Pack/E03.mkv",))
    _touch(staging, "Pack/E01.mkv", days_old=10)
    preview = _sweep(staging, dry_run=True)
    assert len(preview.planned) == 1
    applied = _sweep(staging)
    assert applied.planned == preview.planned
    assert applied.files_deleted == 1 and applied.bytes_freed == 10


def test_daily_retry_does_not_scan_history_once_per_completed_job(staging, monkeypatch):
    completed(staging)
    _touch(staging, "Pack/E01.mkv", days_old=10)

    def repeated_history_scan(_download_id):
        raise AssertionError("daily retry must use the batch history snapshot")

    monkeypatch.setattr(downloads_store, "history_for_download", repeated_history_scan)
    assert len(_sweep(staging, dry_run=True).planned) == 1


@pytest.mark.parametrize("owner_kind", ["same_hash", "unknown_writer", "exact_owner"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_deferred_cleanup_rechecks_owners_added_after_initial_snapshot(
        staging, monkeypatch, owner_kind, dry_run):
    completed(staging, manifest=("Pack/S01/E01.mkv",), neighbours=("Pack/S01/E02.mkv",))
    neighbor = _touch(staging, "Pack/S01/E02.mkv", days_old=10)
    original = staging_sweep._neighbour_history

    def add_owner_before_candidates_return():
        if owner_kind == "same_hash":
            _pinned(staging, [], status="queued")
        elif owner_kind == "unknown_writer":
            _make(staging, [], status="downloading", write=False)
        else:
            _make(staging, ["Pack/S01/E02.mkv"], status="moved", write=False)
        return original()

    monkeypatch.setattr(staging_sweep, "_neighbour_history", add_owner_before_candidates_return)
    report = _sweep(staging, dry_run=dry_run)
    assert neighbor.exists()
    assert report.files_deleted == 0
    assert report.planned == []
