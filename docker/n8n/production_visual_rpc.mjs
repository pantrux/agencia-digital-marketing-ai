import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

try {
  const input = readFileSync(0);
  if (input.length > 110000) throw new Error("Solicitud visual demasiado grande");

  const request = JSON.parse(input);
  const { agentId, message, idempotencyKey, attachments } = request;
  if (
    !["agency-director", "agency-critic"].includes(agentId) ||
    typeof message !== "string" || !message.trim() ||
    typeof idempotencyKey !== "string" ||
    !Array.isArray(attachments) || attachments.length < 1 || attachments.length > 3
  ) throw new Error("Solicitud visual inválida");

  const sdk = join(homedir(), ".npm-global/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js");
  const { callGatewayFromCli } = await import(pathToFileURL(sdk).href);
  const result = await callGatewayFromCli("agent", {
    json: true,
    expectUrl: "ws://127.0.0.1:18789",
    expectFinal: true,
    timeout: "65000",
  }, {
    agentId,
    message,
    idempotencyKey,
    attachments,
    deliver: false,
    modelRun: true,
    promptMode: "none",
    cleanupBundleMcpOnRunEnd: true,
    timeout: 55,
  });
  process.stdout.write(JSON.stringify(result));
} catch {
  process.stderr.write("Falló la solicitud visual al Gateway local\n");
  process.exitCode = 1;
}
