# =============================================================================
# ui_helpers.py
# =============================================================================
# Small reusable Tk helpers shared by the desktop tabs:
#
#   make_sortable(tree)  — click a Treeview column header to sort by it
#                          (click again to reverse). Numeric-aware.
#   Spinner              — animated "working…" indicator label (braille
#                          frames, no image asset needed).
#   ellipsize /
#   mirror_ellipsized    — Task L item 3: truncate a status line for a
#                          full-width row, with the untruncated text still
#                          reachable via add_tooltip.
#   begin_busy           — Task L item 4: the one shared "disable this button
#                          and show a working label until the job reports
#                          back" helper, used by Shows scan/sync, watchlist
#                          pulls, and the Maintenance tab's Apply Selected.
# =============================================================================

import datetime as _datetime
import re
import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Protocol, runtime_checkable

import config


@runtime_checkable
class _StringVarLike(Protocol):
    """Structural stand-in for tk.StringVar — get/set/trace_add is the
    entire surface mirror_ellipsized touches. A real tk.StringVar satisfies
    this trivially; so does a plain test double with no Tcl interpreter
    behind it, which is the whole reason this is a Protocol instead of
    requiring the concrete tk.StringVar class. (begin_busy below stays on
    the concrete ttk.Button type instead of a matching Protocol — it
    isinstance()-branches single-button vs. an iterable of them, which a
    structural Protocol can't narrow cleanly; its tests silence the
    resulting duck-typed-fake mismatch with a plain # type: ignore instead.)
    """
    def get(self) -> str: ...
    def set(self, value: str) -> None: ...
    def trace_add(self, mode: str, callback: Callable[..., None]) -> None: ...


def local_ts(ts: str) -> str:
    """Render a stored timestamp in the machine's local timezone.

    Stored timestamps (SQLite CURRENT_TIMESTAMP and the explicit
    datetime.now(timezone.utc) writes) are UTC; naive values are assumed UTC.
    Shared by every tab that displays a stored time (desktop_app._local_ts
    delegates here).
    """
    try:
        dt = _datetime.datetime.fromisoformat(ts.replace("T", " ").strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_datetime.timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts

_NUMERIC_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*%?\s*$")   # "42", "3.5", "17%"
_FRACTION_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")  # "123/321" have-counts


def _sort_key(value: str):
    """Key that sorts numbers (including percentages and fractions)
    numerically and everything else case-folded."""
    m = _FRACTION_RE.match(value)
    if m:
        return (0, int(m.group(1)))
    if _NUMERIC_RE.match(value):
        return (0, float(value.replace("%", "").strip()))
    return (1, value.casefold())


def make_sortable(tree: ttk.Treeview, *, on_sorted=None) -> None:
    """Make every column of a Treeview sortable by clicking its header.

    Sorting reorders the existing rows in place (iids and values are kept),
    so callers that map iids back to data keep working. on_sorted() fires
    after each re-order, for callers that track row order separately.
    """
    state = {"column": None, "reverse": False}
    columns = tree["columns"]

    def sort_by(col: str) -> None:
        reverse = state["column"] == col and not state["reverse"]
        state["column"], state["reverse"] = col, reverse
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        rows.sort(key=lambda pair: _sort_key(pair[0]), reverse=reverse)
        for index, (_val, iid) in enumerate(rows):
            tree.move(iid, "", index)
        # Arrow on the active header only.
        for c in columns:
            base = tree.heading(c, "text").rstrip(" ▲▼")
            suffix = ""
            if c == col:
                suffix = " ▼" if reverse else " ▲"
            tree.heading(c, text=base + suffix)
        if on_sorted is not None:
            on_sorted()

    for col in columns:
        # Default arg binds the current column name.
        tree.heading(col, command=lambda c=col: sort_by(c))


def bind_smooth_vscroll(canvas: tk.Canvas) -> None:
    """Page-level mouse-wheel scrolling without lag or ghost afterimages.

    The smearing came from Tk redrawing a widget-heavy embedded frame once
    PER WHEEL EVENT — high-resolution wheels/touchpads fire dozens of small
    events per flick, each triggering a partial repaint. Here the deltas
    are accumulated and applied at most once per frame (~16 ms), followed
    by update_idletasks() so each step paints completely before the next.
    Widgets that scroll themselves (trees, listboxes, text) are left alone.
    """
    canvas.configure(yscrollincrement=1)  # pixel-precise positioning
    state = {"pending": 0, "scheduled": False}

    def _self_scrolling(widget) -> bool:
        w = widget
        while w is not None and w is not canvas:
            if isinstance(w, (ttk.Treeview, tk.Listbox, tk.Text)):
                return True
            w = getattr(w, "master", None)
        return False

    def _flush() -> None:
        state["scheduled"] = False
        pixels = state["pending"]
        state["pending"] = 0
        if pixels:
            try:
                canvas.yview_scroll(pixels, "units")
                canvas.update_idletasks()  # complete this paint before more input
            except tk.TclError:
                pass

    def _wheel(event) -> None:
        if _self_scrolling(event.widget):
            return
        # 120 delta-units (one notch) ≈ 60 px of travel.
        state["pending"] += int(-event.delta / 2)
        if not state["scheduled"]:
            state["scheduled"] = True
            try:
                canvas.after(16, _flush)
            except tk.TclError:
                state["scheduled"] = False

    canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel), add="+")
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"), add="+")


def add_tooltip(widget: tk.Misc, text: "str | Callable[[], str]", *,
                delay_ms: int = 500) -> None:
    """Hover tooltip for any widget. Honours config.TOOLTIPS_ENABLED at
    show time, so the Settings toggle applies without a restart.

    `text` is normally a fixed string, but may also be a zero-arg callable
    returning the current text — Task L item 3 uses this to tooltip a
    StringVar-backed status label with its live, untruncated value (the
    label itself shows an ellipsized copy so a long status can't blow out a
    full-width row). A callable returning an empty/falsy string shows no
    tooltip at all.
    """
    state: dict = {"after_id": None, "tip": None}

    def show() -> None:
        state["after_id"] = None
        if not getattr(config, "TOOLTIPS_ENABLED", True) or state["tip"] is not None:
            return
        content = text() if callable(text) else text
        if not content:
            return
        try:
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tip.attributes("-topmost", True)
            tk.Label(
                tip, text=content, justify=tk.LEFT, wraplength=340,
                bg="#2b2b2b", fg="#e8e8e8", relief=tk.SOLID, borderwidth=1,
                font=("Segoe UI", 9), padx=8, pady=4,
            ).pack()
            state["tip"] = tip
        except tk.TclError:
            state["tip"] = None

    def schedule(_e=None) -> None:
        cancel()
        try:
            state["after_id"] = widget.after(delay_ms, show)
        except tk.TclError:
            pass

    def cancel(_e=None) -> None:
        if state["after_id"] is not None:
            try:
                widget.after_cancel(state["after_id"])
            except tk.TclError:
                pass
            state["after_id"] = None
        if state["tip"] is not None:
            try:
                state["tip"].destroy()
            except tk.TclError:
                pass
            state["tip"] = None

    widget.bind("<Enter>", schedule, add="+")
    widget.bind("<Leave>", cancel, add="+")
    widget.bind("<ButtonPress>", cancel, add="+")


class Spinner:
    """Animated text spinner — the "loading gif" for long operations.

    Attach to any ttk.Label; start() animates braille frames next to the
    given message, stop() clears it. Runs on the Tk after() loop, so it must
    be started/stopped from the UI thread.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: ttk.Label) -> None:
        self._label = label
        self._running = False
        self._frame = 0
        self._text = ""

    def start(self, text: str = "Working…") -> None:
        self._text = text
        if not self._running:
            self._running = True
            self._tick()

    def update_text(self, text: str) -> None:
        self._text = text

    def stop(self, final_text: str = "") -> None:
        self._running = False
        self._label.configure(text=final_text)

    def _tick(self) -> None:
        if not self._running:
            return
        frame = self._FRAMES[self._frame % len(self._FRAMES)]
        self._frame += 1
        self._label.configure(text=f"{frame} {self._text}")
        try:
            self._label.after(120, self._tick)
        except tk.TclError:
            self._running = False


def ellipsize(text: str | None, max_chars: int) -> str:
    """Truncate `text` to max_chars, replacing the tail with a single "…"
    when it doesn't fit (Task L item 3 — a full-width status row still
    needs to stay one line; the untruncated text belongs in a tooltip, not
    wrapped across rows). A short string passes through unchanged; None
    (a StringVar that's never been set, say) is treated as empty."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"[:max_chars]
    return text[:max_chars - 1].rstrip() + "…"


def mirror_ellipsized(var: _StringVarLike, max_chars: int = 110) -> tk.StringVar:
    """A StringVar that mirrors `var`, ellipsized to max_chars.

    Pair with a Label bound to the RETURNED var (not the original) plus
    add_tooltip(label, var.get) so the full untruncated text is still one
    hover away — this is the exact shape Task L item 3 asks for: a
    full-width status row, ellipsized, with a tooltip. `var` keeps being the
    thing every caller already sets (self._status_var.set(...) is unchanged
    anywhere in the tab modules); this just adds a derived display copy.
    """
    display = tk.StringVar(value=ellipsize(var.get(), max_chars))

    def _sync(*_args: object) -> None:
        display.set(ellipsize(var.get(), max_chars))

    var.trace_add("write", _sync)
    return display


def begin_busy(buttons: "ttk.Button | Iterable[ttk.Button] | None", *,
              working_text: str | None = None) -> Callable[[], None]:
    """Task L item 4: the one shared "busy button" helper.

    Disables every button in `buttons` (a single button or an iterable of
    them; None is a harmless no-op) and, when `working_text` is given, swaps
    each one's label to it. Returns a zero-arg restore() callable — call it
    from the job's completion handler (already marshaled onto the UI thread,
    same as every other cross-thread update in this app) to re-enable the
    button(s) and put their original label back.

    restore() is idempotent (safe to call more than once) and tolerates a
    widget that no longer exists (e.g. its dialog was closed mid-run) by
    swallowing TclError — the point is to never let a stuck 'disabled'
    button block the UI, not to guarantee the label always resets.
    """
    if buttons is None:
        widgets: list[ttk.Button] = []
    elif isinstance(buttons, ttk.Button):
        widgets = [buttons]
    else:
        widgets = list(buttons)

    saved: list[tuple[ttk.Button, str]] = []
    for btn in widgets:
        try:
            saved.append((btn, str(btn.cget("text"))))
            btn.state(["disabled"])
            if working_text is not None:
                btn.configure(text=working_text)
        except tk.TclError:
            pass

    restored = {"done": False}

    def restore() -> None:
        if restored["done"]:
            return
        restored["done"] = True
        for btn, original_text in saved:
            try:
                btn.configure(text=original_text)
                btn.state(["!disabled"])
            except tk.TclError:
                pass

    return restore
