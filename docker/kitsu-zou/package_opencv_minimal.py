"""Inputs y wheel OpenCV propios; no descarga, instala paquetes ni ejecuta SDK."""

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import struct
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

CV_VERSION = "4.13.0"
WHEEL_VERSION = "4.13.0.92+agency1"
DIST_INFO = "opencv_python_headless-" + WHEEL_VERSION + ".dist-info"
WHEEL_NAME = "opencv_python_headless-" + WHEEL_VERSION + "-cp312-cp312-linux_x86_64.whl"
VERIFIER_SHA256 = "b00b1a1a1822b13ed145128209664a15f3efa781b39cd850d0d63bcc0666192b"
NUMPY_REQUIRES = ['numpy<2.0; python_version < "3.9"', 'numpy>=2; python_version >= "3.9"']
PROJECT = Path(__file__).resolve().parents[2]
REQUIRED_CACHE = {
    "BUILD_LIST": "core,imgproc,imgcodecs,python3",
    "BUILD_opencv_python3": "ON",
    "BUILD_opencv_python2": "OFF",
    "BUILD_opencv_world": "OFF",
    "BUILD_opencv_videoio": "OFF",
    "BUILD_opencv_highgui": "OFF",
    "WITH_FFMPEG": "OFF",
    "WITH_GSTREAMER": "OFF",
    "WITH_V4L": "OFF",
    "WITH_QT": "OFF",
    "WITH_GTK": "OFF",
    "WITH_OPENGL": "OFF",
    "WITH_OPENCL": "OFF",
    "WITH_CUDA": "OFF",
    "WITH_IPP": "OFF",
    "WITH_ITT": "OFF",
    "WITH_TBB": "OFF",
    "WITH_LAPACK": "OFF",
    "WITH_EIGEN": "OFF",
    "WITH_PNG": "ON",
    "BUILD_PNG": "OFF",
    "BUILD_ZLIB": "OFF",
    "WITH_OPENEXR": "ON",
    "BUILD_OPENEXR": "OFF",
    "WITH_IMGCODEC_HDR": "ON",
    "WITH_IMGCODEC_GIF": "OFF",
    "WITH_IMGCODEC_PXM": "OFF",
    "WITH_IMGCODEC_SUNRASTER": "OFF",
    "WITH_IMGCODEC_PFM": "OFF",
    "WITH_JPEG": "OFF",
    "WITH_TIFF": "OFF",
    "WITH_WEBP": "OFF",
    "WITH_OPENJPEG": "OFF",
    "WITH_JASPER": "OFF",
    "WITH_AVIF": "OFF",
    "WITH_JPEGXL": "OFF",
    "WITH_GDAL": "OFF",
    "WITH_GDCM": "OFF",
    "BUILD_TESTS": "OFF",
    "BUILD_PERF_TESTS": "OFF",
    "BUILD_EXAMPLES": "OFF",
    "BUILD_opencv_apps": "OFF",
    "BUILD_opencv_java": "OFF",
    "PYTHON3_LIMITED_API": "OFF",
    "OPENCV_SKIP_PYTHON_LOADER": "ON",
    "OPENCV_LIB_INSTALL_PATH": "lib",
    "OPENCV_FORCE_PYTHON_LIBS": "OFF",
    "CMAKE_INSTALL_RPATH": "$ORIGIN/opencv_python_headless.libs;$ORIGIN",
    "CMAKE_INSTALL_RPATH_USE_LINK_PATH": "OFF",
    "CMAKE_MAKE_PROGRAM": "/usr/lib/ninja-build/bin/ninja",
    "CMAKE_DISABLE_FIND_PACKAGE_Git": "ON",
    "OPENCV_VCSVERSION": CV_VERSION + "+agency1",
}


def _verifier():
    path = PROJECT / "docker/postgres-immich/native_inputs.py"
    raw = read_regular(path, 128 * 1024)
    if hashlib.sha256(raw).hexdigest() != VERIFIER_SHA256:
        raise ValueError("El verificador congelado cambió.")
    spec = importlib.util.spec_from_file_location("opencv_apk_frozen_verifier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_regular(path, maximum):
    path = Path(path)
    for part in (*reversed(path.absolute().parents), path.absolute()):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("No se admiten enlaces ni puntos de reanálisis.")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise ValueError("Archivo irregular, enlazado o excesivo.")
    with path.open("rb") as stream:
        held = os.fstat(stream.fileno())
        data = stream.read(maximum + 1)
    if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino) or len(data) != info.st_size:
        raise ValueError("El archivo cambió durante la lectura.")
    return data


def _pinned(raw, expected):
    if (
        not isinstance(raw, bytes)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or hashlib.sha256(raw).hexdigest() != expected
    ):
        raise ValueError("Hash externo distinto.")


def _json(raw):
    def unique(pairs):
        out = {}
        for name, value in pairs:
            if name in out:
                raise ValueError("JSON duplicado.")
            out[name] = value
        return out

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


@dataclass(frozen=True)
class AuthenticatedIndex:
    records: object
    sha256: str
    key_sha256: str


def authenticate_index(raw, public_key, *, index_sha256, key_sha256):
    _pinned(raw, index_sha256)
    _pinned(public_key, key_sha256)
    verified = _verifier().verified_control(raw, {"wolfi-signing.rsa.pub": public_key})
    if (
        verified["signature_scheme"] != "RSA-SHA256"
        or verified["payload"]
        or "APKINDEX" not in verified["files"]
    ):
        raise ValueError("Se exige índice Wolfi RSA256 completo sin bytes sobrantes.")
    records = {}
    for block in verified["files"]["APKINDEX"].decode("utf-8", errors="strict").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if len(line) < 2 or line[1] != ":":
                raise ValueError("Registro APK mal formado.")
            if line[0] in fields:
                raise ValueError("Registro APK ambiguo.")
            fields[line[0]] = line[2:]
        if not fields:
            continue
        if not {"P", "V", "A", "C", "S"} <= fields.keys():
            raise ValueError("Registro APK incompleto.")
        identity = fields["P"], fields["V"]
        if identity in records:
            raise ValueError("Versión APK repetida.")
        records[identity] = MappingProxyType(fields)
    return AuthenticatedIndex(MappingProxyType(records), index_sha256, key_sha256)


@dataclass(frozen=True)
class VerifiedApk:
    name: str
    version: str
    sha256: str
    payload: bytes
    scripts: tuple
    signature_mode: str = "index-rsa256-control-q1-payload-sha256"
    individual_signature: bool = False


def verify_wolfi_apk(raw, index, name, version, archive_sha256):
    if type(index) is not AuthenticatedIndex or (name, version) not in index.records:
        raise ValueError("El paquete no pertenece al índice autenticado.")
    _pinned(raw, archive_sha256)
    record = index.records[(name, version)]
    if len(raw) != int(record["S"]) or record["A"] not in ("x86_64", "noarch"):
        raise ValueError("Tamaño/arquitectura diferentes del índice.")
    verifier = _verifier()
    control_raw, control_tar, payload = verifier.gzip_part(raw)
    files = verifier.tar_files(control_tar)
    # Este wrapper es exclusivo del formato Wolfi sin SIGN. Alpine sigue congelado.
    if ".PKGINFO" not in files or any(name.startswith(".SIGN.") for name in files):
        raise ValueError("No es el formato Wolfi control+payload previsto.")
    identity = "Q1" + base64.b64encode(hashlib.sha1(control_raw).digest()).decode("ascii")
    if identity != record["C"]:
        raise ValueError("El control no coincide con el índice RSA256.")
    values = {}
    repeated = {"depend", "provides", "install_if", "triggers", "replaces"}
    for line in files[".PKGINFO"].decode("utf-8", errors="strict").splitlines():
        if not line or line.startswith("#"):
            continue
        if " = " not in line:
            raise ValueError("PKGINFO mal formado.")
        key, value = line.split(" = ", 1)
        if key in values and key not in repeated:
            raise ValueError("Identidad APK ambigua.")
        if key in repeated:
            values.setdefault(key, []).append(value)
        else:
            values[key] = value
    if (
        values.get("pkgname") != name
        or values.get("pkgver") != version
        or values.get("arch") not in (record["A"], "noarch")
        or not re.fullmatch(r"[a-f0-9]{64}", values.get("datahash", ""))
        or hashlib.sha256(payload).hexdigest() != values["datahash"]
    ):
        raise ValueError("Identidad o payload diferentes del control autenticado.")
    _compressed, plain, remaining = verifier.gzip_part(payload, 512 * 1024 * 1024)
    if remaining:
        raise ValueError("Bytes extra tras el payload APK.")
    return VerifiedApk(
        name,
        version,
        archive_sha256,
        plain,
        tuple(sorted(key for key in files if key != ".PKGINFO")),
    )


def payload_inventory(verified):
    if type(verified) is not VerifiedApk:
        raise ValueError("Se exige un payload autenticado.")
    result = []
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(verified.payload), mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or name in seen
                or len(result) >= 100000
            ):
                raise ValueError("Ruta de payload ambigua o fuera de raíz.")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("Tipo especial en payload.")
            seen.add(name)
            row = {
                "path": name,
                "size": member.size,
                "type": member.type.decode("ascii"),
                "link": member.linkname,
            }
            if member.isfile():
                if member.size > 256 * 1024 * 1024:
                    raise ValueError("Archivo de payload excesivo.")
                data = archive.extractfile(member).read(member.size + 1)
                if len(data) != member.size:
                    raise ValueError("Archivo de payload truncado.")
                row["sha256"] = hashlib.sha256(data).hexdigest()
            result.append(row)
    return result


def validate_cmake_cache(cache):
    for key, wanted in REQUIRED_CACHE.items():
        value = cache.get(key)
        if wanted in ("ON", "OFF"):
            normalized = (
                "ON"
                if str(value).upper() in ("ON", "1", "TRUE", "YES")
                else "OFF"
                if str(value).upper() in ("OFF", "0", "FALSE", "NO")
                else None
            )
            if normalized != wanted:
                raise ValueError("CMake no respeta una opción obligatoria: " + key)
        elif value != wanted:
            raise ValueError("CMake no respeta una identidad obligatoria: " + key)


def validate_configuration(cache, cvconfig, modules, openexr_directory):
    validate_cmake_cache(cache)
    if cache.get("OpenEXR_DIR") != openexr_directory or not openexr_directory.endswith(
        "/lib/cmake/OpenEXR"
    ):
        raise ValueError("OpenEXR externo no acreditado.")
    found = set(re.findall(r"^\s*#define\s+(HAVE_[A-Z0-9_]+)(?:\s+1)?\s*$", cvconfig, re.MULTILINE))
    if not {"HAVE_PNG", "HAVE_OPENEXR"} <= found or found & {
        "HAVE_FFMPEG",
        "HAVE_GSTREAMER",
        "HAVE_CUDA",
        "HAVE_OPENCL",
        "HAVE_IPP",
        "HAVE_ITT",
        "HAVE_TBB",
        "HAVE_QT",
        "HAVE_GTK",
    }:
        raise ValueError("Los codecs efectivos no respetan el contrato.")
    actual = set(
        re.findall(r"^\s*#define\s+HAVE_OPENCV_([A-Z0-9_]+)(?:\s+1)?\s*$", modules, re.MULTILINE)
    )
    if actual != {"CORE", "IMGPROC", "IMGCODECS"}:
        raise ValueError("El conjunto efectivo de módulos no es mínimo.")


def _relative(root, value):
    if type(value) is not str or not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Ruta de input no permitida.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Ruta de input fuera de raíz.")
    return Path(root).joinpath(*path.parts)


def verify_cached_inputs(manifest, inputs):
    if (
        manifest.get("schema_version") != 1
        or manifest.get("cv_version") != CV_VERSION
        or manifest.get("wheel_version") != WHEEL_VERSION
        or manifest.get("numpy_requires_dist") != NUMPY_REQUIRES
    ):
        raise ValueError("Identidad de inputs diferente.")
    if (
        manifest["source"]["sha256"]
        != "6a7508554941c1a698c243b2212b2985ce59a65a3e0348d53c2158607d801e61"
    ):
        raise ValueError("Fuente OpenCV no fijada.")
    index_info = manifest["wolfi_index"]
    if (
        index_info["sha256"] != "9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd"
        or index_info["public_signing_digest"]
        != "f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc"
    ):
        raise ValueError("Ancla Wolfi distinta.")
    index = authenticate_index(
        read_regular(_relative(inputs, index_info["path"]), 16 * 1024 * 1024),
        read_regular(_relative(inputs, index_info["key_path"]), 16384),
        index_sha256=index_info["sha256"],
        key_sha256=index_info["public_signing_digest"],
    )
    for row in (manifest["source"], manifest["numpy_headers"]):
        raw = read_regular(_relative(inputs, row["path"]), 160 * 1024 * 1024)
        _pinned(raw, row["sha256"])
    selected = {}
    for row in manifest["packages"] + manifest["direct_headers"]:
        identity = row["name"], row["version"]
        if identity in selected or identity not in index.records:
            raise ValueError("Input repetido o ausente del índice.")
        raw = read_regular(_relative(inputs, row["path"]), 256 * 1024 * 1024)
        checked = verify_wolfi_apk(raw, index, *identity, row["sha256"])
        selected[identity] = dict(index.records[identity])
        receipt_raw = read_regular(_relative(inputs, row["inventory_receipt"]), 16 * 1024 * 1024)
        _pinned(receipt_raw, row["inventory_receipt_sha256"])
        if row["role"] == "managed-build-sdk":
            if row["record"] != selected[identity] or re.search(
                r"(?:python-3\.13|py3\.13|py3-imath)", row["name"]
            ):
                raise ValueError("Identidad SDK o Python no permitidos.")
        else:
            inventory = {item["path"]: item for item in payload_inventory(checked)}
            for item in row["selection"]:
                if (
                    item != inventory.get(item["path"])
                    or item["type"] != "0"
                    or "PyImath" in item["path"]
                    or not item["path"].startswith(
                        (
                            "usr/include/Imath/",
                            "usr/include/OpenEXR/",
                            "usr/lib/cmake/Imath/",
                            "usr/lib/cmake/OpenEXR/",
                            "usr/lib/pkgconfig/",
                        )
                    )
                ):
                    raise ValueError("Selección de headers alterada.")
    base = manifest["base"]["packages"]
    for name, row in base.items():
        if dict(index.records.get((name, row["V"]), {})) != row:
            raise ValueError("Base diferente del índice autenticado.")
    managed = {row["name"]: selected[(row["name"], row["version"])] for row in manifest["packages"]}
    closure = _verifier().resolve_closure(managed, manifest["closure"]["seeds"], base)
    if closure["packages"] != managed:
        raise ValueError("La clausura declarada no coincide.")
    return {
        "managed_apks": len(managed),
        "direct_header_apks": len(manifest["direct_headers"]),
        "edges": len(closure["edges"]),
        "signature_mode": "index-rsa256-control-q1-payload-sha256",
        "sdk_executed": False,
        "native_solver_confirmation_pending": True,
    }


def elf_dynamic(raw):
    """Lee ELF64 como datos; no usa ldd ni carga el objeto compartido."""
    if len(raw) < 64 or raw[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Se exige ELF64 little-endian.")
    header = struct.unpack_from("<HHIQQQIHHHHHH", raw, 16)
    section_offset, section_size, section_count = header[5], header[10], header[11]
    if (
        header[:3] != (3, 62, 1)
        or section_size != 64
        or not 0 < section_count < 512
        or section_offset + section_count * 64 > len(raw)
    ):
        raise ValueError("ELF no acredita GNU amd64 compartido.")
    sections = [
        struct.unpack_from("<IIQQQQIIQQ", raw, section_offset + index * 64)
        for index in range(section_count)
    ]
    dynamic = [item for item in sections if item[1] == 6]
    if len(dynamic) != 1:
        raise ValueError("ELF sin sección dinámica única.")
    section = dynamic[0]
    if section[6] >= len(sections) or section[5] % 16 or section[4] + section[5] > len(raw):
        raise ValueError("Sección dinámica fuera de límites.")
    strings = sections[section[6]]
    if strings[1] != 3 or strings[4] + strings[5] > len(raw):
        raise ValueError("Tabla de strings ELF no válida.")
    table = raw[strings[4] : strings[4] + strings[5]]
    result = {"needed": [], "soname": None, "runpath": None}
    for offset in range(section[4], section[4] + section[5], 16):
        tag, value = struct.unpack_from("<qQ", raw, offset)
        if tag == 0:
            break
        if tag not in (1, 14, 15, 29):
            continue
        end = table.find(b"\x00", value)
        if value >= len(table) or end < 0:
            raise ValueError("String ELF fuera de límites.")
        text = table[value:end].decode("ascii", errors="strict")
        if tag == 1:
            result["needed"].append(text)
        else:
            key = "soname" if tag == 14 else "runpath"
            if result[key] is not None:
                raise ValueError("Identidad o RPATH ELF ambiguos.")
            result[key] = text
    allowed = re.compile(
        r"(?:libopencv_(?:core|imgproc|imgcodecs)\.so\.413|lib(?:c|m|dl|pthread|rt|gcc_s|stdc\+\+|atomic)\.so\.[0-9]+|libpng16\.so\.16|libz\.so\.1|lib(?:OpenEXR|OpenEXRCore|OpenEXRUtil|Iex|IlmThread)-3_5\.so\.34|libImath-3_2\.so\.30)"
    )
    if any(allowed.fullmatch(name) is None for name in result["needed"]):
        raise ValueError("El ELF depende de una biblioteca fuera del contrato.")
    paths = (result["runpath"] or "").split(":")
    if not paths or any(
        path not in ("$ORIGIN", "$ORIGIN/opencv_python_headless.libs") for path in paths
    ):
        raise ValueError("RPATH no reubicable o no acreditado.")
    return result


def runtime_files(staging):
    staging = Path(staging)
    extension_name = "cv2.cpython-312-x86_64-linux-gnu.so"
    extension = read_regular(staging / extension_name, 128 * 1024 * 1024)
    metadata = elf_dynamic(extension)
    expected = {
        "libopencv_" + component + ".so.413" for component in ("core", "imgproc", "imgcodecs")
    }
    if not expected <= set(
        metadata["needed"]
    ) or "$ORIGIN/opencv_python_headless.libs" not in metadata["runpath"].split(":"):
        raise ValueError("Extensión sin sus tres bibliotecas locales.")
    files = {extension_name: extension}
    for component in ("core", "imgproc", "imgcodecs"):
        raw = read_regular(staging / "lib" / f"libopencv_{component}.so.4.13.0", 128 * 1024 * 1024)
        data = elf_dynamic(raw)
        soname = f"libopencv_{component}.so.413"
        if data["soname"] != soname or "$ORIGIN" not in data["runpath"].split(":"):
            raise ValueError("Biblioteca OpenCV de otra identidad.")
        if component == "imgcodecs" and not {"libpng16.so.16", "libOpenEXR-3_5.so.34"} <= set(
            data["needed"]
        ):
            raise ValueError("PNG/OpenEXR deben estar enlazados a codecs externos.")
        # Se guardan archivos regulares bajo SONAME; no enlaces del builder en el ZIP.
        files["opencv_python_headless.libs/" + soname] = raw
    return files


def write_wheel(staging, output, license_path, manifest):
    if manifest.get("numpy_requires_dist") != NUMPY_REQUIRES:
        raise ValueError("No se conservan los requisitos NumPy de upstream.")
    staging, output = Path(staging), Path(output)
    files = runtime_files(staging)
    if not files or len(files) > 2000 or sum(map(len, files.values())) > 256 * 1024 * 1024:
        raise ValueError("Wheel vacío o excesivo.")
    metadata = [
        "Metadata-Version: 2.4",
        "Name: opencv-python-headless",
        "Version: " + WHEEL_VERSION,
        "Requires-Python: >=3.12,<3.13",
        "License-Expression: Apache-2.0",
        "License-File: licenses/LICENSE",
        "Summary: Compilación mínima propia de OpenCV, no wheel oficial PyPI",
    ]
    numpy_license = base64.b64decode(manifest["numpy_license"]["base64"], validate=True)
    _pinned(numpy_license, manifest["numpy_license"]["sha256"])
    metadata[4] = "License-Expression: Apache-2.0 AND BSD-3-Clause"
    metadata.append("License-File: licenses/NUMPY-LICENSE")
    metadata += ["Requires-Dist: " + item for item in NUMPY_REQUIRES]
    files[DIST_INFO + "/METADATA"] = ("\n".join(metadata) + "\n\n").encode()
    files[DIST_INFO + "/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: agencia-opencv-minimal-v1\nRoot-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n"
    )
    files[DIST_INFO + "/licenses/LICENSE"] = read_regular(license_path, 65536)
    files[DIST_INFO + "/licenses/NUMPY-LICENSE"] = numpy_license
    files[DIST_INFO + "/AGENCY-BUILD.json"] = (
        json.dumps(
            {
                "own_build": True,
                "cv2_version": CV_VERSION,
                "distribution_version": WHEEL_VERSION,
                "source_sha256": manifest["source"]["sha256"],
                "runtime_verified_by_packager": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for name, data in sorted(files.items()):
        writer.writerow(
            (
                name,
                "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="),
                len(data),
            )
        )
    writer.writerow((DIST_INFO + "/RECORD", "", ""))
    files[DIST_INFO + "/RECORD"] = records.getvalue().encode()
    target = output / WHEEL_NAME
    try:
        descriptor = os.open(
            target, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except FileExistsError:
        raise ValueError("No se sobrescribe un wheel existente.") from None
    with os.fdopen(descriptor, "w+b") as stream:
        with zipfile.ZipFile(
            stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        stream.flush()
        os.fsync(stream.fileno())
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--inputs-root", type=Path)
    parser.add_argument("--verify-inputs", action="store_true")
    parser.add_argument("--cvconfig", type=Path)
    parser.add_argument("--modules", type=Path)
    parser.add_argument("--openexr-directory")
    parser.add_argument("--package", action="store_true")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--license", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest_raw = read_regular(args.manifest, 2 * 1024 * 1024)
        if args.manifest_sha256:
            _pinned(manifest_raw, args.manifest_sha256)
        manifest = _json(manifest_raw)
        if (
            manifest.get("schema_version") != 1
            or manifest.get("cv_version") != CV_VERSION
            or manifest.get("wheel_version") != WHEEL_VERSION
        ):
            raise ValueError("Manifiesto de otra identidad.")
        if args.cache:
            cache = {}
            for line in read_regular(args.cache, 2 * 1024 * 1024).decode().splitlines():
                if line and not line.startswith(("#", "//")) and "=" in line:
                    key, value = line.split("=", 1)
                    name = key.split(":", 1)[0]
                    if name in cache:
                        raise ValueError("Cache CMake duplicada.")
                    cache[name] = value
            validate_cmake_cache(cache)
            if not args.cvconfig or not args.modules or not args.openexr_directory:
                raise ValueError("Faltan pruebas de detección efectiva.")
            validate_configuration(
                cache,
                read_regular(args.cvconfig, 65536).decode(),
                read_regular(args.modules, 65536).decode(),
                args.openexr_directory,
            )
        if args.verify_inputs:
            if not args.manifest_sha256 or not args.inputs_root:
                raise ValueError("Faltan pins externos de inputs.")
            print(json.dumps(verify_cached_inputs(manifest, args.inputs_root)))
            return 0
        if args.package:
            if (
                manifest.get("build_enabled") is not True
                or not args.manifest_sha256
                or not args.cache
                or not args.staging
                or not args.output
                or not args.license
            ):
                raise ValueError("Empaquetado no habilitado o sin evidencia.")
            target = write_wheel(args.staging, args.output, args.license, manifest)
            print(
                json.dumps(
                    {
                        "wheel": str(target),
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "sdk_executed_by_packager": False,
                        "runtime_verified_by_packager": False,
                    }
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "plan_only": True,
                    "build_enabled": manifest.get("build_enabled") is True,
                    "cv_version": CV_VERSION,
                    "wheel_version": WHEEL_VERSION,
                    "sdk_executed": False,
                }
            )
        )
        return 0
    except (OSError, ValueError, KeyError, tarfile.TarError):
        print(json.dumps({"ok": False, "error": "OPENCV_PLAN_RECHAZADO"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
