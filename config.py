import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, cast


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
PLEX_LIBRARY_PATHS: list[str] = _split_semicolon_list(
    os.getenv("PLEX_LIBRARY_PATHS", "")
)
LIBRARY_INDEX_EXTENSIONS: tuple[str, ...] = tuple(
    ext.lower()
    for ext in _split_semicolon_list(
        os.getenv(
            "LIBRARY_INDEX_EXTENSIONS",
            ".mkv;.mp4;.avi;.mov;.wmv;.m4v;.mpg;.mpeg;.ts;.m2ts;.iso",
        )
    )
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
