import calendar
import os
from datetime import datetime, timedelta

import pytest

import db
import downloads_store
import staging_sweep

NOW = datetime(2026, 10, 9, 12, 0, 0)


@pytest.fixture(autouse=True)
def _isolated_rows():
    """Every test starts with no download rows: the sweep reads ALL rows, so
    one test's leftovers would otherwise protect or expose another's files."""
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("downloads", "download_history", "download_files"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()
    yield


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _make(staging, rel_files, *, status="error", progress=0.0, age_days=10.0,
          removed_reason=None, write=True, size=100):
    did = downloads_store.create_download(
        title="t", magnet="magnet:?xt=urn:btih:abc", source=None,
        media_type="movie", request_id=None, staging_dir=str(staging),
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False)
    downloads_store.set_files(did, rel_files)
    with db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET status=?, progress=?, created_at=?,"
            " completed_at=NULL, removed_reason=?, removed_at=? WHERE id=?",
            (status, progress, _ts(age_days), removed_reason,
             _ts(age_days) if removed_reason else None, did))
        conn.commit()
    if write:
        stamp = calendar.timegm((NOW - timedelta(days=age_days)).timetuple())
        for rel in rel_files:
            p = staging / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)
            # The file was last written when the row failed, not just now.
            os.utime(p, (stamp, stamp))
    return did


@pytest.fixture
def staging(tmp_path):
    d = tmp_path / "staging"
    d.mkdir()
    return d


def _sweep(staging, **kw):
    return staging_sweep.sweep_stalled_leftovers(now=NOW, staging_root=staging, **kw)


def test_old_zero_progress_swept_and_empty_dir_removed(staging):
    _make(staging, ["Show/ep1.mkv"])
    rep = _sweep(staging)
    assert not (staging / "Show").exists()
    assert staging.exists()
    assert rep.files_deleted == 1 and rep.rows_swept == 1
    assert rep.bytes_freed == 100


def test_young_row_kept(staging):
    _make(staging, ["a.mkv"], age_days=0.5)
    rep = _sweep(staging)
    assert (staging / "a.mkv").exists()
    assert rep.files_deleted == 0


def test_progress_row_kept_at_10_days_swept_at_40(staging):
    _make(staging, ["a.mkv"], progress=0.6, age_days=10)
    rep = _sweep(staging)
    assert (staging / "a.mkv").exists()
    assert rep.kept_resumable == 1
    rep = staging_sweep.sweep_stalled_leftovers(
        now=NOW + timedelta(days=30), staging_root=staging)
    assert not (staging / "a.mkv").exists()
    assert rep.files_deleted == 1


def test_quarantine_row_untouched(staging):
    _make(staging, ["q.mkv"], removed_reason="quarantine:verify failed")
    _sweep(staging)
    assert (staging / "q.mkv").exists()


def test_tombstoned_other_reason_is_swept(staging):
    _make(staging, ["t.mkv"], removed_reason="user removed")
    _sweep(staging)
    assert not (staging / "t.mkv").exists()


def test_shared_with_live_row_kept(staging):
    _make(staging, ["shared.mkv"])
    _make(staging, ["Shared.MKV"], status="downloading", progress=0.1, write=False)
    rep = _sweep(staging)
    assert (staging / "shared.mkv").exists()
    assert rep.skipped_shared == 1 and rep.files_deleted == 0


def test_shared_with_kept_error_row_kept(staging):
    _make(staging, ["s.mkv"], progress=0.0, age_days=10)
    _make(staging, ["s.mkv"], progress=0.9, age_days=5, write=False)
    rep = _sweep(staging)
    assert (staging / "s.mkv").exists()
    assert rep.skipped_shared == 1


def test_escaping_path_skipped(staging, tmp_path):
    outside = tmp_path / "x"
    outside.write_bytes(b"keep")
    _make(staging, ["../x"], write=False)
    rep = _sweep(staging)
    assert outside.exists()
    assert rep.skipped_outside_root >= 1 and rep.files_deleted == 0


def test_dry_run_deletes_nothing_but_plans(staging):
    did = _make(staging, ["d/a.mkv"])
    rep = _sweep(staging, dry_run=True)
    assert (staging / "d" / "a.mkv").exists()
    assert rep.dry_run and rep.files_deleted == 0
    assert rep.planned == [(did, str((staging / "d" / "a.mkv").resolve()), 100)]
    assert not [h for h in downloads_store.history_for_download(did) if h.action == "swept"]


def test_history_written(staging):
    did = _make(staging, ["a.mkv"])
    _sweep(staging)
    swept = [h for h in downloads_store.history_for_download(did) if h.action == "swept"]
    assert len(swept) == 1
    assert swept[0].after_value == "staging leftovers removed: 1 file(s), 0 MB"


def test_part_file_removed(staging):
    _make(staging, ["a.mkv"])
    part = staging / "a.mkv.part"
    part.write_bytes(b"p")
    old = calendar.timegm((NOW - timedelta(days=10)).timetuple())
    os.utime(part, (old, old))
    rep = _sweep(staging)
    assert not (staging / "a.mkv.part").exists()
    assert rep.files_deleted == 2


def test_second_run_is_noop(staging):
    did = _make(staging, ["a.mkv"])
    _sweep(staging)
    rep = _sweep(staging)
    assert rep.files_deleted == 0 and rep.rows_swept == 0
    swept = [h for h in downloads_store.history_for_download(did) if h.action == "swept"]
    assert len(swept) == 1


def test_manager_schedules_the_sweep_daily(monkeypatch):
    """The queue monitor runs the sweep ten minutes after launch, then daily."""
    import config
    import download_manager
    import staging_sweep
    calls = []
    monkeypatch.setattr(staging_sweep, "sweep_stalled_leftovers",
                        lambda **k: calls.append(k))
    monkeypatch.setattr(config, "STAGING_SWEEP_ENABLED", True)
    monkeypatch.setattr(download_manager.DownloadManager,
                        "_QUEUE_MONITOR_AUTOSTART", False)
    monkeypatch.setattr(download_manager.DownloadManager,
                        "_recover_previous_session", lambda self: None)
    dm = download_manager.DownloadManager()
    dm._maybe_sweep_staging(now=1000.0)          # schedules, does not run
    dm._maybe_sweep_staging(now=1000.0 + 599)
    assert calls == []
    dm._maybe_sweep_staging(now=1000.0 + 600)
    assert len(calls) == 1
    dm._maybe_sweep_staging(now=1000.0 + 600 + 3600)
    assert len(calls) == 1
    dm._maybe_sweep_staging(now=1000.0 + 600 + 24 * 3600)
    assert len(calls) == 2
    monkeypatch.setattr(config, "STAGING_SWEEP_ENABLED", False)
    dm._maybe_sweep_staging(now=1000.0 + 600 + 48 * 3600)
    assert len(calls) == 2


def _set_magnet(did, ih):
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET magnet = ? WHERE id = ?",
                     (f"magnet:?xt=urn:btih:{ih}", did))
        conn.commit()


def test_same_torrent_live_without_a_file_list_protects_the_old_row(staging):
    """Verifier finding: a Real-Debrid/qBittorrent retry of the same torrent
    has no files_json until it finishes, yet writes the same paths. The old
    failed row must not sweep them out from under it."""
    ih = "f" * 40
    old = _make(staging, ["X/E01.mkv", "X/E02.mkv"], progress=0.2)
    _set_magnet(old, ih)
    live = _make(staging, [], status="downloading", age_days=0, write=False)
    _set_magnet(live, ih)
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET files_json = NULL WHERE id = ?", (live,))
        conn.commit()
    rep = _sweep(staging)
    assert (staging / "X/E01.mkv").exists() and (staging / "X/E02.mkv").exists()
    assert rep.files_deleted == 0 and rep.skipped_shared >= 1


def test_recently_written_file_is_left_alone(staging):
    _make(staging, ["busy.mkv"], age_days=10)
    fresh = calendar.timegm((NOW - timedelta(hours=2)).timetuple())
    os.utime(staging / "busy.mkv", (fresh, fresh))
    rep = _sweep(staging)
    assert (staging / "busy.mkv").exists()
    assert rep.skipped_recent == 1 and rep.files_deleted == 0


# --- pack neighbours ----------------------------------------------------------

import json as _json

PACK_HASH = "1c" * 20


def _pinned(staging, manifest, *, status="moved", ih=PACK_HASH, age_days=0.0,
            neighbours=()):
    did = _make(staging, manifest, status=status, age_days=age_days, write=False)
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET magnet = ?, want_json = ? WHERE id = ?",
                     (f"magnet:?xt=urn:btih:{ih}",
                      _json.dumps({"file_idx": 87, "file_name": "E12.mkv"}), did))
        conn.commit()
    if neighbours:
        downloads_store.add_history(did, "neighbours", before=None,
                                    after=_json.dumps(list(neighbours)))
    return downloads_store.get_download(did)


def _touch(staging, rel, *, days_old=0.0):
    p = staging / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 10)
    if days_old:
        stamp = calendar.timegm((NOW - timedelta(days=days_old)).timetuple())
        os.utime(p, (stamp, stamp))
    return p


def test_only_the_recorded_neighbours_are_removed(staging):
    row = _pinned(staging, ["Pack/S06/E12.mkv"],
                  neighbours=["Pack/S06/E11.mkv", "Pack/S07/E01.mkv"])
    _touch(staging, "Pack/S06/E11.mkv")
    _touch(staging, "Pack/S07/E01.mkv")
    stranger = _touch(staging, "Pack/S06/notes.txt")   # nobody reported it
    removed = staging_sweep.remove_pack_neighbours(row, staging_root=staging)
    assert removed == 2
    assert stranger.exists()
    assert not (staging / "Pack/S07").exists()        # emptied dir removed


def test_neighbours_kept_while_the_same_torrent_is_live(staging):
    row = _pinned(staging, ["Pack/S06/E12.mkv"], neighbours=["Pack/S06/E11.mkv"])
    _pinned(staging, ["Pack/S06/E10.mkv"], status="downloading")
    _touch(staging, "Pack/S06/E11.mkv")
    assert staging_sweep.remove_pack_neighbours(row, staging_root=staging) == 0
    assert (staging / "Pack/S06/E11.mkv").exists()


def test_a_live_download_with_no_file_list_yet_stops_the_cleanup(staging):
    """Verifier N1: a Real-Debrid/qBittorrent download of a DIFFERENT torrent
    with the same folder name has not named its files yet; its E11 must not
    be taken for our neighbour."""
    row = _pinned(staging, ["Show.S01/E12.mkv"], neighbours=["Show.S01/E11.mkv"])
    other = _make(staging, [], status="downloading", write=False)
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET files_json = NULL, magnet = ?"
                     " WHERE id = ?", ("magnet:?xt=urn:btih:" + "2d" * 20, other))
        conn.commit()
    _touch(staging, "Show.S01/E11.mkv")
    assert staging_sweep.remove_pack_neighbours(row, staging_root=staging) == 0
    assert (staging / "Show.S01/E11.mkv").exists()


def test_a_file_another_row_owns_is_kept(staging):
    row = _pinned(staging, ["Pack/S06/E12.mkv"],
                  neighbours=["Pack/S06/E11.mkv", "Pack/S06/E13.mkv"])
    _make(staging, ["Pack/S06/E11.mkv"], status="moved", write=False)
    _touch(staging, "Pack/S06/E11.mkv")
    _touch(staging, "Pack/S06/E13.mkv")
    assert staging_sweep.remove_pack_neighbours(row, staging_root=staging) == 1
    assert (staging / "Pack/S06/E11.mkv").exists()
    assert not (staging / "Pack/S06/E13.mkv").exists()


def test_a_neighbour_path_outside_the_root_is_refused(staging, tmp_path):
    outside = tmp_path / "precious.mkv"
    outside.write_bytes(b"keep")
    row = _pinned(staging, ["Pack/S06/E12.mkv"],
                  neighbours=["../precious.mkv", str(outside)])
    assert staging_sweep.remove_pack_neighbours(row, staging_root=staging) == 0
    assert outside.exists()


def test_unpinned_downloads_are_left_alone(staging):
    plain = downloads_store.get_download(
        _make(staging, ["Pack/S06/E12.mkv"], status="moved", write=False))
    downloads_store.add_history(plain.download_id, "neighbours", before=None,
                                after=_json.dumps(["Pack/S06/E11.mkv"]))
    _touch(staging, "Pack/S06/E11.mkv")
    assert staging_sweep.remove_pack_neighbours(plain, staging_root=staging) == 0
    assert (staging / "Pack/S06/E11.mkv").exists()


def test_sweep_clears_old_neighbours_of_a_failed_pinned_row(staging):
    _pinned(staging, ["Pack/S06/E12.mkv"], status="error", age_days=10,
            neighbours=["Pack/S06/E11.mkv", "Pack/S06/E13.mkv"])
    _touch(staging, "Pack/S06/E12.mkv", days_old=10)
    _touch(staging, "Pack/S06/E11.mkv", days_old=10)
    _touch(staging, "Pack/S06/E13.mkv")              # written just now: kept
    rep = _sweep(staging)
    assert not (staging / "Pack/S06/E12.mkv").exists()
    assert not (staging / "Pack/S06/E11.mkv").exists()
    assert (staging / "Pack/S06/E13.mkv").exists()
    assert rep.files_deleted == 2


def test_a_finished_sibling_does_not_shield_a_failed_one_forever(staging):
    """Verifier N5: E05 of a pack placed, E06 of the same pack failed. The
    finished row no longer protects the failed row's partials by hash."""
    _pinned(staging, ["Pack/S01/E05.mkv"], status="moved")
    _pinned(staging, ["Pack/S01/E06.mkv"], status="error", age_days=10)
    _touch(staging, "Pack/S01/E06.mkv", days_old=10)
    rep = _sweep(staging)
    assert not (staging / "Pack/S01/E06.mkv").exists()
    assert rep.files_deleted == 1


def test_queued_or_finished_rows_without_a_file_list_do_not_block_cleanup(staging):
    """Verifier N11: only a RUNNING download can be writing unnamed files."""
    row = _pinned(staging, ["Pack/S06/E12.mkv"], neighbours=["Pack/S06/E11.mkv"])
    for status in ("queued", "downloaded"):
        other = _make(staging, [], status=status, write=False)
        with db.connect() as conn:
            conn.execute("UPDATE downloads SET files_json = NULL, magnet = ?"
                         " WHERE id = ?", (f"magnet:?xt=urn:btih:{status[0] * 40}", other))
            conn.commit()
    _touch(staging, "Pack/S06/E11.mkv")
    assert staging_sweep.remove_pack_neighbours(row, staging_root=staging) == 1
