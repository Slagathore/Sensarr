// =============================================================================
// download.mjs — headless webtorrent downloader for PlexResetButton
// =============================================================================
// Usage: node download.mjs <magnet-uri> <destination-dir> [stallTimeoutSec]
//
// Protocol: newline-delimited JSON on stdout, consumed by download_manager.py:
//   {"event":"metadata","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"progress","progress":0.42,"downloadSpeed":123456,"peers":7}
//   {"event":"done","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"error","message":"..."}
//
// Seeding stops the moment the download completes (client.destroy on "done") —
// this runner never uploads beyond what the swarm gets during the download.
// =============================================================================

import WebTorrent from "webtorrent";

const [magnet, destDir, stallTimeoutArg] = process.argv.slice(2);
if (!magnet || !destDir) {
  console.log(JSON.stringify({ event: "error", message: "usage: download.mjs <magnet> <destDir> [stallSec]" }));
  process.exit(2);
}
const STALL_MS = (parseInt(stallTimeoutArg, 10) || 900) * 1000;

const emit = (obj) => console.log(JSON.stringify(obj));

const client = new WebTorrent();
let finished = false;

const die = (code) => {
  finished = true;
  client.destroy(() => process.exit(code));
  // Belt and braces: force-exit if destroy hangs.
  setTimeout(() => process.exit(code), 10_000).unref();
};

client.on("error", (err) => {
  emit({ event: "error", message: String(err.message || err) });
  die(1);
});

let lastDownloaded = 0;
let lastActivity = Date.now();

const torrent = client.add(magnet, { path: destDir });

torrent.on("error", (err) => {
  emit({ event: "error", message: String(err.message || err) });
  die(1);
});

torrent.on("metadata", () => {
  lastActivity = Date.now();
  emit({
    event: "metadata",
    name: torrent.name,
    files: torrent.files.map((f) => ({ path: f.path, size: f.length })),
  });
});

const progressTimer = setInterval(() => {
  if (finished) return;
  if (torrent.downloaded > lastDownloaded) {
    lastDownloaded = torrent.downloaded;
    lastActivity = Date.now();
  } else if (Date.now() - lastActivity > STALL_MS) {
    emit({ event: "error", message: `stalled: no data for ${STALL_MS / 1000}s` });
    clearInterval(progressTimer);
    die(1);
    return;
  }
  emit({
    event: "progress",
    progress: Number(torrent.progress.toFixed(4)),
    downloadSpeed: Math.round(torrent.downloadSpeed),
    peers: torrent.numPeers,
  });
}, 2000);

torrent.on("done", () => {
  clearInterval(progressTimer);
  emit({
    event: "done",
    name: torrent.name,
    files: torrent.files.map((f) => ({ path: f.path, size: f.length })),
  });
  die(0); // destroy immediately — stops seeding
});
