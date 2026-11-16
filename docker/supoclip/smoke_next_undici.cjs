const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

async function checkFetch({fetch, Request, Response, Headers, WebSocket}, origin) {
    assert.equal(typeof WebSocket, 'function');
    const headers = new Headers({'x-agency-probe': 'loopback'});
    headers.append('x-agency-probe', 'stream');
    const request = new Request(origin, {
        method: 'POST', headers, body: new ReadableStream({
            start(controller) {
                controller.enqueue(new TextEncoder().encode('sintético'));
                controller.close();
            },
        }),
        signal: AbortSignal.timeout(3_500),
    });
    const response = await fetch(request);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-type'), 'text/plain');
    assert.equal(await response.text(), 'sintético');
    assert.equal(Response.error().type, 'error');
    assert.equal(Response.error().status, 0);
}

async function bundled(nextRoot) {
    const root = path.resolve(nextRoot, 'dist', 'compiled');
    const {load} = require(path.join(root, '@edge-runtime', 'primitives', 'load.js'));
    const {EdgeRuntime} = require(path.join(root, 'edge-runtime', 'index.js'));
    const server = http.createServer(async (request, response) => {
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const accepted = request.method === 'POST' && request.url === '/'
            && request.headers['x-agency-probe'] === 'loopback, stream';
        response.writeHead(accepted ? 200 : 400, {'Content-Type': 'text/plain'});
        response.end(Buffer.concat(chunks));
    });
    await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', resolve);
    });
    try {
        const origin = `http://127.0.0.1:${server.address().port}/`;
        await checkFetch(load(), origin);
        const edge = new EdgeRuntime();
        await checkFetch(edge.context, origin);
        edge.evaluate(`addEventListener('fetch', event => {
            event.respondWith(new Response('edge', {headers: {'x-probe': 'edge'}}));
        });`);
        const dispatched = await edge.dispatchFetch(origin);
        assert.equal(await dispatched.text(), 'edge');
        assert.equal(dispatched.headers.get('x-probe'), 'edge');
        return {primitives_fetch: 200, edge_fetch: 200, edge_dispatch: 200};
    } finally {
        await new Promise((resolve) => {
            server.close(resolve);
            server.closeAllConnections();
        });
    }
}

async function application(portText, standaloneRoot, publicOrigin) {
    assert.match(portText, /^[1-9][0-9]{0,4}$/);
    const port = Number(portText);
    assert.ok(port <= 65_535);
    const get = (url, options) => fetch(`http://127.0.0.1:${port}${url}`, {
        signal: AbortSignal.timeout(3_500), redirect: 'error', ...options,
    });
    const sitemap = await get('/sitemap.xml');
    assert.equal(sitemap.status, 200);
    assert.match(sitemap.headers.get('content-type'), /xml/);
    const body = await sitemap.text();
    assert.ok(body.includes('<urlset') && body.includes(`${publicOrigin}/changelog`));

    const chunks = path.resolve(standaloneRoot, '.next', 'static', 'chunks');
    const leaf = fs.readdirSync(chunks).sort().find((name) => /^[a-zA-Z0-9_-]+\.js$/.test(name));
    assert.ok(leaf);
    const asset = await get(`/_next/static/chunks/${leaf}`);
    assert.equal(asset.status, 200);
    const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
    assert.equal(hash(Buffer.from(await asset.arrayBuffer())), hash(fs.readFileSync(path.join(chunks, leaf))));

    const unauthorized = await get('/api/upload/authorization', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    assert.equal(unauthorized.status, 401);
    assert.deepEqual(await unauthorized.json(), {error: 'Unauthorized'});
    return {sitemap: 200, static_asset: 200, anonymous_upload: 401};
}

const [mode, ...args] = process.argv.slice(2);
(async () => {
    if (typeof process.getuid === 'function') assert.equal(process.getuid(), 1000);
    if (mode === 'bundles' && args.length === 1) return bundled(...args);
    if (mode === 'application' && args.length === 3) return application(...args);
    throw new Error('Uso: bundles NEXT_ROOT | application PORT STANDALONE_ROOT PUBLIC_ORIGIN');
})().then((result) => console.log(JSON.stringify(result))).catch((error) => {
    console.error(`${error.name}: ${error.message}`);
    process.exitCode = 1;
});
