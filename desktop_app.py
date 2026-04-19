import logging
import threading
import tkinter as tk
from importlib import import_module
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
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
from plex_control import get_status, hard_reset, launch_plex, soft_reset
from queue_store import (add_request, complete_request, initialize_queue_db,
                          list_requests, open_request_count)
from telegram_service import TelegramBotService

logger = logging.getLogger(__name__)
Image = cast(Any, import_module("PIL.Image"))
ImageDraw = cast(Any, import_module("PIL.ImageDraw"))
pystray = cast(Any, import_module("pystray"))
Icon = pystray.Icon
Menu = pystray.Menu
MenuItem = pystray.MenuItem
PillowImage = Any


class DesktopApp:
    def __init__(self) -> None:
        self.bot_service = TelegramBotService()
        self.root = tk.Tk()
        self.root.title("Plex Reset Button")
        self.root.geometry("860x620")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        initialize_queue_db()

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
        self.library_summary_text: scrolledtext.ScrolledText | None = None
        self.library_results_tree: ttk.Treeview | None = None
        self.metrics_text: scrolledtext.ScrolledText | None = None
        self._last_log_count = 0
        self._tray_icon = self._build_tray_icon()
        self._quitting = False
        self._library_summary_refresh_running = False
        self._library_summary_refresh_pending = False
        self._library_metrics_refresh_running = False
        self._library_metrics_refresh_pending = False

        self._build_ui()

    def run(self) -> None:
        try:
            self.bot_service.start()
            self.bot_status_var.set("Telegram bot: running")
        except Exception as exc:
            logger.exception("Failed to start Telegram bot service.")
            self.bot_status_var.set(f"Telegram bot: failed ({exc})")
            messagebox.showerror(
                "Telegram bot failed",
                f"Could not start the Telegram bot service.\n\n{exc}",
            )

        self._tray_icon.run_detached()
        self.root.after(0, self._initialize_runtime_state)
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
        self._schedule_status_refresh()
        self._schedule_log_refresh()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        self.root.rowconfigure(4, weight=1)

        header = ttk.Frame(self.root, padding=16)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="Plex Reset Button",
            font=("Segoe UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.bot_status_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(header, textvariable=self.last_action_var).grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.queue_var).grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.library_var).grid(row=4, column=0, sticky="w", pady=(4, 0))

        actions = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        actions.grid(row=1, column=0, sticky="ew")
        for index in range(5):
            actions.columnconfigure(index, weight=1)

        ttk.Button(actions, text="Launch Plex", command=lambda: self.run_action("Launch Plex", launch_plex)).grid(row=0, column=0, padx=4, sticky="ew")
        ttk.Button(actions, text="Soft Reset", command=lambda: self.run_action("Soft Reset", soft_reset)).grid(row=0, column=1, padx=4, sticky="ew")
        ttk.Button(actions, text="Hard Reset", command=self.confirm_hard_reset).grid(row=0, column=2, padx=4, sticky="ew")
        ttk.Button(actions, text="Refresh Status", command=self.refresh_status).grid(row=0, column=3, padx=4, sticky="ew")
        ttk.Button(actions, text="Get Plex Token", command=self.authenticate_plex_account).grid(row=0, column=4, padx=4, sticky="ew")

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=2, column=0, padx=16, pady=(0, 12), sticky="nsew")

        status_tab = ttk.Frame(notebook, padding=12)
        requests_tab = ttk.Frame(notebook, padding=12)
        library_tab = ttk.Frame(notebook, padding=12)
        metrics_tab = ttk.Frame(notebook, padding=12)
        logs_tab = ttk.Frame(notebook, padding=12)
        notebook.add(status_tab, text="Status")
        notebook.add(requests_tab, text="Requests")
        notebook.add(library_tab, text="Library")
        notebook.add(metrics_tab, text="Metrics")
        notebook.add(logs_tab, text="Logs")

        status_tab.columnconfigure(0, weight=1)
        status_tab.rowconfigure(0, weight=1)
        requests_tab.columnconfigure(0, weight=1)
        requests_tab.rowconfigure(1, weight=1)
        library_tab.columnconfigure(0, weight=1)
        library_tab.rowconfigure(2, weight=1)
        metrics_tab.columnconfigure(0, weight=1)
        metrics_tab.rowconfigure(1, weight=1)
        logs_tab.columnconfigure(0, weight=1)
        logs_tab.rowconfigure(0, weight=1)

        self.status_text = scrolledtext.ScrolledText(
            status_tab,
            wrap=tk.WORD,
            height=14,
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        self.status_text.grid(row=0, column=0, sticky="nsew")

        requests_toolbar = ttk.Frame(requests_tab)
        requests_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        requests_toolbar.columnconfigure(3, weight=1)

        ttk.Label(requests_toolbar, text="Requester").grid(row=0, column=0, sticky="w")
        ttk.Entry(requests_toolbar, textvariable=self.requester_var, width=18).grid(row=0, column=1, padx=(6, 12), sticky="w")
        ttk.Label(requests_toolbar, text="Request").grid(row=0, column=2, sticky="w")
        request_entry = ttk.Entry(requests_toolbar, textvariable=self.request_content_var)
        request_entry.grid(row=0, column=3, padx=(6, 12), sticky="ew")
        request_entry.bind("<Return>", lambda _event: self.add_request_from_ui())
        ttk.Button(requests_toolbar, text="Add", command=self.add_request_from_ui).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(requests_toolbar, text="Complete Selected", command=self.complete_selected_request).grid(row=0, column=5, padx=(0, 6))
        ttk.Button(requests_toolbar, text="Refresh", command=self.refresh_requests).grid(row=0, column=6)

        requests_frame = ttk.Frame(requests_tab)
        requests_frame.grid(row=1, column=0, sticky="nsew")
        requests_frame.columnconfigure(0, weight=1)
        requests_frame.rowconfigure(0, weight=1)

        self.requests_tree = ttk.Treeview(
            requests_frame,
            columns=("id", "requester", "created", "content"),
            show="headings",
            height=14,
        )
        self.requests_tree.heading("id", text="ID")
        self.requests_tree.heading("requester", text="Requester")
        self.requests_tree.heading("created", text="Created")
        self.requests_tree.heading("content", text="Request")
        self.requests_tree.column("id", width=70, anchor=tk.CENTER, stretch=False)
        self.requests_tree.column("requester", width=140, anchor=tk.W, stretch=False)
        self.requests_tree.column("created", width=150, anchor=tk.W, stretch=False)
        self.requests_tree.column("content", width=420, anchor=tk.W)
        self.requests_tree.grid(row=0, column=0, sticky="nsew")

        requests_scroll = ttk.Scrollbar(requests_frame, orient=tk.VERTICAL, command=self.requests_tree.yview)
        requests_scroll.grid(row=0, column=1, sticky="ns")
        self.requests_tree.configure(yscrollcommand=requests_scroll.set)

        library_toolbar = ttk.Frame(library_tab)
        library_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        library_toolbar.columnconfigure(1, weight=1)

        ttk.Label(library_toolbar, text="Search").grid(row=0, column=0, sticky="w")
        library_search_entry = ttk.Entry(library_toolbar, textvariable=self.library_search_var)
        library_search_entry.grid(row=0, column=1, padx=(6, 12), sticky="ew")
        library_search_entry.bind("<Return>", lambda _event: self.search_library_from_ui())
        ttk.Button(library_toolbar, text="Search", command=self.search_library_from_ui).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(library_toolbar, text="Reindex", command=self.rebuild_library_index_from_ui).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(library_toolbar, text="Refresh Summary", command=self.refresh_library_summary).grid(row=0, column=4)

        self.library_summary_text = scrolledtext.ScrolledText(
            library_tab,
            wrap=tk.WORD,
            height=7,
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        self.library_summary_text.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        library_results_frame = ttk.Frame(library_tab)
        library_results_frame.grid(row=2, column=0, sticky="nsew")
        library_results_frame.columnconfigure(0, weight=1)
        library_results_frame.rowconfigure(0, weight=1)

        self.library_results_tree = ttk.Treeview(
            library_results_frame,
            columns=("name", "root", "path"),
            show="headings",
            height=12,
        )
        self.library_results_tree.heading("name", text="Name")
        self.library_results_tree.heading("root", text="Library Root")
        self.library_results_tree.heading("path", text="Path")
        self.library_results_tree.column("name", width=240, anchor=tk.W, stretch=False)
        self.library_results_tree.column("root", width=220, anchor=tk.W, stretch=False)
        self.library_results_tree.column("path", width=420, anchor=tk.W)
        self.library_results_tree.grid(row=0, column=0, sticky="nsew")

        library_scroll = ttk.Scrollbar(
            library_results_frame,
            orient=tk.VERTICAL,
            command=self.library_results_tree.yview,
        )
        library_scroll.grid(row=0, column=1, sticky="ns")
        self.library_results_tree.configure(yscrollcommand=library_scroll.set)

        metrics_toolbar = ttk.Frame(metrics_tab)
        metrics_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(metrics_toolbar, text="Refresh Metrics", command=self.refresh_library_metrics).grid(row=0, column=0, sticky="w")

        self.metrics_text = scrolledtext.ScrolledText(
            metrics_tab,
            wrap=tk.WORD,
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        self.metrics_text.grid(row=1, column=0, sticky="nsew")

        self.log_text = scrolledtext.ScrolledText(
            logs_tab,
            wrap=tk.WORD,
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        summary = ttk.Frame(self.root, padding=(16, 0, 16, 16))
        summary.grid(row=3, column=0, sticky="ew")
        ttk.Label(summary, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

    def _build_tray_icon(self) -> Icon:
        image = self._create_tray_image()
        menu = Menu(
            MenuItem("Show Admin", lambda icon, item: self.show_window()),
            MenuItem("Launch Plex", lambda icon, item: self.run_action("Launch Plex", launch_plex)),
            MenuItem("Soft Reset", lambda icon, item: self.run_action("Soft Reset", soft_reset)),
            MenuItem("Hard Reset", lambda icon, item: self.confirm_hard_reset(from_tray=True)),
            MenuItem("Refresh Status", lambda icon, item: self.refresh_status()),
            MenuItem("Get Plex Token", lambda icon, item: self.authenticate_plex_account()),
            MenuItem("Quit", lambda icon, item: self.request_exit()),
        )
        return Icon("PlexResetButton", image, "Plex Reset Button", menu)

    def _create_tray_image(self) -> PillowImage:
        asset_path = Path(config.TASKBAR_ICON_PATH)
        if asset_path.is_file():
            try:
                base = Image.open(asset_path).convert("RGBA")
                icon = Image.new("RGBA", (64, 64), (24, 28, 36, 255))
                resized = base.copy()
                resized.thumbnail((44, 44))
                left = (64 - resized.width) // 2
                top = (64 - resized.height) // 2
                icon.paste(resized, (left, top), resized)
                return icon
            except OSError:
                logger.warning("Could not load %s for tray icon; using fallback image.", asset_path)

        icon = Image.new("RGBA", (64, 64), (24, 28, 36, 255))
        draw = ImageDraw.Draw(icon)
        draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(227, 88, 51, 255))
        draw.text((22, 18), "PR", fill=(255, 255, 255, 255))
        return icon

    def show_window(self) -> None:
        self._post_to_ui(self._show_window)

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
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

    def refresh_status(self) -> None:
        self.run_action("Refresh Status", get_status, show_popup=False, update_status_only=True)

    def refresh_requests(self) -> None:
        if self.requests_tree is None:
            return

        requests = list_requests(limit=100)
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
            self.requests_tree.insert(
                "",
                "end",
                iid=row_id,
                values=(
                    request.request_id,
                    request.requester,
                    request.created_at.replace("T", " "),
                    request.content,
                ),
            )

        if selected_id and self.requests_tree.exists(selected_id):
            self.requests_tree.selection_set(selected_id)

        self.queue_var.set(f"Open requests: {open_request_count()}")

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

    def search_library_from_ui(self) -> None:
        if self.library_results_tree is None:
            return

        query = self.library_search_var.get().strip()
        if not query:
            self._show_warning("Missing search", "Enter a title or keyword to search for.")
            return

        results = search_library(query)
        for item_id in self.library_results_tree.get_children():
            self.library_results_tree.delete(item_id)

        for index, entry in enumerate(results):
            self.library_results_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(entry.name, entry.root_path, entry.path),
            )

        self.status_var.set(f'Library search for "{query}" returned {len(results)} result(s)')
        self.last_action_var.set("Last action: library search")
        if not results:
            self._show_info("No results", f'No indexed library matches for "{query}".')

    def rebuild_library_index_from_ui(self) -> None:
        def worker() -> None:
            try:
                result = rebuild_library_index()
                message = format_reindex_result_message(result)
            except Exception as exc:
                logger.exception("Library reindex failed.")
                message = f"Library reindex failed: {exc}"

            self._post_to_ui(lambda: self._handle_library_reindex_result(message))

        threading.Thread(target=worker, name="ui-library-reindex", daemon=True).start()

    def _handle_library_reindex_result(self, message: str) -> None:
        self.refresh_library_summary()
        self.refresh_library_metrics()
        self.status_var.set(message.splitlines()[0] if message else "Library reindex complete")
        self.last_action_var.set("Last action: library reindex")
        self._show_info("Library Reindex", message)

    def add_request_from_ui(self) -> None:
        requester = self.requester_var.get().strip() or "Admin"
        content = self.request_content_var.get().strip()
        if not content:
            self._show_warning("Missing request", "Enter the request details before adding it.")
            return

        created = add_request(content, requester)
        self.request_content_var.set("")
        self.last_action_var.set(f"Last action: added request #{created.request_id}")
        self.status_var.set(f"Queued request #{created.request_id}: {created.content}")
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
                self._post_to_ui(
                    lambda: self._handle_plex_auth_started(session, browser_opened),
                )
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
            message = (
                "Could not open your browser automatically.\n\n"
                + message
            )
        self._show_info("Authorize Plex", message)

    def _handle_plex_auth_timeout(self) -> None:
        self.last_action_var.set("Last action: Plex authorization timed out")
        self.status_var.set("Plex authorization timed out")
        self._show_warning(
            "Plex Authorization",
            "The Plex authorization window expired before approval completed. Start the flow again.",
        )

    def _handle_plex_auth_failure(self, error_message: str) -> None:
        self.last_action_var.set("Last action: Plex authorization failed")
        self.status_var.set("Plex authorization failed")
        self._show_warning("Plex Authorization", error_message)

    def _handle_plex_auth_success(self, result: PlexTokenResult) -> None:
        masked_token = (
            f"{result.auth_token[:6]}...{result.auth_token[-4:]}"
            if len(result.auth_token) >= 12
            else result.auth_token
        )
        self.last_action_var.set("Last action: Plex token saved")
        self.status_var.set("Saved Plex token to .env")
        self.refresh_library_metrics()
        self._show_info(
            "Plex Authorization",
            "Plex authorization succeeded.\n\n"
            "Saved PLEX_TOKEN and PLEX_CLIENT_IDENTIFIER to .env.\n"
            f"Token: {masked_token}",
        )

    def run_action(
        self,
        action_name: str,
        action,
        *,
        show_popup: bool = True,
        update_status_only: bool = False,
    ) -> None:
        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                logger.exception("%s failed.", action_name)
                result = f"{action_name} failed: {exc}"

            self._post_to_ui(
                lambda: self._handle_action_result(
                    action_name,
                    result,
                    show_popup=show_popup,
                    update_status_only=update_status_only,
                )
            )

        threading.Thread(target=worker, name=f"ui-{action_name.lower().replace(' ', '-')}", daemon=True).start()

    def _handle_action_result(
        self,
        action_name: str,
        result: str,
        *,
        show_popup: bool,
        update_status_only: bool,
    ) -> None:
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
        self._schedule_status_refresh()

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
