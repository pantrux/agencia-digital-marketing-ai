"""Instrumenta el trabajador facial fijado para un ensayo con datos sintéticos."""

import argparse
import hashlib
from pathlib import Path

SOURCE_SHA256 = "bcf27c993174fcb7e7edb505ee30a529f370307dedcb1f1d4323fd4558ab33ef"
START = b"    async handleRecognizeFaces({ id, deferred }) {\n"
END = b"    async handlePersonMigration("


def derive(raw, core_without_fallback=False, core_fallback_distance=None):
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError("La fuente del trabajador facial no coincide con la imagen fijada")
    head, worker = raw.split(START, 1)
    worker, tail = worker.split(END, 1)
    additions = {
        b"        const { machineLearning }": (
            b'        const trace = (event, fields = {}) => this.logger.log("AGENCIA_FACE_TRACE " + '
            b"JSON.stringify({ event, id, deferred: !!deferred, ...fields }));\n"
            b'        trace("inicio");\n'
        ),
        b"        if (machineLearning.facialRecognition.minFaces > 1": (
            b'        trace("vecinos", { matches: matches.map(({ id, personGroupId, distance }) => '
            b"({ id, personGroupId, distance })) });\n"
        ),
        b"            personGroupId = matchWithPerson?.personGroupId ?? undefined;": (
            b'            trace("vecino_asignado", { match: matchWithPerson ? '
            b"{ id: matchWithPerson.id, personGroupId: matchWithPerson.personGroupId, "
            b"distance: matchWithPerson.distance } : null });\n"
        ),
        b"            const group = await this.personRepository.createGroup(ownerId);": (
            b'            trace("crear_grupo");\n'
        ),
    }
    for anchor, added in additions.items():
        if worker.count(anchor) != 1:
            raise ValueError("La instrucción de diagnóstico no tiene un punto de inserción único")
        worker = worker.replace(anchor, added + anchor, 1)
    anchor = b"            await this.personRepository.reassignFaces({ faceIds: [id], newPersonGroupId: personGroupId });\n"
    if worker.count(anchor) != 1:
        raise ValueError("No se identificó la asignación del rostro")
    worker = worker.replace(
        anchor, anchor + b'            trace("asignado", { personGroupId });\n', 1
    )
    if core_without_fallback:
        condition = b"        if (!personGroupId) {\n            const [matchWithPerson]"
        if worker.count(condition) != 1:
            raise ValueError("No se identificó la búsqueda de rostros ya agrupados")
        worker = worker.replace(
            condition,
            b"        if (!personGroupId && !isCore) {\n            const [matchWithPerson]",
            1,
        )
    if core_fallback_distance is not None:
        if core_fallback_distance != 0.30 or core_without_fallback:
            raise ValueError("El ensayo sólo admite el límite fijado de 0.30 para esa búsqueda")
        option = (
            b"                maxDistance: machineLearning.facialRecognition.maxDistance,\n"
            b"                numResults: 1,\n"
            b"                hasPerson: true,"
        )
        if worker.count(option) != 1:
            raise ValueError("No se identificó el radio de la segunda búsqueda")
        worker = worker.replace(
            option,
            option.replace(
                b"machineLearning.facialRecognition.maxDistance,",
                b"isCore ? Math.min(0.30, machineLearning.facialRecognition.maxDistance)"
                b" : machineLearning.facialRecognition.maxDistance,",
            ),
            1,
        )
    return head + START + worker + END + tail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--core-without-fallback", action="store_true")
    options.add_argument("--core-fallback-distance", type=float, choices=(0.30,))
    args = parser.parse_args()
    output = derive(
        args.source.read_bytes(), args.core_without_fallback, args.core_fallback_distance
    )
    with args.output.open("xb") as handle:
        handle.write(output)
    print(hashlib.sha256(output).hexdigest())


if __name__ == "__main__":
    main()
