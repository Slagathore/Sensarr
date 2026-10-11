# =============================================================================
# "Match existing size" shows are ones Cole wants the moment they air
# (2026-10-11): their newly aired episodes are rechecked every 30 minutes, may
# take a release up to 4x the standard size straight away (no day's wait), and
# prefer the library's own size, then the standard size, then the rest.
# =============================================================================

import datetime as dt
import hashlib

import pytest

import config
import db
import download_manager
import downloads_store
import queue_store
import shows_store
from download_manager import DownloadManager
from torrent_search import TorrentResult

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    with db.connect() as conn:
        for table in ("downloads", "selection_runs", "candidate_decisions",
                      "failed_grabs", "grab_deferrals", "episodes",
                      "tracked_shows", "show_folders"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass   # a table a fresh test DB has not created yet
        conn.commit()
    monkeypatch.setattr(config, "SIZE_PREF_MB_PER_MIN_TV", 22.1)
    monkeypatch.setattr(config, "SIZE_MAX_MB_PER_MIN_TV", 41.0)
    monkeypatch.setattr(config, "FAST_FOLLOW_ENABLED", True)
    monkeypatch.setattr(config, "FAST_FOLLOW_SIZE_FACTOR", 4.0)
    monkeypatch.setattr(config, "FAST_FOLLOW_RECHECK_MINUTES", 30.0)
    monkeypatch.setattr(config, "FAST_FOLLOW_WINDOW_DAYS", 7)


_IDS = iter(range(555001, 556000))


def _show(*, match=True, aired_days_ago=0):
    show_id = shows_store.upsert_show(title="Fast Show", media_type="tv",
                                      source="tvdb", external_id=str(next(_IDS)),
                                      status="Continuing")
    shows_store.set_show_runtime(show_id, 25.0)
    if match:
        shows_store.set_show_size_mode(show_id, "match_library")
    aired = (dt.date.today() - dt.timedelta(days=aired_days_ago)).isoformat()
    shows_store.replace_episodes(show_id, [
        type("E", (), {"season": 1, "episode": 3, "title": "E3", "air_date": aired})()])
    show = shows_store.get_show(show_id)
    return show, shows_store.missing_episodes(show_id)[0]


def _release(size_mb, tag="a"):
    ih = hashlib.sha1(tag.encode()).hexdigest()
    return TorrentResult(title=f"Fast Show S01E03 1080p WEB-DL {tag}",
                         magnet=f"magnet:?xt=urn:btih:{ih}", size_bytes=size_mb * MB,
                         seeders=200, source="tpb", media_type="tv")


def _dm(monkeypatch, releases, grabbed):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)

    class _Pool:
        results = tuple(releases)
        pool_stats = {"per_source": {"tpb": len(releases)}}
    monkeypatch.setattr(download_manager, "search_collect", lambda *a, **k: _Pool())
    monkeypatch.setattr(dm, "grab", lambda r, **k: grabbed.append(r) or 999)
    return dm


def test_only_recent_episodes_of_match_size_shows_fast_follow():
    show, ep = _show(aired_days_ago=1)
    assert DownloadManager._is_fast_follow(show, ep)
    old_show, old_ep = _show(aired_days_ago=10)
    assert not DownloadManager._is_fast_follow(old_show, old_ep)
    plain, plain_ep = _show(match=False)
    assert not DownloadManager._is_fast_follow(plain, plain_ep)


def test_an_oversized_release_is_taken_right_away(monkeypatch):
    # 867 MB at 25 min: over 1.2x the 22.1 MB/min preference, under 41 MB/min.
    show, ep = _show()
    grabbed: list = []
    dm = _dm(monkeypatch, [_release(867)], grabbed)
    assert dm._grab_one_episode(show, ep) == [999]
    assert downloads_store.get_grab_deferral(f"ep:{show.show_id}:1:3") is None


def test_a_plain_show_still_waits_a_day_for_it(monkeypatch):
    show, ep = _show(match=False)
    grabbed: list = []
    dm = _dm(monkeypatch, [_release(867)], grabbed)
    assert dm._grab_one_episode(show, ep) == [] and not grabbed


def test_the_cap_rises_to_four_times_the_standard(monkeypatch):
    # 4 x 22.1 MB/min x 25 min = 2210 MB. 2000 MB passes, 2500 MB does not.
    show, ep = _show()
    grabbed: list = []
    dm = _dm(monkeypatch, [_release(2000)], grabbed)
    assert dm._grab_one_episode(show, ep) == [999]

    show2, ep2 = _show()
    grabbed2: list = []
    dm2 = _dm(monkeypatch, [_release(2500, "b")], grabbed2)
    downloads_store.clear_grab_deferral(f"ep:{show2.show_id}:1:3")
    assert dm2._grab_one_episode(show2, ep2) == [] and not grabbed2


def test_nothing_found_is_rechecked_in_thirty_minutes(monkeypatch):
    show, ep = _show()
    grabbed: list = []
    dm = _dm(monkeypatch, [], grabbed)
    assert dm._grab_one_episode(show, ep) == []
    hold = downloads_store.get_grab_deferral(f"ep:{show.show_id}:1:3")
    due = dt.datetime.fromisoformat(hold["next_attempt_at"])
    left = due - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(minutes=25) < left <= dt.timedelta(minutes=31)


def test_the_size_order_is_library_then_standard_then_bigger():
    minutes, library, standard = 25.0, 40.0, 22.1
    big = _release(2000, "big")            # 80 MB/min
    std = _release(500, "std")             # 20 MB/min
    lib = _release(1000, "lib")            # 40 MB/min, the library's own size
    pick = DownloadManager._fast_follow_pick
    # Survivors arrive best-first from the selector; the tiers override that.
    assert pick([big, std, lib], big, minutes=minutes, library_rate=library,
                standard_rate=standard) is lib
    assert pick([big, std], big, minutes=minutes, library_rate=library,
                standard_rate=standard) is std
    assert pick([big], big, minutes=minutes, library_rate=library,
                standard_rate=standard) is big
    # No library sample yet: the standard size comes first.
    assert pick([big, lib, std], big, minutes=minutes, library_rate=None,
                standard_rate=standard) is std


def test_the_pass_only_grabs_followed_fast_follow_episodes(monkeypatch):
    # Reviewer finding: the size tag alone must not start grabbing a show the
    # user never followed; it only speeds up one that is followed.
    unfollowed, _ = _show(aired_days_ago=0)
    grabbed: list = []
    dm = _dm(monkeypatch, [_release(600)], grabbed)
    monkeypatch.setattr("show_tracker._sync_show_impl", lambda sid: "")
    monkeypatch.setattr(config, "SHOWS_AUTO_GRAB", False)
    assert dm.fast_follow_pass() == [] and not grabbed
    _set_auto_grab(unfollowed.show_id)
    assert dm.fast_follow_pass() == [999]
    assert len(grabbed) == 1


def _set_auto_grab(show_id):
    with db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET auto_grab = 1 WHERE id = ?", (show_id,))
        conn.commit()


def test_a_show_resyncs_once_per_air_date(monkeypatch):
    show, _ = _show(aired_days_ago=0)
    _set_auto_grab(show.show_id)
    with db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET next_air_date = ? WHERE id = ?",
                     (dt.date.today().isoformat(), show.show_id))
        conn.commit()
    syncs: list = []
    monkeypatch.setattr("show_tracker._sync_show_impl", lambda sid: syncs.append(sid))
    dm = _dm(monkeypatch, [], [])
    dm._fast_follow_due()
    dm._fast_follow_due()
    assert syncs == [show.show_id]


def test_the_recorded_decision_names_what_was_grabbed(monkeypatch):
    # The selector may rank a big, well-seeded copy first; fast follow takes
    # the standard-size one, and the stored run must say so.
    show, ep = _show()
    big = TorrentResult(title="Fast Show S01E03 2160p WEB-DL big",
                        magnet="magnet:?xt=urn:btih:" + hashlib.sha1(b"big").hexdigest(),
                        size_bytes=2000 * MB, seeders=900, source="tpb", media_type="tv")
    std = _release(500, "std")
    grabbed: list = []
    dm = _dm(monkeypatch, [big, std], grabbed)
    assert dm._grab_one_episode(show, ep) == [999]
    assert grabbed[0].title == std.title
    with db.connect() as conn:
        title = conn.execute("SELECT chosen_title FROM selection_runs "
                             "ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert title == std.title
