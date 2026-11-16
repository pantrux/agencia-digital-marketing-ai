// Aceptación QUB contra la API local del contenedor descartable.
"use strict";

const { createHash } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { setTimeout: wait } = require("node:timers/promises");

const ORIGIN = "http://127.0.0.1:2283/api";
const DIRECTORY = "/run/immich/fixtures";
const CONFIG = "/run/immich/face-accept.json";
const CONFIG_SHA = "781c60625b6d4f32f448571dfa641db222dd91405a69c47e140e03c8f58cedc1";
const QUB_RECEIPT_SHA = "5ab9cb919ae985801232d858e38b0bdcdba79a8c22c3078748217689c637e4c4";
const MODEL = "agency_yunet_auraface_v1";
const EMAIL = "immich-face-accept@example.invalid";
const IDENTITIES = ["00001", "00006", "00007"];
const QUEUES = ["metadataExtraction", "thumbnailGeneration", "faceDetection", "facialRecognition"];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const FILE_ERRORS = new Set(["EACCES", "EPERM", "ENOENT", "ENOTDIR", "EISDIR", "EIO", "EBADF", "EINVAL"]);
const FIXTURES = Object.freeze({
  "00001_A_270_90_128.png": "4a4e9396fee1a6d8cac34e86e368f8123279335ce1f07ba128e6dd7c29d5981f",
  "00001_A_240_90_128.png": "99658e522b6acb2faaa02e577c729b864389fdd81a4c03a0ac18609b2456d91c",
  "00001_A_300_90_128.png": "93c4dd6c8fe18bce765df00aa05b91de8031e56417aa31ded8a2f5e041049e03",
  "00001_B_270_90_128.png": "7595de73fd28a1b07c2a85a765f0bd959e74ad981c09e1279eb07fd4d0ea7831",
  "00001_C_270_75_128.png": "e7b0d55979515e78373668137fb5e86a4356096911bec352083305a4e8054eb5",
  "00001_D_270_90_128.png": "c5d835d66fffce5fc6629bfdd538b5cb2fe4337df0665c2181afe22f33c4eb3f",
  "00006_A_270_90_128.png": "e773525a44454463f630fdf5b7099a361405f6858d4293c51fdde66070d2a344",
  "00006_A_240_90_128.png": "b80b0d740d7f1fc5ba71eb3517eefef2a84442e77febfb3e1699aae179321ffa",
  "00006_A_300_90_128.png": "d68107b8829c67a4067b00f84c6768cfeaed3be8353efcf6c17ecc4a999b5d84",
  "00006_B_270_90_128.png": "102fceeff75eb47a445dcdcde4ce27ae9e1e92399e2762b4d4debe53074d27ea",
  "00006_C_270_75_128.png": "dec77fcc6fc3ec07412df1bea4af0f5edd9b1378d920e58ef6009315a2d00992",
  "00006_D_270_90_128.png": "b871d27b50194015ba65c778f1aa6a8b98b8ec56f877c8a0114a2574fc374afc",
  "00007_A_270_90_128.png": "0b51aa5a4ff14e354068ab6b19f6c809023a550f5f2755390d8e3c283c8ae6c3",
  "00007_A_240_90_128.png": "170f4beb9b196d8590840c0de1c7291d694661300197c883986363673d2fdcb0",
  "00007_A_300_90_128.png": "56aeacce0a84de54d4332a5bb1cdcf01d99e18935d3f2561dd7da89f2f0a8ba1",
  "00007_B_270_90_128.png": "d3afe0a2d8eb6ef9cd60eab88e4a6ddc82ba9eff0460bf4fdbd350e34fe5c37d",
  "00007_C_270_75_128.png": "9f262cd8a866797d22aa47e90bcb463208a6e2956c78f9fd4fc022512d71f165",
  "00007_D_270_90_128.png": "5f3a205962ef8335395c70fbeb73e0338659b41a3d5650ef1781b070261dc7b6",
});

function need(condition, code) {
  if (!condition) throw new Error(code);
}

function sha(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function fileOperation(label, action) {
  try {
    return action();
  } catch (cause) {
    const code = FILE_ERRORS.has(cause?.code) ? cause.code : "FALLO";
    throw new Error(`${label}_${code}`);
  }
}

function loadFixtures(directory) {
  const root = fileOperation("CORPUS_RAIZ", () => fs.lstatSync(directory));
  need(root.isDirectory(), "CORPUS_RAIZ_TIPO");
  const names = fileOperation("CORPUS_LISTADO", () => fs.readdirSync(directory))
    .filter((name) => name.toLowerCase().endsWith(".png"));
  need(names.length === 18 && names.every((name) => Object.hasOwn(FIXTURES, name)), "CORPUS_LISTA");
  return Object.entries(FIXTURES).map(([name, digest]) => {
    const filename = path.join(directory, name);
    const file = fileOperation("CORPUS_ESTADO", () => fs.lstatSync(filename));
    need(file.isFile() && file.size >= 24 && file.size <= 128 * 1024, "CORPUS_ARCHIVO");
    const bytes = fileOperation("CORPUS_LECTURA", () => fs.readFileSync(filename));
    need(sha(bytes) === digest, "CORPUS_SHA");
    need(bytes.readUInt32BE(16) === 128 && bytes.readUInt32BE(20) === 128, "CORPUS_DIMENSIONES");
    return { name, digest, bytes, identity: name.slice(0, 5) };
  });
}

async function call(endpoint, { method = "GET", body, token, binary = false, maxBytes = 128 * 1024 } = {}) {
  const headers = { Accept: binary ? "image/*, application/octet-stream" : "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(ORIGIN + endpoint, {
    method, body, headers, redirect: "error", signal: AbortSignal.timeout(12000),
  });
  const chunks = [];
  let size = 0;
  if (response.body) {
    for await (const chunk of response.body) {
      size += chunk.length;
      need(size <= maxBytes, "API_RESPUESTA_LIMITE");
      chunks.push(chunk);
    }
  }
  const raw = Buffer.concat(chunks, size);
  return { status: response.status, body: binary ? raw : raw.length ? JSON.parse(raw.toString("utf8")) : null };
}

async function checked(request, endpoint, options, status, error) {
  const result = await request(endpoint, options);
  need(result.status === status, error);
  return result.body;
}

function peopleIds(people) {
  need(Array.isArray(people?.people) && people.hasNextPage === false &&
    people.people.every((person) => UUID.test(person.id)), "PERSONAS_RESPUESTA");
  return people.people.map((person) => person.id);
}

function verifyConfig(config) {
  const ml = config?.machineLearning;
  const face = ml?.facialRecognition;
  need(ml?.enabled === true && face?.enabled === true && face.modelName === MODEL &&
    face.minScore === 0.7 && face.maxDistance === 0.575 && face.minFaces === 3 &&
    ml.clip.enabled === false && ml.ocr.enabled === false && ml.urls.length === 1 &&
    ml.urls[0] === "http://immich-ml-auth:3003" && config.metadata.faces.import === false &&
    config.nightlyTasks.clusterNewFaces === false && config.job.faceDetection.concurrency === 1,
    "CONFIG_API");
}

async function authenticate(request, password, resume, name = "Ensayo facial QUB") {
  const version = await checked(request, "/server/version", {}, 200, "VERSION_HTTP");
  need(version?.major === 3 && version.minor === 3 && version.patch === 6 &&
    version.prerelease === null, "VERSION");
  await checked(request, "/users/me", {}, 401, "SIN_AUTENTICACION");
  if (!resume) {
    await checked(request, "/auth/admin-sign-up", { method: "POST", body: {
      email: EMAIL, name, password,
    } }, 201, "ALTA_TEMPORAL");
  }
  const session = await checked(request, "/auth/login", { method: "POST", body: {
    email: EMAIL, password,
  } }, 201, "LOGIN_TEMPORAL");
  need(session?.isAdmin === true && typeof session.accessToken === "string" &&
    session.accessToken.length >= 16, "LOGIN_RESPUESTA");
  return session.accessToken;
}

async function upload(request, fixtures, token) {
  const assets = [];
  for (const fixture of fixtures) {
    const form = new FormData();
    form.append("assetData", new Blob([fixture.bytes], { type: "image/png" }), fixture.name);
    form.append("fileCreatedAt", "2026-01-01T00:00:00.000Z");
    form.append("fileModifiedAt", "2026-01-01T00:00:00.000Z");
    form.append("visibility", "timeline");
    const row = await checked(request, "/assets", { method: "POST", body: form, token }, 201, "CARGA_HTTP");
    need(UUID.test(row?.id) && row.duplicate !== true && !assets.some((item) => item.assetId === row.id),
      "CARGA_DUPLICADA");
    assets.push({ name: fixture.name, identity: fixture.identity, pngSha256: fixture.digest, assetId: row.id });
  }
  return assets;
}

function resumeAssets(fixtures, saved) {
  need(Array.isArray(saved) && saved.length === fixtures.length, "REANUDACION_LISTA");
  const ids = new Set();
  return fixtures.map((fixture, index) => {
    const previous = saved[index];
    need(previous?.name === fixture.name && previous.identity === fixture.identity &&
      previous.pngSha256 === fixture.digest && UUID.test(previous.assetId) && !ids.has(previous.assetId),
      "REANUDACION_IDENTIDAD");
    ids.add(previous.assetId);
    return { name: fixture.name, identity: fixture.identity, pngSha256: fixture.digest, assetId: previous.assetId };
  });
}

async function verifyOriginals(request, assets, token) {
  for (const asset of assets) {
    const original = await checked(request, `/assets/${asset.assetId}/original`, { token, binary: true },
      200, "ORIGINAL_HTTP");
    need(Buffer.isBuffer(original) && sha(original) === asset.pngSha256, "ORIGINAL_SHA");
  }
}

function queueStats(body, name) {
  const values = body?.statistics;
  const fields = ["active", "waiting", "delayed", "failed", "completed", "paused"];
  const suffix = name.replace(/([a-z])([A-Z])/g, "$1_$2").toUpperCase();
  need(body?.name === name, `COLA_NOMBRE_${suffix}`);
  need(typeof body.isPaused === "boolean", `COLA_TIPO_PAUSA_${suffix}`);
  for (const key of fields) {
    need(Number.isSafeInteger(values?.[key]) && values[key] >= 0,
      `COLA_CONTEO_${suffix}_${key.toUpperCase()}`);
  }
  return { isPaused: body.isPaused, ...Object.fromEntries(fields.map((key) => [key, values[key]])) };
}

async function poll(request, token, pause = wait, stopOnFailure = false, deadline = performance.now() + 300000) {
  let stable = 0;
  let stats = {};
  while (performance.now() < deadline && stable < 3) {
    const rows = await Promise.all(QUEUES.map(async (name) => {
      const body = await checked(request, `/queues/${name}`, { token }, 200, "COLA_HTTP");
      return [name, queueStats(body, name)];
    }));
    stats = Object.fromEntries(rows);
    if (stopOnFailure && Object.values(stats).some((row) => row.failed > 0)) break;
    stable = Object.values(stats).every(queueIdle) ? stable + 1 : 0;
    if (stable < 3) await pause(2500);
  }
  return { drained: stable === 3, stats };
}

function queueIdle(row) {
  return row.active + row.waiting + row.delayed + row.paused === 0;
}

async function reprocessAssets(request, assets, token, pause, report) {
  const auth = { token };
  const users = await checked(request, "/admin/users", auth, 200, "REPROCESO_USUARIOS_HTTP");
  need(Array.isArray(users) && users.length === 1 && users[0].email === EMAIL && users[0].isAdmin === true,
    "REPROCESO_USUARIOS");
  const count = await checked(request, "/assets/statistics", auth, 200, "REPROCESO_INVENTARIO_HTTP");
  need(count?.images === 18 && count.total === 18 && count.videos === 0, "REPROCESO_INVENTARIO");

  const initial = await poll(request, token, pause);
  report.reprocess = { initial, stages: [] };
  need(initial.drained && Object.values(initial.stats).every((row) => row.failed === 0), "REPROCESO_COLAS_OCUPADAS");
  const metadata = "metadataExtraction";
  need(QUEUES.filter((name) => name !== metadata).every((name) => !initial.stats[name].isPaused),
    "REPROCESO_OTRAS_COLAS_PAUSADAS");
  if (initial.stats[metadata].isPaused) {
    const updated = await checked(request, `/queues/${metadata}`, {
      method: "PUT", body: { isPaused: false }, token,
    }, 200, "REPROCESO_METADATA_REANUDAR_HTTP");
    const state = queueStats(updated, metadata);
    need(!state.isPaused && queueIdle(state) && state.failed === 0, "REPROCESO_METADATA_REANUDAR");
    report.reprocess.metadataResumed = true;
  }

  const assetIds = assets.map((asset) => asset.assetId);
  let after;
  for (const name of ["refresh-metadata", "regenerate-thumbnail", "refresh-faces"]) {
    await checked(request, "/assets/jobs", { method: "POST", body: { name, assetIds }, token }, 204,
      `REPROCESO_ENCOLAR_${name.toUpperCase().replaceAll("-", "_")}`);
    after = await poll(request, token, pause, true);
    report.reprocess.stages.push({ name, ...after });
    need(after.drained && Object.values(after.stats).every((row) => row.failed === 0),
      `REPROCESO_INCOMPLETO_${name.toUpperCase().replaceAll("-", "_")}`);
    if (name === "refresh-metadata") {
      for (const asset of assets) {
        const info = await checked(request, `/assets/${asset.assetId}`, auth, 200, "REPROCESO_METADATA_HTTP");
        need(info?.id === asset.assetId && info.width === 128 && info.height === 128 &&
          info.exifInfo?.exifImageWidth === 128 && info.exifInfo?.exifImageHeight === 128,
          "REPROCESO_METADATA_AUSENTE");
      }
      report.reprocess.metadataVerified = assetIds.length;
    }
    if (name === "regenerate-thumbnail") {
      for (const id of assetIds) {
        const preview = await checked(request, `/assets/${id}/thumbnail?size=preview`, { token, binary: true },
          200, "REPROCESO_PREVIEW_HTTP");
        need(Buffer.isBuffer(preview) && preview.length > 0, "REPROCESO_PREVIEW_VACIO");
      }
      report.reprocess.previewsVerified = assetIds.length;
    }
  }
  return after;
}

function inspectFaces(faces) {
  need(Array.isArray(faces) && faces.every((face) => UUID.test(face?.id) &&
    (face.sourceType === undefined || face.sourceType === "machine-learning") &&
    (face.person === null || UUID.test(face.person?.id))), "ROSTROS_RESPUESTA");
  return faces.map((face) => ({ faceId: face.id, personId: face.person?.id ?? null }));
}

async function getFaces(request, assetId, token) {
  return inspectFaces(await checked(request, `/faces?id=${assetId}`, { token }, 200, "ROSTROS_HTTP"));
}

async function readFaces(request, assets, token) {
  for (const asset of assets) {
    asset.faces = await getFaces(request, asset.assetId, token);
  }
}

async function recoverMissingFaces(request, token, pause, report) {
  const missing = report.assets.filter((asset) => asset.faces.length === 0);
  const recovery = {
    initial: report.assets.map(({ assetId, faces }) => ({ assetId, faces })),
    initialMissing: missing.map(({ assetId }) => assetId),
    attempts: [],
  };
  report.reprocess.faceRecovery = recovery;
  need(report.assets.every((asset) => asset.faces.length <= 1), "REPROCESO_ROSTROS_MULTIPLES");

  const deadline = performance.now() + 300000;
  for (const asset of missing) {
    need(performance.now() < deadline, "REPROCESO_REINTENTO_LIMITE");
    const attempt = { assetId: asset.assetId };
    recovery.attempts.push(attempt);
    await checked(request, "/assets/jobs", {
      method: "POST", body: { name: "refresh-faces", assetIds: [asset.assetId] }, token,
    }, 204, "REPROCESO_REINTENTO_ENCOLAR_HTTP");
    attempt.queues = await poll(request, token, pause, true, deadline);
    report.queues = attempt.queues;
    need(attempt.queues.drained && Object.values(attempt.queues.stats).every((row) => row.failed === 0),
      "REPROCESO_REINTENTO_COLAS");
    attempt.faces = await getFaces(request, asset.assetId, token);
    asset.faces = attempt.faces;
    need(attempt.faces.length <= 1, "REPROCESO_ROSTROS_MULTIPLES");
  }

  // Recognition can update assignments while the retry jobs drain.
  if (missing.length > 0) await readFaces(request, report.assets, token);
}

function clusters(assets, listed) {
  const owners = new Map();
  const identities = IDENTITIES.map((id) => {
    const group = assets.filter((asset) => asset.identity === id);
    const assignments = group.filter((asset) => asset.faces.length === 1 && asset.faces[0].personId);
    const people = [...new Set(assignments.map((asset) => asset.faces[0].personId))];
    for (const personId of people) {
      if (!owners.has(personId)) owners.set(personId, new Set());
      owners.get(personId).add(id);
    }
    return { identity: id, assigned: assignments.length, personIds: people,
      missing: group.filter((asset) => asset.faces.length === 0).map((asset) => asset.name),
      unassigned: group.filter((asset) => asset.faces.length === 1 && asset.faces[0].personId === null)
        .map((asset) => asset.name) };
  });
  const collisions = [...owners].filter(([, ids]) => ids.size > 1)
    .map(([personId, ids]) => ({ personId, identities: [...ids] }));
  const strict = assets.every((asset) => asset.faces.length === 1) &&
    collisions.length === 0 && identities.every((group) => group.assigned >= 3 && group.personIds.length === 1) &&
    new Set(listed).size === owners.size && listed.every((personId) => owners.has(personId));
  return { strict, collisions, identities };
}

async function run(input, fixtures, request = call, pause = wait, resume = false, reprocess = false) {
  const { password, order = "forward" } = input;
  need(typeof password === "string" && password.length >= 16 && password.length <= 128 &&
    ["forward", "reverse"].includes(order), "ENTRADA");
  need(!reprocess || resume, "REPROCESO_REQUIERE_RECIBO");
  const ordered = order === "reverse" ? fixtures.slice().reverse() : fixtures;
  const saved = resume ? resumeAssets(ordered, input.assets) : null;
  const report = { success: false, calibrated: false, configSha256: CONFIG_SHA,
    qubCudaReceiptSha256: QUB_RECEIPT_SHA, order, resumed: resume, version: "3.3.6",
    model: MODEL, minScore: 0.7, maxDistance: 0.575, minFaces: 3 };
  let token;
  try {
    token = await authenticate(request, password, resume);
    const auth = { token };
    verifyConfig(await checked(request, "/admin/config", auth, 200, "CONFIG_HTTP"));
    const libraries = await checked(request, "/libraries", auth, 200, "BIBLIOTECAS_HTTP");
    need(Array.isArray(libraries) && libraries.length === 0, "BIBLIOTECAS_EXISTENTES");
    const people = "/people?withHidden=true";
    if (resume) {
      report.assets = saved;
      await verifyOriginals(request, saved, token);
      report.originalsVerified = saved.length;
    } else {
      need(peopleIds(await checked(request, people, auth, 200, "PERSONAS_HTTP")).length === 0,
        "PERSONAS_EXISTENTES");
      report.assets = await upload(request, ordered, token);
    }
    report.queues = reprocess
      ? await reprocessAssets(request, report.assets, token, pause, report)
      : await poll(request, token, pause);
    await readFaces(request, report.assets, token);
    if (reprocess) await recoverMissingFaces(request, token, pause, report);
    const listed = peopleIds(await checked(request, people, auth, 200, "PERSONAS_HTTP"));
    report.cluster = clusters(report.assets, listed);
    report.pausedQueues = Object.entries(report.queues.stats).filter(([, row]) => row.isPaused).map(([name]) => name);
    if (report.pausedQueues.length > 0 &&
        (!report.queues.drained || report.assets.some((asset) => asset.faces.length === 0))) {
      report.error = "COLA_PAUSADA_PENDIENTE";
    }
    report.success = report.queues.drained && Object.values(report.queues.stats).every((row) => row.failed === 0) &&
      report.cluster.strict;
  } catch (error) {
    report.error = errorCode(error);
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

function errorCode(error) {
  return error?.name === "Error" && /^[A-Z0-9_]+$/.test(error.message) ? error.message : "OPERACION_FALLIDA";
}

function readInput(fd = 0) {
  const buffer = Buffer.alloc(8193);
  const length = fileOperation("ENTRADA_LECTURA", () => {
    let size = 0;
    while (size < buffer.length) {
      const chunk = fs.readSync(fd, buffer, size, buffer.length - size, null);
      if (chunk === 0) break;
      size += chunk;
    }
    return size;
  });
  need(length > 0, "ENTRADA_VACIA");
  need(length < buffer.length, "ENTRADA_LIMITE");
  let value;
  try {
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, length)));
  } catch {
    throw new Error("ENTRADA_JSON");
  }
  need(value && typeof value === "object" && !Array.isArray(value), "ENTRADA_OBJETO");
  return value;
}

function configFixtures() {
  const config = fileOperation("CONFIG_LECTURA", () => fs.readFileSync(CONFIG));
  need(sha(config) === CONFIG_SHA, "CONFIG_SHA");
  return loadFixtures(DIRECTORY);
}

async function main(args) {
  need(args.length === 0 ||
    (args.length === 1 && ["--preflight", "--resume", "--reprocess"].includes(args[0])), "ARGUMENTOS");
  const fixtures = configFixtures();
  const report = args[0] === "--preflight"
    ? { success: true, action: "preflight", configSha256: CONFIG_SHA, fixtures: fixtures.length,
        fixtureBytes: fixtures.reduce((total, fixture) => total + fixture.bytes.length, 0) }
    : await run(readInput(), fixtures, call, wait, args.length === 1, args[0] === "--reprocess");
  process.stdout.write(JSON.stringify(report) + "\n");
  process.exitCode = report.success ? 0 : 1;
}

if (require.main === module) {
  main(process.argv.slice(2)).catch((error) => {
    process.stdout.write(JSON.stringify({ success: false, error: errorCode(error) }) + "\n");
    process.exitCode = 1;
  });
}

module.exports = {
  FIXTURES, loadFixtures, readInput, run,
  api: Object.freeze({ authenticate, call, checked, errorCode, need, peopleIds, poll, readFaces, sha, upload }),
};
