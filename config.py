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


# Optional at startup: when empty, the desktop app opens the Setup Wizard
# instead of crashing, and the Telegram bot simply doesn't start until a
# token is saved. (_require() kept for callers that need a hard guarantee.)
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


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
APP_PRODUCT_NAME: str = "Plexxarr"
APP_VERSION: str = "1.1"

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
    os.getenv("PLEX_REQUEST_TIMEOUT_SECONDS", "15")
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


# ---------------------------------------------------------------------------
# Telegram-side hard reset — OFF by default. Many admins won't want remote
# users force-killing Plex mid-stream; enable from the Settings tab.
# ---------------------------------------------------------------------------
TELEGRAM_HARD_RESET_ENABLED: bool = _env_bool("TELEGRAM_HARD_RESET_ENABLED", False)

# Support link shown in the desktop app header.
KOFI_URL: str = "https://ko-fi.com/sparklemuffin"

# Hover tooltips on buttons (toggle in Settings).
TOOLTIPS_ENABLED: bool = _env_bool("TOOLTIPS_ENABLED", True)

# ---------------------------------------------------------------------------
# Optional qBittorrent delegation — when enabled, downloads go through a
# running qBittorrent instance (Web UI API) instead of the built-in
# webtorrent runner. Purely opt-in; nothing prompts for it.
# ---------------------------------------------------------------------------
QBITTORRENT_ENABLED: bool = _env_bool("QBITTORRENT_ENABLED", False)
QBITTORRENT_URL: str = os.getenv("QBITTORRENT_URL", "http://127.0.0.1:8080").strip()
QBITTORRENT_USERNAME: str = os.getenv("QBITTORRENT_USERNAME", "admin").strip()
QBITTORRENT_PASSWORD: str = os.getenv("QBITTORRENT_PASSWORD", "").strip()

# ---------------------------------------------------------------------------
# Where NEW content lands. Empty = automatic: among the configured library
# roots of the right type, pick the one on the drive with the most free
# space. Set to a specific folder (or drive root like "I:\") to hard-pin.
# ---------------------------------------------------------------------------
DOWNLOAD_ROOT_OVERRIDE: str = os.getenv("DOWNLOAD_ROOT_OVERRIDE", "").strip()

# ---------------------------------------------------------------------------
# Preferred download size, in MB per minute of runtime, per media type.
# 0 = no preference (pick by seeders alone). Used by the torrent pickers to
# prefer results whose size best matches the target, and shown in Settings as
# both MB/min and an approximate total (movies ≈ 2 h, episodes ≈ 24 min).
# Grab math anchors on each show/movie's REAL runtime when known.
# Defaults: movies 10 MB/min ≈ 1.2 GB per 2 h; episodic types 22.1 MB/min
# ≈ 530 MB per 24-min episode.
# ---------------------------------------------------------------------------
SIZE_PREF_MB_PER_MIN_MOVIE: float = float(os.getenv("SIZE_PREF_MB_PER_MIN_MOVIE", "10"))
SIZE_PREF_MB_PER_MIN_TV: float = float(os.getenv("SIZE_PREF_MB_PER_MIN_TV", "22.1"))
SIZE_PREF_MB_PER_MIN_ANIME: float = float(os.getenv("SIZE_PREF_MB_PER_MIN_ANIME", "22.1"))
SIZE_PREF_MB_PER_MIN_XANIME: float = float(os.getenv("SIZE_PREF_MB_PER_MIN_XANIME", "22.1"))

# Hard ceiling, same units (0 = no cap). Auto-grab NEVER takes a result whose
# implied MB/min exceeds this — the answer to "only one result at 2× target".
# Episodic default matches the preference: 530 MB per 24-min episode.
SIZE_MAX_MB_PER_MIN_MOVIE: float = float(os.getenv("SIZE_MAX_MB_PER_MIN_MOVIE", "0"))
SIZE_MAX_MB_PER_MIN_TV: float = float(os.getenv("SIZE_MAX_MB_PER_MIN_TV", "22.1"))
SIZE_MAX_MB_PER_MIN_ANIME: float = float(os.getenv("SIZE_MAX_MB_PER_MIN_ANIME", "22.1"))
SIZE_MAX_MB_PER_MIN_XANIME: float = float(os.getenv("SIZE_MAX_MB_PER_MIN_XANIME", "22.1"))

# Never auto-download cam/telesync releases (movies). Manual grabs still obey
# the user's click.
BLOCK_CAMS: bool = _env_bool("BLOCK_CAMS", True)

# Movies with less than this MB per minute of runtime are "low quality" in
# the Library tab's quality scan.
LOW_QUALITY_MB_PER_MIN: float = float(os.getenv("LOW_QUALITY_MB_PER_MIN", "5"))

# Subtitle language for the Library tab's "Find Subtitles" (ISO 639-1).
SUBTITLE_LANGUAGE: str = os.getenv("SUBTITLE_LANGUAGE", "en").strip()

# Which Plex account is YOU — used by the Watchlist/Recs tab.
PLEX_ACCOUNT_NAME: str = os.getenv("PLEX_ACCOUNT_NAME", "").strip()

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
# Default is gemma3:1b: ~815 MB, runs on CPU or ≤1 GB VRAM, so the app never
# makes a modest machine crawl — and Ollama unloads idle models after ~5 min
# anyway. The LLM's jobs here are tiny (pick a title from a list, classify a
# request), and rapidfuzz covers for it when it's absent or wrong.
# Quality ladder if the box can afford more:
#   qwen2.5:0.5b (~400 MB, ultra-light) < gemma3:1b (default) <
#   llama3.2:3b (~2 GB) < any ":cloud" tag (no local load, but requires a
#   free ollama.com account + `ollama signin`, and request text leaves the
#   machine — processed by ollama.com under their privacy policy).
# OLLAMA_HOST defaults to http://localhost:11434 (standard Ollama install).
# ---------------------------------------------------------------------------
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434").strip()
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma3:1b").strip()

# ---------------------------------------------------------------------------
# AniDB — used for xAnime (explicit anime) title lookup
# Register a free client name at: https://anidb.net/software/add
# ---------------------------------------------------------------------------
ANIDB_CLIENT: str = os.getenv("ANIDB_CLIENT", "plexrequestbot").strip()
ANIDB_CLIENT_VER: int = int(os.getenv("ANIDB_CLIENT_VER", "1"))

# ---------------------------------------------------------------------------
# Overnight pre-caching — while the app sits idle, quietly refresh every
# expensive scan (index delta, inventory, duplicates, sanitize preview,
# junk, unindexed, missing episodes, movie-quality probe) so each tool
# opens instantly from cache the next day. Skipped while downloads run.
# ---------------------------------------------------------------------------
IDLE_CACHE_ENABLED: bool = _env_bool("IDLE_CACHE_ENABLED", True)
IDLE_CACHE_HOUR: int = int(os.getenv("IDLE_CACHE_HOUR", "4"))

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
# Built-in engine queue: at most this many downloads run at once; the rest
# wait as 'queued'. 3-5 is the sweet spot — more just splits bandwidth and
# stalls everything (the 22-at-once request burst proved it). qBittorrent
# mode uses qBit's own queueing instead.
MAX_ACTIVE_DOWNLOADS: int = int(os.getenv("MAX_ACTIVE_DOWNLOADS", "4"))
# A running download that makes NO progress for this many minutes gets
# rotated back to the queue when something else is waiting.
DOWNLOAD_SLOW_ROTATE_MINUTES: int = int(os.getenv("DOWNLOAD_SLOW_ROTATE_MINUTES", "10"))
# Path to the Node.js executable used to run the webtorrent downloader.
NODE_PATH: str = os.getenv("NODE_PATH_EXE", "node")

# ---------------------------------------------------------------------------
# Shows auto-grab — the full Sonarr-style loop
# ---------------------------------------------------------------------------
# When on, missing episodes of tracked shows are searched and grabbed
# automatically (rename+move forced on; routing is deterministic for tracked
# episodes). Toggled from the Shows tab.
SHOWS_AUTO_GRAB: bool = _env_bool("SHOWS_AUTO_GRAB", False)
# Max episodes grabbed per pass (politeness cap toward the torrent sources).
SHOWS_GRAB_LIMIT_PER_PASS: int = int(os.getenv("SHOWS_GRAB_LIMIT_PER_PASS", "8"))
# Re-sync a show's episode list when older than this before auto-grabbing.
SHOWS_SYNC_MAX_AGE_HOURS: int = int(os.getenv("SHOWS_SYNC_MAX_AGE_HOURS", "12"))
