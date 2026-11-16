"""Instala y arranca el backend CPU de SupoClip con entradas fijadas."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

INPUTS = Path("/inputs")
PRODUCTS = Path("/products")
ROOT = Path("/opt/supoclip")
APP = ROOT / "backend"
SITE = ROOT / "site"
STATE = Path("/state")
SECRETS = Path("/run/secrets")
UID = 1000
PIP = Path("/usr/share/python-wheels/pip-26.2.2-py3-none-any.whl")
PIP_SHA = "38f4aacff7c3b0d7b216cac0a106a1036eef98b0a7ff5b932ea2fcb787e97bc5"
OPENCV_VERSION = "4.13.0.92+agency1"
OPENCV = f"opencv_python-{OPENCV_VERSION}-cp312-cp312-linux_x86_64.whl"
OPENCV_SHA = "c1d04e99d5abebba09668def83943b36f0b242d6b7792d10515aa4d2d15dd9f7"
ASS = Path("/opt/supoclip-ffmpeg-ass/bin/ffmpeg")
ASS_SHA = "e8fb8dc1d20240de4017bbe0f56c06067cb96e62a4a859426ca129ee1fd905eb"
STOCK_FFMPEG = Path("/usr/bin/ffmpeg")
STOCK_FFPROBE = Path("/usr/bin/ffprobe")
SOURCE_TAR = "075be34ef695acd38c9a02b27a50c18445b715ed9fa6a91fb62b7f5c98e641c8"
SOURCE_RECEIPT = "5565713d1f2fbe59256bfcc23ee63e9d7439c492cd017c27d538add0ec7c1865"
WHEELS_TAR = "bcea6fecdfd66b0142567f21a2732d8f9f39cdbfd7d89333fc131400b300a200"
WHEELS_RECEIPT = "770b00da9b629ece821da3ce3bf2977aea7334c15d4fd91ea1712b88d04b9ca1"
OPENCV_PROBE = "0471ba7f93a73eb540e2d28cd37fb70122dd799ef34890fdc31bb9c75357bd57"
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def node(path, maximum):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 <= info.st_size <= maximum,
        "ENTRADA_REGULAR",
    )
    return info


def read(path, maximum, pin=None):
    info = node(path, maximum)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) == info.st_size, "ENTRADA_TAMANO")
    if pin is not None:
        require(hashlib.sha256(raw).hexdigest() == pin, "ENTRADA_SHA256")
    return raw


def pinned(path, maximum, pin, size=None):
    info = node(path, maximum)
    require(size is None or size == info.st_size, "ENTRADA_TAMANO")
    with path.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == pin, "ENTRADA_SHA256")


def write(path, raw, mode=0o644):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(mode)


def unpack(bundle, dest, members):
    dest.mkdir(mode=0o755, parents=True, exist_ok=True)
    seen = set()
    with tarfile.open(bundle, mode="r:") as archive:
        for item in archive:
            name = item.name
            path = PurePosixPath(name)
            require(
                item.isfile()
                and item.mode == 0o444
                and name not in seen
                and name in members
                and path.as_posix() == name
                and not path.is_absolute()
                and ".." not in path.parts
                and "\\" not in name
                and ":" not in name,
                "TAR_MIEMBRO",
            )
            expected = members[name]
            require(0 <= item.size == expected["bytes"] <= 96 * 1024**2, "TAR_TAMANO")
            raw = archive.extractfile(item).read(item.size + 1)
            require(
                len(raw) == item.size and hashlib.sha256(raw).hexdigest() == expected["sha256"],
                "TAR_SHA256",
            )
            write(dest.joinpath(*path.parts), raw)
            seen.add(name)
    require(seen == set(members), "TAR_COBERTURA")


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_distributions(raw):
    result = {}
    for line in raw.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.+!_-]+) --hash=sha256:[a-f0-9]{64}", line
        )
        require(match is not None, "LOCK_FILA")
        name = canonical_name(match[1])
        require(name not in result, "LOCK_DUPLICADO")
        result[name] = match[2]
    return result


def command(argv, *, timeout, capture=False):
    return subprocess.run(
        argv,
        env={"LC_ALL": "C", "PATH": PATH, "PIP_CONFIG_FILE": "/dev/null"},
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
    )


def install():
    require(os.geteuid() == os.getegid() == 0, "BUILD_UID")
    require(sys.version_info[:3] == (3, 12, 16), "CPYTHON_VERSION")
    os.umask(0o022)
    source = INPUTS / "backend-source.tar"
    wheels = INPUTS / "wheelhouse-preopencv.tar"
    pinned(source, 16 * 1024**2, SOURCE_TAR, 10567680)
    pinned(wheels, 96 * 1024**2, WHEELS_TAR, 69949440)
    source_receipt = json.loads(read(INPUTS / "backend-source-receipt.json", 65536, SOURCE_RECEIPT))
    files = source_receipt["archives"]["backend-source.tar"]["members"]
    require(
        len(files) == 177
        and all(name == "LICENSE" or name.startswith("backend/") for name in files),
        "TAR_FUENTES",
    )
    unpack(source, ROOT, files)
    (APP / "logs").mkdir(mode=0o755)

    wheel_receipt = json.loads(read(INPUTS / "wheelhouse-receipt.json", 65536, WHEELS_RECEIPT))
    members = wheel_receipt["members"]
    require(
        len(members) == 84
        and "requirements.preopencv.lock" in members
        and all(
            name == "requirements.preopencv.lock"
            or re.fullmatch(r"wheelhouse/[A-Za-z0-9_.+-]+\.whl", name)
            for name in members
        ),
        "TAR_RUEDAS",
    )
    require(set(os.listdir(PRODUCTS)) == {"ffmpeg", OPENCV}, "PRODUCTOS_COBERTURA")
    write(ASS, read(PRODUCTS / "ffmpeg", 128 * 1024**2, ASS_SHA), 0o755)
    pinned(PIP, 8 * 1024**2, PIP_SHA)
    with tempfile.TemporaryDirectory(prefix="supoclip-pip-") as temp:
        work = Path(temp)
        unpack(wheels, work, members)
        wheel = read(PRODUCTS / OPENCV, 13132554, OPENCV_SHA)
        require(len(wheel) == 13132554, "OPENCV_RUEDA_TAMANO")
        write(work / "wheelhouse" / OPENCV, wheel)
        locked = (work / "requirements.preopencv.lock").read_bytes()
        expected = locked_distributions(locked)
        require(
            len(expected) == 83
            and expected.get("srt") == "3.5.3"
            and not any(name.startswith("opencv-") for name in expected),
            "LOCK_PREOPENCV",
        )
        expected["opencv-python"] = OPENCV_VERSION
        locked += f"opencv-python=={OPENCV_VERSION} --hash=sha256:{OPENCV_SHA}\n".encode()
        write(work / "requirements.lock", locked)
        SITE.mkdir(mode=0o755)
        command(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                f"import runpy,sys;sys.path.insert(0,{str(PIP)!r});runpy.run_module('pip',run_name='__main__')",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-compile",
                "--find-links",
                str(work / "wheelhouse"),
                "--target",
                str(SITE),
                "--requirement",
                str(work / "requirements.lock"),
            ],
            timeout=180,
        )
    packages = list(importlib.metadata.distributions(path=[str(SITE)]))
    installed = {canonical_name(package.metadata["Name"]): package.version for package in packages}
    require(len(packages) == 84 and installed == expected, "SITE_INVENTARIO")
    print(json.dumps({"success": True, "distributions": 84, "opencv": OPENCV_VERSION}))


def rootless():
    require(os.geteuid() == os.getegid() == UID, "RUNTIME_UID")
    os.umask(0o077)


def probe():
    rootless()
    tool = ROOT / "bin/verify_opencv_expanded.py"
    pinned(tool, 65536, OPENCV_PROBE)
    pinned(ASS, 128 * 1024**2, ASS_SHA)
    filters = command([ASS, "-hide_banner", "-filters"], timeout=15, capture=True).stdout
    require(re.search(rb"(?m)^\s*[.TSC]{2,3}\s+subtitles\s+V->V\b", filters), "FFMPEG_ASS")
    command([STOCK_FFPROBE, "-version"], timeout=15, capture=True)
    command(["/usr/bin/node", "--version"], timeout=15, capture=True)
    command(["/usr/bin/tesseract", "--version"], timeout=15, capture=True)
    with tempfile.TemporaryDirectory(prefix="supoclip-video-") as temp:
        video = Path(temp) / "synthetic.mp4"
        command(
            [
                STOCK_FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=12:duration=1.5",
                "-an",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            timeout=20,
        )
        with video.open("rb") as stream:
            pin = hashlib.file_digest(stream, "sha256").hexdigest()
        command(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(tool),
                "--site",
                str(SITE),
                "--video",
                str(video),
                "--video-sha256",
                pin,
                "--yunet",
                str(APP / "src/media/models/face_detection_yunet_2023mar.onnx"),
            ],
            timeout=45,
        )


def secret(name, *, read_value=True):
    path = SECRETS / ("supoclip-" + name)
    info = node(path, 8192)
    require(
        info.st_uid == UID and info.st_gid == UID and stat.S_IMODE(info.st_mode) in {0o400, 0o600},
        "SECRETO_PERMISOS",
    )
    if not read_value:
        require(info.st_size > 0, "SECRETO_VACIO")
        return str(path)
    value = read(path, 8192).decode("utf-8").removesuffix("\n")
    require(
        value and value == value.strip() and all(ord(char) >= 32 for char in value), "SECRETO_VALOR"
    )
    return value


def launch(role):
    rootless()
    for path in (STATE, STATE / "outputs", STATE / "temp", APP / "logs"):
        info = path.lstat()
        require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == info.st_gid == UID
            and stat.S_IMODE(info.st_mode) == 0o700,
            "ESTADO_PRIVADO",
        )
    database = secret("database-url")
    parsed = urlsplit(database)
    require(
        parsed.scheme == "postgresql+asyncpg"
        and parsed.hostname
        and parsed.username
        and parsed.path,
        "BASE_URL",
    )
    require(os.environ.get("REDIS_HOST"), "REDIS_HOST")
    os.environ.update(
        PATH=PATH,
        PYTHON_DOTENV_DISABLED="1",
        DATABASE_URL=database,
        BACKEND_AUTH_SECRET=secret("backend-auth"),
        REDIS_PASSWORD=secret("redis-password"),
        TRANSCRIPTION_PROVIDER="speaches",
        SPEACHES_BEARER_TOKEN_FILE=secret("speaches", read_value=False),
        ALLOW_UNSIGNED_BACKEND_AUTH="false",
        SELF_HOST="true",
        MEDIA_STORAGE="local",
        OUTPUT_DIR=str(STATE / "outputs"),
        TEMP_DIR=str(STATE / "temp"),
        XDG_CACHE_HOME=str(STATE / "temp/cache"),
    )
    os.chdir(APP)
    sys.path[:0] = [str(APP), str(SITE)]
    if role == "api":
        import uvicorn

        uvicorn.run("src.main:app", host="0.0.0.0", port=8000, workers=1, proxy_headers=False)
    else:
        from arq import run_worker
        from src.workers.tasks import WorkerSettings

        run_worker(WorkerSettings, max_jobs=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("install", "probe", "api", "worker"))
    args = parser.parse_args()
    try:
        if args.stage == "install":
            install()
        elif args.stage == "probe":
            probe()
        else:
            launch(args.stage)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = str(error)
        message = detail if re.fullmatch(r"[A-Z0-9_]{2,60}", detail) else type(error).__name__
        print(
            json.dumps({"success": False, "stage": args.stage, "error": message}), file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
