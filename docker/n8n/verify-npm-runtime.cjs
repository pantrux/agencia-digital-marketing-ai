const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');

const npmRoot = resolve(process.argv[2] || '/usr/local/lib/node_modules/npm');
const npmCli = join(npmRoot, 'bin/npm-cli.js');
const npxCli = join(npmRoot, 'bin/npx-cli.js');
const metadata = (path) => JSON.parse(readFileSync(join(path, 'package.json'), 'utf8'));

assert.equal(metadata(npmRoot).version, '11.21.2');
assert.equal(metadata(join(npmRoot, 'node_modules/brace-expansion')).version, '5.0.12');
assert.equal(metadata(join(npmRoot, 'node_modules/undici')).version, '6.29.1');
if (!process.argv[2]) {
  assert.equal(realpathSync('/usr/local/bin/npm'), npmCli);
  assert.equal(realpathSync('/usr/local/bin/npx'), npxCli);
}

const trial = mkdtempSync(join(tmpdir(), 'n8n-npm-'));
const source = join(trial, 'source');
const consumer = join(trial, 'consumer');
const env = {
  PATH: process.env.PATH,
  HOME: trial,
  npm_config_cache: join(trial, 'cache'),
  npm_config_registry: 'http://127.0.0.1:9',
  npm_config_userconfig: join(trial, 'npmrc'),
  npm_config_globalconfig: join(trial, 'global-npmrc'),
  npm_config_update_notifier: 'false',
};
const run = (cli, args, cwd) => execFileSync(process.execPath, [cli, ...args], {
  cwd, env, encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'pipe'],
}).trim();

try {
  assert.equal(run(npmCli, ['--version'], trial), '11.21.2');
  assert.equal(run(npxCli, ['--version'], trial), '11.21.2');
  mkdirSync(source);
  mkdirSync(consumer);
  writeFileSync(join(source, 'package.json'), JSON.stringify({
    name: 'n8n-npm-fixture', version: '1.0.0', main: 'index.js',
  }));
  writeFileSync(join(source, 'index.js'), 'module.exports = 42;\n');
  writeFileSync(join(consumer, 'package.json'), '{"name":"n8n-npm-consumer","private":true}\n');
  const archive = run(npmCli, ['pack', '--offline', '--ignore-scripts', '--silent'], source);
  assert.equal(archive, 'n8n-npm-fixture-1.0.0.tgz');
  run(npmCli, [
    'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
    '--no-package-lock', '--no-save', join(source, archive),
  ], consumer);
  assert.equal(require(join(consumer, 'node_modules/n8n-npm-fixture')), 42);
  console.log(JSON.stringify({ success: true, npm: '11.21.2', pack_install_offline: true }));
} finally {
  rmSync(trial, { force: true, recursive: true });
}
