import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {loadConfig, verifyBinary} from '../src/config.ts';

test('JSON privado prevalece y P: se normaliza sin directorio actual de unidad', async () => {
  const temp = await mkdtemp(path.join(tmpdir(), 'render-config-'));
  try {
    const file = path.join(temp, 'private.json');
    await writeFile(file, JSON.stringify({GPU_BRIDGE_BEARER_TOKEN: 'a'.repeat(40), GPU_FFMPEG_PATH: 'C:\\tools\\ffmpeg.exe', GPU_FFPROBE_PATH: 'C:\\tools\\ffprobe.exe', GPU_REMOTION_BROWSER_PATH: 'C:\\tools\\chrome.exe', GPU_MODEL_CACHE_ROOT: 'C:\\GPU-Bridge\\models', GPU_SMB_DRIVE: 'P:'}));
    const result = await loadConfig({GPU_RUNTIME_CONFIG_PATH: file, GPU_BRIDGE_BEARER_TOKEN: 'incorrecto'});
    assert.equal(result.token, 'a'.repeat(40)); assert.equal(result.nasRoot, 'P:\\');
  } finally {await rm(temp, {recursive: true, force: true});}
});
test('el hash del ejecutable se compara antes de poder ejecutarlo', async () => {
  const temp = await mkdtemp(path.join(tmpdir(), 'render-binary-'));
  try {
    const file = path.join(temp, 'tool.exe'); await writeFile(file, 'binario');
    await verifyBinary(file, createHash('sha256').update('binario').digest('hex'));
    await assert.rejects(verifyBinary(file, '0'.repeat(64)), /hash/);
  } finally {await rm(temp, {recursive: true, force: true});}
});

test('la raíz opcional de render se separa de modelos y el JSON privado prevalece', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-cache-config-'));
  const environment = {GPU_BRIDGE_BEARER_TOKEN:'a'.repeat(40),GPU_FFMPEG_PATH:'C:\\tools\\ffmpeg.exe',GPU_FFPROBE_PATH:'C:\\tools\\ffprobe.exe',GPU_REMOTION_BROWSER_PATH:'C:\\tools\\chrome.exe',GPU_MODEL_CACHE_ROOT:'C:\\GPU-Bridge\\models',GPU_SMB_DRIVE:'P:'};
  try {
    assert.equal((await loadConfig(environment)).cacheRoot, environment.GPU_MODEL_CACHE_ROOT);
    assert.equal((await loadConfig({...environment,GPU_RENDER_CACHE_ROOT:'D:\\cache\\render'})).cacheRoot,'D:\\cache\\render');
    const file = path.join(root,'private.json');
    await writeFile(file,JSON.stringify({...environment,GPU_RENDER_CACHE_ROOT:'D:\\privado\\render'}));
    assert.equal((await loadConfig({GPU_RUNTIME_CONFIG_PATH:file,GPU_RENDER_CACHE_ROOT:'D:\\otra\\raiz'})).cacheRoot,'D:\\privado\\render');
    for (const value of ['', 'relativo', '\\\\servidor\\recurso', 'D:\\cache\\..\\ajeno']) {
      await assert.rejects(loadConfig({...environment,GPU_RENDER_CACHE_ROOT:value}),/caché|ruta/i);
    }
  } finally {await rm(root,{recursive:true,force:true});}
});
