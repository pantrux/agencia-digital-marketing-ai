import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Application, type Backend} from '../src/server.ts';

const token = 'a'.repeat(40);
const request = (route: string, body = '') => ({method: route.startsWith('/health') ? 'GET' : 'POST', path: route, remoteAddress: '192.168.0.85', authorization: `Bearer ${token}`, body: Buffer.from(body)});
const data = {project_id: 'sansevieras', job_id: 'prueba', composition: 'AgencyVertical', plan: 'Free', props: {title: 'Prueba', duration_seconds: 3, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'}};
function backend(): Backend {return {health: () => ({service: 'remotion', status: 'degraded', ready: false, version: '4.0.541', nvenc_available: false}), render: async () => ({ok: true}), stills: async () => ({ok: true}), inspect: async () => ({ok: true})};}
test('todas las rutas exigen token y socket permitido; salud no inventa disponibilidad', async () => {
  const app = new Application(token, backend());
  assert.equal((await app.handle({...request('/health'), authorization: undefined})).status, 401);
  assert.equal((await app.handle({...request('/health'), remoteAddress: '192.168.0.100'})).status, 403);
  assert.equal((await app.handle(request('/healthz'))).status, 503);
  assert.equal((await app.handle(request('/render', '{'))).status, 422);
  assert.equal((await app.handle(request('/render', 'x'.repeat(65537)))).status, 413);
});
test('tercer render se rechaza mientras dos operaciones siguen activas', async () => {
  let release!: () => void; const blocked = new Promise<void>((resolve) => {release = resolve;});
  const fake = backend(); fake.render = async () => {await blocked; return {ok: true};};
  const app = new Application(token, fake);
  const first = app.handle(request('/render', JSON.stringify(data))), second = app.handle(request('/render', JSON.stringify(data)));
  assert.equal((await app.handle(request('/render', JSON.stringify(data)))).status, 429);
  release(); assert.deepEqual((await Promise.all([first, second])).map((result) => result.status), [200, 200]);
  assert.equal((await app.handle(request('/render', JSON.stringify(data)))).status, 200);
});

test('los dos slots son globales para MP4, ProRes y capturas, sin crear cupos por plan o formato', async () => {
  let release!: () => void; const blocked = new Promise<void>((resolve) => {release = resolve;});
  const fake = backend();
  fake.render = fake.stills = async () => {await blocked; return {ok: true};};
  const app = new Application(token, fake);
  const prores = {...data, plan: 'Ilimitado', format: 'prores4444', resolution: '4k'};
  const first = app.handle(request('/render', JSON.stringify(data)));
  const second = app.handle(request('/render', JSON.stringify(prores)));
  assert.equal((await app.handle(request('/stills', JSON.stringify(prores)))).status, 429);
  release(); assert.deepEqual((await Promise.all([first, second])).map((result) => result.status), [200, 200]);
  assert.equal((await app.handle(request('/stills', JSON.stringify(prores)))).status, 200);
});
