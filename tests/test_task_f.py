# =============================================================================
# Task F (Phase 6) — persistent CAM/quality labels.
#
# downloads.quality_label is written at grab time from the RTN parse; every
# VERIFIED move writes media_quality keyed by the qualified identity; renames
# route their file_path update through the ONE central helper; a replacement
# flips the label and appends media_quality_history; the unresolved manual
# replacement shape is marked source='manual-unresolved'; and the low-quality
# scan reads media_quality FIRST, name regex second.
# =============================================================================

import hashlib
import json

import pytest

import config
import db
import download_manager
import downloads_store
import maintenance
import media_quality
import queue_store
import shows_store
import torrent_select
from download_manager import DownloadManager, identity_from_movie_path
from torrent_search import CollectedPool, TorrentResult

MB = 1024 * 1024


def _clean():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    media_quality.initialize_media_quality_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "selection_runs", "candidate_decisions", "failed_grabs",
                      "grab_deferrals", "blocklist", "download_files",
                      "request_downloads", "needs_placement", "episodes",
                      "tracked_shows", "show_folders", "season_targets",
                      "media_quality", "media_quality_history"):
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


def _write(path, size=4000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _dm(monkeypatch):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    return dm


def _movie_root(monkeypatch, root):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="movie")])
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR",
                        str(root.parent / "staging"))


def _movie_want(*, title, year, source="tmdb", ext="27205"):
    return {
        "schema": 1, "media_type": "movie", "identity_source": source,
        "external_id": ext, "canonical_title": title, "canonical_year": year,
        "origin_countries": [], "aliases": [], "search_alias": title,
        "season": None, "episode": None, "size_pref_mb_min": 0,
        "size_max_rate": 0, "runtime_minutes": None,
    }


def _make_movie_download(*, want, staging, files, title=None, seed=None,
                         quality_label=None, replace_path=None):
    seed = seed or files[0]
    did = downloads_store.create_download(
        title=title or files[0].rsplit("/", 1)[-1].rsplit(".", 1)[0],
        magnet=f"magnet:?xt=urn:btih:{_hash(seed)}",
        source="tpb", media_type="movie", request_id=None,
        staging_dir=str(staging), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=True, auto_move=True,
        want_json=json.dumps(want), quality_label=quality_label,
        replace_path=replace_path)
    downloads_store.set_files(did, files)
    downloads_store.set_status(did, "downloaded", completed=True)
    return did


# ---------------------------------------------------------------------------
# Label derivation (torrent_select)
# ---------------------------------------------------------------------------

def test_parse_quality_label_shapes():
    assert torrent_select.parse_quality_label(
        "Inception.2010.1080p.BluRay.x264-GRP") == "bluray-1080p"
    assert torrent_select.parse_quality_label(
        "Longlegs.2024.CAM.x264-YIFY") == "cam"
    assert torrent_select.parse_quality_label(
        "Movie.2019.TELESYNC.XViD") == "telesync"
    assert torrent_select.parse_quality_label(
        "Show.S02E03.720p.WEBRip.h264-GRP") == "webrip-720p"
    assert torrent_select.parse_quality_label("") is None


def test_score_breakdown_carries_quality_label():
    from media_identity import MediaIdentity
    want = torrent_select.SelectWant(
        identity=MediaIdentity(media_type="movie",
                               canonical_title="Inception",
                               canonical_year=2010))
    cand = torrent_select.Candidate(
        title="Inception.2010.1080p.BluRay.x264-GRP",
        infohash=_hash("inc"), size_bytes=1400 * MB, seeders=40)
    decision = torrent_select.select_torrent([cand], want)
    assert decision.chosen
    assert decision.scores[0].quality_label == "bluray-1080p"


def test_grab_writes_quality_label_on_download_row(monkeypatch, tmp_path):
    _movie_root(monkeypatch, tmp_path / "Movies")
    dm = _dm(monkeypatch)
    ih = _hash("grablabel")
    res = TorrentResult(
        title="Heat.1995.2160p.WEB-DL.x265-GRP",
        magnet=f"magnet:?xt=urn:btih:{ih}", size_bytes=4000 * MB,
        seeders=12, source="tpb", media_type="movie")
    did = dm.grab(res, auto_rename=False, auto_move=False)
    row = downloads_store.get_download(did)
    assert row.quality_label == "web-dl-2160p"


# ---------------------------------------------------------------------------
# Verified move writes media_quality (identity-keyed)
# ---------------------------------------------------------------------------

def test_verified_movie_move_writes_identity_keyed_quality(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    f = _write(staging / "Inception.2010.1080p.BluRay.x264-GRP.mkv")
    want = _movie_want(title="Inception", year=2010, ext="27205")
    did = _make_movie_download(want=want, staging=staging, files=[f.name],
                               quality_label="bluray-1080p")
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    row = media_quality.get_quality(
        media_quality.movie_identity_key("tmdb", "27205"))
    assert row is not None
    assert row.quality_label == "bluray-1080p"
    assert row.source == "parsed"
    final = movies / "Inception (2010) {tmdb-27205}" / "Inception (2010).mkv"
    assert row.file_path == str(final)
    # First label = one history append (None -> bluray-1080p).
    hist = media_quality.list_history(row.identity_key)
    assert len(hist) == 1
    assert (hist[0].old_label, hist[0].new_label) == (None, "bluray-1080p")
    assert hist[0].cause == "verified_move"


def test_verified_episode_pack_writes_per_episode_quality(tmp_path, monkeypatch):
    tv_root = tmp_path / "TV"
    show_dir = tv_root / "My Show"
    (show_dir / "Season 01").mkdir(parents=True)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(tv_root), media_type="tv")])
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR",
                        str(tmp_path / "staging"))
    show_id = shows_store.upsert_show(title="My Show", media_type="tv",
                                      source="tvdb", external_id="100")
    shows_store.add_show_folder(show_id, str(show_dir))
    staging = tmp_path / "staging"
    e1 = _write(staging / "My.Show.S02E01.720p.WEBRip.h264-GRP.mkv")
    e2 = _write(staging / "My.Show.S02E02.720p.WEBRip.h264-GRP.mkv")
    want = {
        "schema": 1, "media_type": "tv", "identity_source": "tvdb",
        "external_id": "100", "canonical_title": "My Show",
        "canonical_year": None, "origin_countries": [], "aliases": [],
        "search_alias": "My Show", "season": 2, "episode": None,
        "size_pref_mb_min": 0, "size_max_rate": 0, "runtime_minutes": None,
    }
    did = downloads_store.create_download(
        title="My.Show.S02.720p.WEBRip.h264-GRP",
        magnet=f"magnet:?xt=urn:btih:{_hash('pack')}",
        source="tpb", media_type="tv", request_id=None,
        staging_dir=str(staging), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=True, auto_move=True,
        show_id=show_id, season=2, episode=None,
        want_json=json.dumps(want), quality_label="webrip-720p")
    downloads_store.set_files(did, [e1.name, e2.name])
    downloads_store.set_status(did, "downloaded", completed=True)
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    for ep in (1, 2):
        row = media_quality.get_quality(
            media_quality.episode_identity_key(show_id, 2, ep))
        assert row is not None, f"episode {ep} has no quality row"
        assert row.quality_label == "webrip-720p"
        assert row.show_id == show_id and row.season == 2 and row.episode == ep
        assert row.file_path and row.file_path.endswith(f"S02E0{ep}.mkv")


# ---------------------------------------------------------------------------
# The label survives a rename through the ONE central helper
# ---------------------------------------------------------------------------

def test_label_survives_sanitize_rename_via_central_helper(tmp_path):
    f = _write(tmp_path / "Inception.2010.1080p.BluRay.x264-GRP.mkv")
    key = media_quality.movie_identity_key("tmdb", "27205")
    media_quality.record_quality(
        key, quality_label="bluray-1080p", file_path=str(f),
        media_type="movie", identity_source="tmdb", external_id="27205")

    new = tmp_path / "Inception (2010).mkv"
    pairs = [maintenance.SanitizePair(original=str(f), sanitized=str(new),
                                      size_bytes=4000)]
    errors = maintenance.apply_sanitization(pairs)

    assert errors == [] and new.is_file()
    row = media_quality.get_quality(key)
    assert row.quality_label == "bluray-1080p"     # the label rode the identity
    assert row.file_path == str(new)               # the pointer followed the file
    # A rename is not a quality change: no history row was appended for it.
    assert len(media_quality.list_history(key)) == 1


def test_update_file_path_rekeys_path_keyed_rows(tmp_path):
    f = _write(tmp_path / "Unknown.Movie.2015.mkv")
    key = media_quality.path_identity_key(str(f))
    media_quality.record_quality(
        key, quality_label="cam", file_path=str(f), media_type="movie",
        source=media_quality.SOURCE_MANUAL_UNRESOLVED)

    new = tmp_path / "Unknown Movie (2015).mkv"
    updated = media_quality.update_file_path(str(f), str(new))

    assert updated == 1
    assert media_quality.get_quality(key) is None  # old path key retired
    row = media_quality.get_quality(media_quality.path_identity_key(str(new)))
    assert row is not None and row.quality_label == "cam"
    assert row.source == "manual-unresolved"


# ---------------------------------------------------------------------------
# Replacement flips the label + appends history
# ---------------------------------------------------------------------------

def test_replacement_flips_label_and_appends_history(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    old = _write(movies / "Longlegs (2024) {tmdb-1001}" / "Longlegs (2024).mkv",
                 size=2000)
    key = media_quality.movie_identity_key("tmdb", "1001")
    media_quality.record_quality(
        key, quality_label="cam", file_path=str(old), media_type="movie",
        identity_source="tmdb", external_id="1001", cause="verified_move")

    staging = tmp_path / "staging"
    f = _write(staging / "Longlegs.2024.1080p.BluRay.x264-GRP.mkv", size=9000)
    want = _movie_want(title="Longlegs", year=2024, ext="1001")
    did = _make_movie_download(want=want, staging=staging, files=[f.name],
                               quality_label="bluray-1080p",
                               replace_path=str(old))
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    assert not old.exists()                        # old cam copy retired
    row = media_quality.get_quality(key)
    assert row.quality_label == "bluray-1080p"     # label flipped
    hist = media_quality.list_history(key)
    assert [(h.old_label, h.new_label, h.cause) for h in hist] == [
        (None, "cam", "verified_move"),
        ("cam", "bluray-1080p", "replacement"),
    ]


def test_unresolved_manual_replacement_marked_manual_unresolved(
        tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    old = _write(movies / "Mystery Film.mkv", size=2000)   # flat, no tmdb tag
    staging = tmp_path / "staging"
    f = _write(staging / "Mystery.Film.2012.1080p.WEBRip.x264-GRP.mkv",
               size=9000)
    # The unresolved replacement shape: want carries a title but NO qualified
    # identity (identity_source/external_id absent).
    want = _movie_want(title="Mystery Film", year=2012, source=None, ext=None)
    did = _make_movie_download(want=want, staging=staging, files=[f.name],
                               quality_label="webrip-1080p",
                               replace_path=str(old))
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    # No identity-keyed row was claimed anywhere.
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT identity_key, source, quality_label FROM media_quality"
        ).fetchall()
    assert len(rows) == 1
    ikey, source, label = rows[0]
    assert ikey.startswith("path:")
    assert source == "manual-unresolved"
    assert label == "webrip-1080p"


# ---------------------------------------------------------------------------
# Identity resolution for the replacement API (Task F item 4)
# ---------------------------------------------------------------------------

def test_identity_from_movie_path_reads_folder_tag(tmp_path):
    # Build the paths natively — a Windows backslash literal doesn't split on
    # POSIX, and the function parses host-native library paths.
    tagged = (tmp_path / "Movies" / "Inception (2010) {tmdb-27205}"
              / "Inception (2010).mkv")
    ident = identity_from_movie_path(str(tagged))
    assert ident is not None
    assert (ident.identity_source, ident.external_id) == ("tmdb", "27205")
    assert ident.canonical_title == "Inception"
    assert ident.canonical_year == 2010
    flat = tmp_path / "Movies" / "Flat.Movie.2015.mkv"
    assert identity_from_movie_path(str(flat)) is None


def test_replace_low_quality_movie_resolves_identity_from_folder(
        tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    old = _write(movies / "Heat (1995) {tmdb-949}" / "Heat (1995).mkv",
                 size=2000)
    ih = _hash("heat-blu")
    good = TorrentResult(
        title="Heat.1995.1080p.BluRay.x264-GRP",
        magnet=f"magnet:?xt=urn:btih:{ih}", size_bytes=2200 * MB,
        seeders=80, source="tpb", media_type="movie")
    # A sequel-shaped release that the resolved identity's gates must reject.
    bad = TorrentResult(
        title="Heat.2.2035.1080p.BluRay.x264-GRP",
        magnet=f"magnet:?xt=urn:btih:{_hash('heat2')}", size_bytes=2300 * MB,
        seeders=500, source="tpb", media_type="movie")
    pool = CollectedPool(results=(bad, good),
                         pool_stats={"per_source": {"tpb": 2}})
    monkeypatch.setattr(download_manager, "search_collect",
                        lambda *a, **k: pool)
    monkeypatch.setattr(config, "QBITTORRENT_ENABLED", False)
    dm = _dm(monkeypatch)

    did = dm.replace_low_quality_movie("Heat 1995", str(old))

    assert did is not None
    row = downloads_store.get_download(did)
    assert "Heat.1995" in row.title                # the sequel lost
    assert row.quality_label == "bluray-1080p"
    want = downloads_store.get_want(did)
    assert want["identity_source"] == "tmdb"       # resolved from the folder tag
    assert want["external_id"] == "949"
    assert want["canonical_title"] == "Heat"
    assert want["canonical_year"] == 1995


# ---------------------------------------------------------------------------
# find_low_quality_movies reads media_quality FIRST
# ---------------------------------------------------------------------------

def _no_probe(monkeypatch):
    import video_quality
    monkeypatch.setattr(video_quality, "_ffprobe_minutes", lambda p: None)


def test_low_quality_scan_trusts_recorded_labels(tmp_path, monkeypatch):
    import video_quality
    _no_probe(monkeypatch)
    # Clean NAME but the database KNOWS it is a cam (sanitize scrubbed the
    # marker after download) -> flagged, with the label as the marker.
    hidden_cam = _write(tmp_path / "Nice Movie (2024).mkv")
    media_quality.record_quality(
        media_quality.movie_identity_key("tmdb", "7"), quality_label="cam",
        file_path=str(hidden_cam), media_type="movie",
        identity_source="tmdb", external_id="7")
    # Cam-looking NAME but the database knows it is a bluray -> NOT flagged.
    false_pos = _write(tmp_path / "Camp.Movie.TS.2020.mkv")
    media_quality.record_quality(
        media_quality.movie_identity_key("tmdb", "8"),
        quality_label="bluray-1080p", file_path=str(false_pos),
        media_type="movie", identity_source="tmdb", external_id="8")
    # No recorded label -> the regex fallback still catches the name.
    plain_cam = _write(tmp_path / "Other.Movie.HDCAM.2021.mkv")

    files = [(str(p), p.name, 500 * MB)
             for p in (hidden_cam, false_pos, plain_cam)]
    flagged = video_quality.find_low_quality_movies(
        files, threshold_mb_min=0.01)

    by_path = {m.path: m for m in flagged}
    assert str(hidden_cam) in by_path
    assert by_path[str(hidden_cam)].cam_hit == "CAM"
    assert str(false_pos) not in by_path           # DB label beats the name
    assert str(plain_cam) in by_path               # regex fallback intact


def test_label_says_cam_verdicts():
    from video_quality import label_says_cam
    assert label_says_cam("cam") is True
    assert label_says_cam("telesync") is True
    assert label_says_cam("scr-1080p") is True
    assert label_says_cam("bluray-1080p") is False
    assert label_says_cam("web-dl-2160p") is False
    assert label_says_cam("webrip-720p") is False
    assert label_says_cam("1080p") is None         # resolution alone decides nothing
    assert label_says_cam(None) is None
    assert label_says_cam("") is None
