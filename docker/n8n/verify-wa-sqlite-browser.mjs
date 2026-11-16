import { wrap } from '/deps/comlink.mjs';

const sourceId = '2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76';
const expectedWasm = 'e25dc2f6014e39bb16eb8f782e8553691359bda567d149da22fe3e5409e42afa';
const row = [2147483648, 'OPFS ✓'];

function require(ok, message) {
  if (!ok) throw new Error(message);
}

function same(actual, expected, message) {
  require(JSON.stringify(actual) === JSON.stringify(expected), message);
}

async function sqliteWorker(test) {
  const worker = new Worker('/worker/worker.js', { type: 'module' });
  const api = wrap(worker);
  let timer;
  try {
    return await Promise.race([
      test(api),
      new Promise((_, reject) => {
        worker.addEventListener('error', (event) => reject(new Error(event.message || 'worker import failed')), { once: true });
        timer = setTimeout(() => reject(new Error('worker timed out')), 15000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
    worker.terminate();
  }
}

async function test() {
  require(isSecureContext && location.hostname === '127.0.0.1' && !crossOriginIsolated, 'loopback origin');
  require(typeof navigator.storage?.getDirectory === 'function', 'OPFS unavailable');
  const bytes = await fetch('/package/dist/wa-sqlite.wasm').then((response) => response.arrayBuffer());
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (value) => value.toString(16).padStart(2, '0')).join('');
  require(hash === expectedWasm, 'WASM digest mismatch');
  const opfs = await navigator.storage.getDirectory();
  const options = await sqliteWorker(async (api) => {
    await api.initialize({ version: '2.46.6' });
    require(await api.isInitialized(), 'first worker not initialized');
    try {
      same((await api.query('SELECT sqlite_version(), sqlite_source_id()')).rows, [['3.54.1', sourceId]], 'SQLite source mismatch');
      const flags = (await api.query('PRAGMA compile_options')).rows.flat();
      require(flags.includes('OMIT_LOAD_EXTENSION') && !flags.some((flag) => flag.startsWith('ENABLE_FTS5')), 'SQLite compile options');
      await api.exec('CREATE TABLE browser_probe (id INTEGER PRIMARY KEY, label TEXT NOT NULL)');
      await api.queryWithParams('INSERT INTO browser_probe VALUES (?, ?)', row);
      same((await api.queryWithParams('SELECT id, label FROM browser_probe WHERE id = ?', [row[0]])).rows, [row], 'first worker row mismatch');
      return flags;
    } finally {
      await api.close();
      require(!await api.isInitialized(), 'first worker remained initialized');
    }
  });
  const directory = await opfs.getDirectoryHandle('n8n-opfs');
  let fileCount = 0;
  for await (const entry of directory.values()) {
    if (entry.kind === 'file' && (await entry.getFile()).size > 4096) fileCount++;
  }
  require(fileCount > 0, 'no persistent OPFS data file');
  await sqliteWorker(async (api) => {
    await api.initialize({ version: '2.46.6' });
    try {
      same((await api.queryWithParams('SELECT id, label FROM browser_probe WHERE id = ?', [row[0]])).rows, [row], 'persisted row mismatch');
    } finally {
      await api.close();
    }
  });
  return { success: true, browser_verified: true, opfs_verified: true, wasm_sha256: hash, sqlite: '3.54.1', source_id: sourceId, separate_workers: 2, reopened_row: row, data_files: fileCount, compile_options: options, editor_bundle_tested: false };
}

let result;
try {
  result = await test();
} catch (error) {
  result = { success: false, error: error.message, stack: error.stack };
}
await fetch('/result', { method: 'POST', body: JSON.stringify(result) });
