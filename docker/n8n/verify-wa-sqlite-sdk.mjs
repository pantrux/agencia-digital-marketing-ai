import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const root = "/work/wa-sqlite";
const version = "3.54.1";
const sourceId =
  "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76";
const variants = ["wa-sqlite", "wa-sqlite-async", "wa-sqlite-jspi"];
const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const fileUrl = (name) => pathToFileURL(`${root}/${name}`).href;

// Los módulos upstream se compilan para web/worker. La prueba inyecta sólo los
// bytes locales del WASM y falla ante cualquier intento de descarga.
globalThis.window = { location: new URL("https://wa-sqlite.invalid/") };
globalThis.fetch = () => {
  throw new Error("WA_SQLITE_FETCH_PROHIBIDO");
};
assert.equal(typeof WebAssembly.Suspending, "function");
assert.equal(typeof WebAssembly.promising, "function");

const {
  Factory,
  SQLITE_DONE,
  SQLITE_OK,
  SQLITE_OPEN_READWRITE,
  SQLITE_OPEN_CREATE,
} = await import(fileUrl("src/sqlite-api.js"));
const { MemoryVFS } = await import(fileUrl("src/examples/MemoryVFS.js"));
const { MemoryAsyncVFS } = await import(fileUrl("src/examples/MemoryAsyncVFS.js"));

const results = {};
for (const variant of variants) {
  const wasmLeaf = `${variant}.wasm`;
  const jsLeaf = `${variant}.mjs`;
  const wasm = await readFile(`${root}/dist/${wasmLeaf}`);
  const js = await readFile(`${root}/dist/${jsLeaf}`);
  assert(wasm.length > 8 && wasm.length <= 4 * 1024 ** 2);
  assert(js.length > 0 && js.length <= 4 * 1024 ** 2);
  const source = new TextDecoder("utf-8", { fatal: true }).decode(js);
  assert.match(source, /\bexport\s+default\b/);
  assert.deepEqual(
    variants.map((name) => `${name}.wasm`).filter((name) => source.includes(name)),
    [wasmLeaf],
  );
  if (variant.endsWith("-jspi")) {
    assert(source.includes("WebAssembly.Suspending") && source.includes("WebAssembly.promising"));
  }

  const wasmModule = new WebAssembly.Module(wasm);
  const imports = WebAssembly.Module.imports(wasmModule);
  const exports = WebAssembly.Module.exports(wasmModule);
  assert(imports.length > 0 && imports.length < 1024);
  assert(imports.every(({ kind, module, name }) => kind === "function" && module && name));
  assert(exports.some(({ kind }) => kind === "memory"));
  assert(exports.some(({ kind }) => kind === "function"));

  const { default: instantiate } = await import(fileUrl(`dist/${jsLeaf}`));
  assert.equal(typeof instantiate, "function");
  const module = await instantiate({ wasmBinary: wasm });
  for (const name of ["cwrap", "ccall", "getTempRet0", "_sqlite3_malloc", "_sqlite3_free"]) {
    assert.equal(typeof module[name], "function", `ABI ${name}`);
  }
  const sqlite = Factory(module);
  const asynchronous = variant !== "wa-sqlite";
  let asyncOpens = 0;
  class ObservedAsyncVFS extends MemoryAsyncVFS {
    async jOpen(...args) {
      await Promise.resolve();
      asyncOpens++;
      return super.jOpen(...args);
    }
  }
  const vfs = asynchronous
    ? new ObservedAsyncVFS("acceptance", module)
    : new MemoryVFS("acceptance", module);
  await vfs.isReady();
  assert.equal(sqlite.vfs_register(vfs, false), SQLITE_OK);
  const db = await sqlite.open_v2(
    "probe.db",
    SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE,
    "acceptance",
  );
  const rows = async (sql) => {
    const values = [];
    assert.equal(await sqlite.exec(db, sql, (row) => values.push(row)), SQLITE_OK);
    return values;
  };
  try {
    assert.deepEqual(await rows("SELECT sqlite_version(), sqlite_source_id()"), [[version, sourceId]]);
    const options = (await rows("PRAGMA compile_options")).flat();
    assert(options.includes("OMIT_LOAD_EXTENSION"));
    assert(!options.some((value) => value.startsWith("ENABLE_FTS5")));
    await rows("CREATE TABLE probe(id INTEGER PRIMARY KEY, label TEXT)");
    let statements = 0;
    for await (const statement of sqlite.statements(db, "INSERT INTO probe VALUES (?, ?)")) {
      assert.equal(sqlite.bind_collection(statement, [2147483648, "sqlite-✓"]), SQLITE_OK);
      assert.equal(await sqlite.step(statement), SQLITE_DONE);
      statements++;
    }
    assert.equal(statements, 1);
    assert.deepEqual(await rows("SELECT id, label FROM probe"), [[2147483648, "sqlite-✓"]]);
    assert(asynchronous ? asyncOpens > 0 : asyncOpens === 0);
  } finally {
    await sqlite.close(db);
    await vfs.close();
  }
  results[variant] = {
    imports: imports.length,
    exports: exports.length,
    import_abi_sha256: sha256(JSON.stringify(imports)),
    export_abi_sha256: sha256(JSON.stringify(exports)),
    js_sha256: sha256(js),
    wasm_sha256: sha256(wasm),
    async_vfs_opens: asyncOpens,
  };
}

await writeFile(
  "/work/.sqlite-functional.json",
  JSON.stringify({
    schema: 1,
    node: process.version,
    version,
    source_id: sourceId,
    variants: results,
    browser_verified: false,
    opfs_verified: false,
  }) + "\n",
  { flag: "wx", mode: 0o600 },
);
