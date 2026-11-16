"""Instala la proyección FFmpeg 9/OpenCV de SupoClip y prueba su runtime."""

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

INPUTS = Path("/inputs")
PRODUCTS = Path("/products")
ROOT = Path("/opt/supoclip")
LIB = Path("/usr/lib")
BIN = Path("/usr/bin")
RECORD = ROOT / "bin/media-runtime-v2.json"
LEGACY_SHA = "499317e2bea45a6962b3ee0a61eefdbdf8ecc775bd0797a37a393c3a5293999f"
SOURCE_SHA = "787707a963609f8e1f33b8c7ca6bd58a49924b59718c0412d5bd5a82ec069c2e"
SOURCE_RECEIPT = "9148e1efb0181241f1e0523de55d01533010f1068e87890731b45c82df84f7ca"
FFMPEG_RECEIPT = "f4cbdbac5879e485333b72a904195e74d502583cdcf692c2215e84ef14aeec4c"
FRIBIDI_SHA = "a56243d2535661a230e90ae2be78c99613a28c17df533626a2ffe874ce557dd6"
OPENCV_PROBE_SHA = "fb5a6c1a30b04bb2a0eaeb75e16ae2eff48a48a786482d74a3257c58fa229b1c"
ASS_PROBE_SHA = "c0c9927d38b2fecc9d02f80bda25aec36da0d95975cf7b30ca99975d2ba32ae3"
WHEEL_VERSION = "4.13.0.92+agency2"
WHEEL = f"opencv_python-{WHEEL_VERSION}-cp312-cp312-linux_x86_64.whl"
SONAMES = {
    "libavcodec.so.63",
    "libavdevice.so.63",
    "libavfilter.so.12",
    "libavformat.so.63",
    "libavutil.so.61",
    "libswresample.so.7",
    "libswscale.so.10",
}
MEDIA = {"bin/ffmpeg", "bin/ffprobe"} | {f"lib/{name}.1.102" for name in SONAMES}
PRODUCT_FILES = {
    "backend-source.tar",
    "backend-source-receipt.json",
    "ffmpeg-receipt.json",
    "verify_opencv_expanded.py",
    "ass-fixture.ttf",
    "media",
    "wheel",
}
FFMPEG_FAMILY = re.compile(
    r"lib(?:avcodec|avdevice|avfilter|avformat|avutil|swresample|swscale|postproc)\.so(?:\..*)?$"
)
MIB = 1024**2


def require(condition, label):
    if not condition:
        raise ValueError(label)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def pin(value):
    return isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value) is not None


def load_legacy():
    raw = Path(__file__).with_name("backend_runtime.py").read_bytes()
    require(sha(raw) == LEGACY_SHA, "HELPER_V1_SHA")
    module = types.ModuleType("supoclip_backend_v1")
    exec(compile(raw, "<supoclip-backend-v1>", "exec"), module.__dict__)
    return module


def media_rows(receipt):
    require(
        receipt["success"] is True
        and receipt["status"] == "exited"
        and receipt["owner"] == "agencia-digital-marketing-ai:supoclip-ffmpeg-shared-link-finish:v1"
        and receipt["exit"] == receipt["pid"] == 0
        and receipt["oom"] is False
        and pin(receipt["libass_map_sha256"])
        and set(receipt["sonames"]) == SONAMES
        and set(receipt["products"]) == MEDIA,
        "FFMPEG_RECIBO",
    )
    return receipt["products"]


def artifact(legacy, path, row, maximum):
    require(
        set(row) == {"bytes", "sha256"}
        and type(row["bytes"]) is int
        and 0 < row["bytes"] <= maximum
        and pin(row["sha256"]),
        "PRODUCTO_FICHA",
    )
    raw = legacy.read(path, row["bytes"], row["sha256"])
    require(len(raw) == row["bytes"], "PRODUCTO_TAMANO")
    return raw


def wheel_bytes(legacy, manifest_pin):
    require(pin(manifest_pin), "WHEEL_MANIFEST_SHA")
    root = PRODUCTS / "wheel"
    require(
        not root.is_symlink()
        and {p.name for p in root.iterdir()} == {WHEEL, "wheel.json", "manifest.json"},
        "WHEEL_CONTEXTO",
    )
    raw = legacy.read(root / "manifest.json", 64 * 1024, manifest_pin)
    manifest = json.loads(raw)
    require(
        canonical(manifest) == raw
        and manifest["schema"] == "supoclip-opencv-ffmpeg9-backend-wheel-v2"
        and manifest["wheel_version"] == WHEEL_VERSION
        and manifest["linux_sdk_probe_verified"] is True
        and manifest["backend_runtime_verified"] is False
        and manifest["final_sca_verified"] is False
        and pin(manifest["probe_sha256"])
        and set(manifest["files"]) == {WHEEL, "wheel.json"},
        "WHEEL_MANIFEST",
    )
    info = json.loads(
        artifact(legacy, root / "wheel.json", manifest["files"]["wheel.json"], 64 * 1024)
    )
    row = manifest["files"][WHEEL]
    require(
        info["success"] is True
        and info["wheel"] == f"/work/wheels/{WHEEL}"
        and info["sha256"] == row["sha256"]
        and {"libavcodec.so.63", "libavformat.so.63", "libavutil.so.61", "libswscale.so.10"}
        <= set(info["elf_dependencies"]["cv2/.libs/libopencv_videoio.so.413"]),
        "WHEEL_RECIBO",
    )
    return row, artifact(legacy, root / WHEEL, row, 256 * MIB)


def install_media(legacy):
    receipt = json.loads(legacy.read(PRODUCTS / "ffmpeg-receipt.json", 16 * 1024, FFMPEG_RECEIPT))
    rows = media_rows(receipt)
    root = PRODUCTS / "media"
    require(
        not root.is_symlink()
        and {p.name for p in root.iterdir()} == {"bin", "lib"}
        and all(not (root / leaf).is_symlink() for leaf in ("bin", "lib"))
        and {
            p.relative_to(root).as_posix()
            for leaf in ("bin", "lib")
            for p in (root / leaf).iterdir()
        }
        == MEDIA,
        "FFMPEG_CONTEXTO",
    )
    for relative, row in rows.items():
        raw = artifact(legacy, root / relative, row, 64 * MIB)
        require(
            raw.startswith(b"\x7fELF\x02\x01") and raw[18:20] == b"\x3e\x00", "FFMPEG_ELF_AMD64"
        )
        legacy.write(Path("/usr") / relative, raw, 0o555)
    for name in sorted(SONAMES):
        (LIB / name).symlink_to(f"{name}.1.102")
    legacy.ASS.parent.mkdir(parents=True, mode=0o755)
    legacy.ASS.symlink_to(BIN / "ffmpeg")
    legacy.command(["/usr/bin/ldconfig"], timeout=20)
    return rows


def install_site(legacy, wheel, wheel_sha):
    wheels = INPUTS / "wheelhouse-preopencv.tar"
    legacy.pinned(wheels, 96 * MIB, legacy.WHEELS_TAR, 69949440)
    receipt = json.loads(
        legacy.read(INPUTS / "wheelhouse-receipt.json", 65536, legacy.WHEELS_RECEIPT)
    )
    members = receipt["members"]
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
    legacy.pinned(legacy.PIP, 8 * MIB, legacy.PIP_SHA)
    with tempfile.TemporaryDirectory(prefix="supoclip-pip-v2-") as temp:
        work = Path(temp)
        legacy.unpack(wheels, work, members)
        legacy.write(work / "wheelhouse" / WHEEL, wheel)
        locked = (work / "requirements.preopencv.lock").read_bytes()
        expected = legacy.locked_distributions(locked)
        require(
            len(expected) == 83
            and expected.get("srt") == "3.5.3"
            and not any(name.startswith("opencv-") for name in expected),
            "LOCK_PREOPENCV",
        )
        expected["opencv-python"] = WHEEL_VERSION
        legacy.write(
            work / "requirements.lock",
            locked + f"opencv-python=={WHEEL_VERSION} --hash=sha256:{wheel_sha}\n".encode(),
        )
        legacy.SITE.mkdir(mode=0o755)
        legacy.command(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                f"import runpy,sys;sys.path.insert(0,{str(legacy.PIP)!r});runpy.run_module('pip',run_name='__main__')",
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
                str(legacy.SITE),
                "--requirement",
                str(work / "requirements.lock"),
            ],
            timeout=180,
        )
    packages = list(importlib.metadata.distributions(path=[str(legacy.SITE)]))
    installed = {
        legacy.canonical_name(package.metadata["Name"]): package.version for package in packages
    }
    require(len(packages) == 84 and installed == expected, "SITE_INVENTARIO")


def install(legacy, manifest_pin):
    require(
        os.geteuid() == os.getegid() == 0 and sys.version_info[:3] == (3, 12, 16),
        "BUILD_UID_PYTHON",
    )
    require({p.name for p in PRODUCTS.iterdir()} == PRODUCT_FILES, "PRODUCTOS_COBERTURA")
    os.umask(0o022)
    source = PRODUCTS / "backend-source.tar"
    legacy.pinned(source, 16 * MIB, SOURCE_SHA, 10567680)
    receipt = json.loads(
        legacy.read(PRODUCTS / "backend-source-receipt.json", 65536, SOURCE_RECEIPT)
    )
    files = receipt["archives"]["backend-source.tar"]["members"]
    require(
        len(files) == 177
        and all(name == "LICENSE" or name.startswith("backend/") for name in files),
        "TAR_FUENTES",
    )
    legacy.unpack(source, ROOT, files)
    (legacy.APP / "logs").mkdir(mode=0o755)
    row, wheel = wheel_bytes(legacy, manifest_pin)
    rows = install_media(legacy)
    install_site(legacy, wheel, row["sha256"])
    legacy.write(
        RECORD,
        canonical(
            {
                "source_sha256": SOURCE_SHA,
                "ffmpeg_receipt_sha256": FFMPEG_RECEIPT,
                "wheel_manifest_sha256": manifest_pin,
                "wheel": row,
                "media": rows,
                "fribidi_sha256": FRIBIDI_SHA,
                "apk_packages": 339,
                "python_distributions": 84,
            }
        ),
        0o444,
    )


def runtime_library_map(raw):
    rows = {}
    for name, source in re.findall(rb"(?m)^\s*([^\s]+) => (/[^\s]+) \(0x[a-f0-9]+\)$", raw):
        soname, path = name.decode("ascii"), source.decode("ascii")
        require(soname not in rows, "LOADER_DUPLICADO")
        if FFMPEG_FAMILY.fullmatch(soname):
            require(
                soname in SONAMES and path in {f"/usr/lib/{soname}", f"/lib/{soname}"},
                "LOADER_FFMPEG_ANTERIOR",
            )
        require(not soname.startswith("libass.so"), "LOADER_LIBASS_DINAMICO")
        rows[soname] = path
    require(
        b"not found" not in raw and SONAMES | {"libdav1d.so.7", "libfribidi.so.0"} <= rows.keys(),
        "LOADER_CIERRE",
    )
    return rows


def check_runtime(legacy, data):
    apk = legacy.read(Path("/usr/lib/apk/db/installed"), 16 * MIB)
    names = re.findall(rb"(?m)^P:(.+)$", apk)
    require(
        len(names) == len(set(names)) == 339
        and not any(name.startswith(b"ffmpeg") for name in names)
        and b"fribidi" in names
        and b"libdav1d" in names,
        "APK_RUNTIME",
    )
    allowed = SONAMES | {f"{name}.1.102" for name in SONAMES}
    require(
        {p.name for p in LIB.iterdir() if FFMPEG_FAMILY.fullmatch(p.name)} == allowed,
        "FFMPEG_ARCHIVOS",
    )
    for name in SONAMES:
        require(os.readlink(LIB / name) == f"{name}.1.102", "FFMPEG_ALIAS")
    for root in (Path("/usr/local/lib"), legacy.SITE):
        require(
            not any(FFMPEG_FAMILY.fullmatch(p.name) for p in root.rglob("*")),
            "FFMPEG_COPIA_ALTERNA",
        )
    require(
        not any(os.path.lexists(Path("/usr/local/bin") / name) for name in ("ffmpeg", "ffprobe")),
        "FFMPEG_BIN_ALTERNO",
    )
    require(legacy.ASS.is_symlink() and os.readlink(legacy.ASS) == "/usr/bin/ffmpeg", "ASS_ALIAS")
    legacy.pinned(LIB / "libfribidi.so.0.4.0", MIB, FRIBIDI_SHA, 142568)
    for relative, row in data["media"].items():
        legacy.pinned(Path("/usr") / relative, 64 * MIB, row["sha256"], row["bytes"])
    for name in ("ffmpeg", "ffprobe"):
        loader = legacy.command(
            ["/lib64/ld-linux-x86-64.so.2", "--list", str(BIN / name)], timeout=12, capture=True
        ).stdout
        libs = runtime_library_map(loader)
        require(
            Path(libs["libfribidi.so.0"]).resolve(strict=True) == LIB / "libfribidi.so.0.4.0",
            "FRIBIDI_PROVIDER",
        )
        require(Path(libs["libdav1d.so.7"]).resolve(strict=True).parent == LIB, "DAV1D_PROVIDER")
        version = legacy.command([str(BIN / name), "-version"], timeout=12, capture=True).stdout
        require(
            re.match(rb"^" + name.encode() + rb" version 9\.0\.2(?:\s|$)", version),
            "FFMPEG_VERSION",
        )


def probe(legacy):
    legacy.rootless()
    data = json.loads(legacy.read(RECORD, 16 * 1024))
    check_runtime(legacy, data)
    ass_probe = Path(__file__).with_name("verify_ffmpeg_ass.py")
    legacy.pinned(ass_probe, 64 * 1024, ASS_PROBE_SHA)
    legacy.command(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(ass_probe),
            "--ffmpeg",
            str(legacy.ASS),
            "--ffmpeg-sha256",
            data["media"]["bin/ffmpeg"]["sha256"],
            "--ffprobe",
            str(BIN / "ffprobe"),
            "--ffprobe-sha256",
            data["media"]["bin/ffprobe"]["sha256"],
            "--font",
            str(PRODUCTS / "ass-fixture.ttf"),
        ],
        timeout=80,
    )
    tool = PRODUCTS / "verify_opencv_expanded.py"
    legacy.pinned(tool, 64 * 1024, OPENCV_PROBE_SHA)
    legacy.command(["/usr/bin/node", "--version"], timeout=15, capture=True)
    legacy.command(["/usr/bin/tesseract", "--version"], timeout=15, capture=True)
    with tempfile.TemporaryDirectory(prefix="supoclip-video-v2-") as temp:
        video = Path(temp) / "synthetic.mp4"
        legacy.command(
            [
                str(BIN / "ffmpeg"),
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
        raw = legacy.command(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(tool),
                "--site",
                str(legacy.SITE),
                "--video",
                str(video),
                "--video-sha256",
                sha(video.read_bytes()),
                "--yunet",
                str(legacy.APP / "src/media/models/face_detection_yunet_2023mar.onnx"),
            ],
            timeout=45,
            capture=True,
        ).stdout
        report = json.loads(raw)
        require(
            report["success"] is True
            and all(path.startswith("/usr/lib/") for path in report["ffmpeg9"]),
            "OPENCV_LOADER_RUNTIME",
        )
        print(raw.decode("utf-8").strip())
    print("SUPOCLIP_BACKEND_V2_RUNTIME_PROBE_VERIFIED")


def main():
    args = sys.argv[1:]
    try:
        legacy = load_legacy()
        if len(args) == 2 and args[0] == "install":
            install(legacy, args[1])
        elif args == ["probe"]:
            probe(legacy)
        else:
            raise ValueError("ETAPA")
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        label = str(error)
        print(
            json.dumps(
                {
                    "success": False,
                    "error": label if re.fullmatch(r"[A-Z0-9_]+", label) else type(error).__name__,
                }
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
