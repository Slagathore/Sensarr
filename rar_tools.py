"""Inspect single-volume RARs and stream exact members from installed 7-Zip."""

from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata


_LIST_LIMIT = 8 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_WINDOWS_INVALID = frozenset('<>:"|?*')
_UNIX_MODE = re.compile(r"^[dlbcps-][rwxstST-]{9}$")


class RarError(RuntimeError):
    """An archive cannot be inspected or extracted without guessing."""


@dataclass(frozen=True)
class RarMember:
    path: str
    size: int


def _seven_zip() -> str:
    if os.name == "nt":
        for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            directory = os.environ.get(name)
            if directory:
                exe = Path(directory) / "7-Zip" / "7z.exe"
                if exe.is_file():
                    return str(exe)
    for name in ("7zz", "7z"):
        if executable := shutil.which(name):
            return executable
    raise RarError("7-Zip is required to unpack this RAR archive.")


def _safe_path_key(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    for part in parts:
        if (not part or part in (".", "..") or part.endswith((" ", "."))
                or any(ord(char) < 32 or char in _WINDOWS_INVALID for char in part)
                or PureWindowsPath(part).is_reserved()):
            raise RarError("The RAR contains a member path that cannot be handled safely.")
    return unicodedata.normalize("NFC", "/".join(parts)).casefold()


def _properties(record: str) -> dict[str, str]:
    fields = {}
    for line in record.split("\n"):
        name, marker, value = line.partition(" = ")
        if not marker or not name or name in fields:
            raise RarError("7-Zip returned an unrecognized RAR listing.")
        fields[name] = value
    return fields


def _enabled(properties: dict[str, str], name: str) -> bool:
    return properties.get(name, "") not in ("", "-", "0")


def _parse_listing(payload: bytes) -> tuple[RarMember, ...]:
    try:
        text = payload.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise RarError("7-Zip's RAR listing was not valid UTF-8.") from exc
    _, header_start, header_text = text.partition("\n--\n")
    archive_text, files_start, files_text = header_text.partition("\n----------\n")
    if not header_start or not files_start:
        raise RarError("7-Zip did not return a complete RAR listing.")
    archive = _properties(archive_text.strip("\n"))
    if archive.get("Type") not in ("Rar", "Rar5"):
        raise RarError("The selected file is not a supported RAR archive.")
    if _enabled(archive, "Multivolume") or archive.get("Volumes", "1") != "1":
        raise RarError("Multipart RAR archives require manual review.")
    if _enabled(archive, "Encrypted"):
        raise RarError("Encrypted RAR archives require manual review.")

    paths = set()
    members = []
    for record in files_text.strip("\n").split("\n\n"):
        item = _properties(record)
        path = item.get("Path", "")
        key = _safe_path_key(path)
        if key in paths:
            raise RarError("The RAR contains ambiguous duplicate member paths.")
        paths.add(key)
        if _enabled(item, "Split Before") or _enabled(item, "Split After"):
            raise RarError("Multipart RAR members require manual review.")
        if item.get("Encrypted") != "-":
            raise RarError("Encrypted or unverified RAR members require manual review.")
        if any(("link" in name.lower() or "reparse" in name.lower()
                or name == "Alternate Stream") and _enabled(item, name) for name in item):
            raise RarError("The RAR contains a link, reparse point, or alternate stream.")
        attributes = item.get("Attributes", "").split()
        if any((_UNIX_MODE.fullmatch(attribute) and attribute[0] not in ("-", "d"))
               or "L" in attribute for attribute in attributes):
            raise RarError("The RAR contains a link or special file.")
        folder = item.get("Folder")
        if folder not in ("+", "-"):
            raise RarError("7-Zip did not identify the RAR member type.")
        if folder == "+":
            continue
        size = item.get("Size", "")
        if not re.fullmatch(r"[0-9]{1,20}", size):
            raise RarError("7-Zip did not report a valid RAR member size.")
        members.append(RarMember(path, int(size)))
    return tuple(members)


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def list_rar_members(archive: Path, *, timeout: float = 30) -> tuple[RarMember, ...]:
    """Return exact 7-Zip selectors and uncompressed sizes for safe file members."""
    arguments = [_seven_zip(), "l", "-slt", "-sccUTF-8", "-bd", "-bsp0",
                 "-bse2", "-bso1", "--", os.fspath(archive.absolute())]
    try:
        with tempfile.TemporaryFile() as listing:
            completed = subprocess.run(arguments, stdin=subprocess.DEVNULL,
                                       stdout=listing, stderr=subprocess.DEVNULL,
                                       timeout=timeout, check=False,
                                       creationflags=_creation_flags())
            if completed.returncode:
                raise RarError(f"7-Zip could not list the RAR (exit {completed.returncode}).")
            listing.seek(0)
            payload = listing.read(_LIST_LIMIT + 1)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RarError("7-Zip could not finish listing this RAR.") from exc
    if len(payload) > _LIST_LIMIT:
        raise RarError("The RAR contains too many member records to inspect.")
    return _parse_listing(payload)


def extract_rar_member(archive: Path, member: RarMember, destination: Path,
                       *, timeout: float = 900) -> None:
    """Create one caller-selected path and stream only the exact listed member into it."""
    _safe_path_key(member.path)
    if member.size < 0:
        raise RarError("The RAR member has an invalid size.")
    arguments = [_seven_zip(), "x", "-so", "-spd", "-ssc", "-r-", "-bd",
                 "-bsp0", "-bse2", "-bso0", "--", os.fspath(archive.absolute()),
                 member.path]
    created = False
    try:
        with destination.open("xb") as output:
            created = True
            with subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  creationflags=_creation_flags()) as process:
                timed_out = threading.Event()

                def stop_on_timeout() -> None:
                    if process.poll() is None:
                        timed_out.set()
                        try:
                            process.kill()
                        except OSError:
                            pass

                timer = threading.Timer(timeout, stop_on_timeout)
                timer.daemon = True
                timer.start()
                try:
                    remaining = member.size
                    while chunk := process.stdout.read(min(_CHUNK_SIZE, remaining + 1)):
                        if len(chunk) > remaining:
                            raise RarError("7-Zip returned more bytes than the listed member size.")
                        output.write(chunk)
                        remaining -= len(chunk)
                    returncode = process.wait()
                    if timed_out.is_set():
                        raise RarError("7-Zip timed out while extracting the RAR member.")
                    if returncode:
                        raise RarError(f"7-Zip could not extract the RAR member (exit {returncode}).")
                    if remaining:
                        raise RarError("7-Zip returned fewer bytes than the listed member size.")
                finally:
                    if process.poll() is None:
                        process.kill()
                    timer.cancel()
    except (OSError, RarError) as exc:
        if created:
            destination.unlink(missing_ok=True)
        if isinstance(exc, RarError):
            raise
        raise RarError("The RAR member could not be written to the selected destination.") from exc
