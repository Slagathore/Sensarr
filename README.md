# Sensarr

**Your whole media-server stack in one Windows app.** Sensarr is a free, open source Plex companion that replaces the Sonarr + Radarr + request-portal + indexer + download-client pile with a single EXE. Your household requests shows over Telegram; Sensarr finds them, downloads them, names them properly, files them into the right library, and keeps every tracked show complete. Anime gets first-class treatment from a bundled 41,000-title offline database.

[![Latest release](https://img.shields.io/github/v/release/Slagathore/Sensarr)](../../releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.11+-3776ab)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-catnip%20fund%20%F0%9F%90%88-ff5f5f)](https://ko-fi.com/sparklemuffin)

![Demo](docs/demo.gif)

*(formerly Plexxarr, and PlexResetButton before that; old repo URLs redirect)*

## Quick start

1. Download the [latest release](../../releases/latest). The installer (`Sensarr-<ver>-Setup.exe`) is the easiest path: it installs to your user folder and adds Start Menu shortcuts. The zip has everything (unzip anywhere, run `Sensarr.exe`); the portable exe is a single file that fetches its anime database on first run. All three are code-signed (publisher: Charles Chambers; see [SIGNING.md](SIGNING.md)). Ordinary launches run without a UAC prompt.
2. The Setup Wizard opens on first run and walks you through the annoying parts: it opens [@BotFather](https://t.me/BotFather) so you can create your Telegram bot (paste the token back and it validates it live), offers one-click winget installs of Node.js and Ollama, and won't save a config that doesn't work. Telegram is skippable; the desktop app works fine on its own.
3. In Settings: add your library folders (tagged movie / tv / anime) and click **Get Plex Token**. A browser PIN page opens, you approve it, done.

No Docker, no YAML, no reverse proxy. Message your bot from the couch.

On Windows, Launch and Hard Reset start Plex as the signed-in desktop user,
including when Sensarr is explicitly run as administrator using a different
account. This keeps Plex's original registry settings, libraries, and AppData
profile. A normal desktop session must be signed in; if its identity cannot be
verified, Hard Reset stops before terminating Plex. No credentials are stored
for this launch behavior. Administrator mode can still stop a legacy Plex
instance that was started under the wrong account.

## What it does

### Requests, from anyone you allow
- A Telegram request flow that lands on an exact match, checked against TMDB, TVDB, AniDB, and AniList, and against what's already in your library, then queued for the admin with one-click actions
- Season-aware requests that tell you what you have: "5 seasons, ended. You have S1-S3." One tap grabs everything missing, airing shows get a Keep it updated button, and owned seasons are marked instead of offered. Works for TV, anime, and hentai. A movie request will never take a `S01E03` release
- Honest replies: if you request something already in the library, the bot says so instead of pretending it queued a download. Requests check the library at grab time, match numeric titles like `2073` correctly, and stop after five failed grabs instead of looping forever
- Unknown users automatically file an access request that pops up live on the Users tab. Approve or deny with a click
- Launch Plex, check status, and read play metrics from your phone. Hard reset exists too, but it's **off by default**. It force-kills Plex mid-stream, so you have to turn it on deliberately
- Watchlist and recommendation views per Plex user, filtered by what's already on disk

### A Sonarr-style show tracker
- Scans your folders, identifies every show, tracks episodes, air dates, and % missing. Sortable, filterable, resizable, with an Upcoming strip showing what airs when
- Two kinds of follow, because they're different things: *grab new releases as they air* vs *keep the show at 100%*. Flag either from a right-click
- Right-click a show to fix a wrong match, open its database entry, merge duplicates, or grab everything missing
- The parser handles the names your files actually have: `S01E05v2`, bare `E01`, `_Ep01_`, `01) Title`, `Season_1 L@mBerT`, OVAs into Specials, `[Raze] Dandadan S2 - 11`, trailing years, release-group junk. You don't have to rename anything first

### The download pipeline
- Built-in torrent search (YTS, TPB, nyaa) with its own webtorrent engine, or point it at your qBittorrent Web UI and it drives that instead
- A real queue: a few downloads active at a time, the rest waiting, and honest status labels (fetching metadata, no peers, stalled, waiting for a slot) instead of a bare 0%. A stalled download fails properly and the next pass grabs a different release. Right-click any row for stop / restart / recheck / remove / search again
- Size targets set as MB-per-minute sliders, anchored on each show's actual runtime (from the local anime database, TMDB, or ffprobe): a 24-minute anime and a 45-minute drama get different targets instead of the same guess. Defaults work out to 530 MB preferred and about 1.2 GB max for a half-hour episode
- Grab discipline: cams and telesyncs are blocked by default, releases with no seeders are skipped and rechecked later instead of grabbed, and oversized-only results wait a day before one is taken
- Renames and files everything into your library structure, keeps subtitles only in your language (multi-sub packs won't dump 30 `.srt` files on you), and recovers cleanly from crashes and interrupted moves
- Placement prompts clear when a job finishes or is canceled. Partially imported packs stay retryable with their earlier placement records intact; missing staged files are held for review instead of triggering another download or borrowing a neighboring job's files
- Every rename, replacement, move, and deletion is written to a ledger, with a missing-files view for things that vanished outside the app

### Library housekeeping
- A full, persistent inventory of every media file across all your drives
- Every file gets tied to its actual database identity (TMDB, TVDB, AniDB and friends) where one is known. Duplicates and library checks match on identity first and only fall back to filename guessing for the leftovers, which are clearly marked. One Identity Backfill button maps an existing library, and on a real 34,000 file collection it covers about 95% on its own
- A duplicate finder that actually handles movies with scene names, keeps reboots and split episodes (S01E01a vs S01E01b) apart, and remembers when you say a pair is not a duplicate. Delete the copies you don't want to the recycle bin from the same screen
- A low-quality scan that finds cams and low-bitrate files (worst first), flags copies made redundant by a better file, and replaces a cam with a proper release in one click; the old file is recycled only after the new one lands
- The index refuses a rescan that would wipe most of it, so an unmounted drive can't nuke your library records
- Subtitle fetching for anything missing them, in the language you pick

### The offline anime database
- Ships with a 41,537-title anime database built from the [manami-project](https://github.com/manami-project/anime-offline-database), [Fribb/anime-lists](https://github.com/Fribb/anime-lists), and [Anime-Lists](https://github.com/Anime-Lists/anime-lists) datasets: titles, synonyms, episode counts, runtimes, and AniDB / TVDB / TMDB / MAL / AniList id mappings. SQLite with full-text search, about 1 ms per lookup. No API keys, works offline
- The database is prebuilt weekly by CI and published as a release artifact, so your install downloads one checksummed file instead of hammering the upstream projects. If the artifact is unreachable, or you set `ANIME_DB_MANIFEST_ENABLED=0` because you'd rather build from the sources yourself, the local build path still works
- Fixes the two classic anime-library headaches in both directions (absolute numbering vs Season folders, and TMDB's merged-cour "one long season 1") using curated per-show mappings rather than guesses
- Hentai support exists but is an opt-in checkbox (wizard or Settings), off by default. Turn it on and it gets its own library type, size sliders, request button, and search source; leave it off and it never appears

### The boring parts
- Everything is persistent. Scans, indexes, maintenance results, recommendations: cached to disk, diffed on refresh, pre-warmed by an overnight idle pass. The app never re-parses 30,000 files because you restarted it
- One-click health check (Plex reachable, token valid, dependencies present, disk space), update checks for both Plex and the app, Task Scheduler autostart, a single-instance lock, and orphan-process cleanup after crashes
- Grabs are matched against the show they're for before anything gets renamed, so an unrelated torrent can't be stamped with your show's name and moved into its folder

## Why not just run the *arr stack?

If Sonarr + Radarr + Prowlarr + Overseerr + qBittorrent behind Docker fits your life, run it; it's the power-user standard for a reason. Sensarr is for the other case: one Windows machine, Plex already on it, and no appetite for maintaining five services to automate one library. One EXE, one wizard, and the pieces already know about each other. If you're attached to qBittorrent, Sensarr will use it as the download engine.

<details>
<summary><b>Screenshots</b></summary>

**Shows tracker**
![Shows tab](docs/shows.png)

**Downloads: queue, routes, and the before/after ledger**
![Downloads tab](docs/downloads.png)

**Status: server vitals and live activity**
![Status tab](docs/status.png)

**Library: inventory and quality tools**
![Library tab](docs/library.png)

**Size preferences (MB/min, anchored to real runtimes)**
![Settings](docs/settings.png)

**Setup wizard**
![Wizard](docs/wizard.png)

**The Telegram request flow: search, fix-a-match, queue**

<img src="docs/telegram.png" alt="Telegram request flow" width="600">

</details>

## Privacy

Everything runs and stays on your machine. No server of ours, no accounts, no telemetry. Your config lives in a local `.env` (git-ignored); your request, show, and download history live in local SQLite files next to the app. The network connections Sensarr makes, each only when the feature is used: the Telegram Bot API (your bot), your own Plex server and plex.tv (auth, watchlists), the metadata sources (TMDB/TVDB keys optional; AniList/Jikan/AniDB for anime), torrent indexers when a search runs, a public tracker-list refresh, and the weekly anime-database rebuild from GitHub. The optional LLM categorization runs on your own Ollama with a ~1 GB local model by default (`gemma3:1b`); request text never leaves your machine unless you deliberately configure a `:cloud` model tag.

## Disclaimer

Sensarr is an unofficial tool, not affiliated with or endorsed by Plex, Inc. (or Telegram, TMDB, TVDB, AniDB, AniList, or any indexer). Plex is a trademark of Plex, Inc.; the name appears here only to say what the app works with. It includes a general-purpose BitTorrent client and searches public indexers. BitTorrent is a neutral protocol, and you are solely responsible for what you search for, download, and share. Use it for content you have the rights to, in accordance with the laws where you live. The software is provided as is, with no warranty of any kind.

## Running from source

The packaged desktop app includes Python. From source, install Python 3.11+. The built-in download engine uses Node.js (`npm install` inside `torrent_runner/`; the wizard can do this for you). Automatic RAR import uses 7-Zip and FFmpeg's `ffprobe`. On Linux, put `7zz` or `7z` and `ffprobe` on PATH. On Windows, Sensarr also finds a standard 7-Zip installation.

```powershell
git clone https://github.com/Slagathore/Sensarr.git
cd Sensarr
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py     # first run opens the Setup Wizard
```

Build your own EXE with `build_exe.bat` (needs `pip install -r requirements-build.txt`, which is just PyInstaller). Output lands in `dist\<timestamp>\Sensarr\Sensarr.exe`. Register autostart-at-login with `setup_autostart.bat`; remove it with `remove_autostart.bat`.

For a source checkout, point desktop shortcuts at `start_sensarr.bat`, the same
launcher used by the logon task. It selects the newest timestamped build with
both `Sensarr.exe` and a staged `.env`, so a rebuilt app is picked up without
replacing the shortcut. Run `start_sensarr.bat --print` to check which build it
will open. Autostart runs after the registered Windows user signs in.

See [CHANGELOG.md](CHANGELOG.md) for the changes and validation notes.

### Linux

Ubuntu LTS (x86_64, Python 3.11+) is the reference target; other distros should work but are unverified until exercised. Install the OS packages first, then the Python side:

```bash
sudo apt install python3-tk ffmpeg xdg-utils
git clone https://github.com/Slagathore/Sensarr.git
cd Sensarr
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python main.py     # first run opens the Setup Wizard
```

Node.js 20+ enables the built-in download engine, same as Windows (`npm install` inside `torrent_runner/`, or use the wizard button). A few things work differently on Linux by design:

- Data follows the XDG directories instead of sitting beside the code: `.env` in `~/.config/sensarr`, databases in `~/.local/share/sensarr`, caches in `~/.cache/sensarr`, downloads staging in `~/.local/share/sensarr/downloads` unless you set `TORRENT_DOWNLOAD_DIR`. An existing install that kept files beside the code gets a one-time migration offer with a full dry run before anything moves.
- The app never runs your package manager and never asks for root. The wizard's "Check Linux dependencies" button names anything missing and prints the commands for you to run.
- The tray icon needs an X11/XWayland session (python-xlib installs with the requirements). Without one the window still works and says "tray unavailable"; closing the window minimizes instead of hiding.
- Local Plex buttons (Launch, Hard Reset) only enable when a local Plex install or process is found, or `PLEX_MEDIA_SERVER_PATH` points at a real executable. A remote-only Plex still gets every API feature.
- Self update is off. Update a source checkout with `git pull`; replace a packaged build with the new artifact from the release page (CI builds one per push: the `sensarr-linux` artifact from the linux-smoke workflow).

<details>
<summary><b>Configuration (.env)</b></summary>

Copy `.env.example` to `.env`. Everything below is also editable from the Settings tab, which writes back to `.env`. The file is git-ignored and never leaves your machine.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | No* | - | Bot token from @BotFather (*required for the bot; the desktop app runs without it) |
| `TELEGRAM_ALLOWED_USER_IDS` | No | - | Comma-separated Telegram user IDs always allowed |
| `TELEGRAM_HARD_RESET_ENABLED` | No | `false` | Expose Hard Reset in the bot (off on purpose) |
| `PLEX_MEDIA_SERVER_PATH` | No | standard install path | Full path to `Plex Media Server.exe` |
| `PLEX_SERVER_URL` | No | `http://127.0.0.1:32400` | Local Plex URL |
| `PLEX_TOKEN` | No | - | Filled automatically by the in-app PIN flow |
| `MEDIA_LIBRARY_PATHS` | No | - | Typed library folders (edit via Settings) |
| `TMDB_API_KEY` / `TVDB_API_KEY` | No | - | Free keys; enable movie/TV lookups and richer matching |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | No | `localhost:11434` / `gemma3:1b` | Optional local LLM for request categorization |
| `OLLAMA_THINK` / `OLLAMA_SHOW_THINKING` | No | `false` / `false` | Reasoning (`false`/`true`/`low`/`medium`/`high`) on thinking-capable models like `kimi-k2.7-code:cloud`; log it or not |
| `QBITTORRENT_ENABLED` / `_URL` / `_USERNAME` / `_PASSWORD` | No | `false` | Use your qBittorrent as the download engine |
| `TORRENT_DOWNLOAD_DIR` | No | `torrent_staging/` | Staging folder before files move into libraries |
| `ANIME_DB_MANIFEST_ENABLED` | No | `true` | Pull the prebuilt weekly anime DB artifact; set `0` to always build locally from the upstream sources |
| `SIZE_PREF_MB_PER_MIN_*` | No | movies `10`, episodic `22.1` | Preferred size per type (530 MB per 24-min episode) |
| `SIZE_MAX_MB_PER_MIN_*` | No | movies `0`, episodic `41` | Hard ceiling per type (1.2 GB per 30-min episode; 0 = no cap) |
| `BLOCK_CAMS` | No | `true` | Never auto-grab cam/telesync releases |
| `SUBTITLE_LANGUAGE` | No | `en` | The only subtitle language kept from multi-sub packs |
| `XANIME_ENABLED` | No | `false` | Hentai libraries, requests, and search (opt-in) |
| `SHOWS_AUTO_GRAB` | No | `false` | Global auto-download of missing episodes (per-show flags work regardless) |
| `MAX_ACTIVE_DOWNLOADS` | No | `4` | Concurrent downloads; the rest queue |

(Plus timing and limit knobs; `.env.example` is fully annotated.)

</details>

<details>
<summary><b>Telegram commands</b></summary>

| Command | Description |
| --- | --- |
| `/start` | Welcome message + inline keyboard |
| `/help` | Command overview |
| `/launch` | Start Plex Media Server |
| `/status` | Is Plex running? |
| `/request <text>` | Start a guided watch request |
| `/requests` | Show the open request queue |
| `/search <title>` | Search the media library |
| `/libraries` | Library summary |
| `/metrics` | Play counts, watch time, sessions |
| `/reindex` | Rebuild the filesystem library index |
| `/hardreset` | Force-kill and relaunch Plex, only if enabled in Settings; asks for confirmation |

</details>

<details>
<summary><b>Architecture</b></summary>

```
main.py              Entry point: launch-mode logging → single-instance lock → app
desktop_app.py       Tkinter GUI + tray icon; owns the app lifecycle
bot.py               Telegram command/callback handlers (async)
telegram_service.py  Runs python-telegram-bot on a background thread
request_flow.py      The guided Telegram request conversation (season picker etc.)
request_intake.py    Request creation, identity resolution, dedup
plex_control.py      Launch / status / hard reset (taskkill)
plex_api.py          Plex HTTP API: metrics, sessions, accounts, watchlists, recs
plex_auth.py         Plex PIN/token flow (no extra deps)
health.py            Health checks + Plex/app update checks
updater.py           Self-update against GitHub releases
library_index.py     SQLite filesystem indexer + file-change ledger
library_identity.py  Per-file identity map (which database entry is this file?)
media_identity.py    Title/year/sequel identity comparison rules
maintenance.py       Duplicate finder, sanitize, junk clean, library checks
maint_jobs.py        Background job registry for maintenance tools
dupe_ignore.py       Your "not a duplicate" verdicts, persisted
queue_store.py       Request queue store
auth_store.py        Telegram allowlist + access requests
torrent_search.py    Indexer search (YTS / TPB / nyaa)
torrent_select.py    Release gating and scoring (seeders, size, identity)
torrent_routing.py   Release-name parsing + destination planning
download_manager.py  Queue pump, grab logic, size math, post-processing
downloads_store.py   Download rows + grab-deferral state
torrent_runner/      Headless Node webtorrent downloader
shows_store.py       Tracked shows / episodes / flags / runtimes
show_tracker.py      Folder scan → identify → episode sync → missing detection
anime_db.py          Local 41k-title anime metadata DB, pulled prebuilt weekly
publish_anime_db.py  Builds the weekly anime DB artifact (runs in CI)
media_lookup.py      TMDB / TVDB / AniDB / AniList / Jikan lookups
video_quality.py     Cam detection, ffprobe bitrate/runtime probing
subtitles.py         Subtitle download (subliminal) + language filtering
watchlist_tab.py     Watchlist + recommendations UI
shows_tab.py         Shows tab UI
ui_helpers.py        Sortable trees, tooltips, smooth scrolling
app_paths.py         Where everything lives on disk (install vs data dirs)
platform_adapter.py  Windows/Linux differences (tray, paths, elevation)
config.py            .env-backed config
db.py                Shared SQLite helper (WAL)
```

</details>

## Data attribution

Anime identification runs on a local database rebuilt weekly from these community datasets. Thank you to their maintainers:

- [manami-project/anime-offline-database](https://github.com/manami-project/anime-offline-database): about 41k anime with titles, synonyms, episode counts, runtimes, seasons, and tags. Licensed [ODbL v1.0](https://opendatacommons.org/licenses/odbl/1-0/) + DbCL v1.0.
- [Fribb/anime-lists](https://github.com/Fribb/anime-lists) and [Anime-Lists/anime-lists](https://github.com/Anime-Lists/anime-lists): the community-curated AniDB / TVDB / TMDB / IMDb / MAL / AniList mapping, the same data the Plex HAMA agent and the Sonarr ecosystem rely on.
- [AniDB](https://anidb.net)'s public title dump for romaji and English coverage.

## Acknowledgements

The download pipeline is modeled on [torlink](https://github.com/baairon/torlink) by **bairon** ([bairon.dev](https://bairon.dev), MIT). The per-category source registry, the webtorrent engine choice, and stop-seeding-on-complete all follow torlink's design. torlink is an interactive terminal app, so Sensarr reimplements the pipeline headlessly rather than embedding his code, but the architecture is his. Thanks, bairon.

## Support

Like the app? You could help me get more catnip for my many, many cats; they NEED their zoomies: [ko-fi.com/sparklemuffin](https://ko-fi.com/sparklemuffin).

## Cat Tax:

<p>
  <img src="docs/cats/cat1.jpg" alt="sunbeam floof" height="220">
  <img src="docs/cats/cat2.jpg" alt="dramatic arch" height="220">
  <img src="docs/cats/cat3.jpg" alt="shelf lounger" height="220">
</p>
<p>
  <img src="docs/cats/cat4.jpg" alt="the pile" height="220">
  <img src="docs/cats/cat5.jpg" alt="sun sleeper" height="220">
  <img src="docs/cats/cat6.jpg" alt="door patrol, with dog" height="220">
  <img src="docs/cats/cat7.jpg" alt="the snuggle" height="220">
</p>

## License

MIT. See [LICENSE](LICENSE).
