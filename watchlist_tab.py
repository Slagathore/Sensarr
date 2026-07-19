# =============================================================================
# watchlist_tab.py
# =============================================================================
# The "Watchlist / Recs" notebook tab (same split-out pattern as shows_tab):
#
#   Watchlist — pulls the Plex account's watchlist from the plex.tv discover
#               API. Right-click / button to search torrents for an item
#               (jumps to Downloads), or queue everything selected as
#               requests for the auto-grab pass to handle hands-free.
#   Recs      — genre-affinity recommendations from one Plex user's watch
#               history, rendered as two honest sections that are never
#               mixed: "In your library (unwatched)" and "Discover (not in
#               library)". Discover is seeded from TMDB similar/
#               recommendations on recency-weighted watch history, falling
#               back to popular-in-genre when history is too thin. The
#               "who are you" account comes from Settings.
# =============================================================================

import logging
import threading
import tkinter as tk
from tkinter import ttk

import config
import queue_store
import request_intake
from ui_helpers import add_tooltip, make_sortable

logger = logging.getLogger(__name__)

# Selectable before any fetch has run; a fetch merges in the user's own top
# genres. Names match the TMDB discover maps in plex_api.
_STANDARD_GENRES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "Horror", "Mystery", "Romance",
    "Science Fiction", "Thriller", "War", "Western",
]

_TYPE_TO_MEDIA = {"movie": "movie", "show": "tv", "anime": "anime", "xanime": "xanime"}
_TYPE_FILTER_VALUES = ["All", "movie", "show", "anime", "xanime"]

_LIB_HEADER = "── In your library (unwatched) ──"
_DISCOVER_HEADER = "── Discover (not in library) ──"


class WatchlistTab:
    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app
        self._watchlist: list = []
        self._library_recs: list = []   # "In your library (unwatched)"
        self._discover_recs: list = []  # "Discover (not in library)"
        self._recs_rows: list = []      # tree row index -> Recommendation | None (header)
        self._generated_at: float = 0.0
        self._top_genres: list[str] = []
        self._status_var = tk.StringVar(
            value="Pull Watchlist to load your Plex watchlist (Plex token required).")

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for text, command, tip in (
            ("Pull Watchlist", self.pull_watchlist,
             "Fetch your Plex watchlist from plex.tv (the account that owns the token)."),
            ("🔍 Search Torrents for Selected", self.search_selected,
             "Jump to the Downloads tab with a torrent search for the selected item."),
            ("⬇ Queue All Selected", self.queue_selected,
             "Add every selected item to the request queue for the auto-grab pass "
             "to download hands-free. Needs 'Auto-grab new requests' turned on."),
        ):
            btn = ttk.Button(toolbar, text=text, command=command)
            btn.pack(side=tk.LEFT, padx=(0, 6))
            add_tooltip(btn, tip)
        ttk.Label(toolbar, textvariable=self._status_var,
                  font=("Segoe UI", 9, "italic")).pack(side=tk.LEFT, padx=(10, 0))

        panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        panes.grid(row=1, column=0, sticky="nsew")

        # --- Watchlist ---------------------------------------------------
        wl_frame = ttk.LabelFrame(panes, text="Plex Watchlist", padding=6)
        panes.add(wl_frame, weight=1)
        wl_frame.columnconfigure(0, weight=1)
        wl_frame.rowconfigure(0, weight=1)
        wl_tree = ttk.Treeview(
            wl_frame, columns=("title", "year", "type", "inlib"),
            show="headings", selectmode="extended",
        )
        for col, text, width, stretch in (
            ("title", "Title", 380, True), ("year", "Year", 60, False),
            ("type", "Type", 70, False), ("inlib", "In Library", 80, False),
        ):
            wl_tree.heading(col, text=text)
            wl_tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        wl_tree.grid(row=0, column=0, sticky="nsew")
        wl_scroll = ttk.Scrollbar(wl_frame, orient=tk.VERTICAL, command=wl_tree.yview)
        wl_scroll.grid(row=0, column=1, sticky="ns")
        wl_tree.configure(yscrollcommand=wl_scroll.set)
        wl_tree.bind("<Button-3>", self._on_watchlist_right_click)
        make_sortable(wl_tree)
        self._wl_tree = wl_tree

        # --- Recommendations ----------------------------------------------
        recs_frame = ttk.LabelFrame(panes, text="Recommendations (what to watch next)", padding=6)
        panes.add(recs_frame, weight=1)
        recs_frame.columnconfigure(0, weight=1)
        recs_frame.rowconfigure(1, weight=1)

        recs_bar = ttk.Frame(recs_frame)
        recs_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(recs_bar, text="Plex user:").pack(side=tk.LEFT)
        self._user_var = tk.StringVar(value=config.PLEX_ACCOUNT_NAME)
        self._user_combo = ttk.Combobox(recs_bar, textvariable=self._user_var,
                                        width=18, state="readonly", values=[])
        # Refetch on dropdown open so a failed startup fetch self-heals.
        self._user_combo.configure(postcommand=self._load_accounts)
        self._user_combo.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(recs_bar, text="Genre:").pack(side=tk.LEFT)
        self._genre_var = tk.StringVar(value="All")
        # Seeded with the standard genre set so the filter works before the
        # first fetch; a fetch swaps in the user's actual top genres.
        self._genre_combo = ttk.Combobox(
            recs_bar, textvariable=self._genre_var, width=16, state="readonly",
            values=["All"] + _STANDARD_GENRES)
        self._genre_combo.pack(side=tk.LEFT, padx=(4, 12))
        # The genre filter is applied at fetch time — picking one refetches,
        # otherwise the selection silently did nothing until the next click.
        self._genre_combo.bind("<<ComboboxSelected>>", lambda _e: self.get_recs())
        ttk.Label(recs_bar, text="Type:").pack(side=tk.LEFT)
        self._type_var = tk.StringVar(value="All")
        type_combo = ttk.Combobox(recs_bar, textvariable=self._type_var, width=8,
                                  state="readonly", values=_TYPE_FILTER_VALUES)
        type_combo.pack(side=tk.LEFT, padx=(4, 12))
        type_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_recs())
        get_btn = ttk.Button(recs_bar, text="Get Recommendations", command=self.get_recs)
        get_btn.pack(side=tk.LEFT)
        add_tooltip(get_btn, "Analyse this user's Plex watch history, find their top "
                             "genres, and show two lists: what you already own but "
                             "haven't watched, and new titles to discover.")

        recs_tree = ttk.Treeview(
            recs_frame, columns=("title", "year", "type", "note", "inlib"),
            show="headings", selectmode="extended",
        )
        for col, text, width, stretch in (
            ("title", "Title", 300, True), ("year", "Year", 60, False),
            ("type", "Type", 60, False), ("note", "Why", 280, True),
            ("inlib", "In Library", 80, False),
        ):
            recs_tree.heading(col, text=text)
            recs_tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        recs_tree.grid(row=1, column=0, sticky="nsew")
        recs_tree.tag_configure("header", font=("Segoe UI", 9, "bold"))
        recs_scroll = ttk.Scrollbar(recs_frame, orient=tk.VERTICAL, command=recs_tree.yview)
        recs_scroll.grid(row=1, column=1, sticky="ns")
        recs_tree.configure(yscrollcommand=recs_scroll.set)
        recs_tree.bind("<Button-3>", self._on_recs_right_click)
        make_sortable(recs_tree)
        self._recs_tree = recs_tree

        self._load_accounts()
        self._load_persisted_recs()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    # JSON, not pickle (Task S item 1): the cache dir is user-writable and
    # the app runs elevated — a cache file must never be able to run code.
    # Bumped to 2 for the library/discover split + freshness stamp (fix
    # sprint, Task C) — a v1 cache is a clean miss, never misread.
    _RECS_CACHE_VERSION = 2

    def _recs_cache_path(self):
        import app_paths
        return app_paths.PATHS.cache_dir / "watchlist_recs.json"

    def _load_persisted_recs(self) -> None:
        """Last fetch survives restarts — the tab is never empty if recs
        have EVER been generated. Get Recommendations refreshes, and a cache
        older than config.RECS_CACHE_TTL_HOURS auto-refreshes itself once
        rendered. A malformed or pre-v2 cache file is a cache miss, never an
        error."""
        import json_cache
        from plex_api import Recommendation
        payload = json_cache.load_json_cache(
            self._recs_cache_path(), version=self._RECS_CACHE_VERSION,
            dataclass_types=(Recommendation,))
        if not isinstance(payload, dict):
            return
        try:
            self._library_recs = payload.get("library", [])
            self._discover_recs = payload.get("discover", [])
            self._generated_at = float(payload.get("generated_at") or 0)
            self._top_genres = payload.get("genres", [])
            if self._top_genres:
                merged = self._top_genres + [g for g in _STANDARD_GENRES
                                             if g not in self._top_genres]
                self._genre_combo.configure(values=["All"] + merged)
            self._render_recs()
            at_label = payload.get("at", "?")
            from plex_api import recs_cache_is_stale
            if recs_cache_is_stale(self._generated_at, ttl_hours=config.RECS_CACHE_TTL_HOURS):
                self._status_var.set(
                    f"Loaded last recommendations (from {at_label}) — stale, refreshing…")
                self.get_recs()
            else:
                self._status_var.set(
                    f"Loaded last recommendations (from {at_label}) — "
                    "Get Recommendations refreshes.")
        except Exception:
            logger.debug("Persisted recs render failed.", exc_info=True)

    def _persist_recs(self) -> None:
        import datetime
        import json_cache
        json_cache.save_json_cache(
            self._recs_cache_path(),
            {"library": self._library_recs, "discover": self._discover_recs,
             "genres": self._top_genres, "generated_at": self._generated_at,
             "at": datetime.datetime.now().strftime("%b %d %H:%M")},
            version=self._RECS_CACHE_VERSION,
            legacy_paths=[self._recs_cache_path().with_suffix(".pkl")])

    def _load_accounts(self) -> None:
        def worker() -> None:
            try:
                from plex_api import list_plex_accounts
                accounts = list_plex_accounts()
            except Exception:
                accounts = []

            def apply() -> None:
                self._user_combo.configure(values=accounts)
                if not self._user_var.get() and accounts:
                    self._user_var.set(accounts[0])
            self.app._post_to_ui(apply)

        threading.Thread(target=worker, name="wl-accounts", daemon=True).start()

    def pull_watchlist(self) -> None:
        self._status_var.set("Pulling watchlist from plex.tv…")

        def worker() -> None:
            try:
                from plex_api import (LibraryProviderIndex,
                                      build_library_provider_index,
                                      check_item_in_library, get_watchlist)
                items = get_watchlist()
                # Presence check: provider id (Plex GUID / TMDB / TVDB id, via
                # WatchlistItem.tmdb_id/tvdb_id/imdb_id) FIRST, identity-aware
                # title match SECOND — never a filename substring (that was
                # the "In Library" column's bug).
                #
                # get_watchlist() only needs PLEX_TOKEN (it talks to
                # discover.provider.plex.tv); build_library_provider_index()
                # additionally needs PLEX_SERVER_URL (it walks the local Plex
                # server's sections) and raises when that's unset. A
                # token-only setup must still degrade to blank In-Library
                # flags here, not fail the whole pull — the per-item
                # check_item_in_library fallback below still gets a chance
                # via media_lookup.check_library_for_title.
                try:
                    provider_index = build_library_provider_index()
                except Exception:
                    logger.debug("Provider index unavailable for watchlist pull "
                                "(PLEX_SERVER_URL likely unset) — falling back "
                                "to title-only presence checks.", exc_info=True)
                    provider_index = LibraryProviderIndex()
                in_lib: dict[int, bool] = {}
                for idx, item in enumerate(items):
                    media_kind = "movie" if item.item_type == "movie" else "show"
                    try:
                        in_lib[idx] = check_item_in_library(
                            item.title, media_kind, tmdb_id=item.tmdb_id,
                            tvdb_id=item.tvdb_id, imdb_id=item.imdb_id,
                            provider_index=provider_index)
                    except Exception:
                        in_lib[idx] = False
            except Exception as exc:
                self.app._post_to_ui(
                    lambda: self._status_var.set(f"Watchlist unavailable: {exc}"))
                return

            def render() -> None:
                self._watchlist = items
                for iid in self._wl_tree.get_children():
                    self._wl_tree.delete(iid)
                for idx, item in enumerate(items):
                    self._wl_tree.insert(
                        "", "end", iid=str(idx),
                        values=(item.title, item.year or "", item.item_type,
                                "✓" if in_lib.get(idx) else ""),
                    )
                self._status_var.set(f"{len(items)} watchlist item(s)")
            self.app._post_to_ui(render)

        threading.Thread(target=worker, name="wl-pull", daemon=True).start()

    def get_recs(self) -> None:
        user = self._user_var.get().strip()
        genre = self._genre_var.get().strip()
        self._status_var.set(f"Analysing {user or 'all users'}'s watch history…")

        def worker() -> None:
            try:
                from plex_api import get_recommendations
                result = get_recommendations(
                    user or None,
                    genre_filter=None if genre in ("", "All") else genre)
            except Exception as exc:
                self.app._post_to_ui(
                    lambda: self._status_var.set(f"Recommendations unavailable: {exc}"))
                return

            def render() -> None:
                self._library_recs = result.library
                self._discover_recs = result.discover
                self._generated_at = result.generated_at
                if result.top_genres:  # keep the previous genre list on a dud fetch
                    self._top_genres = result.top_genres
                    merged = result.top_genres + [g for g in _STANDARD_GENRES
                                                  if g not in result.top_genres]
                    self._genre_combo.configure(values=["All"] + merged)
                self._render_recs()
                self._persist_recs()
            self.app._post_to_ui(render)

        threading.Thread(target=worker, name="wl-recs", daemon=True).start()

    def _render_recs(self) -> None:
        """Render two honest, never-mixed sections: library (owned,
        unwatched) then discover (not owned). The type filter narrows each
        section independently; a section that renders empty still shows its
        header with a (0) count, so 'no discover results' reads as an
        answer, not a blank pane."""
        type_filter = self._type_var.get()

        def _filtered(items):
            return items if type_filter == "All" else [
                r for r in items if r.item_type == type_filter]

        library = _filtered(list(getattr(self, "_library_recs", [])))
        discover = _filtered(list(getattr(self, "_discover_recs", [])))

        for iid in self._recs_tree.get_children():
            self._recs_tree.delete(iid)
        self._recs_rows = []

        def _insert_header(label: str, count: int) -> None:
            idx = len(self._recs_rows)
            self._recs_rows.append(None)
            self._recs_tree.insert(
                "", "end", iid=str(idx), tags=("header",),
                values=(f"{label} ({count})", "", "", "", ""))

        def _insert_items(items) -> None:
            for r in items:
                idx = len(self._recs_rows)
                self._recs_rows.append(r)
                self._recs_tree.insert(
                    "", "end", iid=str(idx),
                    values=(r.title, r.year or "", r.item_type, r.note,
                            "✓" if r.in_library else ""),
                )

        _insert_header(_LIB_HEADER, len(library))
        _insert_items(library)
        _insert_header(_DISCOVER_HEADER, len(discover))
        _insert_items(discover)

        top_genres = getattr(self, "_top_genres", [])
        self._status_var.set(
            f"{len(library)} in your library, {len(discover)} to discover — "
            "top genres: " + (", ".join(top_genres[:5]) or "none found"))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _selected_watchlist_items(self) -> list:
        return [self._watchlist[int(i)] for i in self._wl_tree.selection()
                if int(i) < len(self._watchlist)]

    def _selected_recs(self) -> list:
        """Selected recommendation rows, header rows silently skipped (a
        header is not a pickable item)."""
        out = []
        for i in self._recs_tree.selection():
            idx = int(i)
            if idx < len(self._recs_rows) and self._recs_rows[idx] is not None:
                out.append(self._recs_rows[idx])
        return out

    def search_selected(self) -> None:
        items = self._selected_watchlist_items()
        if not items:
            self.app._show_warning("Nothing selected", "Select a watchlist item first.")
            return
        item = items[0]
        query = f"{item.title} {item.year}" if item.year else item.title
        self.app.open_downloads_search(query, _TYPE_TO_MEDIA.get(item.item_type, "movie"))
        if len(items) > 1:
            self._status_var.set(
                "Searched the first selected item — use Queue All Selected for bulk.")

    def queue_selected(self) -> None:
        items = self._selected_watchlist_items()
        if not items:
            self.app._show_warning("Nothing selected", "Select watchlist items first.")
            return
        if not config.TORRENT_AUTO_GRAB:
            self.app._show_info(
                "Automation is off",
                "Queue All Selected hands these to the auto-grab pass, but "
                "'Auto-grab new requests' is currently OFF (Downloads tab).\n\n"
                "Turn it on first, or use 'Search Torrents for Selected' to "
                "grab items by hand.",
            )
            return
        queued = 0
        needs_id = 0
        for item in items:
            # Use the item's parsed Plex GUID identity instead of re-searching
            # by title (Task A). Items without a usable GUID (or shows whose
            # season list can't be resolved) land as needs_identity: visible,
            # never silently grabbed as the wrong thing.
            outcome = request_intake.queue_watchlist_item(item, "Watchlist")
            queued += len(outcome.request_ids)
            if outcome.status == queue_store.STATUS_NEEDS_IDENTITY:
                needs_id += 1
        note = (f" ({needs_id} need a title/season picked before they grab)"
                if needs_id else "")
        self._status_var.set(
            f"Queued {queued} request(s){note} — the auto-grab pass (runs every "
            "5 min) will search and download the resolved ones.")
        self.app.refresh_requests()

    def _on_watchlist_right_click(self, event) -> None:
        iid = self._wl_tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self._wl_tree.selection():
            self._wl_tree.selection_set(iid)
        item = self._watchlist[int(iid)]
        menu = tk.Menu(self._wl_tree, tearoff=0)
        query = f"{item.title} {item.year}" if item.year else item.title
        menu.add_command(
            label=f"🔍 Search torrents for '{item.title}'",
            command=lambda: self.app.open_downloads_search(
                query, _TYPE_TO_MEDIA.get(item.item_type, "movie")),
        )
        menu.add_command(label="⬇ Queue for auto-download", command=self.queue_selected)
        menu.tk_popup(event.x_root, event.y_root)

    def _on_recs_right_click(self, event) -> None:
        iid = self._recs_tree.identify_row(event.y)
        if not iid:
            return
        idx = int(iid)
        if idx >= len(self._recs_rows) or self._recs_rows[idx] is None:
            return  # header row — nothing to act on
        if iid not in self._recs_tree.selection():
            self._recs_tree.selection_set(iid)
        r = self._recs_rows[idx]
        menu = tk.Menu(self._recs_tree, tearoff=0)
        query = f"{r.title} {r.year}" if r.year else r.title
        menu.add_command(
            label=f"🔍 Search torrents for '{r.title}'",
            command=lambda: self.app.open_downloads_search(
                query, _TYPE_TO_MEDIA.get(r.item_type, "movie")),
        )
        menu.tk_popup(event.x_root, event.y_root)
