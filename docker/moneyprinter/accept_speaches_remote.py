"""Arnés Linux explícito; Root acredita namespace y paquete antes de ejecutar."""

import argparse
import base64
import hashlib
import io
import json
import os
import signal
import socket
import stat
import struct
import sys
import tempfile
import time
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path

OWNER = "agencia-digital-marketing-ai:moneyprinter-speaches-acceptance:v1"
ROOT_PATH = "/home/jandrade/.agency-moneyprinter-speaches-acceptance-v1"
MARKER = ".agency-mpt-acceptance-owner.json"
MAX_BUNDLE = 2 * 1024**2


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def frame(stream, maximum):
    def exact(size):
        value = bytearray()
        while len(value) < size:
            block = stream.read(size - len(value))
            require(block, "MPT_FRAME_TRUNCATED")
            value.extend(block)
        return bytes(value)

    size = struct.unpack(">I", exact(4))[0]
    require(0 < size <= maximum, "MPT_FRAME_LIMIT")
    return exact(size)


def checked(info, *, directory=False):
    require(
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid == info.st_gid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
        and (directory or info.st_nlink == 1),
        "MPT_PRIVATE_NODE",
    )


def read_at(directory, name, maximum):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        checked(info)
        require(info.st_size <= maximum, "MPT_FILE_LIMIT")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        require(
            len(raw) == info.st_size
            and (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
            "MPT_FILE_CHANGED",
        )
        return raw
    finally:
        os.close(fd)


def write_at(directory, name, raw):
    require(name and "/" not in name and name not in {".", ".."}, "MPT_FILE_NAME")
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


def open_root(proof):
    require(
        type(proof) is dict
        and set(proof) == {"path", "owner", "directory", "marker_sha256"}
        and proof["path"] == ROOT_PATH
        and proof["owner"] == OWNER,
        "MPT_ROOT_PROOF",
    )
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(ROOT_PATH).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            node = os.fstat(descriptor)
            require(
                stat.S_ISDIR(node.st_mode)
                and node.st_uid in {0, 1000}
                and not stat.S_IMODE(node.st_mode) & 0o022,
                "MPT_ROOT_ANCESTOR",
            )
        node = os.fstat(descriptor)
        checked(node, directory=True)
        identity = {"device": node.st_dev, "inode": node.st_ino}
        marker = read_at(descriptor, MARKER, 4096)
        require(
            identity == proof["directory"]
            and sha(marker) == proof["marker_sha256"]
            and marker
            == canonical({"schema": 1, "owner": OWNER, "path": ROOT_PATH, "directory": identity}),
            "MPT_ROOT_IDENTITY",
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def host_guard():
    require(
        sys.platform.startswith("linux")
        and os.geteuid() in {0, 1000}
        and socket.gethostname() == "vm-media-01",
        "MPT_HOST",
    )
    import fcntl

    found = False
    interfaces = socket.if_nameindex()
    require(len(interfaces) <= 64, "MPT_HOST_INTERFACES")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
        for _, name in interfaces:
            try:
                observed = fcntl.ioctl(
                    handle.fileno(), 0x8915, struct.pack("256s", name.encode()[:15])
                )
                found |= socket.inet_ntoa(observed[20:24]) == "192.168.0.85"
            except OSError:
                pass
    require(found, "MPT_HOST_IP")


def fixtures(core, bundle, work):
    code = core.decode(bundle["code"]["test_moneyprinter_speaches_acceptance.py"])
    module = types.ModuleType("test_moneyprinter_speaches_acceptance")
    module.__file__ = str(work / "tests/test_moneyprinter_speaches_acceptance.py")
    module.CORE, module.BUNDLE = core, bundle
    sys.modules[module.__name__] = module
    try:
        exec(compile(code, module.__file__, "exec"), module.__dict__)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        expected = suite.countTestCases()
        require(expected == 22, "MPT_FIXTURE_COUNT")

        class Capture(io.StringIO):
            def __init__(self):
                super().__init__()
                self.bytes_written = 0

            def write(self, value):
                self.bytes_written += len(value.encode())
                require(self.bytes_written <= 1024**2, "MPT_FIXTURE_LOG_LIMIT")
                return super().write(value)

        capture = Capture()
        result = unittest.TextTestRunner(stream=capture, verbosity=2).run(suite)
        raw = capture.getvalue().encode()
        require(len(raw) <= 1024**2, "MPT_FIXTURE_LOG_LIMIT")
        with (work / "fixture-tests.log").open("xb") as stream:
            stream.write(raw)
        require(
            result.wasSuccessful()
            and result.testsRun == expected
            and not result.skipped
            and not result.expectedFailures
            and not result.unexpectedSuccesses,
            "MPT_FIXTURE_FAILED",
        )
        return {
            "success": True,
            "tests_passed": expected,
            "skipped": 0,
            "log_sha256": sha(raw),
            "simulated_silence": True,
            "socketpair_only": True,
            "http_real": False,
            "srt_linux_probe": True,
        }
    finally:
        sys.modules.pop(module.__name__, None)


def http_probe(core, bundle, work, token):
    require(type(token) is bytes and 32 <= len(token) <= 256, "MPT_TOKEN_FRAME")
    descriptor = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    token_info, token_written = None, False
    try:
        secret_fd = os.open(
            "bearer",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        try:
            token_info = os.fstat(secret_fd)
            checked(token_info)
            with os.fdopen(secret_fd, "wb", closefd=False) as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            token_written = True
        finally:
            os.close(secret_fd)
        wrong = b"TEST_ONLY_INVALID_BEARER_0000000000000000000000000000000000000000"
        write_at(descriptor, "wrong-bearer", wrong)
        audio = work / "probe.wav"
        started = time.monotonic()
        with core.modules(bundle) as modules:
            try:
                modules.client.transcribe_audio(
                    audio, work / "wrong-bearer", language="es", total_seconds=15, idle_seconds=5
                )
            except modules.contract.SpeachesError as error:
                require(error.code == "SPEACHES_AUTH_REJECTED", "MPT_WRONG_TOKEN_NOT_401")
            else:
                raise ValueError("MPT_WRONG_TOKEN_ACCEPTED")
            data = modules.client.transcribe_audio(
                audio, work / "bearer", language="es", total_seconds=280, idle_seconds=15
            )
            require(
                data["language"] == "es"
                and len(data["words"]) == 28
                and len(data["segments"]) == 3,
                "MPT_REAL_TRANSCRIPT_COUNT",
            )
            result = core.publish_response(bundle, modules.provider, data, work)
            result.update(
                http_real=True,
                http_requests=2,
                wrong_token_401=True,
                words=28,
                segments=3,
                elapsed_seconds=round(time.monotonic() - started, 3),
                endpoint="http://192.168.0.6:8000/v1/audio/transcriptions",
                model=modules.client.MODEL,
                real_silence_tested=False,
                response_sha256=core.sha(core.canonical(data)),
                srt_linux_probe=core.srt_probe(bundle, work),
            )
            return result
    finally:
        try:
            if token_info is not None:
                actual = os.stat("bearer", dir_fd=descriptor, follow_symlinks=False)
                require(
                    (actual.st_dev, actual.st_ino, actual.st_nlink)
                    == (token_info.st_dev, token_info.st_ino, 1)
                    and (
                        read_at(descriptor, "bearer", 256) == token
                        if token_written
                        else token.startswith(read_at(descriptor, "bearer", 256))
                    ),
                    "MPT_TOKEN_CHANGED",
                )
                os.unlink("bearer", dir_fd=descriptor)
        finally:
            os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", choices=["fixtures", "http"])
    parser.add_argument("--bundle-sha256")
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "owner": OWNER,
                    "root": ROOT_PATH,
                    "marker": MARKER,
                    "default": "plan_sin_lectura_stdin_secretos_o_red",
                    "modes": ["fixtures", "http"],
                    "requires_root_prepared_identity": True,
                    "application_imported": False,
                    "sdk_executed": False,
                },
                sort_keys=True,
            )
        )
        return 0

    def expired(_sig, _frame):
        raise TimeoutError("MPT_ACCEPTANCE_DEADLINE")

    def terminated(_sig, _frame):
        raise InterruptedError("MPT_ACCEPTANCE_CANCELLED")

    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, terminated)
    signal.alarm(45 if args.execute == "fixtures" else 310)
    root_fd = run_fd = None
    output = {"success": False, "error": "MPT_ACCEPTANCE_REJECTED"}
    try:
        host_guard()
        stream = sys.stdin.buffer.raw
        proof_raw = frame(stream, 4096)
        raw = frame(stream, MAX_BUNDLE)
        require(sha(raw) == args.bundle_sha256, "MPT_BUNDLE_PIN")
        # El hash externo liga también los bytes del núcleo: no se consulta un loader/pyc.
        preliminary = json.loads(raw)
        code_row = preliminary["code"]["accept_speaches.py"]
        code = base64.b64decode(code_row["data"], validate=True)
        require(sha(code) == code_row["sha256"], "MPT_CORE_PIN")
        core = types.ModuleType("moneyprinter_acceptance_core")
        core.__file__ = ROOT_PATH + "/accept_speaches.py"
        exec(compile(code, core.__file__, "exec"), core.__dict__)
        bundle = core.strict_json(raw)
        core.validate_bundle(bundle)
        require(
            core.decode(bundle["code"]["accept_speaches_remote.py"])
            == core.read_regular(Path(__file__)),
            "MPT_REMOTE_PIN",
        )
        proof = core.strict_json(proof_raw)
        root_fd = open_root(proof)
        name = args.execute + "-v1"
        os.umask(0o077)
        os.mkdir(name, 0o700, dir_fd=root_fd)
        run_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        checked(os.fstat(run_fd), directory=True)
        node = os.fstat(run_fd)
        identity = {"device": node.st_dev, "inode": node.st_ino}
        work = Path(ROOT_PATH) / name
        write_at(
            run_fd,
            ".owner.json",
            canonical(
                {
                    "owner": OWNER,
                    "mode": args.execute,
                    "directory": identity,
                    "bundle_sha256": sha(raw),
                }
            ),
        )
        for filename, row in bundle["inputs"].items():
            write_at(run_fd, filename, core.decode(row))
        os.mkdir("tmp", 0o700, dir_fd=run_fd)
        old_temp = tempfile.tempdir
        tempfile.tempdir = str(work / "tmp")
        os.environ.clear()
        os.environ.update(
            PATH="/usr/bin:/bin",
            HOME=str(work),
            LANG="C.UTF-8",
            TMPDIR=str(work / "tmp"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        try:
            result = (
                fixtures(core, bundle, work)
                if args.execute == "fixtures"
                else http_probe(core, bundle, work, frame(stream, 256))
            )
        finally:
            tempfile.tempdir = old_temp
        for filename, row in bundle["inputs"].items():
            require(
                sha(read_at(run_fd, filename, MAX_BUNDLE)) == row["sha256"], "MPT_MATERIAL_CHANGED"
            )
        require(not list((work / "tmp").iterdir()), "MPT_TEMP_LEFTOVER")
        output = {
            "success": True,
            "owner": OWNER,
            "mode": args.execute,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "bundle_sha256": sha(raw),
            "root": proof,
            "run_path": str(work),
            "directory": identity,
            "result": result,
            "application_imported": False,
            "docker_executed": False,
            "sdk_executed": False,
            "real_secrets_recorded": False,
            "evidence_removed": False,
        }
        write_at(run_fd, "receipt.json", canonical(output))
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except BaseException:
        if run_fd is not None:
            try:
                write_at(run_fd, "failure.json", canonical(output))
            except (OSError, ValueError):
                pass
        print(json.dumps(output))
        return 1
    finally:
        if run_fd is not None:
            os.close(run_fd)
        if root_fd is not None:
            os.close(root_fd)
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
