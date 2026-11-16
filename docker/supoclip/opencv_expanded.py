"""Valida la configuración externa y empaqueta OpenCV ampliado para SupoClip."""

import argparse
import ast
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
from pathlib import Path

MODULES = (
    "core",
    "imgproc",
    "imgcodecs",
    "videoio",
    "video",
    "calib3d",
    "features2d",
    "flann",
    "objdetect",
    "dnn",
    "python3",
)
CODECS = ("JPEG", "PNG", "OPENEXR")
REQUIRED = {
    "BUILD_LIST": ",".join(MODULES),
    "WITH_FFMPEG": "ON",
    "__VALID_FFMPEG": "TRUE",
    "FFMPEG_libavcodec_VERSION": "61.19.101",
    "FFMPEG_libavformat_VERSION": "61.7.103",
    "FFMPEG_libavutil_VERSION": "59.39.100",
    "FFMPEG_libswscale_VERSION": "8.3.100",
    "BUILD_PROTOBUF": "OFF",
    "PROTOBUF_UPDATE_FILES": "ON",
    "CMAKE_CXX_STANDARD": "17",
    "Protobuf_DIR": "/usr/lib/cmake/protobuf",
    "OpenEXR_DIR": "/sdk-data/openexr3/usr/lib/cmake/OpenEXR",
    "Imath_DIR": "/sdk-data/openexr3/usr/lib/cmake/Imath",
    "PYTHON3_EXECUTABLE": "/usr/bin/python3.12",
    "PYTHON3_NUMPY_VERSION": "2.4.4",
    "PYTHON3_NUMPY_INCLUDE_DIRS": "/sdk-data/numpy",
    "OPENCV_PYTHON3_INSTALL_PATH": "python",
}
SOURCE_SHA = "6a7508554941c1a698c243b2212b2985ce59a65a3e0348d53c2158607d801e61"
NUMPY_SHA = "81f4a14bee47aec54f883e0cad2d73986640c1590eb9bfaaba7ad17394481e6e"
LOADER_SHA = "936bd94c5a5debf0212fc751af79d3a163652f3e850259df2159db6aa3ed8ad8"
ZOU_HELPER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
RECORD_HELPER_SHA = "8cb78d9231f9619f31717e733a355899702fa312d8a703f5b1dece8998e28d1a"
ELF_HELPER_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
EXTRACTOR_SHA = "e5a69391db0ea7f4f2c27afa60b20ab31baaf41016d4a25492b1fa8e33393291"
WHEEL_VERSION = "4.13.0.92+agency1"
DIST = f"opencv_python-{WHEEL_VERSION}.dist-info"
EXTENSION = "cv2/python-3.12/cv2.cpython-312-x86_64-linux-gnu.so"
FFMPEG = {"libavcodec.so.61", "libavformat.so.61", "libavutil.so.59", "libswscale.so.8"}
HAAR = {
    "haarcascade_frontalface_default.xml": "0f7d4527844eb514d4a4948e822da90fbb16a34a0bbbbc6adc6498747a5aafb0",
    "haarcascade_profileface.xml": "b39a4a3be45539db146a7fc1d3e761a292c196eb88421185e6a615b3055e612d",
}
SDK = Path("/sdk-data")
INPUTS = Path("/inputs")
WORK = Path("/work")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def source(path, pin):
    with path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    require(len(raw) <= 256 * 1024 and digest(raw) == pin, "HELPER_SHA256")
    return raw


def helper(path, pin, names):
    tree = ast.parse(source(path, pin))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    require({node.name for node in nodes} == set(names) and len(nodes) == len(names), "HELPER_AST")
    namespace = {
        "os": os,
        "stat": stat,
        "Path": Path,
        "hashlib": hashlib,
        "csv": csv,
        "io": io,
        "base64": base64,
        "struct": struct,
        "require": require,
    }
    if "elf_info" in names:
        limits = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "MAX_FILE" for target in node.targets
            )
        ]
        require(len(limits) == 1, "HELPER_ELF_LIMIT")
        nodes = [*limits, *nodes]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def check_config(cache_text, cvconfig, modules, ninja, log):
    cache = {}
    for line in cache_text.splitlines():
        if not line or line.startswith(("//", "#")) or "=" not in line:
            continue
        field, value = line.split("=", 1)
        field = field.split(":", 1)[0]
        require(field not in cache, "CMAKE_CACHE_DUPLICATE")
        cache[field] = value
    for field, expected in REQUIRED.items():
        require(cache.get(field) == expected, "CMAKE_CACHE_" + field)
    macros = set(re.findall(r"(?m)^#define HAVE_([A-Z0-9_]+)(?:\s+1)?[ \t]*$", cvconfig))
    require(
        set(CODECS) <= macros and not macros & {"CUDA", "OPENCL", "GSTREAMER", "IPP"},
        "CMAKE_CODECS",
    )
    present = set(re.findall(r"(?m)^#define HAVE_OPENCV_([A-Z0-9_]+)(?:\s+1)?[ \t]*$", modules))
    require(
        {name.upper() for name in MODULES if name != "python3"} <= present
        and "HIGHGUI" not in present,
        "CMAKE_MODULES",
    )
    require(EXTENSION.rsplit("/", 1)[-1] in ninja, "CMAKE_PYTHON_EXTENSION")
    for codec in ("FFMPEG", "WEBP"):
        require(
            re.search(rf"(?m)^\s*DEFINES\s*=.*-DHAVE_{codec}(?:=1)?(?:\s|$)", ninja),
            "CMAKE_TARGET_" + codec,
        )
    require(
        re.search(
            r"(?m)^\s*(?:--\s*)?Protobuf:\s+/usr/lib/libprotobuf\.so\.29\.5\.0\s+\(29\.5\.0\)[ \t]*$",
            log,
        ),
        "CMAKE_PROTOBUF_PROVIDER",
    )
    require(
        re.search(
            r"(?m)^\s*COMMAND\s*=.*?/usr/bin/protoc(?:-29\.5\.0)?\s.*?opencv-onnx\.proto\b", ninja
        ),
        "CMAKE_ONNX_REGENERATION",
    )


def configuration(reader):
    paths = (
        "build/CMakeCache.txt",
        "build/opencv2/cvconfig.h",
        "build/opencv2/opencv_modules.hpp",
        "build/build.ninja",
        "configure.log",
    )
    files = {name: reader(WORK / name, 32 * 1024**2) for name in paths}
    check_config(*(files[name].decode("utf-8") for name in paths))
    return {
        "success": True,
        "configured": True,
        "wheel_built": False,
        "artifacts": {name: digest(raw) for name, raw in files.items()},
    }


def relocate_loader(config, version_config):
    before = b"os.path.join(LOADER_DIR, 'lib')"
    require(config.count(before) == 1 and b"/work" not in config, "LOADER_BINARY_PATH")
    require(
        version_config.count(b"os.path.join(LOADER_DIR, 'python-3.12')") == 1,
        "LOADER_EXTENSION_PATH",
    )
    require(b"/work" not in version_config, "LOADER_ABSOLUTE_PATH")
    return config.replace(before, b"os.path.join(LOADER_DIR, '.libs')")


def make_record(files):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, raw in sorted(files.items()):
        pin = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        writer.writerow((name, "sha256=" + pin, len(raw)))
    writer.writerow((DIST + "/RECORD", "", ""))
    return output.getvalue().encode()


def elf_gate(raw, elf_info, expected_soname, allowed):
    require(raw[16:18] == b"\x03\x00", "WHEEL_ELF_NOT_SHARED")
    info = elf_info(raw)
    require(info["soname"] == expected_soname, "WHEEL_ELF_SONAME")
    require(bool(info["rpath"]) != bool(info["runpath"]), "WHEEL_ELF_SEARCH_PATH")
    rpaths = set((info["rpath"] or info["runpath"]).split(":"))
    require(rpaths == {"$ORIGIN", "$ORIGIN/../.libs"}, "WHEEL_ELF_UNRELOCATABLE")
    needed = set(info["needed"])
    require(
        needed and len(needed) == len(info["needed"]) and needed <= allowed, "WHEEL_ELF_PROVIDER"
    )
    require(
        not any(
            name.startswith(("libQt", "libGL", "libvulkan", "libgtk", "libcuda")) for name in needed
        ),
        "WHEEL_ELF_GUI_GPU",
    )
    return needed


def wheel_files(reader, elf_info):
    stage = WORK / "stage"
    loader = stage / "python/cv2"
    names = (
        "__init__.py",
        "config.py",
        "config-3.12.py",
        "load_config_py2.py",
        "load_config_py3.py",
    )
    files = {"cv2/" + name: reader(loader / name, 64 * 1024) for name in names}
    require(digest(files["cv2/__init__.py"]) == LOADER_SHA, "LOADER_SOURCE")
    files["cv2/config.py"] = relocate_loader(files["cv2/config.py"], files["cv2/config-3.12.py"])
    components = {name: f"libopencv_{name}.so.413" for name in MODULES if name != "python3"}
    allowed = set(reader(INPUTS / "runtime-sonames.txt", 64 * 1024).decode().splitlines()) | set(
        components.values()
    )
    files[EXTENSION] = reader(stage / "python" / EXTENSION, 128 * 1024**2)
    native = {EXTENSION: sorted(elf_gate(files[EXTENSION], elf_info, None, allowed))}
    require(
        {components[name] for name in ("videoio", "dnn", "objdetect", "calib3d", "video")}
        <= set(native[EXTENSION]),
        "WHEEL_PYTHON_MODULE_LINKS",
    )
    requirements = {
        "videoio": FFMPEG,
        "dnn": {"libprotobuf.so.29.5.0"},
        "imgcodecs": {"libpng16.so.16", "libOpenEXR-3_5.so.34", "libjpeg.so.8", "libwebp.so.7"},
    }
    for name, soname in components.items():
        path = "cv2/.libs/" + soname
        raw = reader(stage / "lib" / f"libopencv_{name}.so.4.13.0", 128 * 1024**2)
        needed = elf_gate(raw, elf_info, soname, allowed)
        require(requirements.get(name, set()) <= needed, "WHEEL_EXTERNAL_LINKS_" + name)
        files[path] = raw
        native[path] = sorted(needed)
    for name in ("__init__.py", *HAAR):
        path = "cv2/data/" + name
        raw = reader(INPUTS / "wheel-data" / path, 1024**2)
        require(name not in HAAR or digest(raw) == HAAR[name], "WHEEL_HAAR_SHA256")
        files[path] = raw
    require(sum(map(len, files.values())) <= 256 * 1024**2, "WHEEL_SIZE")
    return files, native


def write_wheel(reader, elf_info, validate_record):
    files, native = wheel_files(reader, elf_info)
    numpy = list((INPUTS / "wheelhouse").glob("numpy-2.4.4-cp312-cp312-manylinux_*.whl"))
    require(len(numpy) == 1, "WHEEL_NUMPY_PIN")
    raw = reader(numpy[0], 18 * 1024**2)
    require(digest(raw) == NUMPY_SHA, "WHEEL_NUMPY_SHA256")
    license_prefix = "numpy-2.4.4.dist-info/licenses/"
    licenses = {"licenses/OPENCV-LICENSE": reader(SDK / "source/LICENSE", 64 * 1024)}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            if not item.filename.startswith(license_prefix) or item.is_dir():
                continue
            name = item.filename.removeprefix(license_prefix)
            path = "licenses/numpy/" + name
            require(
                re.fullmatch(r"[A-Za-z0-9_./-]+", name) and ".." not in Path(name).parts,
                "WHEEL_NUMPY_LICENSE_PATH",
            )
            require(
                path not in licenses and 0 < item.file_size <= 1024**2, "WHEEL_NUMPY_LICENSE_SIZE"
            )
            licenses[path] = archive.read(item)
    require("licenses/numpy/LICENSE.txt" in licenses, "WHEEL_NUMPY_LICENSE")
    files.update({DIST + "/" + name: body for name, body in licenses.items()})
    metadata = [
        "Metadata-Version: 2.4",
        "Name: opencv-python",
        f"Version: {WHEEL_VERSION}",
        "Requires-Python: >=3.12,<3.13",
        "Requires-Dist: numpy<2.0; python_version < '3.9'",
        "Requires-Dist: numpy>=2; python_version >= '3.9'",
        "Summary: Compilacion ampliada CPU de OpenCV para SupoClip",
    ]
    metadata.extend("License-File: " + name for name in sorted(licenses))
    files[DIST + "/METADATA"] = ("\n".join(metadata) + "\n\n").encode()
    files[DIST + "/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: agencia-opencv-expanded-v1\nRoot-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n"
    )
    files[DIST + "/RECORD"] = make_record(files)
    validate_record(files, DIST)
    target = WORK / "wheels" / f"opencv_python-{WHEEL_VERSION}-cp312-cp312-linux_x86_64.whl"
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w+b") as output:
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type, info.create_system, info.external_attr = (
                    zipfile.ZIP_DEFLATED,
                    3,
                    0o100644 << 16,
                )
                archive.writestr(info, content)
        output.flush()
        os.fsync(output.fileno())
        output.seek(0)
        pin = hashlib.file_digest(output, "sha256").hexdigest()
    return {
        "success": True,
        "wheel": str(target),
        "sha256": pin,
        "elf_dependencies": native,
        "linux_runtime_verified": False,
        "final_sca_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("source", "configure", "wheel"))
    args = parser.parse_args()
    try:
        require(sys.platform == "linux" and sys.version_info[:2] == (3, 12), "LINUX_CPYTHON312")
        reuse = INPUTS / "reuse"
        read_regular = helper(
            reuse / "package_opencv_minimal.py", ZOU_HELPER_SHA, ("read_regular",)
        )["read_regular"]
        if args.stage == "source":
            raw = read_regular(INPUTS / "opencv-source.tar.gz", 128 * 1024**2)
            require(digest(raw) == SOURCE_SHA and os.geteuid() == 0, "OPENCV_SOURCE_SHA_OR_UID")
            extractor = types.ModuleType("supo_opencv_extractor")
            code = source(reuse / "prepare_backup_sdk.py", EXTRACTOR_SHA)
            exec(compile(code, "prepare_backup_sdk.py", "exec"), extractor.__dict__)
            members = extractor.extract_archive(
                raw,
                "tar.gz",
                "opencv-fe38fc608f6acb8b68953438a62305d8318f4fcd",
                SDK / "source",
                max_bytes=256 * 1024**2,
                max_file=64 * 1024**2,
                max_members=10000,
            )
            for directory, _, files in os.walk(SDK / "source"):
                Path(directory).chmod(0o555)
                for name in files:
                    (Path(directory) / name).chmod(0o444)
            result = {"source_sha256": SOURCE_SHA, "files": len(members)}
        elif args.stage == "configure":
            result = configuration(read_regular)
        else:
            configuration(read_regular)
            elf = helper(reuse / "final_image_remote.py", ELF_HELPER_SHA, ("part", "elf_info"))[
                "elf_info"
            ]
            validate = helper(
                reuse / "inventory_opencv_wheel.py", RECORD_HELPER_SHA, ("validate_record",)
            )["validate_record"]
            result = write_wheel(read_regular, elf, validate)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(str(error)[:240], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
