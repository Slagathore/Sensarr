// =============================================================================
// download.mjs — headless webtorrent downloader for Sensarr
// =============================================================================
// Usage: node download.mjs <magnet-uri> <destination-dir> [stallTimeoutSec] [selectionJson]
//
// Protocol: newline-delimited JSON on stdout, consumed by download_manager.py:
//   {"event":"metadata","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"progress","progress":0.42,"downloadSpeed":123456,"peers":7}
//   {"event":"done","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"error","message":"..."}
//
// Seeding stops the moment the download completes (client.destroy on "done") —
// this runner never uploads beyond what the swarm gets during the download.
//
// Credit: the design of this pipeline (webtorrent engine, per-category
// sources, stop-seed-on-complete) is modeled on torlink by bairon
// (https://github.com/baairon/torlink, MIT). See README Acknowledgements.
// =============================================================================

import dgram from "node:dgram";
import net from "node:net";
import WebTorrent from "webtorrent";

const [magnet, destDir, stallTimeoutArg, selectionArg] = process.argv.slice(2);

// webtorrent listens for TCP on a random port, then binds uTP (UDP) to the SAME
// number. Windows reserves whole UDP ranges for Hyper-V/WinNAT (55056-56006 and
// 61887-63014 on Cole's machine, 2026-10-09), so a random pick sometimes lands
// in one, utp-native's bind throws EACCES and the runner dies with "permission
// denied" (65 downloads before this fix). Pick ports that are known to bind.
const udpFree = (port) => new Promise((resolve) => {
  const sock = dgram.createSocket("udp4");
  sock.once("error", () => { try { sock.close(); } catch {} resolve(false); });
  sock.bind(port, () => sock.close(() => resolve(true)));
});
const tcpFree = (port) => new Promise((resolve) => {
  const server = net.createServer();
  server.once("error", () => resolve(false));
  server.listen(port, () => server.close(() => resolve(true)));
});
const pickPort = async (needTcp) => {
  for (let attempt = 0; attempt < 50; attempt++) {
    const port = 20000 + Math.floor(Math.random() * 30000);
    if (await udpFree(port) && (!needTcp || await tcpFree(port))) return port;
  }
  return 0; // let webtorrent choose, as before
};

// Deterministic no-network smoke mode (Task H item 9): prove the runner's
// dependency tree loads and a client constructs/destroys cleanly, with every
// peer-discovery mechanism disabled so nothing touches the network. CI runs
// this; process.exit here means the download flow below never starts.
if (magnet === "--smoke-test") {
  const smokeClient = new WebTorrent({
    dht: false, tracker: false, lsd: false, utPex: false, webSeeds: false,
  });
  console.log(JSON.stringify({ event: "smoke", ok: true, webtorrent: WebTorrent.VERSION || "unknown" }));
  const destroyed = new Promise((resolve) => smokeClient.destroy(resolve));
  const timedOut = new Promise((resolve) => setTimeout(() => resolve("timeout"), 10_000).unref());
  const result = await Promise.race([destroyed, timedOut]);
  if (result === "timeout") {
    console.log(JSON.stringify({ event: "error", message: "smoke destroy timed out" }));
    process.exit(1);
  }
  process.exit(0);
}

if (!magnet || !destDir) {
  console.log(JSON.stringify({ event: "error", message: "usage: download.mjs <magnet> <destDir> [stallSec]" }));
  process.exit(2);
}
const STALL_MS = (parseInt(stallTimeoutArg, 10) || 900) * 1000;
let selection = {};
try {
  selection = selectionArg ? JSON.parse(selectionArg) : {};
} catch {
  selection = {};
}

const emit = (obj) => console.log(JSON.stringify(obj));

const torrentPort = await pickPort(true);
const dhtPort = await pickPort(false);
const client = new WebTorrent({
  ...(torrentPort ? { torrentPort } : {}),
  ...(dhtPort && dhtPort !== torrentPort ? { dhtPort } : {}),
});
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
const startedAt = Date.now();
let wantedFiles = [];

// Start with NOTHING selected. By default webtorrent selects the whole torrent
// the moment metadata arrives, peers start on random 16 MB pieces of every
// file, and those finish even after chooseFiles() narrows the selection: a
// single pinned episode of a Futurama pack also wrote 113 MB of S07E26.
const torrent = client.add(magnet, { path: destDir, deselect: true });

// Piece ranges of the files we want (filled by chooseFiles at "ready").
let wantedPieceRanges = [];
const pieceWanted = (index) => wantedPieceRanges.some(([a, b]) => index >= a && index <= b);

// BEP 6 "allowed fast": a peer that is choking us may offer a few pieces for
// free, and webtorrent requests them WITHOUT checking the selection. Give each
// wire a list that only keeps offers inside the wanted files.
torrent.on("wire", (wire) => {
  const offered = [];
  offered.push = (...indexes) => Array.prototype.push.apply(offered, indexes.filter(pieceWanted));
  wire.peerAllowedFastSet = offered;
});

torrent.on("error", (err) => {
  emit({ event: "error", message: String(err.message || err) });
  die(1);
});

const extension = (filePath) => {
  const match = String(filePath || "").toLowerCase().match(/(\.[^.\\/]+)$/);
  return match ? match[1] : "";
};

const pathTokens = (filePath) => new Set(
  String(filePath || "").toLowerCase()
    .split(/[\\/\s._\-\[\](){}]+/)
    .filter(Boolean)
);

const episodeMarker = (filePath) => {
  const text = String(filePath || "");
  let match = text.match(/(?:^|\D)s(\d{1,2})[ ._\-]*e(\d{1,3})(?:\D|$)/i);
  if (match) return { season: Number(match[1]), episode: Number(match[2]) };
  match = text.match(/(?:^|\D)(\d{1,2})x(\d{1,3})(?:\D|$)/i);
  return match ? { season: Number(match[1]), episode: Number(match[2]) } : null;
};

const subtitleLanguageOk = (filePath) => {
  const spec = selection.subtitleLanguage || {};
  const tokens = pathTokens(filePath);
  const all = new Set(spec.allLanguageTokens || []);
  const wanted = new Set(spec.wantedTokens || []);
  const found = [...tokens].filter((token) => all.has(token));
  return found.length === 0 || found.some((token) => wanted.has(token));
};

const baseName = (filePath) => String(filePath || "").split(/[\\/]/).pop();
const stemOf = (filePath) => baseName(filePath).replace(/\.[^.]+$/, "").toLowerCase();

// Torrentio names the exact file a result is about inside a pack (fileName,
// with fileIdx as the fallback). Taking that one file is what keeps a single
// episode from pulling a whole 30 GB season pack whose files carry no SxxEyy.
const pinnedVideo = (videos, videoExts) => {
  if (selection.fileName) {
    const wanted = String(selection.fileName).toLowerCase();
    const byName = videos.filter((file) => baseName(file.path).toLowerCase() === wanted);
    if (byName.length === 1) return byName[0];
  }
  if (Number.isInteger(selection.fileIdx)) {
    const file = torrent.files[selection.fileIdx];
    if (file && videoExts.has(extension(file.path))) return file;
  }
  return null;
};

const chooseFiles = () => {
  const videoExts = new Set(selection.videoExtensions || []);
  const subtitleExts = new Set(selection.subtitleExtensions || []);
  const videos = torrent.files.filter((file) => videoExts.has(extension(file.path)));
  const wantedSeason = Number.isInteger(selection.season) ? selection.season : null;
  const wantedEpisode = Number.isInteger(selection.episode) ? selection.episode : null;
  const pinned = pinnedVideo(videos, videoExts);

  let selectedVideos = videos;
  if (pinned) {
    selectedVideos = [pinned];
  } else if (wantedEpisode !== null) {
    const exact = videos.filter((file) => {
      const marker = episodeMarker(file.path);
      return marker && marker.episode === wantedEpisode
        && (wantedSeason === null || marker.season === wantedSeason);
    });
    // Never guess: prune a pack only after positively finding the target.
    if (exact.length > 0) selectedVideos = exact;
  } else if (wantedSeason !== null) {
    const exactSeason = videos.filter((file) => {
      const marker = episodeMarker(file.path);
      return marker && marker.season === wantedSeason;
    });
    if (exactSeason.length > 0) selectedVideos = exactSeason;
  }

  const selected = new Set(selectedVideos);
  for (const file of torrent.files) {
    if (!subtitleExts.has(extension(file.path)) || !subtitleLanguageOk(file.path)) continue;
    // A pinned file only brings the subtitles named after it.
    if (pinned && !stemOf(file.path).startsWith(stemOf(pinned.path))) continue;
    const marker = episodeMarker(file.path);
    if (wantedEpisode !== null && marker
        && (marker.episode !== wantedEpisode
            || (wantedSeason !== null && marker.season !== wantedSeason))) continue;
    if (wantedEpisode === null && wantedSeason !== null && marker
        && marker.season !== wantedSeason) continue;
    selected.add(file);
  }

  torrent.files.forEach((file) => file.deselect());
  selected.forEach((file) => file.select());
  return [...selected];
};

torrent.on("ready", () => {
  lastActivity = Date.now();
  const selectedFiles = chooseFiles();
  wantedFiles = selectedFiles;
  wantedPieceRanges = selectedFiles
    .filter((f) => f.length > 0)
    .map((f) => [Math.floor(f.offset / torrent.pieceLength),
      Math.floor((f.offset + f.length - 1) / torrent.pieceLength)]);
  const videoExts = new Set(selection.videoExtensions || []);
  if (!selectedFiles.some((file) => videoExts.has(extension(file.path)))) {
    emit({ event: "error", message: "torrent metadata contains no selectable video file" });
    die(1);
    return;
  }
  // Unselected files that share a piece with a selected one: webtorrent
  // writes those pieces into both files, so these get partially written too.
  // Reported exactly so the app can clean them up without guessing.
  const chosen = new Set(selectedFiles);
  const neighbours = torrent.files.filter((f) => !chosen.has(f) && f.length > 0 && (() => {
    const first = Math.floor(f.offset / torrent.pieceLength);
    const last = Math.floor((f.offset + f.length - 1) / torrent.pieceLength);
    return wantedPieceRanges.some(([a, b]) => first <= b && last >= a);
  })());
  emit({
    event: "metadata",
    name: torrent.name,
    files: selectedFiles.map((f) => ({ path: f.path, size: f.length })),
    skippedFiles: torrent.files.length - selectedFiles.length,
    neighbours: neighbours.map((f) => f.path),
  });
  // Finish when OUR files are done (see finish()). Resumed data may already
  // complete them.
  selectedFiles.forEach((file) => file.on("done", () => { if (wantedDone()) finish(); }));
  if (wantedDone()) finish();
});

const progressTimer = setInterval(() => {
  if (finished) return;
  const maxRuntimeMs = Number(selection.maxRuntimeSeconds || 0) * 1000;
  if (maxRuntimeMs > 0 && Date.now() - startedAt > maxRuntimeMs) {
    emit({
      event: "error",
      message: `timed out: exceeded maximum runtime of ${selection.maxRuntimeSeconds}s`,
    });
    clearInterval(progressTimer);
    die(1);
    return;
  }
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
    progress: Number(selectedProgress().toFixed(4)),
    downloadSpeed: Math.round(torrent.downloadSpeed),
    peers: torrent.numPeers,
  });
}, 2000);

// Progress of the files we actually want. torrent.progress covers the whole
// torrent, so one pinned episode of a 30 GB pack read ~3% when nearly done,
// and the app's "50%+ is worth resuming" rules never fired for it.
function selectedProgress() {
  const total = wantedFiles.reduce((sum, f) => sum + (f.length || 0), 0);
  if (!total) return torrent.progress;
  const done = wantedFiles.reduce(
    (sum, f) => sum + Math.min(f.length || 0, f.downloaded || 0), 0);
  return Math.max(0, Math.min(1, done / total));
}

// webtorrent's own torrent "done" waits for EVERY file in the torrent, so a
// pack pruned to the files we want never raised it: the episode finished and
// then sat until the stall timer killed it. Done = all wanted files done.
function wantedDone() {
  return wantedFiles.length > 0 && wantedFiles.every((f) => f.done);
}

function finish() {
  if (finished) return;
  clearInterval(progressTimer);
  emit({
    event: "done",
    name: torrent.name,
    files: wantedFiles.map((f) => ({ path: f.path, size: f.length })),
  });
  die(0); // destroy immediately — stops seeding
}

torrent.on("done", finish);
