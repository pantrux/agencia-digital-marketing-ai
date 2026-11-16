import assert from "node:assert/strict";
import { test } from "node:test";
import { createIsolatedPlugin, completeOwnerTurn } from "../openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/index.mjs";

const policy = { id: "agency-mateo-production", agent: "agency-strategist", model: "gpt-6.1-sol", prompt: "Tres ángulos en JSON", timeoutMs: 40000 };
const profile = "openai:fixture";

test("Mateo usa el runtime aislado y sólo el perfil OAuth admitido", async () => {
  const calls = [];
  const api = { runtime: {
    config: { current: () => ({ plugins: { entries: { codex: { enabled: true } } } }) },
    modelConfig: {
      resolveDefaultModelForAgent: ({ agentId }) => {
        assert.equal(agentId, policy.agent);
        return { provider: "openai", model: policy.model };
      },
      resolveModelRuntimePolicy: ({ modelId }) => {
        assert.equal(modelId, policy.model);
        return { policy: { id: "codex" } };
      },
    },
    agent: { resolveAgentDir: () => "/fixture/mateo" },
    modelAuth: { ensureAuthProfileStoreAsync: () => ({ profiles: { [profile]: { provider: "openai", type: "oauth" } } }) },
    llm: { complete: async (input) => {
      calls.push(input);
      return { agentId: policy.agent, provider: "openai", model: policy.model, text: "Tres ángulos", execution: {
        mode: "isolated-agent-runtime", owner: { kind: "harness", id: "codex" },
      } };
    } },
  } };
  const signal = new AbortController().signal;
  assert.equal(await completeOwnerTurn(api, profile, policy)("brief", signal), "Tres ángulos");
  assert.deepEqual(calls[0].execution, { mode: "isolated-agent-runtime", authProfileId: profile, timeoutMs: 40000 });
  assert.equal(calls[0].agentId, policy.agent);
  assert.equal(calls[0].systemPrompt, policy.prompt);
  assert.deepEqual(calls[0].messages, [{ role: "user", content: JSON.stringify("brief") }]);
  await assert.rejects(completeOwnerTurn(api, "openai:otro", policy)("brief", signal), /PREFLIGHT_OAUTH_TYPE/u);
  assert.equal(calls.length, 1);
  const services = [];
  createIsolatedPlugin({ ...policy, name: "Mateo", path: "/v1/mateo/strategy", socketName: "mateo.sock" })
    .register({ registerService: (service) => services.push(service) });
  assert.equal(services[0].id, policy.id);
});
