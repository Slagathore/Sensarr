# =============================================================================
# grab_queue.py  —  the Grab-queue view model (Task E)
# =============================================================================
# State/query contract FIRST, UI second: the Tk subtab renders GrabQueueRow
# objects produced here and never reconstructs state from scattered columns.
# Every row type the spec names has a query in this module:
#
#   request           — open / deferred / grabbing / verifying / placed rows,
#                       with deferral reason + next_attempt_at joined on
#   needs_identity    — unresolved rows (the resolve action lives on these)
#   needs_attention   — verify-failed rows, with the verification reason
#   keep_at_100       — one row per keep-at-100% show with missing episodes
#   follow_new        — upcoming / aired-unfetched episodes of followed shows
#   active_download   — downloads currently queued / downloading / downloaded
#   needs_placement   — staged downloads waiting for a folder (one-click create)
#   blocklist         — scoped blocklist entries (view / remove)
#
# Store-level ACTIONS the UI's right-click menu calls also live here (defer /
# grab now / reopen-expired), so they are testable without Tk.
# =============================================================================

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

import downloads_store
import queue_store
import shows_store

logger = logging.getLogger(__name__)

ROW_REQUEST = "request"
ROW_NEEDS_IDENTITY = "needs_identity"
ROW_NEEDS_ATTENTION = "needs_attention"
ROW_KEEP_AT_100 = "keep_at_100"
ROW_FOLLOW_NEW = "follow_new"
ROW_ACTIVE_DOWNLOAD = "active_download"
ROW_NEEDS_PLACEMENT = "needs_placement"
ROW_BLOCKLIST = "blocklist"

# The default user Defer duration (hours). One day matches the oversize rule.
DEFER_HOURS_DEFAULT = 24.0


@dataclass(frozen=True)
class GrabQueueRow:
    """One unified grab-queue line. `detail` carries per-type extras the
    detail pane renders (episode lists, blocklist scope, deferral stats...)."""
    row_type: str
    subject_key: str | None
    display_title: str
    state: str
    reason: str | None = None
    next_attempt_at: str | None = None
    download_id: int | None = None
    request_id: int | None = None
    show_id: int | None = None
    selection_run_id: int | None = None
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-type queries
# ---------------------------------------------------------------------------

def _request_subject_key(req) -> str | None:
    if req.is_qualified:
        key = f"{req.identity_source}:{req.external_id}"
        if req.season is not None:
            key += f":s{req.season}"
        return key
    return None


def _latest_run_id_for_request(request_id: int) -> int | None:
    for dl in downloads_store.downloads_for_request(request_id):
        if dl.selection_run_id is not None:
            return dl.selection_run_id
    run = None
    try:
        import db
        with db.connect() as conn:
            row = conn.execute(
                "SELECT MAX(id) FROM selection_runs WHERE request_id = ?",
                (request_id,)).fetchone()
        run = row[0] if row else None
    except Exception:
        logger.debug("selection run lookup failed for request #%s",
                     request_id, exc_info=True)
    return run


def request_rows() -> list[GrabQueueRow]:
    """Open/deferred/grabbing/verifying/placed requests (deferral info joined),
    plus needs_identity and needs_attention rows with their reasons."""
    deferrals = {d["key"]: d for d in downloads_store.list_grab_deferrals()}
    rows: list[GrabQueueRow] = []
    for req in queue_store.list_requests(status="active", limit=500):
        d = deferrals.get(f"req:{req.request_id}")
        title = req.resolved_title or req.content
        if req.season is not None:
            title = f"{title} S{int(req.season):02d}"
        common = dict(
            subject_key=_request_subject_key(req), display_title=title,
            state=req.status, request_id=req.request_id,
            selection_run_id=(d or {}).get("selection_run_id")
            or _latest_run_id_for_request(req.request_id),
            detail={
                "media_type": req.media_type,
                "requester": req.requester,
                "found_in_library": req.found_in_library,
                "candidate_stats": (d or {}).get("candidate_stats"),
            },
        )
        if req.status == queue_store.STATUS_NEEDS_IDENTITY:
            rows.append(GrabQueueRow(
                row_type=ROW_NEEDS_IDENTITY,
                reason="no provider-qualified identity — resolve to enable "
                       "auto-grab",
                **common))
        elif req.status == queue_store.STATUS_NEEDS_ATTENTION:
            rows.append(GrabQueueRow(
                row_type=ROW_NEEDS_ATTENTION,
                reason=_attention_reason(req.request_id),
                **common))
        else:
            rows.append(GrabQueueRow(
                row_type=ROW_REQUEST,
                reason=(d or {}).get("reason"),
                next_attempt_at=(d or {}).get("next_attempt_at"),
                **common))
    return rows


def _attention_reason(request_id: int) -> str | None:
    """The verification reason behind a needs_attention request: the newest
    linked download that failed/quarantined says why."""
    for dl in sorted(downloads_store.downloads_for_request(request_id),
                     key=lambda r: -r.download_id):
        if dl.verification_reason:
            return dl.verification_reason
        if dl.error:
            return dl.error
    return None


def keep_at_100_rows() -> list[GrabQueueRow]:
    """One row per keep-at-100% show that still has missing episodes."""
    rows: list[GrabQueueRow] = []
    for show in shows_store.list_shows():
        if not show.auto_grab or show.missing_count <= 0:
            continue
        missing = shows_store.missing_episodes(show.show_id)
        rows.append(GrabQueueRow(
            row_type=ROW_KEEP_AT_100,
            subject_key=f"{show.source}:{show.external_id}",
            display_title=show.title,
            state="missing",
            reason=f"{len(missing)} missing episode(s)",
            show_id=show.show_id,
            detail={
                "episodes": [f"S{e.season:02d}E{e.episode:02d}"
                             for e in missing[:50]],
                "missing_count": len(missing),
            }))
    return rows


def follow_new_rows() -> list[GrabQueueRow]:
    """Followed shows: the announced next episode (upcoming) and any episode
    that AIRED since follow_since but has no file yet (aired-unfetched)."""
    today = date.today().isoformat()
    rows: list[GrabQueueRow] = []
    for show in shows_store.list_shows():
        if not show.follow_new:
            continue
        since = show.follow_since or ""
        unfetched = [
            e for e in shows_store.missing_episodes(show.show_id)
            if e.air_date and (not since or e.air_date >= since)
        ]
        upcoming = None
        if (show.next_air_date and show.next_air_date >= today
                and show.next_season is not None
                and show.next_episode is not None):
            upcoming = (f"S{show.next_season:02d}E{show.next_episode:02d}"
                        f" airs {show.next_air_date}")
        if not unfetched and not upcoming:
            continue
        reason_bits = []
        if unfetched:
            reason_bits.append(f"{len(unfetched)} aired, not fetched")
        if upcoming:
            reason_bits.append(upcoming)
        rows.append(GrabQueueRow(
            row_type=ROW_FOLLOW_NEW,
            subject_key=f"{show.source}:{show.external_id}",
            display_title=show.title,
            state="following",
            reason="; ".join(reason_bits),
            next_attempt_at=show.next_air_date,
            show_id=show.show_id,
            detail={
                "aired_unfetched": [f"S{e.season:02d}E{e.episode:02d}"
                                    for e in unfetched[:50]],
                "follow_since": show.follow_since,
            }))
    return rows


def active_download_rows() -> list[GrabQueueRow]:
    """Downloads in flight (reuses the Downloads-tab query, filtered)."""
    rows: list[GrabQueueRow] = []
    for dl in downloads_store.list_downloads(limit=200):
        if dl.status not in ("queued", "downloading", "downloaded"):
            continue
        rows.append(GrabQueueRow(
            row_type=ROW_ACTIVE_DOWNLOAD,
            subject_key=None,
            display_title=dl.title,
            state=dl.status,
            reason=(f"{dl.progress * 100:.0f}%"
                    if dl.status == "downloading" else None),
            download_id=dl.download_id,
            request_id=dl.request_id,
            show_id=dl.show_id,
            selection_run_id=dl.selection_run_id,
            detail={"media_type": dl.media_type,
                    "quality_label": dl.quality_label,
                    "planned_dest": dl.planned_dest}))
    return rows


def needs_placement_rows() -> list[GrabQueueRow]:
    """Staged downloads waiting for a folder (one-click create action)."""
    rows: list[GrabQueueRow] = []
    for np in downloads_store.list_needs_placement():
        dl = downloads_store.get_download(np.download_id)
        title = dl.title if dl is not None else f"download #{np.download_id}"
        rows.append(GrabQueueRow(
            row_type=ROW_NEEDS_PLACEMENT,
            subject_key=None,
            display_title=title,
            state="needs placement",
            reason=np.reason,
            download_id=np.download_id,
            request_id=dl.request_id if dl is not None else None,
            show_id=np.show_id,
            selection_run_id=(dl.selection_run_id if dl is not None else None),
            detail={"suggested_dir": np.suggested_dir, "season": np.season}))
    return rows


def blocklist_rows(*, limit: int = 200) -> list[GrabQueueRow]:
    """Scoped blocklist entries (the browser: view / remove / widen)."""
    rows: list[GrabQueueRow] = []
    for b in downloads_store.list_blocklist(limit=limit):
        rows.append(GrabQueueRow(
            row_type=ROW_BLOCKLIST,
            subject_key=b.subject_key,
            display_title=b.parsed_title or b.infohash or "(unknown release)",
            state=b.reason_code,
            reason=b.reason_detail,
            detail={"blocklist_id": b.blocklist_id,
                    "subject_type": b.subject_type,
                    "infohash": b.infohash,
                    "created_at": b.created_at,
                    "created_by": b.created_by}))
    return rows


def list_grab_queue_rows() -> list[GrabQueueRow]:
    """THE public aggregator: every row type, in render order."""
    rows: list[GrabQueueRow] = []
    rows.extend(request_rows())
    rows.extend(keep_at_100_rows())
    rows.extend(follow_new_rows())
    rows.extend(active_download_rows())
    rows.extend(needs_placement_rows())
    rows.extend(blocklist_rows())
    return rows


@dataclass(frozen=True)
class StatusCounts:
    """Every count worth showing on the Status page. The `needs_*` group is
    things the USER must act on; the rest is live activity. Kept here (not in
    the Tk layer) so it is testable and one source of truth."""
    downloading: int = 0
    queued: int = 0
    needs_identity: int = 0      # resolve a provider match to enable grabbing
    needs_attention: int = 0     # a grab failed verification — decide what to do
    needs_placement: int = 0     # a download is staged, waiting for a folder
    deferred: int = 0            # waiting for the next attempt (informational)
    open_requests: int = 0       # qualified, waiting to be grabbed
    grabbing: int = 0            # actively being selected/fetched

    @property
    def actionable(self) -> int:
        """How many things genuinely need the user. Deferred/open are waiting on
        the app, not the user, so they are shown but not counted here."""
        return (self.needs_identity + self.needs_attention
                + self.needs_placement)


def status_counts() -> StatusCounts:
    """Compute the Status-page counts from the stores in one pass."""
    active = [r for r in downloads_store.list_downloads(limit=500)
              if r.status in ("downloading", "queued")]
    by_status: dict[str, int] = {}
    for req in queue_store.list_requests(status="active", limit=1000):
        by_status[req.status] = by_status.get(req.status, 0) + 1
    try:
        needs_placement = len(downloads_store.list_needs_placement())
    except Exception:
        logger.debug("needs_placement count failed", exc_info=True)
        needs_placement = 0
    return StatusCounts(
        downloading=sum(1 for r in active if r.status == "downloading"),
        queued=sum(1 for r in active if r.status == "queued"),
        needs_identity=by_status.get(queue_store.STATUS_NEEDS_IDENTITY, 0),
        needs_attention=by_status.get(queue_store.STATUS_NEEDS_ATTENTION, 0),
        needs_placement=needs_placement,
        deferred=by_status.get(queue_store.STATUS_DEFERRED, 0),
        open_requests=by_status.get(queue_store.STATUS_OPEN, 0),
        grabbing=by_status.get(queue_store.STATUS_GRABBING, 0),
    )


def status_summary_line(counts: StatusCounts | None = None) -> str:
    """Human 'Needs you' line for the Status page. Empty string when nothing
    needs the user AND nothing is in flight (the page shows 'all clear')."""
    c = counts or status_counts()
    needs = []
    if c.needs_identity:
        needs.append(f"{c.needs_identity} need identity resolved")
    if c.needs_attention:
        needs.append(f"{c.needs_attention} need attention (grab failed)")
    if c.needs_placement:
        needs.append(f"{c.needs_placement} need a folder (staged)")
    waiting = []
    if c.open_requests:
        waiting.append(f"{c.open_requests} open")
    if c.deferred:
        waiting.append(f"{c.deferred} deferred")
    if c.grabbing:
        waiting.append(f"{c.grabbing} grabbing")
    parts = []
    if needs:
        parts.append("⚠ Needs you: " + ", ".join(needs))
    if waiting:
        parts.append("Waiting: " + ", ".join(waiting))
    return "   |   ".join(parts)


# ---------------------------------------------------------------------------
# Decision detail (the pane rendering a SelectionDecision from its run rows)
# ---------------------------------------------------------------------------

def decision_detail(selection_run_id: int) -> dict | None:
    """Everything the detail pane needs for one selection run: the receipt,
    the forever histogram, and (until pruned) every candidate's gate verdict
    and score breakdown."""
    run = downloads_store.get_selection_run(selection_run_id)
    if run is None:
        return None

    def _maybe(text):
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None

    decisions = downloads_store.list_candidate_decisions(selection_run_id)
    return {
        "run": run,
        "pool_stats": _maybe(run.pool_stats_json) or {},
        "verdict_histogram": _maybe(run.verdict_histogram_json) or {},
        "candidates": decisions,
        "details_pruned": (not decisions
                           or (len(decisions) == 1
                               and run.chosen_infohash is not None)),
    }


# ---------------------------------------------------------------------------
# Actions (store-level; the Tk right-click menu is thin glue over these)
# ---------------------------------------------------------------------------

def defer_request(request_id: int, *, hours: float = DEFER_HOURS_DEFAULT,
                  reason: str = "deferred by user") -> None:
    """User Defer: move the request to 'deferred' with an explicit deferral
    row (reason + next_attempt_at). Auto-grab skips deferred rows and
    reopen_expired_deferrals() brings it back once the clock passes."""
    queue_store.set_status(request_id, queue_store.STATUS_DEFERRED)
    downloads_store.set_grab_deferral(
        f"req:{request_id}", wait_hours=hours, reason=reason)


def grab_now_request(request_id: int) -> None:
    """User Grab-now: clear any deferral and reopen the request so the next
    auto-grab pass (or a manually triggered one) takes it immediately."""
    downloads_store.clear_grab_deferral(f"req:{request_id}")
    req = queue_store.get_request(request_id)
    if req is not None and req.status in (queue_store.STATUS_DEFERRED,
                                          queue_store.STATUS_OPEN):
        queue_store.set_status(request_id, queue_store.STATUS_OPEN)


def reopen_expired_deferrals() -> list[int]:
    """Deferred requests whose next_attempt_at has passed reopen (called at
    the start of every auto-grab pass). Returns the reopened request ids."""
    reopened: list[int] = []
    for req in queue_store.list_requests(
            status=queue_store.STATUS_DEFERRED, limit=500):
        detail = downloads_store.get_grab_deferral(f"req:{req.request_id}")
        if downloads_store.deferral_expired(detail):
            queue_store.set_status(req.request_id, queue_store.STATUS_OPEN)
            downloads_store.clear_grab_deferral(f"req:{req.request_id}")
            reopened.append(req.request_id)
    if reopened:
        logger.info("Grab queue: reopened %d expired deferral(s): %s",
                    len(reopened), reopened)
    return reopened
