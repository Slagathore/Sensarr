# =============================================================================
# stage_build.py
# =============================================================================
# Prepares the newest dist\<timestamp>\Sensarr bundle for actual use:
#
#   1. Copies your .env next to the EXE (the EXE reads config from ITS OWN
#      folder), then pins APP_DB_PATH and TORRENT_DOWNLOAD_DIR to absolute
#      paths in THIS repo — so every build, and source runs, share ONE
#      database, one staging folder, one set of caches. No divergence.
#   2. Copies the offline AniDB title dump and tracker cache so the new
#      bundle doesn't need to re-download them.
#
# Run directly (python stage_build.py) or let update_sensarr.bat call it.
# =============================================================================

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def newest_bundle() -> Path | None:
    dist = REPO / "dist"
    if not dist.is_dir():
        return None
    # Only timestamp-named dirs auto-qualify: manual folders (release-signed\,
    # portable\) sort after the digits and would shadow every newer build.
    for stamp in sorted((d for d in dist.iterdir()
                         if d.is_dir() and d.name[:1].isdigit()),
                        key=lambda d: d.name, reverse=True):
        for name in ("Sensarr", "Plexxarr", "PlexResetButton"):
            exe = stamp / name / f"{name}.exe"
            if exe.is_file():
                return exe.parent
    return None


def main() -> int:
    bundle = newest_bundle()
    if bundle is None:
        print("No built bundle found under dist\\ — run build_exe.bat first.")
        return 1
    print(f"Staging bundle: {bundle}")

    src_env = REPO / ".env"
    if not src_env.is_file():
        print("No .env in the repo — nothing to stage. (Run the app from "
              "source once, or create .env from .env.example.)")
        return 1

    dest_env = bundle / ".env"
    shutil.copy2(src_env, dest_env)
    print("  copied .env")

    # Pin shared data locations so the EXE and source runs use the same
    # database/staging no matter where the bundle lives.
    try:
        from dotenv import dotenv_values, set_key
        current = dotenv_values(str(dest_env))
        db_path = current.get("APP_DB_PATH") or ""
        if not Path(db_path).is_absolute():
            set_key(str(dest_env), "APP_DB_PATH",
                    str(REPO / "plex_reset_button.db"), quote_mode="auto")
            print(f"  pinned APP_DB_PATH -> {REPO / 'plex_reset_button.db'}")
        staging = current.get("TORRENT_DOWNLOAD_DIR") or ""
        if not staging or not Path(staging).is_absolute():
            set_key(str(dest_env), "TORRENT_DOWNLOAD_DIR",
                    str(REPO / "downloads"), quote_mode="auto")
            print(f"  pinned TORRENT_DOWNLOAD_DIR -> {REPO / 'downloads'}")
    except ImportError:
        print("  WARNING: python-dotenv missing — could not pin APP_DB_PATH; "
              "the bundle will use its own fresh database!")

    for cache in ("anidb_titles.dat.gz", "trackers_cache.txt",
                  "anime_meta.sqlite", "maintenance_cache.json",
                  "library_lowqual.json", "watchlist_recs.json",
                  "unidentified_folders.json"):
        src = REPO / cache
        if src.is_file():
            shutil.copy2(src, bundle / cache)
            print(f"  copied {cache}")
    # Clear any partially-built anime DB from a previous bundle launch
    # (best-effort — a still-running instance may hold it).
    try:
        (bundle / "anime_meta.building").unlink(missing_ok=True)
    except OSError as exc:
        print(f"  note: couldn't remove partial anime DB ({exc}) — harmless")

    print("\nStaged. Launch:")
    print(f"  {bundle / (bundle.name + '.exe')}")
    print("Then (once, elevated): setup_autostart.bat to repoint autostart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
