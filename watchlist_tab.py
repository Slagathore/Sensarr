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
#               history: unwatched in-library items in their top genres,
#               optionally topped up with popular TMDB titles not in the
#               library. The "who are you" account comes from Settings.
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

_TYPE_TO_MEDIA = {"movie": "movie", "show": "tv"}


class WatchlistTab:
    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app
        self._watchlist: list = []
        self._recs: list = []          # currently displayed (filtered)
        self._recs_all: list = []      # full result set from the last fetch
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
                                  state="readonly", values=["All", "movie", "show"])
        type_combo.pack(side=tk.LEFT, padx=(4, 12))
        type_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_recs())
        # One selector instead of two checkboxes — "In-library only" plus
        # "Not-in-library only" both ticked used to intersect to zero rows.
        ttk.Label(recs_bar, text="Show:").pack(side=tk.LEFT)
        self._libmode_var = tk.StringVar(value="Everything")
        libmode = ttk.Combobox(recs_bar, textvariable=self._libmode_var, width=14,
                               state="readonly",
                               values=["Everything", "In library", "Not in library"])
        libmode.pack(side=tk.LEFT, padx=(4, 12))
        libmode.bind("<<ComboboxSelected>>", lambda _e: self._libmode_changed())
        get_btn = ttk.Button(recs_bar, text="Get Recommendations", command=self.get_recs)
        get_btn.pack(side=tk.LEFT)
        add_tooltip(get_btn, "Analyse this user's Plex watch history, find their top "
                             "genres, and suggest unwatched titles. Untick 'In-library "
                             "only' to add popular TMDB titles you don't have yet.")

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
    _RECS_CACHE_VERSION = 1

    def _recs_cache_path(self):
        from pathlib import Path
        return Path(config.APP_DIR) / "watchlist_recs.json"

    def _load_persisted_recs(self) -> None:
        """Last fetch survives restarts — the tab is never empty if recs
        have EVER been generated. Get Recommendations refreshes. A malformed
        or pickle-era cache file is a cache miss, never an error."""
        import json_cache
        from plex_api import Recommendation
        payload = json_cache.load_json_cache(
            self._recs_cache_path(), version=self._RECS_CACHE_VERSION,
            dataclass_types=(Recommendation,))
        if not isinstance(payload, dict):
            return
        try:
            self._recs_all = payload.get("recs", [])
            self._top_genres = payload.get("genres", [])
            if self._top_genres:
                merged = self._top_genres + [g for g in _STANDARD_GENRES
                                             if g not in self._top_genres]
                self._genre_combo.configure(values=["All"] + merged)
            self._render_recs()
            self._status_var.set(
                f"Loaded last recommendations (from {payload.get('at', '?')}) — "
                "Get Recommendations refreshes.")
        except Exception:
            logger.debug("Persisted recs render failed.", exc_info=True)

    def _persist_recs(self) -> None:
        import datetime
        from pathlib import Path
        import json_cache
        json_cache.save_json_cache(
            self._recs_cache_path(),
            {"recs": self._recs_all, "genres": self._top_genres,
             "at": datetime.datetime.now().strftime("%b %d %H:%M")},
            version=self._RECS_CACHE_VERSION,
            legacy_paths=[Path(config.APP_DIR) / "watchlist_recs.pkl"])

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
                from library_index import search_library
                from plex_api import get_watchlist
                items = get_watchlist()
                in_lib: dict[str, bool] = {}
                for item in items:
                    try:
                        hits = search_library(item.title, limit=3)
                        in_lib[item.title] = any(
                            item.title.casefold() in h.name.casefold() for h in hits)
                    except Exception:
                        in_lib[item.title] = False
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
                                "✓" if in_lib.get(item.title) else ""),
                    )
                self._status_var.set(f"{len(items)} watchlist item(s)")
            self.app._post_to_ui(render)

        threading.Thread(target=worker, name="wl-pull", daemon=True).start()

    def _libmode_changed(self) -> None:
        """Re-render — and refetch when the cached recs can't satisfy the new
        mode (e.g. 'Not in library' after an in-library-only fetch, which
        used to render as a permanent 'none')."""
        mode = self._libmode_var.get()
        cached = getattr(self, "_recs_all", []) or []
        if mode == "Not in library" and not any(not r.in_library for r in cached):
            self.get_recs()
            return
        self._render_recs()

    def get_recs(self) -> None:
        user = self._user_var.get().strip()
        genre = self._genre_var.get().strip()
        self._status_var.set(f"Analysing {user or 'all users'}'s watch history…")

        def worker() -> None:
            try:
                from plex_api import get_recommendations
                top_genres, recs = get_recommendations(
                    user or None,
                    in_library_only=(self._libmode_var.get() == "In library"),
                    genre_filter=None if genre in ("", "All") else genre,
                )
            except Exception as exc:
                self.app._post_to_ui(
                    lambda: self._status_var.set(f"Recommendations unavailable: {exc}"))
                return

            def render() -> None:
                self._recs_all = recs
                if top_genres:  # keep the previous genre list on a dud fetch
                    self._top_genres = top_genres
                    merged = top_genres + [g for g in _STANDARD_GENRES
                                           if g not in top_genres]
                    self._genre_combo.configure(values=["All"] + merged)
                self._render_recs()
                self._persist_recs()
            self.app._post_to_ui(render)

        threading.Thread(target=worker, name="wl-recs", daemon=True).start()

    def _render_recs(self) -> None:
        """Re-render the recs tree applying the type / not-in-library filters
        (client-side, so toggling them doesn't refetch anything)."""
        recs = list(getattr(self, "_recs_all", []))
        type_filter = self._type_var.get()
        if type_filter != "All":
            recs = [r for r in recs if r.item_type == type_filter]
        mode = self._libmode_var.get()
        if mode == "In library":
            recs = [r for r in recs if r.in_library]
        elif mode == "Not in library":
            recs = [r for r in recs if not r.in_library]
        self._recs = recs
        for iid in self._recs_tree.get_children():
            self._recs_tree.delete(iid)
        for idx, r in enumerate(recs):
            self._recs_tree.insert(
                "", "end", iid=str(idx),
                values=(r.title, r.year or "", r.item_type, r.note,
                        "✓" if r.in_library else ""),
            )
        top_genres = getattr(self, "_top_genres", [])
        self._status_var.set(
            f"{len(recs)} recommendation(s) shown — top genres: "
            + (", ".join(top_genres[:5]) or "none found"))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _selected_watchlist_items(self) -> list:
        return [self._watchlist[int(i)] for i in self._wl_tree.selection()
                if int(i) < len(self._watchlist)]

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
        if iid not in self._recs_tree.selection():
            self._recs_tree.selection_set(iid)
        r = self._recs[int(iid)]
        menu = tk.Menu(self._recs_tree, tearoff=0)
        query = f"{r.title} {r.year}" if r.year else r.title
        menu.add_command(
            label=f"🔍 Search torrents for '{r.title}'",
            command=lambda: self.app.open_downloads_search(
                query, _TYPE_TO_MEDIA.get(r.item_type, "movie")),
        )
        menu.tk_popup(event.x_root, event.y_root)
