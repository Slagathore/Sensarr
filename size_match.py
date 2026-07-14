# =============================================================================
# size_match.py  —  per-show "match my existing sizes" derivation (Task G)
# =============================================================================
# Deterministic algorithm, exactly per the bootstrap (section 10 item 3):
#
#   - Eligible sample: existing, readable video files of the show's episodes
#     only. Stale episodes.file_path entries, samples/extras/subtitles, and
#     zero/unknown sizes are excluded.
#   - bucket = floor(log(size_bytes) / log(1.10))  — FIXED origin, log-space.
#     (v1's "10% geometric bins" was underdetermined: a shifting bin origin
#     changes the answer; a fixed origin cannot.)
#   - Winner: highest population; tie -> the bucket whose median is nearest the
#     overall median (further tie -> the lower bucket index; fully
#     deterministic, so input order can never change the answer).
#   - Target = median of the winning bucket, converted to MB/min via the show's
#     runtime_min.
#   - Fewer than 3 eligible files -> global fallback, flagged in pick_meta.
#
# The pure math lives in pick_from_sizes(); eligible_episode_sizes() is the
# thin filesystem-facing sampler. DownloadManager caches one SizePick per show
# per pass ("cache per pass") and folds pick.meta() into the selection run's
# pool_stats so every decision explains its size rules.
# =============================================================================

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

SIZE_MODE_GLOBAL = "global"
SIZE_MODE_MATCH_LIBRARY = "match_library"

_LOG_1_10 = math.log(1.10)

# Minimum eligible files before the library is trusted over the global knobs.
MIN_SAMPLE = 3


@dataclass(frozen=True)
class SizePick:
    """The derived size preference for one show, plus everything pick_meta
    needs to explain it."""
    mb_per_min: float | None      # derived preference; None on fallback
    sample_count: int
    winning_bucket: int | None
    bucket_population: int
    median_bytes: int | None      # median of the winning bucket (the target)
    fallback_reason: str | None   # None when the derivation succeeded
    size_mode: str                # match_library | global

    @property
    def ok(self) -> bool:
        return self.mb_per_min is not None and self.mb_per_min > 0

    def meta(self) -> dict:
        """The pick_meta block recorded on every selection run for an
        override show (section 10 item 6). The 1.2x oversize deferral and the
        1.8x hard max are DISTINCT rules and both named here."""
        return {
            "size_mode": self.size_mode,
            "sample_count": self.sample_count,
            "winning_bucket": self.winning_bucket,
            "bucket_population": self.bucket_population,
            "median_bytes": self.median_bytes,
            "derived_mb_per_min": (round(self.mb_per_min, 3)
                                   if self.mb_per_min else None),
            "fallback_reason": self.fallback_reason,
            "size_rules": {
                "oversize_deferral": "1.2x preferred target defers one day",
                "hard_max": "1.8x preferred target is the hard size cap",
            },
        }


def bucket_for(size_bytes: int) -> int:
    """floor(log(size)/log(1.10)) — the fixed-origin log-space bucket."""
    return math.floor(math.log(size_bytes) / _LOG_1_10)


def _median(sorted_values: list[int]) -> float:
    """Median of a pre-sorted list (even count -> mean of the middle two)."""
    n = len(sorted_values)
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


def pick_from_sizes(sizes: list[int], runtime_min: float | None) -> SizePick:
    """The deterministic derivation over an eligible size sample.

    Pure: sorts its own copy, so permuting the input can never change the
    answer. Returns a fallback-flagged pick (mb_per_min None) when the sample
    is too small or the runtime is unknown.
    """
    eligible = sorted(int(s) for s in sizes if s and s > 0)
    count = len(eligible)
    if count < MIN_SAMPLE:
        return SizePick(
            mb_per_min=None, sample_count=count, winning_bucket=None,
            bucket_population=0, median_bytes=None,
            fallback_reason=f"only {count} eligible file(s), need {MIN_SAMPLE}",
            size_mode=SIZE_MODE_MATCH_LIBRARY)
    if not runtime_min or runtime_min <= 0:
        return SizePick(
            mb_per_min=None, sample_count=count, winning_bucket=None,
            bucket_population=0, median_bytes=None,
            fallback_reason="no known episode runtime",
            size_mode=SIZE_MODE_MATCH_LIBRARY)

    buckets: dict[int, list[int]] = {}
    for s in eligible:
        buckets.setdefault(bucket_for(s), []).append(s)

    overall_median = _median(eligible)
    top_pop = max(len(v) for v in buckets.values())
    tied = [b for b, v in buckets.items() if len(v) == top_pop]
    # Tie: the bucket whose own median sits nearest the overall median;
    # a further exact tie takes the lower bucket index (deterministic).
    winner = min(tied, key=lambda b: (abs(_median(buckets[b]) - overall_median), b))

    target = _median(buckets[winner])
    median_bytes = int(target)
    mb_per_min = (target / (1024 * 1024)) / float(runtime_min)
    return SizePick(
        mb_per_min=mb_per_min, sample_count=count, winning_bucket=winner,
        bucket_population=len(buckets[winner]), median_bytes=median_bytes,
        fallback_reason=None, size_mode=SIZE_MODE_MATCH_LIBRARY)


def eligible_episode_sizes(episodes, *, video_exts: set[str],
                           is_junk_name=None) -> list[int]:
    """Sizes of the existing, readable episode video files for one show.

    `episodes` is any iterable of rows exposing has_file/file_path (the
    shows_store EpisodeRow shape). Excluded per spec: stale file_path entries
    (missing on disk), non-video extensions (subtitles), sample/extra names,
    and zero/unknown sizes. `is_junk_name(name) -> bool` lets the caller pass
    the sample/extra classifier without this module importing it.
    """
    sizes: list[int] = []
    for ep in episodes:
        if not getattr(ep, "has_file", False):
            continue
        raw = getattr(ep, "file_path", None)
        if not raw:
            continue
        path = Path(raw)
        if path.suffix.lower() not in video_exts:
            continue
        if is_junk_name is not None:
            try:
                if is_junk_name(path.name):
                    continue
            except Exception:
                pass
        try:
            size = path.stat().st_size
        except OSError:
            continue  # stale entry — the file is gone or unreadable
        if size > 0:
            sizes.append(int(size))
    return sizes
