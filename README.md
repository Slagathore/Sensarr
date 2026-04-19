# PlexResetButton

A Windows system-tray application and Telegram bot that lets you remotely **launch, soft-reset, hard-reset, monitor, and manage Plex Media Server** from your phone — without ever touching the server.

```
Phone (Telegram) ──► Telegram Bot API ──► PlexResetButton.exe ──► Plex Media Server
```

---

## Features

| Category             | Details                                                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Telegram control** | Inline keyboard + slash commands: Launch, Soft Reset, Hard Reset, Status, Metrics                            |
| **Soft reset**       | Gracefully exits Plex via system-tray icon automation (pyautogui image matching) then relaunches             |
| **Hard reset**       | Force-kills every Plex process tree via `taskkill /F` (runs elevated; never fails silently)                  |
| **UAC elevation**    | EXE requests admin at launch via `uac_admin=True`; Python script self-elevates with `ShellExecuteW("runas")` |
| **System tray**      | Lives in the Windows notification area; hides to tray on close                                               |
| **Request queue**    | Household members submit watch requests via `/request`; admin marks them complete                            |
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
plex_control.py      Soft reset (pyautogui), hard reset (taskkill), launch, status
icon_finder.py       pyautogui screen-image search helpers (tray icon, exit menu)
plex_api.py          Plex HTTP API client — metrics, sessions, library inventory
plex_auth.py         Plex PIN/token authentication flow (urllib, no extra deps)
library_index.py     SQLite-backed filesystem media indexer and searcher
metrics_report.py    Formats combined metrics message for bot and desktop
queue_store.py       SQLite request queue — add, complete, list, count
config.py            Centralised config: reads .env, validates required keys
app_logging.py       Dual log handler: stderr stream + in-memory ring buffer
diagnose.py          Standalone DPI/asset diagnostic tool (read-only, safe to run)
```

---

## Prerequisites

- **Windows 10 or 11** (64-bit)
- **Python 3.11+** (if running from source)
- **Plex Media Server** installed locally (default path auto-detected)
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)

---

## Installation (from source)

```powershell
git clone https://github.com/<you>/PlexResetButton.git
cd PlexResetButton

python -m pip install -r requirements.txt

# Copy the example config and fill in your values
Copy-Item .env.example .env
notepad .env
```

---

## Configuration — `.env`

Copy `.env.example` to `.env` and edit the values. The `.env` file is git-ignored; it never leaves your machine.

| Variable                       | Required | Default                                                         | Description                                                 |
| ------------------------------ | -------- | --------------------------------------------------------------- | ----------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`           | **Yes**  | —                                                               | Bot token from @BotFather                                   |
| `PLEX_MEDIA_SERVER_PATH`       | No       | `C:\Program Files\Plex\Plex Media Server\Plex Media Server.exe` | Full path to the Plex executable                            |
| `PLEX_SERVER_URL`              | No       | `http://127.0.0.1:32400`                                        | Local Plex server URL                                       |
| `PLEX_TOKEN`                   | No       | —                                                               | Plex authentication token (obtain via the in-app auth flow) |
| `PLEX_CLIENT_IDENTIFIER`       | No       | —                                                               | Stable client UUID for Plex API requests                    |
| `PLEX_VERIFY_SSL`              | No       | `false`                                                         | Verify Plex TLS certificate                                 |
| `PLEX_REQUEST_TIMEOUT_SECONDS` | No       | `10`                                                            | HTTP timeout for Plex API calls                             |
| `PLEX_HISTORY_FETCH_LIMIT`     | No       | `200`                                                           | Max history records to fetch for metrics                    |
| `APP_DB_PATH`                  | No       | `plex_reset_button.db`                                          | Path to the SQLite database                                 |
| `PLEX_LIBRARY_PATHS`           | No       | —                                                               | Semicolon-separated media root paths for local indexing     |
| `LIBRARY_INDEX_EXTENSIONS`     | No       | `.mkv;.mp4;…`                                                   | File extensions to index                                    |
| `LIBRARY_SEARCH_RESULT_LIMIT`  | No       | `25`                                                            | Max search results returned                                 |
| `PLEX_EXIT_WAIT`               | No       | `3`                                                             | Seconds between each "did Plex exit?" poll                  |
| `PLEX_EXIT_GRACE`              | No       | `3`                                                             | Initial grace period before polling starts                  |
| `PLEX_LAUNCH_WAIT`             | No       | `5`                                                             | Seconds to wait after launching Plex                        |
| `MAX_EXIT_RETRIES`             | No       | `10`                                                            | Poll attempts before giving up on soft reset                |
| `TRAY_ICON_CONFIDENCE`         | No       | `0.85`                                                          | pyautogui confidence for tray icon match                    |
| `TASKBAR_ICON_CONFIDENCE`      | No       | `0.85`                                                          | pyautogui confidence for taskbar icon match                 |
| `ADMIN_STATUS_REFRESH_SECONDS` | No       | `15`                                                            | How often the desktop UI polls Plex status                  |

---

## Running

### Option A — Python script (development)

```powershell
python main.py
```

On first run the app will trigger a UAC prompt to request administrator privileges. This is required to reliably force-kill Plex processes. Accept it once; subsequent runs will also prompt (or configure auto-start below).

### Option B — Compiled EXE

See **Building the EXE** below. The EXE requests elevation automatically at launch via its embedded manifest (`uac_admin=True` in the PyInstaller spec).

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

Registers a Windows Task Scheduler entry that:

- Triggers on every login (`/sc onlogon`)
- Runs at **highest privilege** (`/rl highest`) — no UAC prompt on startup
- Prefers the compiled EXE; falls back to `pythonw.exe main.py`

To remove:

```bat
remove_autostart.bat
```

---

## Telegram Commands

| Command           | Description                                                                   |
| ----------------- | ----------------------------------------------------------------------------- |
| `/start`          | Show welcome message and inline keyboard                                      |
| `/launch`         | Start Plex Media Server                                                       |
| `/reset`          | Soft reset (graceful tray exit + relaunch)                                    |
| `/hardreset`      | Hard reset (force-kill all Plex processes + relaunch) — asks for confirmation |
| `/status`         | Show whether Plex is running and asset health                                 |
| `/request <text>` | Add a watch request to the queue                                              |
| `/requests`       | List all open requests                                                        |
| `/search <title>` | Search the media library                                                      |
| `/reindex`        | Rebuild the local filesystem library index                                    |
| `/libraries`      | Show library summary                                                          |
| `/metrics`        | Show play counts, watch time, sessions, library counts                        |

---

## Screen-Matching Assets (`assets/`)

The soft-reset flow uses `pyautogui.locateOnScreen` to find the Plex tray icon and exit menu item. The reference images in `assets/` must match your screen's DPI and theme.

If the soft reset fails to find the tray icon, run the diagnostic tool:

```powershell
python diagnose.py
```

It captures a screenshot, reports your DPI/scale factor, and saves a cropped tray region to `debug_tray_corner.png` so you can re-capture a matching reference image.

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

- Your `.env` file is git-ignored and never committed
- The Telegram bot token grants full control of the bot — treat it like a password
- The Plex token is stored locally in `.env` only
- The app only responds to Telegram updates delivered to your bot; no ports are opened

---

## License

MIT — see [LICENSE](LICENSE) if present, otherwise free to use and modify.
