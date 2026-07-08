# Plexxarr

*(formerly PlexResetButton — the old repo URL redirects)*

[![Ko-fi](https://img.shields.io/badge/Ko--fi-half%20a%20coffee%20%E2%98%95-ff5f5f)](https://ko-fi.com/sparklemuffin)

A Windows system-tray application and Telegram bot that lets you remotely **launch, hard-reset, monitor, and manage Plex Media Server** from your phone — plus a Sonarr/Radarr-style request queue, show tracker, and torrent download pipeline, all in one app.

```
Phone (Telegram) ──► Telegram Bot API ──► PlexResetButton.exe ──► Plex Media Server
```

---

## Features

| Category             | Details                                                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Telegram control** | Inline keyboard + slash commands: Launch, Status, Metrics, Requests. Hard Reset is opt-in (Settings tab).    |
| **Hard reset**       | Force-kills every Plex process tree via `taskkill /F` (runs elevated; never fails silently)                  |
| **Request queue**    | Telegram users are guided through a library-aware, DB-linked request flow (TMDB/TVDB/AniDB/AniList)          |
| **Shows tracker**    | Sonarr-style: scan library folders, identify shows, track episodes/air dates, auto-grab missing episodes     |
| **Downloads**        | In-app torrent search (YTS/TPB/nyaa/sukebei), webtorrent downloads, automatic rename + routing to libraries  |
| **Users**            | Telegram allowlist with in-app approval of access requests (updates live when a request arrives)             |
| **Health**           | One-click server health check, Plex + app update check, dependency and disk-space issues surfaced            |
| **Library index**    | Indexes your media folders to SQLite for fast search; fallback to Plex API search                            |
| **Play metrics**     | Per-user play counts, watch time, active sessions, and library section inventory                             |
| **Plex PIN auth**    | Built-in browser-based PIN flow to obtain and persist your Plex token                                        |
| **Auto-start**       | One-click Task Scheduler registration at highest privilege level                                             |
| **EXE build**        | Single-folder PyInstaller bundle via `build_exe.bat`                                                         |

---

## Architecture

```
main.py              Entry point — UAC elevation gate → starts desktop_app
desktop_app.py       Tkinter GUI + pystray tray icon; owns the app lifecycle
bot.py               All Telegram command/callback handlers (async)
telegram_service.py  Runs the python-telegram-bot Application on a background thread
plex_control.py      Hard reset (taskkill), launch, status
plex_api.py          Plex HTTP API client — metrics, sessions, library inventory
plex_auth.py         Plex PIN/token authentication flow (urllib, no extra deps)
health.py            Health checks + Plex/app update checks (Status tab)
library_index.py     SQLite-backed filesystem media indexer and searcher
metrics_report.py    Formats combined metrics message for bot and desktop
queue_store.py       SQLite request queue — add, complete, list, count
auth_store.py        Telegram allowlist + access requests (admin approval)
db.py                Shared SQLite connection helper (WAL, busy timeout)
torrent_search.py    In-app torrent search (YTS / TPB / nyaa / sukebei)
torrent_routing.py   Destination planner — show/season folders, safe renames
download_manager.py  Torrent downloads via Node webtorrent runner + history
downloads_store.py   Downloads + before/after history tables
torrent_runner/      Headless Node webtorrent downloader (npm install once)
shows_store.py       Tracked-show inventory: shows, folders, episode air dates
show_tracker.py      Folder scan → tracker identify → episode sync → missing
shows_tab.py         Shows tab UI (Sonarr-style; first split-out tab module)
ui_helpers.py        Shared Tk helpers: sortable tree columns, busy spinner
config.py            Centralised config: reads .env, validates required keys
app_logging.py       Dual log handler: stderr stream + in-memory ring buffer
```

---

## Setting up on a fresh computer

What a new user needs, assuming they got a built EXE folder (`PlexResetButton/` from `dist\`):

1. **Windows 10/11 (64-bit)** with **Plex Media Server** installed locally.
2. **First run**: put the app folder anywhere, run `PlexResetButton.exe` (accept the UAC prompt). With no bot token configured, the **Setup Wizard opens automatically** and walks you through everything below — including one-click winget installs of Node.js and Ollama, and validating your bot token against Telegram before saving it.
3. **Telegram bot**: the wizard opens [@BotFather](https://t.me/BotFather) for you — send `/newbot`, paste the token back into the wizard. (Telegram requires a human to create the bot; there's no API for it.)
4. **In the app**: Settings tab → add your library folders (tagged movie/tv/anime/…) → Save. Click **Get Plex Token** to run the browser PIN flow (persists automatically).
5. **Optional — for request lookups**: free API keys for [TMDB](https://www.themoviedb.org/settings/api) and [TVDB](https://thetvdb.com/api-information) in Settings. Anime identification works offline out of the box (AniDB title dump downloads automatically).
6. **Optional — downloads**: the wizard can install Node.js and run `npm install` for the torrent runner. Prefer qBittorrent? Enable it in Settings (Web UI URL + login) and downloads delegate to it instead.
7. **Optional**: `setup_autostart.bat` to launch at login without UAC prompts.
8. Message your bot on Telegram. Unknown users automatically file an access request that appears instantly on the **Users** tab.

Use the **Status tab → 🩺 Health Check** button to verify every dependency at once.

### Running from source instead

```powershell
git clone https://github.com/Slagathore/Plexxarr.git
cd Plexxarr
python -m pip install -r requirements.txt   # Python 3.11+
Copy-Item .env.example .env ; notepad .env  # paste your bot token
python main.py
```

---

## Configuration — `.env`

Copy `.env.example` to `.env` and edit the values. The `.env` file is git-ignored; it never leaves your machine. Everything except the bot token can also be edited from the **Settings tab**, which writes back to `.env`.

| Variable                       | Required | Default                                                         | Description                                                 |
| ------------------------------ | -------- | --------------------------------------------------------------- | ----------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`           | **Yes**  | —                                                               | Bot token from @BotFather                                   |
| `TELEGRAM_ALLOWED_USER_IDS`    | No       | —                                                               | Comma-separated Telegram user IDs always allowed            |
| `TELEGRAM_HARD_RESET_ENABLED`  | No       | `false`                                                         | Show the Hard Reset button/command in the Telegram bot      |
| `PLEX_MEDIA_SERVER_PATH`       | No       | `C:\Program Files\Plex\Plex Media Server\Plex Media Server.exe` | Full path to the Plex executable                            |
| `PLEX_SERVER_URL`              | No       | `http://127.0.0.1:32400`                                        | Local Plex server URL                                       |
| `PLEX_TOKEN`                   | No       | —                                                               | Plex authentication token (obtain via the in-app auth flow) |
| `MEDIA_LIBRARY_PATHS`          | No       | —                                                               | Typed library folders (edit via Settings tab)               |
| `TMDB_API_KEY` / `TVDB_API_KEY`| No       | —                                                               | Free keys for movie/TV request lookups                      |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | No       | `localhost:11434`                                               | Optional LLM for request categorization                     |
| `TORRENT_DOWNLOAD_DIR`         | No       | `downloads/`                                                    | Torrent staging folder                                      |
| `SIZE_PREF_MB_PER_MIN_*`       | No       | `0`                                                             | Preferred download size per type (MB per minute; 0 = off)   |
| `SHOWS_AUTO_GRAB`              | No       | `false`                                                         | Auto-download missing episodes of tracked shows             |

(Plus timing/limit knobs — see `.env.example` for the full annotated list.)

---

## Building the EXE

```bat
build_exe.bat
```

Output lands in `dist\<timestamp>\PlexResetButton\PlexResetButton.exe`.

Requirements: `python -m pip install -r requirements-build.txt` (just PyInstaller).

---

## Auto-start on Login

```bat
setup_autostart.bat
```

Registers a Windows Task Scheduler entry that triggers on login, runs at highest privilege (no UAC prompt), prefers the compiled EXE, and falls back to `pythonw.exe main.py`. Remove with `remove_autostart.bat`.

---

## Telegram Commands

| Command           | Description                                                                   |
| ----------------- | ----------------------------------------------------------------------------- |
| `/start`          | Show welcome message and inline keyboard                                      |
| `/launch`         | Start Plex Media Server                                                       |
| `/hardreset`      | Hard reset (force-kill + relaunch) — only if enabled in Settings; confirms first |
| `/status`         | Show whether Plex is running                                                  |
| `/request <text>` | Add a watch request to the queue                                              |
| `/requests`       | List all open requests                                                        |
| `/search <title>` | Search the media library                                                      |
| `/reindex`        | Rebuild the local filesystem library index                                    |
| `/libraries`      | Show library summary                                                          |
| `/metrics`        | Show play counts, watch time, sessions, library counts                        |

---

## Plex Authentication

To enable API-based features (metrics, library search, session data) you need a Plex token. The desktop app includes a built-in authentication flow:

1. Open the desktop app (or tray icon → **Open**)
2. Go to the **Settings** tab
3. Click **Authenticate with Plex** — a browser window opens with the Plex PIN page
4. Approve the request in the browser
5. The token and client identifier are written to your `.env` automatically

---

## Security Notes

- **The bot only answers allowlisted Telegram users.** Unknown users file an
  access request that appears instantly on the desktop app's Users tab for
  approval or denial. The allowlist lives in the app database.
- **Hard reset via Telegram is off by default** — enable it in Settings only
  if you're comfortable with remote users force-killing Plex mid-stream.
- Your `.env` file is git-ignored and never committed.
- The Telegram bot token grants full control of the bot — treat it like a password.
- The Plex token is stored locally in `.env` only.
- The app only responds to Telegram updates delivered to your bot; no ports are opened.
- Note: the default `OLLAMA_MODEL` ends in `:cloud`, which relays request text
  through Ollama's hosted service. Use a local model tag to keep inference on-box.

---

## Support

If this app keeps your movie nights running, consider [buying me half a coffee](https://ko-fi.com/sparklemuffin). ☕

## Acknowledgements

The torrent download pipeline (Downloads tab) is modeled on
[**torlink**](https://github.com/baairon/torlink) by **bairon**
([bairon.dev](https://bairon.dev), MIT) — the per-category source registry,
the webtorrent engine choice, and the stop-seeding-on-complete behavior all
follow torlink's design. torlink ships as an interactive terminal app, so this
project reimplements that pipeline headlessly rather than embedding his code —
but the architecture is his. Thanks, bairon. 🙏

## License

MIT — see [LICENSE](LICENSE) if present, otherwise free to use and modify.
