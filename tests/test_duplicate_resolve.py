# =============================================================================
# A finished download whose episode the library already has, in a different
# size, is a duplicate when the library copy is full length (ffprobe), not a
# "needs import" row that sits in staging for weeks (Furious S01, Lanterns
# S01E01-03, Dexter Resurrection S01E04 through 2026-10-09).
# =============================================================================

import download_manager
import video_quality


def _durations(monkeypatch, table):
    def fake(path, size):
        minutes, exact = table[str(path)]
        return minutes, exact
    monkeypatch.setattr(video_quality, "duration_minutes", fake)


def test_full_length_library_copy_means_duplicate(tmp_path, monkeypatch):
    have, got = tmp_path / "lib.mkv", tmp_path / "staged.mkv"
    have.write_bytes(b"x" * 10)
    got.write_bytes(b"x" * 30)
    _durations(monkeypatch, {str(have): (43.5, True), str(got): (44.0, True)})
    monkeypatch.setattr(download_manager, "_plays_to_the_end", lambda p, m: True)
    assert download_manager._library_copy_is_complete(have, got)


def test_truncated_copy_with_a_full_header_is_not_complete(tmp_path, monkeypatch):
    """Verifier finding: the runtime ffprobe reports comes from the container
    header, which an interrupted copy keeps. Checked on real files 2026-10-09:
    a 30% copy of a 49.7 min episode still reports 49.7 min."""
    have, got = tmp_path / "lib.mkv", tmp_path / "staged.mkv"
    have.write_bytes(b"x")
    got.write_bytes(b"x")
    _durations(monkeypatch, {str(have): (49.7, True), str(got): (49.7, True)})
    monkeypatch.setattr(download_manager, "_plays_to_the_end", lambda p, m: False)
    assert not download_manager._library_copy_is_complete(have, got)


def test_no_ffprobe_means_not_proven_to_play_to_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(video_quality, "_find_ffprobe", lambda: None)
    assert not download_manager._plays_to_the_end(tmp_path / "x.mkv", 45.0)


def test_recycle_failure_leaves_the_file(tmp_path, monkeypatch):
    import sys
    import types
    f = tmp_path / "staged.mkv"
    f.write_bytes(b"x")
    def broken(path):
        raise OSError("no recycle bin on this drive")
    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=broken))
    assert download_manager._send_to_recycle_bin(f) is False
    assert f.exists()


def test_short_library_copy_is_not_complete(tmp_path, monkeypatch):
    have, got = tmp_path / "lib.mkv", tmp_path / "staged.mkv"
    have.write_bytes(b"x")
    got.write_bytes(b"x")
    _durations(monkeypatch, {str(have): (20.0, True), str(got): (44.0, True)})
    assert not download_manager._library_copy_is_complete(have, got)


def test_unknown_runtime_keeps_the_old_refusal(tmp_path, monkeypatch):
    # No ffprobe -> duration_minutes guesses (exact=False) -> never resolve.
    have, got = tmp_path / "lib.mkv", tmp_path / "staged.mkv"
    have.write_bytes(b"x")
    got.write_bytes(b"x")
    _durations(monkeypatch, {str(have): (120.0, False), str(got): (120.0, False)})
    assert not download_manager._library_copy_is_complete(have, got)


def test_missing_file_is_not_complete(tmp_path):
    assert not download_manager._library_copy_is_complete(
        tmp_path / "gone.mkv", tmp_path / "also-gone.mkv")
