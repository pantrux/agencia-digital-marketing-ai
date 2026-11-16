"""Entradas finitas de Zou; importar este módulo no lee secretos ni carga la app."""

import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import sys
from pathlib import Path

SITE = "/opt/zou/site"
SECRETS = {
    "DB_PASSWORD": "/run/secrets/zou_db_password",
    "KV_PASSWORD": "/run/secrets/zou_kv_password",
    "SECRET_KEY": "/run/secrets/zou_secret_key",
}
INPUTS = Path("/inputs")
PIP_WHEEL = "/usr/share/python-wheels/pip-26.2.2-py3-none-any.whl"
PIP_SHA = "38f4aacff7c3b0d7b216cac0a106a1036eef98b0a7ff5b932ea2fcb787e97bc5"


def require(value, code):
    if not value:
        raise ValueError(code)


def read_secret(path):
    path = Path(path)
    before = path.lstat()
    require(
        stat.S_ISREG(before.st_mode)
        and before.st_nlink == 1
        and before.st_uid in {0, 1000}
        and not before.st_mode & 0o022
        and 0 < before.st_size <= 4096,
        "SECRET_FILE",
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(4097)
        after = os.fstat(stream.fileno())
    final = path.lstat()

    def signature(row):
        return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns, row.st_nlink

    require(
        signature(before) == signature(opened) == signature(after) == signature(final)
        and len(raw) <= 4096,
        "SECRET_CHANGED",
    )
    return raw.decode("utf-8").removesuffix("\n").removesuffix("\r")


def runtime_environment(mapping, secret_reader=read_secret):
    result = {}
    for key in ("DB_HOST", "DB_USERNAME", "DB_DATABASE", "KV_HOST"):
        value = mapping.get(key, "")
        require(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", value),
            "RUNTIME_CONFIG",
        )
        result[key] = value
    for key, default in (("DB_PORT", "5432"), ("KV_PORT", "6379")):
        value = mapping.get(key, default)
        require(
            isinstance(value, str) and value.isdecimal() and 1 <= int(value) <= 65535,
            "RUNTIME_PORT",
        )
        result[key] = value
    for key, path in SECRETS.items():
        value = secret_reader(path)
        require(
            isinstance(value, str)
            and 12 <= len(value) <= 4096
            and "\x00" not in value
            and value not in {"CHANGE_ME", "mysecretpassword", "mysecretkey"}
            and not value.startswith(("COMPLETAR_", "GENERAR_")),
            "SECRET_PENDING",
        )
        result[key] = value
    result.update(
        {
            "PSYCOPG_IMPL": "python",
            "DB_DRIVER": "postgresql+psycopg",
            "PREVIEW_FOLDER": "/var/lib/zou/previews",
            "TMP_DIR": "/var/lib/zou/tmp",
            "TMPDIR": "/var/lib/zou/tmp",
            "HOME": "/var/lib/zou",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LD_LIBRARY_PATH": "/usr/lib64:/usr/lib",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "MAIL_ENABLED": "false",
            "SAML_ENABLED": "false",
            "GUNICORN_CMD_ARGS": "",
        }
    )
    domain = mapping.get("DOMAIN_NAME", "localhost")
    protocol = mapping.get("DOMAIN_PROTOCOL", "http")
    require(
        isinstance(domain, str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", domain)
        and protocol in {"http", "https"},
        "DOMAIN_CONFIG",
    )
    result.update(DOMAIN_NAME=domain, DOMAIN_PROTOCOL=protocol)
    return result


def gunicorn_arguments(mode):
    require(mode in {"api", "events"}, "COMMAND")
    worker, port, application = (
        ("gevent", "5000", "zou.app:app")
        if mode == "api"
        else (
            "geventwebsocket.gunicorn.workers.GeventWebSocketWorker",
            "5001",
            "zou.event_stream:app",
        )
    )
    return [
        "gunicorn",
        "--bind",
        "0.0.0.0:" + port,
        "--workers",
        "1",
        "--worker-class",
        worker,
        "--timeout",
        "60",
        "--access-logfile",
        "-",
        "--error-logfile",
        "-",
        application,
    ]


def activate_site():
    require(
        sys.flags.isolated == 1 and sys.flags.no_site == 1 and os.geteuid() == os.getegid() == 1000,
        "ROOTLESS_ISOLATED_REQUIRED",
    )
    info = Path(SITE).lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
        "SITE_DIRECTORY",
    )
    sys.path.insert(0, SITE)  # No site.addsitedir: ningún .pth se procesa.


def installed_database(raw):
    result = {}
    for block in raw.decode("utf-8").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line[:2] in {"P:", "V:"}:
                require(line[0] not in fields, "APK_DB_DUPLICATE_FIELD")
                fields[line[0]] = line[2:]
        if fields:
            require(set(fields) == {"P", "V"} and fields["P"] not in result, "APK_DB_IDENTITY")
            result[fields["P"]] = fields["V"]
    require(result, "APK_DB_EMPTY")
    return result


def apk_arguments(packages):
    names = set()
    selectors = []
    for row in packages:
        name, version = row["name"], row["version"]
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9+_.-]*", name)
            and re.fullmatch(r"[A-Za-z0-9+_.-]+", version)
            and name not in names,
            "APK_SELECTOR",
        )
        names.add(name)
        selectors.append(name + "=" + version)
    require(selectors, "APK_EMPTY")
    return [
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
        *sorted(selectors),
    ]


def pip_arguments():
    return [
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
        SITE,
        "--requirement",
        "/inputs/requirements.lock",
    ]


def build_bytes(path, maximum):
    path = Path(path)
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum, "BUILD_FILE"
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    final = path.lstat()

    def signature(row):
        return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns, row.st_nlink

    require(
        signature(info) == signature(before) == signature(after) == signature(final)
        and len(raw) == info.st_size,
        "BUILD_FILE_CHANGED",
    )
    return raw


def verify_build_inputs():
    manifest = json.loads(build_bytes(INPUTS / "build-inputs.json", 256 * 1024))
    require(
        manifest.get("kind") == "zou-application-build-inputs-v1"
        and len(manifest.get("apk_packages", [])) == 10
        and len(manifest.get("base_packages", {})) == 19
        and len(manifest.get("wheels", [])) == 141,
        "BUILD_INPUT_COVERAGE",
    )
    expected = {
        "requirements.lock",
        "repositories",
        "keys/wolfi-signing.rsa.pub",
        "apk-repo/x86_64/APKINDEX.tar.gz",
    }
    expected.update("wheels/" + row["filename"] for row in manifest["wheels"])
    expected.update("apk-repo/x86_64/" + row["filename"] for row in manifest["apk_packages"])
    require(
        set(manifest.get("files", {})) == expected and len(expected) == 155, "BUILD_INPUT_FILES"
    )
    for name, row in manifest["files"].items():
        require(
            re.fullmatch(r"[A-Za-z0-9_+./-]+", name)
            and all(p not in {"", ".", ".."} for p in name.split("/")),
            "BUILD_PATH",
        )
        raw = build_bytes(INPUTS.joinpath(*name.split("/")), 96 * 1024**2)
        require(
            len(raw) == row["bytes"] and hashlib.sha256(raw).hexdigest() == row["sha256"],
            "BUILD_SHA",
        )
    return manifest


def install_inputs(mode):
    require(
        sys.flags.isolated == sys.flags.no_site == 1 and os.geteuid() == os.getegid() == 0,
        "BUILD_ROOT_ISOLATED_REQUIRED",
    )
    os.umask(0o022)
    manifest = verify_build_inputs()
    if mode == "install-apks":
        database = Path("/usr/lib/apk/db/installed")
        require(
            installed_database(build_bytes(database, 1024**2)) == manifest["base_packages"],
            "BASE_DB_CHANGED",
        )
        subprocess.run(apk_arguments(manifest["apk_packages"]), check=True, timeout=120)
        expected = manifest["base_packages"] | {
            row["name"]: row["version"] for row in manifest["apk_packages"]
        }
        require(
            len(expected) == 29 and installed_database(build_bytes(database, 1024**2)) == expected,
            "INSTALLED_DB_CHANGED",
        )
        verify_build_inputs()
        return 0
    require(mode == "install-wheels", "BUILD_MODE")
    raw = build_bytes(PIP_WHEEL, 8 * 1024**2)
    require(hashlib.sha256(raw).hexdigest() == PIP_SHA, "PIP_WHEEL_SHA")
    Path(SITE).mkdir(mode=0o755)
    for path in ("/var/lib/zou", "/var/lib/zou/previews", "/var/lib/zou/tmp"):
        Path(path).mkdir(mode=0o700)
        os.chown(path, 1000, 1000)
    sys.path.insert(0, PIP_WHEEL)
    sys.argv = pip_arguments()
    # pip sólo instala las ruedas locales con hash; no procesa .pth del sitio destino.
    try:
        runpy.run_module("pip", run_name="__main__")
    except SystemExit as finished:
        require(finished.code in {None, 0}, "PIP_INSTALL_FAILED")
    verify_build_inputs()
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    require(len(args) <= 1, "COMMAND")
    mode = args[0] if args else "api"
    if mode in {"install-apks", "install-wheels"}:
        return install_inputs(mode)
    require(mode in {"api", "events", "init-db", "create-admin"}, "COMMAND")
    activate_site()
    settings = runtime_environment(os.environ)
    admin_email = os.environ.get("ZOU_ADMIN_EMAIL", "")
    os.environ.clear()
    os.environ.update(settings)
    os.umask(0o077)
    if mode in {"api", "events"}:
        from gunicorn.app.wsgiapp import run

        sys.argv = gunicorn_arguments(mode)
        run()
    else:
        from zou import cli

        if mode == "init-db":
            cli.cli.main(args=["init-db"], standalone_mode=False)
        else:
            require(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", admin_email), "ADMIN_EMAIL")
            password = read_secret("/run/secrets/zou_admin_password")
            require(12 <= len(password) <= 4096 and password != "CHANGE_ME", "ADMIN_SECRET")
            cli.create_admin.callback(admin_email, password)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as failure:
        print("ZOU_LAUNCH_REJECTED " + type(failure).__name__, file=sys.stderr)
        raise SystemExit(1) from None
