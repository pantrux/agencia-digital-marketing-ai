// Ejecutar desde /usr/src/app/server en la imagen candidata aislada.
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const { accessSync, constants, readdirSync, readFileSync } = require("node:fs");
const { createRequire } = require("node:module");
const { join } = require("node:path");

const requireServer = createRequire("/usr/src/app/server/package.json");
const sha = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");

function countReadableFiles(directory) {
  accessSync(directory, constants.R_OK | constants.X_OK);
  let count = 0;
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      count += countReadableFiles(path);
    } else {
      assert.ok(entry.isFile());
      accessSync(path, constants.R_OK);
      count += 1;
    }
  }
  return count;
}

async function verify() {
  const fixture = process.argv[2];
  const expectedNode = process.argv[3] || "v24.22.1";
  assert.equal(process.version, expectedNode);
  assert.equal(process.getuid(), 1000);
  assert.equal(process.getgid(), 1000);
  assert.equal(countReadableFiles("dist"), 1378);
  assert.equal(sha("dist/main.js"), "7894328add69a1fe0a44f7d8c84cd527efeafb9bc6dc01b3b0280d236f010861");
  assert.equal(sha(fixture), "00bf9721fcfbe2dfd69780d912048c969098d693c8ee25216a8b4f49da94fb99");
  const sharp = requireServer("sharp");
  const png = await sharp({ create: { width: 2, height: 2, channels: 3, background: "blue" } }).png().toBuffer();
  assert.equal((await sharp(png).metadata()).width, 2);
  const bcrypt = requireServer("bcrypt");
  const sample = "immich-fixture-offline";
  assert.equal(await bcrypt.compare(sample, await bcrypt.hash(sample, 4)), true);
  const audio = JSON.parse(execFileSync("ffprobe", ["-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "json", fixture], { timeout: 5000 }));
  assert.match(audio.streams[0].codec_name, /^pcm_/);
  execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", fixture, "-map", "0:a:0", "-f", "null", "-"], { timeout: 10000 });
  console.log(JSON.stringify({ success: true, node: process.version, sharp: sharp.versions.sharp, vips: sharp.versions.vips, bcrypt: true, audio: audio.streams[0].codec_name }));
}

verify().catch((error) => {
  console.error(error.name, error.code || "RUNTIME_CHECK");
  process.exitCode = 1;
});
