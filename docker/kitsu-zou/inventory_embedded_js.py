"""Catálogo JS incorporado: lectura de archivos/mapas, sin ejecutar sus fuentes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("embedded-js-inputs.json")
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON con claves duplicadas.")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique)


def name_in_archive(raw):
    if not raw or "\\" in raw or "\x00" in raw or ":" in raw or raw.startswith("/"):
        raise ValueError("Nombre de archivo no relativo y portable.")
    name = raw.removeprefix("./")
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("Nombre de archivo con escape o ambigüedad.")
    return name


def read_archive(raw, kind, maximum_total=128 * 1024**2):
    files = {}
    seen = set()
    total = 0

    def add(name, size, reader):
        nonlocal total
        canonical = name_in_archive(name)
        if canonical.casefold() in seen or size > 16 * 1024**2 or len(files) >= 5000:
            raise ValueError("Duplicado, colisión o miembro fuera de límite.")
        seen.add(canonical.casefold())
        total += size
        if total > maximum_total:
            raise ValueError("Archivo expandido fuera de límite.")
        value = reader.read(size + 1)
        if len(value) != size:
            raise ValueError("Miembro truncado o tamaño diferente.")
        files[canonical] = value

    if kind == "tar.gz":
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("TAR con enlace o tipo especial.")
                with archive.extractfile(member) as stream:
                    add(member.name, member.size, stream)
    elif kind == "zip":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in (0, stat.S_IFREG) or member.flag_bits & 1:
                    raise ValueError("ZIP cifrado, enlazado o especial.")
                with archive.open(member) as stream:
                    add(member.filename, member.file_size, stream)
    else:
        raise ValueError("Formato no admitido.")
    return files


def vlq(segment):
    numbers = []
    accumulator = shift = 0
    for character in segment:
        try:
            value = ALPHABET.index(character)
        except ValueError as error:
            raise ValueError("Carácter VLQ inválido.") from error
        accumulator |= (value & 31) << shift
        if value & 32:
            shift += 5
            if shift > 30:
                raise ValueError("VLQ fuera de límite.")
        else:
            number = accumulator >> 1
            # ECMA-426 reserva el negativo con magnitud cero para -2**31.
            numbers.append((-(2**31) if number == 0 else -number) if accumulator & 1 else number)
            accumulator = shift = 0
    if shift:
        raise ValueError("VLQ incompleto.")
    return numbers


def map_references(value):
    if value.get("version") != 3 or "sections" in value:
        raise ValueError("Sólo se admiten mapas v3 planos.")
    sources, contents = value.get("sources"), value.get("sourcesContent")
    names, mappings = value.get("names", []), value.get("mappings")
    if not isinstance(sources, list) or not sources or not all(isinstance(s, str) for s in sources):
        raise ValueError("Mapa sin fuentes textuales.")
    if contents is not None and (
        not isinstance(contents, list)
        or len(contents) != len(sources)
        or not all(c is None or isinstance(c, str) for c in contents)
    ):
        raise ValueError("sourcesContent incompleto o mal formado.")
    if (
        not isinstance(names, list)
        or not all(isinstance(n, str) for n in names)
        or not isinstance(mappings, str)
    ):
        raise ValueError("Nombres/mappings inválidos.")
    source = original_line = original_column = name_index = segments = 0
    referenced = set()
    for line in mappings.split(";"):
        generated_column = 0
        for segment in line.split(","):
            if not segment:
                continue
            fields = vlq(segment)
            segments += 1
            if segments > 2_000_000 or len(fields) not in (1, 4, 5):
                raise ValueError("Cantidad o campos de segmentos inválidos.")
            generated_column += fields[0]
            if generated_column < 0:
                raise ValueError("Columna generada negativa.")
            if len(fields) == 1:
                continue
            source += fields[1]
            original_line += fields[2]
            original_column += fields[3]
            if not 0 <= source < len(sources) or min(original_line, original_column) < 0:
                raise ValueError("Índice fuente o posición fuera de rango.")
            referenced.add(source)
            if len(fields) == 5:
                name_index += fields[4]
                if not 0 <= name_index < len(names):
                    raise ValueError("Índice de nombre fuera de rango.")
    return referenced


def package_name(source):
    if "node_modules/" not in source:
        return None
    tail = source.rsplit("node_modules/", 1)[1]
    parts = tail.split("/")
    name = "/".join(parts[:2]) if tail.startswith("@") else parts[0]
    if not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name):
        raise ValueError("Nombre de paquete no interpretable.")
    return name


def lock_candidates(lock):
    if lock is None:
        return {}
    if lock.get("lockfileVersion") != 3 or not isinstance(lock.get("packages"), dict):
        raise ValueError("Se requiere package-lock v3.")
    result = {}
    for path, row in lock["packages"].items():
        name = package_name(path)
        if name and row.get("version"):
            result.setdefault(name, []).append(
                {
                    "lock_path": path,
                    "version": row["version"],
                    "resolved": row.get("resolved"),
                    "integrity": row.get("integrity"),
                    "license_declared_in_lock": row.get("license"),
                    "bundle_bytes_verified": False,
                }
            )
    return result


def inventory_artifact(artifact, files, claims, lock=None):
    packages = {}
    maps = []
    linked = set()
    without_map = []
    candidates = lock_candidates(lock)
    for name, raw in sorted(files.items()):
        if not name.endswith(".js"):
            continue
        links = re.findall(rb"(?m)^[ \t]*//[#@][ \t]*sourceMappingURL=([^\s]+)[ \t]*$", raw)
        if not links:
            without_map.append(name)
            continue
        if len(links) != 1:
            raise ValueError("Múltiples vínculos sourceMappingURL.")
        relative = links[0].decode("utf-8", errors="strict")
        inline = relative.startswith("data:")
        if inline:
            prefix, separator, payload = relative.partition(",")
            if (
                prefix
                not in ("data:application/json;charset=utf8;base64", "data:application/json;base64")
                or not separator
                or len(payload) > 8 * 1024**2
            ):
                raise ValueError("Mapa inline de tipo o tamaño no admitido.")
            map_raw = base64.b64decode(payload, validate=True)
            if len(map_raw) > 6 * 1024**2:
                raise ValueError("Mapa inline fuera de límite.")
            map_name = name + "#source-map-inline"
        else:
            if ":" in relative or relative.startswith("/") or "?" in relative or "#" in relative:
                raise ValueError("Mapa remoto o ambiguo: no se descarga.")
            map_name = posixpath.normpath(posixpath.join(posixpath.dirname(name), relative))
            if map_name not in files or map_name.startswith("../"):
                raise ValueError("sourceMappingURL sin mapa local incorporado.")
            linked.add(map_name)
            map_raw = files[map_name]
        data = decode(map_raw)
        references = map_references(data)
        contents = data.get("sourcesContent")
        maps.append(
            {
                "member": map_name,
                "inline": inline,
                "sha256": digest(map_raw),
                "generated_member": name,
                "generated_sha256": digest(raw),
                "sources_listed": len(data["sources"]),
                "sources_referenced": len(references),
            }
        )
        for index in sorted(references):
            source = data["sources"][index]
            package = package_name(source)
            if package is None:
                continue
            content = contents[index] if contents is not None else None
            source_sha = (
                digest(content.encode("utf-8", errors="strict")) if content is not None else None
            )
            row = packages.setdefault(
                package,
                {
                    "name": package,
                    "version": None,
                    "version_status": "pending",
                    "incorporation_evidence": "source-map segment references a source attributed to node_modules; no rebuild reproduced",
                    "lock_candidates": candidates.get(package, []),
                    "sources": [],
                },
            )
            row["sources"].append(
                {"map": map_name, "source_index": index, "source": source, "sha256": source_sha}
            )
    orphans = sorted(name for name in files if name.endswith(".js.map") and name not in linked)
    if orphans:
        raise ValueError("Mapa JS sin generado local que lo vincule.")
    evidence = []
    for claim in claims:
        raw = files[claim["member"]]
        needle = claim["needle"].encode("utf-8")
        if (
            digest(raw) != claim["sha256"]
            or raw.count(needle) != 1
            or claim["version"] not in claim["needle"]
        ):
            raise ValueError("La evidencia de versión revisada no coincide.")
        evidence.append(
            {
                **claim,
                "offset": raw.index(needle),
                "classification": "self-declared; not registry archive equivalence",
                "artifact": artifact,
            }
        )
    return {
        "artifact": artifact,
        "sca_approved": False,
        "catalog": [
            {"member": name, "bytes": len(raw), "sha256": digest(raw)}
            for name, raw in sorted(files.items())
        ],
        "maps": maps,
        "packages": [packages[name] for name in sorted(packages)],
        "version_evidence": evidence,
        "coverage": {
            "javascript_files": sum(name.endswith(".js") for name in files),
            "linked_javascript_maps": len(maps),
            "javascript_without_linked_map": without_map,
            "unlinked_other_maps": sorted(
                name for name in files if name.endswith(".map") and name not in linked
            ),
            "wasm_files": sorted(name for name in files if name.endswith(".wasm")),
            "component_version_coverage_complete": False,
        },
    }


def compare_embedded(wheel_files, sdist_files):
    checked = 0
    for name, raw in wheel_files.items():
        if not name.startswith(("flasgger/ui2/", "flasgger/ui3/")):
            continue
        expected = sdist_files.get("flasgger-0.9.7.1/" + name)
        if expected is None or digest(expected) != digest(raw):
            raise ValueError("Un recurso incorporado difiere del sdist original.")
        checked += 1
    return checked


def pinned(spec):
    path = ROOT / spec["path"]
    info = path.lstat()
    if (
        not path.resolve().is_relative_to(ROOT.resolve())
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > spec["maximum_bytes"]
        or getattr(info, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("Entrada no regular, enlazada o fuera de límite.")
    raw = path.read_bytes()
    if len(raw) != info.st_size or digest(raw) != spec["sha256"]:
        raise ValueError("Entrada alterada frente a su pin.")
    if "git_blob_sha1" in spec:
        identity = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if identity != spec["git_blob_sha1"]:
            raise ValueError("Identidad de blob distinta de la captura API fijada.")
    return raw


def prepare(manifest_path):
    manifest_raw = manifest_path.read_bytes()
    manifest = decode(manifest_raw)
    files = {
        key: read_archive(pinned(spec), spec["kind"]) for key, spec in manifest["artifacts"].items()
    }
    for member, expected in manifest["kitsu_identity"].items():
        if files["kitsu"][member].decode("utf-8").strip() != expected:
            raise ValueError("Identidad de release Kitsu diferente.")
    count = compare_embedded(files["flasgger"], files["flasgger_sdist"])
    lock = decode(pinned(manifest["kitsu_lock"])) if manifest.get("kitsu_lock") else None
    package = decode(pinned(manifest["kitsu_package"])) if manifest.get("kitsu_package") else None
    if package is not None and (package.get("name"), package.get("version")) != (
        "kitsu",
        manifest["kitsu_identity"][".version.txt"],
    ):
        raise ValueError("El manifiesto upstream no corresponde a la release Kitsu.")
    inventories = [
        inventory_artifact(
            name,
            files[name],
            manifest["version_claims"].get(name, []),
            lock if name == "kitsu" else None,
        )
        for name in ("kitsu", "flasgger")
    ]
    return {
        "schema_version": 1,
        "manifest_sha256": digest(manifest_raw),
        "inputs": manifest["artifacts"],
        "kitsu_lock": manifest.get("kitsu_lock"),
        "kitsu_package": manifest.get("kitsu_package"),
        "flasgger_embedded_members_identical_to_sdist": count,
        "inventories": inventories,
        "primary_references": manifest["primary_references"],
        "limitations": manifest["limitations"],
        "sca_approved": False,
        "javascript_executed": False,
        "network_used": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--output", type=Path, help="Publica un JSON nuevo; sin sobrescribir.")
    args = parser.parse_args()
    result = prepare(args.manifest)
    if args.output:
        raw = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )
        with args.output.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"path": str(args.output), "sha256": digest(raw), "bytes": len(raw)}))
    print(
        json.dumps(
            {
                "artifacts": [
                    {
                        "name": x["artifact"],
                        "packages_attributed_by_referenced_source": len(x["packages"]),
                        "version_claims": len(x["version_evidence"]),
                        "javascript_files": x["coverage"]["javascript_files"],
                    }
                    for x in result["inventories"]
                ],
                "sca_approved": False,
            }
        )
    )


if __name__ == "__main__":
    main()
