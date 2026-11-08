"""Correct-account launches and reset failure paths; never stop the real Plex."""

from contextlib import contextmanager
import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import plex_control
import windows_desktop_launch as launch


DESKTOP = launch.ProcessIdentity("S-1-desktop", 1, False)


class FakeAPI:
    def __init__(self, current=DESKTOP, desktop=DESKTOP, failure=None):
        self.current = current
        self.desktop = desktop
        self.failure = failure
        self.events = []
        self.environ = {"USERPROFILE": "desktop-profile", "LOCALAPPDATA": "desktop-local",
                        "APPDATA": "desktop-roaming"}

    def desktop_pid(self):
        if self.failure == "no-desktop":
            raise launch.DesktopLaunchError("No signed-in Windows desktop was found.")
        return 99

    def open_token(self, pid, *, duplicate=False):
        self.events.append(("open", pid, duplicate))
        if self.failure == "open" and pid == 99:
            raise OSError("access denied")
        return "current" if pid is None else "desktop"

    def identity(self, token):
        return self.current if token == "current" else self.desktop

    def close_handle(self, token):
        self.events.append(("close", token))

    def duplicate_primary(self, token):
        self.events.append(("duplicate", token))
        return "primary"

    def environment(self, token):
        self.events.append(("environment", token))
        if self.failure == "environment":
            raise OSError("user profile missing")
        return "env-block", self.environ

    def destroy_environment(self, block):
        self.events.append(("destroy-env", block))

    def spawn_with_token(self, token, block, executable, args):
        self.events.append(("spawn-token", token, block, executable, args))
        if self.failure == "spawn":
            raise OSError("privilege not held")
        return 101


@pytest.fixture
def executable(tmp_path):
    exe = tmp_path / "Plex Media Server.exe"
    exe.write_bytes(b"fixture only; never execute")
    return exe


def test_normal_account_uses_user_environment_without_native_impersonation(monkeypatch, executable):
    api = FakeAPI()
    monkeypatch.setattr(launch, "_WindowsAPI", lambda: api)
    seen = {}

    def spawn(args, **kwargs):
        seen.update(args=args, **kwargs)
        return SimpleNamespace(pid=101)

    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    with launch.prepare_desktop_launch() as prepared:
        assert prepared.launch(executable) == 101
    assert seen["env"] == api.environ
    assert seen["cwd"] == str(executable.parent)
    assert seen["args"] == [str(executable)]
    assert not any(event[0] in ("duplicate", "spawn-token") for event in api.events)
    assert ("destroy-env", "env-block") in api.events
    assert api.events.count(("close", "desktop")) == 1
    assert api.events.count(("close", "current")) == 1


@pytest.mark.parametrize("current", [
    launch.ProcessIdentity("S-1-desktop", 1, True),  # same-account elevation
    launch.ProcessIdentity("S-1-administrator", 1, True),  # different UAC account
    launch.ProcessIdentity("S-1-other", 1, False),  # runas without elevation
])
def test_alternate_identity_uses_desktop_primary_token(monkeypatch, executable, current):
    api = FakeAPI(current=current)
    monkeypatch.setattr(launch, "_WindowsAPI", lambda: api)
    monkeypatch.setattr(launch.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not inherit administrator token"))
    with launch.prepare_desktop_launch() as prepared:
        assert prepared.identity == DESKTOP
        assert prepared.launch(executable) == 101
    assert ("duplicate", "desktop") in api.events
    assert ("environment", "primary") in api.events
    assert any(event[:3] == ("spawn-token", "primary", "env-block") for event in api.events)
    assert api.events[-3:] == [("destroy-env", "env-block"), ("close", "primary"),
                               ("close", "desktop")]


@pytest.mark.parametrize("desktop,current,reason", [
    (launch.ProcessIdentity("S-1-desktop", 2, False), DESKTOP, "different Windows session"),
    (launch.ProcessIdentity("S-1-desktop", 1, True), DESKTOP, "desktop itself is elevated"),
])
def test_unsafe_desktop_rejected_before_launch(monkeypatch, desktop, current, reason):
    api = FakeAPI(desktop=desktop, current=current)
    monkeypatch.setattr(launch, "_WindowsAPI", lambda: api)
    with pytest.raises(launch.DesktopLaunchError, match=reason):
        with launch.prepare_desktop_launch():
            pytest.fail("unsafe identity should never be exposed")
    assert not any(event[0] == "environment" for event in api.events)
    assert api.events[-1] == ("close", "desktop")


@pytest.mark.parametrize("failure", ["no-desktop", "open", "environment", "spawn"])
def test_native_failure_never_falls_back_to_calling_account(monkeypatch, executable, failure):
    api = FakeAPI(current=launch.ProcessIdentity("S-1-admin", 1, True), failure=failure)
    monkeypatch.setattr(launch, "_WindowsAPI", lambda: api)
    monkeypatch.setattr(launch.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("unsafe fallback"))
    with pytest.raises(launch.DesktopLaunchError):
        with launch.prepare_desktop_launch() as prepared:
            prepared.launch(executable)
    if failure in ("environment", "spawn"):
        assert ("close", "primary") in api.events
        assert ("close", "desktop") in api.events
    if failure == "spawn":
        assert ("destroy-env", "env-block") in api.events


class FakeDesktop:
    def __init__(self, matches=True, fail_launch=False):
        self.matches = matches
        self.fail_launch = fail_launch
        self.launched = []

    def process_matches(self, pid):
        return self.matches

    def launch(self, path):
        if self.fail_launch:
            raise launch.DesktopLaunchError("No safe desktop launch")
        self.launched.append(path)
        return 101


def test_launch_is_idempotent_for_existing_correct_server(monkeypatch, executable):
    monkeypatch.setattr(plex_control.config, "PLEX_MEDIA_SERVER_PATH", str(executable))
    monkeypatch.setattr(plex_control, "_list_primary_plex_server_pids", lambda: {101})
    desktop = FakeDesktop()
    assert plex_control._launch_plex_from_executable(desktop) == "Plex is already running."
    assert not desktop.launched


def test_existing_wrong_account_is_reported_without_starting_duplicate(monkeypatch, executable):
    monkeypatch.setattr(plex_control.config, "PLEX_MEDIA_SERVER_PATH", str(executable))
    monkeypatch.setattr(plex_control, "_list_primary_plex_server_pids", lambda: {101})
    desktop = FakeDesktop(matches=False)
    result = plex_control._launch_plex_from_executable(desktop)
    assert "different Windows account" in result
    assert not desktop.launched


def test_native_launch_failure_does_not_spawn_fallback(monkeypatch, executable):
    monkeypatch.setattr(plex_control.config, "PLEX_MEDIA_SERVER_PATH", str(executable))
    monkeypatch.setattr(plex_control, "_list_primary_plex_server_pids", lambda: set())
    monkeypatch.setattr(plex_control.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("unsafe fallback"))
    assert "No safe desktop launch" in plex_control._launch_plex_from_executable(
        FakeDesktop(fail_launch=True))


def test_start_verification_rejects_wrong_owner(monkeypatch):
    monkeypatch.setattr(plex_control.config, "PLEX_LAUNCH_WAIT", 0)
    monkeypatch.setattr(plex_control, "_list_primary_plex_server_pids", lambda: {101})
    assert not plex_control._wait_for_plex_start(desktop=FakeDesktop(matches=False))
    assert plex_control._wait_for_plex_start(desktop=FakeDesktop(matches=True))
    assert not plex_control._wait_for_plex_start({101}, desktop=FakeDesktop(matches=True))


def test_reset_without_desktop_does_not_stop_healthy_server(monkeypatch, executable):
    monkeypatch.setattr(plex_control.sys, "platform", "win32")
    monkeypatch.setattr(plex_control.config, "PLEX_MEDIA_SERVER_PATH", str(executable))

    @contextmanager
    def no_desktop():
        raise launch.DesktopLaunchError("No signed-in Windows desktop was found.")
        yield  # pragma: no cover

    monkeypatch.setattr(plex_control, "prepare_desktop_launch", no_desktop)
    monkeypatch.setattr(plex_control, "_kill_plex_processes",
                        lambda: pytest.fail("must preflight before killing"))
    assert "No signed-in Windows desktop" in plex_control.hard_reset()


def test_reset_missing_executable_does_not_stop_healthy_server(monkeypatch, tmp_path):
    monkeypatch.setattr(plex_control.config, "PLEX_MEDIA_SERVER_PATH", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(plex_control, "_kill_plex_processes",
                        lambda: pytest.fail("must preflight before killing"))
    assert "running server was not stopped" in plex_control.hard_reset()


def test_reset_never_starts_second_server_after_failed_stop(monkeypatch):
    monkeypatch.setattr(plex_control, "_kill_plex_processes", lambda: ([], ["access denied"]))
    monkeypatch.setattr(plex_control, "_is_plex_gone", lambda: False)
    monkeypatch.setattr(plex_control, "_lingering_plex_processes", lambda: ["Plex [101]"])
    monkeypatch.setattr(plex_control.config, "PLEX_EXIT_GRACE", 0)
    monkeypatch.setattr(plex_control.config, "MAX_EXIT_RETRIES", 0)
    monkeypatch.setattr(plex_control, "_launch_plex", lambda *a: pytest.fail("unsafe duplicate"))
    assert "No second server was launched" in plex_control._hard_reset_with_desktop(FakeDesktop())


def test_normal_entrypoint_runs_ui_without_relaunching_as_admin(monkeypatch):
    import main

    seen = []
    monkeypatch.setattr(sys, "argv", ["Sensarr.exe"])
    monkeypatch.setattr(main, "_already_running", lambda: False)
    monkeypatch.setattr(main, "_offer_legacy_migration", lambda: None)
    monkeypatch.setattr(main, "_is_admin", lambda: False)
    monkeypatch.setitem(sys.modules, "desktop_app",
                        SimpleNamespace(run_desktop_app=lambda: seen.append("UI")))
    # A normal launch must reach the UI even without administrator membership.
    main.main()
    assert seen == ["UI"]


@pytest.mark.parametrize("spec_name", ["Sensarr.spec", "Sensarr-portable.spec"])
def test_packaged_app_does_not_require_administrator(spec_name):
    spec = Path(__file__).resolve().parents[1] / spec_name
    tree = ast.parse(spec.read_text(encoding="utf-8"))
    exe_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "EXE"]
    assert len(exe_calls) == 1
    requirement = next(arg.value for arg in exe_calls[0].keywords if arg.arg == "uac_admin")
    assert ast.literal_eval(requirement) is False


def test_primary_token_requests_windows_process_setup_rights():
    # Regression for a real cross-account ERROR_ACCESS_DENIED. Neither extra
    # right alone was sufficient in the native diagnostic fixture.
    api = launch._WindowsAPI.__new__(launch._WindowsAPI)
    observed = {}

    def duplicate(token, rights, attributes, level, token_type, result):
        observed.update(rights=rights, level=level, token_type=token_type)
        result._obj.value = 123
        return True

    api.advapi = SimpleNamespace(DuplicateTokenEx=duplicate)
    result = api.duplicate_primary(99)
    assert result.value == 123
    assert observed == {"rights": 0x18B, "level": 2, "token_type": 1}


@pytest.mark.parametrize("name", [
    "Plex Update Service.exe", "Plex Update Service Launcher.exe",
])
@pytest.mark.parametrize("match_by_path", [False, True])
def test_updater_service_is_not_part_of_server_reset(monkeypatch, tmp_path, name, match_by_path):
    install = tmp_path / "Plex Media Server"
    monkeypatch.setattr(plex_control.config, "PLEX_MEDIA_SERVER_PATH",
                        str(install / "Plex Media Server.exe"))
    process = SimpleNamespace(pid=424242, info={
        "name": "" if match_by_path else name,
        "exe": str(install / name),
    })
    assert not plex_control._is_plex_process(process)
    monkeypatch.setattr(plex_control.psutil, "process_iter", lambda *a, **k: [process])
    monkeypatch.setattr(plex_control, "_stop_process_tree",
                        lambda *a: pytest.fail("updater must not be terminated"))
    assert plex_control._is_plex_gone()
    assert plex_control._kill_plex_processes() == ([], [])
