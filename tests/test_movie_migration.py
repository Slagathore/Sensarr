# =============================================================================
# Task D2 item 7 — the flat-movie Maintenance migration engine (RESOLVED
# DECISION 7). Existing media is untouched until a confirmed dry-run plan
# executes; an interrupted journal resumes safely; ambiguous movies are skipped,
# never guessed; a completed run is reversible.
# =============================================================================

import db
import movie_migration


def _clean():
    with db.connect() as conn:
        try:
            conn.execute("DELETE FROM movie_migration_journal")
        except Exception:
            pass
        conn.commit()


import pytest


@pytest.fixture(autouse=True)
def _fresh():
    _clean()
    yield
    _clean()


def _write(path, size=2000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _resolver(mapping):
    """title -> (canonical_title, year, tmdb_id) or None (ambiguous)."""
    def r(title, year):
        return mapping.get((title, year)) or mapping.get(title)
    return r


# ---------------------------------------------------------------------------
# Dry-run plan touches nothing; ambiguous skipped
# ---------------------------------------------------------------------------

def test_plan_is_dry_run_and_begin_run_moves_nothing(tmp_path):
    root = tmp_path / "Movies"
    f = _write(root / "Inception.2010.1080p.BluRay.x264-GRP.mkv")
    resolver = _resolver({("Inception", 2010): ("Inception", 2010, "27205")})

    plan, skipped = movie_migration.plan_migration([root], resolver=resolver)
    assert len(plan) == 1 and not skipped
    item = plan[0]
    assert item.new_path.endswith(
        str("Inception (2010) {tmdb-27205}") + "\\Inception (2010).mkv") \
        or item.new_path.endswith("Inception (2010) {tmdb-27205}/Inception (2010).mkv")
    assert item.has_tmdb
    # DRY RUN: nothing moved.
    assert f.exists()

    run_id = movie_migration.begin_run(plan)
    # Journal recorded, still nothing on disk changed.
    assert f.exists()
    ops = movie_migration.list_ops(run_id)
    assert len(ops) == 1 and ops[0].state == "planned"


def test_ambiguous_movie_skipped_never_guessed(tmp_path):
    root = tmp_path / "Movies"
    good = _write(root / "Arrival.2016.1080p.mkv")
    ambiguous = _write(root / "Untitled.Home.Video.mkv")
    resolver = _resolver({("Arrival", 2016): ("Arrival", 2016, "329865")})

    plan, skipped = movie_migration.plan_migration([root], resolver=resolver)
    planned_olds = {p.old_path for p in plan}
    assert str(good) in planned_olds
    assert str(ambiguous) in {str(s) for s in skipped}    # skipped, not guessed
    assert str(ambiguous) not in planned_olds


def test_no_tmdb_still_plans_untagged_folder(tmp_path):
    root = tmp_path / "Movies"
    _write(root / "Some.Film.2001.mkv")
    resolver = _resolver({("Some Film", 2001): ("Some Film", 2001, None)})
    plan, _skipped = movie_migration.plan_migration([root], resolver=resolver)
    assert len(plan) == 1
    assert not plan[0].has_tmdb
    assert "{tmdb-" not in plan[0].new_path


# ---------------------------------------------------------------------------
# Execute (confirmed) actually moves
# ---------------------------------------------------------------------------

def test_execute_run_moves_into_tagged_folder(tmp_path):
    root = tmp_path / "Movies"
    f = _write(root / "Dune.2021.2160p.BluRay.x265-GRP.mkv")
    resolver = _resolver({("Dune", 2021): ("Dune", 2021, "438631")})
    plan, _ = movie_migration.plan_migration([root], resolver=resolver)
    run_id = movie_migration.begin_run(plan)

    summary = movie_migration.execute_run(run_id)

    assert summary["moved"] == 1 and summary["failed"] == 0
    assert not f.exists()
    assert (root / "Dune (2021) {tmdb-438631}" / "Dune (2021).mkv").is_file()
    assert all(op.state == "done" for op in movie_migration.list_ops(run_id))


# ---------------------------------------------------------------------------
# Interrupted journal resumes safely
# ---------------------------------------------------------------------------

def test_interrupted_journal_resumes_safely(tmp_path):
    root = tmp_path / "Movies"
    f1 = _write(root / "A.Movie.2001.mkv")
    f2 = _write(root / "B.Movie.2002.mkv")
    resolver = _resolver({
        ("A Movie", 2001): ("A Movie", 2001, "111"),
        ("B Movie", 2002): ("B Movie", 2002, "222"),
    })
    plan, _ = movie_migration.plan_migration([root], resolver=resolver)
    run_id = movie_migration.begin_run(plan)

    # Simulate a crash AFTER op1's file physically moved but BEFORE the journal
    # marked it done: move the file, leave the journal 'planned'.
    op1 = next(o for o in movie_migration.list_ops(run_id)
               if o.old_path == str(f1))
    from pathlib import Path
    Path(op1.new_path).parent.mkdir(parents=True, exist_ok=True)
    Path(op1.old_path).rename(op1.new_path)

    # Resume: op1 is detected already-at-destination (no double move, no error),
    # op2 completes.
    summary = movie_migration.resume_run(run_id)
    assert summary["failed"] == 0
    assert Path(op1.new_path).is_file() and not f1.exists()
    assert not f2.exists()
    assert (root / "B Movie (2002) {tmdb-222}" / "B Movie (2002).mkv").is_file()
    assert all(o.state == "done" for o in movie_migration.list_ops(run_id))


def test_resume_skips_already_done_without_removing(tmp_path):
    root = tmp_path / "Movies"
    _write(root / "C.Movie.2003.mkv")
    _write(root / "D.Movie.2004.mkv")
    resolver = _resolver({
        ("C Movie", 2003): ("C Movie", 2003, "333"),
        ("D Movie", 2004): ("D Movie", 2004, "444"),
    })
    plan, _ = movie_migration.plan_migration([root], resolver=resolver)
    run_id = movie_migration.begin_run(plan)
    first = movie_migration.execute_run(run_id)
    assert first["moved"] == 2
    # A second execute is a no-op (all done) — never re-moves.
    second = movie_migration.execute_run(run_id)
    assert second["moved"] == 0 and second["already_done"] == 2


# ---------------------------------------------------------------------------
# Reversible
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Production TMDB resolver — verified or skipped, never guessed
# ---------------------------------------------------------------------------

def _tmdb_result(title, year, ext_id):
    import media_lookup
    return media_lookup.MediaResult(
        title=title, year=year, external_id=str(ext_id),
        external_url=f"https://www.themoviedb.org/movie/{ext_id}",
        media_type="movie", overview="", source="tmdb")


def test_tmdb_resolver_verifies_or_skips(monkeypatch):
    import media_lookup

    def fake_search(title, year=None, **kw):
        catalog = {
            "Inception": [_tmdb_result("Inception", 2010, 27205)],
            "The Angry Birds Movie": [
                _tmdb_result("The Angry Birds Movie 2", 2019, 480204)],
            "Solaris": [_tmdb_result("Solaris", 2002, 2069)],
            "Totally Unrelated": [_tmdb_result("Different Film", 2010, 1)],
        }
        return catalog.get(title, [])

    monkeypatch.setattr(media_lookup, "search_tmdb_movies", fake_search)

    # Verified: high similarity + agreeing year.
    assert movie_migration.tmdb_resolver("Inception", 2010) == \
        ("Inception", 2010, "27205")
    # Sequel guard: the best hit is the SEQUEL — skipped, never guessed.
    assert movie_migration.tmdb_resolver("The Angry Birds Movie", 2016) is None
    # Year contradiction (1972 file vs 2002 remake) — skipped.
    assert movie_migration.tmdb_resolver("Solaris", 1972) is None
    # Low similarity — skipped.
    assert movie_migration.tmdb_resolver("Totally Unrelated", 2010) is None
    # No results / no title — skipped.
    assert movie_migration.tmdb_resolver("Nothing Here", 2020) is None
    assert movie_migration.tmdb_resolver(None, 2020) is None


def test_tmdb_resolver_network_failure_is_skip(monkeypatch):
    import media_lookup

    def boom(title, year=None, **kw):
        raise OSError("network down")

    monkeypatch.setattr(media_lookup, "search_tmdb_movies", boom)
    assert movie_migration.tmdb_resolver("Inception", 2010) is None


def test_revert_run_restores_originals(tmp_path):
    root = tmp_path / "Movies"
    f = _write(root / "Heat.1995.1080p.mkv")
    resolver = _resolver({("Heat", 1995): ("Heat", 1995, "949")})
    plan, _ = movie_migration.plan_migration([root], resolver=resolver)
    run_id = movie_migration.begin_run(plan)
    movie_migration.execute_run(run_id)
    assert not f.exists()

    summary = movie_migration.revert_run(run_id)
    assert summary["reverted"] == 1 and summary["failed"] == 0
    assert f.exists()                                   # back where it started
    assert all(o.state == "reverted" for o in movie_migration.list_ops(run_id))
