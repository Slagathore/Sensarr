# =============================================================================
# grab_queue_tab.py  —  the Requests tab's "Grab queue" subtab (Task E)
# =============================================================================
# Thin Tk rendering over grab_queue.py's store-level view model, following the
# ShowsTab extraction pattern: this class owns its widgets and talks back to
# the DesktopApp through a narrow surface (_post_to_ui, _show_warning,
# _ask_yes_no, download_manager, resolve_request_by_id).
#
# Everything decision-shaped lives in grab_queue.py / downloads_store and is
# pytest-covered; this file is Tk glue and is MANUAL-VERIFIED per the house
# rule (stated in the Phase 6 build report).
# =============================================================================

import json
import logging
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

import downloads_store
import grab_queue
from ui_helpers import (add_tooltip, begin_busy, local_ts, make_sortable,
                        mirror_ellipsized)

logger = logging.getLogger(__name__)

_TYPE_LABEL = {
    grab_queue.ROW_REQUEST: "Request",
    grab_queue.ROW_NEEDS_IDENTITY: "Needs identity",
    grab_queue.ROW_NEEDS_ATTENTION: "Needs attention",
    grab_queue.ROW_KEEP_AT_100: "Keep at 100%",
    grab_queue.ROW_FOLLOW_NEW: "Follow new",
    grab_queue.ROW_ACTIVE_DOWNLOAD: "Download",
    grab_queue.ROW_NEEDS_PLACEMENT: "Needs placement",
    grab_queue.ROW_BLOCKLIST: "Blocklist",
}


class GrabQueueTab:
    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app
        self._rows: list[grab_queue.GrabQueueRow] = []
        self._status_var = tk.StringVar(value="Refresh to load the grab queue.")

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        refresh_btn = ttk.Button(toolbar, text="Refresh", command=self.refresh)
        refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
        add_tooltip(refresh_btn,
                    "Rebuild the queue from the database: requests, deferrals, "
                    "keep-at-100 gaps, followed shows, active downloads, "
                    "needs-placement, and the scoped blocklist.")
        # Task L item 4: the uniform busy affordance (ui_helpers.begin_busy).
        # This subtab has no per-action toolbar button — grab/place/grab-
        # missing all come off the right-click menu, which is gone before
        # its command even runs — so Refresh is the one persistent button
        # available; disabling it stops a refresh from racing a still-
        # running grab/placement and repainting the tree out from under it.
        self._refresh_btn = refresh_btn

        # Task L item 3: full-width row under the toolbar, ellipsized with a
        # tooltip for the untruncated text — replaces the old toolbar-packed
        # far-right status label.
        status_row = ttk.Frame(parent)
        status_row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        status_row.columnconfigure(0, weight=1)
        self._status_display_var = mirror_ellipsized(self._status_var, 130)
        status_label = ttk.Label(status_row, textvariable=self._status_display_var,
                                 anchor="w", font=("Segoe UI", 9, "italic"))
        status_label.grid(row=0, column=0, sticky="ew")
        add_tooltip(status_label, self._status_var.get)

        panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        panes.grid(row=2, column=0, sticky="nsew")

        tree_frame = ttk.Frame(panes)
        panes.add(tree_frame, weight=3)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            tree_frame,
            columns=("type", "title", "state", "reason", "next"),
            show="headings", selectmode="browse")
        for col, text, width, stretch in (
            ("type", "Type", 110, False), ("title", "Title", 380, True),
            ("state", "State", 110, False), ("reason", "Reason", 300, True),
            ("next", "Next attempt", 130, False),
        ):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor=tk.W, stretch=stretch)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        tree.bind("<<TreeviewSelect>>", lambda _e: self._render_detail())
        tree.bind("<Button-3>", self._on_right_click)
        make_sortable(tree)
        self._tree = tree

        detail_frame = ttk.LabelFrame(
            panes, text="Decision detail — what was chosen, why it won, what "
                        "every loser failed on", padding=4)
        panes.add(detail_frame, weight=2)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(0, weight=1)
        self._detail = scrolledtext.ScrolledText(
            detail_frame, wrap=tk.WORD, height=8, font=("Consolas", 9),
            state=tk.DISABLED)
        self._detail.grid(row=0, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        try:
            self._rows = grab_queue.list_grab_queue_rows()
        except Exception:
            logger.exception("Grab queue refresh failed.")
            msg = "Refresh failed, see the log."
            self._status_var.set(msg)
            self.app.post_activity("Grab queue", msg, level="error", tab="Requests")
            return
        for item in self._tree.get_children():
            self._tree.delete(item)
        for idx, row in enumerate(self._rows):
            self._tree.insert(
                "", "end", iid=str(idx),
                values=(_TYPE_LABEL.get(row.row_type, row.row_type),
                        row.display_title, row.state, row.reason or "",
                        (local_ts(row.next_attempt_at)
                         if row.next_attempt_at else "")))
        self._status_var.set(f"{len(self._rows)} row(s).")

    def _selected_row(self) -> grab_queue.GrabQueueRow | None:
        sel = self._tree.selection()
        if not sel:
            return None
        try:
            return self._rows[int(sel[0])]
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Detail pane
    # ------------------------------------------------------------------

    def _render_detail(self) -> None:
        row = self._selected_row()
        text = ""
        if row is not None:
            lines = [f"{_TYPE_LABEL.get(row.row_type, row.row_type)}: "
                     f"{row.display_title}",
                     f"State: {row.state}"]
            if row.subject_key:
                lines.append(f"Subject: {row.subject_key}")
            if row.reason:
                lines.append(f"Reason: {row.reason}")
            if row.next_attempt_at:
                lines.append(f"Next attempt: {row.next_attempt_at}")
            for key, value in (row.detail or {}).items():
                if value in (None, "", [], {}):
                    continue
                lines.append(f"{key}: {value}")
            if row.selection_run_id is not None:
                lines.append("")
                lines.append(self._decision_text(row.selection_run_id))
            text = "\n".join(lines)
        self._detail.configure(state=tk.NORMAL)
        self._detail.delete("1.0", tk.END)
        self._detail.insert("1.0", text)
        self._detail.configure(state=tk.DISABLED)

    @staticmethod
    def _decision_text(selection_run_id: int) -> str:
        detail = grab_queue.decision_detail(selection_run_id)
        if detail is None:
            return f"(selection run #{selection_run_id} not found)"
        run = detail["run"]
        lines = [f"— Selection run #{run.selection_run_id} "
                 f"({run.mode}, {run.profile}, RTN {run.rtn_version}, "
                 f"{run.created_at}) —"]
        if run.chosen_title:
            lines.append(f"CHOSEN: {run.chosen_title}")
            if run.chosen_infohash:
                lines.append(f"        {run.chosen_infohash}")
        else:
            lines.append(f"NOTHING CHOSEN: {run.reason or '(no reason)'}")
        hist = detail["verdict_histogram"]
        if hist:
            parts = ", ".join(f"{count} {code}" for code, count
                              in sorted(hist.items(), key=lambda kv: -kv[1]))
            lines.append(f"Pool verdicts: {parts}")
        stats = detail["pool_stats"]
        if stats:
            lines.append(f"Pool stats: {json.dumps(stats)}")
        candidates = detail["candidates"]
        if candidates:
            lines.append("")
            for c in candidates[:25]:
                if c.passed and c.score_total is not None:
                    lines.append(
                        f"  #{c.rank_position or '-'} {c.title}  "
                        f"score {c.score_total} "
                        f"{c.score_components_json or ''}")
                else:
                    lines.append(f"  ✗ {c.title}  [{c.reason_code}] "
                                 f"{c.detail or ''}")
            if len(candidates) > 25:
                lines.append(f"  … and {len(candidates) - 25} more")
        if detail["details_pruned"]:
            lines.append("(per-loser detail rows pruned — the receipt and "
                         "histogram above are kept forever)")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Right-click actions (thin glue over grab_queue / DownloadManager)
    # ------------------------------------------------------------------

    def _on_right_click(self, event) -> None:
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        self._tree.selection_set(iid)
        row = self._selected_row()
        if row is None:
            return

        menu = tk.Menu(self._tree, tearoff=0)
        rt = row.row_type
        if rt in (grab_queue.ROW_REQUEST, grab_queue.ROW_NEEDS_ATTENTION):
            menu.add_command(label="⬇ Grab now",
                             command=lambda: self._grab_now(row))
            menu.add_command(label="⏸ Defer 24 h",
                             command=lambda: self._defer(row))
        if rt == grab_queue.ROW_NEEDS_IDENTITY:
            menu.add_command(label="🆔 Resolve identity…",
                             command=lambda: self._resolve(row))
        if rt in (grab_queue.ROW_NEEDS_ATTENTION,
                  grab_queue.ROW_ACTIVE_DOWNLOAD):
            menu.add_command(label="🚫 Wrong grab — block + reopen…",
                             command=lambda: self._wrong_grab(row))
        if rt == grab_queue.ROW_NEEDS_PLACEMENT:
            menu.add_command(label="📁 Create folder and place",
                             command=lambda: self._create_placement(row))
        if rt in (grab_queue.ROW_KEEP_AT_100, grab_queue.ROW_FOLLOW_NEW):
            menu.add_command(label="⬇ Grab missing now",
                             command=lambda: self._grab_missing(row))
        if rt == grab_queue.ROW_BLOCKLIST:
            menu.add_command(label="Remove blocklist entry",
                             command=lambda: self._unblock(row))
        if row.selection_run_id is not None:
            menu.add_separator()
            menu.add_command(label="Open decision detail",
                             command=self._render_detail)
            menu.add_command(
                label="Keep details forever (exempt from prune)",
                command=lambda: self._keep_details(row))
            menu.add_command(label="Export decision JSON…",
                             command=lambda: self._export_decision(row))
        menu.tk_popup(event.x_root, event.y_root)

    def _grab_now(self, row) -> None:
        if row.request_id is None:
            return
        grab_queue.grab_now_request(row.request_id)
        working_msg = f"Request #{row.request_id} reopened, grabbing…"
        self._status_var.set(working_msg)
        self.app.post_activity("Grab queue", working_msg, level="working", tab="Requests")
        restore = begin_busy(self._refresh_btn, working_text="Grabbing…")

        def worker() -> None:
            try:
                started = self.app.download_manager.auto_grab_open_requests()
                msg = (f"Started {len(started)} download(s)."
                       if started else "No acceptable result this pass.")
                level = "success" if started else "warning"
            except Exception as exc:
                logger.exception("Grab-now pass failed.")
                msg = f"Grab failed: {exc}"
                level = "error"

            def done() -> None:
                restore()
                self._status_var.set(msg)
                self.app.post_activity("Grab queue", msg, level=level, tab="Requests")
                self.refresh()
            self.app._post_to_ui(done)
        threading.Thread(target=worker, name="grabqueue-grab-now",
                         daemon=True).start()

    def _defer(self, row) -> None:
        if row.request_id is None:
            return
        grab_queue.defer_request(row.request_id)
        msg = f"Request #{row.request_id} deferred 24 h."
        self._status_var.set(msg)
        self.app.post_activity("Grab queue", msg, level="success", tab="Requests")
        self.refresh()

    def _resolve(self, row) -> None:
        if row.request_id is None:
            return
        self.app.resolve_request_by_id(row.request_id)
        self.refresh()

    def _wrong_grab(self, row) -> None:
        download_id = row.download_id
        if download_id is None and row.request_id is not None:
            dls = downloads_store.downloads_for_request(row.request_id)
            download_id = dls[-1].download_id if dls else None
        if download_id is None:
            self.app._show_warning("No download",
                                   "No download is linked to this row.")
            return
        if not self.app._ask_yes_no(
                "Wrong grab",
                "Block this release for its identity, reopen the request, and "
                "QUARANTINE the files (reversible)?"):
            return
        recycle = self.app._ask_yes_no(
            "Recycle now?",
            "Also send the staged files to the recycle bin now?\n\n"
            "'No' keeps them quarantined in staging (default — reversible).")
        outcome = self.app.download_manager.mark_wrong_grab(
            download_id, recycle=recycle)
        self._status_var.set(outcome)
        self.app.post_activity("Grab queue", outcome, level="warning", tab="Requests")
        self.refresh()

    def _create_placement(self, row) -> None:
        if row.download_id is None:
            return
        working_msg = "Creating folder and placing…"
        self._status_var.set(working_msg)
        self.app.post_activity("Grab queue", working_msg, level="working", tab="Requests")
        restore = begin_busy(self._refresh_btn, working_text="Placing…")

        def worker() -> None:
            try:
                msg = self.app.download_manager.create_placement_folder(
                    row.download_id)
                level = "success"
            except Exception as exc:
                logger.exception("Placement failed.")
                msg = f"Placement failed: {exc}"
                level = "error"

            def done() -> None:
                restore()
                self._status_var.set(msg)
                self.app.post_activity("Grab queue", msg, level=level, tab="Requests")
                self.refresh()
            self.app._post_to_ui(done)
        threading.Thread(target=worker, name="grabqueue-place",
                         daemon=True).start()

    def _grab_missing(self, row) -> None:
        if row.show_id is None:
            return
        working_msg = "Searching missing episodes…"
        self._status_var.set(working_msg)
        self.app.post_activity("Grab queue", working_msg, level="working", tab="Requests")
        restore = begin_busy(self._refresh_btn, working_text="Searching…")

        def worker() -> None:
            try:
                started = self.app.download_manager.auto_grab_missing_episodes(
                    show_ids=[row.show_id])
                msg = (f"Started {len(started)} download(s)."
                       if started else "Nothing grabbable this pass.")
                level = "success" if started else "warning"
            except Exception as exc:
                logger.exception("Grab-missing pass failed.")
                msg = f"Grab failed: {exc}"
                level = "error"

            def done() -> None:
                restore()
                self._status_var.set(msg)
                self.app.post_activity("Grab queue", msg, level=level, tab="Requests")
                self.refresh()
            self.app._post_to_ui(done)
        threading.Thread(target=worker, name="grabqueue-grab-missing",
                         daemon=True).start()

    def _unblock(self, row) -> None:
        blocklist_id = (row.detail or {}).get("blocklist_id")
        if not blocklist_id:
            return
        if not self.app._ask_yes_no(
                "Remove blocklist entry",
                f"Allow '{row.display_title}' again for {row.subject_key}?"):
            return
        downloads_store.remove_blocklist_entry(int(blocklist_id))
        msg = "Blocklist entry removed."
        self._status_var.set(msg)
        self.app.post_activity("Grab queue", msg, level="success", tab="Requests")
        self.refresh()

    def _keep_details(self, row) -> None:
        if row.selection_run_id is None:
            return
        downloads_store.set_keep_details(row.selection_run_id, True)
        msg = f"Run #{row.selection_run_id}: loser details kept forever."
        self._status_var.set(msg)
        self.app.post_activity("Grab queue", msg, level="success", tab="Requests")

    def _export_decision(self, row) -> None:
        if row.selection_run_id is None:
            return
        from tkinter import filedialog
        text = downloads_store.export_selection_run_json(row.selection_run_id)
        if text is None:
            self.app._show_warning("Export", "Selection run not found.")
            return
        path = filedialog.asksaveasfilename(
            title="Export decision JSON",
            defaultextension=".json",
            initialfile=f"selection-run-{row.selection_run_id}.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        msg = f"Exported to {path}"
        self._status_var.set(msg)
        self.app.post_activity("Grab queue", msg, level="success", tab="Requests")
