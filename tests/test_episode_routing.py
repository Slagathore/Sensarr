"""plan_for_episode: deterministic routing for tracked episodes."""
import config
import show_tracker
import shows_store


def _make_show(tmp_path, folders, title="Loop Show", ext_id="tt-loop"):
    show_id = shows_store.upsert_show(
        title=title, media_type="tv", source="tvdb", external_id=ext_id,
    )
    for f in folders:
        shows_store.add_show_folder(show_id, str(f))
    return shows_store.get_show(show_id)


def test_season_target_rule_wins(tmp_path):
    show_dir = tmp_path / "Loop Show"
    (show_dir / "Season 01").mkdir(parents=True)
    target = tmp_path / "other_drive" / "Loop Show S2"
    target.mkdir(parents=True)

    show = _make_show(tmp_path, [show_dir], ext_id="tt-loop-a")
    shows_store.set_season_target(show.show_id, 2, str(target))

    plan = show_tracker.plan_for_episode(show, 2, 3)
    assert plan.confident
    assert plan.dest_dir == str(target)          # rule wins over any folder logic
    assert plan.new_filename == "Loop Show - S02E03"

    # Clearing the rule falls back to folder logic.
    shows_store.clear_season_target(show.show_id, 2)
    plan2 = show_tracker.plan_for_episode(show, 2, 3)
    assert plan2.dest_dir != str(target)


def test_routes_to_folder_already_holding_that_season(tmp_path):
    drive_a = tmp_path / "a" / "Split"
    drive_b = tmp_path / "b" / "Split"
    (drive_a / "Season 01").mkdir(parents=True)
    (drive_b / "Season 02").mkdir(parents=True)

    show = _make_show(tmp_path, [drive_a, drive_b], title="Split", ext_id="tt-loop-b")

    # S2 episode must land on drive B (which owns Season 02), not drive A.
    plan = show_tracker.plan_for_episode(show, 2, 5)
    assert plan.confident
    assert str(drive_b) in plan.dest_dir
    # A brand-new season falls back to the first mapped folder.
    plan_s3 = show_tracker.plan_for_episode(show, 3, 1)
    assert str(drive_a) in plan_s3.dest_dir
    assert plan_s3.dest_dir.endswith("Season 03")  # copies drive A's padded style


def test_filename_is_sanitized(tmp_path):
    show_dir = tmp_path / "WeirdShow"
    (show_dir / "Season 01").mkdir(parents=True)
    show_id = shows_store.upsert_show(
        title='What? A "Show": Yes', media_type="tv",
        source="tvdb", external_id="tt-loop-c",
    )
    shows_store.add_show_folder(show_id, str(show_dir))
    show = shows_store.get_show(show_id)

    plan = show_tracker.plan_for_episode(show, 1, 1)
    assert plan.new_filename is not None
    for ch in '<>:"/\\|?*':
        assert ch not in plan.new_filename


def test_no_folders_stays_in_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path / "staging"))
    show_id = shows_store.upsert_show(
        title="Folderless", media_type="tv", source="tvdb", external_id="tt-loop-d",
    )
    show = shows_store.get_show(show_id)
    plan = show_tracker.plan_for_episode(show, 1, 1)
    assert not plan.confident
    assert plan.dest_dir == str(tmp_path / "staging")


def _two_roots(tmp_path, monkeypatch):
    big, small = tmp_path / "I" / "Tv Shows", tmp_path / "D" / "TV Shows"
    big.mkdir(parents=True)
    small.mkdir(parents=True)
    monkeypatch.setattr(config, "media_paths_for_types",
                        lambda *t: [str(small), str(big)])
    # The roomier drive wins for new folders.
    monkeypatch.setattr("torrent_routing.pick_root_by_free_space",
                        lambda c: str(big) if str(big) in c else (c[0] if c else None))
    return big, small


def test_new_show_episode_gets_its_own_folder(tmp_path, monkeypatch):
    # Monster / East of Eden / Max & Ruby, 2026-10-10: single episodes of a
    # show with no folder sat in staging while a season pack would have
    # created the folder.
    big, _small = _two_roots(tmp_path, monkeypatch)
    show = _make_show(tmp_path, [], title="Max & Ruby", ext_id="tt-new-a")
    plan = show_tracker.plan_for_episode(show, 1, 4)
    assert plan.confident
    assert plan.dest_dir == str(big / "Max & Ruby" / "Season 01")
    assert plan.show_folder == str(big / "Max & Ruby")
    assert plan.new_filename == "Max & Ruby - S01E04"
    # The pack planner lands in the same place.
    assert show_tracker.plan_for_season(show, 1).dest_dir == plan.dest_dir


def test_new_show_reuses_a_same_name_folder_on_another_root(tmp_path, monkeypatch):
    _big, small = _two_roots(tmp_path, monkeypatch)
    (small / "Monster High" / "Season 1").mkdir(parents=True)
    show = _make_show(tmp_path, [], title="Monster High", ext_id="tt-new-b")
    plan = show_tracker.plan_for_episode(show, 1, 2)
    assert plan.dest_dir == str(small / "Monster High" / "Season 1")  # its style


def test_new_show_never_takes_another_shows_folder(tmp_path, monkeypatch):
    big, _small = _two_roots(tmp_path, monkeypatch)
    taken = big / "Monster"
    taken.mkdir()
    _make_show(tmp_path, [taken], title="Monster", ext_id="tt-new-c")
    show_id = shows_store.upsert_show(title="Monster", media_type="tv",
                                      source="tvdb", external_id="tt-new-d", year=2022)
    plan = show_tracker.plan_for_episode(shows_store.get_show(show_id), 1, 1)
    assert plan.dest_dir == str(big / "Monster (2022)" / "Season 01")


def test_an_existing_title_year_folder_is_reused(tmp_path, monkeypatch):
    # Reviewer finding: with only "Monster (2022)" on disk, a plain "Monster"
    # folder was created beside it.
    big, _small = _two_roots(tmp_path, monkeypatch)
    (big / "Year Show (2022)").mkdir()
    show_id = shows_store.upsert_show(title="Year Show", media_type="tv",
                                      source="tvdb", external_id="tt-new-e", year=2022)
    plan = show_tracker.plan_for_episode(shows_store.get_show(show_id), 1, 1)
    assert plan.dest_dir == str(big / "Year Show (2022)" / "Season 01")


def test_folder_ownership_ignores_case_and_slashes(tmp_path, monkeypatch):
    big, _small = _two_roots(tmp_path, monkeypatch)
    taken = big / "Case Show"
    taken.mkdir()
    _make_show(tmp_path, [str(taken).upper().replace("\\", "/")],
               title="Owner", ext_id="tt-new-f")
    show_id = shows_store.upsert_show(title="Case Show", media_type="tv",
                                      source="tvdb", external_id="tt-new-g", year=2020)
    plan = show_tracker.plan_for_episode(shows_store.get_show(show_id), 1, 1)
    import os
    if os.name == "nt":   # case-insensitive paths only on Windows
        assert plan.dest_dir == str(big / "Case Show (2020)" / "Season 01")


def test_a_file_without_sxxeyy_takes_the_planned_episode_name():
    # "01. Monster High - Jaundice Brothers.mp4" was moved but kept its name.
    from pathlib import Path
    import torrent_routing
    from download_manager import DownloadManager
    plan = torrent_routing.RoutePlan(confident=True, dest_dir="x",
                                     new_filename="Monster High - S01E01", reason="t")
    stem = DownloadManager._episode_stem_for_file
    assert stem(Path("01. Monster High - Jaundice Brothers.mp4"), show_name="Monster High",
                plan=plan, single_video=True) == "Monster High - S01E01"
    # A file that names its own episode still wins, and a pack (several
    # videos, no single-episode plan name) never borrows one.
    assert stem(Path("Monster.High.S01E05.mkv"), show_name="Monster High",
                plan=plan, single_video=True) == "Monster High - S01E05"
    assert stem(Path("01. Jaundice Brothers.mp4"), show_name="Monster High",
                plan=plan, single_video=False) is None


def test_grab_marker_lifecycle(tmp_path):
    show_id = shows_store.upsert_show(
        title="Marker", media_type="tv", source="tvdb", external_id="tt-loop-e",
    )
    shows_store.replace_episodes(
        show_id,
        [type("E", (), {"season": 1, "episode": 1, "title": "x", "air_date": "2020-01-01"})()],
    )
    shows_store.set_episode_grab(show_id, 1, 1, 42)
    ep = shows_store.missing_episodes(show_id)[0]
    assert ep.grab_download_id == 42
    shows_store.set_episode_grab(show_id, 1, 1, None)
    assert shows_store.missing_episodes(show_id)[0].grab_download_id is None
    # Marking the file present removes it from missing entirely.
    shows_store.set_episode_file(show_id, 1, 1, "D:/x/marker.mkv")
    assert shows_store.missing_episodes(show_id) == []
