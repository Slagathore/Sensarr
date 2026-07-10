"""Cam detection, low-quality scanning/sorting, and auto-grab viability filters."""
import config
from download_manager import filter_viable_results, pick_best_result
from torrent_search import TorrentResult
from video_quality import find_low_quality_movies, is_cam_release


def _result(title: str, size: int, seeders: int) -> TorrentResult:
    return TorrentResult(title=title, magnet="magnet:?xt=urn:btih:x",
                         size_bytes=size, seeders=seeders,
                         source="tpb", media_type="movie")


def test_cam_regex_hits_and_misses():
    hits = [
        "Movie.2024.HDCAM.x264", "Movie 2024 CAMRip", "Film.2023.TS.x265",
        "Film 2023 HD-TS", "Thing.2022.TELESYNC", "Thing 2022 TeleCine",
        "Old.Movie.DVDSCR", "New.Movie.2024.HQCAM",
    ]
    misses = [
        "The Cats Return (2002) 1080p",       # 'cats' must not hit 'cam'/'ts'
        "Its.A.Wonderful.Life.1946.BluRay",   # 'Its' must not hit 'ts'
        "Camp Rock (2008) 720p",              # 'Camp' must not hit 'cam'
        "Scream.1996.1080p.WEB",              # 'scr' only as a standalone token
    ]
    for name in hits:
        assert is_cam_release(name + ".mkv"), name
    for name in misses:
        assert not is_cam_release(name + ".mkv"), name


def test_filter_viable_drops_zero_size_cams_and_oversize(monkeypatch):
    monkeypatch.setattr(config, "BLOCK_CAMS", True)
    monkeypatch.setattr(config, "SIZE_MAX_MB_PER_MIN_MOVIE", 20.0)  # cap 2.4 GB per 2h
    results = [
        _result("Movie 2024 HDCAM", 900_000_000, 99),      # cam → out
        _result("Movie 2024 WEB", 0, 80),                  # size 0 → out
        _result("Movie 2024 REMUX", 40_000_000_000, 70),   # over the max → out
        _result("Movie 2024 1080p BluRay", 1_500_000_000, 30),
    ]
    viable = filter_viable_results(results, "movie")
    assert [r.title for r in viable] == ["Movie 2024 1080p BluRay"]
    # And the picker returns None when nothing survives.
    assert pick_best_result([], "movie") is None


def test_runtime_anchor_changes_target_and_cap(monkeypatch):
    """Real runtimes reshape the size math: a 45-min drama episode and a
    24-min anime episode have different targets/caps under one MB/min pref."""
    monkeypatch.setattr(config, "SIZE_PREF_MB_PER_MIN_TV", 10.0)
    monkeypatch.setattr(config, "SIZE_MAX_MB_PER_MIN_TV", 20.0)
    mb = 1024 * 1024
    results = [
        _result("Show S01E01 300MB", 300 * mb, 50),
        _result("Show S01E01 450MB", 450 * mb, 40),
        _result("Show S01E01 700MB", 700 * mb, 30),
    ]
    # Flat 24-min fallback: target 240 MB → the 300 MB result is closest.
    flat = pick_best_result(results, "tv")
    assert flat is not None and flat.size_bytes == 300 * mb
    # A known 45-min runtime: target 450 MB → the 450 MB result wins.
    anchored = pick_best_result(results, "tv", minutes=45)
    assert anchored is not None and anchored.size_bytes == 450 * mb

    # Max cap scales with runtime too: 20 MB/min × 24 min = 480 MB cap.
    viable = filter_viable_results(results, "tv", minutes=24)
    assert [r.size_bytes for r in viable] == [300 * mb, 450 * mb]
    # …while a 45-min episode keeps the 700 MB result in play (cap 900 MB).
    viable = filter_viable_results(results, "tv", minutes=45)
    assert len(viable) == 3


def test_low_quality_scan_sorts_cams_first_and_flags_redundant(tmp_path, monkeypatch):
    # No ffprobe → rates assume 120 min. 600 MB → 5.0, 120 MB → 1.0, 2400 MB → 20.
    import video_quality
    monkeypatch.setattr(video_quality, "_ffprobe_minutes", lambda _p: None)
    monkeypatch.setattr(config, "LOW_QUALITY_MB_PER_MIN", 5.0)

    mb = 1024 * 1024
    movies = [
        ("/lib/Good Movie (2020) 1080p.mkv", "Good Movie (2020) 1080p.mkv", 2400 * mb),
        ("/lib/Good Movie (2020) HDCAM.mkv", "Good Movie (2020) HDCAM.mkv", 300 * mb),
        ("/lib/Tiny Flick (2019).mkv", "Tiny Flick (2019).mkv", 120 * mb),
        ("/lib/Other Cam TS.mkv", "Other Cam TS.mkv", 500 * mb),
    ]
    flagged = find_low_quality_movies(movies)
    names = [m.name for m in flagged]
    assert "Good Movie (2020) 1080p.mkv" not in names          # fine as-is
    # Cams first (lowest rate first), then keyword-free low-bitrate files.
    assert names[0] == "Good Movie (2020) HDCAM.mkv"           # 2.5 MB/min cam
    assert names[1] == "Other Cam TS.mkv"                      # 4.2 MB/min cam
    assert names[2] == "Tiny Flick (2019).mkv"                 # 1.0 MB/min no keyword
    cam_copy = flagged[0]
    assert cam_copy.redundant_with == "/lib/Good Movie (2020) 1080p.mkv"
    assert flagged[2].redundant_with is None
