// Ensayo de agrupamiento con un manifiesto sintético acreditado.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { parseArgs } = require("node:util");
const { api, readInput } = require("./face_accept_probe.js");
const { authenticate, call, checked, errorCode, need, peopleIds, poll, readFaces, sha, upload } = api;

const ROOT = "/run/immich/fixtures";
const CONFIG = "/run/immich/face-accept.json";
const MODEL = "agency_yunet_auraface_v1";
const EMAIL = "immich-face-accept@example.invalid";
const DIGEST = /^[a-f0-9]{64}$/;
const LABEL = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/;
const MAX_ASSETS = 128;
const MAX_PNG = 4 * 1024 * 1024;
const MAX_BYTES = 64 * 1024 * 1024;
const PNG_MAGIC = Buffer.from("89504e470d0a1a0a", "hex");
const SPLITS = ["calibration", "validation"];
const CONTROLFACE = "https://huggingface.co/datasets/HuMInGameLab/ControlFace10K";

function verifyLosslessPreview(raw) {
  need(Buffer.isBuffer(raw) && raw.length >= 25 && raw.toString("ascii", 0, 4) === "RIFF" &&
    raw.toString("ascii", 8, 12) === "WEBP" && raw.readUInt32LE(4) + 8 === raw.length, "PREVIEW_WEBP");
  let lossless = 0;
  for (let offset = 12; offset < raw.length;) {
    need(offset + 8 <= raw.length, "PREVIEW_WEBP");
    const kind = raw.toString("ascii", offset, offset + 4);
    const size = raw.readUInt32LE(offset + 4);
    const next = offset + 8 + size + (size % 2);
    need(next <= raw.length && !["VP8 ", "ANIM", "ANMF"].includes(kind), "PREVIEW_WEBP");
    if (kind === "VP8L") {
      need(size >= 5 && raw[offset + 8] === 0x2f, "PREVIEW_WEBP");
      lossless += 1;
    }
    offset = next;
  }
  need(lossless === 1, "PREVIEW_WEBP");
}

function readFile(file, maximum, error) {
  const expected = fs.lstatSync(file);
  need(expected.isFile() && expected.size > 0 && expected.size <= maximum, error);
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  try {
    const current = fs.fstatSync(fd);
    need(current.isFile() && current.dev === expected.dev && current.ino === expected.ino &&
      current.size === expected.size, error);
    const raw = Buffer.alloc(current.size);
    let offset = 0;
    while (offset < raw.length) {
      const count = fs.readSync(fd, raw, offset, raw.length - offset, null);
      need(count > 0, error);
      offset += count;
    }
    const after = fs.fstatSync(fd);
    need(after.size === current.size && after.mtimeMs === current.mtimeMs && after.ctimeMs === current.ctimeMs, error);
    return raw;
  } finally {
    fs.closeSync(fd);
  }
}

function verifyPng(raw, width, height) {
  need(raw.length >= 33 && raw.subarray(0, 8).equals(PNG_MAGIC) && raw.readUInt32BE(8) === 13 &&
    raw.toString("ascii", 12, 16) === "IHDR", "CORPUS_PNG");
  need(raw.readUInt32BE(16) === width && raw.readUInt32BE(20) === height, "CORPUS_DIMENSIONES");
}

function conditionFor(row, dataset) {
  // Early ControlFace10K manifests bind the orientation in their exact fixture ID.
  if (row.condition === undefined && dataset === CONTROLFACE) {
    const orientation = /^cf-[0-9]{2}-(o[345])$/.exec(row.id);
    need(orientation && row.identity === row.id.slice(0, -3), "CORPUS_IDENTIDAD");
    return orientation[1];
  }
  need(typeof row.condition === "string" && LABEL.test(row.condition), "CORPUS_IDENTIDAD");
  return row.condition;
}

function loadCorpus(root, pin, count, split) {
  need(Number.isSafeInteger(count) && count >= 6 && count <= MAX_ASSETS && SPLITS.includes(split), "CORPUS_CONTEO");
  need(fs.lstatSync(root).isDirectory(), "CORPUS_RAIZ");
  const imageDirectory = path.join(root, "images");
  need(fs.lstatSync(imageDirectory).isDirectory(), "CORPUS_RUTA");
  const manifest = readFile(path.join(root, "fixtures.json"), 512 * 1024, "MANIFIESTO_ARCHIVO");
  need(DIGEST.test(pin) && sha(manifest) === pin, "MANIFIESTO_SHA");
  const doc = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(manifest));
  need(doc?.schema === 1 && doc.synthetic === true && Array.isArray(doc.fixtures) &&
    doc.fixtures.length <= 2 * MAX_ASSETS, "MANIFIESTO_ESQUEMA");
  const ids = new Set();
  const digests = new Set();
  const identitySplits = new Map();
  const views = new Map();
  const rows = doc.fixtures.map((row) => {
    need(typeof row?.id === "string" && LABEL.test(row.id) && !ids.has(row.id.toLowerCase()) &&
      typeof row.prepared_sha256 === "string" && DIGEST.test(row.prepared_sha256) &&
      !digests.has(row.prepared_sha256) && (row.original_sha256 === undefined ||
        typeof row.original_sha256 === "string" && DIGEST.test(row.original_sha256)), "CORPUS_FICHA");
    ids.add(row.id.toLowerCase());
    digests.add(row.prepared_sha256);
    need(typeof row.path === "string" && ["png", "PNG"].some((ext) => row.path === `images/${row.id}.${ext}`),
      "CORPUS_RUTA");
    need([row.width, row.height].every((value) => Number.isSafeInteger(value) && value >= 32 && value <= 4096) &&
      row.width * row.height <= 4 * 1024 * 1024,
      "CORPUS_DIMENSIONES");
    const control = row.expected_faces === 0;
    need((control && row.identity === null || row.expected_faces === 1 &&
      typeof row.identity === "string" && LABEL.test(row.identity)) &&
      (SPLITS.includes(row.split) || control && row.split === "control"), "CORPUS_IDENTIDAD");
    const condition = conditionFor(row, doc.dataset);
    if (!control) {
      const identityKey = row.identity.toLowerCase();
      const previous = identitySplits.get(identityKey);
      need(!previous || previous.split === row.split && previous.identity === row.identity, "CORPUS_SPLIT_IDENTIDAD");
      identitySplits.set(identityKey, {identity: row.identity, split: row.split});
      if (row.split === split) {
        if (!views.has(row.identity)) views.set(row.identity, new Set());
        const conditions = views.get(row.identity);
        need(!conditions.has(condition), "CORPUS_VISTA_DUPLICADA");
        conditions.add(condition);
      }
    }
    return {...row, condition};
  });
  const selected = rows.filter((row) => row.split === split || row.split === "control");
  need(selected.length === count, "CORPUS_CONTEO");
  need(views.size >= 2 && [...views.values()].every((rows) => rows.size >= 3), "CORPUS_COBERTURA");
  let total = 0;
  return selected.map((row) => {
    const filename = path.join(imageDirectory, path.posix.basename(row.path));
    const bytes = readFile(filename, Math.min(MAX_PNG, MAX_BYTES - total), "CORPUS_ARCHIVO");
    total += bytes.length;
    need(sha(bytes) === row.prepared_sha256, "CORPUS_SHA");
    verifyPng(bytes, row.width, row.height);
    return { id: row.id, name: `${row.id}.png`, identity: row.identity, condition: row.condition,
      expectedFaces: row.expected_faces, digest: row.prepared_sha256, bytes,
      ...(row.wave !== undefined && { wave: row.wave }) };
  });
}

function configThresholds(config) {
  const ml = config?.machineLearning;
  const face = ml?.facialRecognition;
  need(ml?.enabled === true && face?.enabled === true && face.modelName === MODEL &&
    [0.60, 0.65, 0.70, 0.75, 0.80].includes(face.minScore) &&
    Number.isFinite(face.maxDistance) && face.maxDistance >= 0.30 && face.maxDistance <= 0.70 &&
    face.minFaces === 3 && ml.clip?.enabled === false && ml.ocr?.enabled === false &&
    ml.duplicateDetection?.enabled === false && Array.isArray(ml.urls) && ml.urls.length === 1 &&
    ml.urls[0] === "http://immich-ml-auth:3003" && config.metadata?.faces?.import === false &&
    config.nightlyTasks?.clusterNewFaces === false && config.nightlyTasks?.generateMemories === false &&
    config.job?.faceDetection?.concurrency === 1 && config.library?.scan?.enabled === false &&
    config.library?.watch?.enabled === false && config.backup?.database?.enabled === false &&
    config.oauth?.enabled === false && config.newVersionCheck?.enabled === false &&
    config.passwordLogin?.enabled === true, "CONFIG_CONTRATO");
  return { model: MODEL, minScore: face.minScore, maxDistance: face.maxDistance, minFaces: face.minFaces };
}

function loadConfig(filename, pin) {
  const raw = readFile(filename, 64 * 1024, "CONFIG_ARCHIVO");
  need(DIGEST.test(pin) && sha(raw) === pin, "CONFIG_SHA");
  return configThresholds(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)));
}

function clusters(assets, listed) {
  const owners = new Map();
  const groups = new Map();
  const negativeFalsePositives = [];
  for (const asset of assets) {
    if (asset.expectedFaces === 0) {
      if (asset.faces.length !== 0) negativeFalsePositives.push(asset.id);
      continue;
    }
    if (!groups.has(asset.identity)) {
      groups.set(asset.identity, {identity: asset.identity, assigned: 0, persons: new Set(),
        missing: [], multiple: [], unassigned: []});
    }
    const group = groups.get(asset.identity);
    if (asset.faces.length === 0) group.missing.push(asset.id);
    if (asset.faces.length > 1) group.multiple.push(asset.id);
    for (const {personId} of asset.faces) {
      if (!personId) continue;
      group.persons.add(personId);
      if (!owners.has(personId)) owners.set(personId, new Set());
      owners.get(personId).add(asset.identity);
    }
    if (asset.faces.length === 1) {
      if (asset.faces[0].personId) group.assigned += 1;
      else group.unassigned.push(asset.id);
    }
  }
  const identities = [...groups.values()].map(({persons, ...group}) => ({...group, personIds: [...persons]}));
  const merges = [...owners].filter(([, ids]) => ids.size > 1)
    .map(([personId, ids]) => ({personId, identities: [...ids]}));
  const splits = identities.filter((group) => group.personIds.length > 1).map((group) => group.identity);
  const unexplainedPeople = listed.filter((personId) => !owners.has(personId));
  const omittedPeople = [...owners.keys()].filter((personId) => !listed.includes(personId));
  const passed = identities.length >= 2 && identities.every((group) =>
    group.assigned >= 3 && group.personIds.length === 1 && group.multiple.length === 0) &&
    merges.length === 0 && splits.length === 0 && negativeFalsePositives.length === 0 &&
    unexplainedPeople.length === 0 && omittedPeople.length === 0 && new Set(listed).size === listed.length;
  return { passed, identities, merges, splits, negativeFalsePositives, unexplainedPeople, omittedPeople };
}

async function isolatedInventory(request, token, expectedCount) {
  const auth = { token };
  const users = await checked(request, "/admin/users", auth, 200, "USUARIOS_HTTP");
  need(Array.isArray(users) && users.length === 1 && users[0].email === EMAIL && users[0].isAdmin === true,
    "USUARIOS_ENSAYO");
  const libraries = await checked(request, "/libraries", auth, 200, "BIBLIOTECAS_HTTP");
  need(Array.isArray(libraries) && libraries.length === 0, "BIBLIOTECAS_EXISTENTES");
  const inventory = await checked(request, "/assets/statistics", auth, 200, "INVENTARIO_HTTP");
  need(inventory?.images === expectedCount && inventory.total === expectedCount && inventory.videos === 0,
    "INVENTARIO_ENSAYO");
}

function drained(state) {
  return state.drained && Object.values(state.stats).every((queue) => !queue.isPaused && queue.failed === 0);
}

async function verifyUploaded(request, assets, token) {
  for (const asset of assets) {
    const bytes = await checked(request, `/assets/${asset.assetId}/original`,
      { token, binary: true, maxBytes: MAX_PNG }, 200, "ORIGINAL_HTTP");
    need(Buffer.isBuffer(bytes) && sha(bytes) === asset.pngSha256, "ORIGINAL_SHA");
  }
}

function uploadWaves(ordered, incremental) {
  if (!incremental) return [ordered];
  const waves = [1, 2, 3].map((wave) => ordered.filter((fixture) => fixture.wave === wave));
  const identities = new Set(ordered.filter((fixture) => fixture.expectedFaces === 1).map((fixture) => fixture.identity));
  need(waves.reduce((count, wave) => count + wave.length, 0) === ordered.length &&
    waves.every((wave) => {
      const positives = wave.filter((fixture) => fixture.expectedFaces === 1);
      return wave.length > positives.length && new Set(positives.map((fixture) => fixture.identity)).size === identities.size;
    }), "CORPUS_TANDAS");
  return waves;
}

async function checkpoint(request, assets, token, people, queues, previous) {
  await readFaces(request, assets, token);
  const cluster = clusters(assets, peopleIds(await checked(request, people, { token }, 200, "PERSONAS_HTTP")));
  const assignments = Object.fromEntries(assets.map(({ id, faces }) => [id, faces]));
  const changedAssignments = Object.entries(previous?.assignments ?? {})
    .filter(([id, faces]) => faces.some(({ faceId, personId }) => personId &&
      !assignments[id]?.some((face) => face.faceId === faceId && face.personId === personId)))
    .map(([id]) => id);
  return { queues, cluster, assignments, changedAssignments };
}

async function run(input, fixtures, experiment, request = call, pause) {
  const report = { success: false, calibrated: false, manifestSha256: experiment.manifestSha256,
    configSha256: experiment.configSha256, order: experiment.order, split: experiment.split,
    expectedCount: fixtures.length, thresholds: experiment.thresholds };
  let token;
  try {
    need(input && Object.keys(input).length === 1 && typeof input.password === "string" &&
      input.password.length >= 16 && input.password.length <= 128, "ENTRADA");
    const publicConfig = await checked(request, "/server/config", {}, 200, "CONFIG_PUBLICA_HTTP");
    need(publicConfig?.isInitialized === false, "INSTANCIA_NO_VACIA");
    token = await authenticate(request, input.password, false, "Ensayo facial sintético");
    const auth = { token };
    const effectiveConfig = await checked(request, "/admin/config", auth, 200, "CONFIG_HTTP");
    const thresholds = configThresholds(effectiveConfig);
    need(Object.keys(thresholds).every((key) => thresholds[key] === experiment.thresholds[key]), "CONFIG_API");
    if (experiment.losslessPreview) {
      const preview = effectiveConfig.image?.preview;
      need(preview?.format === "webp" && preview.size === 1440 && preview.quality === 80 &&
        preview.progressive === false, "CONFIG_PREVIEW");
    }
    await isolatedInventory(request, token, 0);
    const people = "/people?withHidden=true";
    need(peopleIds(await checked(request, people, auth, 200, "PERSONAS_HTTP")).length === 0, "PERSONAS_EXISTENTES");
    const initialQueues = await poll(request, token, pause, true);
    need(initialQueues.stats.metadataExtraction?.isPaused !== true, "COLA_METADATA_PAUSADA");
    need(drained(initialQueues), "COLAS_INICIALES");

    const ordered = experiment.order === "reverse" ? fixtures.slice().reverse() : fixtures;
    const waves = uploadWaves(ordered, experiment.incremental);
    report.assets = [];
    if (experiment.incremental) report.waves = [];
    if (experiment.losslessPreview) report.losslessPreviews = {};
    let previous;
    for (const wave of waves) {
      const uploaded = await upload(request, wave, token);
      const assets = uploaded.map((asset, index) => ({ ...asset, id: wave[index].id,
        expectedFaces: wave[index].expectedFaces, condition: wave[index].condition }));
      report.assets.push(...assets);
      await isolatedInventory(request, token, report.assets.length);
      await verifyUploaded(request, assets, token);
      report.originalsVerified = report.assets.length;
      report.queues = await poll(request, token, pause, true);
      if (experiment.incremental) need(drained(report.queues), "COLAS_TANDA");
      if (experiment.losslessPreview) {
        for (const asset of assets) {
          const bytes = await checked(request, `/assets/${asset.assetId}/thumbnail?size=preview`,
            { token, binary: true, maxBytes: MAX_PNG }, 200, "PREVIEW_HTTP");
          verifyLosslessPreview(bytes);
          report.losslessPreviews[asset.id] = sha(bytes);
        }
      }
      previous = await checkpoint(request, report.assets, token, people, report.queues, previous);
      if (experiment.incremental) report.waves.push({ fixtures: assets.map(({ id }) => id), ...previous });
    }
    report.cluster = previous.cluster;
    report.success = drained(report.queues) && report.cluster.passed;
    if (experiment.incremental) {
      report.establishedBeforeLastWave = report.waves[1].cluster.identities.every(({ personIds }) => personIds.length === 1);
      report.automaticPassed = report.success && report.establishedBeforeLastWave &&
        report.waves.every(({ cluster, changedAssignments }) =>
        changedAssignments.length === 0 && cluster.merges.length === 0 && cluster.splits.length === 0 &&
        cluster.negativeFalsePositives.length === 0 && cluster.unexplainedPeople.length === 0 &&
        cluster.identities.every(({ assigned, multiple, personIds }) => multiple.length === 0 &&
          (assigned < 3 || personIds.every((id) => !cluster.omittedPeople.includes(id)))));
      report.success = report.automaticPassed;
      if (experiment.retryMissing && report.automaticPassed && report.cluster.identities.some(({ unassigned }) =>
        unassigned.length > 0)) {
        await checked(request, "/jobs/facialRecognition", {
          method: "PUT", body: { command: "start", force: false }, token,
        }, 200, "RECONOCIMIENTO_FALTANTES_HTTP");
        report.queues = await poll(request, token, pause, true);
        const retry = await checkpoint(request, report.assets, token, people, report.queues, previous);
        const recovered = Object.keys(retry.assignments).filter((id) => previous.assignments[id].some(({ faceId, personId }) =>
          personId === null && retry.assignments[id].some((face) => face.faceId === faceId && face.personId)));
        report.retryMissing = { ...retry, recovered };
        report.cluster = retry.cluster;
        report.success = drained(report.queues) && report.cluster.passed && retry.changedAssignments.length === 0;
      }
    }
  } catch (error) {
    report.error = errorCode(error);
    report.success = false;
  } finally {
    if (token) {
      try {
        const closed = await checked(request, "/auth/logout", { method: "POST", token }, 200, "LOGOUT_HTTP");
        report.sessionRevoked = closed?.successful === true && (await request("/users/me", { token })).status === 401;
        report.success &&= report.sessionRevoked;
      } catch (error) {
        report.logoutError = errorCode(error);
        report.success = false;
      }
    }
  }
  return report;
}

async function main(args) {
  const { values } = parseArgs({ args, options: {
    "manifest-sha256": { type: "string" }, "config-sha256": { type: "string" },
    "expected-count": { type: "string" }, split: { type: "string" },
    order: { type: "string" }, preflight: { type: "boolean" }, "lossless-preview": { type: "boolean" },
    incremental: { type: "boolean" }, "retry-missing": { type: "boolean" },
  } });
  const manifestSha256 = values["manifest-sha256"];
  const configSha256 = values["config-sha256"];
  const expectedCount = values["expected-count"];
  const { split, order = "forward" } = values;
  need(DIGEST.test(manifestSha256) && DIGEST.test(configSha256) && /^[0-9]{1,3}$/.test(expectedCount) &&
    SPLITS.includes(split) && ["forward", "reverse"].includes(order) &&
    (!values["retry-missing"] || values.incremental), "ARGUMENTOS");
  const thresholds = loadConfig(CONFIG, configSha256);
  const fixtures = loadCorpus(ROOT, manifestSha256, Number(expectedCount), split);
  const experiment = { manifestSha256, configSha256, split, order, thresholds,
    ...(values["lossless-preview"] && { losslessPreview: true }),
    ...(values.incremental && { incremental: true }),
    ...(values["retry-missing"] && { retryMissing: true }) };
  const report = values.preflight
    ? { success: true, action: "preflight", ...experiment, fixtures: fixtures.length,
      ...(experiment.incremental && { waves: uploadWaves(fixtures, true).map((wave) => wave.length) }) }
    : await run(readInput(), fixtures, experiment);
  process.stdout.write(JSON.stringify(report) + "\n");
  process.exitCode = report.success ? 0 : 1;
}

if (require.main === module) {
  main(process.argv.slice(2)).catch((error) => {
    process.stdout.write(JSON.stringify({ success: false, error: errorCode(error) }) + "\n");
    process.exitCode = 1;
  });
}

module.exports = { clusters, loadConfig, loadCorpus, run, verifyLosslessPreview };
