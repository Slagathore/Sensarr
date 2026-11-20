"""Choose and validate the media files to import from a RAR."""

from pathlib import Path, PurePosixPath
import re
import subprocess

import shows_store
import subtitles
import torrent_routing
import verification
import video_quality
from archive_signatures import detect_archive
from media_identity import MediaIdentity
from rar_tools import RarError, RarMember


def _member_path(member: RarMember) -> PurePosixPath:
    return PurePosixPath(member.path.replace("\\", "/"))


def _key(value: str) -> str:
    return re.sub(r"[\W_]+", " ", value.casefold()).strip()


def verify_expected_members(members: tuple[RarMember, ...],
                            expected: list[tuple[str, int]]) -> None:
    for name, size in expected:
        leaf = PurePosixPath(name.replace("\\", "/")).name.casefold()
        matches = [member for member in members
                   if _member_path(member).name.casefold() == leaf
                   and (size <= 0 or member.size == size)]
        if len(matches) != 1:
            raise RarError("Real-Debrid's archive is missing an unambiguous selected video member")


def select_members(members: tuple[RarMember, ...], *,
                   want_data: dict | None, identity: MediaIdentity,
                   show_id: int | None, season: int | None,
                   episode: int | None) -> list[RarMember]:
    videos = [m for m in members if _member_path(m).suffix.lower()
              in torrent_routing.VIDEO_EXTENSIONS]
    main = [m for m in videos if verification.classify_role(
        _member_path(m), is_video=True, is_subtitle=False,
        single_video=len(videos) == 1, want=identity)
        in verification._IDENTITY_GATING_ROLES]
    pinned = str((want_data or {}).get("file_name") or "")
    pinned_path = PurePosixPath(pinned.replace("\\", "/"))
    if pinned:
        wanted = _key(pinned_path.name)
        selected = [m for m in main if _key(_member_path(m).name) == wanted]
        if len(selected) == 1:
            main = selected
    if episode is not None:
        title = next((item.title for item in shows_store.list_episodes(show_id)
                      if item.season == season and item.episode == episode), "") if show_id else ""
        title_key = _key(title)

        def matches(member: RarMember) -> bool:
            path = _member_path(member)
            parsed = verification.parse_file(path)
            if parsed.seasons and season is not None and season not in parsed.seasons:
                return False
            if parsed.episodes:
                return episode in parsed.episodes
            stem_key = _key(path.stem)
            return (bool(pinned) and stem_key == _key(pinned_path.stem)
                    or bool(title_key) and stem_key.endswith(" " + title_key))

        main = [m for m in main if matches(m)]
        if len(main) != 1:
            raise RarError("could not identify exactly one video for the requested episode")
    elif season is not None:
        slots = set()
        for member in main:
            parsed = verification.parse_file(_member_path(member))
            if parsed.seasons != (season,) or not parsed.episodes or slots.intersection(parsed.episodes):
                raise RarError("season archive episode numbers are missing, ambiguous, or for another season")
            slots.update(parsed.episodes)
    elif len(main) != 1:
        raise RarError("could not identify exactly one primary video in the archive")
    if not main:
        raise RarError("archive contains no primary video")

    stems = [_member_path(member).stem.casefold() for member in main]
    available_subs = [m for m in members
                      if _member_path(m).suffix.lower() in torrent_routing.SUBTITLE_EXTENSIONS
                      and subtitles.subtitle_language_ok(_member_path(m))]
    matched_subs = [m for m in available_subs if any(
        _member_path(m).stem.casefold() == stem
        or _member_path(m).stem.casefold().startswith(stem + ".")
        for stem in stems)]
    if len(main) == 1 and len(videos) == 1 and len(available_subs) == 1 and not matched_subs:
        candidate = available_subs[0]
        if _member_path(candidate).suffix.lower() not in torrent_routing.VOBSUB_EXTENSIONS:
            matched_subs = [candidate]
    return main + matched_subs


def output_name(member: RarMember) -> str:
    return _member_path(member).name


def verify_extracted_video(path: Path) -> None:
    if detect_archive(path):
        raise RarError(f"{path.name} is another archive")
    ffprobe = video_quality._find_ffprobe()
    if not ffprobe:
        raise RarError("ffprobe is required to verify the extracted video")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "V:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RarError(f"could not verify extracted video {path.name}") from exc
    if result.returncode or not result.stdout.strip():
        raise RarError(f"{path.name} has no readable video stream")
