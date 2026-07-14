# Security notes

Plexxarr is a single user desktop app that runs on the same Windows box as your
Plex server. There is no cloud service, no account, and no telemetry. Everything
below is about what the app does on your machine and what it talks to, so you can
decide if that trust model works for you.

## Why it asks for admin (UAC)

The app self elevates at launch. The packaged EXE requests elevation through its
manifest, and a source run relaunches itself with the "runas" verb. It needs that
because the Hard Reset feature force kills every Plex process and restarts the
server, and because updates swap files in the install folder. If you never use
Hard Reset or self update you could run it unelevated, but that is not the tested
path.

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

Everything sits in the install folder: the SQLite database (requests, shows,
downloads, run history), the JSON caches, and .env. The database and .env are
plaintext. .env holds your Plex token, Telegram bot token, and API keys, so treat
the folder like you treat your Plex config: anyone with an account on the machine
can read it. If that matters to you, keep the box single user or lock the folder
down with NTFS permissions. The app never uploads any of it.

Two hardening details worth knowing: the npm install helper resolves the real npm
binary and runs it without a shell, and every SQL migration uses static column
names baked into the source, never anything user supplied. Dependencies are
pinned to exact versions in requirements.txt and torrent_runner/package.json.

## Reporting an issue

If you find a security problem, open an issue at
https://github.com/Slagathore/Plexxarr/issues. If it is something you would
rather not post publicly, open a bare issue that just says "security, want a
private channel" and I will sort one out. Please include your app version and
enough detail to reproduce.
