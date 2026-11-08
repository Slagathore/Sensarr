"""Resolve staging ownership without following symlinks or Windows junctions."""

import os
from pathlib import Path
import stat


def has_redirected_component(path: Path) -> bool:
    """Treat inaccessible ancestors as unsafe, and inspect links with lstat."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue  # A not-yet-created descendant still has a safe boundary.
        except (OSError, ValueError):
            return True
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            return True
    return False


def staging_root(root: str | Path) -> Path | None:
    try:
        path = Path(os.path.abspath(root))
        if has_redirected_component(path):
            return None
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def owned_staging_path(root: str | Path, entry: str | Path) -> Path | None:
    """A strict descendant of root, or None when ownership cannot be proved.

    Absolute legacy manifest entries are supported only inside this boundary.
    Canonical containment handles '..'; checking both the lexical and resolved
    paths also rejects symlinks/junctions that happen to point back inside it.
    """
    base = staging_root(root)
    if base is None:
        return None
    try:
        raw = Path(str(entry).replace("\\", os.sep))
        path = raw if raw.is_absolute() else base / raw
        if has_redirected_component(path):
            return None
        resolved = path.resolve()
        if (resolved == base or not resolved.is_relative_to(base)
                or has_redirected_component(resolved)):
            return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None
