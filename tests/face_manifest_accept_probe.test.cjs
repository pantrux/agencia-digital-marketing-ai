const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

// The repository root is ESM. The isolated container stages these as CommonJS.
function loadProbe(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "immich-face-manifest-"));
  t.after(() => fs.rmSync(root, { recursive: true }));
  const source = path.resolve(__dirname, "../docker/immich-server");
  fs.writeFileSync(path.join(root, "package.json"), '{"type":"commonjs"}');
  for (const name of ["face_accept_probe.js", "face_manifest_accept_probe.js"]) {
    fs.copyFileSync(path.join(source, name), path.join(root, name));
  }
  return { probe: require(path.join(root, "face_manifest_accept_probe.js")), root };
}

const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const id = (index) => `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;

test("distingue la vista VP8L de WebP con pérdida y datos truncados", (t) => {
  const {probe} = loadProbe(t);
  const frame = Buffer.alloc(26);
  frame.write("RIFF", 0);
  frame.writeUInt32LE(frame.length - 8, 4);
  frame.write("WEBPVP8L", 8);
  frame.writeUInt32LE(5, 16);
  frame[20] = 0x2f;
  assert.doesNotThrow(() => probe.verifyLosslessPreview(frame));
  assert.throws(() => probe.verifyLosslessPreview(frame.subarray(0, 25)), /PREVIEW_WEBP/);
  frame.write("VP8 ", 12);
  assert.throws(() => probe.verifyLosslessPreview(frame), /PREVIEW_WEBP/);
});

function pngHeader(index) {
  const data = Buffer.alloc(33);
  Buffer.from("89504e470d0a1a0a", "hex").copy(data);
  data.writeUInt32BE(13, 8);
  data.write("IHDR", 12);
  data.writeUInt32BE(512, 16);
  data.writeUInt32BE(512, 20);
  data[24] = 8;
  data[25] = 2;
  data.writeUInt32BE(index, 29);
  return data;
}

function fixtureManifest(root) {
  const images = path.join(root, "images");
  fs.mkdirSync(images);
  const fixtures = [];
  for (const [i, identity] of ["mh-001", "mh-002", null].entries()) {
    for (let view = 0; view < (identity ? 4 : 1); view += 1) {
      const name = identity ? `${identity}_${view}` : "control";
      const raw = pngHeader(i * 10 + view);
      fs.writeFileSync(path.join(images, `${name}.png`), raw);
      fixtures.push({id: name, identity, split: "validation", condition: `view-${view}`,
        path: `images/${name}.png`, prepared_sha256: sha(raw), width: 512, height: 512,
        expected_faces: identity ? 1 : 0});
    }
  }
  const manifest = Buffer.from(JSON.stringify({schema: 1, synthetic: true, fixtures}));
  fs.writeFileSync(path.join(root, "fixtures.json"), manifest);
  return manifest;
}

test("el manifiesto coteja cantidad, dimensiones, hashes y confinamiento", (t) => {
  const {probe, root} = loadProbe(t);
  const manifest = fixtureManifest(root);
  const fixtures = probe.loadCorpus(root, sha(manifest), 9, "validation");
  assert.equal(fixtures.length, 9);
  assert.equal(fixtures.at(-1).expectedFaces, 0);
  assert.throws(() => probe.loadCorpus(root, sha(manifest), 8, "validation"), /CORPUS_CONTEO/);
  assert.throws(() => probe.loadCorpus(root, "0".repeat(64), 9, "validation"), /MANIFIESTO_SHA/);
  const changed = JSON.parse(manifest);
  changed.fixtures[0].path = "../mh-001_0.png";
  const raw = Buffer.from(JSON.stringify(changed));
  fs.writeFileSync(path.join(root, "fixtures.json"), raw);
  assert.throws(() => probe.loadCorpus(root, sha(raw), 9, "validation"), /CORPUS_RUTA/);
});

test("reconoce orientaciones del manifiesto original ControlFace10K sin cambiar su pin", (t) => {
  const {probe, root} = loadProbe(t);
  const fixtures = JSON.parse(fixtureManifest(root)).fixtures.filter((row) => row.expected_faces === 1);
  const selected = [];
  for (const [i, row] of fixtures.entries()) {
    const view = i % 4;
    if (view === 3) continue;
    const identity = i < 4 ? "cf-01" : "cf-02";
    const label = `${identity}-o${view + 3}`;
    const renamed = `images/${label}.PNG`;
    fs.renameSync(path.join(root, row.path), path.join(root, renamed));
    delete row.condition;
    selected.push({...row, id: label, identity, path: renamed});
  }
  const manifest = Buffer.from(JSON.stringify({schema: 1, synthetic: true,
    dataset: "https://huggingface.co/datasets/HuMInGameLab/ControlFace10K", fixtures: selected}));
  fs.writeFileSync(path.join(root, "fixtures.json"), manifest);
  assert.deepEqual(probe.loadCorpus(root, sha(manifest), 6, "validation").map((row) => row.condition),
    ["o3", "o4", "o5", "o3", "o4", "o5"]);
});

test("agrupamiento distingue ausencias, control, fusiones y divisiones", (t) => {
  const {probe, root} = loadProbe(t);
  const fixtures = probe.loadCorpus(root, sha(fixtureManifest(root)), 9, "validation");
  const assigned = fixtures.map((fixture, i) => ({...fixture, faces: fixture.expectedFaces && i !== 3
    ? [{personId: i < 4 ? id(1) : id(2)}] : []}));
  const good = probe.clusters(assigned, [id(1), id(2)]);
  assert.equal(good.passed, true);
  assert.deepEqual(good.identities[0].missing, ["mh-001_3"]);
  assigned[0].faces[0].personId = id(2);
  const mixed = probe.clusters(assigned, [id(1), id(2)]);
  assert.equal(mixed.passed, false);
  assert.equal(mixed.merges.length, 1);
  assert.deepEqual(mixed.splits, ["mh-001"]);
  assigned.at(-1).faces.push({personId: null});
  assert.equal(probe.clusters(assigned, [id(1), id(2)]).negativeFalsePositives.length, 1);
});

function mockInstance(fixtures, config, options = {}) {
  const uploads = [];
  const calls = [];
  let loggedOut = false;
  let retried = false;
  const ok = (body, status = 200) => ({body, status});
  const request = async (endpoint, {method = "GET", body, token} = {}) => {
    calls.push([method, endpoint]);
    if (endpoint === "/server/config") return ok({isInitialized: options.initialized ?? false});
    if (endpoint === "/server/version") return ok({major: 3, minor: 3, patch: 6, prerelease: null});
    if (endpoint === "/users/me") return ok(null, 401);
    if (endpoint === "/auth/admin-sign-up") return ok(null, 201);
    if (endpoint === "/auth/login") return ok({accessToken: "test-token-only-0123", isAdmin: true}, 201);
    assert.equal(token, "test-token-only-0123");
    if (endpoint === "/admin/config") return ok(config);
    if (endpoint === "/admin/users") return ok([{email: "immich-face-accept@example.invalid", isAdmin: true}]);
    if (endpoint === "/libraries") return ok([]);
    if (endpoint === "/assets/statistics") return ok({images: uploads.length, total: uploads.length, videos: 0});
    if (endpoint.startsWith("/queues/")) return ok({name: endpoint.slice(8),
      isPaused: endpoint === "/queues/metadataExtraction" && options.metadataPaused === true,
      statistics: {active: 0, waiting: 0, delayed: 0, failed: 0, completed: 0, paused: 0}});
    if (endpoint === "/people?withHidden=true") return ok({hasNextPage: false,
      people: uploads.length >= (options.listMinimum ?? 1) ? [{id: id(100)}, {id: id(101)}] : []});
    if (endpoint === "/jobs/facialRecognition") {
      assert.equal(method, "PUT");
      assert.deepEqual(body, {command: "start", force: false});
      if (options.retryRejected) return ok({}, 403);
      retried = true;
      return ok({});
    }
    if (endpoint === "/assets" && method === "POST") {
      const file = body.get("assetData");
      const fixture = fixtures.find((item) => item.name === file.name);
      assert(fixture && Buffer.from(await file.arrayBuffer()).equals(fixture.bytes));
      const asset = {fixture, id: id(uploads.length + 1)};
      uploads.push(asset);
      return ok({id: asset.id}, 201);
    }
    if (/^\/assets\/[^/]+\/original$/.test(endpoint)) {
      const asset = uploads.find((item) => item.id === endpoint.split("/")[2]);
      return ok(options.corruptOriginal ? Buffer.from("mismatch") : asset.fixture.bytes);
    }
    if (endpoint.startsWith("/faces?id=")) {
      const i = uploads.findIndex((item) => item.id === endpoint.slice(10));
      const {fixture} = uploads[i];
      if (!fixture.expectedFaces || fixture.id === "mh-001_3") return ok([]);
      if (options.retryFixture === fixture.id && !retried) return ok([{id: id(i + 20), person: null}]);
      const changed = options.moveExisting && fixture.id === "mh-001_0" && uploads.length > 6;
      return ok([{id: id(i + 20), person: {id: id(fixture.identity === "mh-001" && !changed ? 100 : 101)}}]);
    }
    if (endpoint === "/auth/logout") {
      loggedOut = true;
      return ok({successful: true});
    }
    throw new Error("MOCK_ROUTE");
  };
  return { request, calls, uploads, loggedOut: () => loggedOut };
}

test("flujo simulado inverso verifica los originales y revoca la sesión", async (t) => {
  const {probe, root} = loadProbe(t);
  const fixtures = probe.loadCorpus(root, sha(fixtureManifest(root)), 9, "validation");
  const rawConfig = fs.readFileSync(path.resolve(__dirname, "../docker/immich-server/immich.face-accept.json"));
  const configPath = path.join(root, "config.json");
  fs.writeFileSync(configPath, rawConfig);
  const config = JSON.parse(rawConfig);
  const thresholds = probe.loadConfig(configPath, sha(rawConfig));
  assert.throws(() => probe.loadConfig(configPath, "0".repeat(64)), /CONFIG_SHA/);
  const experiment = {order: "reverse", split: "validation", thresholds};
  const instance = mockInstance(fixtures, config);
  const result = await probe.run({password: "temporary-secret-0123"}, fixtures, experiment,
    instance.request, async () => {});
  assert.equal(result.success, true, result.error);
  assert.equal(result.sessionRevoked, true);
  assert.equal(result.originalsVerified, 9);
  assert.deepEqual(instance.uploads.map(({fixture}) => fixture.id), fixtures.map(({id}) => id).reverse());
  assert.equal(result.cluster.identities.find((row) => row.identity === "mh-001").assigned, 3);
  assert(!JSON.stringify(result).includes("temporary-secret-0123"));

  const occupied = mockInstance(fixtures, config, {initialized: true});
  const rejected = await probe.run({password: "temporary-secret-0123"}, fixtures, experiment,
    occupied.request, async () => {});
  assert.equal(rejected.error, "INSTANCIA_NO_VACIA");
  assert.deepEqual(occupied.calls, [["GET", "/server/config"]]);

  const paused = mockInstance(fixtures, config, {metadataPaused: true});
  const pausedResult = await probe.run({password: "temporary-secret-0123"}, fixtures, experiment,
    paused.request, async () => {});
  assert.equal(pausedResult.error, "COLA_METADATA_PAUSADA");
  assert.equal(paused.uploads.length, 0);
  assert.equal(paused.loggedOut(), true);

  const corrupted = mockInstance(fixtures, config, {corruptOriginal: true});
  const mismatch = await probe.run({password: "temporary-secret-0123"}, fixtures, experiment,
    corrupted.request, async () => {});
  assert.equal(mismatch.error, "ORIGINAL_SHA");
  assert.equal(corrupted.loggedOut(), true);
});

test("el ensayo por tandas conserva las asignaciones anteriores y detecta cambios", async (t) => {
  const {probe, root} = loadProbe(t);
  const fixtures = probe.loadCorpus(root, sha(fixtureManifest(root)), 9, "validation")
    .map((fixture) => ({...fixture, wave: {"view-0": 1, "view-1": 2, "view-2": 3, "view-3": 3}[fixture.condition]}));
  for (const wave of [2, 3]) {
    const bytes = pngHeader(20 + wave);
    fixtures.push({...fixtures.at(-1), id: `control-${wave}`, name: `control-${wave}.png`,
      wave, bytes, digest: sha(bytes)});
  }
  const configPath = path.resolve(__dirname, "../docker/immich-server/immich.face-accept.json");
  const config = JSON.parse(fs.readFileSync(configPath));
  const thresholds = probe.loadConfig(configPath, sha(fs.readFileSync(configPath)));
  const experiment = {order: "reverse", split: "validation", thresholds, incremental: true};
  const pass = mockInstance(fixtures, config, {listMinimum: 11});
  const successful = await probe.run({password: "temporary-secret-0123"}, fixtures, experiment,
    pass.request, async () => {});
  assert.equal(successful.success, true, successful.error);
  assert.deepEqual(successful.waves.map(({ fixtures }) => fixtures.length), [3, 3, 5]);
  assert.equal(successful.establishedBeforeLastWave, true);
  assert.equal(successful.waves[1].cluster.omittedPeople.length, 2);

  const fail = mockInstance(fixtures, config, {moveExisting: true});
  const changed = await probe.run({password: "temporary-secret-0123"}, fixtures, experiment,
    fail.request, async () => {});
  assert.equal(changed.success, false);
  assert.deepEqual(changed.waves.map(({changedAssignments}) => changedAssignments), [[], [], ["mh-001_0"]]);
  assert.equal(changed.waves[0].assignments["mh-001_0"][0].personId, id(100));
  assert.equal(changed.waves[2].assignments["mh-001_0"][0].personId, id(101));

  const retry = mockInstance(fixtures, config, {listMinimum: 11, retryFixture: "mh-002_0"});
  const recovered = await probe.run({password: "temporary-secret-0123"}, fixtures,
    {...experiment, retryMissing: true}, retry.request, async () => {});
  assert.equal(recovered.success, true, recovered.error);
  assert.equal(recovered.waves[2].assignments["mh-002_0"][0].personId, null);
  assert.deepEqual(recovered.retryMissing.recovered, ["mh-002_0"]);
  assert.deepEqual(recovered.retryMissing.changedAssignments, []);

  const denied = mockInstance(fixtures, config, {listMinimum: 11, retryFixture: "mh-002_0", retryRejected: true});
  const rejected = await probe.run({password: "temporary-secret-0123"}, fixtures,
    {...experiment, retryMissing: true}, denied.request, async () => {});
  assert.equal(rejected.success, false);
  assert.equal(rejected.automaticPassed, true);
  assert.equal(rejected.error, "RECONOCIMIENTO_FALTANTES_HTTP");
  assert.equal(rejected.sessionRevoked, true);
});
