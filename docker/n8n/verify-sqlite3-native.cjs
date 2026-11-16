const assert = require('node:assert/strict');

const sqlite = require(process.argv[2]);
const version = '3.54.1';
const source = '2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76';

async function verify() {
  const db = await new Promise((resolve, reject) => {
    const connection = new sqlite.Database(':memory:', sqlite.OPEN_READWRITE | sqlite.OPEN_CREATE, (error) => {
      if (error) reject(error);
      else resolve(connection);
    });
  });
  const exec = (sql) => new Promise((resolve, reject) => {
    db.exec(sql, (error) => error ? reject(error) : resolve());
  });
  const all = (sql, values = []) => new Promise((resolve, reject) => {
    db.all(sql, values, (error, rows) => error ? reject(error) : resolve(rows));
  });
  try {
    assert.deepEqual(await all('SELECT sqlite_version() AS version, sqlite_source_id() AS source'), [{ version, source }]);
    const options = (await all('PRAGMA compile_options')).map((row) => row.compile_options);
    for (const option of ['ENABLE_FTS3', 'ENABLE_FTS4', 'ENABLE_FTS5', 'ENABLE_RTREE', 'ENABLE_DBSTAT_VTAB', 'ENABLE_MATH_FUNCTIONS', 'THREADSAFE=1']) {
      assert(options.includes(option), option);
    }
    await exec("CREATE VIRTUAL TABLE probe USING fts5(label); INSERT INTO probe VALUES ('hola');");
    assert.deepEqual(await all('SELECT label, sqrt(49) AS valor FROM probe WHERE probe MATCH ?', ['hola']), [{ label: 'hola', valor: 7 }]);
    assert.equal(typeof db.loadExtension, 'function');
    await assert.rejects(all('SELECT load_extension(?)', ['ext-prueba-inexistente']), /not authorized/);
  } finally {
    await new Promise((resolve, reject) => {
      db.close((error) => error ? reject(error) : resolve());
    });
  }
  console.log(JSON.stringify({ success: true, sqlite: version, source, napi: process.versions.napi, fts5: true }));
}

verify().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
