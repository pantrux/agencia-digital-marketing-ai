//#region Flatted reference resolution

/*
 * Adapted from Flatted (ISC License).
 * Copyright (c) 2018-2020, Andrea Giammarchi, @WebReflection
 *
 * Permission to use, copy, modify, and/or distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
 * REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
 * AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
 * INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
 * LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
 * OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
 * PERFORMANCE OF THIS SOFTWARE.
 */

const Primitive = String;

function resolveFlatted(input: unknown[]): unknown {
	const root = input[0];
	if (typeof root !== 'object' || root === null) return root;

	const pending = [root];
	const visited = new Set(pending);
	for (const current of pending) {
		const output = current as Record<string, unknown>;
		for (const key of Object.keys(output)) {
			const value = output[key];
			if (!(value instanceof Primitive)) continue;

			// Flatted's serializer emits canonical decimal indices. Never read inherited keys.
			const reference = value.valueOf();
			const index = Number(reference);
			const target =
				Number.isSafeInteger(index) &&
				index >= 0 &&
				index < input.length &&
				String(index) === reference &&
				Object.hasOwn(input, index)
					? input[index]
					: undefined;
			output[key] = target;
			if (typeof target === 'object' && target !== null && !visited.has(target)) {
				visited.add(target);
				pending.push(target);
			}
		}
	}
	return root;
}

//#endregion Flatted reference resolution

/**
 * Box strings inside records to distinguish references from top-level string values.
 */
function prepareFlatted(rawParsed: unknown[]): unknown[] {
	const pending: object[] = [rawParsed];
	while (pending.length > 0) {
		const output = pending.pop() as Record<string, unknown>;
		for (const key of Object.keys(output)) {
			const value = output[key];
			if (typeof value === 'string') output[key] = new Primitive(value);
			else if (typeof value === 'object' && value !== null) pending.push(value);
		}
	}
	return rawParsed.map((value) => (value instanceof Primitive ? value.valueOf() : value));
}

