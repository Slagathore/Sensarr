# Plexxarr

**Your whole media-server stack in one Windows app.** Plexxarr is a free, open-source Plex companion that replaces the Sonarr + Radarr + request-portal + indexer + download-client pile with a single EXE: your household requests shows over Telegram, Plexxarr finds them, downloads them, names them properly, files them into the right library, and keeps every tracked show complete — with best-in-class anime handling powered by a local 41,000-title database.

[![Latest release](https://img.shields.io/github/v/release/Slagathore/Plexxarr)](../../releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.11+-3776ab)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-catnip%20fund%20%F0%9F%90%88-ff5f5f)](https://ko-fi.com/sparklemuffin)

![Demo](docs/demo.gif)

*(formerly PlexResetButton — the old repo URL redirects)*

## Quick start

1. Download the [latest release](../../releases/latest), unzip anywhere, run `Plexxarr.exe` (accept the UAC prompt — it needs it to manage Plex).
2. The **Setup Wizard** opens on first run and does the annoying parts *with* you: it opens [@BotFather](https://t.me/BotFather) so you can create your Telegram bot (paste the token back and it validates it live), offers one-click `winget` installs of Node.js and Ollama, and won't save a config that doesn't work. Telegram is skippable — the desktop app is fully usable on its own.
3. In **Settings**: add your library folders (tagged movie / tv / anime / …) and click **Get Plex Token** (a browser PIN page opens; approve it, done).

That's it. No Docker, no YAML, no reverse proxy, no sacrificial offering to the compose gods. Message your bot from the couch.

## What it does

### 📱 Requests, from anyone you allow
- **Telegram request flow** that guides users to an exact match — resolved against TMDB, TVDB, AniDB, and AniList, checked against what's already in your library, and queued with a link the admin can act on
- **Season-aware grabbing** — an episodic request finishes the latest season you own, then moves to the next aired one; a brand-new show starts at season 1; a movie request will *never* take a `S01E03` release
- **Allowlist with zero friction** — unknown users automatically file an access request that pops up live on the Users tab; approve or deny with a click
- **Server control from your phone** — launch Plex, check status, see play metrics; hard reset is there too but **off by default** (it force-kills Plex mid-stream, so it's opt-in in Settings)
- **Watchlists & recommendations** — pulls each user's Plex watchlist and builds genre-aware suggestions, filtered by what's already on disk

### 📺 A Sonarr-style show tracker
- Scans your folders, identifies every show, tracks episodes, air dates, and **% missing** — sortable, filterable, resizable, with an **Upcoming** strip showing exactly what airs when (silence any show; one click to restore)
- **Two kinds of follow**, because they're different things: 🆕 *grab new releases as they air* vs ✅ *keep the show at 100%* — flag either from a right-click
- **Right-click everything** — fix a wrong match, jump to its database entry, merge duplicates, sanitize-and-push names, grab all missing
- **Parses real-world naming** — `S01E05v2`, bare `E01`, `_Ep01_`, `01) Title`, `Season_1 L@mBerT`, OVAs into Specials, `[Raze] Dandadan S2 - 11`, release-group junk, trailing years… the parser meets your files where they are instead of demanding you rename them

### ⬇️ A download pipeline that thinks
- **Built-in torrent search** (YTS, TPB, nyaa, sukebei) with its own webtorrent engine — or point it at your **qBittorrent** Web UI and it drives that instead
- **A real queue**: a few active at a time, the rest waiting, stalled downloads rotated out automatically; right-click any row for stop / restart / recheck / remove (± files) / search again
- **Size preferences that mean something** — you set MB-per-minute targets and caps with sliders; grabs are anchored to each show's **real runtime** (from the local anime DB, TMDB, or ffprobe), so a 24-min anime and a 45-min drama get honest targets, not the same guess. Defaults to ~530 MB per 24-min episode
- **Grab discipline** — cam/telesync releases blocked by default; when everything on offer is oversized it waits a day before settling; when nothing has seeders it races five candidates for an hour and keeps the winner
- **Renames and files everything** into your library structure, fetches subtitles in *your* language (multi-sub packs won't flood you with 30 `.srt`s), and recovers cleanly from crashes and interrupted moves
- **A file-change ledger** — every rename, replacement, move, and deletion in your libraries is logged with who/what did it, plus a missing-files view for things that vanished outside the app

### 🗃️ Library housekeeping
- Full inventory of every media file across all your drives, searchable and filterable
- **Low-quality scan** — finds cams and low-bitrate files (worst first), flags copies that are redundant because a better file already exists, and can **replace a cam with a proper release in one click** — the old file is recycled only after the new one lands
- Subtitle fetching for anything missing them, in the language you pick

### 🧠 A local-first anime brain
- Ships with a **41,537-title anime database** (built from the excellent [manami-project](https://github.com/manami-project/anime-offline-database), [Fribb/anime-lists](https://github.com/Fribb/anime-lists), and [Anime-Lists](https://github.com/Anime-Lists/anime-lists) datasets): titles, synonyms, episode counts, runtimes, and AniDB ↔ TVDB ↔ TMDB ↔ MAL ↔ AniList id mappings — SQLite + full-text search, ~1 ms lookups, refreshed weekly, **zero API keys, works offline**
- Fixes the two classic anime-library nightmares in *both* directions: absolute numbering vs Season folders, and TMDB's "one long season 1" merged-cour lists — using curated per-show mappings, not guesswork
- Hentai handled as a first-class type with its own library routing, size prefs, and metadata (it's your server; we don't judge)

### 🔩 The boring-but-important parts
- **Everything is persistent.** Scans, indexes, maintenance results, recommendations — cached to disk, diffed on refresh, pre-warmed by an overnight idle pass. You will never watch it re-parse 30,000 files because you restarted the app
- One-click **health check** (Plex reachable, token valid, dependencies present, disk space), Plex + app update checks, Task Scheduler autostart, single-instance lock, orphan-process cleanup on startup
- **Won't wreck your library**: grabs are matched against the show they're for before anything is renamed — an unrelated torrent can't get stamped with your show's name and moved into its folder

## Why not just run the *arr stack?

If Sonarr + Radarr + Prowlarr + Overseerr + qBittorrent behind Docker fits your life, run it — it's the power-user gold standard and nothing here is a knock on it. Plexxarr is for the other 90%: **one Windows machine, Plex already on it, and no appetite for maintaining five services to automate it.** One EXE, one wizard, and the pieces already know about each other. (Already attached to qBittorrent? Plexxarr will happily use it as the download engine.)

<details>
<summary><b>📸 Screenshots</b></summary>

**Shows tracker**
![Shows tab](docs/shows.png)

**Downloads queue**
![Downloads tab](docs/downloads.png)

**Status — server vitals + live activity**
![Status tab](docs/status.png)

**Telegram request flow**
![Telegram](docs/telegram.png)

**Setup wizard**
![Wizard](docs/wizard.png)

**Size preferences**
![Settings](docs/settings.png)

</details>

## Privacy

**Everything runs and stays on your machine.** No server of ours, no accounts, no telemetry, no analytics. Your config lives in a local `.env` (git-ignored), your request/show/download history in local SQLite files next to the app. The only network connections Plexxarr makes, and only when the feature is used: the Telegram Bot API (your bot), your own Plex server + plex.tv (auth/watchlists), the metadata sources you enable (TMDB/TVDB keys are optional; AniList/Jikan/AniDB for anime), torrent indexers when a search runs, a public tracker list refresh, and the weekly anime-database rebuild from GitHub. The optional LLM categorization runs on **your** Ollama with a ~1 GB local model by default (`gemma3:1b`) — request text never leaves your machine unless you deliberately configure a `:cloud` model tag.

## Disclaimer

Plexxarr is an **unofficial** tool and is not affiliated with, endorsed by, or connected to Plex, Inc. (or Telegram, TMDB, TVDB, AniDB, AniList, or any indexer). It includes a general-purpose BitTorrent client and searches public indexers; BitTorrent is a neutral protocol, and **you are solely responsible for what you search for, download, and share**. Use it for content you have the rights to — your own media, public-domain and freely-licensed works, and content licensed for you to obtain this way, in accordance with the laws where you live. The software is provided **as is**, with no warranty of any kind.

## Running from source

Packaged builds need nothing installed. From source: Python 3.11+, plus Node.js only if you use the built-in download engine (`npm install` inside `torrent_runner/`; the wizard can do all of this for you).

```powershell
git clone https://github.com/Slagathore/Plexxarr.git
cd Plexxarr
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py     # first run opens the Setup Wizard
```

Build your own EXE with `build_exe.bat` (needs `pip install -r requirements-build.txt`, just PyInstaller) — output lands in `dist\<timestamp>\Plexxarr\Plexxarr.exe`. Register autostart-at-login (highest privilege, no UAC prompt) with `setup_autostart.bat`; remove with `remove_autostart.bat`.

<details>
<summary><b>⚙️ Configuration (.env)</b></summary>

Copy `.env.example` to `.env`. Everything below is also editable from the **Settings tab**, which writes back to `.env`. The file is git-ignored and never leaves your machine.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | No* | — | Bot token from @BotFather (*required for the bot; the desktop app runs without it) |
| `TELEGRAM_ALLOWED_USER_IDS` | No | — | Comma-separated Telegram user IDs always allowed |
| `TELEGRAM_HARD_RESET_ENABLED` | No | `false` | Expose Hard Reset in the bot (off by default on purpose) |
| `PLEX_MEDIA_SERVER_PATH` | No | standard install path | Full path to `Plex Media Server.exe` |
| `PLEX_SERVER_URL` | No | `http://127.0.0.1:32400` | Local Plex URL |
| `PLEX_TOKEN` | No | — | Filled automatically by the in-app PIN flow |
| `MEDIA_LIBRARY_PATHS` | No | — | Typed library folders (edit via Settings) |
| `TMDB_API_KEY` / `TVDB_API_KEY` | No | — | Free keys; enable movie/TV lookups and richer matching |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | No | `localhost:11434` / `gemma3:1b` | Optional local LLM for request categorization |
| `QBITTORRENT_ENABLED` / `_URL` / `_USERNAME` / `_PASSWORD` | No | `false` | Use your qBittorrent as the download engine |
| `TORRENT_DOWNLOAD_DIR` | No | `downloads/` | Staging folder before files are moved into libraries |
| `SIZE_PREF_MB_PER_MIN_*` | No | movies `10`, episodic `22.1` | Preferred size per type (≈530 MB per 24-min episode) |
| `SIZE_MAX_MB_PER_MIN_*` | No | movies `0`, episodic `22.1` | Hard ceiling per type (0 = no cap) |
| `BLOCK_CAMS` | No | `true` | Never auto-grab cam/telesync releases |
| `SUBTITLE_LANGUAGE` | No | `en` | The only subtitle language kept from multi-sub packs |
| `SHOWS_AUTO_GRAB` | No | `false` | Global auto-download of missing episodes (per-show flags work regardless) |
| `MAX_ACTIVE_DOWNLOADS` | No | `4` | Concurrent downloads; the rest queue |

(Plus timing/limit knobs — `.env.example` is fully annotated.)

</details>

<details>
<summary><b>💬 Telegram commands</b></summary>

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
| `/hardreset` | Force-kill + relaunch Plex — only if enabled in Settings; asks for confirmation |

</details>

<details>
<summary><b>🏗️ Architecture</b></summary>

```
main.py              Entry point — UAC elevation gate → single-instance lock → app
desktop_app.py       Tkinter GUI + tray icon; owns the app lifecycle
bot.py               Telegram command/callback handlers (async)
telegram_service.py  Runs python-telegram-bot on a background thread
plex_control.py      Launch / status / hard reset (taskkill)
plex_api.py          Plex HTTP API — metrics, sessions, accounts, watchlists
plex_auth.py         Plex PIN/token flow (no extra deps)
health.py            Health checks + Plex/app update checks
library_index.py     SQLite filesystem indexer + file-change ledger
queue_store.py       Request queue store
auth_store.py        Telegram allowlist + access requests
torrent_search.py    Indexer search (YTS / TPB / nyaa / sukebei)
torrent_routing.py   Release-name parsing + destination planning
download_manager.py  Queue pump, grab logic, size math, post-processing
downloads_store.py   Download rows + grab-deferral state
torrent_runner/      Headless Node webtorrent downloader
shows_store.py       Tracked shows / episodes / flags / runtimes
show_tracker.py      Folder scan → identify → episode sync → missing detection
anime_db.py          Local 41k-title anime metadata DB (manami + Fribb + Anime-Lists)
media_lookup.py      TMDB / TVDB / AniDB / AniList / Jikan lookups
video_quality.py     Cam detection, ffprobe bitrate/runtime probing
subtitles.py         Subtitle download (subliminal) + language filtering
watchlist_tab.py     Watchlist + recommendations UI
shows_tab.py         Shows tab UI
ui_helpers.py        Sortable trees, tooltips, smooth scrolling
config.py            .env-backed config
db.py                Shared SQLite helper (WAL)
```

</details>

## Data attribution

Anime identification runs on a **local database** rebuilt weekly from these community datasets — thank you to their maintainers:

- [manami-project/anime-offline-database](https://github.com/manami-project/anime-offline-database) — ~41k anime: titles, synonyms, episode counts, runtimes, seasons, tags. Licensed [ODbL v1.0](https://opendatacommons.org/licenses/odbl/1-0/) + DbCL v1.0.
- [Fribb/anime-lists](https://github.com/Fribb/anime-lists) and [Anime-Lists/anime-lists](https://github.com/Anime-Lists/anime-lists) — the community-curated AniDB ↔ TVDB ↔ TMDB ↔ IMDb ↔ MAL ↔ AniList mapping (the same data the Plex HAMA agent and the Sonarr ecosystem rely on).
- [AniDB](https://anidb.net)'s public title dump for romaji/English coverage.

## Acknowledgements

The download pipeline is modeled on [**torlink**](https://github.com/baairon/torlink) by **bairon** ([bairon.dev](https://bairon.dev), MIT) — the per-category source registry, the webtorrent engine choice, and stop-seeding-on-complete all follow torlink's design. torlink is an interactive terminal app, so Plexxarr reimplements the pipeline headlessly rather than embedding his code — but the architecture is his. Thanks, bairon. 🙏

## Support

😺 If Plexxarr keeps your movie nights running, you could help me get more catnip for my many, many cats — they NEED their zoomies: [buy me Half a cup of coffee](https://ko-fi.com/sparklemuffin). Tarriffs amirite. 🥲

## License

MIT — see [LICENSE](LICENSE).
