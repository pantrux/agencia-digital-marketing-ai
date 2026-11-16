import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire, stripTypeScriptTypes } from 'node:module';
import { resolve } from 'node:path';
import { Readable } from 'node:stream';
import { test } from 'node:test';

const source = readFileSync(process.argv[2], 'utf8');
const begin = source.indexOf('//#region Flatted reference resolution');
const end = source.indexOf('/**\n * Parse a flatted JSON string asynchronously');
assert.ok(begin >= 0 && end > begin, 'No se encontró el bloque del deserializador');
const decode = new Function(
	stripTypeScriptTypes(source.slice(begin, end)) +
		'\nreturn (input) => resolveFlatted(prepareFlatted(input));',
)();

test('las referencias no numéricas no devuelven propiedades heredadas', () => {
	const parsed = decode([{ prototype: '__proto__', method: 'map', length: 'length' }]);
	assert.deepEqual(parsed, { prototype: undefined, method: undefined, length: undefined });
	assert.equal(Object.getPrototypeOf(parsed), Object.prototype);
});

test('los índices no canónicos, fuera de rango o sin entrada propia se rechazan', () => {
	const input = [{ leadingZero: '01', exponent: '1e0', signed: '+1', absent: '2', beyond: '3' }];
	input.push('present');
	input.length = 3;
	assert.deepEqual(decode(input), {
		leadingZero: undefined,
		exponent: undefined,
		signed: undefined,
		absent: undefined,
		beyond: undefined,
	});
});

test('una cadena profunda de referencias se resuelve sin recursión', () => {
	const count = 16_384;
	const input = Array.from({ length: count }, (_, i) => ({ next: String((i + 1) % count) }));
	const root = decode(input);
	let item = root;
	for (let i = 0; i < count; i++) item = item.next;
	assert.equal(item, root);
});

test('la preparación de objetos anidados tampoco depende de la pila', () => {
	const depth = 16_384;
	let nested = { leaf: '1' };
	for (let i = 0; i < depth; i++) nested = [nested];
	let prepared = decode([nested, 'texto']);
	for (let i = 0; i < depth; i++) prepared = prepared[0];
	assert.equal(prepared.leaf.valueOf(), '1');
});

test('conserva texto, arrays, primitivas, referencias compartidas y ciclos válidos', () => {
	const root = decode([
		{ self: '0', list: '1', same: '1', text: '2', bool: false, nil: null },
		['2', '0', 12],
		'ñ ☕',
	]);
	assert.equal(root.self, root);
	assert.equal(root.list, root.same);
	assert.equal(root.list[1], root);
	assert.deepEqual([root.text, root.bool, root.nil, root.list[0], root.list[2]], [
		'ñ ☕', false, null, 'ñ ☕', 12,
	]);
	assert.equal(decode(['texto']), 'texto');
	assert.equal(decode([null]), null);
});

if (process.argv[3]) {
	const require = createRequire(resolve(process.argv[3], 'probe.cjs'));
	const imports = [
		"import { ensureError } from '@n8n/utils/errors/ensure-error';",
		"import { Readable } from 'stream';",
		"import { parser } from 'stream-json';",
		"import Asm from 'stream-json/Assembler';",
	];
	let body = stripTypeScriptTypes(source);
	for (const statement of imports) {
		assert.equal(body.split(statement).length, 2);
		body = body.replace(statement, '');
	}
	const parse = new Function(
		'Readable',
		'parser',
		'Asm',
		'ensureError',
		body.replace('export async function parseFlattedAsync', 'async function parseFlattedAsync') +
			'\nreturn parseFlattedAsync;',
	)(Readable, require('stream-json').parser, require('stream-json/Assembler'), (error) => error);

	test('el parser de flujo mantiene ciclos y texto UTF-8 a través de varios bloques', async () => {
		const payload = 'mañana ☕'.repeat(140_000);
		const result = await parse(JSON.stringify([{ self: '0', text: '1', invalid: 'constructor' }, payload]));
		assert.equal(result.self, result);
		assert.ok(result.text === payload, 'El texto cambió entre bloques');
		assert.equal(result.invalid, undefined);
	});

	test('el parser de flujo propaga errores de sintaxis', async () => {
		await assert.rejects(parse('[{'));
	});
}
