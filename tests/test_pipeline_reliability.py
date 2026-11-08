import logging
from pathlib import Path
from types import SimpleNamespace

import app_logging
import config
import downloads_store
import torrent_search
from download_manager import DownloadManager
from media_identity import MediaIdentity
from torrent_search import TorrentResult
from torrent_select import SelectWant
import request_intake
from subtitles import subtitle_language_filter_spec


def test_provider_circuit_pays_one_failure_not_one_per_query(monkeypatch):
    torrent_search._PROVIDER_FAILURE_UNTIL.clear()
    torrent_search._PROVIDER_LAST_ERROR.clear()
    calls = []

    def fail(url):
        calls.append(url)
        raise OSError("dns unavailable")

    monkeypatch.setattr(torrent_search, "_http_get", fail)
    monkeypatch.setattr(config, "TORRENT_PROVIDER_BACKOFF_SECONDS", 900)
    assert torrent_search.search_yts("one") == []
    assert torrent_search.search_yts("two") == []
    assert len(calls) == 1
    status = torrent_search.provider_circuit_status(("yts",))["yts"]
    assert status["available"] is False
    assert status["retry_in_seconds"] > 0
    torrent_search._PROVIDER_FAILURE_UNTIL.clear()
    torrent_search._PROVIDER_LAST_ERROR.clear()


def test_subtitle_runner_spec_uses_same_language_aliases(monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_LANGUAGE", "en")
    spec = subtitle_language_filter_spec()
    assert spec["preferred"] == "en"
    assert {"en", "eng", "english"}.issubset(set(spec["wantedTokens"]))
    assert "french" in spec["allLanguageTokens"]


def test_recently_failed_only_candidate_is_cooled_down_not_reselected(monkeypatch):
    infohash = "d" * 40
    result = TorrentResult(
        title="Example Movie 2024 1080p WEB-DL", media_type="movie",
        magnet="magnet:?xt=urn:btih:" + infohash,
        size_bytes=1_000_000_000, seeders=50, source="tpb")
    want = SelectWant(
        identity=MediaIdentity(media_type="movie", canonical_title="Example Movie",
                               canonical_year=2024),
        size_pref_mb_min=10.0, fallback_minutes=120.0)
    manager = object.__new__(DownloadManager)
    monkeypatch.setattr(manager, "_recent_failure_hashes", lambda key: {infohash})
    monkeypatch.setattr(manager, "_blocklist_for", lambda _want: None)

    decision, _by_hash = manager._run_selection(
        [result], want, failure_key="req:1")

    assert decision.chosen is False
    assert decision.pool_stats["recent_failures_skipped"] == 1


def test_active_request_reuse_is_not_reported_as_in_library():
    row = SimpleNamespace(status="grabbing", request_id=42)
    message = request_intake.reused_request_message(row, "Silo S01")
    assert "request #42" in message
    assert "grabbing" in message
    assert "in your library" not in message


def test_completed_import_exception_becomes_actionable_not_torrent_error(monkeypatch):
    did = downloads_store.create_download(
        title="Complete Payload", magnet="magnet:?xt=urn:btih:" + "b" * 40,
        source="tpb", media_type="movie", request_id=None, staging_dir="D:/stage",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=True, auto_move=True)
    downloads_store.set_status(did, "downloaded", completed=True)
    manager = object.__new__(DownloadManager)
    manager._on_update = None
    monkeypatch.setattr(manager, "_post_process",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))

    outcome = manager._finish_post_process(did, request_title="Complete Payload")

    row = downloads_store.get_download(did)
    assert outcome == "import failed: locked"
    assert row.status == "downloaded"
    assert row.error == "import failed: locked"
    assert downloads_store.display_status(row.status, row.progress,
                                          error=row.error) == "needs import"


def test_destination_collision_becomes_visible_needs_import(monkeypatch):
    did = downloads_store.create_download(
        title="Complete Collision", magnet="magnet:?xt=urn:btih:" + "e" * 40,
        source="tpb", media_type="movie", request_id=None, staging_dir="D:/stage",
        planned_dest="D:/movies/Complete Collision", planned_name=None,
        route_reason=None, auto_rename=True, auto_move=True)
    downloads_store.set_status(did, "downloaded", completed=True)
    manager = object.__new__(DownloadManager)
    manager._on_update = None
    monkeypatch.setattr(
        manager, "_post_process",
        lambda *a, **k: "processed (no move) — planned: collision")

    outcome = manager._finish_post_process(did, request_title="Complete Collision")

    row = downloads_store.get_download(did)
    assert outcome.startswith("processed (no move)")
    assert row.status == "downloaded"
    assert row.error.startswith("needs import:")
    assert downloads_store.display_status(row.status, row.progress,
                                          error=row.error) == "needs import"


def test_exact_prior_move_is_reconciled_instead_of_failed(tmp_path: Path):
    placed = tmp_path / "Library" / "Show - S01E01.mkv"
    placed.parent.mkdir()
    placed.write_bytes(b"already placed")
    did = downloads_store.create_download(
        title="Show S01E01", magnet="magnet:?xt=urn:btih:" + "f" * 40,
        source="tpb", media_type="tv", request_id=None, staging_dir=str(tmp_path),
        planned_dest=str(placed.parent), planned_name=placed.stem,
        route_reason=None, auto_rename=True, auto_move=True)
    downloads_store.set_status(did, "downloaded", completed=True)
    downloads_store.add_history(
        did, "moved", before=str(tmp_path / "staged.mkv"), after=str(placed))
    manager = object.__new__(DownloadManager)

    found = manager._reconcile_prior_move(downloads_store.get_download(did))

    row = downloads_store.get_download(did)
    assert found == [str(placed)]
    assert row.status == "moved"
    assert row.verification_state == "verified"


def test_missing_payload_is_not_reported_as_needs_placement(tmp_path: Path):
    did = downloads_store.create_download(
        title="Unknown Show S01E01",
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        source="tpb", media_type="other", request_id=None,
        staging_dir=str(tmp_path), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=True, auto_move=True)
    downloads_store.set_files(did, ["missing.mkv"])
    downloads_store.set_status(did, "downloaded", completed=True)
    manager = object.__new__(DownloadManager)
    manager._on_update = None

    outcome = manager._finish_post_process(
        did, request_title="Unknown Show S01E01")

    row = downloads_store.get_download(did)
    assert outcome == "no media files found in staging for this download"
    assert row.status == "downloaded"
    assert row.error.startswith("missing staging payload:")
    assert downloads_store.display_status(row.status, row.progress,
                                          error=row.error) == "missing files"
    assert "needs placement" not in (row.error or "")


def test_status_query_is_not_limited_to_newest_300_rows():
    old_id = downloads_store.create_download(
        title="Old completed payload", magnet="magnet:?xt=urn:btih:" + "c" * 40,
        source="tpb", media_type="movie", request_id=None, staging_dir="D:/stage",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=True, auto_move=True)
    downloads_store.set_status(old_id, "downloaded", completed=True)
    for index in range(305):
        did = downloads_store.create_download(
            title=f"new-{index}",
            magnet="magnet:?xt=urn:btih:" + f"{index:040d}"[-40:],
            source="tpb", media_type="movie", request_id=None,
            staging_dir="D:/stage", planned_dest=None, planned_name=None,
            route_reason=None, auto_rename=False, auto_move=False)
        downloads_store.set_status(did, "moved", completed=True)
    found = downloads_store.list_downloads_by_status(("downloaded",))
    assert old_id in {row.download_id for row in found}


def test_log_redaction_removes_bot_and_query_tokens():
    raw = ("GET https://api.telegram.org/bot123456:ABC-secret/sendMessage"
           "?api_key=also-secret&token=third-secret")
    clean = app_logging.redact_log_text(raw)
    assert "ABC-secret" not in clean
    assert "also-secret" not in clean
    assert "third-secret" not in clean
    assert clean.count("<redacted>") == 3


def test_redaction_filter_formats_arguments_before_redacting():
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        "GET %s", ("https://api.telegram.org/bot1:token/path",), None)
    assert app_logging.SecretRedactionFilter().filter(record)
    assert "token" not in record.getMessage()
