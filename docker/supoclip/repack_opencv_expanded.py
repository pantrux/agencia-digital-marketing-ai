"""Reubica una copia de los binarios ya compilados y vuelve a empaquetar OpenCV."""

import hashlib
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

WORK = Path("/work")
INPUTS = Path("/inputs")
OUT = WORK / "packaging-v2"
PACKAGER = "a9cf5fcf62e0d2b463d82822d8f3716464b2543eae395da5906902c165fceb05"
DIAGNOSTIC = "4e1ca287327a6e0363d65ee78fbc5c0fda913fd5e3ba8f3118970810bf810a50"
OLD_LOADER = b"os.path.join(os.path.join(LOADER_DIR, '../../'), 'lib')"
INTERMEDIATE_LOADER = b"os.path.join(LOADER_DIR, 'lib')"
NEW_RPATH = "$ORIGIN:$ORIGIN/../.libs"
OLD_RPATH = NEW_RPATH + ":/sdk-data/openexr3/usr/lib"
EXT = "stage/python/cv2/python-3.12/cv2.cpython-312-x86_64-linux-gnu.so"
RELOCATE = {EXT, "stage/lib/libopencv_imgcodecs.so.4.13.0", "stage/lib/libopencv_videoio.so.4.13.0"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, raw):
    with path.open("xb") as f:
        f.write(raw)


def main():
    assert os.getuid() == os.getgid() == 1000 and sys.version_info[:3] == (3, 12, 16)
    assert {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"}
    os.umask(0o077)
    src = INPUTS / "opencv_expanded.py"
    body = src.read_bytes()
    assert sha(body) == PACKAGER
    module = types.ModuleType("opencv_packager_pinned")
    module.__file__ = str(src)
    exec(compile(body, str(src), "exec"), module.__dict__)
    reuse = INPUTS / "reuse"
    reader = module.helper(
        reuse / "package_opencv_minimal.py", module.ZOU_HELPER_SHA, ("read_regular",)
    )["read_regular"]
    parse = module.helper(
        reuse / "final_image_remote.py", module.ELF_HELPER_SHA, ("part", "elf_info")
    )["elf_info"]
    record = module.helper(
        reuse / "inventory_opencv_wheel.py", module.RECORD_HELPER_SHA, ("validate_record",)
    )["validate_record"]
    module.configuration(reader)
    diag = reader(WORK / "packaging-v2-input.json", 32768)
    assert sha(diag) == DIAGNOSTIC
    info = json.loads(diag)
    assert info["success"] and len(info["files"]) == 11
    assert {name for name, row in info["files"].items() if row["rpaths"] == [OLD_RPATH]} == RELOCATE
    OUT.mkdir(mode=0o700)
    (OUT / "wheels").mkdir(mode=0o700)
    loader = OUT / "stage/python/cv2"
    loader.mkdir(parents=True, mode=0o700)
    for name in (
        "__init__.py",
        "config.py",
        "config-3.12.py",
        "load_config_py2.py",
        "load_config_py3.py",
    ):
        data = reader(WORK / "stage/python/cv2" / name, 65536)
        if name in info["configs"]:
            assert sha(data) == info["configs"][name]["sha256"]
        if name == "config.py":
            assert data.count(OLD_LOADER) == 1
            data = data.replace(OLD_LOADER, INTERMEDIATE_LOADER)
        write(loader / name, data)
    changed = []
    for name, row in sorted(info["files"].items()):
        assert name == EXT or re.fullmatch(r"stage/lib/libopencv_[a-z0-9]+\.so\.4\.13\.0", name)
        data = reader(WORK / name, 128 * 1024**2)
        assert len(data) == row["bytes"] and sha(data) == row["sha256"]
        target = OUT / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        write(target, data)
        if name in RELOCATE:
            cmake = OUT / f"rpath-{len(changed)}.cmake"
            write(
                cmake,
                f'file(RPATH_CHANGE FILE "{target}" OLD_RPATH "{OLD_RPATH}" NEW_RPATH "{NEW_RPATH}")\n'.encode(),
            )
            subprocess.run(
                ["/usr/bin/cmake", "-P", str(cmake)],
                timeout=15,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            new = reader(target, 128 * 1024**2)
            before, after = parse(data), parse(new)
            old_tag, new_tag = (
                before.pop("rpath") or before["runpath"],
                after.pop("rpath") or after["runpath"],
            )
            before.pop("runpath")
            after.pop("runpath")
            assert old_tag == OLD_RPATH and new_tag == NEW_RPATH and before == after
            changed.append({"path": name, "before": sha(data), "after": sha(new)})
    assert len(changed) == 3
    module.WORK = OUT
    result = module.write_wheel(reader, parse, record)
    result.update(relocated=changed, original_compiled_outputs_preserved=True, packaging_version=2)
    raw = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
    write(OUT / "wheel.json", raw)
    print(raw.decode(), end="")


if __name__ == "__main__":
    main()
