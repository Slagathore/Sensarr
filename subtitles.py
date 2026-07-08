# =============================================================================
# subtitles.py
# =============================================================================
# Subtitle fetching for the Library tab, built on `subliminal` (aggregates
# free providers — podnapisi, tvsubtitles, gestdown, opensubtitles.com when
# configured — no API key needed for the basic providers).
#
# subliminal is an OPTIONAL dependency: everything degrades to a clear
# "pip install subliminal" message when it's missing.
# =============================================================================

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ISO 639-1 codes shown in the Library tab's language dropdown.
LANGUAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("pt", "Portuguese"), ("ja", "Japanese"),
    ("ko", "Korean"), ("zh", "Chinese"), ("ru", "Russian"), ("ar", "Arabic"),
    ("hi", "Hindi"), ("nl", "Dutch"), ("sv", "Swedish"), ("pl", "Polish"),
)


def subtitles_available() -> bool:
    try:
        import subliminal  # noqa: F401
        return True
    except ImportError:
        return False


def download_subtitles(paths: list[str], language: str = "en",
                       progress=None) -> tuple[int, list[str]]:
    """Fetch + save subtitles next to each video. Returns (saved, errors)."""
    try:
        from babelfish import Language
        from subliminal import download_best_subtitles, save_subtitles, scan_video
    except ImportError:
        return 0, [
            "The 'subliminal' package is not installed. Install it with:\n"
            "    pip install subliminal\n"
            "then restart the app."
        ]

    try:
        lang = Language.fromietf(language)
    except Exception:
        return 0, [f"Unknown language code: {language!r}"]

    saved = 0
    errors: list[str] = []
    for i, raw in enumerate(paths):
        if progress is not None:
            progress(i, len(paths), Path(raw).name)
        try:
            video = scan_video(raw)
            subs = download_best_subtitles({video}, {lang})
            found = subs.get(video) or []
            if not found:
                errors.append(f"No {language} subtitles found: {Path(raw).name}")
                continue
            save_subtitles(video, found)
            saved += 1
        except Exception as exc:
            logger.exception("Subtitle fetch failed for %s", raw)
            errors.append(f"{Path(raw).name}: {exc}")
    return saved, errors
