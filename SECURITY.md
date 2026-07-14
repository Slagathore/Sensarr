# Security notes

Plexxarr is a single user desktop app that runs on the same box as your Plex
server, on Windows or Linux. There is no cloud service, no account, and no
telemetry. Everything below is about what the app does on your machine and what
it talks to, so you can decide if that trust model works for you.

## Why it asks for admin (UAC, Windows only)

On Windows the app self elevates at launch. The packaged EXE requests elevation
through its manifest, and a source run relaunches itself with the "runas" verb.
It needs that because the Hard Reset feature force kills every Plex process and
restarts the server, and because updates swap files in the install folder. If
you never use Hard Reset or self update you could run it unelevated, but that is
not the tested path.

On Linux the app never elevates and never asks for root. Process control uses
plain signals against processes you own, the setup guidance prints install
commands instead of running them, and self update is disabled entirely (you
replace the build yourself, or git pull a source checkout).

Elevation is also why the caches are JSON now. The app used to persist scan
results with pickle, and a pickle file in a user writable folder that gets loaded
by an elevated process is a privilege escalation waiting to happen: any local
process could plant a poisoned cache and run code as admin at next launch. All
three caches (maintenance results, low quality movie scan, watchlist recs) are
plain JSON today. A malformed or leftover pickle file is ignored as a cache miss
and deleted once a fresh JSON cache is written. Cache files cannot execute code.

## What talks to the internet

- Public torrent indexes (YTS, The Pirate Bay mirrors, nyaa.si, sukebei) for
  search results. These requests go over https and carry your search text.
- TMDB, TVDB, OMDB, and Jikan for titles, runtimes, and air dates, using the API
  keys you put in .env.
- Telegram, if you configure the request bot.
- plex.tv for the PIN login flow and watchlists.
- GitHub, for the nightly release check and self update downloads.
- The webtorrent runner (Node subprocess) talks to torrent trackers and peers
  while a download runs.

Ollama runs against whatever host you configure. The default is your own local
install, which sends nothing anywhere. If you point OLLAMA_MODEL at a cloud tag,
request titles you ask it to match will go to that provider. Either way it never
sees your library contents, only request text.

## Where your data lives, and how protected it is

On Windows everything sits in the install folder: the SQLite database (requests,
shows, downloads, run history), the JSON caches, and .env. On Linux the app
follows the XDG directories instead, because a packaged install must never write
beside read only code: .env lives in ~/.config/plexxarr, the databases in
~/.local/share/plexxarr, caches in ~/.cache/plexxarr, and those folders are
created with permissions only your user can read.

The database and .env are plaintext on both platforms. .env holds your Plex
token, Telegram bot token, and API keys, so treat those folders like you treat
your Plex config: anyone with an account on the machine (or admin rights) may be
able to read them. On Windows, keep the box single user or lock the folder down
with NTFS permissions if that matters to you. The app never uploads any of it.

Two hardening details worth knowing: the npm install helper resolves the real npm
binary and runs it without a shell, and every SQL migration uses static column
names baked into the source, never anything user supplied. Dependencies are
pinned to exact versions in requirements.txt and torrent_runner/package.json.

## Known dependency issue I have not fixed

npm audit reports four high severity advisories in the download runner. They are
all the same root cause: the `ip` package, which webtorrent reaches through
bittorrent-tracker, mis-categorizes some addresses as public. There is no
version of webtorrent that fixes it. I checked the current release (3.0.16) and
it carries the same advisories, and npm's only suggested remedy is webtorrent
0.7.3, which is a downgrade of several major versions and does not run this
code. So the app ships on webtorrent 2.8.5 with the issue present and known.

What it means in practice: the flaw is about how a peer address is classified,
inside a component that is already talking to untrusted peers by design. It does
not give anyone a path into your machine or your library. If a fixed webtorrent
lands, the pin moves.

## Reporting an issue

If you find a security problem, open an issue at
https://github.com/Slagathore/Plexxarr/issues. If it is something you would
rather not post publicly, open a bare issue that just says "security, want a
private channel" and I will sort one out. Please include your app version and
enough detail to reproduce.
