"""Recognize archive bytes even when the filename says this is media."""

from pathlib import Path


_SIGNATURES = (
    (b"Rar!\x1a\x07\x00", "RAR"),
    (b"Rar!\x1a\x07\x01\x00", "RAR5"),
    (b"PK\x03\x04", "ZIP"),
    (b"PK\x05\x06", "ZIP"),
    (b"PK\x07\x08", "split ZIP"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip"),
    (b"\x1f\x8b\x08", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "XZ"),
    (b"\x28\xb5\x2f\xfd", "Zstandard"),
)

ARCHIVE_SUFFIXES = frozenset({
    ".rar", ".zip", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".tbz2",
    ".xz", ".txz", ".zst", ".tzst",
})


def detect_archive(path: Path) -> str | None:
    """Read the first block only. Let callers handle any read failure."""
    with path.open("rb") as stream:
        header = stream.read(512)
    for prefix, name in _SIGNATURES:
        if header.startswith(prefix):
            return name
    if header[257:263] in (b"ustar\x00", b"ustar "):
        return "TAR"
    return None
