import {spawn} from "node:child_process";
import {readFileSync, realpathSync, statSync} from "node:fs";
import {isAbsolute, join} from "node:path";

const maxArguments = 32768;
const maxResult = 4 * 1024 * 1024;

function invoke(root, row, parameters, signal) {
  return new Promise((resolve, reject) => {
    const input = Buffer.from(JSON.stringify(parameters), "utf8");
    if (input.length > maxArguments) {
      reject(new Error("MCP_ARGUMENTOS_INVALIDOS"));
      return;
    }
    const child = spawn(
      join(root, "venv", "bin", "python"),
      ["-E", "-B", join(root, "mcp_servers", "call_stdio.py"), row.server, row.tool],
      {
        cwd: root,
        env: {PATH: "/usr/bin:/bin", LANG: "C.UTF-8"},
        stdio: ["pipe", "pipe", "ignore"],
        signal,
        timeout: 1250000,
        windowsHide: true,
      },
    );
    const chunks = [];
    let count = 0;
    const fail = (code = "MCP_OPERACION_NO_DISPONIBLE") => reject(new Error(code));
    child.once("error", () => fail());
    child.stdin.on("error", () => fail());
    child.stdout.on("data", (chunk) => {
      count += chunk.length;
      if (count > maxResult) {
        child.kill();
        fail("MCP_RESPUESTA_INVALIDA");
        return;
      }
      chunks.push(chunk);
    });
    child.once("close", (code) => {
      if (code !== 0 || count > maxResult) {
        fail();
        return;
      }
      let output;
      try {
        output = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      } catch {
        fail("MCP_RESPUESTA_INVALIDA");
        return;
      }
      if (typeof output?.error === "string" && /^[A-Z][A-Z0-9_]{2,80}$/.test(output.error)) {
        fail(output.error);
        return;
      }
      if (!output?.result || typeof output.result !== "object" || Array.isArray(output.result)) {
        fail("MCP_RESPUESTA_INVALIDA");
        return;
      }
      resolve(output);
    });
    child.stdin.end(input);
  });
}

export default {
  id: "agency-audiovisual-mcp",
  name: "MCP audiovisual de la agencia",
  description: "Proyecta herramientas MCP stdio según el rol del agente.",
  register(api) {
    const root = api.pluginConfig?.runtimeRoot;
    if (
      typeof root !== "string" ||
      !isAbsolute(root) ||
      realpathSync(root) !== root ||
      (statSync(root).mode & 0o077) !== 0
    ) {
      throw new Error("MCP_RAIZ_INVALIDA");
    }
    const catalog = JSON.parse(readFileSync(new URL("./catalog.json", import.meta.url), "utf8"));
    for (const row of catalog.tools) {
      api.registerTool(
        {
          contextVersion: 2,
          create(context) {
            if (!row.agents.includes(context.agentId)) return null;
            return {
              name: row.name,
              label: row.tool,
              description: row.description,
              parameters: row.schema,
              async execute(_callId, parameters, signal) {
                signal?.throwIfAborted();
                context.assertInvocationCurrent();
                const {result: details, image} = await invoke(root, row, parameters, signal);
                const content = [{type: "text", text: JSON.stringify(details)}];
                if (
                  row.tool === "inspeccionar_medio" &&
                  image?.mimeType === "image/jpeg" &&
                  typeof image.data === "string"
                ) {
                  content.push({type: "image", mimeType: image.mimeType, data: image.data});
                }
                return {content, details};
              },
            };
          },
        },
        {name: row.name, optional: true, workspaceAccess: false},
      );
    }
  },
};
