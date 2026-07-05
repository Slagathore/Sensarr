import os
import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, cast


@dataclass(frozen=True)
class MediaLibraryPath:
    """One configured library path, tagged with the kind of media it holds."""
    path: str
    media_type: str   # "movie" | "tv" | "anime" | "xanime" | "mixed"


# All known media-type tags, plus "mixed" for un-typed paths (legacy or
# untyped roots that hold more than one kind of content).
MEDIA_TYPE_TAGS: tuple[str, ...] = ("movie", "tv", "anime", "xanime", "mixed")


def _load_dotenv_func() -> Callable[..., Any] | None:
    try:
        dotenv_module = cast(Any, import_module("dotenv"))
    except ImportError:
        return None
    return cast(Callable[..., Any] | None, getattr(dotenv_module, "load_dotenv", None))


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", _app_dir()))


APP_DIR: Path = _app_dir()
BUNDLE_DIR: Path = _bundle_dir()
DOTENV_PATH: Path = APP_DIR / ".env"
load_dotenv = _load_dotenv_func()

if load_dotenv is not None:
    if DOTENV_PATH.is_file():
        load_dotenv(DOTENV_PATH)
    else:
        load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(
            f"Required environment variable '{key}' is missing. "
            "Check your .env file."
        )
    return value


def _split_semicolon_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")


def _parse_id_list(raw: str) -> tuple[int, ...]:
    """Parse a comma/semicolon-separated list of numeric Telegram user IDs."""
    out: list[int] = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return tuple(out)


# Telegram user IDs that are always authorized to use the bot, in addition to
# the SQLite allowlist managed by auth_store (which is seeded from past
# requesters). Find your ID by messaging the bot once — denied users are shown
# their own ID so the admin can paste it here.
TELEGRAM_ALLOWED_USER_IDS: tuple[int, ...] = _parse_id_list(
    os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
)
APP_PRODUCT_NAME: str = "Plex Reset Button"
APP_VERSION: str = "1.0"

TRAY_ICON_CONFIDENCE: float = float(os.getenv("TRAY_ICON_CONFIDENCE", "0.85"))
TASKBAR_ICON_CONFIDENCE: float = float(os.getenv("TASKBAR_ICON_CONFIDENCE", "0.85"))
# How long (seconds) to pause between each process-gone check after Exit is clicked.
# Plex can take 10-30s to fully stop — 3s intervals × 10 retries = 30s total window.
PLEX_EXIT_WAIT: float = float(os.getenv("PLEX_EXIT_WAIT", "3"))
# Initial grace period (seconds) to wait before the first exit check.
# Gives Plex time to begin its shutdown sequence before we start polling.
PLEX_EXIT_GRACE: float = float(os.getenv("PLEX_EXIT_GRACE", "3"))
PLEX_LAUNCH_WAIT: float = float(os.getenv("PLEX_LAUNCH_WAIT", "5"))
MAX_EXIT_RETRIES: int = int(os.getenv("MAX_EXIT_RETRIES", "10"))
ADMIN_STATUS_REFRESH_SECONDS: int = int(os.getenv("ADMIN_STATUS_REFRESH_SECONDS", "15"))
PLEX_MEDIA_SERVER_PATH: str = os.getenv(
    "PLEX_MEDIA_SERVER_PATH",
    r"C:\Program Files\Plex\Plex Media Server\Plex Media Server.exe",
)
PLEX_SERVER_URL: str = os.getenv("PLEX_SERVER_URL", "http://127.0.0.1:32400").strip()
PLEX_TOKEN: str = os.getenv("PLEX_TOKEN", "").strip()
PLEX_CLIENT_IDENTIFIER: str = os.getenv("PLEX_CLIENT_IDENTIFIER", "").strip()
PLEX_VERIFY_SSL: bool = _env_bool("PLEX_VERIFY_SSL", False)
PLEX_REQUEST_TIMEOUT_SECONDS: int = int(
    os.getenv("PLEX_REQUEST_TIMEOUT_SECONDS", "10")
)
PLEX_HISTORY_FETCH_LIMIT: int = int(os.getenv("PLEX_HISTORY_FETCH_LIMIT", "200"))
APP_DB_PATH: str = os.getenv(
    "APP_DB_PATH",
    os.getenv("REQUESTS_DB_PATH", str(APP_DIR / "plex_reset_button.db")),
)
REQUESTS_DB_PATH: str = APP_DB_PATH
def _parse_media_library_paths(raw: str) -> list[MediaLibraryPath]:
    """
    Parse the MEDIA_LIBRARY_PATHS env var.

    Format: ``path|type;path|type;...`` where type is one of MEDIA_TYPE_TAGS.
    Entries without an explicit type fall back to "mixed".
    """
    parsed: list[MediaLibraryPath] = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "|" in entry:
            path_part, type_part = entry.split("|", 1)
            media_type = type_part.strip().lower()
            if media_type not in MEDIA_TYPE_TAGS:
                media_type = "mixed"
            parsed.append(MediaLibraryPath(path=path_part.strip(), media_type=media_type))
        else:
            parsed.append(MediaLibraryPath(path=entry, media_type="mixed"))
    return parsed


def format_media_library_paths(paths: list[MediaLibraryPath]) -> str:
    """Inverse of _parse_media_library_paths — for writing back to .env."""
    return ";".join(f"{p.path}|{p.media_type}" for p in paths)


# New typed format takes precedence; fall back to legacy PLEX_LIBRARY_PATHS
# (untyped, treated as "mixed") so existing installs keep working.
MEDIA_LIBRARY_PATHS: list[MediaLibraryPath] = _parse_media_library_paths(
    os.getenv("MEDIA_LIBRARY_PATHS", "")
)
if not MEDIA_LIBRARY_PATHS:
    MEDIA_LIBRARY_PATHS = [
        MediaLibraryPath(path=p, media_type="mixed")
        for p in _split_semicolon_list(os.getenv("PLEX_LIBRARY_PATHS", ""))
    ]

# Flat list of just paths — preserved for backward compatibility with existing
# code that walked PLEX_LIBRARY_PATHS without caring about media type.
PLEX_LIBRARY_PATHS: list[str] = [p.path for p in MEDIA_LIBRARY_PATHS]


def media_paths_for_types(*types: str) -> list[str]:
    """
    Return library paths that match any of the given media-type tags, plus any
    paths tagged "mixed" (since those may contain multiple kinds of content).
    """
    target = set(types) | {"mixed"}
    return [p.path for p in MEDIA_LIBRARY_PATHS if p.media_type in target]
def _parse_extensions(raw: str) -> tuple[str, ...]:
    """
    Parse the LIBRARY_INDEX_EXTENSIONS env var.

    Canonical format is semicolon-separated: ``.mkv;.mp4;.avi``. This parser
    is lenient and also accepts a few common alternates so a fat-fingered .env
    edit (or a stringified tuple from an older save bug) still works:
      - Wrapped in (...) or [...]  → unwrapped
      - Comma-separated             → split on commas too
      - Single/double quotes        → stripped
      - Missing leading dot         → added
    """
    cleaned = raw.strip()
    if cleaned.startswith(("(", "[")) and cleaned.endswith((")", "]")):
        cleaned = cleaned[1:-1]
    cleaned = cleaned.replace("'", "").replace('"', "").replace(",", ";")
    out: list[str] = []
    for token in cleaned.split(";"):
        token = token.strip().lower()
        if not token:
            continue
        if not token.startswith("."):
            token = "." + token
        out.append(token)
    return tuple(out)


LIBRARY_INDEX_EXTENSIONS: tuple[str, ...] = _parse_extensions(
    os.getenv(
        "LIBRARY_INDEX_EXTENSIONS",
        ".mkv;.mp4;.avi;.mov;.wmv;.m4v;.mpg;.mpeg;.ts;.m2ts;.iso",
    )
)
# Defensive fallback: if parsing somehow yields nothing (corrupt .env), use
# the same defaults rather than silently matching zero files.
if not LIBRARY_INDEX_EXTENSIONS:
    LIBRARY_INDEX_EXTENSIONS = (
        ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
        ".mpg", ".mpeg", ".ts", ".m2ts", ".iso",
    )
LIBRARY_SEARCH_RESULT_LIMIT: int = int(
    os.getenv("LIBRARY_SEARCH_RESULT_LIMIT", "25")
)


def _assets_dir() -> Path:
    external_assets = APP_DIR / "assets"
    if external_assets.is_dir():
        return external_assets
    return BUNDLE_DIR / "assets"


ASSETS_DIR: Path = _assets_dir()
TRAY_ICON_PATH: str = str(ASSETS_DIR / "tray_icon.png")
TASKBAR_ICON_PATH: str = str(ASSETS_DIR / "taskbar_icon.png")
TRAY_EXPAND_ARROW_PATH: str = str(ASSETS_DIR / "tray_expand_arrow.png")
EXIT_MENU_ITEM_PATH: str = str(ASSETS_DIR / "exit_menu_item.png")

# ---------------------------------------------------------------------------
# External media database API keys
# Get TMDB key free at: https://www.themoviedb.org/settings/api
# Get TVDB key free at: https://thetvdb.com/api-information
# ---------------------------------------------------------------------------
TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "").strip()
TVDB_API_KEY: str = os.getenv("TVDB_API_KEY", "").strip()
# OMDB is an alternate movie DB used as an opt-in fallback when TMDB results
# don't include what the user is looking for. Free key from omdbapi.com.
OMDB_API_KEY: str = os.getenv("OMDB_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Ollama — used for fuzzy title matching and 'Other' request categorization
# The Ollama *daemon* runs locally (https://ollama.com), but note: the default
# model tag ends in ":cloud", which means Ollama relays the prompt to its
# hosted cloud service for inference — request text (user titles, including
# xanime requests) LEAVES THIS MACHINE and is processed by ollama.com under
# their privacy policy. This is a deliberate choice (no local GPU inference
# required). To keep everything on-box instead, set OLLAMA_MODEL to a local
# tag you have pulled (e.g. "llama3.1:8b") — the code needs no other change.
# OLLAMA_HOST defaults to http://localhost:11434 (standard Ollama install).
# ---------------------------------------------------------------------------
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434").strip()
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemini-3-flash-preview:cloud").strip()

# ---------------------------------------------------------------------------
# AniDB — used for xAnime (explicit anime) title lookup
# Register a free client name at: https://anidb.net/software/add
# ---------------------------------------------------------------------------
ANIDB_CLIENT: str = os.getenv("ANIDB_CLIENT", "plexrequestbot").strip()
ANIDB_CLIENT_VER: int = int(os.getenv("ANIDB_CLIENT_VER", "1"))

# ---------------------------------------------------------------------------
# Daily library check — hour of day (0-23) at which the scheduled check runs
# The check compares open requests against the Plex library and marks found ones
# ---------------------------------------------------------------------------
LIBRARY_CHECK_HOUR: int = int(os.getenv("LIBRARY_CHECK_HOUR", "3"))

# ---------------------------------------------------------------------------
# Torrent download pipeline (webtorrent engine via Node runner)
# ---------------------------------------------------------------------------
# Staging directory — every torrent downloads here first; files only move to
# a library folder when the route is confident and "move" is enabled (or the
# admin clicks Apply Route). Keeping a fixed staging dir means a confused
# route can never scatter files somewhere hard to find.
TORRENT_DOWNLOAD_DIR: str = os.getenv(
    "TORRENT_DOWNLOAD_DIR", str(APP_DIR / "downloads")
)
# Default states for the Downloads-tab checkboxes (persisted when toggled).
TORRENT_AUTO_RENAME: bool = _env_bool("TORRENT_AUTO_RENAME", False)
TORRENT_AUTO_MOVE: bool = _env_bool("TORRENT_AUTO_MOVE", False)
# When on, new bot requests are searched and grabbed automatically
# (best-seeded result). Off = admin approves each grab by hand.
TORRENT_AUTO_GRAB: bool = _env_bool("TORRENT_AUTO_GRAB", False)
# Abort a download when no data arrives for this many seconds.
TORRENT_STALL_TIMEOUT_SECONDS: int = int(
    os.getenv("TORRENT_STALL_TIMEOUT_SECONDS", "900")
)
# Path to the Node.js executable used to run the webtorrent downloader.
NODE_PATH: str = os.getenv("NODE_PATH_EXE", "node")
