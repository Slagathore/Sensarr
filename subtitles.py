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


# Language tokens seen in subtitle filenames / folder names, per ISO code.
_LANG_ALIASES: dict[str, set[str]] = {
    "en": {"en", "eng", "english", "en-us", "en-gb", "engl"},
    "es": {"es", "spa", "spanish", "espanol", "español", "es-la", "es-es", "lat", "latino"},
    "fr": {"fr", "fre", "fra", "french", "francais", "français"},
    "de": {"de", "ger", "deu", "german", "deutsch"},
    "it": {"it", "ita", "italian"},
    "pt": {"pt", "por", "portuguese", "pt-br", "pt-pt", "brazilian"},
    "ja": {"ja", "jpn", "japanese"},
    "ko": {"ko", "kor", "korean"},
    "zh": {"zh", "chi", "zho", "chinese", "chs", "cht", "zh-cn", "zh-tw", "mandarin"},
    "ru": {"ru", "rus", "russian"},
    "ar": {"ar", "ara", "arabic"},
    "hi": {"hi", "hin", "hindi"},
    "nl": {"nl", "dut", "nld", "dutch"},
    "sv": {"sv", "swe", "swedish"},
    "pl": {"pl", "pol", "polish"},
}
_ALL_LANG_TOKENS: set[str] = set().union(*_LANG_ALIASES.values())


def subtitle_language_ok(path, preferred: str | None = None) -> bool:
    """Should this subtitle file be kept, given the user's language setting?

    Multi-sub release packs ship .ass/.srt files for EVERY language; when a
    download is moved into the library, only the preferred language (and
    untagged subs, which are usually the release's default) come along.
    """
    import config as _config
    from pathlib import Path as _Path

    preferred = (preferred or _config.SUBTITLE_LANGUAGE or "en").casefold()
    wanted = _LANG_ALIASES.get(preferred, {preferred})

    p = _Path(path)
    # Tokens from the filename ("Show.S01E01.por.ass") and parent folders
    # ("Subs/French/…").
    tokens: set[str] = set()
    stem_parts = p.stem.replace("[", ".").replace("]", ".").replace("(", ".") \
                       .replace(")", ".").replace("_", ".").replace("-", ".").split(".")
    tokens.update(t.strip().casefold() for t in stem_parts if t.strip())
    for parent in list(p.parents)[:3]:
        tokens.add(parent.name.strip().casefold())

    found_langs = tokens & _ALL_LANG_TOKENS
    if not found_langs:
        return True  # untagged — keep (usually the release default track)
    return bool(found_langs & wanted)


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
