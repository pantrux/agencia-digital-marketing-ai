"""Prefijo alternativo como TAR de datos; no ejecuta CMake, SDK, Docker ni SSH."""

import argparse
import ast
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import sys
import tarfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC_PATH = HERE / "openexr3-inputs.json"
OWNER = "agencia-digital-marketing-ai:opencv-openexr3-inputs:v1"
MAX_BYTES = 256 * 1024**2
PACKAGER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
RECOVERY_SHA = "09e28a43b7ba8911b08146f1fd06e7a0ddaa597de1630d354abc8f79d0c0efbd"
ELF_READER_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def require(value, message):
    if not value:
        raise ValueError(message)


def json_object(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "JSON duplicado.")
            value[key] = item
        return value

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def read_regular(path, maximum=MAX_BYTES, expected=None):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "Ruta enlazada o con reparse point.",
        )
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
        "Archivo irregular o excesivo.",
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())

    def signature(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink, stat.S_IFMT(s.st_mode))

    require(
        len(raw) == info.st_size
        and all(signature(s) == signature(info) for s in (before, after, path.lstat())),
        "Archivo cambió durante lectura.",
    )
    require(expected is None or digest(raw) == expected, "SHA físico diferente del pin.")
    return raw


def module_bytes(name, path, checksum):
    raw = read_regular(path, 256 * 1024, checksum)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def safe_name(name):
    require(
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9_.+/-]+", name)
        and not name.startswith("/")
        and all(p not in {"", ".", ".."} for p in name.split("/")),
        "Ruta de selección no canónica.",
    )
    return name


def validate_projection(files, links, expected):
    require(
        set(files).isdisjoint(links) and set(files) | set(links) == set(expected),
        "La proyección no coincide con la whitelist completa.",
    )
    require(
        len(expected) <= 512 and sum(len(raw) for raw in files.values()) <= MAX_BYTES,
        "Proyección excesiva.",
    )
    for name, raw in files.items():
        safe_name(name)
        row = expected[name]
        require(
            row["kind"] == "file" and row["size"] == len(raw) and row["sha256"] == digest(raw),
            "Bytes de proyección distintos.",
        )
    for name, target in links.items():
        safe_name(name)
        require(
            target not in {"", ".", ".."}
            and "/" not in target
            and "\\" not in target
            and expected[name] == {"kind": "symlink", "target": target},
            "Enlace no local o diferente de su selección.",
        )
        destination = posixpath.join(posixpath.dirname(name), target)
        visited = {name}
        while destination in links:
            require(destination not in visited, "Ciclo de enlaces.")
            visited.add(destination)
            destination = posixpath.join(posixpath.dirname(destination), links[destination])
        require(destination in files, "Enlace sin archivo seleccionado.")


def check_build_graph(commands, ninja):
    require(isinstance(commands, list) and commands, "No hay unidades de compilación observadas.")
    for row in commands:
        source = row.get("file", "")
        require(
            isinstance(source, str)
            and source.startswith("/")
            and "/3rdparty/openexr/" not in source.replace("\\", "/"),
            "La configuración contiene fallback OpenEXR interno.",
        )
    for line in ninja.replace("$\r\n", "").replace("$\n", "").splitlines():
        if re.match(r"\s*(LINK_LIBRARIES|LINK_FLAGS)\s*=", line):
            require(
                "libdeflate.a" not in line,
                "libdeflate.a sólo es dato de build; no puede entrar al enlace.",
            )


def validate_targets(report, expected, prefix):
    require(
        prefix.startswith("/") and ".." not in prefix.split("/") and not prefix.endswith("/"),
        "Prefijo de targets no canónico.",
    )
    require(
        report.get("OpenEXR_VERSION") == "3.5.3" and report.get("Threads_FOUND") is True,
        "El probe no acredita OpenEXR3.5.3 y Threads.",
    )
    require(
        report.get("targets") == {name: prefix + "/" + path for name, path in expected.items()},
        "Los imported targets no corresponden a los archivos físicos fijados.",
    )


def validate_needed(names):
    allowed = {
        "libOpenEXR-3_5.so.34",
        "libOpenEXRCore-3_5.so.34",
        "libOpenEXRUtil-3_5.so.34",
        "libIex-3_5.so.34",
        "libIlmThread-3_5.so.34",
        "libImath-3_2.so.30",
        "libdeflate.so.0",
        "libopenjph.so.0.32",
        "libc.so.6",
        "libm.so.6",
        "libdl.so.2",
        "libpthread.so.0",
        "librt.so.1",
        "libstdc++.so.6",
        "libgcc_s.so.1",
        "libatomic.so.1",
        "ld-linux-x86-64.so.2",
    }
    require(
        "libOpenEXR-3_5.so.34" in names and len(names) == len(set(names)) and set(names) <= allowed,
        "El ELF no acredita enlace dinámico con OpenEXR3.5.",
    )


def probe_sources(targets):
    rows = [f'"{name}":"$<TARGET_FILE:{name}>"' for name in sorted(targets)]
    checks = "\n".join(
        f'if(NOT TARGET {name})\n  message(FATAL_ERROR "Falta target {name}")\nendif()'
        for name in sorted(targets)
    )
    cmake = """cmake_minimum_required(VERSION 3.31)
project(agency_openexr3_probe LANGUAGES C CXX)
set(CMAKE_BUILD_WITH_INSTALL_RPATH ON)
set(CMAKE_INSTALL_RPATH "$ORIGIN")
set(CMAKE_FIND_USE_PACKAGE_REGISTRY OFF)
set(CMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY OFF)
find_package(OpenEXR 3.5.3 EXACT CONFIG REQUIRED)
if(NOT TARGET Threads::Threads)
  message(FATAL_ERROR "Falta Threads::Threads")
endif()
CHECKS
add_library(agency-exr-probe SHARED probe.cc)
set_target_properties(agency-exr-probe PROPERTIES PREFIX "")
target_link_libraries(agency-exr-probe PRIVATE OpenEXR::OpenEXR)
file(GENERATE OUTPUT "${CMAKE_BINARY_DIR}/targets.json" CONTENT "JSON")
""".replace("CHECKS", checks)
    content = (
        '{"OpenEXR_VERSION":"${OpenEXR_VERSION}","Threads_FOUND":true,"targets":{'
        + ",".join(rows)
        + "}}"
    )
    cmake = cmake.replace("JSON", content.replace('"', '\\"'))
    source = '#include <OpenEXR/ImfRgbaFile.h>\nextern "C" int agency_openexr_probe(const char* path) { Imf::RgbaInputFile input(path); return input.displayWindow().max.x; }\n'
    return cmake, source


def runtime_guard_source():
    source = read_regular(Path(__file__), 128 * 1024)
    tree = ast.parse(source)
    wanted = {
        "digest",
        "canonical",
        "require",
        "json_object",
        "read_regular",
        "safe_name",
        "validate_projection",
        "check_build_graph",
        "validate_targets",
        "validate_needed",
    }
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    require({n.name for n in nodes} == wanted, "Selección de guard incompleta.")
    return (
        "import argparse,hashlib,json,os,posixpath,re,stat,sys,types\nfrom pathlib import Path\nMAX_BYTES=256*1024**2\n"
        + "\n\n".join(ast.get_source_segment(source.decode(), n) for n in nodes)
        + GUARD_MAIN
    ).encode()


def elf_reader_source():
    path = ROOT / "docker/postgres-immich/final_image_remote.py"
    raw = read_regular(path, 128 * 1024, ELF_READER_SHA)
    tree = ast.parse(raw)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"part", "elf_info"}
    ]
    require(
        {node.name for node in functions} == {"part", "elf_info"},
        "El lector ELF congelado no conserva las funciones requeridas.",
    )
    return (
        "import struct\nMAX_FILE=64*1024**2\n"
        + "\n\n".join(ast.get_source_segment(raw.decode(), node) for node in functions)
    ).encode()


GUARD_MAIN = """
def main():
 p=argparse.ArgumentParser()
 p.add_argument('--prefix',required=True);p.add_argument('--expected-spec-sha',required=True)
 p.add_argument('--targets',required=True);p.add_argument('--probe',required=True);p.add_argument('--ninja',required=True)
 p.add_argument('--compile-commands',required=True)
 a=p.parse_args()
 require(os.name=='posix' and os.statvfs(a.prefix).f_flag & os.ST_RDONLY,'El prefijo alternativo debe estar montado RO.')
 spec=json_object(read_regular(Path(a.prefix)/'agency/input-spec.json',262144,a.expected_spec_sha))
 files={};links={}
 for name,row in spec['entries'].items():
  path=Path(a.prefix).joinpath(*name.split('/'));safe_name(name)
  if row['kind']=='file':files[name]=read_regular(path,67108864,row['sha256'])
  else:
   require(path.is_symlink(),'Falta enlace de prefijo');links[name]=os.readlink(path)
 validate_projection(files,links,spec['entries'])
 validate_targets(json_object(read_regular(a.targets,16384)),spec['targets'],a.prefix)
 check_build_graph(json_object(read_regular(a.compile_commands,16777216)),read_regular(a.ninja,16777216).decode())
 reader=Path(a.prefix)/'agency/elf_reader.py'
 raw=read_regular(reader,32768,spec['elf_reader_sha256'])
 module=types.ModuleType('probe_elf_frozen');module.__file__=str(reader);sys.modules[module.__name__]=module
 exec(compile(raw,str(reader),'exec'),module.__dict__)
 elf=module.elf_info(read_regular(a.probe,67108864));validate_needed(elf['needed'])
 print(json.dumps({'verified':True,'OpenEXR_VERSION':'3.5.3','external_link':True,'static_linked':False,'probe_executed':False,'main_build_accepted':False}))
if __name__=='__main__':main()
"""


def derive_recipe(guard_sha, spec_sha):
    if isinstance(guard_sha, bytes):
        guard_sha = guard_sha.decode()
    require(
        re.fullmatch(r"[a-f0-9]{64}", guard_sha) and re.fullmatch(r"[a-f0-9]{64}", spec_sha),
        "Pins de guard/spec inválidos.",
    )
    source = HERE / "opencv_build_recovery_remote.py"
    recovery = module_bytes("openexr3_recipe_frozen", source, RECOVERY_SHA)
    raw = recovery.derive_recipe(
        read_regular(HERE / "build-opencv-minimal.sh", 16384, recovery.RECIPE_SHA)
    )
    raw = raw.replace(
        b"flags=(\n",
        b"flags=(\n  -DCMAKE_REQUIRE_FIND_PACKAGE_OpenEXR=ON -DCMAKE_EXPORT_COMPILE_COMMANDS=ON\n",
        1,
    )
    before = b"# El contenedor del controlador debe tener network=none y entradas montadas RO.\n"
    probe = """# Probe de compilación/enlace previo; no se ejecuta su binario.
"${sandbox_env[@]}" /usr/bin/cmake -S "$overlay/agency/probe" -B "$work/openexr-probe" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++ -DCMAKE_MAKE_PROGRAM=/usr/lib/ninja-build/bin/ninja "-DCMAKE_PREFIX_PATH=$overlay/usr" "-DOpenEXR_DIR=$overlay/usr/lib/cmake/OpenEXR" "-DImath_DIR=$overlay/usr/lib/cmake/Imath" "-Dlibdeflate_DIR=$overlay/usr/lib/cmake/libdeflate" "-Dopenjph_DIR=$overlay/usr/lib/cmake/openjph"
"${sandbox_env[@]}" /usr/bin/cmake --build "$work/openexr-probe" --parallel 2
GUARD --ninja "$work/openexr-probe/build.ninja" --compile-commands "$work/openexr-probe/compile_commands.json"
"""
    guard = (
        '"$controller_python" "$overlay/agency/guard.py" --prefix "$overlay" --expected-spec-sha '
        + spec_sha
        + ' --targets "$work/openexr-probe/targets.json" --probe "$work/openexr-probe/agency-exr-probe.so"'
    )
    verification = (
        '"$controller_python" - "$overlay/agency/guard.py" <<\'GUARD_SHA\'\nimport hashlib,sys\nfrom pathlib import Path\nwith Path(sys.argv[1]).open("rb") as stream:raw=stream.read(131073)\nif len(raw)>131072 or hashlib.sha256(raw).hexdigest()!='
        + repr(guard_sha)
        + ':raise SystemExit("Guard diferente del pin externo")\nGUARD_SHA\n'
    )
    require(raw.count(before) == 1, "Punto de inserción de probe no único.")
    raw = raw.replace(
        before, verification.encode() + probe.replace("GUARD", guard).encode() + before
    )
    build = b'"${sandbox_env[@]}" /usr/bin/cmake --build "$work/build" --parallel 2\n'
    require(raw.count(build) == 1, "Punto de guard principal no único.")
    raw = raw.replace(
        build,
        (
            guard
            + ' --ninja "$work/build/build.ninja" --compile-commands "$work/build/compile_commands.json"\n'
        ).encode()
        + build,
    )
    return raw


def expected_entries(spec):
    return {
        name: {k: v for k, v in row.items() if k not in {"source", "member"}}
        for name, row in spec["entries"].items()
    }


def validate_sca_scope(value, sources):
    names = {"imath-dev", "openexr-dev", "libdeflate-dev", "openjph-dev", "libdeflate-static"}
    partials = value.get("partial_apk_inputs", [])
    require(
        value.get("installed_sdk_apk_count") == 89
        and value.get("expected_apk_identities") == 94
        and value.get("expected_total_components") == 95
        and value.get("numpy", {}).get("name") == "numpy"
        and value.get("numpy", {}).get("version") == "2.4.6"
        and value.get("sdk_packages_changed_by_preparer") is False
        and value.get("new_inputs_sca_verified") is False
        and value.get("historical_2ce_receipt_covers_new_inputs") is False
        and value.get("required_before_cmake_probe") is True
        and len(partials) == 5
        and {row.get("name") for row in partials} == names,
        "La cobertura debe conservar 89 instalados, cinco parciales y NumPy; aún sin SCA nueva.",
    )
    for row in partials:
        source = sources[row["name"]]
        require(
            row.get("installed") is False
            and row.get("version") == source["version"]
            and row.get("archive_sha256") == source["sha256"]
            and row.get("scope")
            == (
                "static-build-data-only"
                if row["name"] == "libdeflate-static"
                else "partial-headers-and-metadata"
            ),
            "Una entrada parcial no conserva su identidad o alcance.",
        )


def read_spec():
    raw = read_regular(SPEC_PATH, 256 * 1024)
    spec = json_object(raw)
    require(
        spec["owner"] == OWNER
        and spec["schema_version"] == 1
        and spec["platform"] == "linux/amd64"
        and spec["openexr_version"] == "3.5.3",
        "Identidad de datos alternativa distinta.",
    )
    require(
        spec["static_build_data_only"] == ["usr/lib/libdeflate.a"] and len(spec["targets"]) == 8,
        "Clausura de targets incompleta.",
    )
    validate_sca_scope(spec["required_sca_scope"], spec["sources"])
    return raw, spec


def generated(spec):
    elf_reader = elf_reader_source()
    input_spec = {
        "entries": expected_entries(spec),
        "targets": spec["targets"],
        "OpenEXR_VERSION": "3.5.3",
        "static_build_data_only": spec["static_build_data_only"],
        "elf_reader_sha256": digest(elf_reader),
    }
    raw_spec = canonical(input_spec)
    cmake, cpp = probe_sources(spec["targets"])
    guard = runtime_guard_source()
    return {
        "agency/input-spec.json": raw_spec,
        "agency/probe/CMakeLists.txt": cmake.encode(),
        "agency/probe/probe.cc": cpp.encode(),
        "agency/guard.py": guard,
        "agency/elf_reader.py": elf_reader,
        "agency/recipe.sh": derive_recipe(digest(guard), digest(raw_spec)),
    }


def reference_limit(name):
    return 8 * 1024**2 if name == "sdk_capture" else 256 * 1024


def plan():
    raw, spec = read_spec()
    for name, row in spec["refs"].items():
        read_regular(row["path"], reference_limit(name), row["sha256"])
    assets = generated(spec)
    return {
        "schema_version": 1,
        "owner": OWNER,
        "spec_path": str(SPEC_PATH),
        "spec_sha256": digest(raw),
        "source_sha256": digest(read_regular(Path(__file__), 128 * 1024)),
        "output_root": spec["output_root"],
        "runtime_work_proposed": spec["runtime_work_proposed"],
        "source_packages": {
            name: {"version": row["version"], "sha256": row["sha256"], "license": row["license"]}
            for name, row in spec["sources"].items()
        },
        "projection_entries": len(spec["entries"]),
        "projection_bytes": sum(row.get("size", 0) for row in spec["entries"].values()),
        "generated": {
            name: {"sha256": digest(body), "bytes": len(body)} for name, body in assets.items()
        },
        "static_build_data_only": spec["static_build_data_only"],
        "required_sca_scope": spec["required_sca_scope"],
        "new_cid_or_mount_contract_required": True,
        "sdk_executed": False,
        "probe_executed": False,
        "runtime_applied": False,
        "wheel_accepted": False,
    }


def authenticated_projection(spec):
    packager = module_bytes("openexr3_packager", spec["refs"]["packager"]["path"], PACKAGER_SHA)
    index = spec["index"]
    verified_index = packager.authenticate_index(
        read_regular(index["path"], 16 * 1024**2, index["sha256"]),
        read_regular(index["key_path"], 16384, index["public_signing_digest"]),
        index_sha256=index["sha256"],
        key_sha256=index["public_signing_digest"],
    )
    files, links = {}, {}
    for name, row in spec["sources"].items():
        require(
            dict(verified_index.records[(name, row["version"])]) == row["record"]
            and row["license"] == row["record"]["L"],
            "Identidad/licencia diferente del índice firmado.",
        )
        raw = read_regular(row["path"], MAX_BYTES, row["sha256"])
        require(len(raw) == row["bytes"], "Tamaño de APK diferente.")
        verified = packager.verify_wolfi_apk(
            raw, verified_index, name, row["version"], row["sha256"]
        )
        wanted = {
            entry["member"]: (target, entry)
            for target, entry in spec["entries"].items()
            if entry["source"] == name
        }
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(verified.payload), mode="r:") as archive:
            for member in archive:
                if member.name not in wanted:
                    continue
                require(member.name not in seen, "Miembro seleccionado duplicado.")
                seen.add(member.name)
                target, entry = wanted[member.name]
                if member.isfile() and entry["kind"] == "file":
                    body = archive.extractfile(member).read(64 * 1024**2 + 1)
                    require(len(body) == member.size, "Miembro truncado.")
                    files[target] = body
                elif member.issym() and entry["kind"] == "symlink":
                    links[target] = member.linkname
                else:
                    raise ValueError("Tipo seleccionado distinto.")
        require(seen == set(wanted), "APK sin todos los miembros seleccionados.")
    validate_projection(files, links, expected_entries(spec))
    return files, links


def write_projection(target, files, links, expected):
    target = Path(target).absolute()
    require(target.parent.resolve(strict=True) == target.parent, "Padre de salida enlazado.")
    validate_projection(files, links, expected)
    target.mkdir(mode=0o700)
    marker = canonical(
        {
            "owner": OWNER,
            "directory": {"device": target.stat().st_dev, "inode": target.stat().st_ino},
        }
    )
    with (target / ".owner.json").open("xb") as stream:
        stream.write(marker)
    archive_path = target / "overlay.tar"
    with archive_path.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w:", format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(set(files) | set(links)):
                member = tarfile.TarInfo(name)
                member.uid = member.gid = member.mtime = 0
                if name in files:
                    member.size = len(files[name])
                    member.mode = 0o444
                    archive.addfile(member, io.BytesIO(files[name]))
                else:
                    member.type = tarfile.SYMTYPE
                    member.linkname = links[name]
                    member.mode = 0o777
                    archive.addfile(member)
        stream.flush()
        os.fsync(stream.fileno())
    return archive_path


def prepare(proposal, target):
    require(
        proposal == plan() and Path(target).absolute() == Path(proposal["output_root"]),
        "Plan o salida no fijados.",
    )
    _raw, spec = read_spec()
    files, links = authenticated_projection(spec)
    extras = generated(spec)
    require(
        set(extras).isdisjoint(files) and set(extras).isdisjoint(links),
        "Datos generados colisionan con upstream.",
    )
    files.update(extras)
    expected = {
        **expected_entries(spec),
        **{
            name: {"kind": "file", "size": len(body), "sha256": digest(body)}
            for name, body in extras.items()
        },
    }
    archive = write_projection(target, files, links, expected)
    raw = read_regular(archive, MAX_BYTES)
    receipt = {
        "owner": OWNER,
        "plan_sha256": digest(canonical(proposal)),
        "archive_sha256": digest(raw),
        "archive_bytes": len(raw),
        "entries": len(expected),
        "source_packages_authenticated": len(spec["sources"]),
        "index_signature_scheme": "RSA-SHA256",
        "installed_apk_count_claimed": 0,
        "partial_build_inputs": True,
        "sdk_executed": False,
        "probe_executed": False,
        "runtime_applied": False,
        "static_build_data_only": spec["static_build_data_only"],
    }
    with (Path(target) / "receipt.json").open("xb") as stream:
        stream.write(canonical(receipt))
        stream.flush()
        os.fsync(stream.fileno())
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("plan", "prepare"), default="plan")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--root-authorized", action="store_true")
    args = parser.parse_args(argv)
    try:
        proposal = plan()
        if args.mode == "plan":
            print(
                json.dumps(
                    {
                        "plan_only": True,
                        "plan_sha256": digest(canonical(proposal)),
                        "plan": proposal,
                    }
                )
            )
            return 0
        require(
            args.root_authorized
            and args.output
            and args.expected_plan_sha256 == digest(canonical(proposal)),
            "Prepare requiere salida/pin revisados y autorización explícita de Root.",
        )
        print(json.dumps({"success": True, "receipt": prepare(proposal, args.output)}))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "detail": str(error)[:240],
                    "partial_data_preserved": True,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
