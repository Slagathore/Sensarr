"""Clean Junk detection, tracker injection, and drive-preference routing."""
from pathlib import Path

import config
import maintenance
import torrent_routing
from download_manager import add_public_trackers


def _mb(n: int) -> bytes:
    return b"\0" * (n * 1024 * 1024)


def test_find_junk_flags_samples_and_notes(tmp_path, monkeypatch):
    movie_dir = tmp_path / "Movies" / "Big Film (2020)"
    movie_dir.mkdir(parents=True)
    (movie_dir / "Big Film (2020) 1080p.mkv").write_bytes(_mb(30))  # "main" (small for test)
    (movie_dir / "big.film.2020.sample.mkv").write_bytes(_mb(2))     # sample by name
    (movie_dir / "RARBG.txt").write_bytes(b"junk")
    (movie_dir / "screens.jpg").write_bytes(b"junk")
    (movie_dir / "poster.jpg").write_bytes(b"art")                   # Plex art — keep
    (movie_dir / "Big Film.en.srt").write_bytes(b"subs")             # keep
    extras = movie_dir / "Featurettes"
    extras.mkdir()
    (extras / "making-of.txt").write_bytes(b"keep me")               # extras — keep
    (tmp_path / "Movies" / "EmptyDir").mkdir()

    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(tmp_path / "Movies")])
    monkeypatch.setattr(config, "MEDIA_LIBRARY_PATHS", [
        config.MediaLibraryPath(path=str(tmp_path / "Movies"), media_type="movie"),
    ])

    junk = maintenance.find_junk_files()
    flagged = {Path(j.path).name for j in junk}
    assert "big.film.2020.sample.mkv" in flagged
    assert "RARBG.txt" in flagged
    assert "screens.jpg" in flagged
    assert "EmptyDir" in flagged
    assert "poster.jpg" not in flagged
    assert "Big Film.en.srt" not in flagged
    assert "making-of.txt" not in flagged
    assert "Big Film (2020) 1080p.mkv" not in flagged


def test_add_public_trackers_appends_and_dedupes():
    magnet = "magnet:?xt=urn:btih:abc&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
    out = add_public_trackers(magnet)
    assert out.startswith(magnet)
    assert out.count("tracker.opentrackr.org") == 1   # not duplicated
    assert out.count("&tr=") >= 5                     # several new ones added
    # Non-magnet input passes through untouched.
    assert add_public_trackers("http://example.com/file.torrent") == "http://example.com/file.torrent"


def test_pick_root_prefers_override_drive(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    # Both dirs are on the same drive here, so exercise the exact-folder pin.
    monkeypatch.setattr(config, "DOWNLOAD_ROOT_OVERRIDE", str(b))
    assert torrent_routing.pick_root_by_free_space([str(a), str(b)]) == str(b)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT_OVERRIDE", "")
    assert torrent_routing.pick_root_by_free_space([str(a), str(b)]) in (str(a), str(b))
    assert torrent_routing.pick_root_by_free_space([]) is None


def test_ascii_preferring_title_and_movie_episode_filter():
    from download_manager import _ascii_preferring_title, _looks_like_episode_release
    # The kanji-folder incident: resolved title was native-script, user text ASCII.
    assert _ascii_preferring_title("逐玉", "Pursuit of jade") == "Pursuit of jade"
    assert _ascii_preferring_title("The Cleaning Lady", "tcl") == "The Cleaning Lady"
    assert _ascii_preferring_title(None, "something") == "something"
    # Movie grabs must reject show-marked releases.
    assert _looks_like_episode_release("The Cleaning Lady S04E09 1080p WEB")
    assert _looks_like_episode_release("Show 3x07 HDTV")
    assert not _looks_like_episode_release("Heat 1995 1080p BluRay")


def test_dandadan_style_episode_parse():
    from show_tracker import _parse_episode_from_file
    got = _parse_episode_from_file(
        "[Raze] Dandadan S2 - 11 (Dual-Audio) x265 10bit 1080p 143.8561 fps.mkv")
    assert got == (2, 11)
