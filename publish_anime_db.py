# =============================================================================
# publish_anime_db.py
# =============================================================================
# Builds the weekly anime-metadata artifact that .github/workflows/anime-db.yml
# attaches to a GitHub Release (Task I / FIX_SPRINT_BOOTSTRAP.md section I).
#
# Bundles ALL THREE sources: manami-project/anime-offline-database (ODbL
# 1.0, share-alike, attribution required — see NOTICE) plus the Fribb/
# anime-lists and Anime-Lists/anime-lists id mappings. Neither of the latter
# two publishes a license file, so there is no permission-by-license to
# redistribute their data. DECISION (Cole, 2026-07-19): bundle them anyway,
# knowingly accepting that risk, with attribution to both projects in the
# NOTICE — see NOTICE_TEXT below and .github/workflows/anime-db.yml's header
# comment. Fribb/Anime-Lists are fetched with graceful degradation: either
# (or both) being unreachable at build time still produces a valid artifact
# (manami tables alone are enough), it just ships without that source's
# mappings until the next successful weekly build.
#
# Headless, no upload logic: the workflow does the `gh release` calls. This
# script only produces the four files a release needs in --output-dir:
#   anime-db.sqlite      (uncompressed, for local inspection — not uploaded)
#   anime-db.sqlite.gz   (the artifact clients actually download)
#   latest.json          (the manifest: schema_version, built, sha256, url, bytes)
#   NOTICE                (attribution for all three sources, see NOTICE_TEXT)
#
# Run: python publish_anime_db.py --output-dir dist-anime-db
# =============================================================================

import argparse
import gzip
import hashlib
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import anime_db

DEFAULT_TAG = anime_db._MANIFEST_TAG
DEFAULT_REPO = anime_db._REPO
ARTIFACT_NAME = "anime-db.sqlite"
GZ_NAME = ARTIFACT_NAME + ".gz"
MANIFEST_NAME = "latest.json"
NOTICE_NAME = "NOTICE"

NOTICE_TEXT = """\
This file (anime-db.sqlite.gz) contains data derived from anime-offline-database
by manami-project (https://github.com/manami-project/anime-offline-database),
made available under the Open Database License (ODbL) v1.0:
https://opendatacommons.org/licenses/odbl/1-0/
Individual contents are licensed under the Database Contents License (DbCL)
v1.0: https://opendatacommons.org/licenses/dbcl/1-0/

As a derivative database, this artifact (Sensarr's weekly anime-db.sqlite.gz
release, published from github.com/{repo}) is likewise made available under
the Open Database License v1.0. You are free to share, create, and adapt it
under that license, which requires attribution and share-alike
redistribution of anything you publish derived from it.

It also contains ID mappings from Fribb/anime-lists
(https://github.com/Fribb/anime-lists) and Anime-Lists/anime-lists
(https://github.com/Anime-Lists/anime-lists), which do not declare a
license for their data. Those mappings are treated as factual data (ids are
not copyrightable expression) and are redistributed here with attribution
to their sources above — a deliberate, informed choice by the Sensarr
maintainer, not a claim that either project has licensed its data for
redistribution.

Built by: Sensarr (https://github.com/{repo})
"""


def _checkpoint_and_vacuum(db_path: Path) -> None:
    """WAL checkpoint before VACUUM: a WAL-mode SQLite file is not cleanly
    single-file until its WAL is folded back in, and a raw .sqlite copied
    mid-WAL is not a valid standalone database for a client that opens it
    directly (no matching -wal/-shm sidecars will ship in the artifact)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def package_artifact(manami: dict, output_dir: Path, *, fribb: list | None = None,
                     xml_root=None, tag: str = DEFAULT_TAG,
                     repo: str = DEFAULT_REPO) -> dict:
    """Build the FULL DB (manami + Fribb + Anime-Lists merge — Cole's
    2026-07-19 decision to bundle all three, see the module header),
    checkpoint+vacuum+gzip+hash it, and write latest.json + NOTICE into
    output_dir. Returns the manifest dict.

    fribb/xml_root default to a live fetch (anime_db._fetch_fribb /
    _fetch_anime_lists_xml, both already degrade to an empty/None result on
    their own on any failure — never raise) so a normal CI run needs no
    extra wiring. Tests pass them in directly to stay fully offline and to
    exercise the degraded (manami-only) publish path on demand.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / ARTIFACT_NAME
    db_path.unlink(missing_ok=True)

    if fribb is None:
        fribb = anime_db._fetch_fribb()
    if not fribb:
        print("  Fribb id mappings unavailable/empty — publishing without "
              "that source's mappings (degraded, not fatal).")
    if xml_root is None:
        xml_root = anime_db._fetch_anime_lists_xml()
    if xml_root is None:
        print("  Anime-Lists XML unavailable — publishing without curated "
              "season/episode offsets from that source (degraded, not fatal).")

    anime_db._build_database(manami, fribb, xml_root, db_path)
    _checkpoint_and_vacuum(db_path)

    db_bytes = db_path.read_bytes()
    gz_bytes = gzip.compress(db_bytes, compresslevel=9, mtime=0)
    gz_path = output_dir / GZ_NAME
    gz_path.write_bytes(gz_bytes)

    sha256 = hashlib.sha256(gz_bytes).hexdigest()
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema_version": anime_db._SCHEMA_VERSION,
        "built": built,
        "sha256": sha256,
        "url": f"https://github.com/{repo}/releases/download/{tag}/{GZ_NAME}",
        "bytes": len(gz_bytes),
    }
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output_dir / NOTICE_NAME).write_text(
        NOTICE_TEXT.format(repo=repo), encoding="utf-8")

    print(f"Built {ARTIFACT_NAME}: {len(manami.get('data', []))} entries, "
          f"{len(fribb)} Fribb mappings, schema {anime_db._SCHEMA_VERSION}")
    print(f"  {GZ_NAME}: {len(gz_bytes):,} bytes, sha256 {sha256}")
    print(f"  {MANIFEST_NAME} + {NOTICE_NAME} written to {output_dir}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the weekly anime-db release artifact "
                    "(manami + Fribb + Anime-Lists).")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Directory to write anime-db.sqlite(.gz)/latest.json/NOTICE into.")
    parser.add_argument("--tag", default=DEFAULT_TAG,
                        help=f"Release tag the manifest URL points at (default: {DEFAULT_TAG}).")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help=f"owner/repo the manifest URL points at (default: {DEFAULT_REPO}).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        manami = anime_db.fetch_manami()
        package_artifact(manami, args.output_dir, tag=args.tag, repo=args.repo)
    except Exception as exc:
        print(f"publish_anime_db failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
