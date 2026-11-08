# Changelog

## 1.6.4 - 2026-11-07

### Plex and Windows startup

- Ordinary Sensarr launches no longer require administrator elevation.
- Launch and Hard Reset start Plex as the signed-in Windows desktop user,
  including when Sensarr was opened with a different administrator account.
  Plex keeps that user's registry settings, libraries, and AppData profile.
- Hard Reset checks that the desktop identity can be used before stopping
  Plex. It does not fall back to launching a different user's server.
- Single-instance detection recognizes protected packaged Sensarr processes
  without requiring their command lines and conservatively preserves
  unreadable live PID locks.

### Responsiveness and Telegram

- Activity, Downloads, Requests, Users, and Grab Queue refresh their database
  snapshots off the Tk thread. Slow reads leave the current view usable.
- Progress bursts share a pending refresh, and UI callbacks are drained in
  bounded batches so input and repaint events can keep running.
- Request search and add operations, including all-season requests, run their
  slow lookup and routing work in background workers.
- Candidate-decision retention uses an indexed lookup and bounded delete
  batches, releasing the database lock between batches.
- Telegram startup retries temporary network failures in the background.
  Long administrative commands no longer block unrelated updates, handler
  failures get a useful error reply, and partial startup shuts down cleanly.

### Placement and staging

- Placement prompts resolve when a job moves, is canceled, is archived, or
  otherwise stops being actionable. Startup reconciles obsolete prompts
  left by older versions without deleting their history.
- Partial pack imports stay retryable and preserve receipts for members
  already placed. A single successful episode cannot verify an entire pack.
- Missing completed payloads are held for review. Automatic episode grabs
  respect a held season pack until it is explicitly canceled or removed.
- Exact manifests cannot fall back to another download's neighboring files.
  Import, recovery, and cleanup reject paths outside staging and paths that
  cross symlinks or Windows junctions.
- Receipt reconciliation rejects stale, removed, superseded, or size-mismatched
  evidence before treating missing staged files as already placed.
- The staging sweep retries deferred neighbors of completed pinned downloads.
  Preview and apply share file selection and byte accounting; current ownership
  and fresh file metadata are checked immediately before cleanup.

### Validation

- 1,152 tests passed on Windows, including Tk heartbeat, desktop-user launch,
  placement lifecycle, junction, receipt, and cleanup-interleaving regressions.
- The packaged Windows build passed its isolated startup smoke test and a
  live upgrade with the existing configuration and database preserved.
- Normal and elevated Plex launch paths were checked against the original
  desktop-user server profile.
