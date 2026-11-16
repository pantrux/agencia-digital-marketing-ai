#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

cd /immich-node
sha256sum -c INPUTS.sha256
printf '%s  %s\n' \
    'e32e2d16081cf644f88392934288086e7662529d0d43277c34f7aad2207cfafb' /usr/local/bin/node \
    | sha256sum -c -
test "$(node --version)" = v24.22.1
test "$(node -p 'process.versions.modules + ":" + process.versions.napi')" = 137:10
test "$(npm --version)" = 11.19.1
test "$(readlink /usr/local/bin/npm)" = ../lib/node_modules/npm/bin/npm-cli.js
test "$(readlink /usr/local/bin/npx)" = ../lib/node_modules/npm/bin/npx-cli.js

rm -rf -- /usr/local/include/node
tar -xzf node-runtime.tar.gz -C /usr/local --no-same-owner --no-same-permissions
printf '%s  %s\n' \
    'a7ec1b37e6ff9d187e4198c5b6df9a4eb1c74dd31804fb3bac55cc4ceca1ac01' /usr/local/bin/node \
    | sha256sum -c -
rm -rf -- /usr/local/lib/node_modules/npm
install -d -m 0755 /usr/local/lib/node_modules/npm
tar -xzf npm-11.21.2.tgz --strip-components=1 -C /usr/local/lib/node_modules/npm \
    --no-same-owner --no-same-permissions

test "$(node --version)" = v24.22.3
test "$(node -p 'process.versions.modules + ":" + process.versions.napi')" = 137:10
test "$(npm --version)" = 11.21.2
test "$(npx --version)" = 11.21.2
test "$(corepack --version)" = 0.36.0
test "$(node -p 'require("/usr/local/lib/node_modules/npm/node_modules/undici/package.json").version')" = 6.29.1
test "$(node -p 'require("/usr/local/lib/node_modules/npm/node_modules/brace-expansion/package.json").version')" = 5.0.12
linked=$(ldd /usr/local/bin/node)
[[ "$linked" == *libc.so.6* && "$linked" != *'not found'* ]]

cd /usr/src/app/server
node --input-type=module <<'JS'
import assert from 'node:assert/strict';
import sharp from 'sharp';
import bcrypt from 'bcrypt';
import '@immich/plugin-sdk';
assert.equal(sharp.versions.vips, '8.18.7');
const input = { create: { width: 2, height: 2, channels: 3, background: 'blue' } };
const png = await sharp(input).png().toBuffer();
assert.equal((await sharp(png).metadata()).width, 2);
assert.ok(bcrypt.compareSync('immich-fixture', bcrypt.hashSync('immich-fixture', 4)));
JS
node --check dist/main.js
