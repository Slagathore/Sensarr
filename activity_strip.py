# =============================================================================
# activity_strip.py
# =============================================================================
# Task L — the header's global activity strip.
#
# Complaint this fixes: statuses packed at a toolbar's far right (Shows
# scan/sync especially) go unseen, and multiple tabs' statuses sit on top of
# one another with no shared place to look. The fix is ONE always-visible
# strip in the header, fed by a single API every part of the app can call:
# post_activity(source, message, level, tab).
#
# ActivityModel below is the pure, Tk-free brain behind that strip: which
# entry is "current" right now, when it expires, and the short history a
# "Recent" dropdown reads. desktop_app.py owns the Tk rendering (a Label +
# a small ticker) and is the only thing that imports tkinter; this module is
# plain Python on purpose so it's unit-testable headless, same reasoning as
# maint_jobs.py's registry.
#
# Level semantics (RESOLVED per the sprint bootstrap's wording):
#   "working"  — an operation is in flight. Persists — never auto-expires —
#                until replaced by a later post() (normally that operation's
#                own completion) or explicitly dismissed.
#   "success"  — finished cleanly. Auto-fades after fade_seconds (~10s
#                default) so the strip goes quiet again on its own.
#   "warning"  — finished with something the user should notice (cancelled,
#                partially failed, "already running", etc). Sticky like
#                error — stays until clicked. Not explicitly named in the
#                bootstrap text ("errors stay until clicked") but treated the
#                same way here: a warning silently expiring after 10s would
#                be just as easy to miss as an error would.
#   "error"    — failed. Sticky — stays until clicked.
#
# Only ONE entry is ever "current" (the strip is a single full-width row,
# not a per-source multi-slot panel) — the most recently posted entry always
# wins. Anything displaced before the user saw it is still reachable from
# history() (the "Recent" dropdown), which is why a missed status is
# recoverable per item 5.
# =============================================================================

import datetime
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

# Valid levels for post(); anything else is almost certainly a caller typo
# and raises immediately rather than silently rendering with a fallback
# color.
LEVELS = ("working", "success", "warning", "error")

# Levels that persist until the strip is clicked (or replaced by a later
# post()) instead of auto-fading. "working" is handled separately below
# since it persists for a different reason (the job isn't done, not "this
# is important") but the effect on current() is the same: no fade.
STICKY_LEVELS = frozenset({"warning", "error"})

DEFAULT_FADE_SECONDS = 10.0
DEFAULT_HISTORY_LIMIT = 12


@dataclass(frozen=True)
class ActivityEntry:
    """One posted activity. Immutable — post() always creates a new one
    rather than mutating history in place."""
    seq: int
    source: str
    message: str
    level: str
    tab: str | None
    created: float   # monotonic seconds — fade math only, never displayed
    at: str          # wall-clock "HH:MM:SS" for the history dropdown


class ActivityModel:
    """Pure model backing the header activity strip. No Tk import anywhere
    in this module — the Tk layer renders whatever current()/history()
    return and calls post()/dismiss_current() in response to job events and
    widget clicks.

    `clock` and `now` are injectable so tests can control fade timing and
    the displayed timestamp without real sleeps.
    """

    def __init__(self, *, fade_seconds: float = DEFAULT_FADE_SECONDS,
                 history_limit: int = DEFAULT_HISTORY_LIMIT,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], "datetime.datetime"] = datetime.datetime.now
                 ) -> None:
        self._fade_seconds = fade_seconds
        self._clock = clock
        self._now = now
        self._history: deque[ActivityEntry] = deque(maxlen=history_limit)
        self._current: ActivityEntry | None = None
        self._seq = 0

    def post(self, source: str, message: str, level: str = "working",
             tab: str | None = None) -> ActivityEntry:
        """Record a new entry and make it the current strip contents.
        Always wins over whatever was showing before, even a sticky
        error/warning — the newest activity is always what's most relevant
        (the displaced entry is still one click away in history())."""
        if level not in LEVELS:
            raise ValueError(f"unknown activity level {level!r} (expected "
                             f"one of {LEVELS})")
        self._seq += 1
        entry = ActivityEntry(
            seq=self._seq, source=source, message=message, level=level,
            tab=tab, created=self._clock(),
            at=self._now().strftime("%H:%M:%S"),
        )
        self._current = entry
        self._history.appendleft(entry)
        return entry

    def current(self) -> ActivityEntry | None:
        """The entry the strip should show right now, or None once a
        non-sticky entry has faded and nothing has replaced it."""
        self._expire_if_needed()
        return self._current

    def dismiss_current(self) -> None:
        """Clicking the strip acknowledges whatever is showing — this is
        what makes a sticky error/warning eventually go away (item 1: 'errors
        stay until clicked'). Safe to call with nothing current."""
        self._current = None

    def history(self, limit: int | None = None) -> list[ActivityEntry]:
        """Newest-first list of the last ~history_limit entries (item 5),
        independent of whether any of them have expired off the strip
        itself — this is the recovery path for a missed status.

        note: deferred (Task L item 6, marked optional) — history() only
        ever holds entries posted THIS session; the Recent dropdown is
        empty right after launch even though maintenance_jobs.py's own
        SQLite journal remembers every run across restarts. Seeding it at
        startup needs: a seed_history()-style method here that appends
        without disturbing current() or the fade clock; a journal-row ->
        ActivityEntry mapper in desktop_app.py (state -> level, label ->
        message, finished_at -> the displayed timestamp, both handled for
        an 'interrupted' row that has no finished_at); and one call site in
        DesktopApp.__init__ right after _activity_model is constructed,
        reading self._maint_registry.history(limit=12). Comes to a little
        over the ~20-line budget once that last edge case and a test are
        both handled honestly rather than assumed away, so it's a
        breadcrumb here instead of a rushed implementation.
        """
        entries = list(self._history)
        return entries if limit is None else entries[:limit]

    def _expire_if_needed(self) -> None:
        entry = self._current
        if entry is None:
            return
        if entry.level == "working" or entry.level in STICKY_LEVELS:
            return
        if self._clock() - entry.created >= self._fade_seconds:
            self._current = None
