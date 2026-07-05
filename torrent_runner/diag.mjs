// Diagnostic: can we reach trackers / DHT at all for a given info hash?
// Usage: node diag.mjs
import Client from "bittorrent-tracker";
import DHT from "bittorrent-dht";
import crypto from "node:crypto";

const infoHash = "2aa4f5a7e209e54b32803d43670971c4c8caaa05".slice(0, 40); // placeholder replaced below
// Ubuntu 24.04.3 desktop amd64 info hash (from the .torrent we fetched):
const args = process.argv.slice(2);
const hash = (args[0] || infoHash).toLowerCase();

const peerId = crypto.randomBytes(20);
let done = 0;
const finish = () => { if (++done >= 2) process.exit(0); };

console.log("testing info hash:", hash);

const client = new Client({
  infoHash: Buffer.from(hash, "hex"),
  peerId,
  port: 6881,
  announce: [
    "https://torrent.ubuntu.com/announce",
    "udp://tracker.opentrackr.org:1337/announce",
  ],
});
let trackerPeers = 0;
client.on("update", (data) => {
  console.log(`tracker update from ${data.announce}: complete=${data.complete} incomplete=${data.incomplete}`);
});
client.on("peer", () => { trackerPeers++; });
client.on("error", (err) => console.log("tracker error:", err.message));
client.on("warning", (err) => console.log("tracker warning:", err.message));
client.start();
setTimeout(() => {
  console.log("tracker peers discovered:", trackerPeers);
  client.stop();
  client.destroy();
  finish();
}, 20000);

const dht = new DHT();
let dhtPeers = 0;
dht.on("peer", () => { dhtPeers++; });
dht.on("error", (err) => console.log("dht error:", err.message));
dht.listen(20000, () => console.log("dht listening"));
dht.on("ready", () => {
  console.log("dht ready (bootstrap ok)");
  dht.lookup(hash);
});
setTimeout(() => {
  console.log("dht peers discovered:", dhtPeers);
  dht.destroy();
  finish();
}, 25000);
