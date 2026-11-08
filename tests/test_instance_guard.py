"""Cross-account startup must not run two bot pollers against one installation."""
from types import SimpleNamespace

import pytest
import psutil

import app_paths
import main


@pytest.fixture
def locked_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(app_paths, "PATHS", SimpleNamespace(runtime_dir=tmp_path))
    lock = tmp_path / "sensarr.pid"
    lock.write_text("424242", encoding="ascii")
    return lock


def denied():
    raise psutil.AccessDenied(424242)


def test_frozen_admin_instance_blocks_without_reading_command_line(monkeypatch, locked_instance):
    proc = SimpleNamespace(is_running=lambda: True, name=lambda: "Sensarr.exe",
                           cmdline=lambda: pytest.fail("admin command line must not be inspected"))
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    assert main._already_running()
    assert locked_instance.read_text() == "424242"


def test_live_uninspectable_instance_keeps_lock(monkeypatch, locked_instance):
    proc = SimpleNamespace(is_running=lambda: True, name=denied)
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    assert main._already_running()
    assert locked_instance.read_text() == "424242"


def test_stale_pid_allows_startup(monkeypatch, locked_instance):
    def missing(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", missing)
    assert not main._already_running()
    assert locked_instance.read_text() == str(main.os.getpid())


def test_unrelated_process_does_not_hold_lock(monkeypatch, locked_instance):
    proc = SimpleNamespace(is_running=lambda: True, name=lambda: "notepad.exe")
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    assert not main._already_running()


def test_source_python_instance_still_blocks(monkeypatch, locked_instance):
    proc = SimpleNamespace(is_running=lambda: True, name=lambda: "pythonw.exe",
                           cmdline=lambda: ["pythonw.exe", str(locked_instance.parent / "main.py")])
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    assert main._already_running()
    assert locked_instance.read_text() == "424242"
