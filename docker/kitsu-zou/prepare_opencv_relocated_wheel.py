"""Deriva un wheel desde nueve capturas fijadas; plan local sin ejecutar ELF/SDK."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import struct
import sys
import types
import zipfile
from pathlib import Path, PurePosixPath

PROJECT = Path(__file__).resolve().parents[2]
PACKAGER_PATH = "docker/kitsu-zou/package_opencv_minimal.py"
PACKAGER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
PARSER_PATH = "docker/postgres-immich/final_image_remote.py"
PARSER_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
OLD = b"$ORIGIN/opencv_python_headless.libs:$ORIGIN:/overlay/usr/lib"
NEW = b"$ORIGIN/opencv_python_headless.libs:$ORIGIN"
SUFFIX = b":/overlay/usr/lib"
SPEC_PATH = "docker/kitsu-zou/opencv-relocation-spec.json"
INPUT_RELATIVE = ".tools/backup-source-build/opencv-relocation-inputs-v1"
OUTPUT_RELATIVE = ".tools/backup-source-build/opencv-relocated-wheel-v1"
OWNER = "agencia-digital-marketing-ai:opencv-relocated-wheel:v1"
RECEIPT_SHA = "0a7a51e8b6178de8fbe717df453710c1404b77cd59a1fa0819cef76961735a04"
INPUT_MARKER_SHA = "250051cd9556e013267cbeed5ace8057c383b2b98aeb70c194a266557f4f154b"
ACL_SOURCE_SHA256 = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
PATCHES = {"stage/cv2.cpython-312-x86_64-linux-gnu.so", "stage/lib/libopencv_imgcodecs.so.4.13.0"}
INPUTS = PATCHES | {
    "stage/lib/libopencv_core.so.4.13.0",
    "stage/lib/libopencv_imgproc.so.4.13.0",
    "build/CMakeCache.txt",
    "build/opencv2/cvconfig.h",
    "build/opencv2/opencv_modules.hpp",
    "manifest-approved.json",
    "LICENSE",
}
STRING_TAGS = {1, 14, 15, 29, 0x7FFFFFFD, 0x7FFFFFFF, 0x6FFFFEFA, 0x6FFFFEFB, 0x6FFFFEFC}
VALUE_TAGS = (set(range(39)) - STRING_TAGS) | {
    0x6FFFFEF5,
    0x6FFFFEF6,
    0x6FFFFEF7,
    0x6FFFFFF0,
    0x6FFFFFF9,
    0x6FFFFFFA,
    0x6FFFFFFB,
    0x6FFFFFFC,
    0x6FFFFFFD,
    0x6FFFFFFE,
    0x6FFFFFFF,
}


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def span(raw, offset, size):
    require(
        type(offset) is int
        and type(size) is int
        and 0 <= offset <= len(raw)
        and 0 <= size <= len(raw) - offset,
        "Rango fuera de archivo.",
    )
    return raw[offset : offset + size]


def overlap(a, b):
    return max(a[0], b[0]) < min(a[1], b[1])


def read_regular(path, maximum=128 * 1024**2):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "Enlace o reparse no admitido.",
        )
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= maximum,
        "Archivo irregular o excesivo.",
    )
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    try:

        def common(n):
            return (n.st_dev, n.st_ino, n.st_mode, n.st_nlink, n.st_size, n.st_mtime_ns)

        before = os.fstat(descriptor)
        require(common(before) == common(info), "Archivo sustituido al abrir.")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(maximum + 1)
        after, end = os.fstat(descriptor), path.lstat()
        require(
            len(raw) == info.st_size
            and common(info) == common(before) == common(after) == common(end)
            and before.st_ctime_ns == after.st_ctime_ns
            and info.st_ctime_ns == end.st_ctime_ns,
            "Archivo cambiado durante lectura.",
        )
        return raw
    finally:
        os.close(descriptor)


def frozen_sources():
    modules = []
    for name, path, pin in (
        ("opencv_packager_frozen", PACKAGER_PATH, PACKAGER_SHA),
        ("opencv_elf_frozen", PARSER_PATH, PARSER_SHA),
    ):
        raw = read_regular(PROJECT / path, 256 * 1024)
        require(sha(raw) == pin, "Fuente congelada distinta del pin.")
        module = types.ModuleType(name)
        module.__file__ = str(PROJECT / path)
        sys.modules[name] = module
        exec(compile(raw, str(PROJECT / path), "exec"), vars(module))
        modules.append(module)
    return tuple(modules)


def rpath_interval(raw):
    """Inventaría cada referencia estándar a dynstr; semántica desconocida se rechaza."""
    require(
        64 <= len(raw) <= 128 * 1024**2 and raw[:7] == b"\x7fELF\x02\x01\x01",
        "ELF64 little-endian requerido.",
    )
    h = struct.unpack_from("<HHIQQQIHHHHHH", raw, 16)
    require(
        h[:3] == (3, 62, 1)
        and h[7] == 64
        and h[8] == 56
        and h[10] == 64
        and 0 < h[9] <= 512
        and 0 < h[11] < 512,
        "Cabecera ELF no admitida.",
    )
    ph = span(raw, h[4], h[8] * h[9])
    sh = span(raw, h[5], h[10] * h[11])
    sections = [struct.unpack_from("<IIQQQQIIQQ", sh, i * 64) for i in range(h[11])]
    dynamic = [(i, row) for i, row in enumerate(sections) if row[1] == 6]
    require(len(dynamic) == 1, "Sección dinámica no única.")
    dynamic_index, d = dynamic[0]
    require(d[6] < len(sections) and d[9] == 16 and d[5] % 16 == 0, "Tabla dinámica inválida.")
    table_index = d[6]
    table = sections[table_index]
    require(table[1] == 3 and table[5] > 0, "dynstr inválida.")
    strings = span(raw, table[4], table[5])
    region = (table[4], table[4] + table[5])
    require(h[12] < len(sections) and sections[h[12]][1] == 3, "shstrtab inválida.")
    names = span(raw, sections[h[12]][4], sections[h[12]][5])

    def cstring(data, at):
        require(0 <= at < len(data), "Índice de cadena fuera de límites.")
        end = data.find(b"\0", at)
        require(end >= at and end - at <= 65536, "Cadena no terminada o excesiva.")
        return data[at:end], end + 1

    require(cstring(names, table[0])[0] == b".dynstr", "Tabla enlazada no es .dynstr.")
    for i, row in enumerate(sections):
        if row[1] != 8 and row[5]:
            span(raw, row[4], row[5])
            require(
                i == table_index or not overlap(region, (row[4], row[4] + row[5])),
                "Secciones comparten bytes de dynstr.",
            )
    for start, size in ((0, 64), (h[4], len(ph)), (h[5], len(sh))):
        require(not overlap(region, (start, start + size)), "dynstr solapa una tabla ELF.")
    loads = []
    for at in range(0, len(ph), 56):
        row = struct.unpack_from("<IIQQQQQQ", ph, at)
        if row[0] == 1 and row[3] <= table[3] and table[3] + table[5] <= row[3] + row[5]:
            loads.append(row[2] + table[3] - row[3])
    require(loads == [table[4]], "Mapeo virtual de dynstr ambiguo.")
    refs = []

    def reference(offset, kind):
        _text, end = cstring(strings, offset)
        refs.append((offset, end, kind))
        require(len(refs) <= 1000000, "Demasiadas referencias ELF.")

    payload = span(raw, d[4], d[5])
    tags = {}
    rpaths = []
    terminated = False
    for at in range(0, len(payload), 16):
        tag, value = struct.unpack_from("<qQ", payload, at)
        if terminated:
            require((tag, value) == (0, 0), "Datos dinámicos tras DT_NULL.")
            continue
        if tag == 0:
            terminated = True
            continue
        tags.setdefault(tag, []).append(value)
        require(
            tag in STRING_TAGS or tag in VALUE_TAGS, "Etiqueta dinámica de semántica no cubierta."
        )
        if tag in STRING_TAGS:
            reference(value, "dynamic:" + str(tag))
            if tag == 15:
                rpaths.append(value)
    require(
        terminated and len(rpaths) == 1 and 29 not in tags, "DT_RPATH no único o RUNPATH presente."
    )
    require(
        tags.get(5) == [table[3]] and tags.get(10) == [table[5]],
        "DT_STRTAB/DT_STRSZ no corresponden a dynstr.",
    )
    for index, row in enumerate(sections):
        if index == dynamic_index or row[6] != table_index:
            continue
        data = span(raw, row[4], row[5])
        kind = row[1]
        if kind in (2, 11):
            require(row[9] == 24 and len(data) % 24 == 0, "Tabla de símbolos inválida.")
            for at in range(0, len(data), 24):
                reference(struct.unpack_from("<I", data, at)[0], "symbol")
        elif kind in (0x6FFFFFFE, 0x6FFFFFFD):
            at = 0
            visited = set()
            while at < len(data):
                require(at not in visited and len(visited) < 10000, "Versiones cíclicas.")
                visited.add(at)
                if kind == 0x6FFFFFFE:
                    version, count, filename, aux, next_row = struct.unpack(
                        "<HHIII", span(data, at, 16)
                    )
                    reference(filename, "version-file")
                    aux_size = 16
                else:
                    version, _flags, _ndx, count, _hash, aux, next_row = struct.unpack(
                        "<HHHHIII", span(data, at, 20)
                    )
                    aux_size = 8
                require(
                    version == 1 and 0 < count <= 10000 and aux > 0, "Cabecera de versión inválida."
                )
                cursor = at + aux
                seen = set()
                for number in range(count):
                    require(cursor not in seen, "Auxiliares cíclicos.")
                    seen.add(cursor)
                    block = span(data, cursor, aux_size)
                    if kind == 0x6FFFFFFE:
                        _hash, _flags, _other, name, next_aux = struct.unpack("<IHHII", block)
                    else:
                        name, next_aux = struct.unpack("<II", block)
                    reference(name, "version-name")
                    require(
                        (number == count - 1 and next_aux == 0)
                        or (number < count - 1 and next_aux >= aux_size),
                        "Cadena auxiliar ambigua.",
                    )
                    cursor += next_aux
                if not next_row:
                    break
                require(next_row > 0, "Cadena de versión inválida.")
                at += next_row
        else:
            raise ValueError("Sección enlazada a dynstr de semántica no cubierta.")
    start = rpaths[0]
    text, end = cstring(strings, start)
    require(
        text == OLD and (start == 0 or strings[start - 1] == 0),
        "RPATH no coincide con la cadena completa fijada.",
    )
    for left, right, kind in refs:
        if kind == "dynamic:15" and left == start:
            continue
        require(
            not overlap((start, end), (left, right)),
            "RPATH comparte almacenamiento con otra referencia.",
        )
    return {
        "interval": [table[4] + end - 1 - len(SUFFIX), table[4] + end - 1],
        "string_interval": [table[4] + start, table[4] + end],
        "dynstr_section": table_index,
        "dynamic_section": dynamic_index,
        "references_checked": len(refs),
        "rpath_before": OLD.decode(),
        "rpath_after": NEW.decode(),
    }


def verify_delta(original, derived, interval):
    start, end = interval
    require(
        len(original) == len(derived) and 0 <= start < end <= len(original),
        "Tamaño o intervalo distinto.",
    )
    require(
        original[:start] == derived[:start] and original[end:] == derived[end:],
        "Cambio fuera del intervalo autorizado.",
    )
    require(
        original[start:end] == SUFFIX and derived[start:end] == bytes(end - start),
        "Transformación diferente del sufijo fijado.",
    )


def relocate(raw):
    proof = rpath_interval(raw)
    start, end = proof["interval"]
    derived = raw[:start] + bytes(end - start) + raw[end:]
    verify_delta(raw, derived, proof["interval"])
    return derived, {
        **proof,
        "sha256_before": sha(raw),
        "sha256_after": sha(derived),
        "bytes": len(raw),
        "outside_interval_unchanged": True,
        "tables_and_offsets_unchanged": True,
        "build_id_preserved": True,
    }


def json_object(raw):
    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "JSON con claves duplicadas.")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _s: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def safe_relative(name):
    require(
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9_+./-]+", name)
        and not name.startswith("/"),
        "Ruta relativa inválida.",
    )
    for part in name.split("/"):
        require(part not in ("", ".", "..") and part.rstrip(". ") == part, "Componente ambiguo.")
        require(
            part.split(".", 1)[0].upper()
            not in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *[f"COM{i}" for i in range(1, 10)],
                *[f"LPT{i}" for i in range(1, 10)],
            },
            "Dispositivo Windows no admitido.",
        )
    return name


def validate_spec(spec):
    require(
        set(spec)
        == {
            "schema_version",
            "owner",
            "input_relative",
            "input_receipt_sha256",
            "input_marker_sha256",
            "inputs",
            "output_relative",
            "openexr_directory",
            "parent_marker",
            "parent_owner",
            "source_capture",
            "patches",
            "sources",
            "wheel_execution_authorized",
            "runtime_verified",
            "security_approved",
        },
        "Campos de spec distintos del contrato.",
    )
    require(
        spec["schema_version"] == 1
        and spec["owner"] == OWNER
        and spec["input_relative"] == INPUT_RELATIVE
        and spec["output_relative"] == OUTPUT_RELATIVE
        and spec["input_receipt_sha256"] == RECEIPT_SHA
        and spec["input_marker_sha256"] == INPUT_MARKER_SHA
        and set(spec["inputs"]) == INPUTS
        and set(spec["patches"]) == PATCHES
        and spec["openexr_directory"] == "/overlay/usr/lib/cmake/OpenEXR"
        and spec["parent_marker"] == ".agency-backup-build-owner"
        and spec["parent_owner"] == "agencia-digital-marketing-ai:backup-source-build:v1"
        and all(
            spec[key] is False
            for key in ("wheel_execution_authorized", "runtime_verified", "security_approved")
        ),
        "Spec de otra identidad o alcance.",
    )
    require(
        spec["sources"]
        == {
            "packager": {"path": PACKAGER_PATH, "sha256": PACKAGER_SHA},
            "elf_parser": {"path": PARSER_PATH, "sha256": PARSER_SHA},
            "acl_reader": {"path": "sre/backup_tools_access.py", "sha256": ACL_SOURCE_SHA256},
        },
        "Fuentes auxiliares no fijadas.",
    )
    for name, row in spec["inputs"].items():
        safe_relative(name)
        require(
            set(row) == {"sha256", "size"}
            and re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
            and type(row["size"]) is int
            and 0 < row["size"] <= 128 * 1024**2,
            "Pin de archivo inválido.",
        )


def load_spec():
    raw = read_regular(PROJECT / SPEC_PATH, 65536)
    spec = json_object(raw)
    validate_spec(spec)
    return spec, sha(raw)


def access_reader():
    path = PROJECT / "sre/backup_tools_access.py"
    raw = read_regular(path, 128 * 1024)
    require(sha(raw) == ACL_SOURCE_SHA256, "Lector ACL distinto del pin.")
    module = types.ModuleType("opencv_relocation_acl")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), "exec"), vars(module))
    return module


def exact_input_tree(root, expected):
    wanted = set(expected)
    parents = {str(p) for name in wanted for p in PurePosixPath(name).parents if str(p) != "."}
    found = set()

    def walk(path, prefix=""):
        with os.scandir(path) as entries:
            for entry in entries:
                # DirEntry.stat de Windows informa nlink/dev/ino como cero; stat explícito conserva esos controles.
                name = prefix + entry.name
                info = os.stat(entry.path, follow_symlinks=False)
                require(
                    not stat.S_ISLNK(info.st_mode)
                    and not getattr(info, "st_file_attributes", 0) & 0x400,
                    "Enlace en captura.",
                )
                if stat.S_ISDIR(info.st_mode):
                    require(name in parents, "Directorio adicional en captura.")
                    walk(Path(entry.path), name + "/")
                else:
                    require(
                        stat.S_ISREG(info.st_mode)
                        and info.st_nlink == 1
                        and name in wanted
                        and name not in found,
                        "Archivo adicional o enlazado en captura.",
                    )
                    found.add(name)

    walk(root)
    require(found == wanted, "Captura incompleta.")


def capture_inputs(spec):
    root = PROJECT / INPUT_RELATIVE
    marker_raw = read_regular(root / ".owner.json", 4096)
    require(sha(marker_raw) == INPUT_MARKER_SHA, "Marcador de captura cambiado.")
    marker = json_object(marker_raw)
    node = root.lstat()
    require(
        marker.get("owner") == "agencia-digital-marketing-ai:opencv-relocation-inputs:v1"
        and marker.get("path") == str(root)
        and marker.get("directory") == {"device": node.st_dev, "inode": node.st_ino},
        "Identidad local de captura distinta del respaldo.",
    )
    raw_receipt = read_regular(root / "receipt.json", 65536)
    require(sha(raw_receipt) == RECEIPT_SHA, "Recibo de captura cambiado.")
    receipt = json_object(raw_receipt)
    require(
        receipt.get("success") is True
        and receipt.get("read_only") is True
        and receipt.get("sdk_executed") is False
        and receipt.get("configuration_validated") is True
        and receipt.get("observation") == spec["source_capture"]
        and receipt["observation"].get("running") is False
        and receipt["observation"].get("pid") == 0,
        "Captura sin observación detenida acreditada.",
    )
    require(
        {
            name: {key: row[key] for key in ("sha256", "size")}
            for name, row in receipt["files"].items()
        }
        == spec["inputs"],
        "Spec no corresponde a nueve pins capturados.",
    )
    exact_input_tree(root, INPUTS | {"receipt.json", ".owner.json"})
    result = {}
    for name, row in spec["inputs"].items():
        raw = read_regular(root / name)
        require(
            len(raw) == row["size"] and sha(raw) == row["sha256"],
            "Bytes capturados distintos del pin: " + name,
        )
        result[name] = raw
    return result


def configuration(packager, inputs, spec):
    cache = {}
    for line in inputs["build/CMakeCache.txt"].decode("utf-8").splitlines():
        if line and not line.startswith(("#", "//")) and "=" in line:
            key, value = line.split("=", 1)
            key = key.split(":", 1)[0]
            require(key not in cache, "Cache CMake duplicada.")
            cache[key] = value
    packager.validate_configuration(
        cache,
        inputs["build/opencv2/cvconfig.h"].decode("utf-8"),
        inputs["build/opencv2/opencv_modules.hpp"].decode("utf-8"),
        spec["openexr_directory"],
    )
    manifest = json_object(inputs["manifest-approved.json"])
    require(
        manifest.get("build_enabled") is True
        and manifest.get("cv_version") == packager.CV_VERSION
        and manifest.get("wheel_version") == packager.WHEEL_VERSION
        and manifest.get("numpy_requires_dist") == packager.NUMPY_REQUIRES,
        "Manifiesto de otra compilación.",
    )
    return manifest


def transformed_inputs(inputs, spec, packager, parser):
    derived = dict(inputs)
    proofs = {}
    elf_metadata = {}
    for name in sorted(n for n in inputs if n.startswith("stage/")):
        before = parser.elf_info(inputs[name])
        if name in PATCHES:
            changed, proof = relocate(inputs[name])
            pin = spec["patches"][name]
            require(
                proof["interval"] == pin["interval"]
                and proof["sha256_after"] == pin["sha256_after"],
                "Intervalo o SHA derivado diferente de la spec.",
            )
            after = parser.elf_info(changed)
            require(after == {**before, "rpath": NEW.decode()}, "Metadata ELF adicional cambió.")
            derived[name] = changed
            proofs[name] = proof
        else:
            require(
                before["rpath"] == NEW.decode() and before["runpath"] is None,
                "ELF sin parche de otra identidad.",
            )
            after = before
        metadata = packager.elf_dynamic(derived[name])
        elf_metadata[name] = {
            "sha256": sha(derived[name]),
            "bytes": len(derived[name]),
            "packager": metadata,
            "elf_metadata_sha256": sha(canonical(after)),
            "non_rpath_metadata_unchanged": True,
        }
    return derived, proofs, elf_metadata


def make_plan():
    spec, spec_sha = load_spec()
    access = access_reader()
    parent = PROJECT / ".tools/backup-source-build"
    input_root = PROJECT / INPUT_RELATIVE
    access.check_access(parent, private=True)
    access.check_access(input_root, private=True)
    marker = read_regular(parent / spec["parent_marker"], 4096)
    require(marker == spec["parent_owner"].encode(), "Raíz privada sin marcador acreditado.")
    output = PROJECT / OUTPUT_RELATIVE
    require(not os.path.lexists(output), "La salida ya existe; no se adopta ni se reemplaza.")
    inputs = capture_inputs(spec)
    packager, parser = frozen_sources()
    configuration(packager, inputs, spec)
    _derived, proofs, metadata = transformed_inputs(inputs, spec, packager, parser)
    node = parent.lstat()
    source = input_root.lstat()
    return {
        "schema_version": 1,
        "owner": OWNER,
        "source_sha256": sha(read_regular(Path(__file__), 128 * 1024)),
        "spec_sha256": spec_sha,
        "input_receipt_sha256": RECEIPT_SHA,
        "input_root": str(input_root),
        "input_identity": {"device": source.st_dev, "inode": source.st_ino},
        "output_root": str(output),
        "parent": {
            "path": str(parent),
            "device": node.st_dev,
            "inode": node.st_ino,
            "marker_sha256": sha(marker),
        },
        "input_files": spec["inputs"],
        "transformations": proofs,
        "elf": metadata,
        "frozen_sources": spec["sources"],
        "configuration_validated": True,
        "wheel_name": packager.WHEEL_NAME,
        "wheel_created": False,
        "runtime_verified": False,
        "security_approved": False,
        "elf_executed": False,
        "sdk_executed": False,
    }


def write_exclusive(path, raw):
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def write_relative(root, name, raw):
    safe_relative(name)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent in (*path.parent.parents, path.parent):
        info = parent.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "Padre de salida no admitido.",
        )
    write_exclusive(path, raw)


def verify_wheel(path, packager, derived, manifest):
    runtime = {
        "cv2.cpython-312-x86_64-linux-gnu.so": derived["stage/cv2.cpython-312-x86_64-linux-gnu.so"]
    }
    runtime.update(
        {
            "opencv_python_headless.libs/libopencv_" + name + ".so.413": derived[
                "stage/lib/libopencv_" + name + ".so.4.13.0"
            ]
            for name in ("core", "imgproc", "imgcodecs")
        }
    )
    dist = packager.DIST_INFO
    expected = set(runtime) | {
        dist + "/" + name
        for name in (
            "METADATA",
            "WHEEL",
            "AGENCY-BUILD.json",
            "licenses/LICENSE",
            "licenses/NUMPY-LICENSE",
            "RECORD",
        )
    }
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()
        names = [row.filename for row in info]
        require(
            len(names) == len(set(names))
            and set(names) == expected
            and sum(row.file_size for row in info) <= 256 * 1024**2,
            "Miembros de wheel distintos del contrato.",
        )
        files = {name: archive.read(name) for name in names}
    require(
        all(files[name] == raw for name, raw in runtime.items()),
        "Wheel no conserva los ELF derivados.",
    )
    require(
        files[dist + "/licenses/LICENSE"] == derived["LICENSE"]
        and files[dist + "/licenses/NUMPY-LICENSE"]
        == base64.b64decode(manifest["numpy_license"]["base64"], validate=True),
        "Licencias no preservadas.",
    )
    metadata = files[dist + "/METADATA"].decode("utf-8").splitlines()
    require(
        "Version: " + packager.WHEEL_VERSION in metadata
        and [line[15:] for line in metadata if line.startswith("Requires-Dist: ")]
        == packager.NUMPY_REQUIRES,
        "Metadatos/requisitos distintos de upstream.",
    )
    rows = list(csv.reader(io.StringIO(files[dist + "/RECORD"].decode("utf-8"))))
    require(
        len(rows) == len(expected)
        and {row[0] for row in rows} == expected
        and all(len(row) == 3 for row in rows),
        "RECORD incompleto o ambiguo.",
    )
    for name, digest, size in rows:
        if name == dist + "/RECORD":
            require(digest == size == "", "RECORD propio no canónico.")
        else:
            require(
                digest
                == "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest())
                .decode()
                .rstrip("=")
                and size == str(len(files[name])),
                "Hash/tamaño de RECORD incorrecto.",
            )
    raw = read_regular(path, 256 * 1024**2)
    return {
        "path": str(path),
        "sha256": sha(raw),
        "bytes": len(raw),
        "members": len(names),
        "record_verified": True,
        "licenses_preserved": True,
        "runtime_verified": False,
        "security_approved": False,
    }


def apply(expected_plan_sha):
    plan = make_plan()
    require(sha(canonical(plan)) == expected_plan_sha, "Plan no corresponde al hash aprobado.")
    spec, _spec_sha = load_spec()
    inputs = capture_inputs(spec)
    packager, parser = frozen_sources()
    manifest = configuration(packager, inputs, spec)
    derived, proofs, _metadata = transformed_inputs(inputs, spec, packager, parser)
    output = Path(plan["output_root"])
    parent = output.parent
    access = access_reader()
    access.check_access(parent, private=True)
    node = parent.lstat()
    require(
        (node.st_dev, node.st_ino) == (plan["parent"]["device"], plan["parent"]["inode"]),
        "Padre privado cambió.",
    )
    os.mkdir(output, 0o700)
    access.check_access(output, private=True)
    identity = output.lstat()
    marker = {
        "owner": OWNER,
        "path": str(output),
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "plan_sha256": expected_plan_sha,
    }
    write_exclusive(output / ".owner.json", canonical(marker))
    write_exclusive(output / "plan.json", canonical(plan))
    for name, raw in derived.items():
        write_relative(output, name, raw)
    require(capture_inputs(spec) == inputs, "La captura original cambió durante la copia.")
    for name, raw in derived.items():
        require(read_regular(output / name) == raw, "La copia derivada cambió.")
    wheel_dir = output / "wheel"
    os.mkdir(wheel_dir, 0o700)
    wheel = packager.write_wheel(output / "stage", wheel_dir, output / "LICENSE", manifest)
    verified = verify_wheel(wheel, packager, derived, manifest)
    require(capture_inputs(spec) == inputs, "La captura original cambió durante empaquetado.")
    receipt = {
        "success": True,
        "owner": OWNER,
        "output": marker,
        "source_capture_receipt_sha256": RECEIPT_SHA,
        "transformations": proofs,
        "wheel": verified,
        "gnu_build_id": "conservado como dato heredado; no recalculado",
        "originals_unchanged": True,
        "frozen_sources": spec["sources"],
        "elf_executed": False,
        "sdk_executed": False,
    }
    write_exclusive(output / "receipt.json", canonical(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            require(
                isinstance(args.expected_plan_sha256, str)
                and re.fullmatch(r"[a-f0-9]{64}", args.expected_plan_sha256),
                "Falta hash externo de plan.",
            )
            result = apply(args.expected_plan_sha256)
        else:
            plan = make_plan()
            result = {"plan": plan, "plan_sha256": sha(canonical(plan))}
        print(canonical(result).decode())
        return 0
    except Exception as error:
        print(
            canonical(
                {
                    "success": False,
                    "error": "OPENCV_RELOCATION_REJECTED",
                    "error_type": type(error).__name__,
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
