import assert from "node:assert/strict";
import { AsyncLocalStorage } from "node:async_hooks";
import { spawn } from "node:child_process";
import { createHash, createHmac, randomUUID } from "node:crypto";
import { once } from "node:events";
import { chmod, link, lstat, mkdtemp, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { request } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import plugin, {
  admitOAuth, completeOwnerTurn, createOwnerService, parseTurn, readTranscript, requireAbsentSocket, scheduleOwnerWork,
} from "../openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/index.mjs";

const KEY = Buffer.from("studio-fixture-only-1111222233334444");
const ACTOR = "a".repeat(64);
const PATH = "/v1/sofia/owner-turn";
const READY_PATH = "/v1/sofia/owner-readiness";
const PROFILE = "openai:fixture";
const TURN = {
  actor_id: ACTOR,
  order_id: "00000000-0000-4000-8000-000000000001",
  request_id: "00000000-0000-4000-8000-000000000002",
  billing_mode: "sandbox",
  plan_id: "free",
  message: "Brief de prueba: edición vertical",
};
const REPLY = {
  agentId: "agency-account", provider: "openai", model: "gpt-6.1-terra",
  execution: { mode: "isolated-agent-runtime", owner: { kind: "harness", id: "codex" } },
  text: "Confirma audiencia y duración.",
};

function oracle(raw, stamp, action, path = PATH) {
  const digest = createHash("sha256").update(raw).digest("hex");
  const direction = action === "POST" ? "request" : "response";
  return "v1=" + createHmac("sha256", KEY)
    .update(`studio-sofia-owner-unix-v1\n${direction}\n${stamp}\n${action}\n${path}\n${digest}`).digest("hex");
}

function wire(value = TURN, seconds = Math.floor(Date.now() / 1000), path = PATH) {
  const raw = Buffer.from(typeof value === "string" ? value : JSON.stringify(value));
  const timestamp = String(seconds);
  const signature = oracle(raw, timestamp, "POST", path);
  return { raw, headers: { timestamp: [timestamp], signature: [signature] } };
}

function fixture() {
  const cfg = { plugins: { entries: { codex: { enabled: true } } } };
  const calls = [];
  const profiles = { [PROFILE]: { provider: "openai", type: "oauth" } };
  const api = { runtime: {
    config: { current: () => cfg },
    modelConfig: {
      resolveDefaultModelForAgent: (input) => {
        assert.deepEqual(input, { cfg, agentId: "agency-account" });
        return { provider: "openai", model: "gpt-6.1-terra" };
      },
      resolveModelRuntimePolicy: (input) => {
        assert.deepEqual(input, { config: cfg, provider: "openai", modelId: "gpt-6.1-terra", agentId: "agency-account" });
        return { policy: { id: "codex" } };
      },
    },
    agent: { resolveAgentDir: (config, agent) => {
      assert.equal(config, cfg);
      assert.equal(agent, "agency-account");
      return "/fixture/agent";
    } },
    modelAuth: { ensureAuthProfileStoreAsync: async (dir, options) => {
      assert.equal(dir, "/fixture/agent");
      assert.deepEqual(options, { config: cfg, readOnly: true, allowKeychainPrompt: false, externalCli: { mode: "none" } });
      return { profiles };
    } },
    llm: { complete: async (input) => {
      calls.push(input);
      return structuredClone(REPLY);
    } },
  } };
  return { api, cfg, calls, profiles };
}

test("el plugin sólo registra un servicio local", () => {
  const services = [];
  plugin.register({ registerService: (service) => services.push(service) });
  assert.equal(services.length, 1);
  assert.equal(services[0].id, "agency-sofia-owner-pilot");
  assert.equal(services[0].apiVersion, 2);
  assert.equal(typeof services[0].start, "function");
  assert.equal(typeof services[0].stop, "function");
});

test("la versión de servicio exige el programador del host antes de abrir el perfil", async () => {
  const services = [];
  plugin.register({ registerService: (service) => services.push(service) });
  await assert.rejects(services[0].start(), { message: "SOFIA_OWNER_PREFLIGHT_SCHEDULER" });
});

function schedulerFixture() {
  const owner = new AbortController();
  const jobs = [];
  const scheduler = {
    version: 1, signal: owner.signal,
    schedule: (job) => {
      const record = { job, cancelled: false };
      jobs.push(record);
      return { cancel: () => { record.cancelled = true; } };
    },
  };
  return { owner, jobs, scheduler };
}

test("programa una sola llamada en un contexto fresco del host y devuelve su resultado", async () => {
  const scope = new AsyncLocalStorage();
  const { jobs, scheduler } = schedulerFixture();
  const caller = new AbortController();
  const reply = scope.run("startup-closed", () =>
    scheduleOwnerWork(scheduler, TURN.request_id, caller.signal, async (signal) => {
      assert.equal(scope.getStore(), "host-fresh");
      assert.equal(signal.aborted, false);
      return "marcador";
    }));
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].job.id, "sofia-owner-" + TURN.request_id);
  assert.equal(jobs[0].job.delayMs, 0);
  assert.deepEqual(Object.keys(jobs[0].job), ["id", "delayMs", "run"]);
  await scope.run("host-fresh", () => jobs[0].job.run());
  assert.equal(await reply, "marcador");
  assert.equal(jobs[0].cancelled, false);
});

test("cancela antes de ejecutar o transfiere el cierre al trabajo ya iniciado", async () => {
  const { jobs, owner, scheduler } = schedulerFixture();
  const caller = new AbortController();
  const queued = scheduleOwnerWork(scheduler, TURN.request_id, caller.signal, () => {
    assert.fail("un turno cancelado no puede ejecutar inferencia");
  });
  caller.abort(new Error("cancelado en cola"));
  await assert.rejects(queued, /cancelado en cola/u);
  assert.equal(jobs[0].cancelled, true);
  await jobs[0].job.run();

  const running = scheduleOwnerWork(scheduler, TURN.request_id, new AbortController().signal, (signal) =>
    new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    }));
  const execution = jobs[1].job.run();
  owner.abort(new Error("servicio en cierre"));
  await assert.rejects(running, /servicio en cierre/u);
  await execution;
  assert.equal(jobs[1].cancelled, false);
});

test("clasifica un fallo al programar sin exponer el error original", async () => {
  const scheduler = {
    signal: new AbortController().signal,
    schedule() { throw new Error("Async work scope is closed"); },
  };
  await assert.rejects(scheduleOwnerWork(scheduler, TURN.request_id, new AbortController().signal, () => {
    assert.fail("el SDK no puede ejecutarse");
  }), {
    message: "SOFIA_OWNER_NATIVE_UNCLASSIFIED", stage: "HOST_SCHEDULE", category: "WORK_SCOPE_CLOSED",
  });
});

test("un socket ausente permite iniciar y un archivo presente se conserva", async () => {
  const root = await mkdtemp(join(tmpdir(), "sofia-socket-check-"));
  const socketPath = join(root, "sofia-owner.sock");
  try {
    await requireAbsentSocket(socketPath);
    await writeFile(socketPath, "fixture", { flag: "wx" });
    await assert.rejects(requireAbsentSocket(socketPath), { message: "SOFIA_OWNER_SOCKET_EXISTS" });
    assert.equal((await lstat(socketPath)).size, 7);
    if (typeof process.getuid === "function") {
      const brokenLink = join(root, "broken.sock");
      await symlink(join(root, "absent-target"), brokenLink);
      await assert.rejects(requireAbsentSocket(brokenLink), { message: "SOFIA_OWNER_SOCKET_EXISTS" });
      assert.ok((await lstat(brokenLink)).isSymbolicLink());
    }
  } finally {
    await rm(root, { recursive: true });
  }
});

test("elimina sólo el socket privado que queda tras morir el proceso", {
  skip: process.platform !== "linux",
}, async () => {
  const root = await mkdtemp(join(tmpdir(), "sofia-socket-check-"));
  const path = join(root, "mateo.sock");
  const child = spawn(process.execPath, ["-e", `
    const fs = require("node:fs");
    const net = require("node:net");
    net.createServer(socket => socket.destroy()).listen(process.argv[1], () => {
      fs.chmodSync(process.argv[1], 0o600);
      process.stdout.write("listo");
    });
  `, path]);
  const closed = once(child, "exit");
  try {
    await once(child.stdout, "data");
    await assert.rejects(requireAbsentSocket(path), { message: "SOFIA_OWNER_SOCKET_EXISTS" });
    assert.ok((await lstat(path)).isSocket());
    child.kill("SIGKILL");
    await closed;
    assert.ok((await lstat(path)).isSocket());
    await requireAbsentSocket(path);
    await assert.rejects(lstat(path), { code: "ENOENT" });
  } finally {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    await closed;
    await rm(root, { recursive: true });
  }
});

test("admite cuentas y planes válidos con firma sobre los bytes canónicos", () => {
  const settings = { key: KEY, ownerActor: ACTOR };
  const parse = ({ raw, headers }) => parseTurn(raw, headers, settings);
  assert.deepEqual(parse(wire()), TURN);
  const other = { ...TURN, actor_id: "b".repeat(64), plan_id: "ilimitado", billing_mode: "production" };
  assert.deepEqual(parse(wire(other)), other);
  for (const [entry, expected] of [
    [wire({ ...TURN, actor_id: "invalido" }), 403],
    [wire({ ...TURN, plan_id: "invalido" }), 403],
    [wire({ ...TURN, billing_mode: "invalido" }), 403],
    [wire({ ...TURN, model: "gpt-other" }), 422],
    [wire({ ...TURN, message: "á".repeat(1025) }), 422],
    [wire({ ...TURN, request_id: "00000000-0000-0000-8000-000000000002" }), 422],
    [wire(JSON.stringify(TURN).replace('"actor_id"', `"actor_id":"${ACTOR}","actor_id"`)), 422],
    [wire(TURN, Math.floor(Date.now() / 1000) - 61), 401],
    [{ ...wire(), headers: { ...wire().headers, signature: [`v1=${ACTOR}`] } }, 401],
    [{ ...wire(), raw: Buffer.from("{}") }, 401],
  ]) assert.throws(() => parse(entry), { status: expected });
});

test("pide al SDK un turno Codex aislado con el OAuth exacto y entrada literal", async () => {
  const { api, calls } = fixture();
  const signal = new AbortController().signal;
  const answer = await completeOwnerTurn(api, PROFILE)("/reset\nTexto", signal);
  assert.equal(answer, REPLY.text);
  assert.equal(calls.length, 1);
  const { systemPrompt, ...projection } = calls[0];
  assert.match(systemPrompt, /asesora audiovisual/u);
  assert.match(systemPrompt, /transcripciones pueden estar pendientes, incompletas o truncadas/u);
  assert.match(systemPrompt, /No afirmes haber visto las imágenes/u);
  assert.deepEqual(projection, {
    agentId: "agency-account", purpose: "agency-sofia-owner-pilot", maxTokens: 800, signal,
    execution: { mode: "isolated-agent-runtime", authProfileId: PROFILE, timeoutMs: 25000 },
    messages: [{ role: "user", content: '"/reset\\nTexto"' }],
  });
});

test("lee únicamente la transcripción correspondiente a la orden", async () => {
  const folder = await mkdtemp(join(tmpdir(), "sofia-transcripcion-"));
  try {
    const path = join(folder, `${TURN.order_id}.transcripcion.json`);
    assert.equal(await readTranscript(TURN.order_id, folder), null);
    await writeFile(path, JSON.stringify({ order_id: TURN.order_id, result: { text: "Plantas resistentes." } }));
    assert.equal(await readTranscript(TURN.order_id, folder), "Plantas resistentes.");
    await writeFile(path, JSON.stringify({ order_id: TURN.request_id, result: { text: "Otra orden" } }));
    await assert.rejects(readTranscript(TURN.order_id, folder), { status: 503 });
    await assert.rejects(readTranscript("../otra", folder), { status: 422 });
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

test("reúne transcripciones de varios videos en el orden declarado y rechaza identidades ajenas", async () => {
  const folder = await mkdtemp(join(tmpdir(), "sofia-multimedia-"));
  const first = "33333333-3333-4333-8333-333333333333";
  const second = "22222222-2222-4222-8222-222222222222";
  try {
    await writeFile(join(folder, `${TURN.order_id}.transcripcion.json`), JSON.stringify({
      order_id: TURN.order_id, result: { text: "Video principal." },
    }));
    const file = (id) => join(folder, `${TURN.order_id}-${id}.transcripcion.json`);
    const item = (id, position, text) => JSON.stringify({ order_id: TURN.order_id, source_id: id, position, result: { text } });
    await writeFile(file(second), item(second, 2, "Tercer video."));
    await writeFile(file(first), item(first, 1, "Segundo video."));
    assert.equal(await readTranscript(TURN.order_id, folder),
      "[Vídeo 1]\nVideo principal.\n\n[Vídeo 2]\nSegundo video.\n\n[Vídeo 3]\nTercer video.");
    await writeFile(file(first), item(second, 1, "Identidad incorrecta."));
    await assert.rejects(readTranscript(TURN.order_id, folder), { status: 503 });
  } finally { await rm(folder, { recursive: true, force: true }); }
});

test("entrega a Sofía el mensaje y la transcripción de la orden", async () => {
  const { api, calls } = fixture();
  const signal = new AbortController().signal;
  const read = async (orderId) => {
    assert.equal(orderId, TURN.order_id);
    return "La planta necesita poca agua.";
  };
  await completeOwnerTurn(api, PROFILE, undefined, read)(TURN.message, signal, TURN.order_id);
  assert.deepEqual(JSON.parse(calls[0].messages[0].content), {
    mensaje: TURN.message,
    transcripcion: "La planta necesita poca agua.",
  });
});

test("rechaza API key, OAuth ausente, transporte TCP y modelo alternativo antes de invocar", async () => {
  const signal = new AbortController().signal;
  for (const tamper of [
    ({ profiles }) => { profiles[PROFILE].type = "api_key"; },
    ({ profiles }) => { delete profiles[PROFILE]; },
    ({ cfg }) => { cfg.plugins.entries.codex.config = { appServer: { transport: "websocket" } }; },
    ({ cfg }) => { cfg.plugins.entries.codex.config = { appServer: { homeScope: "user" } }; },
    ({ cfg }) => { cfg.plugins.entries.codex.config = { appServer: { command: "/other/codex" } }; },
    ({ cfg }) => { cfg.plugins.entries.codex.config = { appServer: { nativeHookRelay: {} } }; },
    ({ api }) => { api.runtime.modelConfig.resolveDefaultModelForAgent = () => ({ provider: "openai", model: "other" }); },
  ]) {
    const target = fixture();
    tamper(target);
    await assert.rejects(completeOwnerTurn(target.api, PROFILE)(TURN.message, signal), { status: 503 });
    assert.deepEqual(target.calls, []);
  }
});

test("rechaza la respuesta si OpenClaw devuelve un fallback o pierde el aislamiento", async () => {
  for (const changes of [
    { responseModel: "other" },
    { execution: { mode: "simple-completion" } },
    { execution: { mode: "isolated-agent-runtime", owner: { kind: "harness", id: "openclaw" } } },
    { text: "á".repeat(4097) },
  ]) {
    const { api } = fixture();
    api.runtime.llm.complete = async () => ({ ...REPLY, ...changes });
    await assert.rejects(completeOwnerTurn(api, PROFILE)(TURN.message, new AbortController().signal), { status: 503 });
  }
});

test("reduce fallos del SDK a códigos admitidos y causas acotadas", async () => {
  const { api } = fixture();
  const signal = new AbortController().signal;
  api.runtime.llm.complete = async () => {
    const cause = Object.assign(new Error("valor privado"), { code: "runtime-unavailable" });
    throw Object.assign(new Error("detalle privado", { cause }), { code: "LLM_RUNTIME_UNAVAILABLE" });
  };
  await assert.rejects(completeOwnerTurn(api, PROFILE)(TURN.message, signal), {
    message: "SOFIA_OWNER_NATIVE_LLM_RUNTIME_UNAVAILABLE", category: "RUNTIME_UNAVAILABLE", stage: "LLM_ASYNC",
  });
  api.runtime.llm.complete = async () => {
    const cause = Object.assign(new Error("ruta privada"), { code: "causa-no-publicable" });
    throw Object.assign(new Error("detalle privado", { cause }), { code: "valor-no-publicable" });
  };
  await assert.rejects(completeOwnerTurn(api, PROFILE)(TURN.message, signal), (error) => {
    assert.equal(error.message, "SOFIA_OWNER_NATIVE_UNCLASSIFIED");
    assert.equal(error.category, undefined);
    assert.equal(error.stage, "LLM_ASYNC");
    return true;
  });
});

test("proyecta etapa de captura y sólo marcos definidos sin exponer mensajes o rutas", async () => {
  const signal = new AbortController().signal;
  const missing = fixture();
  delete missing.api.runtime.llm;
  await assert.rejects(completeOwnerTurn(missing.api, PROFILE)(TURN.message, signal), {
    message: "SOFIA_OWNER_NATIVE_UNCLASSIFIED", category: "CALL_SHAPE", stage: "LLM_SURFACE",
  });
  const scope = fixture();
  scope.api.runtime.llm.complete = () => {
    const failure = new Error('Plugin "secret-looking-example" runtime is no longer active.');
    failure.stack = "private headline\n at runWithPluginScope (/name-sensitive/registry-123.mjs:4:5)"
      + "\n at runtime-llm.runtime (/secret-path/runtime-llm.runtime.mjs:5:6)"
      + "\n at runWithAsyncWorkResources (/should-not-be-returned/async-work-resources-a.mjs:5:7)";
    throw failure;
  };
  await assert.rejects(completeOwnerTurn(scope.api, PROFILE)(TURN.message, signal), (error) => {
    assert.equal(error.stage, "LLM_SCOPE_SYNC");
    assert.equal(error.category, "PLUGIN_GENERATION");
    assert.deepEqual(error.frames, ["PLUGIN_SCOPE", "LLM_RUNTIME"]);
    assert.doesNotMatch(JSON.stringify(error), /private|secret|name-sensitive/);
    return true;
  });
});

test("identifica etapa SDK por código sin devolver errores ni datos originales", async () => {
  for (const [stage, tamper] of [
    ["CONFIG_API", ({ api }) => { api.runtime.config.current = () => { throw new Error("detalle privado"); }; }],
    ["APP_SERVER", ({ cfg }) => { cfg.plugins.entries.codex.enabled = false; }],
    ["MODEL_API", ({ api }) => { delete api.runtime.modelConfig.resolveDefaultModelForAgent; }],
    ["MODEL_DEFAULT", ({ api }) => { api.runtime.modelConfig.resolveDefaultModelForAgent = () => null; }],
    ["RUNTIME_API", ({ api }) => { delete api.runtime.modelConfig.resolveModelRuntimePolicy; }],
    ["MODEL_RUNTIME", ({ api }) => { api.runtime.modelConfig.resolveModelRuntimePolicy = () => ({}); }],
    ["AGENT_DIR", ({ api }) => { api.runtime.agent.resolveAgentDir = () => "relative"; }],
    ["PROFILE_STORE", ({ api }) => { api.runtime.modelAuth.ensureAuthProfileStoreAsync = async () => { throw new Error("oauth privado"); }; }],
    ["OAUTH_TYPE", ({ profiles }) => { delete profiles[PROFILE]; }],
  ]) {
    const target = fixture();
    tamper(target);
    await assert.rejects(admitOAuth(target.api, PROFILE), { message: `SOFIA_OWNER_PREFLIGHT_${stage}` });
    assert.deepEqual(target.calls, []);
  }
});

function send(socketPath, turn, path = PATH) {
  const { raw, headers } = wire(turn, undefined, path);
  return new Promise((done, fail) => {
    const req = request({ socketPath, method: "POST", path, headers: {
      "content-type": "application/json", "content-length": raw.length,
      timestamp: headers.timestamp[0], signature: headers.signature[0],
    } }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => done({ status: res.statusCode, headers: res.headers, raw: Buffer.concat(chunks) }));
      res.on("error", fail);
    });
    req.on("error", fail);
    req.end(raw);
  });
}

test("un recibo en publicación o inválido no detiene los turnos de Sofía", {
  skip: process.platform !== "linux",
}, async () => {
  const root = await mkdtemp(join(tmpdir(), "sofia-transcripcion-socket-"));
  const socketPath = join(root, "sofia-owner.sock");
  const receipt = join(root, TURN.order_id + ".transcripcion.json");
  const publishingLink = join(root, ".orden-publicando");
  const extraLink = join(root, ".otro-enlace");
  const otherOrder = randomUUID();
  const { api, calls } = fixture();
  const complete = completeOwnerTurn(api, PROFILE, undefined, (id) => readTranscript(id, root));
  const service = createOwnerService({ socketPath, key: Buffer.from(KEY) }, complete, () => admitOAuth(api, PROFILE));
  const sendTurn = (order_id = TURN.order_id, message = TURN.message, path = PATH) =>
    send(socketPath, { ...TURN, order_id, request_id: randomUUID(), message }, path);
  try {
    await service.start();
    assert.equal((await sendTurn(otherOrder, "startup-readiness", READY_PATH)).status, 200);
    await writeFile(receipt, JSON.stringify({ order_id: TURN.order_id, result: { text: "Plantas resistentes." } }));
    await link(receipt, publishingLink);
    assert.equal((await sendTurn()).status, 200);
    assert.equal(JSON.parse(calls[0].messages[0].content), TURN.message);

    await link(receipt, extraLink);
    await assert.rejects(readTranscript(TURN.order_id, root), { status: 503 });
    await unlink(extraLink);
    await unlink(publishingLink);
    assert.equal((await sendTurn()).status, 200);
    assert.deepEqual(JSON.parse(calls[1].messages[0].content), {
      mensaje: TURN.message, transcripcion: "Plantas resistentes.",
    });

    await writeFile(receipt, "{recibo inválido");
    const invalid = await sendTurn();
    assert.equal(invalid.status, 503);
    assert.deepEqual(JSON.parse(invalid.raw), { error: "SOFIA_OWNER_REJECTED" });
    assert.equal(calls.length, 2);
    assert.equal((await sendTurn(otherOrder)).status, 200);
    assert.equal(calls.length, 3);

    api.runtime.llm.complete = async () => {
      throw Object.assign(new Error("timeout de ensayo"), { code: "LLM_COMPLETION_TIMEOUT" });
    };
    const failure = await sendTurn(otherOrder);
    assert.equal(failure.status, 503);
    assert.equal(JSON.parse(failure.raw).error, "SOFIA_OWNER_NATIVE_LLM_COMPLETION_TIMEOUT");
    assert.equal((await sendTurn(otherOrder)).status, 429);
  } finally {
    await service.stop();
    await rm(root, { recursive: true, force: true });
  }
});

test("Unix 0600 verifica firma, excluye concurrencia y detiene admisión tras un fallo", {
  skip: typeof process.getuid !== "function",
}, async () => {
  const root = await mkdtemp(join(tmpdir(), "sofia-owner-"));
  await chmod(root, 0o700);
  const socketPath = join(root, "sofia-owner.sock");
  const enter = Promise.withResolvers();
  const release = Promise.withResolvers();
  const service = createOwnerService({ socketPath, key: Buffer.from(KEY), ownerActor: ACTOR }, async (message) => {
    if (message === "error de proveedor") throw new Error("detalle privado");
    enter.resolve();
    await release.promise;
    return REPLY.text;
  });
  try {
    await service.start();
    assert.equal((await lstat(socketPath)).mode & 0o7777, 0o600);
    const first = send(socketPath, TURN);
    await enter.promise;
    const alternate = { ...TURN, request_id: "00000000-0000-4000-8000-000000000003" };
    assert.equal((await send(socketPath, alternate)).status, 429);
    release.resolve();
    const reply = await first;
    assert.equal(reply.status, 200);
    assert.equal(reply.headers.signature, oracle(reply.raw, reply.headers.timestamp, "200"));
    assert.equal(JSON.parse(reply.raw).text, REPLY.text);
    assert.equal((await send(socketPath, TURN)).status, 409);
    const failure = await send(socketPath, { ...alternate, message: "error de proveedor" });
    assert.equal(failure.status, 503);
    assert.equal(failure.headers.signature, oracle(failure.raw, failure.headers.timestamp, "503"));
    assert.deepEqual(JSON.parse(failure.raw), { actor_id: TURN.actor_id, order_id: TURN.order_id,
      request_id: alternate.request_id, error: "SOFIA_OWNER_NATIVE_UNCLASSIFIED" });
    assert.doesNotMatch(String(failure.raw), /detalle/);
    const third = { ...TURN, request_id: "00000000-0000-4000-8000-000000000004" };
    assert.equal((await send(socketPath, third)).status, 429);
  } finally {
    release.resolve();
    await service.stop();
    await rm(root, { recursive: true });
  }
});

test("readiness Unix firmada diagnostica SDK y deja inferencia sin invocar", {
  skip: typeof process.getuid !== "function",
}, async () => {
  const root = await mkdtemp(join(tmpdir(), "sofia-ready-"));
  await chmod(root, 0o700);
  const socketPath = join(root, "sofia-owner.sock");
  const { api, calls, profiles } = fixture();
  const service = createOwnerService({ socketPath, key: Buffer.from(KEY), ownerActor: ACTOR },
    completeOwnerTurn(api, PROFILE), () => admitOAuth(api, PROFILE));
  try {
    await service.start();
    assert.equal((await send(socketPath, TURN)).status, 503);
    const good = await send(socketPath, { ...TURN, message: "startup-readiness" }, READY_PATH);
    assert.equal(good.status, 200);
    assert.equal(good.headers.signature, oracle(good.raw, good.headers.timestamp, "200", READY_PATH));
    assert.equal(JSON.parse(good.raw).text, "ready");
    delete profiles[PROFILE];
    const bad = await send(socketPath, { ...TURN, request_id: "00000000-0000-4000-8000-000000000003",
      message: "startup-readiness" }, READY_PATH);
    assert.equal(bad.status, 503);
    assert.equal(bad.headers.signature, oracle(bad.raw, bad.headers.timestamp, "503", READY_PATH));
    assert.deepEqual(JSON.parse(bad.raw), { error: "SOFIA_OWNER_PREFLIGHT_OAUTH_TYPE" });
    assert.equal((await send(socketPath, { ...TURN, request_id: "00000000-0000-4000-8000-000000000004" })).status, 503);
    assert.deepEqual(calls, []);
  } finally {
    await service.stop();
    await rm(root, { recursive: true });
  }
});
