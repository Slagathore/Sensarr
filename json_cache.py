# =============================================================================
# json_cache.py
# =============================================================================
# JSON replacement for the app's pickle cache files (Task S item 1).
#
# Why this exists: the app self-elevates via UAC and the cache directory is
# the user-writable install folder. Unpickling a file any standard user
# process can replace means arbitrary code execution in the ELEVATED process
# at next launch. json.loads() can only ever produce data.
#
# Contract:
#   - save_json_cache() writes {"version": N, "payload": <encoded>} and, on
#     success, deletes any legacy .pkl files it is told about (the app dir is
#     user data — stale pickle files are removed, not orphaned).
#   - load_json_cache() returns the decoded payload dict, or None on ANY
#     problem: missing file, malformed JSON, wrong version, unknown dataclass
#     tag, pickle-era bytes at the path. A bad cache is a cache miss, never
#     an error and never code execution.
#   - Dataclasses are serialized explicitly with a type tag and reconstructed
#     only from the caller's allowlist. Tuples survive the round trip (several
#     cached payloads are isinstance-checked as tuples). Dicts with non-string
#     keys (e.g. ShowInventory.seasons: dict[int, int]) are preserved too.
# =============================================================================

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Marker keys inside encoded JSON. Only decode() gives them meaning; a
# hostile cache file can at worst build tuples/allowlisted dataclasses.
_TUPLE_KEY = "__tuple__"
_KDICT_KEY = "__kdict__"
_DC_KEY = "__dc__"
_DC_FIELDS_KEY = "fields"


def encode_payload(value: Any) -> Any:
    """Encode a cache payload into plain JSON-serializable structures.

    Raises TypeError for anything outside the supported shapes — callers
    treat that as "don't cache this", never as a crash.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple):
        return {_TUPLE_KEY: [encode_payload(v) for v in value]}
    if isinstance(value, list):
        return [encode_payload(v) for v in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: encode_payload(v) for k, v in value.items()}
        return {_KDICT_KEY: [[encode_payload(k), encode_payload(v)]
                             for k, v in value.items()]}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            _DC_KEY: type(value).__name__,
            _DC_FIELDS_KEY: {
                f.name: encode_payload(getattr(value, f.name))
                for f in dataclasses.fields(value)
            },
        }
    raise TypeError(f"Unsupported cache payload type: {type(value).__name__}")


def decode_payload(value: Any, dataclass_types: dict[str, type]) -> Any:
    """Reverse encode_payload(). Unknown dataclass tags raise ValueError so
    the whole cache load degrades to a miss."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [decode_payload(v, dataclass_types) for v in value]
    if isinstance(value, dict):
        if _TUPLE_KEY in value and len(value) == 1:
            return tuple(decode_payload(v, dataclass_types)
                         for v in value[_TUPLE_KEY])
        if _KDICT_KEY in value and len(value) == 1:
            return {decode_payload(k, dataclass_types):
                    decode_payload(v, dataclass_types)
                    for k, v in value[_KDICT_KEY]}
        if _DC_KEY in value:
            name = value.get(_DC_KEY)
            cls = dataclass_types.get(name)
            if cls is None:
                raise ValueError(f"Unknown dataclass tag in cache: {name!r}")
            fields = value.get(_DC_FIELDS_KEY) or {}
            kwargs = {k: decode_payload(v, dataclass_types)
                      for k, v in fields.items()}
            return cls(**kwargs)
        return {k: decode_payload(v, dataclass_types) for k, v in value.items()}
    raise ValueError(f"Unsupported value in cache file: {type(value).__name__}")


def save_json_cache(
    path: Path | str,
    payload: Any,
    *,
    version: int,
    legacy_paths: Iterable[Path | str] = (),
) -> bool:
    """Best-effort write. Returns True when the cache landed on disk.

    On success, deletes the given legacy pickle files — once a JSON cache
    exists the .pkl siblings are dead weight AND a lingering deserialization
    hazard for older builds.
    """
    path = Path(path)
    try:
        encoded = encode_payload(payload)
        text = json.dumps({"version": version, "payload": encoded})
        path.write_text(text, encoding="utf-8")
    except Exception:
        logger.debug("JSON cache save failed for %s.", path, exc_info=True)
        return False
    for legacy in legacy_paths:
        try:
            Path(legacy).unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove legacy cache %s.", legacy,
                         exc_info=True)
    return True


def load_json_cache(
    path: Path | str,
    *,
    version: int,
    dataclass_types: Iterable[type] = (),
) -> Any | None:
    """Load a cache written by save_json_cache(). None = cache miss.

    Every failure mode (missing file, garbage bytes, a planted pickle file,
    a version bump, an unknown dataclass tag) is a graceful miss. The file
    content is only ever passed to json.loads — it cannot execute.
    """
    path = Path(path)
    types = {cls.__name__: cls for cls in dataclass_types}
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != version:
            logger.debug("JSON cache %s rejected (version/shape mismatch).", path)
            return None
        return decode_payload(raw.get("payload"), types)
    except Exception:
        logger.debug("JSON cache load failed for %s — treating as a miss.",
                     path, exc_info=True)
        return None
