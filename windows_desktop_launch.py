"""Launch applications as the signed-in desktop user, even after UAC elevation.

An over-the-shoulder UAC prompt can change the account, not just the privilege
level. Inheriting Sensarr's token/environment then selects the administrator's
HKCU and AppData, which makes Plex load a different server. The desktop shell
is our authority for the intended interactive user. Never fall back to the
caller's account when that identity cannot be established.

Microsoft API references:
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getshellwindow
https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-createprocesswithtokenw
https://learn.microsoft.com/windows/win32/api/userenv/nf-userenv-createenvironmentblock
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Iterator


class DesktopLaunchError(RuntimeError):
    """The desktop user's identity could not be used safely."""


@dataclass(frozen=True)
class ProcessIdentity:
    user_sid: str
    session_id: int
    elevated: bool

    def same_user_session(self, other: ProcessIdentity) -> bool:
        return (self.user_sid == other.user_sid
                and self.session_id == other.session_id)


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _WindowsAPI:
    """Small native boundary, kept separate from launch policy for testing."""

    TOKEN_QUERY = 0x0008
    TOKEN_DUPLICATE = 0x0002
    TOKEN_ASSIGN_PRIMARY = 0x0001
    TOKEN_ADJUST_DEFAULT = 0x0080
    TOKEN_ADJUST_SESSIONID = 0x0100

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DesktopLaunchError("Desktop-user launch is only available on Windows.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.userenv = ctypes.WinDLL("userenv", use_last_error=True)
        handle = wintypes.HANDLE
        ptr_handle = ctypes.POINTER(handle)
        dword = wintypes.DWORD
        ptr_dword = ctypes.POINTER(dword)
        void = ctypes.c_void_p
        self._bind(self.kernel, "GetCurrentProcess", [], handle)
        self._bind(self.kernel, "OpenProcess", [dword, wintypes.BOOL, dword], handle)
        self._bind(self.kernel, "CloseHandle", [handle], wintypes.BOOL)
        self._bind(self.kernel, "LocalFree", [void], void)
        self._bind(self.user, "GetShellWindow", [], wintypes.HWND)
        self._bind(self.user, "GetWindowThreadProcessId",
                   [wintypes.HWND, ptr_dword], dword)
        self._bind(self.advapi, "OpenProcessToken", [handle, dword, ptr_handle],
                   wintypes.BOOL)
        self._bind(self.advapi, "GetTokenInformation",
                   [handle, ctypes.c_int, void, dword, ptr_dword], wintypes.BOOL)
        self._bind(self.advapi, "ConvertSidToStringSidW",
                   [void, ctypes.POINTER(wintypes.LPWSTR)], wintypes.BOOL)
        self._bind(self.advapi, "DuplicateTokenEx",
                   [handle, dword, void, ctypes.c_int, ctypes.c_int, ptr_handle],
                   wintypes.BOOL)
        self._bind(self.userenv, "CreateEnvironmentBlock",
                   [ctypes.POINTER(void), handle, wintypes.BOOL], wintypes.BOOL)
        self._bind(self.userenv, "DestroyEnvironmentBlock", [void], wintypes.BOOL)
        self._bind(self.advapi, "CreateProcessWithTokenW",
                   [handle, dword, wintypes.LPCWSTR, wintypes.LPWSTR, dword,
                    void, wintypes.LPCWSTR, ctypes.POINTER(_STARTUPINFOW),
                    ctypes.POINTER(_PROCESS_INFORMATION)], wintypes.BOOL)

    @staticmethod
    def _bind(dll, name, args, result) -> None:
        fn = getattr(dll, name)
        fn.argtypes = args
        fn.restype = result

    @staticmethod
    def _error(operation: str) -> OSError:
        error = ctypes.WinError(ctypes.get_last_error())
        return OSError(f"{operation}: {error}")

    def close_handle(self, handle) -> None:
        if handle:
            self.kernel.CloseHandle(handle)

    def desktop_pid(self) -> int:
        window = self.user.GetShellWindow()
        if not window:
            raise DesktopLaunchError(
                "No signed-in Windows desktop was found. Sign in to the Windows "
                "account that owns Plex before using Launch or Hard Reset. "
                "Plex was not launched under the administrator account.")
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(window, ctypes.byref(pid)) or not pid.value:
            raise self._error("Reading the desktop shell process")
        return pid.value

    def open_token(self, pid: int | None, *, duplicate: bool = False):
        # None is the current process; pseudo-handles must not be closed.
        process = (self.kernel.GetCurrentProcess() if pid is None else
                   self.kernel.OpenProcess(0x1000, False, pid))
        if not process:
            raise self._error("Opening the desktop process")
        try:
            token = wintypes.HANDLE()
            rights = self.TOKEN_QUERY | (self.TOKEN_DUPLICATE if duplicate else 0)
            if not self.advapi.OpenProcessToken(process, rights, ctypes.byref(token)):
                raise self._error("Opening the desktop user token")
            return token
        finally:
            if pid is not None:
                self.close_handle(process)

    def _token_info(self, token, info_class: int):
        size = wintypes.DWORD()
        self.advapi.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
        if not size.value:
            raise self._error("Measuring Windows token information")
        buffer = ctypes.create_string_buffer(size.value)
        if not self.advapi.GetTokenInformation(
                token, info_class, buffer, size.value, ctypes.byref(size)):
            raise self._error("Reading Windows token information")
        return buffer

    def identity(self, token) -> ProcessIdentity:
        user_info = self._token_info(token, 1)  # TokenUser
        sid = ctypes.cast(user_info, ctypes.POINTER(_SID_AND_ATTRIBUTES)).contents.Sid
        text = wintypes.LPWSTR()
        if not self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise self._error("Reading the Windows user SID")
        try:
            user_sid = text.value or ""
        finally:
            self.kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        session = wintypes.DWORD.from_buffer_copy(self._token_info(token, 12)).value
        elevated = bool(wintypes.DWORD.from_buffer_copy(self._token_info(token, 20)).value)
        return ProcessIdentity(user_sid, session, elevated)

    def duplicate_primary(self, token):
        primary = wintypes.HANDLE()
        # The Secondary Logon service also needs these two adjustment rights
        # on its primary-token handle. Native cross-account probes on Windows 11
        # returned ERROR_ACCESS_DENIED when either was omitted. They grant no
        # new user privileges and never change the original desktop shell token.
        rights = (self.TOKEN_QUERY | self.TOKEN_DUPLICATE | self.TOKEN_ASSIGN_PRIMARY
                  | self.TOKEN_ADJUST_DEFAULT | self.TOKEN_ADJUST_SESSIONID)
        # SecurityImpersonation=2; TokenPrimary=1. This copies an already
        # non-elevated desktop token; it never grants a higher privilege level.
        if not self.advapi.DuplicateTokenEx(
                token, rights, None, 2, 1, ctypes.byref(primary)):
            raise self._error("Preparing the desktop user token")
        return primary

    def environment(self, token) -> tuple[ctypes.c_void_p, dict[str, str]]:
        block = ctypes.c_void_p()
        # False is essential: inheriting the administrator's LOCALAPPDATA would
        # undo the account fix even though the new process has the right SID.
        if not self.userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise self._error("Loading the desktop user's environment")
        try:
            entries: dict[str, str] = {}
            offset = 0
            while True:
                line = ctypes.wstring_at(block.value + offset)
                if not line:
                    break
                split = line.find("=", 1)  # Also handle Windows' '=C:' entries.
                if split > 0:
                    entries[line[:split]] = line[split + 1:]
                # Python len() counts an astral Unicode character once, while
                # Windows stores it as two UTF-16 code units.
                offset += len(line.encode("utf-16-le", errors="surrogatepass")) + 2
            by_name = {name.upper(): value for name, value in entries.items()}
            if not all(by_name.get(name) for name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA")):
                raise DesktopLaunchError("The signed-in Windows user's profile is not loaded.")
            return block, entries
        except BaseException:
            self.destroy_environment(block)
            raise

    def destroy_environment(self, block) -> None:
        if block:
            self.userenv.DestroyEnvironmentBlock(block)

    def spawn_with_token(self, token, block, executable: Path, args: tuple[str, ...]) -> int:
        command = subprocess.list2cmdline([str(executable), *args])
        if len(command) >= 1024:
            raise DesktopLaunchError("The desktop launch command exceeds Windows' 1024-character limit.")
        startup = _STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = "winsta0\\default"
        startup.dwFlags = 1  # STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE: Plex supplies its own tray icon.
        info = _PROCESS_INFORMATION()
        mutable_command = ctypes.create_unicode_buffer(command)
        # LOGON_WITH_PROFILE loads the correct HKCU. The caller and the shell
        # must share a session: CreateProcessWithTokenW uses the CALLER's session.
        if not self.advapi.CreateProcessWithTokenW(
                token, 1, str(executable), mutable_command, 0x08000400,
                block, str(executable.parent), ctypes.byref(startup), ctypes.byref(info)):
            raise self._error("Starting Plex as the desktop user")
        try:
            return int(info.dwProcessId)
        finally:
            self.close_handle(info.hThread)
            self.close_handle(info.hProcess)


@dataclass
class DesktopLaunch:
    identity: ProcessIdentity
    environment: dict[str, str]
    _api: _WindowsAPI
    _token: object
    _environment_block: object
    _same_unelevated_user: bool

    def process_matches(self, pid: int) -> bool:
        token = self._api.open_token(pid)
        try:
            return self.identity.same_user_session(self._api.identity(token))
        finally:
            self._api.close_handle(token)

    def launch(self, executable: Path, args: tuple[str, ...] = ()) -> int:
        executable = executable.resolve(strict=True)
        try:
            if self._same_unelevated_user:
                return subprocess.Popen(
                    [str(executable), *args], cwd=str(executable.parent),
                    env=self.environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).pid
            return self._api.spawn_with_token(
                self._token, self._environment_block, executable, args)
        except OSError as exc:
            raise DesktopLaunchError(
                f"Could not start Plex as the signed-in desktop user: {exc}. "
                "No administrator-profile fallback was attempted.") from exc


@contextmanager
def prepare_desktop_launch() -> Iterator[DesktopLaunch]:
    """Keep the verified desktop identity alive through a reset and relaunch.

    Fail before stopping Plex if a scheduled/headless invocation has no desktop,
    the shell is elevated, or Windows denies access to the intended user token.
    No saved password or new scheduled task is needed.
    """
    try:
        api = _WindowsAPI()
        with ExitStack() as stack:
            desktop_token = api.open_token(api.desktop_pid(), duplicate=True)
            stack.callback(api.close_handle, desktop_token)
            desktop = api.identity(desktop_token)
            current_token = api.open_token(None)
            try:
                current = api.identity(current_token)
            finally:
                api.close_handle(current_token)
            if desktop.session_id != current.session_id:
                raise DesktopLaunchError("The Plex desktop belongs to a different Windows session.")
            if desktop.elevated:
                raise DesktopLaunchError(
                    "The Windows desktop itself is elevated. Sign in with the normal "
                    "Windows account that owns Plex before launching it through Sensarr.")
            same_user = desktop.same_user_session(current) and not current.elevated
            primary = desktop_token
            if not same_user:
                primary = api.duplicate_primary(desktop_token)
                stack.callback(api.close_handle, primary)
            block, environment = api.environment(primary)
            stack.callback(api.destroy_environment, block)
            yield DesktopLaunch(desktop, environment, api, primary, block, same_user)
    except OSError as exc:
        raise DesktopLaunchError(
            f"Windows could not prepare the signed-in desktop user's Plex launch: {exc}. "
            "Sign in normally on this PC; Sensarr will not substitute the administrator account.") from exc
