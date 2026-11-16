"""Instalación offline y entrada privada de MoneyPrinterTurbo sobre Wolfi."""

import hashlib
import importlib.metadata
import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

INPUTS = Path("/inputs")
APP = Path("/MoneyPrinterTurbo")
SITE = Path("/opt/moneyprinter/site")
STATE = Path("/run/mpt")
RUNTIME = "sha256:ba69f696b21955483e5e47e46eab2d7a9e556a3cf5fd4bd40817554901781af8"
PIP_WHEEL = Path("/usr/share/python-wheels/pip-26.2.2-py3-none-any.whl")
PIP_SHA256 = "38f4aacff7c3b0d7b216cac0a106a1036eef98b0a7ff5b932ea2fcb787e97bc5"
APK_DB = Path("/usr/lib/apk/db/installed")
KEY = "keys/wolfi-signing.rsa.pub"
WOLFI_SIGNING_SHA256 = "f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc"
INDEX = "apk-repo/x86_64/APKINDEX.tar.gz"
LOCK = "requirements.lock"
FONT = Path("/usr/share/fonts/font-liberation/LiberationSans-Regular.ttf")
TOOL_PINS = {
    "/usr/bin/ffmpeg": "cb75539bea6a57dc0824ac5cfa712a52b570e207fd1aa94b60312ac353b16dad",
    "/usr/bin/ffprobe": "0c213b062bddfd988970f14b4002d15d90ebc677197830ffe81a483b577cccbd",
    str(FONT): "559f38bdfcf12beead8a744579715dc7249d6978616e757de7d8cf06403aa531",
}
INPUT_PINS = {
    LOCK: "cfdb90ecf17f52c1f23b441c703bf81d219b6f0f43a9c972b3cf57e0882b902c",
    KEY: WOLFI_SIGNING_SHA256,
    INDEX: "9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd",
    "app-tree/app/services/agency_speaches.py": (
        "4053c26cf06765d0262cec4f51cd61645629c28af4c13a38214e31154b37c3d7"
    ),
    "app-tree/speaches_client.py": "f5421409c749c65768b3f8364ee41896a803f9f36b21e5dc793809b3dc5295c5",
}
FIXED_APKS = {
    "ffmpeg-7": "7.1.5-r8",
    "font-liberation": "2.1.5-r6",
    "openssl-4.0-dev": "4.0.3-r8",
    "libbz2-1": "1.0.8-r30",
    "libexpat1": "2.10.0-r1",
    "libffi": "3.8.0-r2",
    "libstdc++": "16.2.0-r2",
    "ncurses": "6.6.20261031-r0",
    "ncurses-terminfo-base": "6.6.20261031-r0",
    "xz": "5.8.4-r1",
}
APP_TOP = {
    "LICENSE",
    "LICENSE.agency-speaches.txt",
    "cli.py",
    "main.py",
    "pyproject.toml",
    "config.example.toml",
    "resource/public/index.html",
    "speaches_client.py",
    "speaches_contract.py",
}
EMPTY_FILES = {"app-tree/app/models/__init__.py", "app-tree/app/services/__init__.py"}
MAX_FILE = 96 * 1024**2


def require(condition, code):
    if not condition:
        raise ValueError(code)


def signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def opened(path, maximum):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 <= info.st_size <= maximum,
        "MPT_INPUT_NODE",
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, "rb")
    if signature(os.fstat(fd)) != signature(info):
        stream.close()
        raise ValueError("MPT_INPUT_CHANGED")
    return stream, info


def unchanged(path, stream, initial):
    require(
        signature(initial) == signature(os.fstat(stream.fileno())) == signature(path.lstat()),
        "MPT_INPUT_CHANGED",
    )


def checked_digest(path, maximum=MAX_FILE):
    stream, initial = opened(path, maximum)
    with stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        unchanged(path, stream, initial)
    return {"bytes": initial.st_size, "sha256": digest}


def checked_bytes(path, maximum):
    stream, initial = opened(path, maximum)
    with stream:
        data = stream.read(maximum + 1)
        unchanged(path, stream, initial)
        require(len(data) == initial.st_size, "MPT_INPUT_SIZE")
    return data


def expected_installed(base, additions):
    require(
        isinstance(base, dict)
        and isinstance(additions, dict)
        and len(base) == 19
        and len(additions) == 65
        and not (base.keys() & additions.keys())
        and all(additions.get(name) == version for name, version in FIXED_APKS.items()),
        "MPT_APK_INVENTORY",
    )
    for name, version in (base | additions).items():
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9+_.-]*", name)
            and isinstance(version, str)
            and re.fullmatch(r"[A-Za-z0-9+_.-]+", version),
            "MPT_APK_IDENTITY",
        )
    return base | additions


def parse_installed(raw):
    result = {}
    for block in raw.decode().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line[:2] in {"P:", "V:"}:
                require(line[0] not in fields, "MPT_APK_DB_DUPLICATE")
                fields[line[0]] = line[2:]
        if fields:
            require(set(fields) == {"P", "V"} and fields["P"] not in result, "MPT_APK_DB_ROW")
            result[fields["P"]] = fields["V"]
    return result


def verify_inputs():
    manifest = json.loads(checked_bytes(INPUTS / "build-inputs.json", 256 * 1024))
    require(
        set(manifest)
        == {"schema", "kind", "runtime_manifest", "base_packages", "apk_packages", "files"}
        and manifest["schema"] == 1
        and manifest["kind"] == "moneyprinter-wolfi-build-inputs-v2"
        and manifest["runtime_manifest"] == RUNTIME,
        "MPT_BUILD_MANIFEST",
    )
    base, apks, files = (manifest[key] for key in ("base_packages", "apk_packages", "files"))
    final = expected_installed(base, apks)
    wheel_names = {name for name in files if re.fullmatch(r"wheels/[A-Za-z0-9_.+-]+\.whl", name)}
    app_names = {name for name in files if name.startswith("app-tree/")}
    require(len(wheel_names) == 99 and len(app_names) == 83, "MPT_BUILD_COVERAGE")
    expected = {LOCK, "repositories", KEY, INDEX} | wheel_names | app_names
    expected.update(f"apk-repo/x86_64/{name}-{version}.apk" for name, version in apks.items())
    require(set(files) == expected and len(files) == 251, "MPT_BUILD_FILES")
    require(
        set(INPUT_PINS) <= files.keys()
        and all(files[name].get("sha256") == pin for name, pin in INPUT_PINS.items())
        and checked_bytes(INPUTS / "repositories", 256) == b"/inputs/apk-repo\n",
        "MPT_BUILD_PINS",
    )
    for name, metadata in files.items():
        parts = name.split("/")
        require(
            all(
                re.fullmatch(r"[A-Za-z0-9_.+-]+", part) and part not in {".", ".."}
                for part in parts
            ),
            "MPT_BUILD_PATH",
        )
        require(
            isinstance(metadata, dict)
            and set(metadata) == {"bytes", "sha256"}
            and type(metadata["bytes"]) is int
            and (metadata["bytes"] == 0 if name in EMPTY_FILES else metadata["bytes"] > 0),
            "MPT_BUILD_FILE_SIZE",
        )
        if name in app_names:
            relative = name.removeprefix("app-tree/")
            require(
                relative in APP_TOP or relative.startswith(("app/", "webui/")),
                "MPT_APP_TREE_SCOPE",
            )
            require(checked_digest(APP / relative) == metadata, "MPT_APP_COPY")
        require(checked_digest(INPUTS / name) == metadata, "MPT_BUILD_SHA")
    app_relative = {name.removeprefix("app-tree/") for name in app_names}
    for root, expected_names in (
        (APP, app_relative),
        (INPUTS / "app-tree", app_relative),
        (INPUTS / "wheels", {Path(name).name for name in wheel_names}),
        (INPUTS / "keys", {Path(KEY).name}),
        (
            INPUTS / "apk-repo/x86_64",
            {Path(name).name for name in expected if name.startswith("apk-repo/x86_64/")},
        ),
    ):
        actual = set()
        for parent, dirs, names in os.walk(root, followlinks=False):
            path = Path(parent)
            require(all(not (path / name).is_symlink() for name in dirs), "MPT_BUILD_TREE_LINK")
            actual.update((path / name).relative_to(root).as_posix() for name in names)
        require(actual == expected_names, "MPT_BUILD_TREE")
    return base, apks, final


def install():
    require(os.geteuid() == os.getegid() == 0, "MPT_BUILD_ROOT")
    os.umask(0o022)
    base, apks, final = verify_inputs()
    require(parse_installed(checked_bytes(APK_DB, 1024**2)) == base, "MPT_BASE_APK_DRIFT")
    subprocess.run(
        [
            "/sbin/apk",
            "--no-network",
            "--no-scripts",
            "--no-commit-hooks",
            "--no-cache",
            "--repositories-file",
            "/inputs/repositories",
            "--keys-dir",
            "/inputs/keys",
            "add",
            *sorted(f"{name}={version}" for name, version in apks.items()),
        ],
        check=True,
        timeout=120,
    )
    require(parse_installed(checked_bytes(APK_DB, 4 * 1024**2)) == final, "MPT_FINAL_APK_DRIFT")
    subprocess.run(["/usr/bin/ldconfig"], check=True, timeout=20)
    for name, pin in TOOL_PINS.items():
        require(checked_digest(Path(name))["sha256"] == pin, "MPT_NATIVE_TOOL_SHA")
    encoders = subprocess.run(
        ["/usr/bin/ffmpeg", "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        check=True,
        timeout=15,
    ).stdout
    require(
        all(
            re.search(rb"(?m)^\s*[AV]\S*\s+" + name + rb"\s", encoders)
            for name in (b"libx264", b"aac")
        ),
        "MPT_ENCODERS",
    )
    subprocess.run(["/usr/bin/ffprobe", "-version"], check=True, timeout=15)
    font_dir = APP / "resource/fonts"
    font_dir.parent.chmod(0o755)
    font_dir.mkdir()
    shutil.copyfile(FONT, font_dir / FONT.name)
    require(checked_digest(PIP_WHEEL)["sha256"] == PIP_SHA256, "MPT_PIP_SHA")
    SITE.mkdir()
    sys.path.insert(0, str(PIP_WHEEL))
    sys.argv = [
        "pip",
        "--isolated",
        "--disable-pip-version-check",
        "install",
        "--no-index",
        "--no-deps",
        "--require-hashes",
        "--only-binary=:all:",
        "--no-compile",
        "--find-links",
        "/inputs/wheels",
        "--target",
        str(SITE),
        "--requirement",
        "/inputs/requirements.lock",
    ]
    try:
        runpy.run_module("pip", run_name="__main__")
    except SystemExit as done:
        require(done.code in {None, 0}, "MPT_PIP_INSTALL")
    installed = list(importlib.metadata.distributions(path=[str(SITE)]))
    require(len(installed) == 99, "MPT_WHEEL_COUNT")


def launch(mode):
    require(os.geteuid() == os.getegid() == 65532, "MPT_PRIVATE_UID")
    root = STATE.lstat()
    require(
        os.environ.get("MPT_HOME") == str(STATE)
        and stat.S_ISDIR(root.st_mode)
        and root.st_uid == root.st_gid == 65532
        and stat.S_IMODE(root.st_mode) == 0o700,
        "MPT_PRIVATE_STATE",
    )
    config_path = STATE / "config.toml"
    config = config_path.lstat()
    require(
        config.st_uid == config.st_gid == 65532 and stat.S_IMODE(config.st_mode) == 0o600,
        "MPT_PRIVATE_CONFIG",
    )
    app_config = tomllib.loads(checked_bytes(config_path, 128 * 1024).decode())["app"]
    require(
        app_config.get("subtitle_provider") == "speaches"
        and app_config.get("subtitle_speaches_token_file") == "/run/secrets/moneyprinter-speaches"
        and app_config.get("ffmpeg_path", "") in {"", "/usr/bin/ffmpeg"}
        and app_config.get("enable_redis", False) is False,
        "MPT_CONFIG_CONTRACT",
    )
    os.umask(0o077)
    os.environ.update(
        FFMPEG_BINARY="/usr/bin/ffmpeg",
        IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg",
        PYTHON_DOTENV_DISABLED="1",
    )
    sys.path[:0] = [str(APP), str(SITE)]
    if mode == "api":
        sys.argv = [str(APP / "main.py")]
        runpy.run_path(sys.argv[0], run_name="__main__")
    else:
        sys.argv = [
            "streamlit",
            "run",
            str(APP / "webui/Main.py"),
            "--server.address=0.0.0.0",
            "--server.port=8501",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ]
        runpy.run_module("streamlit", run_name="__main__", alter_sys=True)


def main():
    require(
        sys.version_info[:3] == (3, 12, 16)
        and sys.flags.isolated == sys.flags.no_site == sys.flags.dont_write_bytecode == 1,
        "MPT_PYTHON_3_12_16",
    )
    require(len(sys.argv) == 2 and sys.argv[1] in {"install", "api", "ui"}, "MPT_MODE")
    mode = sys.argv[1]
    if mode == "install":
        install()
    else:
        launch(mode)


if __name__ == "__main__":
    main()
