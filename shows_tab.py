# =============================================================================
# shows_tab.py
# =============================================================================
# The "Shows" notebook tab — Radarr/Sonarr-style tracking UI. This is the
# first tab extracted out of the DesktopApp god-class into its own module:
# it owns its widgets and talks back to the app through a narrow surface
# (_post_to_ui, _show_warning, open_downloads_search).
#
# Layout (vertical PanedWindow — every section is drag-resizable):
#   toolbar  — Scan / Sync / Refresh / Untrack / Merge / Silenced… / Fix
#              Titles + auto-grab toggle + animated progress spinner
#   upcoming — one box per air date (next 14 days); each box lists what
#              releases that day and right-click offers Silence /
#              Auto-download-on-release / Find torrent
#   shows    — inventory tree: search box + per-type filter checkboxes,
#              click a column header to sort
#   missing  — missing episodes for the selected show
# =============================================================================

import logging
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import config
import show_tracker
import shows_store
from ui_helpers import Spinner, add_tooltip, bind_smooth_vscroll, make_sortable

logger = logging.getLogger(__name__)


class ShowsTab:
    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app  # DesktopApp — narrow surface only (see module docstring)
        self._shows: list[shows_store.TrackedShow] = []
        self._missing: list[shows_store.EpisodeRow] = []
        self._status_var = tk.StringVar(value="Scan Folders to identify your show libraries.")
        self._search_var = tk.StringVar()
        # Only one scan/sync/grab may run at a time — see _run_guarded. This
        # flag is touched only on the Tk main thread, so no lock is needed.
        self._busy = False
        # (show_id, listbox-entry metadata) per upcoming box, for the
        # right-click menu.
        self._upcoming_boxes: list[tuple[tk.Listbox, list[tuple[int, str]]]] = []

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for text, command, tip in (
            ("Scan Folders", self.scan_folders,
             "Walk your tv/anime/xanime library folders and identify any new show folders against the trackers."),
            ("Sync Episodes", self.sync_all,
             "Refresh episode lists, air dates, and on-disk state for every tracked show. Can take a while."),
            ("Refresh", self.refresh, "Redraw this tab from the database (no network calls)."),
            ("Untrack", self.untrack_selected,
             "Stop tracking the selected show(s). Files on disk are never touched."),
            ("Merge Selected", self.merge_selected,
             "Ctrl-click rows that are actually the same show, then merge them into the top-most one."),
            ("Silenced…", self.open_silenced_dialog,
             "List shows whose releases you've silenced, and restore them."),
            ("Unidentified…", self.open_unidentified_dialog,
             "Folders the last scan couldn't match to any tracker — identify "
             "them by hand here instead of digging through the log."),
            ("Fix Titles (English)", self.fix_titles,
             "Rename AniDB-identified shows to their official English titles — IN THE APP "
             "ONLY, files and folders on disk are never touched (the Folders column still "
             "shows the original folder name). Offline, safe to re-run."),
            ("⬇ Grab Missing Now", self.grab_missing_now,
             "Search torrents for missing episodes of the SELECTED show(s). With nothing "
             "selected it runs the full auto-grab pass over every eligible show. Respects "
             "your size preferences."),
        ):
            btn = ttk.Button(toolbar, text=text, command=command)
            btn.pack(side=tk.LEFT, padx=(0, 6))
            add_tooltip(btn, tip)
        self._auto_grab_var = tk.BooleanVar(value=config.SHOWS_AUTO_GRAB)
        auto_cb = ttk.Checkbutton(
            toolbar, text="Auto-grab missing", variable=self._auto_grab_var,
            command=lambda: self.app._persist_dl_toggle("SHOWS_AUTO_GRAB", self._auto_grab_var),
        )
        auto_cb.pack(side=tk.LEFT, padx=(0, 6))
        add_tooltip(auto_cb, "Every 6 hours, automatically search and download missing episodes "
                             "for ALL tracked shows. Individual shows can also be marked "
                             "auto-download from the Upcoming boxes.")
        spinner_label = ttk.Label(toolbar, text="", font=("Segoe UI", 9))
        spinner_label.pack(side=tk.LEFT, padx=(10, 0))
        self._spinner = Spinner(spinner_label)
        ttk.Label(toolbar, textvariable=self._status_var,
                  font=("Segoe UI", 9, "italic")).pack(side=tk.LEFT, padx=(10, 0))

        # The whole body below the toolbar scrolls as one page, so the
        # tracked-shows and missing sections can both be tall.
        body_canvas = tk.Canvas(parent, highlightthickness=0)
        body_canvas.grid(row=1, column=0, sticky="nsew")
        body_scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=body_canvas.yview)
        body_scroll.grid(row=1, column=1, sticky="ns")
        body_canvas.configure(yscrollcommand=body_scroll.set)
        body = ttk.Frame(body_canvas)
        body_id = body_canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: body_canvas.configure(scrollregion=body_canvas.bbox("all")))
        body_canvas.bind("<Configure>",
                         lambda e: body_canvas.itemconfigure(body_id, width=e.width))
        bind_smooth_vscroll(body_canvas)
        body.columnconfigure(0, weight=1)

        # ------------------------------------------------------------------
        # Upcoming — per-date boxes on a horizontally scrollable strip
        # ------------------------------------------------------------------
        upcoming_frame = ttk.LabelFrame(body, text="Upcoming (next 14 days) — right-click an entry to silence or keep-at-100%", padding=6)
        upcoming_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        upcoming_frame.columnconfigure(0, weight=1)
        upcoming_frame.rowconfigure(0, weight=1)

        self._upcoming_canvas = tk.Canvas(upcoming_frame, height=150, highlightthickness=0)
        self._upcoming_canvas.grid(row=0, column=0, sticky="nsew")
        up_scroll = ttk.Scrollbar(upcoming_frame, orient=tk.HORIZONTAL,
                                  command=self._upcoming_canvas.xview)
        up_scroll.grid(row=1, column=0, sticky="ew")
        self._upcoming_canvas.configure(xscrollcommand=up_scroll.set)
        self._upcoming_inner = ttk.Frame(self._upcoming_canvas)
        self._upcoming_window = self._upcoming_canvas.create_window(
            (0, 0), window=self._upcoming_inner, anchor="nw")
        self._upcoming_inner.bind(
            "<Configure>",
            lambda _e: self._upcoming_canvas.configure(
                scrollregion=self._upcoming_canvas.bbox("all")),
        )

        # ------------------------------------------------------------------
        # Tracked shows — search + type filter + sortable tree
        # ------------------------------------------------------------------
        shows_frame = ttk.LabelFrame(body, text="Tracked shows", padding=6)
        shows_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        shows_frame.columnconfigure(0, weight=1)
        shows_frame.rowconfigure(1, weight=1)

        filter_row = ttk.Frame(shows_frame)
        filter_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(filter_row, text="Search:").pack(side=tk.LEFT)
        search_entry = ttk.Entry(filter_row, textvariable=self._search_var, width=28)
        search_entry.pack(side=tk.LEFT, padx=(4, 12))
        search_entry.bind("<KeyRelease>", lambda _e: self._render_shows())
        ttk.Label(filter_row, text="Type:").pack(side=tk.LEFT)
        self._type_filter_vars: dict[str, tk.BooleanVar] = {}
        for tag, label in (("tv", "TV"), ("anime", "Anime"), ("xanime", "xAnime")):
            shown = tag != "xanime" or config.XANIME_ENABLED
            var = tk.BooleanVar(value=shown)
            self._type_filter_vars[tag] = var
            if not shown:
                continue  # hentai is an opt-in pickup — chip hidden, rows filtered out
            ttk.Checkbutton(filter_row, text=label, variable=var,
                            command=self._render_shows).pack(side=tk.LEFT, padx=4)

        shows_tree_frame = ttk.Frame(shows_frame)
        shows_tree_frame.grid(row=1, column=0, sticky="nsew")
        shows_tree_frame.columnconfigure(0, weight=1)
        shows_tree_frame.rowconfigure(0, weight=1)
        shows = ttk.Treeview(
            shows_tree_frame,
            columns=("id", "title", "type", "status", "have", "missing", "misspct",
                     "next", "flags", "folders"),
            show="headings", selectmode="extended",
        )
        for col, text, width, stretch in (
            ("id", "#", 40, False), ("title", "Title", 250, True),
            ("type", "Type", 60, False), ("status", "Status", 100, False),
            ("have", "Have", 70, False), ("missing", "Missing", 60, False),
            ("misspct", "% Missing", 70, False),
            ("next", "Next air", 90, False), ("flags", "Flags", 60, False),
            ("folders", "Folders", 220, True),
        ):
            shows.heading(col, text=text)
            shows.column(col, width=width, anchor=tk.W, stretch=stretch)
        shows.grid(row=0, column=0, sticky="nsew")
        shows_scroll = ttk.Scrollbar(shows_tree_frame, orient=tk.VERTICAL, command=shows.yview)
        shows_scroll.grid(row=0, column=1, sticky="ns")
        shows.configure(yscrollcommand=shows_scroll.set)
        shows.bind("<<TreeviewSelect>>", lambda _e: self._show_selected_missing())
        shows.bind("<Button-3>", self._on_show_right_click)
        make_sortable(shows)
        self._shows_tree = shows

        # ------------------------------------------------------------------
        # Missing episodes
        # ------------------------------------------------------------------
        missing_frame = ttk.LabelFrame(body, text="Missing episodes (selected show)", padding=6)
        missing_frame.grid(row=2, column=0, sticky="ew")
        missing_frame.columnconfigure(0, weight=1)
        missing_frame.rowconfigure(1, weight=1)

        missing_bar = ttk.Frame(missing_frame)
        missing_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(missing_bar, text="Sync Selected Show",
                   command=self.sync_selected).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(missing_bar, text="Set Season Target Folder…",
                   command=self.set_season_target_for_selected).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(missing_bar, text="⬇ Find Torrent for Selected Episode",
                   command=self.find_torrent_for_selected_episode).pack(side=tk.RIGHT)

        missing_tree_frame = ttk.Frame(missing_frame)
        missing_tree_frame.grid(row=1, column=0, sticky="nsew")
        missing_tree_frame.columnconfigure(0, weight=1)
        missing_tree_frame.rowconfigure(0, weight=1)
        missing = ttk.Treeview(
            missing_tree_frame, columns=("ep", "title", "aired"),
            show="headings", selectmode="browse",
        )
        for col, text, width, stretch in (
            ("ep", "Episode", 90, False), ("title", "Title", 380, True),
            ("aired", "Aired", 100, False),
        ):
            missing.heading(col, text=text)
            missing.column(col, width=width, anchor=tk.W, stretch=stretch)
        missing.grid(row=0, column=0, sticky="nsew")
        missing_scroll = ttk.Scrollbar(missing_tree_frame, orient=tk.VERTICAL, command=missing.yview)
        missing_scroll.grid(row=0, column=1, sticky="ns")
        missing.configure(yscrollcommand=missing_scroll.set)
        make_sortable(missing)
        self._missing_tree = missing

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._shows = shows_store.list_shows()
        self._render_shows()
        self._render_upcoming()
        self._show_selected_missing()

    def _render_shows(self) -> None:
        selected = set(self._selected_show_ids())
        query = self._search_var.get().strip().casefold()
        active_types = {tag for tag, var in self._type_filter_vars.items() if var.get()}

        for item in self._shows_tree.get_children():
            self._shows_tree.delete(item)
        for s in self._shows:
            if s.media_type in self._type_filter_vars and s.media_type not in active_types:
                continue
            if query and query not in s.title.casefold():
                continue
            folders = "; ".join(s.folders) if s.folders else "—"
            flags = (("🔕" if s.silenced else "")
                     + ("✅" if s.auto_grab else "")
                     + ("🆕" if s.follow_new else "")
                     + ("📏" if s.size_mode == "match_library" else ""))
            miss_pct = (f"{s.missing_count / s.episode_count * 100:.0f}%"
                        if s.episode_count else "")
            self._shows_tree.insert(
                "", "end", iid=str(s.show_id),
                values=(s.show_id, s.title, s.media_type, s.status or "?",
                        f"{s.have_count}/{s.episode_count}", s.missing_count,
                        miss_pct, s.next_air_date or "", flags, folders),
            )
            if s.show_id in selected:
                self._shows_tree.selection_add(str(s.show_id))

    def _render_upcoming(self) -> None:
        for child in self._upcoming_inner.winfo_children():
            child.destroy()
        self._upcoming_boxes = []

        by_date: dict[str, list[tuple[shows_store.TrackedShow, shows_store.EpisodeRow]]] = {}
        for show, ep in shows_store.upcoming_episodes(days=14):
            by_date.setdefault(ep.air_date or "?", []).append((show, ep))

        if not by_date:
            ttk.Label(self._upcoming_inner,
                      text="Nothing airing in the next 14 days (or nothing synced yet).",
                      font=("Segoe UI", 9, "italic")).grid(row=0, column=0, padx=8, pady=8)
            return

        for col, date_str in enumerate(sorted(by_date)):
            box = ttk.LabelFrame(self._upcoming_inner, text=date_str, padding=4)
            box.grid(row=0, column=col, sticky="ns", padx=(0, 8), pady=(0, 4))
            entries = by_date[date_str]
            listbox = tk.Listbox(box, width=34, height=min(max(len(entries), 3), 6),
                                 activestyle="none", exportselection=False)
            listbox.grid(row=0, column=0, sticky="nsew")
            if len(entries) > 6:
                sb = ttk.Scrollbar(box, orient=tk.VERTICAL, command=listbox.yview)
                sb.grid(row=0, column=1, sticky="ns")
                listbox.configure(yscrollcommand=sb.set)
            meta: list[tuple[int, str]] = []
            for show, ep in entries:
                marker = ("✅ " if show.auto_grab else "") + ("🆕 " if show.follow_new else "")
                have = " ✓" if ep.has_file else ""
                listbox.insert(tk.END, f"{marker}{show.title}  S{ep.season:02d}E{ep.episode:02d}{have}")
                meta.append((show.show_id, f"{show.title} S{ep.season:02d}E{ep.episode:02d}"))
            listbox.bind("<Button-3>", self._on_upcoming_right_click)
            listbox.bind("<Button-1>", self._on_upcoming_left_click)
            listbox.bind("<Motion>", self._on_upcoming_motion)
            listbox.bind("<Leave>", lambda _e: self._hide_upcoming_tip())
            self._upcoming_boxes.append((listbox, meta))
        if hasattr(self.app, "_apply_dark_widget_styles"):
            self.app._apply_dark_widget_styles(self._upcoming_inner)

    def _on_upcoming_motion(self, event) -> None:
        """Hover tooltip with the FULL entry text when it's clipped."""
        listbox = event.widget
        index = listbox.nearest(event.y)
        if index < 0 or index >= listbox.size():
            self._hide_upcoming_tip()
            return
        text = listbox.get(index)
        # Only tip when the text is actually wider than the box.
        try:
            import tkinter.font as tkfont
            width_px = tkfont.nametofont("TkDefaultFont").measure(text)
        except Exception:
            width_px = len(text) * 8
        if width_px <= listbox.winfo_width() - 8:
            self._hide_upcoming_tip()
            return
        tip = getattr(self, "_upcoming_tip", None)
        if tip is not None and getattr(self, "_upcoming_tip_text", "") == text:
            return
        self._hide_upcoming_tip()
        tip = tk.Toplevel(listbox)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{event.x_root + 14}+{event.y_root + 12}")
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, bg="#2b2b2b", fg="#e8e8e8",
                 relief=tk.SOLID, borderwidth=1, font=("Segoe UI", 9),
                 padx=6, pady=3).pack()
        self._upcoming_tip = tip
        self._upcoming_tip_text = text

    def _hide_upcoming_tip(self) -> None:
        tip = getattr(self, "_upcoming_tip", None)
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass
        self._upcoming_tip = None
        self._upcoming_tip_text = ""

    def _on_upcoming_left_click(self, event) -> None:
        """Clicking an already-selected upcoming entry unselects it."""
        listbox = event.widget
        index = listbox.nearest(event.y)
        if index >= 0 and index in listbox.curselection():
            # The class binding re-selects after us — clear just after it runs.
            listbox.after(1, lambda: listbox.selection_clear(index))

    def _on_upcoming_right_click(self, event) -> None:
        listbox = event.widget
        meta = next((m for lb, m in self._upcoming_boxes if lb is listbox), None)
        if meta is None:
            return
        index = listbox.nearest(event.y)
        if index < 0 or index >= len(meta):
            return
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(index)
        show_id, query = meta[index]
        show = shows_store.get_show(show_id)
        if show is None:
            return

        menu = tk.Menu(listbox, tearoff=0)
        menu.add_command(
            label=f"🔕 Silence releases of '{show.title}'",
            command=lambda: (shows_store.set_show_silenced(show_id, True), self.refresh()),
        )
        menu.add_command(
            label=("Stop keeping at 100%" if show.auto_grab
                   else "✅ Keep show at 100% (finish ALL missing + grab new releases)"),
            command=lambda: (shows_store.set_show_auto_grab(show_id, not show.auto_grab),
                             self.refresh()),
        )
        menu.add_command(
            label=("Stop following new releases" if show.follow_new
                   else "🆕 Follow new releases (ONLY episodes airing from today)"),
            command=lambda: (shows_store.set_show_follow_new(show_id, not show.follow_new),
                             self.refresh()),
        )
        menu.add_command(
            label=("Size: use global preferences" if show.size_mode == "match_library"
                   else "📏 Size: match existing episodes"),
            command=lambda: (shows_store.set_show_size_mode(
                show_id,
                "global" if show.size_mode == "match_library" else "match_library"),
                self.refresh()),
        )
        menu.add_separator()
        menu.add_command(
            label="Find torrent now…",
            command=lambda: self.app.open_downloads_search(query, show.media_type),
        )
        menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------
    # Tracked-shows right-click menu
    # ------------------------------------------------------------------

    def _on_show_right_click(self, event) -> None:
        iid = self._shows_tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self._shows_tree.selection():
            self._shows_tree.selection_set(iid)
        show = shows_store.get_show(int(iid))
        if show is None:
            return

        menu = tk.Menu(self._shows_tree, tearoff=0)
        menu.add_command(label="🔧 Fix match…",
                         command=lambda: self.open_fix_match_dialog(show.show_id))
        if show.external_url:
            import webbrowser
            menu.add_command(label="🌐 Open DB link",
                             command=lambda u=show.external_url: webbrowser.open_new_tab(u))
        menu.add_separator()
        menu.add_command(
            label=("🔔 Unsilence releases" if show.silenced else "🔕 Silence releases"),
            command=lambda: (shows_store.set_show_silenced(show.show_id, not show.silenced),
                             self.refresh()),
        )
        menu.add_command(
            label=("Stop following new releases" if show.follow_new
                   else "🆕 Follow new releases (auto-grab this + future episodes)"),
            command=lambda: (shows_store.set_show_follow_new(show.show_id, not show.follow_new),
                             self.refresh()),
        )
        menu.add_command(
            label=("Stop keeping at 100%" if show.auto_grab
                   else "✅ Keep show at 100% (finish ALL missing + grab new releases)"),
            command=lambda: (shows_store.set_show_auto_grab(show.show_id, not show.auto_grab),
                             self.refresh()),
        )
        menu.add_command(
            label=("Size: use global preferences" if show.size_mode == "match_library"
                   else "📏 Size: match existing episodes"),
            command=lambda: (shows_store.set_show_size_mode(
                show.show_id,
                "global" if show.size_mode == "match_library" else "match_library"),
                self.refresh()),
        )
        menu.add_separator()
        menu.add_command(label="🧹 Send files to Sanitize (Maintenance tab)",
                         command=lambda: self.app.open_sanitize_for_show(show))
        menu.add_command(label="Sync this show",
                         command=self.sync_selected)
        menu.add_command(label="Untrack", command=self.untrack_selected)
        menu.tk_popup(event.x_root, event.y_root)

    def open_fix_match_dialog(self, show_id: int) -> None:
        """Re-identify a mismatched show against the trackers by hand."""
        show = shows_store.get_show(show_id)
        if show is None:
            return

        win = tk.Toplevel(self._shows_tree)
        win.title(f"Fix match — {show.title}")
        win.geometry("640x420")
        win.transient(self._shows_tree.winfo_toplevel())
        win.columnconfigure(0, weight=1)
        win.rowconfigure(2, weight=1)

        folder_hint = Path(show.folders[0]).name if show.folders else show.title
        ttk.Label(win, text=(f"Currently matched to: {show.title} "
                             f"({show.source}:{show.external_id})\n"
                             f"Folder: {folder_hint}"),
                  justify=tk.LEFT).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))

        bar = ttk.Frame(win)
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        bar.columnconfigure(0, weight=1)
        query_var = tk.StringVar(value=show_tracker.clean_show_folder_name(folder_hint)[0]
                                 or show.title)
        entry = ttk.Entry(bar, textvariable=query_var)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        status_var = tk.StringVar(value="Search the trackers for the correct match.")

        results_tree = ttk.Treeview(
            win, columns=("title", "year", "source", "id"),
            show="headings", selectmode="browse",
        )
        for col, text, width in (("title", "Title", 330), ("year", "Year", 60),
                                 ("source", "Source", 80), ("id", "ID", 90)):
            results_tree.heading(col, text=text)
            results_tree.column(col, width=width, anchor=tk.W)
        results_tree.grid(row=2, column=0, sticky="nsew", padx=10)
        candidates: list = []

        def do_search() -> None:
            query = query_var.get().strip()
            if not query:
                return
            status_var.set("Searching trackers…")

            def worker() -> None:
                from media_lookup import (search_anidb, search_anilist,
                                          search_tmdb_shows, search_tvdb_shows)
                found = []
                try:
                    if show.media_type == "tv":
                        found = search_tvdb_shows(query, None) + search_tmdb_shows(query, None)
                    else:
                        found = show_tracker.anime_db_search_results(
                            query, show.media_type,
                        ) + search_anidb(
                            query, media_type=show.media_type,
                        ) + search_anilist(query, explicit=(show.media_type == "xanime"))
                except Exception as exc:
                    self.app._post_to_ui(lambda: status_var.set(f"Search failed: {exc}"))
                    return
                self.app._post_to_ui(lambda: render(found))

            threading.Thread(target=worker, name="fix-match-search", daemon=True).start()

        def render(found) -> None:
            candidates.clear()
            candidates.extend(found)
            for item in results_tree.get_children():
                results_tree.delete(item)
            for idx, r in enumerate(found):
                results_tree.insert("", "end", iid=str(idx),
                                    values=(r.title, r.year or "", r.source, r.external_id))
            status_var.set(f"{len(found)} candidate(s) — pick one and click Use Selected.")

        def use_selected() -> None:
            sel = results_tree.selection()
            if not sel:
                return
            r = candidates[int(sel[0])]
            outcome, final_id = shows_store.reidentify_show(
                show.show_id, title=r.title, source=r.source,
                external_id=r.external_id, external_url=r.external_url or None,
                year=r.year,
            )
            win.destroy()
            note = ("merged into the existing tracked row"
                    if outcome == "merged" else f"re-matched to '{r.title}'")
            self._status_var.set(f"{show.title}: {note} — syncing…")
            self.refresh()
            self._run_guarded("Sync show", "shows-fix-match-sync", "Syncing new match…",
                              lambda: show_tracker.sync_show(final_id), lambda msg: msg)

        ttk.Button(bar, text="Search", command=do_search).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(bar, text="Use Selected", style="Accent.TButton",
                   command=use_selected).grid(row=0, column=2)
        entry.bind("<Return>", lambda _e: do_search())
        ttk.Label(win, textvariable=status_var,
                  font=("Segoe UI", 9, "italic")).grid(row=3, column=0, sticky="w",
                                                       padx=10, pady=(4, 10))
        if hasattr(self.app, "_apply_dark_widget_styles"):
            self.app._apply_dark_widget_styles(win)
        do_search()

    def open_unidentified_dialog(self) -> None:
        """Folders the scanner gave up on → manual identification."""
        folders = show_tracker.load_unidentified()
        win = tk.Toplevel(self._shows_tree)
        win.title("Unidentified folders")
        win.geometry("720x420")
        win.transient(self._shows_tree.winfo_toplevel())
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)

        ttk.Label(win, wraplength=680, justify=tk.LEFT, text=(
            f"{len(folders)} folder(s) from the last scan couldn't be matched "
            "automatically (junk-heavy names, obscure titles, or non-show "
            "folders). Select one and click Identify to search the trackers "
            "yourself — or leave it; non-show folders are fine to ignore."
        )).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 6))

        listbox = tk.Listbox(win, activestyle="none")
        listbox.grid(row=1, column=0, sticky="nsew", padx=10)
        for f in folders:
            listbox.insert(tk.END, f)
        if not folders:
            listbox.insert(tk.END, "Nothing unidentified — run Scan Folders to refresh.")

        def identify() -> None:
            sel = listbox.curselection()
            if not sel or not folders:
                return
            folder = folders[sel[0]]
            win.destroy()
            self.open_identify_dialog(folder)

        bar = ttk.Frame(win)
        bar.grid(row=2, column=0, sticky="e", padx=10, pady=(6, 10))
        ttk.Button(bar, text="Identify…", style="Accent.TButton",
                   command=identify).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side=tk.RIGHT)
        if hasattr(self.app, "_apply_dark_widget_styles"):
            self.app._apply_dark_widget_styles(win)

    def open_identify_dialog(self, folder: str) -> None:
        """Search the trackers for one unidentified folder and track it."""
        folder_path = Path(folder)
        media_type = "anime"
        for entry in config.MEDIA_LIBRARY_PATHS:
            try:
                folder_path.relative_to(entry.path)
                if entry.media_type in ("tv", "anime", "xanime"):
                    media_type = entry.media_type
                break
            except ValueError:
                continue

        win = tk.Toplevel(self._shows_tree)
        win.title(f"Identify — {folder_path.name}")
        win.geometry("640x420")
        win.transient(self._shows_tree.winfo_toplevel())
        win.columnconfigure(0, weight=1)
        win.rowconfigure(2, weight=1)

        ttk.Label(win, text=f"Folder: {folder}\nSearching as: {media_type}",
                  justify=tk.LEFT).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))

        bar = ttk.Frame(win)
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        bar.columnconfigure(0, weight=1)
        query_var = tk.StringVar(
            value=show_tracker.clean_show_folder_name(folder_path.name)[0]
            or folder_path.name)
        entry = ttk.Entry(bar, textvariable=query_var)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        status_var = tk.StringVar(value="Adjust the query and Search.")

        results_tree = ttk.Treeview(
            win, columns=("title", "year", "source", "id"),
            show="headings", selectmode="browse",
        )
        for col, text, width in (("title", "Title", 330), ("year", "Year", 60),
                                 ("source", "Source", 80), ("id", "ID", 90)):
            results_tree.heading(col, text=text)
            results_tree.column(col, width=width, anchor=tk.W)
        results_tree.grid(row=2, column=0, sticky="nsew", padx=10)
        candidates: list = []

        def do_search() -> None:
            query = query_var.get().strip()
            if not query:
                return
            status_var.set("Searching trackers…")

            def worker() -> None:
                from media_lookup import (search_anidb, search_anilist,
                                          search_tmdb_shows, search_tvdb_shows)
                try:
                    if media_type == "tv":
                        found = search_tvdb_shows(query, None) + search_tmdb_shows(query, None)
                    else:
                        found = show_tracker.anime_db_search_results(
                            query, media_type,
                        ) + search_anidb(
                            query, media_type=media_type,
                        ) + search_anilist(query, explicit=(media_type == "xanime"))
                except Exception as exc:
                    self.app._post_to_ui(lambda: status_var.set(f"Search failed: {exc}"))
                    return

                def render() -> None:
                    candidates.clear()
                    candidates.extend(found)
                    for item in results_tree.get_children():
                        results_tree.delete(item)
                    for idx, r in enumerate(found):
                        results_tree.insert("", "end", iid=str(idx),
                                            values=(r.title, r.year or "", r.source,
                                                    r.external_id))
                    status_var.set(f"{len(found)} candidate(s).")
                self.app._post_to_ui(render)

            threading.Thread(target=worker, name="identify-search", daemon=True).start()

        def track_selected() -> None:
            sel = results_tree.selection()
            if not sel:
                return
            r = candidates[int(sel[0])]
            show_id = shows_store.upsert_show(
                title=r.title, media_type=media_type, source=r.source,
                external_id=r.external_id, external_url=r.external_url or None,
                year=r.year,
            )
            shows_store.add_show_folder(show_id, folder)
            win.destroy()
            self._status_var.set(f"Tracked '{r.title}' ← {folder_path.name} — syncing…")
            self.refresh()
            self._run_guarded("Sync show", "identify-sync", "Syncing new show…",
                              lambda: show_tracker.sync_show(show_id), lambda msg: msg)

        ttk.Button(bar, text="Search", command=do_search).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(bar, text="Track Selected Match", style="Accent.TButton",
                   command=track_selected).grid(row=0, column=2)
        entry.bind("<Return>", lambda _e: do_search())
        ttk.Label(win, textvariable=status_var,
                  font=("Segoe UI", 9, "italic")).grid(row=3, column=0, sticky="w",
                                                       padx=10, pady=(4, 10))
        if hasattr(self.app, "_apply_dark_widget_styles"):
            self.app._apply_dark_widget_styles(win)
        do_search()

    def open_silenced_dialog(self) -> None:
        """List silenced shows with a Restore button each."""
        silenced = [s for s in shows_store.list_shows() if s.silenced]
        win = tk.Toplevel(self._shows_tree)
        win.title("Silenced shows")
        win.geometry("420x360")
        win.transient(self._shows_tree.winfo_toplevel())
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        listbox = tk.Listbox(win, activestyle="none")
        listbox.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        for s in silenced:
            listbox.insert(tk.END, f"{s.title}  ({s.media_type})")
        if not silenced:
            listbox.insert(tk.END, "No silenced shows.")

        def restore() -> None:
            sel = listbox.curselection()
            if not sel or not silenced:
                return
            idx = sel[0]
            if idx >= len(silenced):
                return
            shows_store.set_show_silenced(silenced[idx].show_id, False)
            listbox.delete(idx)
            del silenced[idx]
            self.refresh()

        bar = ttk.Frame(win)
        bar.grid(row=1, column=0, sticky="e", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Restore Selected", command=restore).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side=tk.RIGHT)
        if hasattr(self.app, "_apply_dark_widget_styles"):
            self.app._apply_dark_widget_styles(win)

    def _selected_show_ids(self) -> list[int]:
        return [int(iid) for iid in self._shows_tree.selection()]

    def _selected_show_id(self) -> int | None:
        ids = self._selected_show_ids()
        return ids[0] if ids else None

    def _show_selected_missing(self) -> None:
        for item in self._missing_tree.get_children():
            self._missing_tree.delete(item)
        show_id = self._selected_show_id()
        self._missing = []
        if show_id is None:
            return
        self._missing = shows_store.missing_episodes(show_id)
        for idx, ep in enumerate(self._missing):
            self._missing_tree.insert(
                "", "end", iid=str(idx),
                values=(f"S{ep.season:02d}E{ep.episode:02d}", ep.title, ep.air_date or ""),
            )

    # ------------------------------------------------------------------
    # Actions (workers post back via app._post_to_ui)
    # ------------------------------------------------------------------

    def _run_guarded(self, name: str, thread_name: str, running_msg: str,
                     work, describe) -> None:
        """Run a scan/sync/grab in a worker thread, at most one at a time.

        Blocks a second click while one is in flight (the reported bug: 4
        clicks = 4 concurrent scans hammering Jikan). describe(result) -> str
        builds the done message; ShowsBusyError (from the module-level guard,
        e.g. the scheduler beat us to it) is reported plainly, not as a crash.
        """
        if self._busy:
            self._status_var.set(f"Already running — '{name}' skipped until it finishes.")
            return
        self._busy = True
        self._status_var.set("")
        self._spinner.start(running_msg)

        def worker() -> None:
            try:
                msg = describe(work())
            except show_tracker.ShowsBusyError as exc:
                msg = str(exc)
            except Exception as exc:
                logger.exception("%s failed.", name)
                msg = f"{name} failed: {exc}"
            self.app._post_to_ui(lambda: self._finish_operation(msg))

        threading.Thread(target=worker, name=thread_name, daemon=True).start()

    def _finish_operation(self, msg: str) -> None:
        self._busy = False
        self._spinner.stop()
        self._status_var.set(msg)
        self.refresh()

    def scan_folders(self) -> None:
        def describe(result) -> str:
            msg = (f"Identified {result.identified} new show(s); "
                   f"{result.already_tracked} already tracked; "
                   f"{len(result.unidentified)} unidentified")
            return msg + (" (see log for folder names)" if result.unidentified else "")

        self._run_guarded(
            "Scan Folders", "shows-scan",
            "Scanning library folders and identifying shows…",
            show_tracker.scan_library_folders, describe,
        )

    def sync_all(self) -> None:
        started_at = time.time()

        def progress(done: int, total: int, title: str) -> None:
            # Called from the worker thread — estimate remaining time from
            # the average per-show pace so far, then hop to the UI thread.
            if done > 0:
                per_show = (time.time() - started_at) / done
                remaining = int(per_show * (total - done))
                eta = f" — ~{remaining // 60}m {remaining % 60:02d}s left"
            else:
                eta = ""
            text = f"Syncing {done + 1}/{total}: {title[:40]}{eta}"
            self.app._post_to_ui(lambda: self._spinner.update_text(text))

        self._run_guarded(
            "Sync Episodes", "shows-sync",
            "Syncing episode lists for all tracked shows…",
            lambda: show_tracker.sync_all(progress=progress),
            lambda summaries: f"Synced {len(summaries)} show(s)",
        )

    def sync_selected(self) -> None:
        show_id = self._selected_show_id()
        if show_id is None:
            self.app._show_warning("No show selected", "Select a show first.")
            return
        self._run_guarded(
            "Sync show", "shows-sync-one", "Syncing…",
            lambda: show_tracker.sync_show(show_id), lambda msg: msg,
        )

    def fix_titles(self) -> None:
        """Rename AniDB-identified shows to their official English titles."""
        self._run_guarded(
            "Fix Titles", "shows-fix-titles",
            "Looking up English titles in the AniDB dump…",
            show_tracker.backfill_english_titles,
            lambda n: f"Renamed {n} show(s) to their English title",
        )

    def untrack_selected(self) -> None:
        ids = self._selected_show_ids()
        if not ids:
            self.app._show_warning("No show selected", "Select a show first.")
            return
        if not self.app._ask_yes_no(
            "Untrack show",
            f"Stop tracking {len(ids)} show(s)? (No files are touched — this "
            "only removes them from the tracker.)",
        ):
            return
        for show_id in ids:
            shows_store.remove_show(show_id)
        self.refresh()

    def merge_selected(self) -> None:
        """Merge duplicate rows: Ctrl-click the rows that are the same show."""
        ids = self._selected_show_ids()
        if len(ids) < 2:
            self.app._show_warning(
                "Merge shows",
                "Ctrl-click two or more rows that are actually the same show, "
                "then click Merge Selected. The FIRST row (top-most) is kept.",
            )
            return
        by_id = {s.show_id: s for s in self._shows}
        titles = [by_id[i].title for i in ids if i in by_id]
        primary = ids[0]
        if not self.app._ask_yes_no(
            "Merge shows",
            "Merge these into one tracked show?\n\n  • " + "\n  • ".join(titles)
            + f"\n\nKeeping: {by_id[primary].title if primary in by_id else primary}. "
            "Folders and on-disk episodes move to it; the other rows are removed.",
        ):
            return
        merged = shows_store.merge_shows(primary, ids[1:])
        self._status_var.set(f"Merged {merged} duplicate(s) into '{by_id[primary].title}'")
        self.refresh()

    def grab_missing_now(self) -> None:
        """Grab missing episodes for the selected show(s) — or run the full
        auto-grab pass when nothing is selected (rename+move forced on)."""
        selected = self._selected_show_ids() or None
        label = (f"Searching torrents for missing episodes of {len(selected)} "
                 "selected show(s)…" if selected
                 else "Searching torrents for missing episodes (all eligible shows)…")
        self._run_guarded(
            "Grab Missing", "shows-grab-missing", label,
            lambda: self.app.download_manager.auto_grab_missing_episodes(
                show_ids=selected),
            lambda started: (
                f"Started {len(started)} download(s) — see the Downloads tab"
                if started else "No grabbable missing episodes found this pass"
            ),
        )

    def set_season_target_for_selected(self) -> None:
        """Pin a season of the selected show to an explicit folder (rule the
        torrent pipeline routes into — including a folder on another drive)."""
        show_id = self._selected_show_id()
        if show_id is None:
            self.app._show_warning("No show selected", "Select a show first.")
            return
        sel = self._missing_tree.selection()
        if sel:
            try:
                season = self._missing[int(sel[0])].season
            except (ValueError, IndexError):
                season = None
        else:
            season = None
        if season is None:
            # No missing episode selected — ask which season the rule is for.
            from tkinter import simpledialog
            season = simpledialog.askinteger(
                "Season target", "Season number for the target-folder rule:",
                minvalue=0, maxvalue=99,
            )
            if season is None:
                return

        current = shows_store.get_season_target(show_id, season)
        path = filedialog.askdirectory(
            title=f"Target folder for Season {season}",
            initialdir=current or None,
        )
        if not path:
            return
        shows_store.set_season_target(show_id, season, path)
        show = shows_store.get_show(show_id)
        self._status_var.set(
            f"Season {season} of '{show.title if show else show_id}' now routes to {path}"
        )

    def find_torrent_for_selected_episode(self) -> None:
        show_id = self._selected_show_id()
        sel = self._missing_tree.selection()
        if show_id is None or not sel:
            self.app._show_warning(
                "No episode selected",
                "Select a show, then one of its missing episodes.",
            )
            return
        show = shows_store.get_show(show_id)
        try:
            ep = self._missing[int(sel[0])]
        except (ValueError, IndexError):
            return
        if show is None:
            return
        query = f"{show.title} S{ep.season:02d}E{ep.episode:02d}"
        self.app.open_downloads_search(
            query, show.media_type,
            episode_context=(show.show_id, ep.season, ep.episode),
        )
