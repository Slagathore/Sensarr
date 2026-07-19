import datetime
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, cast

import config
from app_logging import get_recent_logs
from library_index import (format_library_summary_message,
                           format_reindex_result_message,
                           rebuild_library_index, search_library)
from metrics_report import format_combined_metrics_message
from plex_auth import (PlexPinSession, PlexTokenResult, launch_auth_browser,
                       save_plex_credentials, start_plex_pin_login,
                       wait_for_plex_token)
import plex_control
from plex_control import get_status, hard_reset, is_plex_running, launch_plex
from maintenance import (
    DuplicateGroup, JunkFile, LibraryInventory, MissingEpisode,
    MissingEpisodesReport, MovieInventory, SanitizePair, SeasonSummary,
    ShowInventory, UnindexedFile, apply_sanitization, daily_library_check,
    delete_files_with_cleanup, find_duplicates, find_junk_files,
    find_missing_episodes, find_unindexed_files, library_inventory,
    media_type_for_path, sanitize_all, sanitize_filename,
)
from queue_store import (complete_request, find_duplicate_requests,
                          get_request, initialize_queue_db, list_requests,
                          open_request_count)
from settings_store import (load_current_settings, reload_config_from_env,
                             save_settings)
from telegram_service import TelegramBotService

import anime_db
import auth_store
import downloads_store
import json_cache
import maint_jobs
import movie_migration
import request_intake
import shows_store
import telegram_service
import torrent_routing
from download_manager import DownloadManager
from grab_queue_tab import GrabQueueTab
from health import format_health_report
from shows_tab import ShowsTab
from torrent_search import TorrentResult, format_size, search_torrents
from watchlist_tab import WatchlistTab
from ui_helpers import add_tooltip, make_sortable

logger = logging.getLogger(__name__)

# Sentinel marking the Firefox controller as "not yet resolved" (distinct from
# None, which means "Firefox not found, fall back to the default browser").
_FIREFOX_UNSET: Any = object()

# Torrent-source search endpoints, keyed by request media_type. {q} is replaced
# with the URL-encoded search query.
#   - movie / tv / other: The Pirate Bay (see screenshot 1; form action "/s/")
#   - anime: nyaa.si filtered to "Anime - English-translated" (c=1_2, screenshot 2)
#   - xanime (🔞): sukebei.nyaa.si, the adult counterpart of nyaa
_TPB_SEARCH = "https://thepiratebay10.info/s/?q={q}&page=0&orderby=99"
_NYAA_ANIME_SEARCH = "https://nyaa.si/?f=0&c=1_2&q={q}&s=seeders&o=desc"
_SUKEBEI_SEARCH = "https://sukebei.nyaa.si/?f=0&c=0_0&q={q}&s=seeders&o=desc"

_SOURCE_SEARCH_BY_TYPE = {
    "movie":  _TPB_SEARCH,
    "tv":     _TPB_SEARCH,
    "other":  _TPB_SEARCH,
    "unknown": _TPB_SEARCH,
    "anime":  _NYAA_ANIME_SEARCH,
    "xanime": _SUKEBEI_SEARCH,
}

# Task E — media-type choices offered by the resolve-identity dialog's
# selector. "auto" maps to media_type=None, i.e. request_intake.
# search_candidates' own default (movie + tv); picking anime/xanime is what
# makes those branches of search_candidates reachable from the UI at all.
_RESOLVE_DIALOG_TYPES = ("auto", "movie", "tv", "anime", "xanime")
# Sun Valley ttk theme (dark). Optional — the app falls back to the stock
# Windows ttk look if the package isn't installed.
try:
    import sv_ttk
except ImportError:
    sv_ttk = None

# Status-indicator palette (also readable on the stock light theme).
_DOT_GREEN = "#3fb950"
_DOT_RED = "#f85149"
_DOT_AMBER = "#d29922"
_DOT_GRAY = "#8b949e"
# Muted helper-text color — depends on whether the dark theme is active.
_MUTED_TEXT = "#9a9a9a" if sv_ttk is not None else "#444"

# Tray stack (PIL + pystray) is lazy/optional now (Task H item 3): a missing
# Linux backend leaves the main window usable with a visible "tray
# unavailable" note instead of an ImportError at startup. Windows installs
# always have both packages, so nothing changes there.
import platform_adapter

_TRAY = platform_adapter.tray_support()
PillowImage = Any


def _local_ts(ts: str) -> str:
    """Stored-UTC timestamp rendered local — canonical impl in ui_helpers."""
    from ui_helpers import local_ts
    return local_ts(ts)


def _resolve_dialog_search(
    query: str, media_type: str | None, *, search_fn=None,
) -> tuple[list, str | None]:
    """Pure query/retry logic behind the resolve-identity dialog (Task E).

    Module-level and Tk-free on purpose: this is the piece the dialog calls
    on open and on every 'Search again', and it's the piece unit tests can
    drive without a display. Runs one candidate search and never raises.

    Returns (candidates, error). error is set only when the lookup itself
    blew up (network/parsing exception) — a clean empty result is
    ([], None) so the dialog renders 'No matches' inline and lets the user
    edit the query, instead of dead-ending in a popup.

    `search_fn` defaults to request_intake.search_candidates and is
    injectable so tests can supply a stub instead of hitting the network.
    """
    fn = search_fn or request_intake.search_candidates
    q = (query or "").strip()
    if not q:
        return [], None
    try:
        return list(fn(q, media_type) or []), None
    except Exception:
        logger.exception(
            "Resolve-dialog search failed for %r (media_type=%s)", q, media_type)
        return [], "Search failed — check the spelling and try again."


class DesktopApp:
    def __init__(self) -> None:
        self.bot_service = TelegramBotService()
        self.root = tk.Tk()
        self.root.title(f"Sensarr v{config.APP_VERSION}")
        try:
            # Taskbar/alt-tab icon — same mark as the tray and the EXE.
            from PIL import ImageTk
            from app_icon import icon_image
            self._window_icon = ImageTk.PhotoImage(icon_image(64))
            self.root.iconphoto(True, self._window_icon)
        except Exception:
            logger.debug("Window icon unavailable; Tk default kept.", exc_info=True)
        # Task H: 860x620/760x520 squished 11 top-level tabs against sv-ttk's
        # default (image-based) tab padding — the old "Watchlist" label (it
        # carried a "/Recs" suffix) and "Maintenance" were clipping at the
        # old default, let alone minsize.
        self.root.geometry("1100x720")
        self.root.minsize(900, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        if sv_ttk is not None:
            try:
                sv_ttk.set_theme("dark")
            except Exception:
                logger.warning("Failed to apply sv-ttk theme; using default look.")
        # Destructive actions get their own button style so they can't be
        # mistaken for a harmless refresh.
        style = ttk.Style(self.root)
        style.configure("Danger.TButton", foreground=_DOT_RED, font=("Segoe UI", 9, "bold"))
        # Task H item 2: sv-ttk's dark theme bakes a wide default padding
        # {16 14 16 6} into its custom tab background element (Notebook.tab,
        # sv_ttk/theme/dark.tcl) — the sole reason 11 tabs squished. TNotebook
        # .Tab's own -padding resource is read by the built-in Notebook.padding
        # sub-element ttk nests inside that background, so overriding it here
        # (after set_theme, so this configure isn't clobbered by theme setup)
        # tightens every tab without touching sv-ttk's rounded-corner artwork.
        style.configure("TNotebook.Tab", padding=(8, 4))
        initialize_queue_db()
        # Run auth migrations even when the Telegram bot isn't configured —
        # the Users tab reads these tables regardless.
        auth_store.initialize_auth_db()
        # Maintenance run journal (Task I): create the table and mark any
        # run persisted as running/queued by a previous process interrupted —
        # never pretend a daemon thread survived a restart.
        maint_jobs.initialize_maint_jobs_db()

        self.status_var = tk.StringVar(value="Starting...")
        self.bot_status_var = tk.StringVar(value="Telegram bot: starting")
        self.last_action_var = tk.StringVar(value="Last action: none")
        self.queue_var = tk.StringVar(value="Open requests: 0")
        self.library_var = tk.StringVar(value="Library index: 0 files")
        self.requester_var = tk.StringVar(value="Admin")
        self.request_content_var = tk.StringVar()
        self.library_search_var = tk.StringVar()

        self.status_text: scrolledtext.ScrolledText | None = None
        self.log_text: scrolledtext.ScrolledText | None = None
        self.requests_tree: ttk.Treeview | None = None
        self.request_detail_text: scrolledtext.ScrolledText | None = None
        # Cached Firefox controller for opening source searches. Sentinel
        # value below means "not yet resolved"; None means "Firefox unavailable".
        self._firefox_browser_cache: Any = _FIREFOX_UNSET
        self.library_summary_text: scrolledtext.ScrolledText | None = None
        self.library_results_tree: ttk.Treeview | None = None
        self.metrics_text: scrolledtext.ScrolledText | None = None
        # Maintenance tab state
        self._maint_results: list[Any] = []
        self._maint_tool_name: str = ""
        # False while the overnight pre-cache pass runs — result handlers
        # skip their popups so nothing interrupts an unattended machine.
        self._maint_popups_ok = True
        self._last_idle_cache_date: str = ""
        # Per-tool result cache: switching to another tool and back re-renders
        # the last results instantly instead of re-running the walk. Loaded
        # from disk so results survive app restarts (🔄 Re-run refreshes).
        self._maint_cache: dict[str, dict[str, Any]] = {}
        self._maint_load_cache()
        self._maint_tree: ttk.Treeview | None = None
        self._maint_status_var = tk.StringVar(value="Select a tool to run.")
        # Task I: every Maintenance tool runs through the process-wide job
        # registry — one at a time, journalled in SQLite, cancellable, and
        # the UI rebuilds its progress display from the registry on demand
        # (so a tab switch never loses a run).
        self._maint_registry = maint_jobs.get_registry()
        self._maint_registry.subscribe(self._on_maint_job_event)
        self._maint_job_indicator_var = tk.StringVar(value="")
        self._maint_banner_var = tk.StringVar(value="")
        self._maint_phase_var = tk.StringVar(value="")
        self._maint_elapsed_var = tk.StringVar(value="")
        self._maint_queue_var = tk.StringVar(value="")
        self._maint_progressbar: ttk.Progressbar | None = None
        self._maint_cancel_btn: ttk.Button | None = None
        self._maint_banner_label: ttk.Label | None = None
        self._maint_bar_indeterminate = False
        self._maint_tick_scheduled = False
        # Job ids submitted by the overnight idle pass — popups stay
        # suppressed until the last of them finishes.
        self._maint_idle_pending: set[int] = set()
        # Task B item 2: what the "custom_rename" grid was last built FROM,
        # so 🔄 Re-run can replay it with the same parameters instead of
        # popping a "can't re-run this" no-op. Either
        # {"kind": "combo_clean", "media_type": ...} or
        # {"kind": "custom_dialog", "rule": ..., "pattern": ..., "allowed_tags": ...}.
        self._maint_custom_rename_replay: dict[str, Any] | None = None
        # Typed rows for re-rendering after a filter toggle.
        # Each entry: (media_type_tag, row_values, action_payload).
        # action_payload is opaque per-tool — for find_duplicates it's the
        # file path so Apply Selected can delete the right files; for
        # sanitize it's the SanitizePair index; for missing_episodes a
        # ("grab_ep", …) tuple. Summary rows carry None (not actionable).
        self._maint_typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = []
        # Visible-row state keyed by tree iid — sorting reorders rows without
        # breaking the checkbox ↔ payload mapping.
        self._maint_row_state: dict[str, tuple[tk.BooleanVar, Any]] = {}
        self._maint_default_checked = False
        # Filter checkbox state — one BooleanVar per media type tag.
        self._maint_filter_vars: dict[str, tk.BooleanVar] = {
            tag: tk.BooleanVar(value=True) for tag in ("movie", "tv", "anime", "xanime", "mixed")
        }
        # Downloads tab state
        self.download_manager = DownloadManager(on_update=self._on_download_update)
        self._dl_search_var = tk.StringVar()
        self._dl_type_var = tk.StringVar(value="movie")
        self._dl_plan_var = tk.StringVar(value="Select a search result to preview its destination.")
        self._dl_status_var = tk.StringVar(value="")
        self._dl_auto_rename_var = tk.BooleanVar(value=config.TORRENT_AUTO_RENAME)
        self._dl_auto_move_var = tk.BooleanVar(value=config.TORRENT_AUTO_MOVE)
        self._dl_auto_grab_var = tk.BooleanVar(value=config.TORRENT_AUTO_GRAB)
        self._dl_results: list[TorrentResult] = []
        self._dl_request_context: tuple[int, str] | None = None  # (request_id, title)
        self._dl_episode_context: tuple[int, int, int] | None = None  # (show_id, season, ep)
        self._last_shows_grab_pass = 0.0
        self._dl_results_tree: ttk.Treeview | None = None
        self._dl_downloads_tree: ttk.Treeview | None = None
        self._dl_history_tree: ttk.Treeview | None = None
        # Users tab state
        self._users_pending_tree: ttk.Treeview | None = None
        self._users_allowed_tree: ttk.Treeview | None = None
        self._notebook: ttk.Notebook | None = None
        self._users_tab: ttk.Frame | None = None
        self._downloads_tab: ttk.Frame | None = None
        # Settings tab state — declared in _build_settings_tab
        self._settings_vars: dict[str, tk.Variable] = {}
        self._size_pref_keys: list[str] = []
        self._settings_paths: list[tuple[str, str]] = []  # (path, media_type)
        self._settings_paths_tree: ttk.Treeview | None = None
        self._settings_status_var = tk.StringVar(value="")
        self._last_library_check_date: str = ""
        self._last_log_count = 0
        self._tray_icon = self._build_tray_icon()
        self._quitting = False
        self._library_summary_refresh_running = False
        self._library_summary_refresh_pending = False
        self._library_metrics_refresh_running = False
        self._library_metrics_refresh_pending = False

        self._build_ui()
        self._apply_dark_widget_styles(self.root)
        # New Telegram access requests refresh the Users tab immediately (the
        # callback fires on the bot thread; marshal to the UI thread).
        telegram_service.set_on_access_request(
            lambda: self._post_to_ui(self.refresh_users_tab)
        )

    def _apply_dark_widget_styles(self, root_widget: tk.Misc) -> None:
        """Restyle plain-tk widgets (Text/ScrolledText/Canvas) for the dark theme.

        ttk widgets follow the sv-ttk theme automatically, but tk.Text and
        tk.Canvas keep their stock white backgrounds, which looks broken on a
        dark window. Walk the tree once after building the UI (and call again
        for any later Toplevel, e.g. the Custom Rename dialog).
        """
        if sv_ttk is None:
            return

        def walk(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if isinstance(child, tk.Text):
                    child.configure(
                        bg="#1e1e1e", fg="#e8e8e8",
                        insertbackground="#e8e8e8",
                        selectbackground="#264f78",
                        relief=tk.FLAT, borderwidth=1,
                    )
                elif isinstance(child, tk.Listbox):
                    child.configure(
                        bg="#1e1e1e", fg="#e8e8e8",
                        selectbackground="#264f78",
                        selectforeground="#ffffff",
                        relief=tk.FLAT, borderwidth=1,
                        highlightthickness=0,
                    )
                elif isinstance(child, tk.Canvas):
                    child.configure(bg="#1c1c1c", highlightthickness=0)
                walk(child)

        walk(root_widget)

    def _screenshot_tour(self) -> None:
        """Docs capture (SENSARR_SHOT_DIR set): maximize, walk the tabs,
        save one PNG per stop, then exit. Drives the real app so the README
        screenshots always show live data."""
        import os
        import time as _time
        from PIL import ImageGrab

        outdir = Path(os.environ["SENSARR_SHOT_DIR"])
        outdir.mkdir(parents=True, exist_ok=True)
        self.root.state("zoomed")
        self.root.lift()
        self.root.focus_force()

        def settle(seconds: float) -> None:
            self.root.update()
            _time.sleep(seconds)
            self.root.update()

        def grab(name: str, widget=None) -> None:
            target = widget if widget is not None else self.root
            x, y = target.winfo_rootx(), target.winfo_rooty()
            w, h = target.winfo_width(), target.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
            img.save(outdir / f"{name}.png")

        nb = self._notebook
        tabs = {nb.tab(t, "text"): t for t in nb.tabs()} if nb is not None else {}

        def show_tab(label: str, wait: float = 2.0) -> bool:
            if nb is None or label not in tabs:
                return False
            nb.select(tabs[label])
            settle(wait)
            return True

        settle(1.5)
        try:
            if show_tab("Status", 3.0):
                grab("status")
            if show_tab("Shows", 2.5):
                grab("shows")
            if show_tab("Downloads"):
                grab("downloads")
            if show_tab("Library"):
                try:
                    self._lib_show_all()
                except Exception:
                    logger.exception("Library listing failed during capture")
                tree = getattr(self, "library_results_tree", None)
                for _ in range(20):  # the full listing loads on a thread
                    settle(1.5)
                    if tree is not None and tree.get_children():
                        break
                settle(1.0)
                grab("library")
            if show_tab("Settings"):
                canvas = getattr(self, "_settings_canvas", None)
                frame = getattr(self, "_size_pref_frame", None)
                inner = getattr(self, "_settings_inner", None)
                if canvas is not None and frame is not None and inner is not None:
                    canvas.yview_moveto(
                        frame.winfo_y() / max(1, inner.winfo_reqheight()))
                    settle(1.0)
                    grab("settings", frame)
                    grab("settings_full")
            wiz = self.open_setup_wizard()
            settle(1.5)
            grab("wizard", wiz)
            wiz.destroy()
        finally:
            logger.info("Screenshot tour complete -> %s", outdir)
            os._exit(0)

    def run(self) -> None:
        if not config.TELEGRAM_BOT_TOKEN:
            # Fresh install: no crash, no bot — walk the user through setup.
            self.bot_status_var.set("Telegram bot: not configured")
            self._bot_dot.configure(foreground=_DOT_AMBER)
            self.root.after(400, self.open_setup_wizard)
        else:
            try:
                self.bot_service.start()
                self.bot_status_var.set("Telegram bot: running")
                self._bot_dot.configure(foreground=_DOT_GREEN)
            except Exception as exc:
                logger.exception("Failed to start Telegram bot service.")
                self.bot_status_var.set(f"Telegram bot: failed ({exc})")
                self._bot_dot.configure(foreground=_DOT_RED)
                messagebox.showerror(
                    "Telegram bot failed",
                    f"Could not start the Telegram bot service.\n\n{exc}",
                )

        if self._tray_icon is not None:
            try:
                self._tray_icon.run_detached()
            except Exception:
                logger.warning("Tray icon failed to start — window-only mode.",
                               exc_info=True)
                self._tray_icon = None
        self.root.after(0, self._initialize_runtime_state)
        # Legacy-path migration (Task H item 2) is offered in main.py BEFORE
        # this class constructs — __init__ initializes databases at the XDG
        # destination, which would otherwise block the main DB's copy.
        import os as _os
        if _os.environ.get("SENSARR_SHOT_DIR"):
            self.root.after(12000, self._screenshot_tour)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            logger.info("Received Ctrl+C from terminal. Shutting down desktop app.")
            self.shutdown_from_terminal()

    def _initialize_runtime_state(self) -> None:
        if self._quitting:
            return
        self.refresh_status()
        self.refresh_requests()
        self.refresh_library_summary()
        self.refresh_library_metrics()
        self.refresh_downloads()
        self.refresh_users_tab()
        self.shows_tab.refresh()
        self._schedule_status_refresh()
        self._schedule_log_refresh()
        self._schedule_daily_library_check()
        self._schedule_auto_grab()
        self._schedule_idle_cache()
        self._schedule_midnight_rollover()
        # Local anime metadata (manami + anime-lists dumps): build/refresh in
        # the background when missing or older than a week.
        anime_db.ensure_fresh(background=True)

    def _schedule_midnight_rollover(self) -> None:
        """At 00:05 every day: re-render Upcoming (yesterday's box must not
        linger) and kick an auto-grab pass so keep-at-100% shows get their
        just-aired episodes without waiting for the 6-hour cadence."""
        if self._quitting:
            return
        now = datetime.datetime.now()
        target = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0)
        self.root.after(int((target - now).total_seconds() * 1000),
                        self._midnight_rollover_tick)

    def _midnight_rollover_tick(self) -> None:
        if self._quitting:
            return
        logger.info("Midnight rollover: refreshing Upcoming + auto-grab kick.")
        self.shows_tab.refresh()
        self._last_shows_grab_pass = 0.0  # let the next auto-grab tick run now

        def worker() -> None:
            try:
                started = self.download_manager.auto_grab_missing_episodes()
                if started:
                    self._post_to_ui(self.refresh_downloads)
            except Exception:
                logger.exception("Midnight auto-grab pass failed.")

        if config.SHOWS_AUTO_GRAB or any(
                s.auto_grab or s.follow_new
                for s in __import__("shows_store").list_shows()):
            threading.Thread(target=worker, name="midnight-grab", daemon=True).start()
        self._schedule_midnight_rollover()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        header = ttk.Frame(self.root, padding=16)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="Sensarr",
            font=("Segoe UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")

        # Bot + Plex status lines with colored ● indicators.
        bot_row = ttk.Frame(header)
        bot_row.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._bot_dot = ttk.Label(bot_row, text="●", foreground=_DOT_AMBER)
        self._bot_dot.grid(row=0, column=0, padx=(0, 6))
        ttk.Label(bot_row, textvariable=self.bot_status_var).grid(row=0, column=1)

        plex_row = ttk.Frame(header)
        plex_row.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._plex_dot = ttk.Label(plex_row, text="●", foreground=_DOT_GRAY)
        self._plex_dot.grid(row=0, column=0, padx=(0, 6))
        self.plex_status_var = tk.StringVar(value="Plex: checking…")
        ttk.Label(plex_row, textvariable=self.plex_status_var).grid(row=0, column=1)

        ttk.Label(header, textvariable=self.last_action_var).grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.queue_var).grid(row=4, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.library_var).grid(row=5, column=0, sticky="w", pady=(4, 0))
        # Task I item 5: a running maintenance job stays visible OUTSIDE the
        # Maintenance tab — this header line shows label + live counts and
        # clears itself when the registry goes idle.
        ttk.Label(header, textvariable=self._maint_job_indicator_var,
                  foreground=_DOT_AMBER).grid(row=6, column=0, sticky="w", pady=(4, 0))
        if not _TRAY.available:
            # Task H item 3: a missing tray backend is a visible state, not
            # a crash. Closing the window minimizes instead of hiding.
            ttk.Label(header, foreground=_DOT_AMBER, wraplength=780,
                      text=("Tray unavailable — the window minimizes instead "
                            f"of hiding to the tray. ({_TRAY.reason})")
                      ).grid(row=7, column=0, sticky="w", pady=(4, 0))

        # Ko-fi support link — sits above the action buttons.
        kofi_row = ttk.Frame(self.root, padding=(16, 0, 16, 4))
        kofi_row.grid(row=1, column=0, sticky="ew")
        kofi_label = ttk.Label(
            kofi_row,
            text=("😺 Like the app? You could help me get more catnip for my "
                  "many, many cats — they NEED their zoomies! 🐈"),
            foreground="#ff5f5f", cursor="hand2",
            font=("Segoe UI", 12, "underline"),
        )
        kofi_label.pack(side=tk.LEFT)
        kofi_label.bind("<Button-1>", lambda _e: webbrowser.open_new_tab(config.KOFI_URL))
        add_tooltip(kofi_label, "Opens my Ko-fi page (ko-fi.com/sparklemuffin). "
                                "Zero pressure — the cats are fine. Probably. 😼")

        actions = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        actions.grid(row=2, column=0, sticky="ew")
        for index in range(4):
            actions.columnconfigure(index, weight=1)

        # Task H item 5: local process controls only claim to work when a
        # local Plex is actually reachable. On Windows this is always True
        # (unchanged behavior); on Linux a remote-only config gets disabled
        # buttons with the reason as the tooltip — not a "broken" server.
        local_ok, local_reason = plex_control.local_control_available()
        for col, (text, style_name, command, tip, needs_local) in enumerate((
            ("Launch Plex", "Accent.TButton",
             lambda: self.run_action("Launch Plex", launch_plex),
             "Start Plex Media Server from its configured executable path.",
             True),
            ("⚠ Hard Reset", "Danger.TButton", self.confirm_hard_reset,
             "Force-kill every Plex process and relaunch. Interrupts anyone currently watching — asks for confirmation.",
             True),
            ("Refresh Status", "", self.refresh_status,
             "Re-check whether Plex is running and update the Status tab.",
             False),
            ("Get Plex Token", "", self.authenticate_plex_account,
             "Open the Plex PIN login in your browser and save the API token to .env automatically.",
             False),
        )):
            btn = (ttk.Button(actions, text=text, style=style_name, command=command)
                   if style_name else ttk.Button(actions, text=text, command=command))
            btn.grid(row=0, column=col, padx=4, sticky="ew")
            if needs_local and not local_ok:
                btn.state(["disabled"])
                tip = f"Disabled: {local_reason}"
            add_tooltip(btn, tip)

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=3, column=0, padx=16, pady=(0, 12), sticky="nsew")

        status_tab = ttk.Frame(notebook, padding=12)
        requests_tab = ttk.Frame(notebook, padding=12)
        downloads_tab = ttk.Frame(notebook, padding=12)
        shows_tab = ttk.Frame(notebook, padding=12)
        watchlist_tab = ttk.Frame(notebook, padding=12)
        library_tab = ttk.Frame(notebook, padding=12)
        metrics_tab = ttk.Frame(notebook, padding=12)
        maintenance_tab = ttk.Frame(notebook, padding=12)
        users_tab = ttk.Frame(notebook, padding=12)
        settings_tab = ttk.Frame(notebook, padding=12)
        logs_tab = ttk.Frame(notebook, padding=12)
        notebook.add(status_tab, text="Status")
        notebook.add(requests_tab, text="Requests")
        notebook.add(downloads_tab, text="Downloads")
        notebook.add(shows_tab, text="Shows")
        notebook.add(watchlist_tab, text="Watchlist")
        notebook.add(library_tab, text="Library")
        notebook.add(metrics_tab, text="Metrics")
        notebook.add(maintenance_tab, text="Maintenance")
        notebook.add(users_tab, text="Users")
        notebook.add(settings_tab, text="Settings")
        notebook.add(logs_tab, text="Logs")
        self._notebook = notebook
        self._users_tab = users_tab
        self._downloads_tab = downloads_tab
        self._maintenance_tab = maintenance_tab
        self._settings_tab = settings_tab
        # Task I item 5: returning to the Maintenance tab repaints the
        # progress strip from the job registry — a run started earlier is
        # still there, mid-run, with live counts.
        notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _e: (self._maint_sync_from_registry()
                        if notebook.select() == str(maintenance_tab)
                        else None),
            add="+")
        self._build_downloads_tab(downloads_tab)
        self._build_users_tab(users_tab)
        # Tabs split out of this class into their own modules — the pattern
        # new tabs should follow.
        self.shows_tab = ShowsTab(shows_tab, self)
        self.watchlist_tab = WatchlistTab(watchlist_tab, self)

        status_tab.columnconfigure(0, weight=1)
        status_tab.rowconfigure(4, weight=1)
        # Requests hosts two SUBTABS (Task E): the classic request list and
        # the grab queue. The sub-notebook fills the whole tab; the classic
        # widgets below are parented into requests_list_tab.
        requests_tab.columnconfigure(0, weight=1)
        requests_tab.rowconfigure(0, weight=1)
        requests_nb = ttk.Notebook(requests_tab)
        requests_nb.grid(row=0, column=0, sticky="nsew")
        requests_list_tab = ttk.Frame(requests_nb, padding=6)
        grab_queue_frame = ttk.Frame(requests_nb, padding=6)
        requests_nb.add(requests_list_tab, text="Requests")
        requests_nb.add(grab_queue_frame, text="Grab queue")
        requests_list_tab.columnconfigure(0, weight=1)
        requests_list_tab.rowconfigure(1, weight=1)
        requests_list_tab.rowconfigure(2, weight=0)
        self.grab_queue_tab = GrabQueueTab(grab_queue_frame, self)
        requests_nb.bind(
            "<<NotebookTabChanged>>",
            lambda _e: (self.grab_queue_tab.refresh()
                        if requests_nb.index("current") == 1 else None))
        library_tab.columnconfigure(0, weight=1)
        library_tab.rowconfigure(2, weight=1)
        metrics_tab.columnconfigure(0, weight=1)
        metrics_tab.rowconfigure(1, weight=1)
        maintenance_tab.columnconfigure(0, weight=1)
        # Rows: 0 toolbar, 1 status, 2 progress strip, 3 filters, 4 results.
        logs_tab.columnconfigure(0, weight=1)
        logs_tab.rowconfigure(0, weight=1)

        # Update banner — hidden until the nightly check finds a release.
        self._update_banner = ttk.Frame(status_tab, padding=(8, 4))
        self._update_banner.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self._update_banner.grid_remove()
        self._update_banner_label = ttk.Label(
            self._update_banner, text="", font=("Segoe UI", 10, "bold"))
        self._update_banner_label.pack(side=tk.LEFT, padx=(0, 10))
        self._update_install_btn = ttk.Button(
            self._update_banner, text="Install update",
            command=self._install_update)
        self._update_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(self._update_banner, text="What's new",
                   command=lambda: webbrowser.open_new_tab(
                       self._update_info.html_url) if self._update_info else None
                   ).pack(side=tk.LEFT, padx=(0, 6))
        self._update_dismiss_btn = ttk.Button(
            self._update_banner, text="Dismiss",
            command=lambda: self._update_banner.grid_remove())
        self._update_dismiss_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._update_skip_btn = ttk.Button(
            self._update_banner, text="Skip this version",
            command=self._skip_update_version)
        self._update_skip_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._update_mute_btn = ttk.Button(
            self._update_banner, text="Mute update notices",
            command=self._mute_update_notices)
        self._update_mute_btn.pack(side=tk.LEFT)
        self._update_info = None
        self._urgent_popup_shown = False

        status_toolbar = ttk.Frame(status_tab)
        status_toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        health_btn = ttk.Button(status_toolbar, text="🩺 Health Check",
                                command=lambda: self.run_health_check(include_updates=False))
        health_btn.grid(row=0, column=0, padx=(0, 6))
        add_tooltip(health_btn, "Check every dependency: Plex, bot, API keys, Node, "
                                "torrent runner, library paths, disk space, Ollama.")
        update_btn = ttk.Button(status_toolbar, text="⬆ Health + Update Check",
                                command=lambda: self.run_health_check(include_updates=True))
        update_btn.grid(row=0, column=1, padx=(0, 6))
        add_tooltip(update_btn, "Health check plus: is a newer Plex Media Server or app "
                                "release available?")
        plex_btn = ttk.Button(status_toolbar, text="Plex Status", command=self.refresh_status)
        plex_btn.grid(row=0, column=2)
        add_tooltip(plex_btn, "Show which Plex processes are running right now.")

        # Frozen header: the vitals stay pinned while activity streams below.
        vitals = ttk.Frame(status_tab)
        vitals.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self._status_plex_dot = ttk.Label(vitals, text="●", foreground=_DOT_GRAY)
        self._status_plex_dot.pack(side=tk.LEFT)
        ttk.Label(vitals, textvariable=self.plex_status_var).pack(side=tk.LEFT, padx=(4, 16))
        self._status_bot_dot = ttk.Label(vitals, text="●", foreground=_DOT_AMBER)
        self._status_bot_dot.pack(side=tk.LEFT)
        ttk.Label(vitals, textvariable=self.bot_status_var).pack(side=tk.LEFT, padx=(4, 16))
        self._activity_summary_var = tk.StringVar(value="")
        ttk.Label(vitals, textvariable=self._activity_summary_var,
                  font=("Segoe UI", 9, "italic")).pack(side=tk.LEFT)

        # "Needs you" line: every count that means the user has something to do,
        # so it is visible without hunting through tabs. Amber when nonzero.
        needs_row = ttk.Frame(status_tab)
        needs_row.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        self._needs_you_var = tk.StringVar(value="")
        self._needs_you_label = ttk.Label(
            needs_row, textvariable=self._needs_you_var,
            font=("Segoe UI", 10, "bold"))
        self._needs_you_label.pack(side=tk.LEFT)

        status_panes = ttk.PanedWindow(status_tab, orient=tk.VERTICAL)
        status_panes.grid(row=4, column=0, sticky="nsew")

        activity_frame = ttk.LabelFrame(
            status_panes, text="Live activity — downloads, renames, moves", padding=4)
        status_panes.add(activity_frame, weight=3)
        activity_frame.columnconfigure(0, weight=1)
        activity_frame.rowconfigure(0, weight=1)
        activity_tree = ttk.Treeview(
            activity_frame, columns=("when", "what", "detail"), show="headings")
        for col, text, width, stretch in (("when", "When", 130, False),
                                          ("what", "What", 110, False),
                                          ("detail", "Detail", 680, True)):
            activity_tree.heading(col, text=text)
            activity_tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        activity_tree.grid(row=0, column=0, sticky="nsew")
        activity_scroll = ttk.Scrollbar(activity_frame, orient=tk.VERTICAL,
                                        command=activity_tree.yview)
        activity_scroll.grid(row=0, column=1, sticky="ns")
        activity_tree.configure(yscrollcommand=activity_scroll.set)
        self._activity_tree = activity_tree

        text_frame = ttk.LabelFrame(status_panes, text="Diagnostics output", padding=4)
        status_panes.add(text_frame, weight=2)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.status_text = scrolledtext.ScrolledText(
            text_frame, wrap=tk.WORD, height=8, font=("Consolas", 10), state=tk.DISABLED,
        )
        self.status_text.grid(row=0, column=0, sticky="nsew")

        requests_toolbar = ttk.Frame(requests_list_tab)
        requests_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        requests_toolbar.columnconfigure(3, weight=1)

        ttk.Label(requests_toolbar, text="Requester").grid(row=0, column=0, sticky="w")
        ttk.Entry(requests_toolbar, textvariable=self.requester_var, width=18).grid(row=0, column=1, padx=(6, 12), sticky="w")
        ttk.Label(requests_toolbar, text="Request").grid(row=0, column=2, sticky="w")
        request_entry = ttk.Entry(requests_toolbar, textvariable=self.request_content_var)
        request_entry.grid(row=0, column=3, padx=(6, 12), sticky="ew")
        request_entry.bind("<Return>", lambda _event: self.add_request_from_ui())
        ttk.Button(requests_toolbar, text="Add", command=self.add_request_from_ui).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(requests_toolbar, text="🔍 Find Source", command=self.search_source_for_selected_request).grid(row=0, column=5, padx=(0, 6))
        ttk.Button(requests_toolbar, text="🆔 Resolve", command=self.resolve_selected_request).grid(row=0, column=9, padx=(0, 6))
        ttk.Button(requests_toolbar, text="⬇ Grab Torrent", command=self.grab_torrent_for_selected_request).grid(row=0, column=6, padx=(0, 6))
        ttk.Button(requests_toolbar, text="Complete Selected", command=self.complete_selected_request).grid(row=0, column=7, padx=(0, 6))
        ttk.Button(requests_toolbar, text="Refresh", command=self.refresh_requests).grid(row=0, column=8)

        requests_frame = ttk.Frame(requests_list_tab)
        requests_frame.grid(row=1, column=0, sticky="nsew")
        requests_frame.columnconfigure(0, weight=1)
        requests_frame.rowconfigure(0, weight=1)

        self.requests_tree = ttk.Treeview(
            requests_frame,
            columns=("id", "type", "requester", "created", "title", "status"),
            show="headings",
            height=10,
        )
        self.requests_tree.heading("id",        text="ID")
        self.requests_tree.heading("type",      text="Type")
        self.requests_tree.heading("requester", text="Requester")
        self.requests_tree.heading("created",   text="Created")
        self.requests_tree.heading("title",     text="Title / Request")
        self.requests_tree.heading("status",    text="In Library")
        self.requests_tree.column("id",        width=50,  anchor=tk.CENTER, stretch=False)
        self.requests_tree.column("type",      width=70,  anchor=tk.CENTER, stretch=False)
        self.requests_tree.column("requester", width=130, anchor=tk.W,      stretch=False)
        self.requests_tree.column("created",   width=140, anchor=tk.W,      stretch=False)
        self.requests_tree.column("title",     width=360, anchor=tk.W)
        self.requests_tree.column("status",    width=70,  anchor=tk.CENTER, stretch=False)
        self.requests_tree.grid(row=0, column=0, sticky="nsew")

        requests_scroll = ttk.Scrollbar(requests_frame, orient=tk.VERTICAL, command=self.requests_tree.yview)
        requests_scroll.grid(row=0, column=1, sticky="ns")
        self.requests_tree.configure(yscrollcommand=requests_scroll.set)

        detail_frame = ttk.LabelFrame(requests_list_tab, text="Request Detail", padding=6)
        detail_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        detail_frame.columnconfigure(0, weight=1)

        self.request_detail_text = scrolledtext.ScrolledText(
            detail_frame, wrap=tk.WORD, height=5, font=("Consolas", 9), state=tk.DISABLED,
        )
        self.request_detail_text.grid(row=0, column=0, sticky="ew")
        self.requests_tree.bind("<<TreeviewSelect>>", self._on_request_selected)
        # Double-click a request to open its torrent-source search in Firefox.
        self.requests_tree.bind("<Double-1>", self._on_request_activated)

        self._build_library_tab(library_tab)

        metrics_toolbar = ttk.Frame(metrics_tab)
        metrics_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(metrics_toolbar, text="Refresh Metrics", command=self.refresh_library_metrics).grid(row=0, column=0, sticky="w")

        self.metrics_text = scrolledtext.ScrolledText(
            metrics_tab, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED,
        )
        self.metrics_text.grid(row=1, column=0, sticky="nsew")

        # -----------------------------------------------------------------
        # Maintenance tab
        # -----------------------------------------------------------------
        maint_toolbar = ttk.Frame(maintenance_tab)
        maint_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        maint_buttons: tuple[tuple[str, Any, str], ...] = (
            ("Daily Check", self._run_daily_check,
             "Scan open requests against the library and mark the ones that are now present."),
            ("Library Inventory", self._run_library_inventory,
             "Per-show season/episode counts and sizes from the files on disk."),
            ("Find Duplicates", self._run_find_duplicates,
             "Find files that look like the same movie/episode twice. You tick the copies to DELETE."),
            ("Sanitize Names", self._run_sanitize,
             "Preview Plex-friendly renames for messy filenames. You tick the rows to RENAME."),
            ("Custom Rename...", self._open_custom_rename,
             "Bulk-rename with find/replace, regex, case, prefix/suffix, and trim rules — previews first."),
            ("Clean Junk", self._run_clean_junk,
             "Find sample videos, release-note .txt/.nfo files, screenshot images, and empty folders. "
             "You tick what to DELETE — subtitles, Plex artwork, and Extras folders are never flagged."),
            ("Missing Episodes", self._run_missing_episodes,
             "Detect numbering gaps inside seasons you already have files for."),
            ("Organize Movies", self._run_movie_migration,
             "Move flat movie files into per-movie folders named "
             "'Title (Year) {tmdb-ID}'. Dry-run preview first — you tick what "
             "moves; every move is journalled and reversible."),
            ("Unindexed Files", self._run_unindexed,
             "Files on disk that aren't in the local search index yet — fix with Reindex."),
            # Reindex runs as a registry job (Task B item 1) — shared with
            # the Library tab's own Reindex button; see
            # rebuild_library_index_from_ui / _handle_library_reindex_result.
            ("Reindex", self.rebuild_library_index_from_ui,
             "Rebuild the local file index used by /search and the Library tab."),
            ("🔄 Re-run", self._maint_rerun_current,
             "Results are cached (even across restarts) — this forces a fresh "
             "scan of the tool currently displayed."),
            ("Apply Selected", self._apply_maint_selection,
             "Perform the action on the rows you've ticked (the checkbox column says what happens)."),
        )
        for col, (text, command, tip) in enumerate(maint_buttons):
            maint_toolbar.columnconfigure(col, weight=1)
            btn = ttk.Button(maint_toolbar, text=text, command=command)
            btn.grid(row=0, column=col, padx=2, sticky="ew")
            add_tooltip(btn, tip)

        ttk.Label(maintenance_tab, textvariable=self._maint_status_var,
                  font=("Segoe UI", 11, "bold")).grid(row=1, column=0, sticky="w", pady=(0, 6))

        # Task I: run progress strip — an HONEST progressbar (determinate
        # with counts where the tool can count, indeterminate + phase +
        # ticking elapsed where it can't; never a fake percentage), a Cancel
        # button, the visible queue, and a persistent result banner. All of
        # it repaints from the job registry, so tab switches lose nothing.
        progress_row = ttk.Frame(maintenance_tab)
        progress_row.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        progress_row.columnconfigure(1, weight=1)
        self._maint_progressbar = ttk.Progressbar(
            progress_row, orient=tk.HORIZONTAL, mode="determinate",
            length=220)
        self._maint_progressbar.grid(row=0, column=0, sticky="w")
        ttk.Label(progress_row, textvariable=self._maint_phase_var).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(progress_row, textvariable=self._maint_elapsed_var,
                  foreground=_MUTED_TEXT).grid(row=0, column=2, sticky="e",
                                               padx=(8, 0))
        self._maint_cancel_btn = ttk.Button(
            progress_row, text="Cancel", command=self._maint_cancel_current,
            state=tk.DISABLED)
        self._maint_cancel_btn.grid(row=0, column=3, padx=(8, 0))
        add_tooltip(self._maint_cancel_btn,
                    "Cooperative cancel — the run stops at its next "
                    "checkpoint and the summary says how far it got.")
        history_btn = ttk.Button(progress_row, text="Run history",
                                 command=self._maint_open_run_history)
        history_btn.grid(row=0, column=4, padx=(6, 0))
        add_tooltip(history_btn,
                    "Every maintenance run ever journalled — state, timing, "
                    "progress, and summary survive app restarts.")
        ttk.Label(progress_row, textvariable=self._maint_queue_var,
                  foreground=_MUTED_TEXT).grid(row=1, column=0, columnspan=3,
                                               sticky="w", pady=(2, 0))
        self._maint_banner_label = ttk.Label(
            progress_row, textvariable=self._maint_banner_var,
            font=("Segoe UI", 9, "bold"))
        self._maint_banner_label.grid(row=2, column=0, columnspan=5,
                                      sticky="w", pady=(2, 0))

        # Media-type filter checkboxes — toggle these to hide/show rows by tag
        # without re-running the tool.
        filter_row = ttk.Frame(maintenance_tab)
        filter_row.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(filter_row, text="Show:").pack(side=tk.LEFT, padx=(0, 6))
        maint_tags = [("movie", "Movies"), ("tv", "TV"), ("anime", "Anime")]
        if config.XANIME_ENABLED:
            maint_tags.append(("xanime", "xAnime"))
        maint_tags.append(("mixed", "Mixed/Other"))
        for tag, label in maint_tags:
            ttk.Checkbutton(
                filter_row, text=label,
                variable=self._maint_filter_vars[tag],
                command=self._apply_maint_filter,
            ).pack(side=tk.LEFT, padx=4)
        ttk.Button(filter_row, text="Select all",
                   command=lambda: self._maint_set_all(True)
                   ).pack(side=tk.LEFT, padx=(14, 3))
        ttk.Button(filter_row, text="Deselect all",
                   command=lambda: self._maint_set_all(False)
                   ).pack(side=tk.LEFT, padx=3)
        combo_btn = ttk.Button(filter_row, text="Combo Clean rename…",
                               command=self._run_combo_clean)
        combo_btn.pack(side=tk.LEFT, padx=(14, 3))
        add_tooltip(combo_btn,
                    "Preview a per-library cleanup rename: dots → spaces, "
                    "[brackets] and {braces} stripped, junk words removed. "
                    "Pick exactly ONE library type above first. Nothing is "
                    "renamed until you Apply Selected.")

        # Task G item 2: "Not a duplicate" — select a group/folder-pair
        # header row (or a specific file row) in the Find Duplicates results
        # above, then click this so it stops being flagged on every rescan.
        not_dup_btn = ttk.Button(filter_row, text="Not a duplicate",
                                 command=self._maint_mark_not_duplicate)
        not_dup_btn.pack(side=tk.LEFT, padx=(14, 3))
        add_tooltip(not_dup_btn,
                    "Run Find Duplicates, select a group header, folder-pair "
                    "header, or a specific file row above, then click this. "
                    "The verdict is remembered (dupe_ignore.json) and survives "
                    "🔄 Re-run and app restarts — see 'Ignored pairs…' to undo.")
        ignored_btn = ttk.Button(filter_row, text="Ignored pairs…",
                                 command=self._maint_open_ignored_pairs)
        ignored_btn.pack(side=tk.LEFT, padx=3)
        add_tooltip(ignored_btn,
                    "Everything marked 'Not a duplicate' — un-ignore any of "
                    "them to let Find Duplicates flag it again.")

        # Adjust the tab's row layout: results frame lives on row 4 (rows 2/3
        # hold the progress strip + filter row).
        maintenance_tab.rowconfigure(4, weight=1)
        maintenance_tab.rowconfigure(2, weight=0)
        maintenance_tab.rowconfigure(3, weight=0)
        maint_results_frame = ttk.Frame(maintenance_tab)
        maint_results_frame.grid(row=4, column=0, sticky="nsew")
        maint_results_frame.columnconfigure(0, weight=1)
        maint_results_frame.rowconfigure(0, weight=1)

        self._maint_tree = ttk.Treeview(
            maint_results_frame,
            columns=("check", "col1", "col2", "col3"),
            show="headings",
            height=18,
        )
        self._maint_tree.heading("check", text="[/]")
        self._maint_tree.heading("col1",  text="Item")
        self._maint_tree.heading("col2",  text="Detail")
        self._maint_tree.heading("col3",  text="Extra")
        self._maint_tree.column("check", width=30,  anchor=tk.CENTER, stretch=False)
        self._maint_tree.column("col1",  width=280, anchor=tk.W)
        self._maint_tree.column("col2",  width=280, anchor=tk.W)
        self._maint_tree.column("col3",  width=160, anchor=tk.W, stretch=False)
        self._maint_tree.grid(row=0, column=0, sticky="nsew")

        maint_scroll = ttk.Scrollbar(maint_results_frame, orient=tk.VERTICAL, command=self._maint_tree.yview)
        maint_scroll.grid(row=0, column=1, sticky="ns")
        self._maint_tree.configure(yscrollcommand=maint_scroll.set)
        self._maint_tree.bind("<ButtonRelease-1>", self._on_maint_tree_click)
        # Row state is keyed by tree iid (not row index), so header-click
        # sorting can reorder rows without breaking the checkboxes.
        make_sortable(self._maint_tree)

        # -----------------------------------------------------------------
        # Settings tab
        # -----------------------------------------------------------------
        self._build_settings_tab(settings_tab)

        # -----------------------------------------------------------------
        # Logs tab — app log + the file-change ledger + missing-file view
        # -----------------------------------------------------------------
        logs_nb = ttk.Notebook(logs_tab)
        logs_nb.grid(row=0, column=0, sticky="nsew")
        app_log_frame = ttk.Frame(logs_nb, padding=6)
        changes_frame = ttk.Frame(logs_nb, padding=6)
        missing_frame = ttk.Frame(logs_nb, padding=6)
        logs_nb.add(app_log_frame, text="App Log")
        logs_nb.add(changes_frame, text="File Changes")
        logs_nb.add(missing_frame, text="Missing Files")
        for fr in (app_log_frame, changes_frame, missing_frame):
            fr.columnconfigure(0, weight=1)
            fr.rowconfigure(1, weight=1)
        app_log_frame.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            app_log_frame, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        ttk.Label(changes_frame, text=(
            "Every add / remove / rename / replacement the app can see — its own "
            "actions are labelled; anything else was done outside the app. Populated "
            "by the nightly index refresh and every in-app file operation."),
            wraplength=900, font=("Segoe UI", 9, "italic")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        changes_tree = ttk.Treeview(
            changes_frame, columns=("at", "event", "path", "detail"), show="headings")
        for col, text, width, stretch in (("at", "When", 130, False),
                                          ("event", "Event", 80, False),
                                          ("path", "Path", 420, True),
                                          ("detail", "Detail", 380, True)):
            changes_tree.heading(col, text=text)
            changes_tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        changes_tree.grid(row=1, column=0, sticky="nsew")
        changes_scroll = ttk.Scrollbar(changes_frame, orient=tk.VERTICAL,
                                       command=changes_tree.yview)
        changes_scroll.grid(row=1, column=1, sticky="ns")
        changes_tree.configure(yscrollcommand=changes_scroll.set)
        make_sortable(changes_tree)
        self._file_changes_tree = changes_tree

        ttk.Label(missing_frame, text=(
            "Files that USED to be indexed but are gone — renames and replacements "
            "are excluded (they're paired automatically). 'Outside the app' means "
            "no Sensarr action touched it."),
            wraplength=900, font=("Segoe UI", 9, "italic")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        missing_tree = ttk.Treeview(
            missing_frame, columns=("at", "path", "detail"), show="headings")
        for col, text, width, stretch in (("at", "Last seen removed", 140, False),
                                          ("path", "Path", 480, True),
                                          ("detail", "What we know", 380, True)):
            missing_tree.heading(col, text=text)
            missing_tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        missing_tree.grid(row=1, column=0, sticky="nsew")
        missing_scroll = ttk.Scrollbar(missing_frame, orient=tk.VERTICAL,
                                       command=missing_tree.yview)
        missing_scroll.grid(row=1, column=1, sticky="ns")
        missing_tree.configure(yscrollcommand=missing_scroll.set)
        make_sortable(missing_tree)
        self._missing_files_tree = missing_tree
        logs_nb.bind("<<NotebookTabChanged>>",
                     lambda _e: self._refresh_file_ledger_views())

        summary = ttk.Frame(self.root, padding=(16, 0, 16, 16))
        summary.grid(row=4, column=0, sticky="ew")
        ttk.Label(summary, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

    # =====================================================================
    # Tray icon
    # =====================================================================

    def _build_tray_icon(self) -> Any | None:
        """The pystray icon, or None when no tray backend is usable — in
        which case the window stays the app (closing minimizes instead of
        hiding, and a visible note explains why)."""
        if not _TRAY.available:
            return None
        pystray = cast(Any, _TRAY.pystray)
        image = self._create_tray_image()
        menu = pystray.Menu(
            # default=True: double-clicking the tray icon reopens the window.
            pystray.MenuItem("Show Admin", lambda icon, item: self.show_window(),
                             default=True),
            pystray.MenuItem("Launch Plex", lambda icon, item: self.run_action("Launch Plex", launch_plex)),
            pystray.MenuItem("Hard Reset", lambda icon, item: self.confirm_hard_reset(from_tray=True)),
            pystray.MenuItem("Refresh Status", lambda icon, item: self.refresh_status()),
            pystray.MenuItem("Get Plex Token", lambda icon, item: self.authenticate_plex_account()),
            pystray.MenuItem("Quit", lambda icon, item: self.request_exit()),
        )
        return pystray.Icon("Sensarr", image,
                            f"Sensarr v{config.APP_VERSION}", menu)

    def _create_tray_image(self) -> PillowImage:
        from app_icon import icon_image
        return icon_image(64)

    # =====================================================================
    # Window management
    # =====================================================================

    def show_window(self) -> None:
        self._post_to_ui(self._show_window)

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        if self._tray_icon is None:
            # No tray to come back from — minimize instead of vanishing.
            self.root.iconify()
            return
        self.root.withdraw()

    def _post_to_ui(self, callback, *, delay_ms: int = 0) -> bool:
        try:
            self.root.after(delay_ms, callback)
            return True
        except (RuntimeError, tk.TclError):
            logger.debug("Skipped UI callback because Tk is not accepting new work.")
            return False

    def _messagebox_parent(self) -> tk.Misc | None:
        return self.root if self.root.winfo_viewable() else None

    def _show_info(self, title: str, message: str) -> None:
        if not self._maint_popups_ok:
            logger.info("Popup suppressed (idle pass) — %s: %s", title,
                        message.splitlines()[0] if message else "")
            return
        parent = self._messagebox_parent()
        if parent is None:
            messagebox.showinfo(title, message)
            return
        messagebox.showinfo(title, message, parent=parent)

    def _show_warning(self, title: str, message: str) -> None:
        parent = self._messagebox_parent()
        if parent is None:
            messagebox.showwarning(title, message)
            return
        messagebox.showwarning(title, message, parent=parent)

    def _ask_yes_no(self, title: str, message: str) -> bool:
        parent = self._messagebox_parent()
        if parent is None:
            return messagebox.askyesno(title, message)
        return messagebox.askyesno(title, message, parent=parent)

    # =====================================================================
    # Requests tab
    # =====================================================================

    _TYPE_LABEL = {
        "movie": "Movie", "tv": "TV", "anime": "Anime",
        "xanime": "xAnime", "other": "Other", "unknown": "?",
    }

    def refresh_requests(self) -> None:
        if self.requests_tree is None:
            return

        # Show every request with outstanding work, including needs_identity
        # rows so they are visible for resolution (Resolve button here; the
        # Grab-queue subtab offers the same action from its right-click menu).
        requests = list_requests(status="active", limit=200)
        selected = self.requests_tree.selection()
        selected_id = None
        if selected:
            selected_values = self.requests_tree.item(selected[0], "values")
            if selected_values:
                selected_id = str(selected_values[0])

        for item_id in self.requests_tree.get_children():
            self.requests_tree.delete(item_id)

        for request in requests:
            row_id = str(request.request_id)
            display_title = request.resolved_title or request.content
            if request.status == "needs_identity":
                display_title = f"{display_title}  [needs identity]"
            type_label = self._TYPE_LABEL.get(request.media_type, "?")
            in_library = "YES" if request.found_in_library else ""
            self.requests_tree.insert(
                "", "end", iid=row_id,
                values=(
                    request.request_id, type_label, request.requester,
                    _local_ts(request.created_at), display_title, in_library,
                ),
            )

        if selected_id and self.requests_tree.exists(selected_id):
            self.requests_tree.selection_set(selected_id)

        self.queue_var.set(f"Open requests: {open_request_count()}")

    def _on_request_selected(self, _event: Any) -> None:
        if self.requests_tree is None or self.request_detail_text is None:
            return

        selected = self.requests_tree.selection()
        if not selected:
            return

        values = self.requests_tree.item(selected[0], "values")
        if not values:
            return

        request_id = int(values[0])
        req = get_request(request_id)
        if req is None:
            return

        type_label = self._TYPE_LABEL.get(req.media_type, "?")
        lines = [
            f"Request #{req.request_id}  |  {type_label} ({req.media_type})  |  {_local_ts(req.created_at)}",
            f"Status    : {req.status}",
            f"Requester : {req.requester}",
            f"Raw input : {req.content}",
        ]
        if req.status == "needs_identity":
            lines.append("[NEEDS IDENTITY]  Resolve a provider match before this can be grabbed.")
        if req.identity_source and req.external_id:
            lines.append(f"Identity  : {req.identity_source}:{req.external_id}")
        if req.resolved_title and req.resolved_title != req.content:
            lines.append(f"Resolved  : {req.resolved_title}")
        if req.external_url:
            lines.append(f"DB Link   : {req.external_url}")
        if req.found_in_library:
            lines.append(f"[IN LIBRARY]  Found in library  (last checked: {req.library_checked_at or 'unknown'})")
        elif req.library_checked_at:
            lines.append(f"[NOT FOUND]  Not in library yet  (last checked: {req.library_checked_at})")
        else:
            lines.append("[PENDING]  Not yet checked against library")

        detail = "\n".join(lines)
        text = self.request_detail_text
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert("1.0", detail)
        # Make the DB link an actual hyperlink (blue, hand cursor, click to open).
        if req.external_url:
            start = text.search(req.external_url, "1.0", tk.END)
            if start:
                end = f"{start}+{len(req.external_url)}c"
                url = req.external_url
                text.tag_add("dblink", start, end)
                text.tag_configure("dblink", foreground="#4da3ff", underline=True)
                text.tag_bind("dblink", "<Button-1>",
                              lambda _e, u=url: webbrowser.open_new_tab(u))
                text.tag_bind("dblink", "<Enter>",
                              lambda _e: text.configure(cursor="hand2"))
                text.tag_bind("dblink", "<Leave>",
                              lambda _e: text.configure(cursor=""))
        text.configure(state=tk.DISABLED)

    def add_request_from_ui(self) -> None:
        requester = self.requester_var.get().strip() or "Admin"
        content = self.request_content_var.get().strip()
        if not content:
            self._show_warning("Missing request", "Enter the request details before adding it.")
            return

        # Route through the same resolve-or-needs_identity path as the Telegram
        # surfaces (Task A): look up candidates, let the user pick the exact
        # movie/show, and only then store a qualified, auto-grabbable identity.
        # No candidate / no pick => needs_identity (visible, never grabbed).
        self.status_var.set(f"Looking up '{content}'…")
        try:
            candidates = request_intake.search_candidates(content)
        except Exception:
            logger.exception("Candidate lookup failed for %r", content)
            candidates = []

        if not candidates:
            created = request_intake.add_needs_identity(content, requester)
            self._finish_request_add(created, needs_identity=True)
            return

        self._show_candidate_picker(content, requester, candidates)

    def _finish_request_add(self, created, *, needs_identity: bool) -> None:
        self.request_content_var.set("")
        tag = " (needs identity — pick a title to enable auto-grab)" if needs_identity else ""
        self.last_action_var.set(f"Last action: added request #{created.request_id}{tag}")
        self.status_var.set(f"Queued request #{created.request_id}: {created.content}{tag}")
        self.refresh_requests()

    def _resolve_identity_dialog(
        self,
        title: str,
        query: str,
        *,
        media_type: str | None = None,
        initial_candidates: list | None = None,
        include_none_of_these: bool = False,
        search_fn=None,
    ) -> tuple[Any, bool, list]:
        """Modal search-and-pick dialog shared by the resolve-identity flow
        and the add-request candidate picker (Task E).

        Unlike the one-shot `_pick_from_list` call this replaces, the query
        is a live Entry and 'Search again' re-runs
        request_intake.search_candidates(query, media_type) in place: a miss
        renders 'No matches' inline and waits for an edited query or Cancel
        instead of dead-ending in a popup. The media-type selector is what
        makes the anime/xanime branches of search_candidates reachable —
        they're never guessed at automatically.

        Returns (picked_MediaResult_or_None, explicit_none_of_these,
        candidates_shown_at_the_time_of_the_pick). The third element carries
        the full sibling list the pick was made from (not just the winner)
        so callers that need same-title disambiguation context (build_aliases'
        country-tag logic) get it even after a mid-dialog re-search replaced
        the original candidate list. Cancel (button or window close) always
        returns (None, False, <last shown list>). When `include_none_of_these`
        is set, an extra 'None of these' row lets the add-request flow
        explicitly file the request as needs_identity — a different outcome
        from Cancel there, which adds nothing at all.
        """
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        win.geometry("520x420")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(4, weight=1)

        candidates: list[Any] = list(initial_candidates or [])
        result: list[Any] = [None, False, candidates]
        NONE_LABEL = "None of these — file as needs identity"

        ttk.Label(win, text="Search:").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 0))
        query_var = tk.StringVar(value=query)
        entry = ttk.Entry(win, textvariable=query_var)
        entry.grid(row=1, column=0, sticky="ew", padx=10)

        type_row = ttk.Frame(win)
        type_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 0))
        ttk.Label(type_row, text="Type:").pack(side=tk.LEFT)
        initial_type = media_type if media_type in _RESOLVE_DIALOG_TYPES else "auto"
        type_var = tk.StringVar(value=initial_type)
        ttk.Combobox(
            type_row, textvariable=type_var, state="readonly", width=10,
            values=list(_RESOLVE_DIALOG_TYPES),
        ).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Button(type_row, text="Search again",
                   command=lambda: _run_search()).pack(side=tk.LEFT)

        status_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=status_var, foreground=_MUTED_TEXT,
                  wraplength=480).grid(
            row=3, column=0, sticky="w", padx=10, pady=(4, 0))

        listbox = tk.Listbox(win)
        listbox.grid(row=4, column=0, sticky="nsew", padx=10, pady=(4, 0))

        def _render(cands: list, error: str | None) -> None:
            listbox.delete(0, tk.END)
            for m in cands:
                listbox.insert(tk.END, request_intake.format_candidate_label(m))
            if include_none_of_these:
                listbox.insert(tk.END, NONE_LABEL)
            if cands:
                listbox.selection_set(0)
                status_var.set(f"{len(cands)} result(s).")
            elif error:
                status_var.set(error)
            else:
                status_var.set("No matches — edit the search above and try again.")

        def _run_search() -> None:
            nonlocal candidates
            mt = type_var.get()
            candidates, error = _resolve_dialog_search(
                query_var.get(), None if mt == "auto" else mt,
                search_fn=search_fn)
            result[2] = candidates
            _render(candidates, error)

        def _confirm() -> None:
            sel = listbox.curselection()
            if not sel:
                status_var.set("Select a result, or Cancel.")
                return
            idx = sel[0]
            if include_none_of_these and idx == len(candidates):
                result[0], result[1] = None, True
            elif 0 <= idx < len(candidates):
                result[0], result[1] = candidates[idx], False
            else:
                status_var.set("Select a result, or Cancel.")
                return
            result[2] = candidates
            win.destroy()

        def _cancel() -> None:
            result[0], result[1], result[2] = None, False, candidates
            win.destroy()

        entry.bind("<Return>", lambda _e: _run_search())
        listbox.bind("<Double-Button-1>", lambda _e: _confirm())

        btns = ttk.Frame(win)
        btns.grid(row=5, column=0, pady=10)
        ttk.Button(btns, text="OK", command=_confirm).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="Cancel", command=_cancel).grid(row=0, column=1, padx=4)

        win.protocol("WM_DELETE_WINDOW", _cancel)
        self._apply_dark_widget_styles(win)

        if candidates:
            _render(candidates, None)
        else:
            _run_search()

        entry.focus_set()
        self.root.wait_window(win)
        return cast(Any, result[0]), bool(result[1]), cast(list, result[2])

    def _show_candidate_picker(self, content: str, requester: str, candidates: list) -> None:
        """Tk glue over request_intake.add_picked_candidate: collect the pick
        (and a season answer for TV) via the shared editable resolve dialog
        (Task E item 3), then hand the decision to the store layer. 'None of
        these' stores needs_identity; a TV pick never creates a season=NULL
        whole-show row; Cancel (unlike 'None of these') adds nothing at all.
        """
        match, explicit_none, shown = self._resolve_identity_dialog(
            "Which one did you mean?", content,
            initial_candidates=candidates, include_none_of_these=True)
        if match is None and not explicit_none:
            return  # dialog cancelled — add nothing

        seasons = None
        if match is not None and match.media_type == "tv":
            from tkinter import simpledialog
            answer = simpledialog.askstring(
                "Which season?",
                f"{match.title}: enter a season number, or 'all' for "
                "every aired season.",
                parent=self.root)
            if answer is None:
                return  # season prompt cancelled — add nothing
            seasons = request_intake.seasons_for_answer(answer, match)

        # Pass the FULL sibling set `shown` (not just [match]) plus the real
        # index of the pick within it: add_picked_candidate builds
        # candidate_titles from every row in the list it's handed
        # (request_intake.py ~521-525), and build_aliases only appends a
        # disambiguating " <CC>" suffix when >=2 siblings share the base
        # title (request_intake.py:95) — same-title country-edition picks
        # (e.g. MAFS AU vs US) need every sibling present, not a
        # single-element list, or that check can never fire from this UI.
        choice_index = None
        picked = shown
        if match is not None:
            try:
                choice_index = shown.index(match)
            except ValueError:
                # Defensive only: `shown` is set from the exact same
                # `candidates` list the pick was read out of (Task E's
                # dialog keeps them in lockstep), so this should be
                # unreachable. Never silently misfile the request if it
                # somehow happens — append the pick so it's still findable.
                logger.warning(
                    "Resolve dialog pick %r missing from its own shown "
                    "list; appending before filing the request.",
                    getattr(match, "title", match))
                picked = shown + [match]
                choice_index = len(picked) - 1
        outcome = request_intake.add_picked_candidate(
            content, requester, picked, choice_index, seasons=seasons)
        self.request_content_var.set("")
        needs = outcome.status == "needs_identity"
        tag = " (needs identity — pick a title to enable auto-grab)" if needs else ""
        self.last_action_var.set(
            f"Last action: added {len(outcome.request_ids)} request(s){tag}")
        self.status_var.set(
            f"Queued {len(outcome.request_ids)} request(s) for '{content}'{tag}")
        self.refresh_requests()

    def resolve_selected_request(self) -> None:
        """The resolve path for a needs_identity row: pick the exact title, then
        attach the identity so it becomes auto-grabbable."""
        request_id = self._selected_request_id()
        if request_id is None:
            self._show_warning("No request selected", "Select a request to resolve.")
            return
        self.resolve_request_by_id(request_id)

    def resolve_request_by_id(self, request_id: int) -> None:
        """Resolve one request by id — shared by the Requests toolbar button
        and the Grab-queue subtab's right-click action.

        Uses the shared editable resolve dialog (Task E): the query starts
        prefilled from the request's resolved_title/content, 'Search again'
        loops on an edited query or media type, and an empty result renders
        inline instead of dead-ending in a popup. Seeding the type selector
        from the request's own media_type is what lets the
        anime/xanime branches of search_candidates actually run here. This
        is also the repair tool for a wrong identity: re-typing the query
        and re-searching replaces a bad pick, it doesn't just accept-or-quit.
        """
        req = get_request(request_id)
        if req is None:
            self._show_warning("Request not found", f"Request #{request_id} is gone.")
            return
        content = (req.resolved_title or req.content or "").strip()
        match, _explicit_none, _shown = self._resolve_identity_dialog(
            "Resolve identity", content, media_type=req.media_type)
        if match is None:
            return  # cancelled — leave the row exactly as it was

        season = None
        if match.media_type == "tv":
            from tkinter import simpledialog
            ans = simpledialog.askstring(
                "Season", f"{match.title}: season number (blank = S1)",
                parent=self.root)
            season = request_intake.parse_single_season(ans, default=1)
        updated = request_intake.resolve_request(request_id, match, season=season)
        if updated is not None:
            self.status_var.set(
                f"Resolved request #{request_id} → {match.title} ({updated.status})")
        self.refresh_requests()

    def complete_selected_request(self) -> None:
        if self.requests_tree is None:
            return

        selected = self.requests_tree.selection()
        if not selected:
            self._show_warning("No request selected", "Select a request in the list first.")
            return

        values = self.requests_tree.item(selected[0], "values")
        request_id = int(values[0])
        if complete_request(request_id):
            self.last_action_var.set(f"Last action: completed request #{request_id}")
            self.status_var.set(f"Marked request #{request_id} as done")
            self.refresh_requests()
        else:
            self._show_info(
                "Nothing changed",
                f"Request #{request_id} was already completed or no longer exists.",
            )

    # ---------------------------------------------------------------------
    # Source search (open torrent site for a request)
    # ---------------------------------------------------------------------

    def _on_request_activated(self, _event: Any) -> None:
        """Double-click handler: open the source search for the clicked row."""
        self.search_source_for_selected_request()

    def _selected_request_id(self) -> int | None:
        if self.requests_tree is None:
            return None
        selected = self.requests_tree.selection()
        if not selected:
            return None
        values = self.requests_tree.item(selected[0], "values")
        if not values:
            return None
        return int(values[0])

    @staticmethod
    def _source_search_url(media_type: str, query: str) -> str:
        template = _SOURCE_SEARCH_BY_TYPE.get(media_type, _TPB_SEARCH)
        return template.format(q=urllib.parse.quote_plus(query))

    def search_source_for_selected_request(self) -> None:
        request_id = self._selected_request_id()
        if request_id is None:
            self._show_warning("No request selected", "Select a request in the list first.")
            return

        req = get_request(request_id)
        if req is None:
            self._show_warning("Request not found", f"Request #{request_id} no longer exists.")
            return

        query = (req.resolved_title or req.content or "").strip()
        if not query:
            self._show_warning("Nothing to search", "This request has no title to search for.")
            return

        url = self._source_search_url(req.media_type, query)
        site = "Sukebei" if req.media_type == "xanime" else (
            "nyaa.si" if req.media_type == "anime" else "The Pirate Bay")
        self._open_in_firefox(url)
        self.last_action_var.set(f"Last action: searched {site} for request #{request_id}")
        self.status_var.set(f"Opened {site} search for: {query}")

    def _open_in_firefox(self, url: str) -> None:
        """Open *url* in a new Firefox tab, falling back to the default browser."""
        browser = self._get_firefox_browser()
        try:
            if browser is not None:
                browser.open_new_tab(url)
            else:
                webbrowser.open_new_tab(url)
        except Exception:
            logger.exception("Failed to open source search URL: %s", url)
            try:
                webbrowser.open_new_tab(url)
            except Exception:
                logger.exception("Default browser also failed to open: %s", url)

    def _get_firefox_browser(self) -> Any:
        """Return a cached webbrowser controller for Firefox, or None."""
        if self._firefox_browser_cache is not _FIREFOX_UNSET:
            return self._firefox_browser_cache

        controller = None
        firefox_path = self._find_firefox_path()
        if firefox_path:
            try:
                webbrowser.register(
                    "plex_reset_firefox", None,
                    webbrowser.BackgroundBrowser(firefox_path),
                )
                controller = webbrowser.get("plex_reset_firefox")
            except Exception:
                logger.exception("Could not register Firefox at %s", firefox_path)
                controller = None
        else:
            logger.info("Firefox not found; source searches will use the default browser.")

        self._firefox_browser_cache = controller
        return controller

    @staticmethod
    def _find_firefox_path() -> str | None:
        """Locate firefox.exe via PATH or the standard install directories."""
        on_path = shutil.which("firefox")
        if on_path:
            return on_path
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Mozilla Firefox" / "firefox.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Mozilla Firefox" / "firefox.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    # =====================================================================
    # Plex actions
    # =====================================================================

    def refresh_status(self) -> None:
        self._update_plex_indicator()
        self.run_action("Refresh Status", get_status, show_popup=False, update_status_only=True)

    def run_health_check(self, *, include_updates: bool = False) -> None:
        """Render the health (and optionally update) report into the Status tab."""
        label = "Health + Update Check" if include_updates else "Health Check"
        self.status_var.set(f"Running {label.lower()}…")
        bot_running = self.bot_service.running
        self.run_action(
            label,
            lambda: format_health_report(bot_running=bot_running,
                                         include_updates=include_updates),
            show_popup=False, update_status_only=True,
        )

    def _update_plex_indicator(self) -> None:
        """Refresh the header's Plex ● dot from a cheap process scan."""
        def worker() -> None:
            running = False
            try:
                running = is_plex_running()
            except Exception:
                logger.exception("Plex status check failed.")
            self._post_to_ui(lambda: self._set_plex_indicator(running))

        threading.Thread(target=worker, name="ui-plex-indicator", daemon=True).start()

    def _set_plex_indicator(self, running: bool) -> None:
        color = _DOT_GREEN if running else _DOT_RED
        self._plex_dot.configure(foreground=color)
        if hasattr(self, "_status_plex_dot"):
            self._status_plex_dot.configure(foreground=color)
        if hasattr(self, "_status_bot_dot"):
            self._status_bot_dot.configure(
                foreground=_DOT_GREEN if self.bot_service.running else _DOT_RED)
        self.plex_status_var.set("Plex: running" if running else "Plex: not running")

    def confirm_hard_reset(self, from_tray: bool = False) -> None:
        self._post_to_ui(lambda: self._confirm_hard_reset(from_tray))

    def _confirm_hard_reset(self, from_tray: bool) -> None:
        confirmed = self._ask_yes_no(
            "Confirm hard reset",
            "Hard reset will forcefully kill all Plex processes and may interrupt users currently watching. Continue?",
        )
        if confirmed:
            self.run_action("Hard Reset", hard_reset)
        elif from_tray:
            self.last_action_var.set("Last action: hard reset cancelled")

    def authenticate_plex_account(self) -> None:
        self.last_action_var.set("Last action: requesting Plex token")
        self.status_var.set("Requesting Plex authorization from plex.tv...")

        def worker() -> None:
            try:
                session = start_plex_pin_login()
                browser_opened = launch_auth_browser(session)
                self._post_to_ui(lambda: self._handle_plex_auth_started(session, browser_opened))
                result = wait_for_plex_token(session)
                save_plex_credentials(result)
            except TimeoutError:
                self._post_to_ui(self._handle_plex_auth_timeout)
                return
            except Exception as exc:
                logger.exception("Plex authorization failed.")
                self._post_to_ui(lambda: self._handle_plex_auth_failure(str(exc)))
                return
            self._post_to_ui(lambda: self._handle_plex_auth_success(result))

        threading.Thread(target=worker, name="ui-plex-auth", daemon=True).start()

    def _handle_plex_auth_started(self, session: PlexPinSession, browser_opened: bool) -> None:
        self.status_var.set("Waiting for Plex authorization in your browser...")
        self.last_action_var.set("Last action: Plex authorization started")
        message = (
            "Approve Plex access in the browser that just opened.\n\n"
            f"Authorization URL:\n{session.auth_url}\n\n"
            f"Authorization code: {session.code}\n\n"
            f"This app will keep polling for up to {session.expires_in} seconds."
        )
        if not browser_opened:
            message = "Could not open your browser automatically.\n\n" + message
        self._show_info("Authorize Plex", message)

    def _handle_plex_auth_timeout(self) -> None:
        self.last_action_var.set("Last action: Plex authorization timed out")
        self.status_var.set("Plex authorization timed out")
        self._show_warning("Plex Authorization",
            "The Plex authorization window expired before approval completed. Start the flow again.")

    def _handle_plex_auth_failure(self, error_message: str) -> None:
        self.last_action_var.set("Last action: Plex authorization failed")
        self.status_var.set("Plex authorization failed")
        self._show_warning("Plex Authorization", error_message)

    def _handle_plex_auth_success(self, result: PlexTokenResult) -> None:
        masked_token = (
            f"{result.auth_token[:6]}...{result.auth_token[-4:]}"
            if len(result.auth_token) >= 12 else result.auth_token
        )
        self.last_action_var.set("Last action: Plex token saved")
        self.status_var.set("Saved Plex token to .env")
        self.refresh_library_metrics()
        self._show_info("Plex Authorization",
            "Plex authorization succeeded.\n\n"
            "Saved PLEX_TOKEN and PLEX_CLIENT_IDENTIFIER to .env.\n"
            f"Token: {masked_token}")

    def run_action(self, action_name: str, action, *, show_popup: bool = True, update_status_only: bool = False) -> None:
        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                logger.exception("%s failed.", action_name)
                result = f"{action_name} failed: {exc}"
            self._post_to_ui(lambda: self._handle_action_result(
                action_name, result, show_popup=show_popup, update_status_only=update_status_only,
            ))

        threading.Thread(target=worker, name=f"ui-{action_name.lower().replace(' ', '-')}", daemon=True).start()

    def _handle_action_result(self, action_name: str, result: str, *, show_popup: bool, update_status_only: bool) -> None:
        self.last_action_var.set(f"Last action: {action_name}")
        self.status_var.set(result.splitlines()[0] if result else action_name)

        if update_status_only:
            self._set_status_text(result)
        else:
            self._set_status_text(get_status())
            self.refresh_requests()
            self.refresh_library_summary()
            self.refresh_library_metrics()
            if show_popup:
                self._show_info(action_name, result)

    def _set_status_text(self, content: str) -> None:
        if self.status_text is None:
            return
        self.status_text.configure(state=tk.NORMAL)
        self.status_text.delete("1.0", tk.END)
        self.status_text.insert("1.0", content)
        self.status_text.configure(state=tk.DISABLED)

    # =====================================================================
    # Library tab
    # =====================================================================

    def refresh_library_summary(self) -> None:
        if self._library_summary_refresh_running:
            self._library_summary_refresh_pending = True
            return
        self._library_summary_refresh_running = True

        def worker() -> None:
            try:
                summary = format_library_summary_message()
            except Exception as exc:
                logger.exception("Library summary refresh failed.")
                summary = f"Library summary unavailable: {exc}"
            self._post_to_ui(lambda: self._handle_library_summary_result(summary))

        threading.Thread(target=worker, name="ui-library-summary", daemon=True).start()

    def _handle_library_summary_result(self, summary: str) -> None:
        self._library_summary_refresh_running = False
        indexed_line = summary.splitlines()[0] if summary else "Library index unavailable"
        self.library_var.set(indexed_line)

        if self.library_summary_text is not None:
            self.library_summary_text.configure(state=tk.NORMAL)
            self.library_summary_text.delete("1.0", tk.END)
            self.library_summary_text.insert("1.0", summary)
            self.library_summary_text.configure(state=tk.DISABLED)

        if self._library_summary_refresh_pending and not self._quitting:
            self._library_summary_refresh_pending = False
            self.refresh_library_summary()

    def refresh_library_metrics(self) -> None:
        if self._library_metrics_refresh_running:
            self._library_metrics_refresh_pending = True
            return
        self._library_metrics_refresh_running = True

        def worker() -> None:
            try:
                metrics = format_combined_metrics_message()
            except Exception as exc:
                logger.exception("Library metrics refresh failed.")
                metrics = f"Metrics unavailable: {exc}"
            self._post_to_ui(lambda: self._handle_library_metrics_result(metrics))

        threading.Thread(target=worker, name="ui-library-metrics", daemon=True).start()

    def _handle_library_metrics_result(self, metrics: str) -> None:
        self._library_metrics_refresh_running = False
        if self.metrics_text is not None:
            self.metrics_text.configure(state=tk.NORMAL)
            self.metrics_text.delete("1.0", tk.END)
            self.metrics_text.insert("1.0", metrics)
            self.metrics_text.configure(state=tk.DISABLED)

        if self._library_metrics_refresh_pending and not self._quitting:
            self._library_metrics_refresh_pending = False
            self.refresh_library_metrics()

    def rebuild_library_index_from_ui(self, *, from_idle: bool = False):
        """Full library-index rebuild as a registry job (Task B item 1).

        Shared by the Library tab's Reindex button, the Maintenance tab's
        Reindex button, and the post-settings-save prompt — one job, one
        completion path. Before this, the Maintenance tab's Reindex button
        called this exact method but the completion handler only ever
        touched Library-tab widgets, so the maintenance status line/progress
        strip never moved: clicking it there looked like a no-op. Now every
        caller gets an honest run in the maintenance progress strip AND the
        Library tab refresh.
        """
        def job(progress, cancel_check) -> Any:
            progress(phase="Rebuilding the library file index…")
            try:
                result = rebuild_library_index()
                message = format_reindex_result_message(result)
                indexed = result.indexed_files
            except Exception as exc:
                logger.exception("Library reindex failed.")
                message = f"Library reindex failed: {exc}"
                indexed = 0
            return maint_jobs.JobResult(
                summary={"indexed_files": indexed}, result=message)

        return self._maint_submit("reindex", "Reindex Library", job,
                                  meta={"idle": from_idle})

    def _handle_library_reindex_result(self, message: str) -> None:
        self.refresh_library_summary()
        self.refresh_library_metrics()
        self._lib_show_all()
        self.status_var.set(message.splitlines()[0] if message else "Library reindex complete")
        self.last_action_var.set("Last action: library reindex")

        # Task B item 1: the Maintenance tab's own tool grid + status line
        # visibly reflect the run too, not just the Library tab.
        self._maint_tool_name = "reindex"
        self._maint_results = []
        first_line = message.splitlines()[0] if message else "Library reindex complete"
        self._maint_status_var.set(first_line)
        lines = [ln for ln in message.splitlines() if ln.strip()]
        self._populate_maint_tree(
            [("", ln, "", "") for ln in lines] or [("", "Reindex complete", "", "")],
            col1="Reindex result", col2="", col3="")

        self._show_info("Library Reindex", message)

    # =====================================================================
    # Library tab — full persistent listing + movie quality tools
    # =====================================================================

    _LIB_TYPE_LABELS = (("movie", "Movies"), ("tv", "TV"), ("anime", "Anime"),
                        ("xanime", "xAnime"), ("mixed", "Mixed"))

    def _build_library_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        # "all" = plain listing, "lowqual" = quality-scan results.
        self._lib_view_mode = "all"
        self._lib_rows: list[Any] = []      # per-row payload (entry or LowQualityMovie)

        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Filter").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(toolbar, textvariable=self.library_search_var)
        entry.grid(row=0, column=1, sticky="ew", padx=(6, 10))
        entry.bind("<Return>", lambda _e: self._lib_show_all())
        for col, (text, command, tip) in enumerate((
            ("Show All", self._lib_show_all,
             "List every indexed file (the index persists between sessions)."),
            ("Refresh", self._lib_refresh,
             "Quick delta pass: add new files, drop deleted ones — no full rebuild."),
            ("Reindex", self.rebuild_library_index_from_ui,
             "Full rebuild of the file index from scratch."),
        ), start=2):
            btn = ttk.Button(toolbar, text=text, command=command)
            btn.grid(row=0, column=col, padx=(0, 6))
            add_tooltip(btn, tip)

        self._lib_type_vars: dict[str, tk.BooleanVar] = {}
        lib_labels = [tl for tl in self._LIB_TYPE_LABELS
                      if tl[0] != "xanime" or config.XANIME_ENABLED]
        for col, (tag, label) in enumerate(lib_labels, start=5):
            var = tk.BooleanVar(value=(tag == "movie"))  # movies-only by default
            self._lib_type_vars[tag] = var
            ttk.Checkbutton(toolbar, text=label, variable=var,
                            command=self._lib_filters_changed).grid(row=0, column=col, padx=2)
        if "xanime" not in self._lib_type_vars:
            self._lib_type_vars["xanime"] = tk.BooleanVar(value=False)

        # Movie tools — surfaced only while the Movies filter is the only one on.
        self._lib_movie_bar = ttk.Frame(tab)
        self._lib_movie_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(self._lib_movie_bar, text="Subs:").pack(side=tk.LEFT)
        from subtitles import LANGUAGE_CHOICES
        self._lib_lang_var = tk.StringVar(value=config.SUBTITLE_LANGUAGE)
        ttk.Combobox(self._lib_movie_bar, textvariable=self._lib_lang_var, width=5,
                     state="readonly", values=[code for code, _n in LANGUAGE_CHOICES],
                     ).pack(side=tk.LEFT, padx=(4, 4))
        for text, command, tip in (
            ("Find Subtitles for Selected", self._lib_find_subs,
             "Download subtitles (in the chosen language) next to each selected movie. "
             "Needs the 'subliminal' package installed."),
            ("Search Low-Quality Movies", self._lib_search_lowqual,
             "Scan movies for cam/telesync releases and files under "
             f"{config.LOW_QUALITY_MB_PER_MIN:g} MB/min. Cams list first, worst first. "
             "Flags copies as redundant when a better version already exists."),
            ("Replace Selected with Non-Cam", self._lib_replace_selected,
             "For each selected low-quality movie: search for a proper (non-cam, "
             "size-appropriate) release, download it, and delete the old file once "
             "the new one is in place."),
        ):
            btn = ttk.Button(self._lib_movie_bar, text=text, command=command)
            btn.pack(side=tk.LEFT, padx=(0, 6))
            add_tooltip(btn, tip)
        del_btn = ttk.Button(self._lib_movie_bar, text="Delete Selected Files…",
                             style="Danger.TButton", command=self._lib_delete_selected)
        del_btn.pack(side=tk.LEFT, padx=(6, 0))
        add_tooltip(del_btn, "Delete the selected files (recycle bin). Asks for confirmation.")

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            tree_frame, columns=("name", "type", "size", "rate", "note", "path"),
            show="headings", selectmode="extended",
        )
        for col, text, width, stretch in (
            ("name", "Name", 300, True), ("type", "Type", 60, False),
            ("size", "Size", 80, False), ("rate", "MB/min", 70, False),
            ("note", "Quality note", 240, True), ("path", "Path", 300, True),
        ):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        make_sortable(tree)
        self.library_results_tree = tree

        self._lib_status_var = tk.StringVar(
            value="Click Show All to list the library (persisted index — no rescan needed).")
        ttk.Label(tab, textvariable=self._lib_status_var,
                  font=("Segoe UI", 9, "italic")).grid(row=3, column=0, sticky="w", pady=(4, 0))
        self._lib_filters_changed(initial=True)

    def _lib_movies_only(self) -> bool:
        return (self._lib_type_vars["movie"].get()
                and not any(v.get() for tag, v in self._lib_type_vars.items()
                            if tag != "movie"))

    def _lib_filters_changed(self, initial: bool = False) -> None:
        if self._lib_movies_only():
            self._lib_movie_bar.grid()
        else:
            self._lib_movie_bar.grid_remove()
        if not initial:
            self._lib_show_all()

    def _lib_active_types(self) -> set[str]:
        return {tag for tag, v in self._lib_type_vars.items() if v.get()}

    def _lib_show_all(self) -> None:
        query = self.library_search_var.get()
        active = self._lib_active_types()
        self._lib_status_var.set("Loading library listing…")

        def worker() -> None:
            from library_index import list_all_files
            entries = list_all_files(name_filter=query)
            rows = []
            type_cache: dict[str, str] = {}
            for e in entries:
                mtype = type_cache.get(e.root_path)
                if mtype is None:
                    mtype = media_type_for_path(e.root_path)
                    type_cache[e.root_path] = mtype
                if mtype not in active:
                    continue
                rows.append((e, mtype))
            self._post_to_ui(lambda: self._lib_render_all(rows))

        threading.Thread(target=worker, name="lib-show-all", daemon=True).start()

    def _lib_render_all(self, rows: list[tuple[Any, str]]) -> None:
        tree = self.library_results_tree
        if tree is None:
            return
        self._lib_view_mode = "all"
        self._lib_rows = [e for e, _t in rows]
        for iid in tree.get_children():
            tree.delete(iid)
        for idx, (e, mtype) in enumerate(rows):
            tree.insert("", "end", iid=str(idx),
                        values=(e.name, mtype, self._fmt_bytes(e.size_bytes),
                                "", "", e.path))
        self._lib_status_var.set(
            f"{len(rows)} file(s) shown. Select rows for bulk actions; "
            "Ctrl+A selects everything visible.")

    def _lib_refresh(self) -> None:
        self._lib_status_var.set("Refreshing index against the filesystem…")

        def worker() -> None:
            from library_index import refresh_library_index
            try:
                result = refresh_library_index()
            except Exception as exc:
                self._post_to_ui(lambda: self._lib_status_var.set(f"Refresh failed: {exc}"))
                return

            def done() -> None:
                self._lib_status_var.set(
                    f"Index refreshed: +{result.added} new, −{result.removed} gone, "
                    f"{result.updated} changed ({result.total} total).")
                self._lib_show_all()
            self._post_to_ui(done)

        threading.Thread(target=worker, name="lib-refresh", daemon=True).start()

    def _lib_selected_paths(self) -> list[str]:
        tree = self.library_results_tree
        if tree is None:
            return []
        paths = []
        for iid in tree.selection():
            values = tree.item(iid, "values")
            if values:
                paths.append(values[-1])
        return paths

    # JSON, not pickle (Task S item 1) — same reasoning as the maintenance
    # cache: a plantable cache file must never execute in this process.
    _LIB_LOWQUAL_CACHE_VERSION = 1
    _LIB_LOWQUAL_CACHE_FILE = "library_lowqual.json"
    _LIB_LOWQUAL_CACHE_LEGACY = "library_lowqual.pkl"

    def _lib_lowqual_cache_path(self) -> Path:
        import app_paths
        return app_paths.PATHS.cache_dir / self._LIB_LOWQUAL_CACHE_FILE

    def _lib_lowqual_save_cache(self, results: list[Any], scanned: int) -> None:
        json_cache.save_json_cache(
            self._lib_lowqual_cache_path(),
            {"results": results, "scanned": scanned,
             "at": datetime.datetime.now().strftime("%b %d %H:%M")},
            version=self._LIB_LOWQUAL_CACHE_VERSION,
            legacy_paths=[self._lib_lowqual_cache_path().parent
                          / self._LIB_LOWQUAL_CACHE_LEGACY])

    def _lib_lowqual_load_cache(self) -> dict[str, Any] | None:
        from video_quality import LowQualityMovie
        payload = json_cache.load_json_cache(
            self._lib_lowqual_cache_path(),
            version=self._LIB_LOWQUAL_CACHE_VERSION,
            dataclass_types=(LowQualityMovie,))
        return payload if isinstance(payload, dict) else None

    def _lib_search_lowqual(self) -> None:
        # First click shows the persisted result from the last scan (survives
        # restarts); clicking again while that cached view is up re-scans.
        # Re-scans are cheap anyway: ffprobe runtimes live in SQLite, so only
        # new/changed files get probed.
        if not getattr(self, "_lib_lowqual_showing_cache", False):
            payload = self._lib_lowqual_load_cache()
            if payload is not None:
                self._lib_lowqual_showing_cache = True
                self._lib_render_lowqual(payload.get("results", []),
                                         payload.get("scanned", 0))
                self._lib_status_var.set(
                    self._lib_status_var.get()
                    + f"  (cached {payload.get('at', '?')} — click again to re-scan)")
                return
        self._lib_lowqual_showing_cache = False
        self._lib_status_var.set("Scanning movies for cams and low-bitrate files…")

        def worker() -> None:
            from library_index import list_all_files
            from video_quality import find_low_quality_movies
            movies = [
                (e.path, e.name, e.size_bytes) for e in list_all_files()
                if media_type_for_path(e.root_path) == "movie"
            ]

            def progress(done: int, total: int) -> None:
                self._post_to_ui(lambda: self._lib_status_var.set(
                    f"Analysing movie {done}/{total}…"))

            try:
                results = find_low_quality_movies(movies, progress=progress)
            except Exception as exc:
                logger.exception("Low-quality scan failed.")
                self._post_to_ui(lambda: self._lib_status_var.set(f"Scan failed: {exc}"))
                return
            self._lib_lowqual_save_cache(results, len(movies))
            self._post_to_ui(lambda: self._lib_render_lowqual(results, len(movies)))

        threading.Thread(target=worker, name="lib-lowqual", daemon=True).start()

    def _lib_render_lowqual(self, results: list[Any], scanned: int) -> None:
        tree = self.library_results_tree
        if tree is None:
            return
        self._lib_view_mode = "lowqual"
        self._lib_rows = results
        for iid in tree.get_children():
            tree.delete(iid)
        for idx, m in enumerate(results):
            notes = []
            if m.cam_hit:
                notes.append(f"⚠ {m.cam_hit}")
            if not m.rate_exact:
                notes.append("rate assumes 2h (install ffmpeg for exact)")
            if m.redundant_with:
                notes.append(f"redundant — better file at {m.redundant_with}")
            tree.insert("", "end", iid=str(idx),
                        values=(m.name, "movie", self._fmt_bytes(m.size_bytes),
                                f"{m.rate_mb_min:.1f}", " · ".join(notes), m.path))
        redundant = sum(1 for m in results if m.redundant_with)
        self._lib_status_var.set(
            f"{len(results)} low-quality movie(s) of {scanned} scanned — cams first, "
            f"worst bitrate first. {redundant} are redundant (a better copy exists) "
            "and can just be deleted; select the rest and click Replace.")

    def _lib_replace_selected(self) -> None:
        if self._lib_view_mode != "lowqual":
            self._show_warning("Run the scan first",
                               "Click 'Search Low-Quality Movies' and select rows from "
                               "its results to replace.")
            return
        tree = self.library_results_tree
        if tree is None:
            return
        selected = [self._lib_rows[int(iid)] for iid in tree.selection()
                    if int(iid) < len(self._lib_rows)]
        if not selected:
            self._show_warning("Nothing selected", "Select low-quality movies to replace.")
            return
        redundant = [m for m in selected if m.redundant_with]
        if redundant and not self._ask_yes_no(
            "Redundant copies included",
            f"{len(redundant)} of the selected files already have a better version in "
            "the library — replacing them downloads ANOTHER copy. Usually you just "
            "delete those instead.\n\nContinue anyway?",
        ):
            return
        if not self._ask_yes_no(
            "Replace with non-cam versions",
            f"Search for and download proper (non-cam) versions of {len(selected)} "
            "movie(s)? Each old file is deleted automatically once its replacement "
            "finishes and moves into the library.",
        ):
            return
        self._lib_status_var.set(f"Searching replacements for {len(selected)} movie(s)…")

        def worker() -> None:
            from maintenance import _normalize_title
            started = 0
            skipped: list[str] = []
            for m in selected:
                title = _normalize_title(m.name)
                year_match = re.search(r"\((\d{4})\)", m.name) or re.search(r"\b(19|20)\d{2}\b", m.name)
                query = f"{title} {year_match.group(0).strip('()')}" if year_match else title
                download_id = self.download_manager.replace_low_quality_movie(query, m.path)
                if download_id is not None:
                    started += 1
                else:
                    skipped.append(m.name)

            def done() -> None:
                msg = f"Started {started} replacement download(s)"
                if skipped:
                    msg += f"; no acceptable non-cam release found for {len(skipped)}"
                self._lib_status_var.set(msg + " — see the Downloads tab.")
                self.refresh_downloads()
            self._post_to_ui(done)

        threading.Thread(target=worker, name="lib-replace", daemon=True).start()

    def _lib_delete_selected(self) -> None:
        paths = self._lib_selected_paths()
        if not paths:
            self._show_warning("Nothing selected", "Select files to delete first.")
            return
        preview = "\n".join(f"  {p}" for p in paths[:10])
        if len(paths) > 10:
            preview += f"\n  … and {len(paths) - 10} more"
        if not self._ask_yes_no(
            "Delete files",
            f"Delete {len(paths)} file(s) (to the recycle bin)?\n\n{preview}",
        ):
            return

        def worker() -> None:
            from library_index import remove_from_index
            deleted, errors, _empty = delete_files_with_cleanup(paths)
            remove_from_index(deleted)

            def done() -> None:
                msg = f"Deleted {len(deleted)} file(s)"
                if errors:
                    msg += f" — {len(errors)} error(s)"
                    self._show_warning("Delete", "\n".join(errors[:8]))
                self._lib_status_var.set(msg)
                if self._lib_view_mode == "lowqual":
                    # Deleted rows make the cached scan stale — force fresh.
                    self._lib_lowqual_showing_cache = True
                    self._lib_search_lowqual()
                else:
                    self._lib_show_all()
            self._post_to_ui(done)

        threading.Thread(target=worker, name="lib-delete", daemon=True).start()

    def _lib_find_subs(self) -> None:
        paths = self._lib_selected_paths()
        if not paths:
            self._show_warning("Nothing selected", "Select movies to fetch subtitles for.")
            return
        language = self._lib_lang_var.get() or "en"
        self._lib_status_var.set(f"Fetching {language} subtitles for {len(paths)} file(s)…")

        def worker() -> None:
            from subtitles import download_subtitles

            def progress(done: int, total: int, name: str) -> None:
                self._post_to_ui(lambda: self._lib_status_var.set(
                    f"Subtitles {done + 1}/{total}: {name[:50]}"))

            saved, errors = download_subtitles(paths, language, progress=progress)

            def done() -> None:
                self._lib_status_var.set(
                    f"Subtitles saved for {saved}/{len(paths)} file(s)"
                    + (f" — {len(errors)} issue(s)" if errors else ""))
                if errors:
                    self._show_warning("Subtitles", "\n".join(errors[:10]))
            self._post_to_ui(done)

        threading.Thread(target=worker, name="lib-subs", daemon=True).start()

    # =====================================================================
    # Downloads tab — in-app torrent search, grab, routing, history
    # =====================================================================

    def _build_downloads_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Search").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(toolbar, textvariable=self._dl_search_var)
        search_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        search_entry.bind("<Return>", lambda _e: self.search_torrents_from_ui())
        ttk.Combobox(
            toolbar, textvariable=self._dl_type_var, state="readonly", width=8,
            values=(["movie", "tv", "anime", "xanime", "other"]
                    if config.XANIME_ENABLED
                    else ["movie", "tv", "anime", "other"]),
        ).grid(row=0, column=2, padx=(0, 6))
        search_btn = ttk.Button(toolbar, text="Search", command=self.search_torrents_from_ui)
        search_btn.grid(row=0, column=3, padx=(0, 6))
        add_tooltip(search_btn, "Search torrent sources for this title "
                                "(YTS/TPB for movies+TV, nyaa for anime, sukebei for xanime).")
        dl_btn = ttk.Button(toolbar, text="⬇ Download Selected", style="Accent.TButton",
                            command=self.download_selected_result)
        dl_btn.grid(row=0, column=4)
        add_tooltip(dl_btn, "Download the selected result to the staging folder; "
                            "renamed/moved per the checkboxes below.")

        options = ttk.Frame(tab)
        options.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Checkbutton(
            options, text="Automatic rename", variable=self._dl_auto_rename_var,
            command=lambda: self._persist_dl_toggle("TORRENT_AUTO_RENAME", self._dl_auto_rename_var),
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(
            options, text="Move to destination", variable=self._dl_auto_move_var,
            command=lambda: self._persist_dl_toggle("TORRENT_AUTO_MOVE", self._dl_auto_move_var),
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(
            options, text="Auto-grab new requests", variable=self._dl_auto_grab_var,
            command=lambda: self._persist_dl_toggle("TORRENT_AUTO_GRAB", self._dl_auto_grab_var),
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(options, textvariable=self._dl_plan_var, foreground=_MUTED_TEXT,
                  font=("Segoe UI", 9, "italic")).pack(side=tk.LEFT, padx=(8, 0))

        panes = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        panes.grid(row=2, column=0, sticky="nsew")

        results_frame = ttk.LabelFrame(panes, text="Search results", padding=4)
        panes.add(results_frame, weight=2)
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)
        results_tree = ttk.Treeview(
            results_frame, columns=("title", "size", "seeders", "source"),
            show="headings", selectmode="browse",
        )
        results_tree.heading("title", text="Title")
        results_tree.heading("size", text="Size")
        results_tree.heading("seeders", text="Seeders")
        results_tree.heading("source", text="Source")
        results_tree.column("title", width=520, anchor=tk.W)
        results_tree.column("size", width=90, anchor=tk.E, stretch=False)
        results_tree.column("seeders", width=70, anchor=tk.E, stretch=False)
        results_tree.column("source", width=80, anchor=tk.W, stretch=False)
        results_tree.grid(row=0, column=0, sticky="nsew")
        results_scroll = ttk.Scrollbar(results_frame, orient=tk.VERTICAL, command=results_tree.yview)
        results_scroll.grid(row=0, column=1, sticky="ns")
        results_tree.configure(yscrollcommand=results_scroll.set)
        results_tree.bind("<<TreeviewSelect>>", lambda _e: self._preview_route_for_selected_result())
        results_tree.bind("<Double-1>", lambda _e: self.download_selected_result())
        make_sortable(results_tree)
        self._dl_results_tree = results_tree

        downloads_frame = ttk.LabelFrame(panes, text="Downloads", padding=4)
        panes.add(downloads_frame, weight=2)
        downloads_frame.columnconfigure(0, weight=1)
        downloads_frame.rowconfigure(1, weight=1)

        dl_bar = ttk.Frame(downloads_frame)
        dl_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(dl_bar, textvariable=self._dl_status_var, foreground=_MUTED_TEXT).pack(side=tk.LEFT)
        ttk.Button(dl_bar, text="Open Staging Folder", command=self.open_staging_folder).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(dl_bar, text="Cancel", command=self.cancel_selected_download).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(dl_bar, text="Apply Route", command=self.apply_route_to_selected_download).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(dl_bar, text="Refresh", command=self.refresh_downloads).pack(side=tk.RIGHT, padx=(6, 0))

        downloads_tree = ttk.Treeview(
            downloads_frame, columns=("id", "title", "status", "progress", "route"),
            show="headings", selectmode="browse",
        )
        for col, text, width, anchor, stretch in (
            ("id", "ID", 40, tk.E, False),
            ("title", "Title", 380, tk.W, True),
            ("status", "Status", 90, tk.W, False),
            ("progress", "%", 50, tk.E, False),
            ("route", "Destination / route", 320, tk.W, True),
        ):
            downloads_tree.heading(col, text=text)
            downloads_tree.column(col, width=width, anchor=cast(Any, anchor), stretch=stretch)
        downloads_tree.grid(row=1, column=0, sticky="nsew")
        downloads_scroll = ttk.Scrollbar(downloads_frame, orient=tk.VERTICAL, command=downloads_tree.yview)
        downloads_scroll.grid(row=1, column=1, sticky="ns")
        downloads_tree.configure(yscrollcommand=downloads_scroll.set)
        downloads_tree.bind("<Button-3>", self._on_download_right_click)
        make_sortable(downloads_tree)
        self._dl_downloads_tree = downloads_tree

        history_frame = ttk.LabelFrame(
            panes, text="History (downloads / renames / moves — before → after)", padding=4)
        panes.add(history_frame, weight=1)
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)
        history_tree = ttk.Treeview(
            history_frame, columns=("at", "action", "before", "after"),
            show="headings", selectmode="browse",
        )
        for col, text, width, anchor, stretch in (
            ("at", "When", 130, tk.W, False),
            ("action", "Action", 90, tk.W, False),
            ("before", "Before", 330, tk.W, True),
            ("after", "After", 330, tk.W, True),
        ):
            history_tree.heading(col, text=text)
            history_tree.column(col, width=width, anchor=cast(Any, anchor), stretch=stretch)
        history_tree.grid(row=0, column=0, sticky="nsew")
        history_scroll = ttk.Scrollbar(history_frame, orient=tk.VERTICAL, command=history_tree.yview)
        history_scroll.grid(row=0, column=1, sticky="ns")
        history_tree.configure(yscrollcommand=history_scroll.set)
        make_sortable(history_tree)
        self._dl_history_tree = history_tree

    def _on_download_update(self, download_id: int) -> None:
        # Called from download worker threads — marshal to the UI thread.
        self._post_to_ui(self.refresh_downloads)
        self._post_to_ui(self.refresh_activity_feed)

    def refresh_activity_feed(self) -> None:
        """The Status tab's live feed: active downloads with progress on
        top, then the recent download/rename/move history, condensed."""
        tree = getattr(self, "_activity_tree", None)
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)

        rows = downloads_store.list_downloads(limit=100)
        active = [r for r in rows if r.status in ("downloading", "queued")]
        for r in active:
            marker = f"⬇ {r.progress * 100:.0f}%" if r.status == "downloading" else "⏳ queued"
            tree.insert("", "end", values=("now", marker, r.title))
        for h in downloads_store.list_history(limit=80):
            icons = {"downloaded": "✔ done", "renamed": "✎ renamed",
                     "moved": "→ moved", "error": "✖ error",
                     "grabbed": "+ grabbed", "cancelled": "✖ cancelled",
                     "stopped": "⏸ stopped", "rotated": "↻ rotated",
                     "replaced": "♻ replaced"}
            what = icons.get(h.action, h.action)
            detail = h.after_value or h.before_value or ""
            if h.action in ("renamed", "moved") and h.before_value and h.after_value:
                detail = f"{Path(h.before_value).name} → {h.after_value}"
            tree.insert("", "end", values=(h.at, what, detail[:200]))
        downloading = sum(1 for r in active if r.status == "downloading")
        queued = len(active) - downloading
        self._activity_summary_var.set(
            f"{downloading} downloading, {queued} queued" if active else "no active downloads")
        self._refresh_needs_you()

    def _refresh_needs_you(self) -> None:
        """The Status tab's 'Needs you' line: every user-actionable count plus
        what's waiting on the app. Amber when something needs the user."""
        var = getattr(self, "_needs_you_var", None)
        if var is None:
            return
        try:
            import grab_queue
            counts = grab_queue.status_counts()
            line = grab_queue.status_summary_line(counts)
        except Exception:
            logger.debug("needs-you refresh failed", exc_info=True)
            return
        var.set(line or "✓ All clear — nothing needs you")
        label = getattr(self, "_needs_you_label", None)
        if label is not None:
            try:
                label.configure(
                    foreground=(_DOT_AMBER if counts.actionable else _DOT_GREEN))
            except tk.TclError:
                pass

    def _persist_dl_toggle(self, key: str, var: tk.BooleanVar) -> None:
        value = bool(var.get())
        setattr(config, key, value)
        try:
            save_settings({key: "true" if value else "false"}, config.DOTENV_PATH)
        except Exception:
            logger.exception("Failed to persist %s to .env", key)

    def search_torrents_from_ui(self, *, keep_context: bool = False) -> None:
        query = self._dl_search_var.get().strip()
        if not query:
            self._show_warning("Missing search", "Enter a title to search for.")
            return
        if not keep_context:
            # A fresh manual search must not inherit a stale request/episode
            # link from an earlier jump into this tab.
            self._dl_request_context = None
            self._dl_episode_context = None
        media_type = self._dl_type_var.get()
        self._dl_status_var.set(f'Searching torrent sources for "{query}"…')

        def worker() -> None:
            try:
                results = search_torrents(query, media_type, limit=40)
            except Exception as exc:
                logger.exception("Torrent search failed.")
                self._post_to_ui(lambda: self._dl_status_var.set(f"Search failed: {exc}"))
                return
            self._post_to_ui(lambda: self._handle_torrent_results(query, results))

        threading.Thread(target=worker, name="ui-torrent-search", daemon=True).start()

    def _handle_torrent_results(self, query: str, results: list[TorrentResult]) -> None:
        if self._dl_results_tree is None:
            return
        self._dl_results = results
        for item in self._dl_results_tree.get_children():
            self._dl_results_tree.delete(item)
        for idx, r in enumerate(results):
            self._dl_results_tree.insert(
                "", "end", iid=str(idx),
                values=(r.title, format_size(r.size_bytes), r.seeders, r.source),
            )
        self._dl_status_var.set(f'{len(results)} result(s) for "{query}"')
        if not results:
            self._dl_plan_var.set("No results — try another search term or media type.")

    def _selected_torrent_result(self) -> TorrentResult | None:
        if self._dl_results_tree is None:
            return None
        selected = self._dl_results_tree.selection()
        if not selected:
            return None
        try:
            return self._dl_results[int(selected[0])]
        except (ValueError, IndexError):
            return None

    def _preview_route_for_selected_result(self) -> None:
        result = self._selected_torrent_result()
        if result is None:
            return
        context_title = self._dl_request_context[1] if self._dl_request_context else None

        def worker() -> None:
            try:
                plan = torrent_routing.plan_route(
                    result.title, self._dl_type_var.get(), request_title=context_title,
                )
                text = plan.describe()
            except Exception as exc:
                text = f"route preview failed: {exc}"
            self._post_to_ui(lambda: self._dl_plan_var.set(text))

        threading.Thread(target=worker, name="ui-route-preview", daemon=True).start()

    def download_selected_result(self) -> None:
        result = self._selected_torrent_result()
        if result is None:
            self._show_warning("No result selected", "Select a search result first.")
            return
        media_type = self._dl_type_var.get()
        # The search result rows carry the source's media type, but the combo
        # box is authoritative — the admin may have overridden it.
        result = TorrentResult(
            title=result.title, magnet=result.magnet, size_bytes=result.size_bytes,
            seeders=result.seeders, source=result.source, media_type=media_type,
        )
        request_id, request_title = (self._dl_request_context or (None, None))
        grab_kwargs = dict(
            request_id=request_id,
            request_title=request_title,
            auto_rename=bool(self._dl_auto_rename_var.get()),
            auto_move=bool(self._dl_auto_move_var.get()),
            episode_context=self._dl_episode_context,
        )
        # Manual pick: run the hard gates as a preflight (mode manual-user-pick).
        # A gate rejection (sequel/identity/country mismatch, CAM, oversize)
        # requires a typed override, recorded in the selection run.
        outcome = self.download_manager.manual_grab(result, **grab_kwargs)
        if outcome.needs_override:
            confirmed = self._ask_yes_no(
                "Override selection gate",
                f"This pick was flagged: {outcome.reason_code}\n"
                f"{outcome.detail}\n\nDownload it anyway?")
            if not confirmed:
                self._dl_status_var.set(
                    f"Grab cancelled — flagged {outcome.reason_code}")
                return
            outcome = self.download_manager.manual_grab(
                result, override_reason=f"user confirmed via UI ({outcome.reason_code})",
                **grab_kwargs)
        if not outcome.ok or outcome.download_id is None:
            self._dl_status_var.set(f"Grab did not start: {outcome.reason_code}")
            return
        self._dl_status_var.set(
            f"Started download #{outcome.download_id}: {result.title}")
        self.last_action_var.set("Last action: torrent grab")
        self.refresh_downloads()

    def _selected_download_id(self) -> int | None:
        if self._dl_downloads_tree is None:
            return None
        selected = self._dl_downloads_tree.selection()
        if not selected:
            return None
        values = self._dl_downloads_tree.item(selected[0], "values")
        return int(values[0]) if values else None

    @staticmethod
    def _download_row_order(row: Any) -> tuple[int, int]:
        """Downloads-tab ordering (per Cole): actively downloading first,
        partial (stopped with progress) next, then queued, then failed,
        then completed/renamed/moved. Newest first within each group."""
        status = row.status
        progress = row.progress or 0.0
        if status == "downloading":
            group = 0
        elif status in ("queued", "cancelled", "stalled") and progress > 0:
            group = 1  # partial — will resume from existing data
        elif status in ("queued", "pending"):
            group = 2
        elif status == "error":
            group = 3
        else:
            group = 4  # downloaded / moved / other terminal states
        return (group, -row.download_id)

    def refresh_downloads(self) -> None:
        if self._dl_downloads_tree is None:
            return
        selected_id = self._selected_download_id()
        for item in self._dl_downloads_tree.get_children():
            self._dl_downloads_tree.delete(item)
        for row in sorted(downloads_store.list_downloads(limit=100),
                          key=self._download_row_order):
            route = row.error if row.status == "error" else (
                (f"{row.planned_dest or ''}" + (f" / {row.planned_name}" if row.planned_name else ""))
                or row.route_reason or ""
            )
            # Honest queue label (fetching metadata / no peers / stalled /
            # waiting for slot / probing) instead of a bare status + 0%.
            dm = getattr(self, "download_manager", None)
            phase = dm.phase_for(row.download_id) if dm is not None else None
            status_label = downloads_store.display_status(
                row.status, row.progress, error=row.error, phase=phase)
            iid = str(row.download_id)
            self._dl_downloads_tree.insert(
                "", "end", iid=iid,
                values=(row.download_id, row.title, status_label,
                        f"{row.progress * 100:.0f}", route),
            )
            if selected_id is not None and row.download_id == selected_id:
                self._dl_downloads_tree.selection_set(iid)
        self.refresh_download_history()

    def refresh_download_history(self) -> None:
        if self._dl_history_tree is None:
            return
        for item in self._dl_history_tree.get_children():
            self._dl_history_tree.delete(item)
        for h in downloads_store.list_history(limit=200):
            self._dl_history_tree.insert(
                "", "end",
                values=(h.at, h.action, h.before_value or "", h.after_value or ""),
            )

    def apply_route_to_selected_download(self) -> None:
        download_id = self._selected_download_id()
        if download_id is None:
            self._show_warning("No download selected", "Select a download first.")
            return

        def worker() -> None:
            outcome = self.download_manager.apply_route(download_id)
            self._post_to_ui(lambda: self._handle_apply_route_result(outcome))

        threading.Thread(target=worker, name="ui-apply-route", daemon=True).start()

    def _handle_apply_route_result(self, outcome: str) -> None:
        self._dl_status_var.set(outcome)
        self.refresh_downloads()

    def _on_download_right_click(self, event: Any) -> None:
        tree = self._dl_downloads_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        download_id = int(iid)
        row = downloads_store.get_download(download_id)
        if row is None:
            return

        def run(op, label: str) -> None:
            def worker() -> None:
                try:
                    outcome = op(download_id)
                except Exception as exc:
                    outcome = f"{label} failed: {exc}"
                self._post_to_ui(lambda: (self._dl_status_var.set(f"#{download_id}: {outcome}"),
                                          self.refresh_downloads()))
            threading.Thread(target=worker, name=f"dl-{label}", daemon=True).start()

        dm = self.download_manager
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="⏸ Stop / Pause", command=lambda: run(dm.stop, "stop"))
        menu.add_command(label="▶ Restart / Resume", command=lambda: run(dm.restart, "restart"))
        menu.add_command(label="🔍 Recheck data", command=lambda: run(dm.recheck, "recheck"))
        menu.add_separator()
        menu.add_command(label="Remove (keep files)",
                         command=lambda: run(lambda d: dm.remove(d, delete_files=False), "remove"))

        def remove_with_files() -> None:
            if self._ask_yes_no("Remove and delete files",
                                f"Remove download #{download_id} AND recycle its "
                                "staged files? (Files already moved into the "
                                "library are not touched.)"):
                run(lambda d: dm.remove(d, delete_files=True), "remove+delete")
        menu.add_command(label="🗑 Remove and delete files…", command=remove_with_files)
        menu.add_separator()
        menu.add_command(
            label=f"🔎 Search again for '{row.title[:40]}…'",
            command=lambda: self.open_downloads_search(
                row.title, row.media_type,
                episode_context=((row.show_id, row.season, row.episode)
                                 if row.show_id is not None and row.season is not None
                                 and row.episode is not None else None)),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def cancel_selected_download(self) -> None:
        download_id = self._selected_download_id()
        if download_id is None:
            self._show_warning("No download selected", "Select a download first.")
            return
        if self.download_manager.cancel(download_id):
            self._dl_status_var.set(f"Cancelled download #{download_id}")
        else:
            self._dl_status_var.set(f"Download #{download_id} is not running.")
        self.refresh_downloads()

    def open_staging_folder(self) -> None:
        staging = Path(config.TORRENT_DOWNLOAD_DIR)
        staging.mkdir(parents=True, exist_ok=True)
        # os.startfile on Windows, xdg-open on Linux — adapter decides.
        if not platform_adapter.open_path(staging):
            self._dl_status_var.set(
                f"Couldn't open a file manager — staging folder: {staging}")

    def open_downloads_search(
        self, query: str, media_type: str,
        *, request_context: tuple[int, str] | None = None,
        episode_context: tuple[int, int, int] | None = None,
    ) -> None:
        """Jump to the Downloads tab with a search pre-filled and running.

        Used by the Requests tab ("Grab Torrent") and the Shows tab ("Find
        Torrent for Selected Episode"). episode_context ties the eventual
        grab to a tracked show's episode for deterministic routing.
        """
        if media_type not in ("movie", "tv", "anime", "xanime"):
            media_type = "other"
        self._dl_request_context = request_context
        self._dl_episode_context = episode_context
        self._dl_search_var.set(query)
        self._dl_type_var.set(media_type)
        if self._notebook is not None and self._downloads_tab is not None:
            self._notebook.select(self._downloads_tab)
        self.search_torrents_from_ui(keep_context=True)

    def grab_torrent_for_selected_request(self) -> None:
        """Requests tab → Downloads tab: pre-fill the search for a request."""
        request_id = self._selected_request_id()
        if request_id is None:
            self._show_warning("No request selected", "Select a request in the list first.")
            return
        req = get_request(request_id)
        if req is None:
            self._show_warning("Request not found", f"Request #{request_id} no longer exists.")
            return
        query = (req.resolved_title or req.content or "").strip()
        self.open_downloads_search(
            query, req.media_type, request_context=(request_id, query),
        )

    # --- Auto-grab poller -------------------------------------------------

    def _schedule_auto_grab(self) -> None:
        if self._quitting:
            return
        # First pass shortly after startup, then every 5 minutes.
        self.root.after(30_000, self._auto_grab_tick)

    def _auto_grab_tick(self) -> None:
        if self._quitting:
            return
        if config.TORRENT_AUTO_GRAB:
            def worker() -> None:
                try:
                    started = self.download_manager.auto_grab_open_requests()
                except Exception:
                    logger.exception("Auto-grab pass failed.")
                    return
                if started:
                    self._post_to_ui(self.refresh_downloads)
            threading.Thread(target=worker, name="ui-auto-grab", daemon=True).start()

        # Shows loop: auto-grab missing episodes at most every 6 hours (the
        # pass re-syncs stale shows itself, so freshly-aired episodes appear).
        # THE BUG THAT STRANDED TANYA S02E01: this used to require the GLOBAL
        # toggle, so per-show 🆕/✅ flags never triggered a pass at all.
        def _any_flagged() -> bool:
            try:
                import shows_store as _ss
                return any(s.auto_grab or s.follow_new for s in _ss.list_shows())
            except Exception:
                return False

        if (time.time() - self._last_shows_grab_pass > 6 * 3600
                and (config.SHOWS_AUTO_GRAB or _any_flagged())):
            self._last_shows_grab_pass = time.time()

            def shows_worker() -> None:
                try:
                    started = self.download_manager.auto_grab_missing_episodes()
                except Exception:
                    logger.exception("Shows auto-grab pass failed.")
                    return
                if started:
                    logger.info("Shows auto-grab started %d download(s).", len(started))
                    self._post_to_ui(self.refresh_downloads)
                    self._post_to_ui(self.shows_tab.refresh)
            threading.Thread(target=shows_worker, name="ui-shows-auto-grab", daemon=True).start()

        self.root.after(300_000, self._auto_grab_tick)

    # =====================================================================
    # Users tab — Telegram access requests and the allowlist
    # =====================================================================

    def _build_users_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(3, weight=2)

        pending_bar = ttk.Frame(tab)
        pending_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(pending_bar, text="Pending access requests", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(pending_bar, text="Refresh", command=self.refresh_users_tab).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(pending_bar, text="Deny", style="Danger.TButton", command=self.deny_selected_access_request).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(pending_bar, text="Approve", style="Accent.TButton", command=self.approve_selected_access_request).pack(side=tk.RIGHT, padx=(6, 0))

        pending_tree = ttk.Treeview(
            tab, columns=("id", "user_id", "name", "username", "requested"),
            show="headings", height=4, selectmode="browse",
        )
        for col, text, width, stretch in (
            ("id", "Req#", 50, False), ("user_id", "Telegram ID", 110, False),
            ("name", "Name", 220, True), ("username", "Username", 150, False),
            ("requested", "Requested", 140, False),
        ):
            pending_tree.heading(col, text=text)
            pending_tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        pending_tree.grid(row=1, column=0, sticky="nsew")
        self._users_pending_tree = pending_tree

        allowed_bar = ttk.Frame(tab)
        allowed_bar.grid(row=2, column=0, sticky="ew", pady=(10, 4))
        ttk.Label(allowed_bar, text="Allowed users", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(allowed_bar, text="Revoke Selected", style="Danger.TButton",
                   command=self.revoke_selected_allowed_user).pack(side=tk.RIGHT)
        history_btn = ttk.Button(allowed_bar, text="📺 Watch History…",
                                 command=self.open_watch_history)
        history_btn.pack(side=tk.RIGHT, padx=(6, 6))
        add_tooltip(history_btn, "Who watched what on Plex, most recent first "
                                 "(needs a Plex token).")
        # Map a Telegram user to a Plex account (drives per-user features).
        self._users_plex_var = tk.StringVar()
        self._users_plex_combo = ttk.Combobox(
            allowed_bar, textvariable=self._users_plex_var, width=16,
            state="readonly", values=[])
        # Refetch whenever the dropdown opens — a failed fetch at startup
        # (Plex still booting, transient timeout) must not leave it empty
        # for the whole session.
        self._users_plex_combo.configure(
            postcommand=self._load_plex_accounts_for_users_tab)
        self._users_plex_combo.pack(side=tk.RIGHT, padx=(6, 0))
        map_btn = ttk.Button(allowed_bar, text="Map → Plex user",
                             command=self.map_selected_user_to_plex)
        map_btn.pack(side=tk.RIGHT)
        add_tooltip(map_btn, "Link the selected Telegram user to the chosen Plex "
                             "account — used by Watchlist and history features.")

        allowed_tree = ttk.Treeview(
            tab, columns=("id", "user_id", "name", "username", "plex", "source", "claimed"),
            show="headings", height=8, selectmode="browse",
        )
        for col, text, width, stretch in (
            ("id", "#", 40, False), ("user_id", "Telegram ID", 110, False),
            ("name", "Name", 180, True), ("username", "Username", 130, False),
            ("plex", "Plex user", 120, False),
            ("source", "Source", 140, False), ("claimed", "Claimed", 130, False),
        ):
            allowed_tree.heading(col, text=text)
            allowed_tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        allowed_tree.grid(row=3, column=0, sticky="nsew")
        self._users_allowed_tree = allowed_tree
        self._load_plex_accounts_for_users_tab()

    def _load_plex_accounts_for_users_tab(self) -> None:
        def worker() -> None:
            try:
                from plex_api import list_plex_accounts
                accounts = list_plex_accounts()
            except Exception:
                accounts = []
            self._post_to_ui(
                lambda: self._users_plex_combo.configure(values=[""] + accounts))

        threading.Thread(target=worker, name="ui-plex-accounts", daemon=True).start()

    def map_selected_user_to_plex(self) -> None:
        if self._users_allowed_tree is None:
            return
        selected = self._users_allowed_tree.selection()
        if not selected:
            self._show_warning("No user selected", "Select an allowed user first.")
            return
        auth_store.set_plex_username(int(selected[0]), self._users_plex_var.get() or None)
        self.refresh_users_tab()

    def refresh_users_tab(self) -> None:
        if self._users_pending_tree is None or self._users_allowed_tree is None:
            return
        pending = auth_store.list_access_requests(status="pending")
        for item in self._users_pending_tree.get_children():
            self._users_pending_tree.delete(item)
        for r in pending:
            self._users_pending_tree.insert(
                "", "end", iid=str(r.request_id),
                values=(r.request_id, r.telegram_user_id, r.display_name or "",
                        f"@{r.username}" if r.username else "", r.requested_at),
            )

        for item in self._users_allowed_tree.get_children():
            self._users_allowed_tree.delete(item)
        for u in auth_store.list_allowed_users():
            self._users_allowed_tree.insert(
                "", "end", iid=str(u.row_id),
                values=(u.row_id, u.telegram_user_id or "—", u.display_name or "",
                        f"@{u.username}" if u.username else "",
                        u.plex_username or "", u.source,
                        u.claimed_at or "unclaimed"),
            )

        # Badge the tab title with the pending count so requests get noticed.
        if self._notebook is not None and self._users_tab is not None:
            label = f"Users ({len(pending)})" if pending else "Users"
            self._notebook.tab(self._users_tab, text=label)

    def _selected_pending_request_id(self) -> int | None:
        if self._users_pending_tree is None:
            return None
        selected = self._users_pending_tree.selection()
        return int(selected[0]) if selected else None

    def approve_selected_access_request(self) -> None:
        request_id = self._selected_pending_request_id()
        if request_id is None:
            self._show_warning("No request selected", "Select a pending request first.")
            return

        def worker() -> None:
            resolved = auth_store.approve_access_request(request_id)
            if resolved is not None and resolved.chat_id is not None:
                self.bot_service.notify_user(
                    resolved.chat_id,
                    "🎉 You've been approved! Send /start to begin using the bot.",
                )
            self._post_to_ui(self.refresh_users_tab)

        threading.Thread(target=worker, name="ui-approve-user", daemon=True).start()

    def deny_selected_access_request(self) -> None:
        request_id = self._selected_pending_request_id()
        if request_id is None:
            self._show_warning("No request selected", "Select a pending request first.")
            return
        if not self._ask_yes_no("Deny access", "Deny this user? They won't be re-prompted."):
            return

        def worker() -> None:
            resolved = auth_store.deny_access_request(request_id)
            if resolved is not None and resolved.chat_id is not None:
                self.bot_service.notify_user(
                    resolved.chat_id, "⛔ Your access request was declined."
                )
            self._post_to_ui(self.refresh_users_tab)

        threading.Thread(target=worker, name="ui-deny-user", daemon=True).start()

    def revoke_selected_allowed_user(self) -> None:
        if self._users_allowed_tree is None:
            return
        selected = self._users_allowed_tree.selection()
        if not selected:
            self._show_warning("No user selected", "Select an allowed user first.")
            return
        row_id = int(selected[0])
        if not self._ask_yes_no("Revoke access", "Remove this user from the allowlist?"):
            return
        auth_store.remove_allowed_user(row_id)
        self.refresh_users_tab()

    # =====================================================================
    # Scheduled refresh loops
    # =====================================================================

    def _schedule_log_refresh(self) -> None:
        self._refresh_logs()
        if not self._quitting:
            self.root.after(1000, self._schedule_log_refresh)

    def _schedule_status_refresh(self) -> None:
        if self._quitting:
            return
        self.root.after(config.ADMIN_STATUS_REFRESH_SECONDS * 1000, self._status_refresh_tick)

    def _status_refresh_tick(self) -> None:
        if self._quitting:
            return
        self.refresh_status()
        self.refresh_requests()
        self.refresh_library_summary()
        self.refresh_library_metrics()
        self.refresh_users_tab()  # keep the pending-request badge current
        self.refresh_activity_feed()
        self._schedule_status_refresh()

    def _refresh_file_ledger_views(self) -> None:
        """Fill the File Changes / Missing Files trees from the ledger."""
        try:
            from library_index import list_file_events, list_missing_files
        except ImportError:
            return
        tree = getattr(self, "_file_changes_tree", None)
        if tree is not None:
            for iid in tree.get_children():
                tree.delete(iid)
            for e in list_file_events(limit=1000):
                tree.insert("", "end", values=(e.at, e.event, e.path, e.detail))
        tree = getattr(self, "_missing_files_tree", None)
        if tree is not None:
            for iid in tree.get_children():
                tree.delete(iid)
            for e in list_missing_files(limit=1000):
                tree.insert("", "end", values=(e.at, e.path, e.detail))

    def _refresh_logs(self) -> None:
        if self.log_text is None:
            return
        logs = get_recent_logs()
        if len(logs) == self._last_log_count:
            return
        self._last_log_count = len(logs)
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert("1.0", "\n".join(logs))
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # =====================================================================
    # Maintenance tab — UI helpers
    # =====================================================================

    # Note: the old _maint_set_busy (which CLEARED the results tree and set a
    # bare string) is gone — Task I item 5. The registry's "started" event
    # sets the status, and the tree keeps its last results through a run.

    def _maint_require_library_paths(self, tool_label: str) -> bool:
        """
        Ensure PLEX_LIBRARY_PATHS is configured before running filesystem
        maintenance tools. Returns True when OK, False (with popup + status
        update) when the user needs to configure paths first.
        """
        if config.PLEX_LIBRARY_PATHS:
            return True
        example = (r"PLEX_LIBRARY_PATHS=D:\Movies;D:\TV;E:\Anime"
                   if sys.platform == "win32" else
                   "PLEX_LIBRARY_PATHS=/mnt/media/Movies;/mnt/media/TV")
        msg = (
            f"{tool_label} needs PLEX_LIBRARY_PATHS to be set in your .env file.\n\n"
            "Add a semicolon-separated list of folders, e.g.:\n"
            + example
        )
        self._maint_status_var.set(f"{tool_label}: PLEX_LIBRARY_PATHS not configured")
        self._show_warning(tool_label, msg)
        return False

    def _maint_require_plex_token(self, tool_label: str) -> bool:
        """Ensure PLEX_TOKEN is configured before running Plex-API-based tools."""
        if config.PLEX_TOKEN and config.PLEX_SERVER_URL:
            return True
        msg = (
            f"{tool_label} needs Plex API access.\n\n"
            "Click 'Get Plex Token' to authorize, then try again."
        )
        self._maint_status_var.set(f"{tool_label}: Plex token not configured")
        self._show_warning(tool_label, msg)
        return False

    def _populate_maint_tree(
        self,
        rows: list[tuple[str, str, str, str]],
        *,
        col1: str = "Item",
        col2: str = "Detail",
        col3: str = "Extra",
        check_label: str = "[/]",
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] | None = None,
        default_checked: bool = False,
    ) -> None:
        """
        Render rows into the maintenance results tree.

        The legacy `rows` parameter (list of (check, c1, c2, c3) 4-tuples) is
        kept so existing tools that don't carry a media-type tag still work —
        they're treated as type "mixed" and never get filtered out.

        check_label names the checkbox column with the ACTION Apply Selected
        will take ("Delete?", "Rename?"), so there's no ambiguity about what
        ticking a row means.

        New callers should pass `typed_rows` so the media-type filter
        checkboxes can hide/show entries without re-running the tool. Each
        typed row is (media_type_tag, (check, c1, c2, c3), action_payload).
        """
        if self._maint_tree is None:
            return
        self._maint_tree.heading("check", text=check_label)
        self._maint_tree.heading("col1", text=col1)
        self._maint_tree.heading("col2", text=col2)
        self._maint_tree.heading("col3", text=col3)

        if typed_rows is None:
            typed_rows = [("mixed", row, None) for row in rows]

        self._maint_typed_rows = list(typed_rows)
        self._maint_default_checked = default_checked
        self._apply_maint_filter()

        # Cache the render so switching tools and back is instant — and
        # persist it so a restart doesn't force another full walk.
        if self._maint_tool_name:
            self._maint_cache[self._maint_tool_name] = {
                "results": self._maint_results,
                "typed_rows": self._maint_typed_rows,
                "cols": (col1, col2, col3),
                "check_label": check_label,
                "default_checked": default_checked,
                "status": self._maint_status_var.get(),
                "at": datetime.datetime.now().strftime("%b %d %H:%M"),
            }
            self._maint_save_cache()

    def _maint_render_cached(self, tool: str) -> bool:
        """Render a tool's cached results (kept across app restarts) instead
        of re-running the walk. Tool buttons ALWAYS prefer the cache; the
        🔄 Re-run button is the only thing that forces a fresh pass. Returns
        True if the cache was rendered (caller should skip the run)."""
        cached = self._maint_cache.get(tool)
        if not cached:
            return False
        self._maint_tool_name = tool
        self._maint_results = cached["results"]
        col1, col2, col3 = cached["cols"]
        # Note: _populate_maint_tree re-stores the cache — harmless.
        self._populate_maint_tree(
            [], col1=col1, col2=col2, col3=col3,
            check_label=cached["check_label"], typed_rows=cached["typed_rows"],
            default_checked=cached.get("default_checked", tool == "clean_junk"),
        )
        self._maint_status_var.set(
            f"{cached['status']}  (cached {cached['at']} — 🔄 Re-run to refresh)"
        )
        return True

    def _maint_rerun_current(self) -> None:
        """Force-refresh the currently displayed maintenance tool (Task B
        item 2). Covers every tool that renders into the maintenance grid,
        not just the tools that happen to use the plain results cache:
        daily_check, movie_migration, and reindex never cached in the first
        place (always fresh already) but still couldn't be re-triggered from
        here before; custom_rename needs its last rule/media-type replayed
        since it has no zero-argument re-run of its own."""
        tool = self._maint_tool_name
        if tool == "custom_rename":
            self._maint_rerun_custom_rename()
            return
        runners: dict[str, Any] = {
            "library_inventory": self._run_library_inventory,
            "find_duplicates": self._run_find_duplicates,
            "sanitize": self._run_sanitize,
            "clean_junk": self._run_clean_junk,
            "missing_episodes": self._run_missing_episodes,
            "unindexed": self._run_unindexed,
            "daily_check": self._run_daily_check,
            "movie_migration": self._run_movie_migration,
            "reindex": self.rebuild_library_index_from_ui,
        }
        runner = runners.get(tool)
        if runner is None:
            self._show_info("Re-run", "Run one of the maintenance tools first "
                                      "(Daily Check / Inventory / Duplicates / "
                                      "Sanitize / Custom Rename / Combo Clean / "
                                      "Clean Junk / Missing Episodes / Organize "
                                      "Movies / Unindexed / Reindex).")
            return
        self._maint_cache.pop(tool, None)
        self._maint_tool_name = ""
        runner()

    def _maint_rerun_custom_rename(self) -> None:
        """Re-run path for the shared 'custom_rename' grid, which is fed by
        TWO different producers (the Custom Rename dialog and Combo Clean) —
        neither takes zero arguments, so the last-used parameters are
        replayed from `_maint_custom_rename_replay` (Task B item 2)."""
        replay = self._maint_custom_rename_replay
        if replay is None:
            self._show_info(
                "Re-run",
                "Custom Rename has no stored rule to replay yet — open "
                "'Custom Rename...' (or run 'Combo Clean rename…') again.")
            return
        self._maint_cache.pop("custom_rename", None)
        self._maint_tool_name = ""
        if replay["kind"] == "combo_clean":
            self._submit_combo_clean(replay["media_type"])
            return

        rule, pattern, allowed_tags = (replay["rule"], replay["pattern"],
                                       replay["allowed_tags"])
        self._maint_status_var.set("Re-running Custom Rename with the last rule…")

        def worker() -> None:
            try:
                pairs = self._build_custom_rename_pairs(rule, pattern, allowed_tags)
            except Exception as exc:
                logger.exception("Custom rename re-run failed.")
                self._post_to_ui(lambda: self._maint_status_var.set(
                    f"Custom Rename re-run failed: {exc}"))
                return
            self._post_to_ui(lambda: self._apply_custom_rename_preview(pairs, None))

        threading.Thread(target=worker, name="custom-rename-rerun",
                         daemon=True).start()

    # Version 3 = the JSON format (Task S item 1 killed the pickle cache;
    # versions 1-2 were pickle-era and load as a miss).
    _MAINT_CACHE_VERSION = 3
    _MAINT_CACHE_FILE = "maintenance_cache.json"
    _MAINT_CACHE_LEGACY = "maintenance_cache.pkl"
    # Dataclasses that may appear in cached results/typed_rows — the JSON
    # decoder reconstructs ONLY these; anything else rejects the cache.
    _MAINT_CACHE_TYPES = (
        DuplicateGroup, SanitizePair, MissingEpisode, SeasonSummary,
        MissingEpisodesReport, UnindexedFile, JunkFile, ShowInventory,
        MovieInventory, LibraryInventory,
    )

    def _maint_cache_path(self) -> Path:
        import app_paths
        return app_paths.PATHS.cache_dir / self._MAINT_CACHE_FILE

    def _maint_save_cache(self) -> None:
        """Persist tool results to disk so a 30k-file walk survives an app
        restart. Best-effort: a failed save just means a re-run later.
        JSON on purpose — a cache file must never be able to execute code
        in this (elevated) process."""
        json_cache.save_json_cache(
            self._maint_cache_path(), self._maint_cache,
            version=self._MAINT_CACHE_VERSION,
            legacy_paths=[self._maint_cache_path().parent
                          / self._MAINT_CACHE_LEGACY])

    def _maint_load_cache(self) -> None:
        cache = json_cache.load_json_cache(
            self._maint_cache_path(), version=self._MAINT_CACHE_VERSION,
            dataclass_types=self._MAINT_CACHE_TYPES)
        if isinstance(cache, dict) and cache:
            self._maint_cache = cache
            logger.info("Maintenance cache loaded: %s", ", ".join(self._maint_cache))

    def _apply_maint_filter(self) -> None:
        """
        Re-render the maintenance tree from `_maint_typed_rows`, hiding rows
        whose media-type tag is currently unchecked. Rebuilds `_maint_row_state`
        (iid → (checkbox var, payload)) so Apply Selected can map a checked
        row back to its action payload even after header-click sorting.
        """
        if self._maint_tree is None:
            return
        active_tags = {tag for tag, var in self._maint_filter_vars.items() if var.get()}

        for item in self._maint_tree.get_children():
            self._maint_tree.delete(item)
        # iid -> (checked BooleanVar, action payload); survives header sorting.
        self._maint_row_state: dict[str, tuple[tk.BooleanVar, Any]] = {}

        checked = bool(getattr(self, "_maint_default_checked", False))
        parents: dict[int, str] = {}
        hidden_at: int | None = None
        for entry in self._maint_typed_rows:
            media_type, (_check, c1, c2, c3), payload = entry[0], entry[1], entry[2]
            level = entry[3] if len(entry) > 3 else 0
            if hidden_at is not None:
                if level > hidden_at:
                    continue  # child of a filtered-out row
                hidden_at = None
            if media_type not in active_tags:
                hidden_at = level
                continue
            # Rows with no action payload (summary lines) are never checked.
            row_checked = checked and payload is not None
            var = tk.BooleanVar(value=row_checked)
            mark = "[X]" if row_checked else "[ ]"
            parent = parents.get(level - 1, "") if level > 0 else ""
            item = self._maint_tree.insert(
                parent, "end", values=(mark, c1, c2, c3), open=(level == 0))
            parents[level] = item
            self._maint_row_state[item] = (var, payload)

    def _on_maint_tree_click(self, event: Any) -> None:
        if self._maint_tree is None:
            return
        region = self._maint_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self._maint_tree.identify_column(event.x)
        if col != "#1":
            return
        row_id = self._maint_tree.identify_row(event.y)
        if not row_id:
            return
        state = getattr(self, "_maint_row_state", {}).get(row_id)
        if state is None:
            return
        var, _payload = state
        var.set(not var.get())
        current_vals = list(self._maint_tree.item(row_id, "values"))
        current_vals[0] = "[X]" if var.get() else "[ ]"
        self._maint_tree.item(row_id, values=current_vals)

    def _run_combo_clean(self) -> None:
        """Combo Clean rename for the ONE library type selected in the filter
        bar. Renders a preview through the custom_rename plumbing, so Apply
        Selected performs the renames with the usual confirmation."""
        active = [tag for tag, var in self._maint_filter_vars.items()
                  if var.get() and tag != "mixed"]
        if len(active) != 1:
            self._show_info(
                "Combo Clean",
                "Tick exactly ONE library type (Movies / TV / Anime / …) in "
                "the filter bar so the cleanup targets a single library.")
            return
        self._submit_combo_clean(active[0])

    def _submit_combo_clean(self, media_type: str):
        """Shared by the toolbar button and 🔄 Re-run's replay path (Task B
        item 2) so both go through one job-submission implementation."""
        def job(progress, cancel_check) -> Any:
            from maintenance import build_combo_renames
            progress(phase=f"Building combo-clean renames for {media_type}…")
            pairs = build_combo_renames(media_type)
            return maint_jobs.JobResult(
                summary={"renames_proposed": len(pairs)}, result=pairs)

        return self._maint_submit(
            "combo_clean", f"Combo Clean preview ({media_type})",
            job, meta={"media_type": media_type})

    def _handle_combo_clean_result(self, job, pairs: list[SanitizePair]) -> None:
        media_type = job.meta.get("media_type", "mixed")
        self._maint_tool_name = "custom_rename"
        self._maint_results = pairs
        # Task B item 2: remember how this grid was built so 🔄 Re-run can
        # replay it (the grid is shared with the Custom Rename dialog, which
        # stamps its own replay dict in _apply_custom_rename_preview).
        self._maint_custom_rename_replay = {
            "kind": "combo_clean", "media_type": media_type,
        }
        if not pairs:
            self._maint_status_var.set(
                f"Combo Clean: nothing to rename in {media_type} — "
                "names are already clean.")
            self._populate_maint_tree(
                [], col1="Current name", col2="New name", col3="")
            return
        self._maint_status_var.set(
            f"Combo Clean: {len(pairs)} file(s) in {media_type} would "
            "be renamed. Review, tick, then Apply Selected.")
        typed_rows = [
            (media_type,
             ("", Path(pr.original).name, Path(pr.sanitized).name, ""),
             pr, 0)
            for pr in pairs
        ]
        self._populate_maint_tree(
            [], col1="Current name", col2="New name", col3="",
            check_label="Rename?", typed_rows=typed_rows,
        )

    def _grab_missing_selected(self, gaps: list[tuple]) -> None:
        """Missing Episodes -> Apply Selected: search + download each checked
        gap through the tracked show it belongs to (same path auto-grab uses,
        so relevance guards and size anchors all apply)."""
        if not self._ask_yes_no(
            "Download missing episodes",
            f"Search for and download {len(gaps)} missing episode(s)?\n\n"
            "Each grab uses the tracked show's runtime-anchored size rules.",
        ):
            return
        self._maint_status_var.set(f"Grabbing {len(gaps)} missing episode(s)…")

        def worker() -> None:
            by_folder: dict[str, Any] = {}
            for s in shows_store.list_shows():
                for f in s.folders:
                    by_folder[f.casefold()] = s
            started = 0
            skipped: list[str] = []
            for _tag, title, season, episode, show_path in gaps:
                label = f"{title} S{season:02d}E{episode:02d}"
                show = by_folder.get(str(show_path).casefold())
                if show is None:
                    skipped.append(label + " (folder not tracked)")
                    continue
                ep_row = next(
                    (e for e in shows_store.list_episodes(show.show_id)
                     if e.season == season and e.episode == episode), None)
                if ep_row is None:
                    skipped.append(label + " (no tracker row — Sync the show)")
                    continue
                try:
                    started += len(
                        self.download_manager._grab_one_episode(show, ep_row))
                except Exception:
                    logger.exception("Missing-episode grab failed for %s", label)
                    skipped.append(label + " (error — see log)")

            def done() -> None:
                msg = f"Missing Episodes: started {started} download(s)."
                if skipped:
                    msg += f" Skipped {len(skipped)}."
                    self._show_warning(
                        "Missing Episodes",
                        "Skipped:\n" + "\n".join(skipped[:12])
                        + ("\n…" if len(skipped) > 12 else ""))
                self._maint_status_var.set(msg)
            self._post_to_ui(done)

        threading.Thread(target=worker, name="maint-grab-missing",
                         daemon=True).start()

    def _maint_set_all(self, state: bool) -> None:
        """Check or uncheck every visible row (summary rows stay unchecked)."""
        if self._maint_tree is None:
            return
        for item, (var, payload) in getattr(self, "_maint_row_state", {}).items():
            value = state and payload is not None
            var.set(value)
            vals = list(self._maint_tree.item(item, "values"))
            vals[0] = "[X]" if value else "[ ]"
            self._maint_tree.item(item, values=vals)

    # =====================================================================
    # Maintenance tab — job registry plumbing (Task I)
    # =====================================================================

    def _maint_submit(self, tool_key: str, label: str, fn,
                      *, meta: dict[str, Any] | None = None,
                      dedupe: bool = True):
        """Queue a tool run on the shared registry. One job runs at a time;
        anything else queues visibly (RESOLVED DECISION 8)."""
        current = self._maint_registry.current_job()
        job = self._maint_registry.submit(tool_key, label, fn, meta=meta,
                                          dedupe=dedupe)
        if current is not None and job.state == "queued":
            self._maint_status_var.set(
                f"Queued: {label} (waiting for {current.label})")
        return job

    def _on_maint_job_event(self, event: str, job) -> None:
        """Registry listener — fires on the worker thread; marshal to Tk."""
        self._post_to_ui(lambda: self._maint_job_event_ui(event, job))

    def _maint_job_event_ui(self, event: str, job) -> None:
        if self._quitting:
            return
        if event == "started":
            self._maint_status_var.set(f"Running {job.label}...")
            self._maint_phase_var.set(job.phase or "Working…")
            self._maint_set_bar(job)
            self._maint_start_elapsed_tick()
            if self._maint_cancel_btn is not None:
                self._maint_cancel_btn.configure(state=tk.NORMAL)
        elif event == "progress":
            self._maint_set_bar(job)
            phase = job.phase or "Working…"
            if job.progress_total:
                phase += f"  ({job.progress_current or 0}/{job.progress_total})"
            self._maint_phase_var.set(phase)
        elif event == "cancel_requested":
            self._maint_phase_var.set(
                f"{job.phase or 'Working…'}  (cancelling…)")
        elif event == "finished":
            self._maint_job_finished_ui(job)
        elif event == "idle":
            # Queue drained — rest the progress strip (the banner stays).
            self._maint_bar_idle()
            self._maint_phase_var.set("")
            if self._maint_cancel_btn is not None:
                self._maint_cancel_btn.configure(state=tk.DISABLED)
        self._maint_update_queue_label()

    def _maint_set_bar(self, job) -> None:
        """Honest progressbar: determinate with real counts, indeterminate
        motion otherwise — never a made-up percentage."""
        bar = self._maint_progressbar
        if bar is None:
            return
        if job.progress_total:
            if self._maint_bar_indeterminate:
                bar.stop()
                bar.configure(mode="determinate")
                self._maint_bar_indeterminate = False
            bar.configure(maximum=job.progress_total,
                          value=job.progress_current or 0)
        elif not self._maint_bar_indeterminate:
            bar.configure(mode="indeterminate")
            bar.start(80)
            self._maint_bar_indeterminate = True

    def _maint_bar_idle(self) -> None:
        bar = self._maint_progressbar
        if bar is None:
            return
        if self._maint_bar_indeterminate:
            bar.stop()
            bar.configure(mode="determinate")
            self._maint_bar_indeterminate = False
        bar.configure(value=0, maximum=100)

    def _maint_start_elapsed_tick(self) -> None:
        if not self._maint_tick_scheduled:
            self._maint_tick_scheduled = True
            self._maint_elapsed_tick()

    def _maint_elapsed_tick(self) -> None:
        job = self._maint_registry.current_job()
        if job is None or self._quitting:
            self._maint_elapsed_var.set("")
            self._maint_tick_scheduled = False
            return
        secs = int(job.elapsed_seconds() or 0)
        self._maint_elapsed_var.set(f"{secs // 60}:{secs % 60:02d} elapsed")
        self.root.after(1000, self._maint_elapsed_tick)

    def _maint_update_queue_label(self) -> None:
        current, queued = self._maint_registry.snapshot()
        if queued:
            names = ", ".join(j.label for j in queued[:3])
            if len(queued) > 3:
                names += f", … +{len(queued) - 3}"
            self._maint_queue_var.set(f"Queued ({len(queued)}): {names}")
        else:
            self._maint_queue_var.set("")
        # Header indicator — visible from every tab.
        if current is None and not queued:
            self._maint_job_indicator_var.set("")
        elif current is None:
            self._maint_job_indicator_var.set(
                f"🛠 Maintenance: {len(queued)} job(s) queued")
        else:
            text = f"🛠 Maintenance: {current.label} running"
            if current.progress_total:
                text += f" ({current.progress_current or 0}/{current.progress_total})"
            if queued:
                text += f" — {len(queued)} queued"
            self._maint_job_indicator_var.set(text)

    def _maint_sync_from_registry(self) -> None:
        """Repaint the progress strip from the registry — called on tab
        select so a run started before the tab switch is still shown live."""
        current, _queued = self._maint_registry.snapshot()
        if current is not None:
            self._maint_status_var.set(f"Running {current.label}...")
            phase = current.phase or "Working…"
            if current.progress_total:
                phase += (f"  ({current.progress_current or 0}"
                          f"/{current.progress_total})")
            self._maint_phase_var.set(phase)
            self._maint_set_bar(current)
            self._maint_start_elapsed_tick()
            if self._maint_cancel_btn is not None:
                self._maint_cancel_btn.configure(state=tk.NORMAL)
        else:
            self._maint_bar_idle()
            self._maint_phase_var.set("")
            if self._maint_cancel_btn is not None:
                self._maint_cancel_btn.configure(state=tk.DISABLED)
        self._maint_update_queue_label()

    def _maint_cancel_current(self) -> None:
        job = self._maint_registry.current_job()
        if job is None:
            queued = self._maint_registry.queued_jobs()
            if not queued:
                return
            job = queued[0]
        self._maint_registry.request_cancel(job.job_id)

    def _maint_job_finished_ui(self, job) -> None:
        """Completion path: persistent banner, status, result rendering,
        journal already written, toast when the window isn't focused.
        Failures are as loud as successes."""
        if self._maint_registry.current_job() is None:
            self._maint_bar_idle()
            self._maint_phase_var.set("")
            if self._maint_cancel_btn is not None:
                self._maint_cancel_btn.configure(state=tk.DISABLED)

        finished_local = _local_ts(job.finished_at or "")
        if job.state == "done":
            summary_bits = ", ".join(
                f"{k.replace('_', ' ')}: {v}"
                for k, v in (job.summary or {}).items() if k != "note")
            banner = f"✔ {job.label} finished {finished_local}"
            if summary_bits:
                banner += f" — {summary_bits}"
            color = _DOT_GREEN
            toast = f"{job.label} finished. {summary_bits}".strip()
        elif job.state == "cancelled":
            msg = (job.summary or {}).get("message", "cancelled")
            banner = f"⏹ {job.label} {msg} ({finished_local})"
            color = _DOT_AMBER
            toast = f"{job.label}: {msg}"
            self._maint_status_var.set(f"{job.label}: {msg}")
        else:  # failed
            err = (job.error or {}).get("message", "unknown error")
            banner = f"✖ {job.label} FAILED {finished_local} — {err}"
            color = _DOT_RED
            toast = f"{job.label} failed: {err}"
            self._maint_status_var.set(f"{job.label} failed: {err}")

        self._maint_banner_var.set(banner)
        if self._maint_banner_label is not None:
            self._maint_banner_label.configure(foreground=color)
        if not job.meta.get("idle"):
            self._notify_if_unfocused(f"Sensarr — {job.label}", toast)

        if job.state == "done":
            try:
                self._maint_dispatch_result(job)
                self._maint_stamp_rescanned(job)
            except Exception:
                logger.exception("Result handler failed for %s.", job.tool_key)
                self._maint_status_var.set(
                    f"{job.label}: result rendering failed (see log)")

        # Idle-pass bookkeeping: popups stay suppressed until the last
        # overnight job is done.
        if job.job_id in self._maint_idle_pending:
            self._maint_idle_pending.discard(job.job_id)
            if not self._maint_idle_pending:
                self._maint_popups_ok = True
                self._maint_status_var.set(
                    "Overnight pre-cache finished — every tool below opens "
                    "instantly from cache. 🔄 Re-run refreshes any of them.")
                logger.info("Idle pre-cache pass finished.")

    # Tool key -> what to call a row of its grid, for the rescan stamp
    # below. Only tools that actually render into the maintenance grid are
    # listed; anything else (the idle-only pre-cache jobs) is left out on
    # purpose so no stamp is appended for them.
    _MAINT_GRID_NOUN: dict[str, str] = {
        "daily_check": "rows", "library_inventory": "rows",
        "find_duplicates": "groups", "sanitize": "rows",
        "clean_junk": "rows", "missing_episodes": "rows",
        "unindexed": "rows", "movie_migration": "rows",
        "combo_clean": "rows", "reindex": "rows",
    }

    def _maint_stamp_rescanned(self, job) -> None:
        """Task B item 3: after ANY completed grid-rendering run, stamp the
        status line with 'rescanned HH:MM, N <groups/rows>' — so a scan that
        comes back with exactly the same-looking results is still visibly
        FRESH, not silently stale. Runs after the tool's own result handler
        has already set its own status text and populated the grid, so the
        row count below reflects what's actually on screen."""
        noun = self._MAINT_GRID_NOUN.get(job.tool_key)
        if noun is None:
            return
        self._maint_append_rescan_stamp(len(self._maint_typed_rows), noun)

    def _maint_append_rescan_stamp(self, count: int, noun: str = "rows") -> None:
        """Shared by the registry-job stamp above and the one non-job scan
        path (the Custom Rename dialog builds its preview on a bare thread,
        not a maint_jobs job)."""
        stamp = f"rescanned {datetime.datetime.now().strftime('%H:%M')}, {count} {noun}"
        current = self._maint_status_var.get()
        self._maint_status_var.set(f"{current}  ({stamp})" if current else stamp)

    def _maint_dispatch_result(self, job) -> None:
        """Route a completed job's in-memory result to its render handler."""
        handlers = {
            "daily_check": self._handle_daily_check_job,
            "library_inventory": self._handle_library_inventory_result,
            "find_duplicates": self._handle_duplicates_result,
            "sanitize": self._handle_sanitize_result,
            "sanitize_show": self._handle_sanitize_result,
            "clean_junk": self._handle_junk_result,
            "missing_episodes": self._handle_missing_episodes_result,
            "unindexed": self._handle_unindexed_result,
            "movie_migration": lambda res: self._handle_movie_migration_plan(*res),
            "combo_clean": lambda res: self._handle_combo_clean_result(job, res),
            "reindex": self._handle_library_reindex_result,
        }
        handler = handlers.get(job.tool_key)
        if handler is None:
            return
        if job.tool_key == "daily_check":
            handler(job)
        else:
            handler(job.result)

    def _notify_if_unfocused(self, title: str, message: str) -> None:
        """Tray toast when the window is hidden or another app has focus —
        matches the existing pystray tray-icon pattern (no new deps)."""
        try:
            focused = (self.root.winfo_viewable()
                       and self.root.focus_displayof() is not None)
        except Exception:
            focused = False
        if focused:
            return
        try:
            if self._tray_icon is not None:
                self._tray_icon.notify(message, title)
        except Exception:
            logger.debug("Tray notification failed.", exc_info=True)

    def _maint_open_run_history(self) -> None:
        """Run history — the journalled maintenance_jobs rows, newest first."""
        win = tk.Toplevel(self.root)
        win.title("Maintenance run history")
        win.geometry("860x420")
        win.transient(self.root)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            win, columns=("tool", "state", "started", "duration", "progress",
                          "summary"),
            show="headings")
        for col, text, width, stretch in (
                ("tool", "Tool", 170, False), ("state", "State", 90, False),
                ("started", "Started", 130, False),
                ("duration", "Duration", 80, False),
                ("progress", "Progress", 90, False),
                ("summary", "Summary / Error", 320, True)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        tree.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        scroll = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=10)
        tree.configure(yscrollcommand=scroll.set)
        make_sortable(tree)

        import json as _json
        rows = self._maint_registry.history(limit=200)
        for row in rows:
            started = _local_ts(row["started_at"]) if row["started_at"] else "—"
            duration = "—"
            if row["started_at"] and row["finished_at"]:
                try:
                    fmt = "%Y-%m-%d %H:%M:%S"
                    delta = (datetime.datetime.strptime(row["finished_at"], fmt)
                             - datetime.datetime.strptime(row["started_at"], fmt))
                    duration = f"{int(delta.total_seconds())}s"
                except ValueError:
                    pass
            progress = ""
            if row["progress_total"]:
                progress = f"{row['progress_current'] or 0}/{row['progress_total']}"
            detail = ""
            for blob in (row["error_json"], row["summary_json"]):
                if blob:
                    try:
                        parsed = _json.loads(blob)
                        detail = ", ".join(f"{k}: {v}" for k, v in parsed.items())
                    except (ValueError, AttributeError):
                        detail = str(blob)
                    break
            tree.insert("", "end", values=(row["label"], row["state"],
                                           started, duration, progress, detail))
        ttk.Label(win, text=f"{len(rows)} journalled run(s) — survives app "
                            "restarts (interrupted runs are marked, never "
                            "silently lost)",
                  font=("Segoe UI", 9, "italic")).grid(
            row=1, column=0, sticky="w", padx=10, pady=(0, 10))
        self._apply_dark_widget_styles(win)

    # =====================================================================
    # Maintenance tab — daily library check
    # =====================================================================

    def _run_daily_check(self, *, silent: bool = False) -> None:
        def job(progress, cancel_check) -> Any:
            progress(phase="Checking open requests against the library…")
            try:
                summary = daily_library_check()
            except Exception as exc:
                logger.exception("Daily library check failed.")
                summary = {"checked": 0, "newly_found": 0, "errors": [str(exc)]}
            if cancel_check():
                raise maint_jobs.JobCancelled(
                    items_done=summary.get("checked", 0))
            # Task E retention: the ONE prune entrypoint rides the daily pass.
            # Loser detail rows older than 90 days go; receipts + histograms
            # stay forever (RESOLVED DECISION 5).
            progress(phase="Pruning selection-run details (90-day retention)…")
            try:
                pruned = downloads_store.prune_selection_run_details()
                if pruned.get("rows_deleted"):
                    logger.info("Selection retention: pruned %s loser row(s) "
                                "across %s run(s).", pruned["rows_deleted"],
                                pruned["runs_pruned"])
            except Exception:
                logger.exception("Selection retention prune failed.")
            # Piggyback the app-update check on the nightly pass.
            progress(phase="Checking for app updates…")
            try:
                import updater
                info = updater.check_for_update()
            except Exception:
                logger.debug("Nightly update check failed.", exc_info=True)
                info = None
            # Task I: cheap manifest-only check (no gz download) for the
            # weekly-published anime DB artifact, right next to the app
            # update check above. ensure_fresh()'s own 7-day age gate would
            # otherwise delay picking up a fresh CI build by up to a week;
            # this lets a genuinely newer artifact get pulled the same day
            # it's published. force=True only fires refresh() when the cheap
            # check above already confirmed something newer is available —
            # refresh() keeps its own untouched-on-failure guarantee either way.
            try:
                if anime_db.check_for_manifest_update():
                    anime_db.ensure_fresh(background=True, force=True)
            except Exception:
                logger.debug("Anime DB manifest check failed.", exc_info=True)
            return maint_jobs.JobResult(
                summary={"checked": summary.get("checked", 0),
                         "newly_found": summary.get("newly_found", 0),
                         "errors": len(summary.get("errors", []))},
                result={"summary": summary, "update": info})

        self._maint_submit("daily_check", "Daily Library Check", job,
                           meta={"silent": silent})

    def _handle_daily_check_job(self, job) -> None:
        payload = job.result or {}
        self._handle_daily_check_result(payload.get("summary", {}),
                                        silent=bool(job.meta.get("silent")))
        info = payload.get("update")
        if info is not None:
            self._show_update_banner(info)

    # ------------------------------------------------------------------
    # In-app updates — nightly check, dismissable Status-tab banner
    # ------------------------------------------------------------------

    def _show_update_banner(self, info) -> None:
        """Show the update banner, honouring dismiss/skip/mute — unless the
        release is marked urgent (SENSARR-URGENT in its notes), which
        overrides every quieting choice and pops the message once."""
        import updater as _updater
        self._update_info = info
        if not info.urgent:
            if not config.UPDATE_NOTIFY:
                return
            if info.version == config.UPDATE_SKIPPED_VERSION:
                return
        text = f"Update available: v{info.version} (you have v{config.APP_VERSION})"
        if info.urgent:
            text = f"⚠ URGENT update v{info.version}: {info.urgent_message}"
            self._update_banner_label.configure(foreground="#ff5f5f")
            # Urgent ignores quieting: no skip/mute, dismiss only hides
            # until the next launch.
            self._update_skip_btn.pack_forget()
            self._update_mute_btn.pack_forget()
        self._update_banner_label.configure(text=text)
        can_self = _updater.can_self_update(info)
        self._update_install_btn.configure(
            text="Install update" if can_self else "Open release page",
            command=self._install_update if can_self else (
                lambda: webbrowser.open_new_tab(info.html_url)))
        if not can_self:
            # Task H item 6: honest per-platform instructions when in-place
            # self-update is unavailable (Linux source: git pull; packaged
            # Linux: download the new artifact; Windows source: git pull).
            hint = platform_adapter.updater_capability().hint
            if hint:
                add_tooltip(self._update_install_btn, hint)
        self._update_banner.grid()
        if info.urgent and not self._urgent_popup_shown:
            self._urgent_popup_shown = True
            self._show_warning(
                "Urgent update",
                f"Sensarr v{info.version} is an urgent release:\n\n"
                f"{info.urgent_message}\n\n"
                "Please update as soon as possible.")

    def _skip_update_version(self) -> None:
        info = self._update_info
        if info is None:
            return
        setattr(config, "UPDATE_SKIPPED_VERSION", info.version)
        try:
            save_settings({"UPDATE_SKIPPED_VERSION": info.version},
                          config.DOTENV_PATH)
        except Exception:
            logger.exception("Failed to persist UPDATE_SKIPPED_VERSION")
        self._update_banner.grid_remove()

    def _mute_update_notices(self) -> None:
        if not self._ask_yes_no(
            "Mute update notices",
            "Hide update notifications permanently? Urgent security "
            "releases will still be shown. You can re-enable notices "
            "any time with UPDATE_NOTIFY=true in .env.",
        ):
            return
        setattr(config, "UPDATE_NOTIFY", False)
        try:
            save_settings({"UPDATE_NOTIFY": "false"}, config.DOTENV_PATH)
        except Exception:
            logger.exception("Failed to persist UPDATE_NOTIFY")
        self._update_banner.grid_remove()

    def _install_update(self) -> None:
        info = self._update_info
        if info is None:
            return
        import updater as _updater
        if not _updater.can_self_update(info):
            webbrowser.open_new_tab(info.html_url)
            return
        if not self._ask_yes_no(
            "Install update",
            f"Download and install v{info.version} now?\n\n"
            "The app will close, swap in the new build (your settings, "
            "databases, and caches are preserved), and relaunch.",
        ):
            return
        self.status_var.set(f"Downloading update v{info.version}…")

        def worker() -> None:
            try:
                bat = _updater.stage_self_update(
                    info, on_status=lambda m: self._post_to_ui(
                        lambda: self.status_var.set(m)))
            except Exception as exc:
                logger.exception("Update staging failed.")
                self._post_to_ui(lambda: self._show_warning(
                    "Update failed",
                    f"Could not stage the update: {exc}\n\n"
                    "Nothing was changed — you can retry or install "
                    "manually from the release page."))
                return

            def go() -> None:
                _updater.launch_staged_update(bat)
                self.shutdown_from_terminal()
            self._post_to_ui(go)

        threading.Thread(target=worker, name="app-update", daemon=True).start()

    def _handle_daily_check_result(self, summary: dict, *, silent: bool = False) -> None:
        self._last_library_check_date = datetime.date.today().isoformat()
        checked = summary.get("checked", 0)
        newly_found = summary.get("newly_found", 0)
        fulfilled = summary.get("fulfilled_from_library", 0)
        reopened = summary.get("reopened", 0)
        errors = summary.get("errors", [])

        status = f"Daily check complete -- {checked} checked, {newly_found} newly found"
        if fulfilled:
            status += f", {fulfilled} closed (already in library)"
        if errors:
            status += f", {len(errors)} error(s)"
        self._maint_status_var.set(status)

        rows: list[tuple[str, str, str, str]] = []
        if fulfilled:
            rows.append(("", f"{fulfilled} request(s) closed — already in your library", "", ""))
        if newly_found:
            rows.append(("", f"{newly_found} request(s) now found in library", "", ""))
        if reopened:
            rows.append(("", f"{reopened} request(s) reopened (no longer on disk)", "", ""))
        rows.append(("", f"Scanned {checked} open requests", "", ""))
        for err in errors:
            rows.append(("", "Error", err, ""))

        self._maint_tool_name = "daily_check"
        self._maint_results = []
        self._populate_maint_tree(rows, col1="Result", col2="Detail", col3="")
        self.refresh_requests()

        if not silent:
            popup_lines = [
                f"Scanned {checked} open request(s).",
                f"Newly found in library: {newly_found}",
            ]
            if errors:
                popup_lines.append(f"Errors: {len(errors)}")
                popup_lines.extend(f"  - {e}" for e in errors[:5])
            self._show_info("Daily Library Check", "\n".join(popup_lines))

    def _schedule_daily_library_check(self) -> None:
        now = datetime.datetime.now()
        target_hour = getattr(config, "LIBRARY_CHECK_HOUR", 3)
        today_str = now.date().isoformat()
        if self._last_library_check_date != today_str and now.hour >= target_hour:
            self._run_daily_check(silent=True)
        if not self._quitting:
            self.root.after(60 * 60 * 1000, self._daily_check_tick)

    def _daily_check_tick(self) -> None:
        if self._quitting:
            return
        now = datetime.datetime.now()
        target_hour = getattr(config, "LIBRARY_CHECK_HOUR", 3)
        today_str = now.date().isoformat()
        if self._last_library_check_date != today_str and now.hour >= target_hour:
            self._run_daily_check(silent=True)
        self.root.after(60 * 60 * 1000, self._daily_check_tick)

    # =====================================================================
    # Overnight pre-cache — warm every expensive scan while nothing happens
    # =====================================================================

    def _schedule_idle_cache(self) -> None:
        if not self._quitting:
            self.root.after(15 * 60 * 1000, self._idle_cache_tick)

    def _idle_cache_tick(self) -> None:
        if self._quitting:
            return
        now = datetime.datetime.now()
        today_str = now.date().isoformat()
        downloads_active = any(
            row.status in ("queued", "downloading")
            for row in downloads_store.list_downloads(limit=50)
        )
        if (config.IDLE_CACHE_ENABLED
                and self._last_idle_cache_date != today_str
                and now.hour >= config.IDLE_CACHE_HOUR
                and not downloads_active):
            self._last_idle_cache_date = today_str
            self._run_idle_cache_pass()
        self.root.after(15 * 60 * 1000, self._idle_cache_tick)

    def _run_idle_cache_pass(self) -> None:
        """Refresh every expensive scan overnight, popups suppressed, so each
        tool opens instantly from cache the next day.

        Task I item 7: the pass runs THROUGH the job registry — one journalled
        job per scan instead of one invisible thread — so overnight runs show
        up in Run history exactly like button clicks, the queue is visible,
        and a mid-pass cancel behaves like any other cancel. The registry
        already serializes jobs, which keeps the pass deliberately unhurried.
        """
        logger.info("Idle pre-cache pass starting.")
        self._maint_popups_ok = False
        jobs: list[Any] = []

        def index_job(progress, cancel_check) -> Any:
            from library_index import refresh_library_index
            progress(phase="Refreshing the library index…")
            added = removed = 0
            try:
                refreshed = refresh_library_index()
                added, removed = refreshed.added, refreshed.removed
                logger.info("Idle cache: index delta +%d/−%d.", added, removed)
            except Exception:
                logger.exception("Idle cache: index refresh failed.")
            if cancel_check():
                raise maint_jobs.JobCancelled()
            progress(phase="Refreshing anime metadata…")
            try:
                anime_db.ensure_fresh(background=False)
            except Exception:
                logger.exception("Idle cache: anime metadata refresh failed.")
            return maint_jobs.JobResult(
                summary={"index_added": added, "index_removed": removed})

        jobs.append(self._maint_submit(
            "idle_index_refresh", "Pre-cache: index + anime metadata",
            index_job, meta={"idle": True}))

        # The same registry jobs the toolbar buttons use — fresh scans, no
        # cache short-circuit, results rendered + cached by the same handlers.
        for runner in (self._run_library_inventory, self._run_find_duplicates,
                       self._run_sanitize, self._run_clean_junk,
                       self._run_unindexed, self._run_missing_episodes):
            job = runner(from_idle=True)
            if job is not None:
                jobs.append(job)

        def lowqual_job(progress, cancel_check) -> Any:
            # Probes only new/changed files (ffprobe durations persist in
            # SQLite) and refreshes the JSON cache the Library tab shows on
            # first click.
            from library_index import list_all_files
            from video_quality import find_low_quality_movies
            progress(phase="Collecting movie files…")
            movies = [
                (e.path, e.name, e.size_bytes) for e in list_all_files()
                if media_type_for_path(e.root_path) == "movie"
            ]

            def scan_progress(done: int, total: int) -> None:
                if cancel_check():
                    raise maint_jobs.JobCancelled(items_done=done)
                progress(done, total, phase="Analysing movie quality…")

            results = find_low_quality_movies(movies, progress=scan_progress)
            self._lib_lowqual_save_cache(results, len(movies))
            self._lib_lowqual_showing_cache = False
            logger.info("Idle cache: movie-quality scan done (%d flagged).",
                        len(results))
            return maint_jobs.JobResult(
                summary={"movies_scanned": len(movies),
                         "flagged": len(results)})

        jobs.append(self._maint_submit(
            "idle_lowqual_scan", "Pre-cache: movie quality scan",
            lowqual_job, meta={"idle": True}))

        self._maint_idle_pending = {j.job_id for j in jobs if j is not None}

    # =====================================================================
    # Maintenance tab — find duplicates
    # =====================================================================

    def _run_find_duplicates(self, *, from_idle: bool = False):
        if not from_idle:
            if self._maint_render_cached("find_duplicates"):
                return None
            if not self._maint_require_library_paths("Find Duplicates"):
                return None
        elif not config.PLEX_LIBRARY_PATHS:
            return None

        def job(progress, cancel_check) -> Any:
            # find_duplicates() is one monolithic walk — no honest count is
            # available, so the bar stays indeterminate with a real phase.
            progress(phase="Walking the library for duplicate titles…")
            results = find_duplicates()
            return maint_jobs.JobResult(
                summary={"duplicate_groups": len(results)}, result=results)

        return self._maint_submit("find_duplicates", "Find Duplicates", job,
                                  meta={"idle": from_idle})

    def _handle_duplicates_result(self, results: list[DuplicateGroup]) -> None:
        self._maint_tool_name = "find_duplicates"
        self._maint_results = results
        if not results:
            self._maint_status_var.set("No duplicate groups found.")
            self._populate_maint_tree([], col1="Title", col2="File Path", col3="File Size")
            self._show_info("Find Duplicates", "No duplicate groups found across the configured library paths.")
            return
        total_size = sum(g.total_size_bytes for g in results)
        recoverable = sum(
            sum(g.candidate_sizes[1:])  # keep one copy, recover the rest
            for g in results
        )
        self._maint_status_var.set(
            f"{len(results)} duplicate group(s) -- {self._fmt_bytes(total_size)} on disk, "
            f"{self._fmt_bytes(recoverable)} recoverable. Tick the copies to DELETE "
            "(keep at least one per group), then Apply Selected."
        )

        # Whole-folder duplication (the same episodes in two directories) is
        # folded into ONE expandable block per folder pair — the flat list
        # buried the signal under hundreds of per-episode rows. Two-candidate
        # groups sharing the same pair of parent folders, 3+ matches → block.
        from collections import defaultdict
        pair_buckets: dict[tuple[str, str], list[Any]] = defaultdict(list)
        singles: list[Any] = []
        for group in results:
            if len(group.candidates) == 2:
                pa = str(Path(group.candidates[0]).parent)
                pb = str(Path(group.candidates[1]).parent)
                if pa != pb:
                    pair_buckets[tuple(sorted((pa, pb)))].append(group)
                    continue
            singles.append(group)

        typed_rows: list[tuple] = []
        folded = 0
        for (da, db), gs in sorted(
                pair_buckets.items(),
                key=lambda kv: -sum(g.total_size_bytes for g in kv[1])):
            if len(gs) < 3:
                singles.extend(gs)
                continue
            folded += 1
            tag = media_type_for_path(da)
            sides = []
            for side in (da, db):
                side_files = []
                side_size = 0
                for g in gs:
                    for c, s in zip(g.candidates, g.candidate_sizes):
                        if str(Path(c).parent) == side:
                            side_files.append(c)
                            side_size += s
                sides.append((side, side_files, side_size))
            total = sum(s for _d, _f, s in sides)
            typed_rows.append((
                tag,
                ("", f"[folder pair] {Path(da).name}  <->  {Path(db).name}",
                 f"{len(gs)} matching file(s) in two folders",
                 self._fmt_bytes(total)),
                # Task G item 2: the header payload isn't a delete action
                # (Apply Selected only understands str/("paths", …) payloads
                # for deletion — a ticked header checkbox here contributes
                # nothing to paths_to_delete, and Apply Selected warns
                # "Nothing selected" if that's ALL that's ticked, same as
                # when this was plain None) — it's what lets "Not a
                # duplicate" ignore the WHOLE folder pair at once.
                ("folder_pair", da, db), 0,
            ))
            for side, side_files, side_size in sides:
                typed_rows.append((
                    tag,
                    ("", "DELETE this whole side", side,
                     f"{len(side_files)} file(s), {self._fmt_bytes(side_size)}"),
                    ("paths", side_files), 1,
                ))
                for c in side_files:
                    try:
                        sz = Path(c).stat().st_size
                    except OSError:
                        sz = 0
                    typed_rows.append((
                        tag, ("", Path(c).name, c, self._fmt_bytes(sz)), c, 2,
                    ))

        for group in sorted(singles, key=lambda g: -g.total_size_bytes):
            tag = media_type_for_path(group.candidates[0])
            typed_rows.append((
                tag,
                ("", group.normalized_title,
                 f"{len(group.candidates)} copies",
                 self._fmt_bytes(group.total_size_bytes)),
                # Same non-delete-action payload convention as the folder-pair
                # header above — lets "Not a duplicate" ignore this one group.
                ("group", list(group.candidates)), 0,
            ))
            for path, size in zip(group.candidates, group.candidate_sizes):
                typed_rows.append((
                    tag, ("", Path(path).name, path, self._fmt_bytes(size)),
                    path, 1,
                ))

        if folded:
            self._maint_status_var.set(
                self._maint_status_var.get()
                + f"  ({folded} whole-folder pair(s) collapsed — expand to inspect)")

        self._populate_maint_tree(
            [], col1="Duplicate", col2="Path", col3="Size",
            check_label="Delete?", typed_rows=typed_rows,
        )
        self._show_info(
            "Find Duplicates",
            f"Found {len(results)} duplicate group(s) -- "
            f"{self._fmt_bytes(total_size)} on disk total.\n"
            f"You could free roughly {self._fmt_bytes(recoverable)} by keeping "
            "one copy from each group.\n\n"
            "Tick the rows you want to delete, then click 'Apply Selected'. "
            "Empty folders left behind will be reported separately so you "
            "can decide whether to remove them.",
        )

    # =====================================================================
    # Maintenance tab — sanitize filenames
    # =====================================================================

    def _run_sanitize(self, *, from_idle: bool = False):
        if not from_idle:
            if self._maint_render_cached("sanitize"):
                return None
            if not self._maint_require_library_paths("Sanitize Names"):
                return None
        elif not config.PLEX_LIBRARY_PATHS:
            return None

        def job(progress, cancel_check) -> Any:
            progress(phase="Previewing Plex-friendly renames…")
            results = sanitize_all(dry_run=True)
            return maint_jobs.JobResult(
                summary={"renames_proposed": len(results)}, result=results)

        return self._maint_submit("sanitize", "Sanitize Names (preview)", job,
                                  meta={"idle": from_idle})

    def _handle_sanitize_result(self, results: list[SanitizePair]) -> None:
        self._maint_tool_name = "sanitize"
        self._maint_results = results
        if not results:
            self._maint_status_var.set("All filenames are already Plex-friendly.")
            self._populate_maint_tree([], col1="Original Name", col2="Proposed Name", col3="Size")
            self._show_info("Sanitize Names", "All filenames are already Plex-friendly -- nothing to rename.")
            return
        self._maint_status_var.set(
            f"{len(results)} rename(s) proposed -- tick the rows to RENAME, then Apply Selected")
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = [
            (
                media_type_for_path(pair.original),
                ("", Path(pair.original).name, Path(pair.sanitized).name, self._fmt_bytes(pair.size_bytes)),
                idx,  # index back into self._maint_results
            )
            for idx, pair in enumerate(results)
        ]
        self._populate_maint_tree(
            [], col1="Original Name", col2="Proposed Name", col3="Size",
            check_label="Rename?", typed_rows=typed_rows,
        )
        self._show_info(
            "Sanitize Names",
            f"{len(results)} rename(s) proposed.\n\n"
            "Tick the checkbox column on rows you want renamed, then click "
            "'Apply Selected' to rename them on disk.",
        )

    # =====================================================================
    # Maintenance tab — flat-movie migration (Task D2 item 7)
    # =====================================================================

    def _run_movie_migration(self) -> None:
        """Dry-run scan: flat movies under the movie roots get a per-movie
        folder plan (verified TMDB identity -> {tmdb-ID} tag). Nothing moves
        until rows are ticked and Apply Selected confirms."""
        if not self._maint_require_library_paths("Organize Movies"):
            return
        roots = [p for p in config.media_paths_for_types("movie")
                 if Path(p).is_dir()]
        if not roots:
            self._show_warning("Organize Movies",
                               "No movie library folder is configured.")
            return

        # Deliberately UNCACHED: the dry-run plan is recomputed fresh on
        # every click so it always reflects the disk as it is right now.
        def job(progress, cancel_check) -> Any:
            progress(phase="Dry-run scan of the movie roots (fresh, uncached)…")
            plan, skipped = movie_migration.plan_migration(
                roots, resolver=movie_migration.tmdb_resolver)
            return maint_jobs.JobResult(
                summary={"planned_moves": len(plan), "skipped": len(skipped)},
                result=(plan, skipped))

        self._maint_submit("movie_migration", "Organize Movies (dry-run scan)",
                           job)

    def _handle_movie_migration_plan(self, plan, skipped) -> None:
        self._maint_tool_name = "movie_migration"
        self._maint_results = plan
        skipped_note = (f" {len(skipped)} skipped (identity unresolved — fix "
                        "manually, never guessed)." if skipped else "")
        if not plan:
            self._maint_status_var.set(
                "Every movie already has its own folder." + skipped_note)
            self._populate_maint_tree(
                [], col1="Current Path", col2="New Path", col3="Identity")
            self._show_info("Organize Movies",
                            "No flat movie files to organize." + skipped_note)
            return
        self._maint_status_var.set(
            f"{len(plan)} movie(s) would get a per-movie folder.{skipped_note} "
            "Tick what to MOVE, then Apply Selected. Every move is journalled "
            "and reversible.")
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = [
            (
                "movie",
                ("", item.old_path, item.new_path,
                 (f"tmdb-{item.tmdb_id}" if item.has_tmdb
                  else "no verified TMDB id")),
                idx,
            )
            for idx, item in enumerate(plan)
        ]
        self._populate_maint_tree(
            [], col1="Current Path", col2="New Path", col3="Identity",
            check_label="Move?", typed_rows=typed_rows,
        )

    def _handle_movie_migration_result(self, summary: dict, run_id: str) -> None:
        moved, failed = summary.get("moved", 0), summary.get("failed", 0)
        self._maint_status_var.set(
            f"Organize Movies: {moved} moved, {failed} failed (run {run_id[:8]})")
        detail = f"{moved} movie(s) moved into per-movie folders."
        if failed:
            detail += (f"\n{failed} move(s) FAILED (collision/permission) — "
                       "the files stayed where they were; details in the log.")
        detail += ("\n\nThe run is journalled: an interrupted run resumes by "
                   "running the tool again; the journal keeps the old paths if "
                   "a revert is ever needed.")
        (self._show_warning if failed else self._show_info)(
            "Organize Movies", detail)
        self._run_movie_migration()  # re-scan; done rows drop off the plan

    # =====================================================================
    # Maintenance tab — junk cleanup (samples, release notes, empty folders)
    # =====================================================================

    def _run_clean_junk(self, *, from_idle: bool = False):
        if not from_idle:
            if self._maint_render_cached("clean_junk"):
                return None
            if not self._maint_require_library_paths("Clean Junk"):
                return None
        elif not config.PLEX_LIBRARY_PATHS:
            return None

        def job(progress, cancel_check) -> Any:
            progress(phase="Scanning for samples, release junk, empty folders…")
            results = find_junk_files()
            return maint_jobs.JobResult(
                summary={"junk_items": len(results)}, result=results)

        return self._maint_submit("clean_junk", "Clean Junk", job,
                                  meta={"idle": from_idle})

    def _handle_junk_result(self, results: list[JunkFile]) -> None:
        self._maint_tool_name = "clean_junk"
        self._maint_results = results
        if not results:
            self._maint_status_var.set("No junk found — your library is squeaky clean.")
            self._populate_maint_tree([], col1="File / Folder", col2="Why", col3="Size",
                                      check_label="Delete?")
            self._show_info("Clean Junk", "No samples, release junk, or empty folders found.")
            return
        total = sum(j.size_bytes for j in results)
        self._maint_status_var.set(
            f"{len(results)} junk item(s) -- {self._fmt_bytes(total)} reclaimable. "
            "Tick what to DELETE, then Apply Selected. Files go to the recycle bin."
        )
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = [
            (
                media_type_for_path(j.path),
                ("", j.path, j.reason,
                 self._fmt_bytes(j.size_bytes) if j.kind == "file" else "folder"),
                (j.kind, j.path),
            )
            for j in results
        ]
        self._populate_maint_tree(
            [], col1="File / Folder", col2="Why it's junk", col3="Size",
            check_label="Delete?", typed_rows=typed_rows,
            default_checked=True,
        )

    # =====================================================================
    # Maintenance tab — missing episodes
    # =====================================================================

    def _run_missing_episodes(self, *, from_idle: bool = False):
        if not from_idle:
            if self._maint_render_cached("missing_episodes"):
                return None
            if not self._maint_require_library_paths("Missing Episodes"):
                return None
        elif not config.PLEX_LIBRARY_PATHS:
            return None

        def job(progress, cancel_check) -> Any:
            progress(phase="Scanning seasons for numbering gaps…")
            report = find_missing_episodes()
            return maint_jobs.JobResult(
                summary={"shows_scanned": report.shows_scanned,
                         "gaps": len(report.gaps)},
                result=report)

        return self._maint_submit("missing_episodes", "Missing Episodes", job,
                                  meta={"idle": from_idle})

    def _handle_missing_episodes_result(self, report: Any) -> None:
        # `report` is a MissingEpisodesReport — typed Any so the import doesn't
        # have to be eagerly named at this call site.
        self._maint_tool_name = "missing_episodes"
        self._maint_results = list(report.gaps)

        if report.shows_scanned == 0:
            self._maint_status_var.set("No show folders found.")
            self._populate_maint_tree([], col1="Show / Season", col2="Detail", col3="Path")
            self._show_info(
                "Missing Episodes",
                "No show folders detected. Make sure your TV / Anime / xAnime "
                "library paths actually contain show subfolders, not bare files.",
            )
            return

        shows_with_gaps = len({g.show_title for g in report.gaps})
        self._maint_status_var.set(
            f"Scanned {report.shows_scanned} show(s), {len(report.seasons)} season(s) total. "
            f"{len(report.gaps)} gap(s) across {shows_with_gaps} show(s)."
        )

        # Build per-season summary rows + per-gap detail rows.
        # Group seasons by show so the user can spot incomplete seasons even
        # when there are no NUMERIC gaps (e.g. "S03 only has E01-04").
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = []
        by_show: dict[str, list[Any]] = {}
        for s in report.seasons:
            by_show.setdefault(s.show_title, []).append(s)

        for show_title in sorted(by_show.keys()):
            seasons = sorted(by_show[show_title], key=lambda s: s.season)
            show_path = seasons[0].show_path
            tag = media_type_for_path(show_path)
            for s in seasons:
                summary = f"{s.episodes_present}/{s.highest_episode} ep present"
                if s.missing_count:
                    summary += f"  -- missing {s.missing_count}"
                typed_rows.append((
                    tag,
                    ("", f"{show_title}  S{s.season:02d}", summary, show_path),
                    None,
                ))
            # Per-gap rows underneath — checkable: Apply Selected downloads them.
            for ep in report.gaps:
                if ep.show_title != show_title:
                    continue
                typed_rows.append((
                    tag,
                    ("", f"  -> missing", f"S{ep.season:02d}E{ep.episode:02d}", ep.show_path),
                    ("grab_ep", ep.show_title, ep.season, ep.episode, ep.show_path),
                ))

        self._populate_maint_tree(
            [], col1="Show / Season", col2="Detail", col3="Path",
            check_label="Grab?", typed_rows=typed_rows,
        )
        self._show_info(
            "Missing Episodes",
            f"Scanned {report.shows_scanned} show folder(s) across "
            f"{len(report.seasons)} season(s).\n\n"
            f"{len(report.gaps)} gap(s) found inside seasons that have media "
            f"(across {shows_with_gaps} show(s)).\n\n"
            "Note: this only detects missing episodes WITHIN a season you "
            "already have files for. It can't tell you that a brand-new "
            "season exists upstream that you don't have any of -- check the "
            "'highest ep present' column and compare to what should exist.",
        )

    # =====================================================================
    # Maintenance tab — unindexed files
    # =====================================================================

    def _run_unindexed(self, *, from_idle: bool = False):
        if not from_idle:
            if self._maint_render_cached("unindexed"):
                return None
            if not self._maint_require_library_paths("Unindexed Files"):
                return None
        elif not config.PLEX_LIBRARY_PATHS:
            return None

        def job(progress, cancel_check) -> Any:
            progress(phase="Comparing disk contents against the index…")
            results = find_unindexed_files()
            return maint_jobs.JobResult(
                summary={"unindexed_files": len(results)}, result=results)

        return self._maint_submit("unindexed", "Unindexed Files", job,
                                  meta={"idle": from_idle})

    def _handle_unindexed_result(self, results: list[UnindexedFile]) -> None:
        self._maint_tool_name = "unindexed"
        self._maint_results = results
        if not results:
            self._maint_status_var.set("No unindexed files found -- library index is up to date.")
            self._populate_maint_tree([], col1="Filename", col2="Full Path", col3="Size")
            self._show_info(
                "Unindexed Files",
                "No unindexed files found -- everything on disk is in the library index.\n\n"
                "Tip: if the index is empty, click 'Reindex' on the Library tab first.",
            )
            return
        total = sum(f.size_bytes for f in results)
        self._maint_status_var.set(f"{len(results)} unindexed file(s) -- {self._fmt_bytes(total)} total")
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = [
            (
                media_type_for_path(f.path),
                ("", f.name, f.path, self._fmt_bytes(f.size_bytes)),
                f.path,
            )
            for f in results
        ]
        self._populate_maint_tree(
            [], col1="Filename", col2="Full Path", col3="Size",
            typed_rows=typed_rows,
        )
        self._show_info(
            "Unindexed Files",
            f"Found {len(results)} unindexed file(s) -- {self._fmt_bytes(total)} total.\n\n"
            "These files exist on disk but aren't in the library index. "
            "Click 'Reindex' on the Library tab to add them.",
        )

    # =====================================================================
    # Maintenance tab — apply selected (sanitize renames)
    # =====================================================================

    def _apply_maint_selection(self) -> None:
        # note: deferred — the APPLY phase (deletes/renames/moves after the
        # user ticks rows and confirms) still runs on short bare threads with
        # its own confirm dialogs and popups. Task I converted the scan tools
        # (the long, cancellable walks); routing these apply actions through
        # the registry is a follow-up so their runs journal too.
        if self._maint_tool_name not in ("sanitize", "find_duplicates",
                                         "custom_rename", "clean_junk",
                                         "missing_episodes", "movie_migration"):
            self._show_info(
                "Apply Selection",
                "The current tool results have no applyable actions.\n\n"
                "Tools that support Apply Selected:\n"
                "  - Find Duplicates    -> DELETES the checked files\n"
                "  - Clean Junk         -> DELETES the checked junk/empty folders\n"
                "  - Sanitize Names     -> RENAMES the checked files\n"
                "  - Custom Rename...   -> RENAMES per your rules\n"
                "  - Missing Episodes   -> DOWNLOADS the checked episodes\n"
                "  - Organize Movies    -> MOVES the checked movies into per-movie folders",
            )
            return

        selected_payloads = [
            payload
            for var, payload in getattr(self, "_maint_row_state", {}).values()
            if var.get() and payload is not None
        ]
        if not selected_payloads:
            self._show_warning("Nothing selected", "Check at least one row before clicking Apply Selected.")
            return

        if self._maint_tool_name == "sanitize":
            # Payloads are indices into self._maint_results (SanitizePair list).
            pairs = [
                self._maint_results[i] for i in selected_payloads
                if isinstance(i, int) and 0 <= i < len(self._maint_results)
            ]
            if not pairs:
                return
            names_preview = "\n".join(
                f"  {Path(p.original).name}  ->  {Path(p.sanitized).name}" for p in pairs[:10]
            )
            if len(pairs) > 10:
                names_preview += f"\n  ... and {len(pairs) - 10} more"
            if not self._ask_yes_no(
                "Confirm Rename",
                f"About to rename {len(pairs)} file(s):\n\n{names_preview}\n\nThis cannot be undone. Continue?",
            ):
                return
            self._maint_status_var.set(f"Renaming {len(pairs)} file(s)...")

            def worker() -> None:
                errors = apply_sanitization(pairs)
                self._post_to_ui(lambda: self._handle_apply_sanitize_result(len(pairs), errors))

            threading.Thread(target=worker, name="maint-apply-sanitize", daemon=True).start()
            return

        if self._maint_tool_name == "missing_episodes":
            gaps = [p for p in selected_payloads
                    if isinstance(p, tuple) and p and p[0] == "grab_ep"]
            if not gaps:
                return
            self._grab_missing_selected(gaps)
            return

        if self._maint_tool_name == "movie_migration":
            items = [
                self._maint_results[i] for i in selected_payloads
                if isinstance(i, int) and 0 <= i < len(self._maint_results)
            ]
            if not items:
                return
            preview = "\n".join(
                f"  {Path(it.old_path).name}\n    -> {it.new_path}"
                for it in items[:8])
            if len(items) > 8:
                preview += f"\n  ... and {len(items) - 8} more"
            if not self._ask_yes_no(
                "Confirm Organize Movies",
                f"About to move {len(items)} movie(s) into per-movie "
                f"folders:\n\n{preview}\n\nEvery move is journalled and "
                "reversible. Continue?",
            ):
                return
            self._maint_status_var.set(f"Moving {len(items)} movie(s)...")

            def worker() -> None:
                run_id = movie_migration.begin_run(items)

                def progress(done: int, total: int, _op) -> None:
                    self._post_to_ui(lambda: self._maint_status_var.set(
                        f"Organize Movies: {done}/{total}"))

                summary = movie_migration.execute_run(
                    run_id, on_progress=progress)
                self._post_to_ui(lambda: self._handle_movie_migration_result(
                    summary, run_id))

            threading.Thread(target=worker, name="maint-apply-movie-migration",
                             daemon=True).start()
            return

        if self._maint_tool_name == "find_duplicates":
            # Payloads: absolute file paths, or ("paths", [...]) for a whole
            # folder side. Checking a side and its children double-counts —
            # dedupe keeps the delete list honest. Group/folder-pair HEADER
            # rows carry a non-None ("group", …) / ("folder_pair", …) payload
            # too (Task G item 2 — that's what lets "Not a duplicate" resolve
            # a selected header), so they pass the earlier `payload is not
            # None` filter but are never a delete action; if ticking only
            # those is all the user did, paths_to_delete comes back empty and
            # that must surface as the same "nothing to do" warning it always
            # did (back when a header's payload was plain None and got
            # filtered out before reaching here), not a silent no-op.
            paths_to_delete: list[str] = []
            for p in selected_payloads:
                if isinstance(p, str):
                    paths_to_delete.append(p)
                elif isinstance(p, tuple) and p and p[0] == "paths":
                    paths_to_delete.extend(p[1])
            paths_to_delete = list(dict.fromkeys(paths_to_delete))
            if not paths_to_delete:
                self._show_warning(
                    "Nothing selected",
                    "Only group/folder-pair header rows are ticked — those "
                    "aren't deletable on their own. Tick a specific file row "
                    "(or a 'DELETE this whole side' row), then try again.")
                return
            self._confirm_and_delete_duplicates(paths_to_delete)
            return

        if self._maint_tool_name == "clean_junk":
            # Payloads are (kind, path) — files recycle, empty dirs rmdir.
            files = [p for kind, p in selected_payloads if kind == "file"]
            dirs = [p for kind, p in selected_payloads if kind == "dir"]
            if not files and not dirs:
                return
            if not self._ask_yes_no(
                "Clean Junk",
                f"Delete {len(files)} junk file(s) (to recycle bin) and remove "
                f"{len(dirs)} empty folder(s)?",
            ):
                return
            self._maint_status_var.set("Cleaning junk…")

            def worker() -> None:
                deleted, errors, empty_dirs = delete_files_with_cleanup(files)
                removed_dirs, dir_errors = self._remove_empty_dirs(dirs + empty_dirs)
                errors.extend(dir_errors)

                def done() -> None:
                    self._maint_status_var.set(
                        f"Removed {len(deleted)} file(s) + {removed_dirs} folder(s)"
                        + (f", {len(errors)} error(s)" if errors else "")
                    )
                    if errors:
                        self._show_warning("Clean Junk",
                                           "Some items failed:\n" + "\n".join(errors[:10]))
                    self._maint_tool_name = ""  # force a fresh re-run next click
                    self._maint_cache.pop("clean_junk", None)
                    self._run_clean_junk()
                self._post_to_ui(done)

            threading.Thread(target=worker, name="maint-apply-junk", daemon=True).start()
            return

        if self._maint_tool_name == "custom_rename":
            # Payloads are SanitizePair instances built by the dialog.
            pairs = [p for p in selected_payloads if isinstance(p, SanitizePair)]
            if not pairs:
                return
            names_preview = "\n".join(
                f"  {Path(p.original).name}  ->  {Path(p.sanitized).name}" for p in pairs[:10]
            )
            if len(pairs) > 10:
                names_preview += f"\n  ... and {len(pairs) - 10} more"
            if not self._ask_yes_no(
                "Confirm Rename",
                f"About to rename {len(pairs)} file(s) using your custom rule:\n\n"
                f"{names_preview}\n\nThis cannot be undone. Continue?",
            ):
                return
            self._maint_status_var.set(f"Renaming {len(pairs)} file(s)...")

            def worker() -> None:
                errors = apply_sanitization(pairs)
                self._post_to_ui(lambda: self._handle_apply_sanitize_result(len(pairs), errors))

            threading.Thread(target=worker, name="maint-apply-custom-rename", daemon=True).start()
            return

    def _confirm_and_delete_duplicates(self, paths: list[str]) -> None:
        """
        Two-step confirmation for destructive duplicate deletion.

        1) Show the list (capped) plus total reclaimed size; require yes.
        2) After deletion, surface any parent folders the deletes left empty
           and let the user decide whether to remove those too. Configured
           library roots are never offered for removal.
        """
        # Sum sizes from current cached DuplicateGroup data
        sizes: dict[str, int] = {}
        if isinstance(self._maint_results, list):
            for grp in self._maint_results:
                if isinstance(grp, DuplicateGroup):
                    for path, size in zip(grp.candidates, grp.candidate_sizes):
                        sizes[path] = size

        total_bytes = sum(sizes.get(p, 0) for p in paths)
        preview_lines = [f"  {p}  ({self._fmt_bytes(sizes.get(p, 0))})" for p in paths[:10]]
        if len(paths) > 10:
            preview_lines.append(f"  ... and {len(paths) - 10} more")

        if not self._ask_yes_no(
            "Confirm Deletion",
            f"About to PERMANENTLY DELETE {len(paths)} file(s) "
            f"({self._fmt_bytes(total_bytes)}):\n\n"
            + "\n".join(preview_lines)
            + "\n\nThis cannot be undone. Continue?",
        ):
            return

        self._maint_status_var.set(f"Deleting {len(paths)} file(s)...")

        def worker() -> None:
            deleted, errors, empty_dirs = delete_files_with_cleanup(paths)
            self._post_to_ui(
                lambda: self._handle_duplicate_delete_result(deleted, errors, empty_dirs)
            )

        threading.Thread(target=worker, name="maint-delete-dupes", daemon=True).start()

    def _handle_duplicate_delete_result(
        self,
        deleted: list[str],
        errors: list[str],
        empty_dirs: list[str],
    ) -> None:
        success = len(deleted)
        if errors:
            self._maint_status_var.set(
                f"Deleted {success} file(s); {len(errors)} error(s)"
            )
            err_preview = "\n".join(errors[:10])
            if len(errors) > 10:
                err_preview += f"\n... and {len(errors) - 10} more"
            self._show_warning(
                "Delete Results",
                f"Deleted {success} file(s).\n\nErrors:\n{err_preview}",
            )
        else:
            self._maint_status_var.set(f"Deleted {success} file(s).")

        if empty_dirs:
            preview = "\n".join(f"  {d}" for d in empty_dirs[:10])
            if len(empty_dirs) > 10:
                preview += f"\n  ... and {len(empty_dirs) - 10} more"
            if self._ask_yes_no(
                "Empty Folders",
                f"{len(empty_dirs)} folder(s) became empty after deletion:\n\n"
                f"{preview}\n\n"
                "Configured library roots are excluded automatically.\n\n"
                "Remove these empty folders?",
            ):
                removed_dirs, dir_errors = self._remove_empty_dirs(empty_dirs)
                if dir_errors:
                    self._show_warning(
                        "Folder Removal",
                        f"Removed {removed_dirs} folder(s).\n\nErrors:\n"
                        + "\n".join(dir_errors[:10]),
                    )
                else:
                    self._show_info(
                        "Folder Removal", f"Removed {removed_dirs} empty folder(s).",
                    )

        # Re-run find_duplicates so the tree reflects what's left. Pop the
        # cache + clear the tool name FIRST (Task B item 4, same pattern as
        # Clean Junk) — otherwise _run_find_duplicates() sees a still-valid
        # cache entry and re-renders the just-deleted files instead of
        # re-scanning the disk.
        self._maint_cache.pop("find_duplicates", None)
        self._maint_tool_name = ""
        self._run_find_duplicates()

    def _maint_mark_not_duplicate(self) -> None:
        """Task G item 2: persist a 'not a duplicate' verdict for whatever is
        selected in the Find Duplicates results tree, then re-scan so it
        drops out of the grid immediately.

        Uses the Treeview's own row SELECTION (click/ctrl-click/shift-click),
        independent of the Delete? checkbox column — ticking Delete? and
        marking 'not a duplicate' are different verbs on the same rows.
        Recognises three selectable shapes, all built by
        _handle_duplicates_result: a folder-pair header (ignores the whole
        pair of folders), a single-group header (ignores that one group by
        its full candidate set), or a leaf file row (resolved back to its
        owning group)."""
        if self._maint_tool_name != "find_duplicates" or self._maint_tree is None:
            self._show_info("Not a duplicate",
                            "Run Find Duplicates first, select a group or "
                            "folder-pair row in the results, then try again.")
            return
        selected = self._maint_tree.selection()
        if not selected:
            self._show_warning(
                "Not a duplicate",
                "Select a duplicate-group row (or a folded folder-pair "
                "header) in the results above first.")
            return

        import dupe_ignore
        row_state = getattr(self, "_maint_row_state", {})
        folder_pairs = 0
        groups = 0
        seen_group_keys: set[tuple] = set()
        for iid in selected:
            state = row_state.get(iid)
            if state is None:
                continue
            _var, payload = state
            if isinstance(payload, tuple) and payload and payload[0] == "folder_pair":
                _, da, db = payload
                key = dupe_ignore.folder_pair_key(da, db)
                if key not in seen_group_keys:
                    seen_group_keys.add(key)
                    dupe_ignore.ignore_folder_pair(da, db)
                    folder_pairs += 1
            elif isinstance(payload, tuple) and payload and payload[0] == "group":
                key = dupe_ignore.file_pair_key(payload[1])
                if key not in seen_group_keys:
                    seen_group_keys.add(key)
                    dupe_ignore.ignore_file_pair(payload[1])
                    groups += 1
            elif isinstance(payload, str):
                group = next(
                    (g for g in self._maint_results
                     if isinstance(g, DuplicateGroup) and payload in g.candidates),
                    None)
                if group is not None:
                    key = dupe_ignore.file_pair_key(group.candidates)
                    if key not in seen_group_keys:
                        seen_group_keys.add(key)
                        dupe_ignore.ignore_file_pair(group.candidates)
                        groups += 1

        if not folder_pairs and not groups:
            self._show_warning(
                "Not a duplicate",
                "Select a group header, folder-pair header, or a specific "
                "file row (not a 'DELETE this whole side' row), then try again.")
            return

        parts = []
        if groups:
            parts.append(f"{groups} group(s)")
        if folder_pairs:
            parts.append(f"{folder_pairs} folder pair(s)")
        self._maint_status_var.set(
            f"Marked not-a-duplicate: {', '.join(parts)}. Re-scanning…")
        self._maint_cache.pop("find_duplicates", None)
        self._maint_tool_name = ""
        self._run_find_duplicates()

    def _maint_open_ignored_pairs(self) -> None:
        """Task G item 2: 'Ignored pairs' viewer with un-ignore, so a verdict
        marked in a moment of certainty is never permanently invisible."""
        import dupe_ignore

        win = tk.Toplevel(self.root)
        win.title("Ignored duplicate pairs")
        win.geometry("820x420")
        win.transient(self.root)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)

        ttk.Label(win, text=(
            "Pairs marked 'Not a duplicate' in Find Duplicates. A folder "
            "pair covers every episode between those two folders, including "
            "ones found on a future rescan; a file pair covers just that one "
            "group's exact files."), wraplength=780, font=("Segoe UI", 9, "italic")
            ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 4))

        tree = ttk.Treeview(win, columns=("kind", "a", "b", "at"), show="headings")
        for col, text, width in (("kind", "Kind", 90), ("a", "Path A", 300),
                                 ("b", "Path B / more", 300), ("at", "Ignored (UTC)", 140)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor=tk.W)
        tree.grid(row=1, column=0, sticky="nsew", padx=10)
        scroll = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        make_sortable(tree)

        entries: dict[str, tuple[str, list[str]]] = {}

        def reload() -> None:
            for iid in tree.get_children():
                tree.delete(iid)
            entries.clear()
            for e in dupe_ignore.list_ignored_folder_pairs():
                paths = e.get("paths") or []
                a = paths[0] if len(paths) > 0 else ""
                b = paths[1] if len(paths) > 1 else ""
                iid = tree.insert("", "end",
                                  values=("Folder pair", a, b, e.get("ignored_at", "")))
                entries[iid] = ("folder", paths)
            for e in dupe_ignore.list_ignored_file_pairs():
                paths = e.get("paths") or []
                a = paths[0] if len(paths) > 0 else ""
                b = "; ".join(paths[1:])
                iid = tree.insert("", "end",
                                  values=("File pair", a, b, e.get("ignored_at", "")))
                entries[iid] = ("file", paths)

        reload()

        def unignore_selected() -> None:
            changed = False
            for iid in tree.selection():
                kind, paths = entries.get(iid, ("", []))
                if kind == "folder" and len(paths) == 2:
                    changed = dupe_ignore.unignore_folder_pair(paths[0], paths[1]) or changed
                elif kind == "file" and len(paths) >= 2:
                    changed = dupe_ignore.unignore_file_pair(paths) or changed
            if changed:
                reload()
                # The dupes grid may now be stale (fewer ignores in effect) —
                # same pop-cache-before-rescan pattern as everywhere else.
                self._maint_cache.pop("find_duplicates", None)
                if self._maint_tool_name == "find_duplicates":
                    self._maint_tool_name = ""

        btn_bar = ttk.Frame(win)
        btn_bar.grid(row=2, column=0, columnspan=2, sticky="e", padx=10, pady=10)
        ttk.Button(btn_bar, text="Un-ignore selected",
                  command=unignore_selected).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Close",
                  command=win.destroy).pack(side=tk.RIGHT, padx=(0, 6))
        self._apply_dark_widget_styles(win)

    # =====================================================================
    # Custom Rename dialog (Bulk Rename Utility-style)
    # =====================================================================

    def _open_custom_rename(self) -> None:
        """
        Open a small Toplevel that lets the user define a find/replace rule
        (with optional regex + case transform) and preview the resulting
        renames before committing them via Apply Selected.

        The preview populates the maintenance tab's tree with SanitizePair
        rows tagged ``custom_rename`` so the existing Apply Selected handler
        can rename only the rows the user ticks.
        """
        if not self._maint_require_library_paths("Custom Rename"):
            return

        win = tk.Toplevel(self.root)
        win.title("Custom Rename")
        win.transient(self.root)
        win.geometry("640x520")
        win.columnconfigure(1, weight=1)

        ttk.Label(win, text="Find:").grid(row=0, column=0, sticky="w", padx=10, pady=(12, 2))
        find_var = tk.StringVar()
        ttk.Entry(win, textvariable=find_var).grid(row=0, column=1, sticky="ew", padx=10, pady=(12, 2))

        ttk.Label(win, text="Replace with:").grid(row=1, column=0, sticky="w", padx=10, pady=2)
        replace_var = tk.StringVar()
        ttk.Entry(win, textvariable=replace_var).grid(row=1, column=1, sticky="ew", padx=10, pady=2)

        regex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(win, text="Treat 'Find' as a regular expression",
                        variable=regex_var).grid(row=2, column=1, sticky="w", padx=10, pady=(4, 8))

        ttk.Label(win, text="Case:").grid(row=3, column=0, sticky="w", padx=10, pady=2)
        case_var = tk.StringVar(value="none")
        ttk.Combobox(
            win, textvariable=case_var, state="readonly",
            values=["none", "lower", "UPPER", "Title Case"],
        ).grid(row=3, column=1, sticky="w", padx=10, pady=2)

        # --- Bulk-Rename-Utility-style extras -----------------------------
        addrem = ttk.LabelFrame(win, text="Add / Remove", padding=8)
        addrem.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 2))
        addrem.columnconfigure(1, weight=1)
        addrem.columnconfigure(3, weight=1)

        ttk.Label(addrem, text="Prefix:").grid(row=0, column=0, sticky="w", pady=2)
        prefix_var = tk.StringVar()
        ttk.Entry(addrem, textvariable=prefix_var).grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=2)
        ttk.Label(addrem, text="Suffix (before extension):").grid(row=0, column=2, sticky="w", pady=2)
        suffix_var = tk.StringVar()
        ttk.Entry(addrem, textvariable=suffix_var).grid(row=0, column=3, sticky="ew", padx=(4, 0), pady=2)

        ttk.Label(addrem, text="Remove first N chars:").grid(row=1, column=0, sticky="w", pady=2)
        first_n_var = tk.StringVar(value="0")
        ttk.Entry(addrem, textvariable=first_n_var, width=6).grid(row=1, column=1, sticky="w", padx=(4, 12), pady=2)
        ttk.Label(addrem, text="Remove last N chars:").grid(row=1, column=2, sticky="w", pady=2)
        last_n_var = tk.StringVar(value="0")
        ttk.Entry(addrem, textvariable=last_n_var, width=6).grid(row=1, column=3, sticky="w", padx=(4, 0), pady=2)

        collapse_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(addrem, text="Collapse repeated spaces/dots/underscores into one space",
                        variable=collapse_var).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Label(win, text="Limit to media types:").grid(row=5, column=0, sticky="nw", padx=10, pady=(8, 2))
        type_frame = ttk.Frame(win)
        type_frame.grid(row=5, column=1, sticky="w", padx=10, pady=(8, 2))
        type_vars: dict[str, tk.BooleanVar] = {}
        for tag, label in (("movie", "Movies"), ("tv", "TV"),
                            ("anime", "Anime"), ("xanime", "xAnime"),
                            ("mixed", "Mixed/Other")):
            v = tk.BooleanVar(value=True)
            type_vars[tag] = v
            ttk.Checkbutton(type_frame, text=label, variable=v).pack(side=tk.LEFT, padx=2)

        status_var = tk.StringVar(value="Rules apply in order: find/replace → case → "
                                        "remove first/last → prefix/suffix → collapse.")
        ttk.Label(win, textvariable=status_var, foreground=_MUTED_TEXT, wraplength=600,
                  font=("Segoe UI", 9, "italic")).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 0)
        )
        self._apply_dark_widget_styles(win)

        button_bar = ttk.Frame(win)
        button_bar.grid(row=7, column=0, columnspan=2, sticky="e", padx=10, pady=(10, 12))

        def do_preview() -> None:
            find = find_var.get()
            use_regex = regex_var.get()
            case_mode = case_var.get()
            try:
                first_n = max(0, int(first_n_var.get() or 0))
                last_n = max(0, int(last_n_var.get() or 0))
            except ValueError:
                self._show_warning("Custom Rename", "Remove first/last N must be numbers.")
                return
            rule = {
                "find": find, "replace": replace_var.get(),
                "case": case_mode, "prefix": prefix_var.get(),
                "suffix": suffix_var.get(), "first_n": first_n,
                "last_n": last_n, "collapse": collapse_var.get(),
            }
            allowed_tags = {tag for tag, v in type_vars.items() if v.get()}

            if not any([find, case_mode != "none", rule["prefix"], rule["suffix"],
                        first_n, last_n, rule["collapse"]]):
                self._show_warning(
                    "Custom Rename",
                    "Set at least one rule (find/replace, case, prefix/suffix, "
                    "remove N, or collapse) — otherwise nothing would change.",
                )
                return

            try:
                pattern = re.compile(find) if (use_regex and find) else None
            except re.error as exc:
                self._show_warning("Custom Rename", f"Invalid regex: {exc}")
                return

            status_var.set("Building preview...")
            win.update_idletasks()

            # Task B item 2: remember this exact rule/pattern/type-selection
            # so 🔄 Re-run can replay it later without the dialog open.
            self._maint_custom_rename_replay = {
                "kind": "custom_dialog", "rule": rule, "pattern": pattern,
                "allowed_tags": allowed_tags,
            }

            def worker() -> None:
                try:
                    pairs = self._build_custom_rename_pairs(rule, pattern, allowed_tags)
                except Exception as exc:
                    logger.exception("Custom rename preview failed.")
                    self._post_to_ui(lambda: status_var.set(f"Error: {exc}"))
                    return
                self._post_to_ui(lambda: self._apply_custom_rename_preview(pairs, win))

            threading.Thread(target=worker, name="custom-rename-preview", daemon=True).start()

        ttk.Button(button_bar, text="Cancel", command=win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Preview", command=do_preview).pack(side=tk.RIGHT)

    def _build_custom_rename_pairs(
        self,
        rule: dict[str, Any],
        pattern: "re.Pattern[str] | None",
        allowed_tags: set[str],
    ) -> list[SanitizePair]:
        """
        Walk every configured library path, apply the custom rule set to each
        media file's stem (find/replace → case → remove first/last → prefix/
        suffix → collapse separators), and return SanitizePair entries for
        files whose new name would actually differ. Runs on a worker thread.
        """
        pairs: list[SanitizePair] = []
        extensions = set(config.LIBRARY_INDEX_EXTENSIONS)
        find = rule["find"]
        replace = rule["replace"]
        case_mode = rule["case"]

        for entry in config.MEDIA_LIBRARY_PATHS:
            if entry.media_type not in allowed_tags:
                continue
            root = Path(entry.path)
            if not root.is_dir():
                continue
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    suffix = Path(name).suffix.lower()
                    if suffix not in extensions:
                        continue
                    stem = Path(name).stem
                    new_stem = stem
                    if pattern is not None:
                        new_stem = pattern.sub(replace, new_stem)
                    elif find:
                        new_stem = new_stem.replace(find, replace)
                    if case_mode == "lower":
                        new_stem = new_stem.lower()
                    elif case_mode == "UPPER":
                        new_stem = new_stem.upper()
                    elif case_mode == "Title Case":
                        new_stem = new_stem.title()
                    if rule["first_n"]:
                        new_stem = new_stem[rule["first_n"]:]
                    if rule["last_n"]:
                        new_stem = new_stem[:-rule["last_n"]] if rule["last_n"] < len(new_stem) else ""
                    new_stem = f"{rule['prefix']}{new_stem}{rule['suffix']}"
                    if rule["collapse"]:
                        new_stem = re.sub(r"[\s._]{2,}", " ", new_stem).strip()
                    if not new_stem or new_stem == stem:
                        continue
                    full = Path(dirpath) / name
                    new_path = full.parent / f"{new_stem}{Path(name).suffix}"
                    try:
                        size = full.stat().st_size
                    except OSError:
                        size = 0
                    pairs.append(SanitizePair(
                        original=str(full),
                        sanitized=str(new_path),
                        size_bytes=size,
                    ))
        return pairs

    def _apply_custom_rename_preview(
        self, pairs: list[SanitizePair], dialog: "tk.Toplevel | None",
    ) -> None:
        """Push the SanitizePair preview into the maintenance tree.

        `dialog` is None when this is a 🔄 Re-run replay (Task B item 2) —
        there's no dialog open to close in that path."""
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

        self._maint_tool_name = "custom_rename"
        self._maint_results = pairs

        if not pairs:
            self._maint_status_var.set("Custom Rename: no files would change.")
            self._populate_maint_tree([], col1="Original", col2="Proposed", col3="Size")
            self._maint_append_rescan_stamp(0)
            self._show_info(
                "Custom Rename",
                "Your rule didn't match any filenames -- no changes proposed.",
            )
            return

        self._maint_status_var.set(
            f"Custom Rename preview: {len(pairs)} file(s) -- check rows then click Apply Selected"
        )
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = [
            (
                media_type_for_path(pair.original),
                (
                    "",
                    Path(pair.original).name,
                    Path(pair.sanitized).name,
                    self._fmt_bytes(pair.size_bytes),
                ),
                pair,  # payload for Apply Selected
            )
            for pair in pairs
        ]
        self._populate_maint_tree(
            [], col1="Original", col2="Proposed", col3="Size",
            typed_rows=typed_rows,
        )
        self._maint_append_rescan_stamp(len(self._maint_typed_rows))
        self._show_info(
            "Custom Rename",
            f"Previewing {len(pairs)} proposed rename(s).\n\n"
            "Tick the rows you want to apply, then click 'Apply Selected'. "
            "The media-type filter checkboxes still hide/show rows by category.",
        )

    @staticmethod
    def _remove_empty_dirs(dirs: list[str]) -> tuple[int, list[str]]:
        removed = 0
        errors: list[str] = []
        for d in dirs:
            try:
                p = Path(d)
                if p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
                    removed += 1
                    logger.info("Removed empty directory: %s", d)
            except OSError as exc:
                errors.append(f"{d}: {exc}")
        return removed, errors

    def _handle_apply_sanitize_result(self, attempted: int, errors: list[str]) -> None:
        success = attempted - len(errors)
        if errors:
            self._maint_status_var.set(f"Renamed {success}/{attempted} file(s) -- {len(errors)} error(s)")
            self._show_warning("Rename Results",
                f"Renamed {success} of {attempted} file(s).\n\nErrors:\n" + "\n".join(errors[:10]))
        else:
            self._maint_status_var.set(f"{success} file(s) renamed successfully.")
            self._show_info("Rename Complete", f"Successfully renamed {success} file(s).")
        # Pop cache + clear tool name FIRST (Task B item 4, same pattern as
        # Clean Junk) -- otherwise the re-run below sees a still-valid cache
        # entry and re-renders the just-renamed (now-stale) preview instead
        # of a fresh scan. This handler serves TWO producers of the
        # 'custom_rename' grid (the Custom Rename dialog and Combo Clean) as
        # well as plain Sanitize Names, so re-run whichever one was actually
        # showing when Apply Selected was clicked.
        tool = self._maint_tool_name
        if tool == "custom_rename":
            self._maint_rerun_custom_rename()
        else:
            self._maint_cache.pop("sanitize", None)
            self._maint_tool_name = ""
            self._run_sanitize()

    # =====================================================================
    # Utilities
    # =====================================================================

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        value: float = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(value) < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"

    # =====================================================================
    # Maintenance tab — library inventory (per-show season/episode counts)
    # =====================================================================

    def _run_library_inventory(self, *, from_idle: bool = False):
        if not from_idle:
            if self._maint_render_cached("library_inventory"):
                return None
            if not self._maint_require_library_paths("Library Inventory"):
                return None
        elif not config.PLEX_LIBRARY_PATHS:
            return None

        def job(progress, cancel_check) -> Any:
            progress(phase="Counting shows, seasons, and movie files…")
            inventory = library_inventory()
            return maint_jobs.JobResult(
                summary={"shows": len(inventory.shows),
                         "movie_files": inventory.movie_count},
                result=inventory)

        return self._maint_submit("library_inventory", "Library Inventory",
                                  job, meta={"idle": from_idle})

    def _handle_library_inventory_result(self, inventory: LibraryInventory) -> None:
        self._maint_tool_name = "library_inventory"
        self._maint_results = list(inventory.shows)

        total_shows = len(inventory.shows)
        total_episodes = sum(s.total_episodes for s in inventory.shows)
        status = (
            f"{total_shows} show(s) / {total_episodes} episode file(s) / "
            f"{inventory.movie_count} movie file(s)"
        )
        if inventory.untyped_files:
            status += f" / {inventory.untyped_files} untyped"
        self._maint_status_var.set(status)

        # One typed row per show AND per movie title — the media-type filter
        # checkboxes hide whole categories (Movies-only now actually lists
        # your movies, not just the stray shows living under a movie root).
        typed_rows: list[tuple[str, tuple[str, str, str, str], Any]] = []
        for show in inventory.shows:
            seasons_summary = ", ".join(
                f"S{season}: {count}"
                for season, count in sorted(show.seasons.items())
            )
            typed_rows.append((
                show.media_type,
                (
                    "",
                    f"{show.title}  [{show.media_type}]",
                    f"{show.total_episodes} ep -- {seasons_summary}",
                    self._fmt_bytes(show.total_size_bytes),
                ),
                None,
            ))
        for movie in inventory.movies:
            detail = "movie" + (f" -- {movie.file_count} file(s)"
                                if movie.file_count > 1 else "")
            typed_rows.append((
                movie.media_type,
                ("", movie.title, detail, self._fmt_bytes(movie.total_size_bytes)),
                None,
            ))

        self._populate_maint_tree(
            [], col1="Show / Movie", col2="Detail", col3="Size",
            typed_rows=typed_rows,
        )

        popup_lines = [
            f"Shows detected: {total_shows}",
            f"Total episode files: {total_episodes}",
            f"Movie files: {inventory.movie_count}",
        ]
        if inventory.untyped_files:
            popup_lines.append(f"Untyped files: {inventory.untyped_files}")
        if inventory.shows:
            popup_lines.append("")
            popup_lines.append("Top shows by episode count:")
            for show in inventory.shows[:5]:
                popup_lines.append(f"  - {show.title}: {show.total_episodes} ep across {len(show.seasons)} season(s)")
        self._show_info("Library Inventory", "\n".join(popup_lines))

    # =====================================================================
    # Settings tab
    # =====================================================================

    # Single source of truth for what shows up in the form, in order.
    # Each entry: (env_key, label, kind)
    #   kind ∈ {"text", "secret", "int", "bool", "path"}
    _SETTINGS_FIELDS: tuple[tuple[str, str, str], ...] = (
        # Telegram
        ("TELEGRAM_BOT_TOKEN", "Bot Token", "secret"),
        ("TELEGRAM_ALLOWED_USER_IDS", "Allowed Telegram user IDs (comma-sep)", "text"),
        ("TELEGRAM_HARD_RESET_ENABLED", "Show Hard Reset button in the Telegram bot", "bool"),
        # Plex API
        ("PLEX_SERVER_URL", "Plex Server URL", "text"),
        ("PLEX_TOKEN", "Plex Auth Token", "secret"),
        ("PLEX_CLIENT_IDENTIFIER", "Plex Client ID", "text"),
        ("PLEX_VERIFY_SSL", "Verify Plex SSL certificate", "bool"),
        ("PLEX_MEDIA_SERVER_PATH", "Plex Media Server EXE", "path"),
        # External DBs
        ("TMDB_API_KEY", "TMDB API Key", "secret"),
        ("TVDB_API_KEY", "TVDB API Key", "secret"),
        ("OMDB_API_KEY", "OMDB API Key (movie fallback)", "secret"),
        ("ANIDB_CLIENT", "AniDB Client Name", "text"),
        # Ollama
        ("OLLAMA_HOST", "Ollama Host URL", "text"),
        ("OLLAMA_MODEL", "Ollama Model Tag", "text"),
        ("OLLAMA_THINK", "LLM thinking (false/true/low/medium/high)", "text"),
        ("OLLAMA_SHOW_THINKING", "Log the LLM's thinking output", "bool"),
        # Torrent pipeline
        ("TORRENT_DOWNLOAD_DIR", "Torrent staging folder", "path"),
        ("TORRENT_STALL_TIMEOUT_SECONDS", "Torrent stall timeout (seconds)", "int"),
        ("MAX_ACTIVE_DOWNLOADS", "Max simultaneous downloads (rest queue)", "int"),
        ("DOWNLOAD_SLOW_ROTATE_MINUTES", "Rotate a no-progress download after (minutes)", "int"),
        ("DOWNLOAD_ROOT_OVERRIDE", "Force new downloads to folder/drive (blank = most free space)", "path"),
        # qBittorrent (optional download engine)
        ("QBITTORRENT_ENABLED", "Use qBittorrent instead of the built-in downloader", "bool"),
        ("QBITTORRENT_URL", "qBittorrent Web UI URL", "text"),
        ("QBITTORRENT_USERNAME", "qBittorrent username", "text"),
        ("QBITTORRENT_PASSWORD", "qBittorrent password", "secret"),
        # Quality rules
        ("BLOCK_CAMS", "Don't auto-download cams/telesyncs (movies)", "bool"),
        ("XANIME_ENABLED", "Hentai (xanime) libraries, requests + search (restart to apply)", "bool"),
        ("LOW_QUALITY_MB_PER_MIN", "Low-quality threshold (MB per minute)", "text"),
        ("SUBTITLE_LANGUAGE", "Subtitle language (en, es, fr, …)", "text"),
        ("SUBTITLE_SUBFOLDER", "Put moved subtitles in a 'Subs' subfolder (verify Plex scans it first)", "bool"),
        # Plex identity
        ("PLEX_ACCOUNT_NAME", "Which Plex user are you? (Watchlist)", "plex_account"),
        # Misc
        ("TOOLTIPS_ENABLED", "Show hover tooltips on buttons", "bool"),
        ("IDLE_CACHE_ENABLED", "Overnight pre-cache (warm all scans while idle)", "bool"),
        ("IDLE_CACHE_HOUR", "Pre-cache start hour (0-23)", "int"),
        ("LIBRARY_CHECK_HOUR", "Daily library check hour (0-23)", "int"),
        ("ADMIN_STATUS_REFRESH_SECONDS", "Status refresh interval (seconds)", "int"),
        ("LIBRARY_SEARCH_RESULT_LIMIT", "Library search result limit", "int"),
        ("LIBRARY_INDEX_EXTENSIONS", "Indexed extensions (.mkv;.mp4;...)", "text"),
    )

    _PATH_TYPE_LABELS: tuple[tuple[str, str], ...] = (
        ("movie",  "Movie"),
        ("tv",     "TV Show"),
        ("anime",  "Anime"),
        ("xanime", "Hentai / xAnime"),
        ("mixed",  "Mixed / Untyped"),
    )

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        """Construct the Settings tab — scrollable form bound to .env."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        # Outer scrollable area: Canvas + inner frame + vertical scrollbar.
        canvas = tk.Canvas(parent, highlightthickness=0)
        vscroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(canvas, padding=(4, 4, 12, 4))
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        self._settings_canvas = canvas
        self._settings_inner = inner

        def _on_inner_configure(_event: Any) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: Any) -> None:
            canvas.itemconfigure(inner_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Smooth mouse-wheel scrolling (small increments, several per notch);
        # bound only while the pointer is over this canvas.
        from ui_helpers import bind_smooth_vscroll
        bind_smooth_vscroll(canvas)

        inner.columnconfigure(0, weight=1)

        # ---- Library Paths section ------------------------------------------
        paths_frame = ttk.LabelFrame(inner, text="Library Paths", padding=10)
        paths_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        paths_frame.columnconfigure(0, weight=1)

        ttk.Label(
            paths_frame,
            text=(
                "Add one entry per folder. Multiple folders tagged with the same type "
                "are treated as a single library for that type."
            ),
            wraplength=720,
            foreground=_MUTED_TEXT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        paths_tree_frame = ttk.Frame(paths_frame)
        paths_tree_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        paths_tree_frame.columnconfigure(0, weight=1)

        self._settings_paths_tree = ttk.Treeview(
            paths_tree_frame, columns=("type", "path"), show="headings", height=6,
        )
        self._settings_paths_tree.heading("type", text="Type")
        self._settings_paths_tree.heading("path", text="Path")
        self._settings_paths_tree.column("type", width=130, anchor=tk.W, stretch=False)
        self._settings_paths_tree.column("path", width=540, anchor=tk.W)
        self._settings_paths_tree.grid(row=0, column=0, sticky="ew")

        paths_scroll = ttk.Scrollbar(paths_tree_frame, orient=tk.VERTICAL, command=self._settings_paths_tree.yview)
        paths_scroll.grid(row=0, column=1, sticky="ns")
        self._settings_paths_tree.configure(yscrollcommand=paths_scroll.set)

        # Add-row controls
        add_row = ttk.Frame(paths_frame)
        add_row.grid(row=2, column=0, columnspan=2, sticky="ew")
        add_row.columnconfigure(2, weight=1)

        new_type_var = tk.StringVar(value=self._PATH_TYPE_LABELS[0][0])
        type_combo = ttk.Combobox(
            add_row,
            textvariable=new_type_var,
            values=[label for _tag, label in self._PATH_TYPE_LABELS
                    if _tag != "xanime" or config.XANIME_ENABLED],
            state="readonly",
            width=18,
        )
        type_combo.set(self._PATH_TYPE_LABELS[0][1])
        type_combo.grid(row=0, column=0, sticky="w")

        new_path_var = tk.StringVar()
        new_path_entry = ttk.Entry(add_row, textvariable=new_path_var)
        new_path_entry.grid(row=0, column=2, sticky="ew", padx=(8, 6))

        def _browse_new_path() -> None:
            chosen = filedialog.askdirectory(parent=self.root, title="Select library folder")
            if chosen:
                new_path_var.set(chosen)

        ttk.Button(add_row, text="Browse...", command=_browse_new_path).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(
            add_row, text="Add Path",
            command=lambda: self._settings_add_path(new_type_var.get(), new_path_var.get()),
        ).grid(row=0, column=4)
        ttk.Button(
            add_row, text="Remove Selected",
            command=self._settings_remove_selected_paths,
        ).grid(row=0, column=5, padx=(6, 0))

        # ---- Generic key/value settings -------------------------------------
        current_values = load_current_settings(config.DOTENV_PATH)

        form_frame = ttk.LabelFrame(inner, text="App Settings", padding=10)
        form_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        form_frame.columnconfigure(1, weight=1)

        for row, (key, label, kind) in enumerate(self._SETTINGS_FIELDS):
            ttk.Label(form_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            current = current_values.get(key, self._format_config_value(getattr(config, key, "")))

            if kind == "bool":
                var: tk.Variable = tk.BooleanVar(value=current.strip().lower() in {"1", "true", "yes", "on"})
                ttk.Checkbutton(form_frame, variable=var).grid(row=row, column=1, sticky="w")
            elif kind == "int":
                var = tk.StringVar(value=current)
                ttk.Entry(form_frame, textvariable=var, width=10).grid(row=row, column=1, sticky="w")
            elif kind == "secret":
                var = tk.StringVar(value=current)
                ttk.Entry(form_frame, textvariable=var, show="•").grid(row=row, column=1, sticky="ew")
            elif kind == "path":
                var = tk.StringVar(value=current)
                cell = ttk.Frame(form_frame)
                cell.grid(row=row, column=1, sticky="ew")
                cell.columnconfigure(0, weight=1)
                ttk.Entry(cell, textvariable=var).grid(row=0, column=0, sticky="ew")
                ttk.Button(
                    cell, text="Browse...",
                    command=lambda v=var: self._settings_browse_file(v),
                ).grid(row=0, column=1, padx=(6, 0))
            elif kind == "plex_account":
                var = tk.StringVar(value=current)
                combo = ttk.Combobox(form_frame, textvariable=var, width=24, values=[])
                combo.grid(row=row, column=1, sticky="w")

                def _fill_accounts(c=combo) -> None:
                    def worker() -> None:
                        try:
                            from plex_api import list_plex_accounts
                            accounts = list_plex_accounts()
                        except Exception:
                            accounts = []
                        if accounts:
                            self._post_to_ui(lambda: c.configure(values=accounts))
                    threading.Thread(target=worker, name="settings-plex-accounts",
                                     daemon=True).start()
                # At build time AND on every dropdown open (retry-friendly).
                combo.configure(postcommand=_fill_accounts)
                _fill_accounts()
            else:  # "text"
                var = tk.StringVar(value=current)
                ttk.Entry(form_frame, textvariable=var).grid(row=row, column=1, sticky="ew")

            self._settings_vars[key] = var

        # ---- Download size preferences --------------------------------------
        self._build_size_pref_section(inner)

        # ---- Save / Reload bar ---------------------------------------------
        button_bar = ttk.Frame(inner)
        button_bar.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        button_bar.columnconfigure(0, weight=1)

        ttk.Label(button_bar, textvariable=self._settings_status_var,
                  font=("Segoe UI", 10, "italic"), foreground="#0a6").grid(row=0, column=0, sticky="w")
        # Only shown while no bot token is configured — the app is useful
        # without Telegram, but this keeps the path back to it obvious.
        self._settings_telegram_btn = ttk.Button(
            button_bar, text="📱 Set up Telegram bot…", style="Accent.TButton",
            command=self.open_setup_wizard)
        self._settings_telegram_btn.grid(row=0, column=1, padx=(0, 6))
        add_tooltip(self._settings_telegram_btn,
                    "No bot token is configured yet — the Telegram side is off. "
                    "Click to run the setup wizard whenever you're ready.")
        qbit_dl = ttk.Button(button_bar, text="Get qBittorrent",
                             command=lambda: webbrowser.open_new_tab(
                                 "https://www.qbittorrent.org/download"))
        qbit_dl.grid(row=0, column=2, padx=(0, 6))
        add_tooltip(qbit_dl, "Opens the qBittorrent download page. After installing: "
                             "Tools → Options → Web UI → enable it, set a password, "
                             "then fill in the fields above and Test Connection.")
        qbit_test = ttk.Button(button_bar, text="Test qBittorrent",
                               command=self._settings_test_qbittorrent)
        qbit_test.grid(row=0, column=3, padx=(0, 6))
        add_tooltip(qbit_test, "Try logging in to the qBittorrent Web UI with the "
                               "URL/username/password entered above.")
        ttk.Button(button_bar, text="Setup Wizard…",
                   command=self.open_setup_wizard).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(button_bar, text="Reload from .env",
                   command=self._settings_reload_from_disk).grid(row=0, column=5, padx=(0, 6))
        ttk.Button(button_bar, text="Save Settings",
                   command=self._settings_save).grid(row=0, column=6)
        self._update_telegram_setup_button()

        # Populate the path list from current config.
        self._settings_load_paths_from_config()

    # Size preference: (env key, label, minutes of runtime for the total-size
    # preview). Movies preview against a 2 h runtime; episodic content 24 min.
    # Actual grabs anchor on each show/movie's real runtime when known.
    _SIZE_PREF_FIELDS: tuple[tuple[str, str, int], ...] = (
        ("SIZE_PREF_MB_PER_MIN_MOVIE", "Movies", 120),
        ("SIZE_PREF_MB_PER_MIN_TV", "TV shows", 24),
        ("SIZE_PREF_MB_PER_MIN_ANIME", "Anime", 24),
        ("SIZE_PREF_MB_PER_MIN_XANIME", "Hentai / xAnime", 24),
    )

    def _build_size_pref_section(self, inner: ttk.Frame) -> None:
        """Per-type preferred download size sliders.

        MB/min is the real metric the downloader follows; the total shown
        next to it (2 h for movies, 24 min for episodes) is just a guideline
        so the number is easy to feel out — grabs use each show's real
        runtime when known. 0 = no preference.
        """
        frame = ttk.LabelFrame(inner, text="Preferred download size (0 = no preference)", padding=10)
        frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self._size_pref_frame = frame
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text=("Drag to set a target bitrate per media type. MB/min is what the "
                  "downloader matches against; the ≈ total is what that works out "
                  "to for a typical runtime (movies 2 h, episodes 24 min). Grabs "
                  "use each show's real runtime when it's known."),
            wraplength=720, foreground=_MUTED_TEXT,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        grid_row = 1
        for key, label, minutes in self._SIZE_PREF_FIELDS:
            if key.endswith("_XANIME") and not config.XANIME_ENABLED:
                continue
            max_key = key.replace("SIZE_PREF_", "SIZE_MAX_")
            for slider_key, slider_label, empty_text in (
                (key, label, "no preference"),
                (max_key, f"{label} — max acceptable", "no cap"),
            ):
                ttk.Label(frame, text=slider_label).grid(
                    row=grid_row, column=0, sticky="w", padx=(0, 8), pady=3)
                var = tk.DoubleVar(value=float(getattr(config, slider_key, 0.0)))
                readout = tk.StringVar()

                def update_readout(_v: str = "", *, var=var, readout=readout,
                                   minutes=minutes, empty_text=empty_text) -> None:
                    mb_min = round(float(var.get()) * 10) / 10  # snap to 0.1 steps
                    var.set(mb_min)
                    if mb_min <= 0:
                        readout.set(empty_text)
                        return
                    total_mb = mb_min * minutes
                    total = (f"{total_mb / 1024:.1f} GB" if total_mb >= 1024
                             else f"{total_mb:.0f} MB")
                    runtime = "2 h movie" if minutes == 120 else "24 min episode"
                    readout.set(f"{mb_min:.1f} MB/min  ≈  {total} per {runtime}")

                scale = ttk.Scale(frame, from_=0.0, to=60.0, variable=var,
                                  command=update_readout)
                scale.grid(row=grid_row, column=1, sticky="ew", pady=3)
                ttk.Label(frame, textvariable=readout, width=42).grid(
                    row=grid_row, column=2, sticky="w", padx=(10, 0), pady=3)
                update_readout()
                self._settings_vars[slider_key] = var
                self._size_pref_keys.append(slider_key)
                grid_row += 1

    def _update_telegram_setup_button(self) -> None:
        """Show the 'Set up Telegram bot' button only while no token exists."""
        btn = getattr(self, "_settings_telegram_btn", None)
        if btn is None:
            return
        if config.TELEGRAM_BOT_TOKEN:
            btn.grid_remove()
        else:
            btn.grid()

    def _settings_test_qbittorrent(self) -> None:
        # Test with the values currently typed into the form (unsaved edits
        # included) so the user can iterate without saving each time.
        url = str(self._settings_vars.get("QBITTORRENT_URL", tk.StringVar()).get()).strip()
        user = str(self._settings_vars.get("QBITTORRENT_USERNAME", tk.StringVar()).get()).strip()
        password = str(self._settings_vars.get("QBITTORRENT_PASSWORD", tk.StringVar()).get())
        self._settings_status_var.set("Testing qBittorrent connection…")

        def worker() -> None:
            import urllib.parse as _up
            import urllib.request as _rq
            try:
                body = _up.urlencode({"username": user, "password": password}).encode()
                req = _rq.Request(url.rstrip("/") + "/api/v2/auth/login", data=body)
                with _rq.urlopen(req, timeout=10) as resp:
                    ok = "Ok" in resp.read().decode("utf-8", errors="replace")
                msg = ("✔ qBittorrent connection works — enable the checkbox and Save."
                       if ok else "Login refused — check the username/password "
                                  "(qBittorrent → Options → Web UI).")
            except Exception as exc:
                msg = (f"Could not reach qBittorrent at {url}: {exc}. Is it running "
                       "with the Web UI enabled?")
            self._post_to_ui(lambda: self._settings_status_var.set(msg))

        threading.Thread(target=worker, name="qbit-test", daemon=True).start()

    # ---- Settings helpers ---------------------------------------------------

    @classmethod
    def _path_type_label(cls, tag: str) -> str:
        for t, label in cls._PATH_TYPE_LABELS:
            if t == tag:
                return label
        return tag

    @classmethod
    def _path_type_tag(cls, label: str) -> str:
        for t, lbl in cls._PATH_TYPE_LABELS:
            if lbl == label:
                return t
        # If the user passes a tag string directly, accept it.
        return label.lower()

    @staticmethod
    def _format_config_value(value: object) -> str:
        """
        Render a config attribute as the string we want pre-filled in the form.

        Crucially, tuple/list values are joined with ';' rather than left as
        their repr (which is what str(tuple) gives you). Without this, opening
        Settings and clicking Save would round-trip ``LIBRARY_INDEX_EXTENSIONS``
        as ``"('.mkv', '.mp4', ...)"`` -- a literal single-element value that
        matches no file on disk and broke every filesystem walk.
        """
        if value is None:
            return ""
        if isinstance(value, (tuple, list)):
            return ";".join(str(v) for v in value)
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _settings_load_paths_from_config(self) -> None:
        """Pull current MEDIA_LIBRARY_PATHS into the editing list + tree."""
        self._settings_paths = [
            (entry.path, entry.media_type) for entry in config.MEDIA_LIBRARY_PATHS
        ]
        self._settings_refresh_paths_tree()

    def _settings_refresh_paths_tree(self) -> None:
        if self._settings_paths_tree is None:
            return
        for item_id in self._settings_paths_tree.get_children():
            self._settings_paths_tree.delete(item_id)
        for index, (path, media_type) in enumerate(self._settings_paths):
            self._settings_paths_tree.insert(
                "", "end", iid=str(index),
                values=(self._path_type_label(media_type), path),
            )

    def _settings_add_path(self, type_label: str, path: str) -> None:
        path = path.strip()
        if not path:
            self._show_warning("Add Path", "Enter a folder path or use Browse to pick one.")
            return
        tag = self._path_type_tag(type_label)
        if tag not in {t for t, _l in self._PATH_TYPE_LABELS}:
            self._show_warning("Add Path", f"Unknown media type '{type_label}'.")
            return
        self._settings_paths.append((path, tag))
        self._settings_refresh_paths_tree()
        self._settings_status_var.set(f"Added {tag} path (not yet saved)")

    def _settings_remove_selected_paths(self) -> None:
        if self._settings_paths_tree is None:
            return
        selection = self._settings_paths_tree.selection()
        if not selection:
            self._show_warning("Remove Path", "Select one or more rows in the list first.")
            return
        # Translate iids (string indices) to ints, drop them from the list.
        to_drop = sorted({int(iid) for iid in selection}, reverse=True)
        for idx in to_drop:
            if 0 <= idx < len(self._settings_paths):
                del self._settings_paths[idx]
        self._settings_refresh_paths_tree()
        self._settings_status_var.set(f"Removed {len(to_drop)} path(s) (not yet saved)")

    def _settings_browse_file(self, var: tk.Variable) -> None:
        chosen = filedialog.askopenfilename(parent=self.root, title="Select file")
        if chosen:
            var.set(chosen)

    def _settings_reload_from_disk(self) -> None:
        """Re-read current values from .env into the form (drops unsaved edits)."""
        current_values = load_current_settings(config.DOTENV_PATH)
        for key, var in self._settings_vars.items():
            value = current_values.get(key, self._format_config_value(getattr(config, key, "")))
            if isinstance(var, tk.BooleanVar):
                var.set(value.strip().lower() in {"1", "true", "yes", "on"})
            elif isinstance(var, tk.DoubleVar):
                try:
                    var.set(float(value) if value.strip() else 0.0)
                except ValueError:
                    var.set(0.0)
            else:
                var.set(value)
        self._settings_load_paths_from_config()
        self._settings_status_var.set("Reloaded from .env")

    def _settings_collect(self) -> tuple[dict[str, str], list[str]]:
        """
        Pull every form value into a {ENV_KEY: str_value} dict for save_settings,
        plus a list of validation errors.
        """
        updates: dict[str, str] = {}
        errors: list[str] = []

        for key, _label, kind in self._SETTINGS_FIELDS:
            var = self._settings_vars.get(key)
            if var is None:
                continue
            if isinstance(var, tk.BooleanVar):
                updates[key] = "true" if var.get() else "false"
                continue
            raw = str(var.get()).strip()
            if kind == "int" and raw:
                try:
                    int(raw)
                except ValueError:
                    errors.append(f"{key} must be an integer (got '{raw}')")
                    continue
            updates[key] = raw

        # Size-preference sliders (DoubleVars registered by
        # _build_size_pref_section; "0" means no preference → drop the key).
        for key in self._size_pref_keys:
            var = self._settings_vars.get(key)
            if var is None:
                continue
            value = float(var.get())
            updates[key] = f"{value:g}" if value > 0 else ""

        # Encode the path list. Always write MEDIA_LIBRARY_PATHS, and clear
        # PLEX_LIBRARY_PATHS so we don't keep a stale legacy fallback active.
        updates["MEDIA_LIBRARY_PATHS"] = config.format_media_library_paths(
            [config.MediaLibraryPath(path=p, media_type=t) for p, t in self._settings_paths]
        )
        updates["PLEX_LIBRARY_PATHS"] = ""

        return updates, errors

    def _settings_save(self) -> None:
        updates, errors = self._settings_collect()
        if errors:
            self._show_warning("Settings", "Cannot save:\n\n" + "\n".join(errors))
            return

        # Snapshot the *encoded* path string before save so we can detect
        # whether the user changed library paths (and therefore needs a
        # reindex).
        old_paths_encoded = config.format_media_library_paths(config.MEDIA_LIBRARY_PATHS)
        new_paths_encoded = updates.get("MEDIA_LIBRARY_PATHS", "")
        paths_changed = old_paths_encoded != new_paths_encoded

        try:
            save_settings(updates, config.DOTENV_PATH)
        except RuntimeError as exc:
            logger.exception("Settings save failed.")
            self._show_warning("Save Settings", str(exc))
            self._settings_status_var.set("Save failed — see popup")
            return

        # Live-reload .env into the running config so changes take effect
        # immediately for everything that reads `config.NAME` at call time
        # (library paths, API keys, server URLs, etc.).
        reload_warning = ""
        try:
            reload_config_from_env(config.DOTENV_PATH)
        except RuntimeError as exc:
            logger.warning("Live config reload failed: %s", exc)
            reload_warning = (
                "\n\nNote: live reload failed -- you'll need to restart the app "
                "for the changes to take effect."
            )

        # Refresh anything in the UI that's keyed on config values.
        self._settings_load_paths_from_config()
        self.refresh_library_summary()
        self.refresh_library_metrics()
        self._update_telegram_setup_button()

        self._settings_status_var.set("Saved and applied")

        if paths_changed and config.PLEX_LIBRARY_PATHS:
            if self._ask_yes_no(
                "Library paths changed",
                "Library paths were updated. The local file index still reflects "
                "the old folders.\n\n"
                "Reindex now? (Searches won't find files in newly-added paths "
                "until the index is rebuilt.)",
            ):
                self.rebuild_library_index_from_ui()
                return

        self._show_info(
            "Settings Saved",
            "Settings were written to your .env file and applied to the "
            "running app." + reload_warning + "\n\n"
            "The Telegram bot itself uses its token from startup, so if you "
            "changed TELEGRAM_BOT_TOKEN, restart the app for that one.",
        )

    # =====================================================================
    # Setup Wizard — first-run onboarding (also reachable from Settings)
    # =====================================================================

    def open_setup_wizard(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Setup Wizard")
        win.geometry("740x600")
        win.transient(self.root)
        win.columnconfigure(0, weight=1)

        status_var = tk.StringVar(value="")

        # --- Step 1: Telegram bot token --------------------------------
        step1 = ttk.LabelFrame(win, text="1 · Telegram bot (required)", padding=10)
        step1.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        step1.columnconfigure(1, weight=1)
        ttk.Label(step1, wraplength=600, justify=tk.LEFT, text=(
            "Telegram doesn't allow apps to create bots automatically — a human "
            "has to ask @BotFather once. It takes about a minute:\n"
            "  1. Get Telegram if you don't have it (button below — phone or desktop).\n"
            "  2. Open @BotFather and send /newbot, pick a name and a unique username.\n"
            "  3. Copy the token it gives you and paste it here.\n\n"
            "No Telegram? No problem — everything except the remote bot works "
            "without it. Skip for now and a 'Set up Telegram bot' button stays "
            "on the Settings tab until you're ready."
        )).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        btn_row1 = ttk.Frame(step1)
        btn_row1.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Button(btn_row1, text="Get Telegram",
                   command=lambda: webbrowser.open_new_tab("https://telegram.org/apps")
                   ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row1, text="Open @BotFather",
                   command=lambda: webbrowser.open_new_tab("https://t.me/BotFather")
                   ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row1, text="Skip — set up Telegram later",
                   command=lambda: (status_var.set(
                       "Telegram skipped — find 'Set up Telegram bot…' on the "
                       "Settings tab when you're ready."))
                   ).pack(side=tk.LEFT)
        token_var = tk.StringVar(value=config.TELEGRAM_BOT_TOKEN)
        ttk.Entry(step1, textvariable=token_var, show="•").grid(row=2, column=1, sticky="ew")
        ttk.Label(step1, text="Token:").grid(row=2, column=0, sticky="w")
        ttk.Button(step1, text="Validate && Save", style="Accent.TButton",
                   command=lambda: self._wizard_validate_token(token_var.get().strip(), status_var)
                   ).grid(row=2, column=2, padx=(8, 0))

        # --- Step 2: library folders ------------------------------------
        step2 = ttk.LabelFrame(win, text="2 · Library folders", padding=10)
        step2.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        ttk.Label(step2, wraplength=600, justify=tk.LEFT, text=(
            "Tell the app where your media lives (tagged movie / tv / anime / …). "
            "Everything — search, tracking, downloads — keys off these."
        )).pack(anchor="w", pady=(0, 6))
        ttk.Button(step2, text="Open Settings → Library Paths",
                   command=lambda: (self._notebook.select(self._settings_tab)
                                    if self._notebook is not None else None)
                   ).pack(anchor="w")

        # --- Step 3: optional components --------------------------------
        step3 = ttk.LabelFrame(win, text="3 · Optional components", padding=10)
        step3.grid(row=2, column=0, sticky="ew", padx=12, pady=6)
        step3.columnconfigure(0, weight=1)
        node_var = tk.BooleanVar(value=shutil.which(config.NODE_PATH) is None)
        ollama_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(step3, variable=node_var,
                        text="Install Node.js LTS (needed for the torrent Downloads tab)"
                        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            step3, variable=ollama_var,
            text=("Install Ollama + pull a small local model (~800 MB, CPU-friendly)\n"
                  "for smarter request understanding — the app works fine without it"),
        ).grid(row=1, column=0, sticky="w")
        xanime_var = tk.BooleanVar(value=config.XANIME_ENABLED)

        def _save_xanime() -> None:
            self._persist_dl_toggle("XANIME_ENABLED", xanime_var)
            status_var.set("Hentai support %s — applies on next launch."
                           % ("on" if xanime_var.get() else "off"))

        ttk.Checkbutton(
            step3, variable=xanime_var, command=_save_xanime,
            text=("Include hentai (xanime) libraries, requests, "
                  "and search — off unless you want it"),
        ).grid(row=2, column=0, sticky="w")
        btn_row = ttk.Frame(step3)
        btn_row.grid(row=3, column=0, sticky="w", pady=(6, 0))
        if platform_adapter.supports_winget():
            ttk.Button(btn_row, text="Install checked (via winget)",
                       command=lambda: self._wizard_install(node_var.get(), ollama_var.get(), status_var)
                       ).pack(side=tk.LEFT, padx=(0, 8))
        else:
            # Task H item 4: Linux never auto-runs a package manager and
            # never asks for root — diagnostics name what's missing and
            # show the commands, the user runs them.
            ttk.Button(btn_row, text="Check Linux dependencies",
                       command=lambda: self._show_info(
                           "Linux dependencies",
                           platform_adapter.dependency_install_guidance())
                       ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="npm install torrent runner",
                   command=lambda: self._wizard_npm_install(status_var)).pack(side=tk.LEFT)

        # --- Step 4: API keys --------------------------------------------
        step4 = ttk.LabelFrame(win, text="4 · Free API keys (optional, recommended)", padding=10)
        step4.grid(row=3, column=0, sticky="ew", padx=12, pady=6)
        ttk.Label(step4, wraplength=600, justify=tk.LEFT, text=(
            "TMDB and TVDB power request lookups and TV episode lists. Both are "
            "free accounts — grab a key and paste it on the Settings tab. Anime "
            "identification already works offline without any key."
        )).pack(anchor="w", pady=(0, 6))
        key_row = ttk.Frame(step4)
        key_row.pack(anchor="w")
        ttk.Button(key_row, text="Get TMDB key",
                   command=lambda: webbrowser.open_new_tab("https://www.themoviedb.org/settings/api")
                   ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(key_row, text="Get TVDB key",
                   command=lambda: webbrowser.open_new_tab("https://thetvdb.com/api-information")
                   ).pack(side=tk.LEFT)

        bottom = ttk.Frame(win, padding=(12, 6))
        bottom.grid(row=4, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=status_var, wraplength=520,
                  font=("Segoe UI", 9, "italic")).grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="Run Health Check",
                   command=lambda: (win.destroy(), self.run_health_check())
                   ).grid(row=0, column=1, padx=(8, 6))
        ttk.Button(bottom, text="Close", command=win.destroy).grid(row=0, column=2)
        self._apply_dark_widget_styles(win)
        return win

    def _wizard_validate_token(self, token: str, status_var: tk.StringVar) -> None:
        if not token:
            status_var.set("Paste a bot token first.")
            return
        status_var.set("Checking token with Telegram…")

        def worker() -> None:
            import json as _json
            import urllib.request as _rq
            try:
                with _rq.urlopen(f"https://api.telegram.org/bot{token}/getMe",
                                 timeout=15) as resp:
                    data = _json.loads(resp.read().decode())
                username = data["result"]["username"]
            except Exception:
                self._post_to_ui(lambda: status_var.set(
                    "Token rejected by Telegram — double-check you copied the whole thing."))
                return

            save_settings({"TELEGRAM_BOT_TOKEN": token}, config.DOTENV_PATH)
            try:
                reload_config_from_env(config.DOTENV_PATH)
            except RuntimeError:
                pass

            def apply() -> None:
                status_var.set(f"Token valid — bot is @{username}. Starting bot…")
                self._update_telegram_setup_button()
                if not self.bot_service.running:
                    try:
                        self.bot_service.start()
                        self.bot_status_var.set("Telegram bot: running")
                        self._bot_dot.configure(foreground=_DOT_GREEN)
                        status_var.set(f"✔ Bot @{username} is up. Message it on Telegram to test!")
                    except Exception as exc:
                        status_var.set(f"Token saved, but the bot failed to start: {exc}")
            self._post_to_ui(apply)

        threading.Thread(target=worker, name="wizard-token", daemon=True).start()

    def _wizard_install(self, node: bool, ollama: bool, status_var: tk.StringVar) -> None:
        if not platform_adapter.supports_winget():
            status_var.set("winget is Windows-only — use 'Check Linux "
                           "dependencies' for the install commands.")
            return
        if not node and not ollama:
            status_var.set("Nothing checked to install.")
            return
        status_var.set("Installing via winget — this can take a few minutes…")

        def worker() -> None:
            lines: list[str] = []
            packages = ([("Node.js", "OpenJS.NodeJS.LTS")] if node else []) + \
                       ([("Ollama", "Ollama.Ollama")] if ollama else [])
            for label, pkg in packages:
                try:
                    result = subprocess.run(
                        ["winget", "install", "--id", pkg, "-e", "--silent",
                         "--accept-source-agreements", "--accept-package-agreements"],
                        capture_output=True, text=True, timeout=900,
                    )
                    ok = result.returncode == 0 or "already installed" in (result.stdout or "").lower()
                    lines.append(f"{label}: {'installed ✔' if ok else 'failed — see log'}")
                    if not ok:
                        logger.warning("winget %s failed: %s", pkg, result.stdout[-500:])
                except Exception as exc:
                    lines.append(f"{label}: failed ({exc})")
            if ollama:
                try:
                    subprocess.run(["ollama", "pull", config.OLLAMA_MODEL],
                                   capture_output=True, text=True, timeout=600)
                    lines.append(f"Pulled model {config.OLLAMA_MODEL}")
                except Exception:
                    lines.append("Model pull skipped — run 'ollama pull "
                                 f"{config.OLLAMA_MODEL}' after Ollama starts.")
            self._post_to_ui(lambda: status_var.set(" · ".join(lines)))

        threading.Thread(target=worker, name="wizard-install", daemon=True).start()

    def _wizard_npm_install(self, status_var: tk.StringVar) -> None:
        """Seed + npm install the torrent runner. Shares ONE implementation with
        the automatic first-grab path (download_manager.ensure_runner_ready), so
        the button and the self-heal can never drift apart."""
        import download_manager as _dm
        status_var.set("Setting up the torrent runner (npm install)…")

        def worker() -> None:
            ok, why = _dm.ensure_runner_ready()
            msg = "Torrent runner ready ✔" if ok else why
            self._post_to_ui(lambda: status_var.set(msg))

        threading.Thread(target=worker, name="wizard-npm", daemon=True).start()

    # =====================================================================
    # Cross-tab helpers
    # =====================================================================

    def open_sanitize_for_show(self, show: Any) -> None:
        """Shows tab → Maintenance: preview sanitize renames for one show's
        files. Nothing is renamed until the user ticks rows + Apply Selected."""
        if self._notebook is not None and self._maintenance_tab is not None:
            self._notebook.select(self._maintenance_tab)

        def job(progress, cancel_check) -> Any:
            progress(phase=f"Previewing renames for '{show.title}'…")
            extensions = set(config.LIBRARY_INDEX_EXTENSIONS)
            pairs: list[SanitizePair] = []
            for folder in show.folders:
                root = Path(folder)
                if not root.is_dir():
                    continue
                for f in root.rglob("*"):
                    if not f.is_file() or f.suffix.lower() not in extensions:
                        continue
                    try:
                        pair = sanitize_filename(str(f), dry_run=True)
                    except Exception:
                        continue
                    if pair.original != pair.sanitized:
                        pairs.append(pair)
            return maint_jobs.JobResult(
                summary={"renames_proposed": len(pairs)}, result=pairs)

        # dedupe=False: two different shows share the tool_key, and the
        # second preview must not be swallowed by the first.
        self._maint_submit("sanitize_show",
                           f"Sanitize preview for '{show.title}'", job,
                           dedupe=False)

    def open_watch_history(self) -> None:
        """Users tab → Plex watch history (who watched what, when)."""
        win = tk.Toplevel(self.root)
        win.title("Plex watch history")
        win.geometry("720x480")
        win.transient(self.root)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        tree = ttk.Treeview(win, columns=("at", "user", "title"), show="headings")
        for col, text, width in (("at", "When", 130), ("user", "User", 140),
                                 ("title", "Watched", 400)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor=tk.W)
        tree.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        scroll = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=10)
        tree.configure(yscrollcommand=scroll.set)
        make_sortable(tree)
        status = ttk.Label(win, text="Loading history from Plex…",
                           font=("Segoe UI", 9, "italic"))
        status.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 10))

        def worker() -> None:
            try:
                from plex_api import get_watch_history
                rows = get_watch_history(limit=config.PLEX_HISTORY_FETCH_LIMIT)
            except Exception as exc:
                self._post_to_ui(lambda: status.configure(
                    text=f"History unavailable: {exc} (Plex token required)"))
                return

            def render() -> None:
                for r in rows:
                    tree.insert("", "end", values=(r.at, r.user, r.title))
                status.configure(text=f"{len(rows)} plays (most recent first)")
            self._post_to_ui(render)

        threading.Thread(target=worker, name="ui-watch-history", daemon=True).start()

    # =====================================================================
    # Shutdown
    # =====================================================================

    def request_exit(self) -> None:
        if self._quitting:
            return
        self._post_to_ui(self._shutdown)

    def shutdown_from_terminal(self) -> None:
        self._shutdown(bot_timeout=3.0)

    def _shutdown(self, *, bot_timeout: float = 20.0) -> None:
        if self._quitting:
            return
        self._quitting = True
        logger.info("Shutting down desktop app.")
        try:
            self.download_manager.shutdown()
        except Exception:
            logger.exception("Failed to stop download runners cleanly.")
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            logger.exception("Failed to stop tray icon cleanly.")
        try:
            self.bot_service.stop(timeout=bot_timeout)
        except Exception:
            logger.exception("Failed to stop Telegram bot service cleanly.")
        try:
            self.root.destroy()
        except tk.TclError:
            logger.debug("Tk root was already destroyed during shutdown.")


def run_desktop_app() -> None:
    DesktopApp().run()
