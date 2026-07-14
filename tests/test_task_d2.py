# =============================================================================
# Task D2 (Phase 5) — per-movie folders (+ {tmdb-ID} tag), per-file movie naming
# (multipart / extras / samples), and Plex-recognizable subtitle identity naming
# (matched basename, forced/sdh, .smi, .sub/.idx pairing, duplicate-language
# collisions, optional Subs subfolder).
#
# Movie routing is exercised through the REAL plan_route + _post_process on tmp
# roots (no plan_route monkeypatch).
# =============================================================================

import hashlib
import json

import pytest

import config
import db
import downloads_store
import queue_store
import subtitles
import torrent_routing
from download_manager import DownloadManager
from subtitles import (SubtitleIdentity, parse_subtitle_identity, subtitle_stem)


def _clean():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "download_files", "request_downloads", "blocklist",
                      "needs_placement", "selection_runs", "candidate_decisions"):
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


def _movie_want(*, title, year, source="tmdb", ext="12345"):
    return {
        "schema": 1, "media_type": "movie", "identity_source": source,
        "external_id": ext, "canonical_title": title, "canonical_year": year,
        "origin_countries": [], "aliases": [], "search_alias": title,
        "season": None, "episode": None, "size_pref_mb_min": 0,
        "size_max_rate": 0, "runtime_minutes": None,
    }


def _make_movie_download(*, want, staging, files, seed=None):
    seed = seed or files[0]
    did = downloads_store.create_download(
        title=files[0].rsplit("/", 1)[-1].rsplit(".", 1)[0],
        magnet=f"magnet:?xt=urn:btih:{_hash(seed)}",
        source="tpb", media_type="movie", request_id=None,
        staging_dir=str(staging), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=True, auto_move=True,
        want_json=json.dumps(want))
    downloads_store.set_files(did, files)
    downloads_store.set_status(did, "downloaded", completed=True)
    return did


def _movie_root(monkeypatch, root):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="movie")])
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(root.parent / "staging"))


def _dm(monkeypatch):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    return dm


# ---------------------------------------------------------------------------
# Per-movie folder + {tmdb-ID} tag
# ---------------------------------------------------------------------------

def test_per_movie_folder_with_tmdb_tag(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    f = _write(staging / "Inception.2010.1080p.BluRay.x264-GRP.mkv")
    want = _movie_want(title="Inception", year=2010, source="tmdb", ext="27205")
    did = _make_movie_download(want=want, staging=staging, files=[f.name])
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    folder = movies / "Inception (2010) {tmdb-27205}"
    assert (folder / "Inception (2010).mkv").is_file()


def test_no_tmdb_id_no_fabricated_tag(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    f = _write(staging / "Some.Indie.Film.2015.1080p.WEB.x264-GRP.mkv")
    # An OMDB/imdb identity (not tmdb) -> no {tmdb-} tag may be fabricated.
    want = _movie_want(title="Some Indie Film", year=2015, source="omdb",
                       ext="tt1234567")
    did = _make_movie_download(want=want, staging=staging, files=[f.name])
    dm = _dm(monkeypatch)

    out = dm._post_process(did)

    assert "moved to" in out
    folder = movies / "Some Indie Film (2015)"
    assert folder.is_dir()
    assert (folder / "Some Indie Film (2015).mkv").is_file()   # readable stem
    # No fabricated tag anywhere.
    assert not any("{tmdb-" in p.name for p in movies.rglob("*"))
    # missing_folder_id was recorded for later metadata repair.
    hist = [h.action for h in downloads_store.list_history()
            if h.download_id == did]
    assert "missing_folder_id" in hist


def test_no_subtitle_movie_still_foldered(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    f = _write(staging / "Dune.2021.2160p.BluRay.x265-GRP.mkv")
    want = _movie_want(title="Dune", year=2021, source="tmdb", ext="438631")
    did = _make_movie_download(want=want, staging=staging, files=[f.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)
    assert (movies / "Dune (2021) {tmdb-438631}" / "Dune (2021).mkv").is_file()


# ---------------------------------------------------------------------------
# Per-file movie naming — multipart, extras, samples
# ---------------------------------------------------------------------------

def test_multipart_cd1_cd2_preserved_no_overwrite(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    cd1 = _write(staging / "Berlin.Alexanderplatz.1980.cd1.x264-GRP.avi", size=4000)
    cd2 = _write(staging / "Berlin.Alexanderplatz.1980.cd2.x264-GRP.avi", size=4100)
    want = _movie_want(title="Berlin Alexanderplatz", year=1980, source="tmdb",
                       ext="24")
    did = _make_movie_download(want=want, staging=staging,
                               files=[cd1.name, cd2.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Berlin Alexanderplatz (1980) {tmdb-24}"
    names = sorted(p.name for p in folder.glob("*.avi"))
    assert names == ["Berlin Alexanderplatz (1980) - cd1.avi",
                     "Berlin Alexanderplatz (1980) - cd2.avi"]


def test_extras_and_samples_not_renamed_to_movie(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    primary = _write(staging / "Heat.1995.1080p.BluRay.x264-GRP.mkv", size=9000)
    sample = _write(staging / "sample.mkv", size=200)
    extra = _write(staging / "Extras" / "Deleted.Scene.mkv", size=300)
    want = _movie_want(title="Heat", year=1995, source="tmdb", ext="949")
    did = _make_movie_download(
        want=want, staging=staging,
        files=[primary.name, sample.name, "Extras/Deleted.Scene.mkv"])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Heat (1995) {tmdb-949}"
    landed = sorted(p.name for p in folder.rglob("*") if p.is_file())
    assert landed == ["Heat (1995).mkv"]          # only the primary, renamed
    assert sample.exists() and extra.exists()      # extras/samples stay staged


# ---------------------------------------------------------------------------
# Subtitle identity naming (matched basename, forced/sdh, subfolder, pairing)
# ---------------------------------------------------------------------------

def test_subtitle_matched_basename_same_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_SUBFOLDER", False)
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    v = _write(staging / "Arrival.2016.1080p.BluRay.x264-GRP.mkv", size=9000)
    s = _write(staging / "Arrival.2016.1080p.BluRay.x264-GRP.en.srt", size=100)
    want = _movie_want(title="Arrival", year=2016, source="tmdb", ext="329865")
    did = _make_movie_download(want=want, staging=staging,
                               files=[v.name, s.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Arrival (2016) {tmdb-329865}"
    assert (folder / "Arrival (2016).mkv").is_file()
    assert (folder / "Arrival (2016).en.srt").is_file()   # matched basename


def test_subtitle_subfolder_option(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_SUBFOLDER", True)
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    v = _write(staging / "Sicario.2015.1080p.BluRay.x264-GRP.mkv", size=9000)
    s = _write(staging / "Sicario.2015.1080p.BluRay.x264-GRP.en.srt", size=100)
    want = _movie_want(title="Sicario", year=2015, source="tmdb", ext="273481")
    did = _make_movie_download(want=want, staging=staging, files=[v.name, s.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Sicario (2015) {tmdb-273481}"
    assert (folder / "Sicario (2015).mkv").is_file()
    assert (folder / "Subs" / "Sicario (2015).en.srt").is_file()


def test_forced_and_sdh_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_SUBFOLDER", False)
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    v = _write(staging / "Tenet.2020.1080p.mkv", size=9000)
    forced = _write(staging / "Tenet.2020.1080p.en.forced.srt", size=80)
    sdh = _write(staging / "Tenet.2020.1080p.en.sdh.srt", size=90)
    want = _movie_want(title="Tenet", year=2020, source="tmdb", ext="577922")
    did = _make_movie_download(want=want, staging=staging,
                               files=[v.name, forced.name, sdh.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Tenet (2020) {tmdb-577922}"
    names = sorted(p.name for p in folder.glob("*.srt"))
    assert names == ["Tenet (2020).en.forced.srt", "Tenet (2020).en.sdh.srt"]


def test_smi_subtitle_handled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_SUBFOLDER", False)
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    v = _write(staging / "Oldboy.2003.1080p.mkv", size=9000)
    smi = _write(staging / "Oldboy.2003.1080p.en.smi", size=100)
    want = _movie_want(title="Oldboy", year=2003, source="tmdb", ext="670")
    did = _make_movie_download(want=want, staging=staging, files=[v.name, smi.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Oldboy (2003) {tmdb-670}"
    assert (folder / "Oldboy (2003).en.smi").is_file()


def test_vobsub_pair_moves_together_lone_idx_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_SUBFOLDER", False)
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    v = _write(staging / "Akira.1988.1080p.mkv", size=9000)
    sub = _write(staging / "Akira.1988.1080p.en.sub", size=500)
    idx = _write(staging / "Akira.1988.1080p.en.idx", size=50)
    lone = _write(staging / "Akira.1988.1080p.fr.idx", size=40)  # no .sub partner
    want = _movie_want(title="Akira", year=1988, source="tmdb", ext="149")
    did = _make_movie_download(
        want=want, staging=staging,
        files=[v.name, sub.name, idx.name, lone.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Akira (1988) {tmdb-149}"
    # The pair moved together with a matched basename.
    assert (folder / "Akira (1988).en.sub").is_file()
    assert (folder / "Akira (1988).en.idx").is_file()
    # The lone .idx (broken half) stayed in staging.
    assert lone.exists()
    assert not (folder / "Akira (1988).fr.idx").exists()


def test_duplicate_language_collision_suffixed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_SUBFOLDER", False)
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    v = _write(staging / "Coco.2017.1080p.mkv", size=9000)
    # Two English subs from different sources -> the second must NOT overwrite.
    s1 = _write(staging / "Coco.2017.eng.srt", size=100)
    s2 = _write(staging / "Coco.2017.english.srt", size=111)
    want = _movie_want(title="Coco", year=2017, source="tmdb", ext="354912")
    did = _make_movie_download(want=want, staging=staging,
                               files=[v.name, s1.name, s2.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Coco (2017) {tmdb-354912}"
    srts = sorted(p.name for p in folder.glob("*.srt"))
    assert srts == ["Coco (2017).en.2.srt", "Coco (2017).en.srt"]
    # Both files survived (no overwrite).
    assert len(srts) == 2


# ---------------------------------------------------------------------------
# Collision safety (Phase 5 verifier blocker B1 / must-fix M1 regressions)
# ---------------------------------------------------------------------------

def test_two_videos_same_stem_both_survive(tmp_path, monkeypatch):
    """Two DIFFERENT payload videos resolving to the same canonical stem must
    both survive — the second diverts to a suffixed sibling, never destroying
    the first (verifier blocker B1)."""
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    v1 = _write(staging / "Twin.Film.2010.1080p.BluRay.x264-AAA.mkv", size=9000)
    v2 = _write(staging / "Twin.Film.2010.720p.WEB.x264-BBB.mkv", size=5000)
    want = _movie_want(title="Twin Film", year=2010, source="tmdb", ext="42")
    did = _make_movie_download(want=want, staging=staging,
                               files=[v1.name, v2.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Twin Film (2010) {tmdb-42}"
    landed = sorted((p.name, p.stat().st_size) for p in folder.glob("*.mkv"))
    # Both distinct files survived with their original sizes.
    assert {s for _, s in landed} == {9000, 5000}
    assert len(landed) == 2


def test_multipart_same_disc_number_never_destroys(tmp_path, monkeypatch):
    """Two files both mapping to '- cd1' keep both payloads (B1's exact shape)."""
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    a = _write(staging / "Old.Film.1990.cd1.x264-AAA.avi", size=7000)
    b = _write(staging / "Old.Film.1990.CD01.x264-BBB.avi", size=7100)
    want = _movie_want(title="Old Film", year=1990, source="tmdb", ext="77")
    did = _make_movie_download(want=want, staging=staging, files=[a.name, b.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Old Film (1990) {tmdb-77}"
    sizes = sorted(p.stat().st_size for p in folder.glob("*.avi"))
    assert sizes == [7000, 7100]                    # nothing was unlinked


def test_single_video_part_in_title_gets_plain_stem(tmp_path, monkeypatch):
    """A SINGLE video whose title contains 'Part 2' is not multipart (M1)."""
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    staging = tmp_path / "staging"
    f = _write(staging / "Mockingjay.Part.2.2015.1080p.BluRay.x264-GRP.mkv",
               size=9000)
    want = _movie_want(title="Mockingjay Part 2", year=2015, source="tmdb",
                       ext="131634")
    did = _make_movie_download(want=want, staging=staging, files=[f.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    folder = movies / "Mockingjay Part 2 (2015) {tmdb-131634}"
    names = [p.name for p in folder.glob("*.mkv")]
    assert names == ["Mockingjay Part 2 (2015).mkv"]   # no '- cd2'


def test_preexisting_foreign_smaller_file_never_overwritten(tmp_path, monkeypatch):
    """A smaller file already AT the canonical target that this download never
    planned is foreign media — it must survive, and the staged file stays."""
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    folder = movies / "Foreign (2012) {tmdb-99}"
    existing = _write(folder / "Foreign (2012).mkv", size=1000)   # smaller
    staging = tmp_path / "staging"
    staged = _write(staging / "Foreign.2012.1080p.BluRay.x264-GRP.mkv", size=9000)
    want = _movie_want(title="Foreign", year=2012, source="tmdb", ext="99")
    did = _make_movie_download(want=want, staging=staging, files=[staged.name])
    dm = _dm(monkeypatch)

    dm._post_process(did)

    assert existing.stat().st_size == 1000            # untouched
    assert staged.exists()                             # not moved
    hist = [h.action for h in downloads_store.list_history()
            if h.download_id == did]
    assert "error" in hist                             # loudly recorded


def test_own_interrupted_partial_is_still_repaired(tmp_path, monkeypatch):
    """The 2026-07-10 interrupted-move repair survives the B1 fix when the
    partial file is attributable to THIS download's own earlier pass."""
    movies = tmp_path / "Movies"
    _movie_root(monkeypatch, movies)
    folder = movies / "Repair Me (2018) {tmdb-55}"
    partial = _write(folder / "Repair Me (2018).mkv", size=500)   # partial copy
    staging = tmp_path / "staging"
    staged = _write(staging / "Repair.Me.2018.1080p.WEB.x264-GRP.mkv", size=9000)
    want = _movie_want(title="Repair Me", year=2018, source="tmdb", ext="55")
    did = _make_movie_download(want=want, staging=staging, files=[staged.name])
    # The earlier (interrupted) pass recorded its rename intent.
    downloads_store.add_history(did, "renamed",
                                before=staged.name,
                                after="Repair Me (2018).mkv")
    dm = _dm(monkeypatch)

    dm._post_process(did)

    assert partial.stat().st_size == 9000              # replaced by the full file
    assert not staged.exists()


# ---------------------------------------------------------------------------
# SubtitleIdentity parser (unit)
# ---------------------------------------------------------------------------

def test_parse_subtitle_identity():
    assert parse_subtitle_identity("Movie (2020).en.srt") == \
        SubtitleIdentity("en", False, False)
    assert parse_subtitle_identity("Movie.eng.forced.srt") == \
        SubtitleIdentity("en", True, False)
    assert parse_subtitle_identity("Movie.en.sdh.srt") == \
        SubtitleIdentity("en", False, True)
    # Unknown language -> None (Plex default track), no fabricated code.
    assert parse_subtitle_identity("Movie.klingon.srt").language is None


def test_subtitle_stem_naming():
    assert subtitle_stem("Film (2020)", SubtitleIdentity("en", False, False)) \
        == "Film (2020).en"
    assert subtitle_stem("Film (2020)", SubtitleIdentity("es", True, False)) \
        == "Film (2020).es.forced"
    assert subtitle_stem("Film (2020)", SubtitleIdentity(None, False, True)) \
        == "Film (2020).sdh"
