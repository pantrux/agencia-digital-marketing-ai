"""Sonda por stdin para el pipeline local de MoneyPrinter en el contenedor Wolfi."""

import argparse
import hashlib
import http.client
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path, PurePosixPath

APP = Path("/MoneyPrinterTurbo")
SITE = Path("/opt/moneyprinter/site")
ROOT = Path("/run/mpt")
WAV = ROOT / "fixtures/whisper-probe.wav"
WAV_BYTES = 493624
FONT = "LiberationSans-Regular.ttf"
FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"
SPEACHES = ("192.168.0.6", 8000)
API = ("127.0.0.1", 8080)
WAV_SHA = "00bf9721fcfbe2dfd69780d912048c969098d693c8ee25216a8b4f49da94fb99"
FONT_SHA = "559f38bdfcf12beead8a744579715dc7249d6978616e757de7d8cf06403aa531"
NAS_ROOT = "/_moneyprinter/api"
NAS_SHARE = "192.168.0.8:/volume2/producciones"
NAS_INODES = {"tasks": 106037331, "local_videos": 106037332}
TASK_ID = re.compile(r"wolfi-video-probe-v[1-9][0-9]{0,3}")
TRIAL_TASK = "wolfi-video-probe-v1"
LOADER = (
    "import sys; s=sys.stdin.buffer; h=s.readline(8); "
    "n=int(h) if len(h)==7 and h.endswith(b'\\n') and h[:-1].isdigit() else 0; "
    "assert 0<n<=65536; b=s.read(n); assert len(b)==n; "
    "exec(compile(b,'moneyprinter-video-probe','exec'),{'__name__':'__main__'})"
)
IMAGE = "sha256:e1f090ca3a3483f70cbbeafd743bc4a89df5edf2de628c4495b316891370e6e4"
DOCKER = ("/usr/bin/docker", "--host", "unix:///run/docker.sock")
HOST_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"}
HOST_NAS = Path("/mnt/producciones/_moneyprinter/api")
HOST_GUARD = Path("/opt/media-platform/moneyprinter-lan-v1/lan_guard.py")
OBSERVE = (
    '{"id":{{json .Id}},"image":{{json .Image}},"status":{{json .State.Status}},'
    '"pid":{{json .State.Pid}},"started":{{json .State.StartedAt}},"restarts":{{json .RestartCount}},'
    '"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"role":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"owner":{{json (index .Config.Labels "agency.owner")}}}'
)
APP_PINS = {
    "app/services/task.py": "bdc1254417ac10db9b9983d2565fd990ed1692cfb387c9dd5e0aeb4cfc4e1c17",
    "app/services/video.py": "7f7ebac91126c3f854534e453c8e215b6cdde9b192857bdfc890d308b654e8fa",
    "app/services/subtitle.py": "f36bd2d53ad40b1b6a1fab5a1651ff60b043d36bba47b1037d7ed349d8617ad4",
}


class ProbeFailure(Exception):
    pass


class ProbeTimeout(BaseException):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def regular(path, maximum, read=False):
    require(path.resolve(strict=True) == path, "MPT_PROBE_PATH")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= maximum,
            "MPT_PROBE_FILE",
        )
        raw = stream.read(maximum + 1) if read else None
        digest = hashlib.sha256(raw) if read else hashlib.file_digest(stream, "sha256")
    return {"bytes": info.st_size, "sha256": digest.hexdigest()}, raw


def private_directory(path, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == info.st_gid == 65532
        and not info.st_mode & 0o022
        and path.resolve(strict=True) == path,
        "MPT_PROBE_DIRECTORY",
    )


def task_id_ok(task_id, *, persistent):
    require(
        TASK_ID.fullmatch(task_id) and (not persistent or task_id != TRIAL_TASK),
        "MPT_PROBE_TASK_ID",
    )


def read_wav(stream):
    raw = stream.read(WAV_BYTES + 1)
    require(
        len(raw) == WAV_BYTES and hashlib.sha256(raw).hexdigest() == WAV_SHA, "MPT_PROBE_WAV_PIN"
    )
    return raw


def persistent_mounts(raw):
    mounts = {}
    targets = {str(PurePosixPath("/run/mpt/storage") / area): area for area in NAS_INODES}
    for line in raw.splitlines():
        left, sep, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if len(fields) < 6 or fields[4] not in targets:
            continue
        area = targets[fields[4]]
        subtree = f"{NAS_ROOT}/{area}"
        # Linux puede presentar el mismo subárbol como raíz del bind o como origen NFS.
        require(
            sep
            and len(filesystem) >= 3
            and area not in mounts
            and re.fullmatch(r"[0-9]+:[0-9]+", fields[2])
            and "rw" in fields[5].split(",")
            and filesystem[0] == "nfs4"
            and (fields[3], filesystem[1]) in {(subtree, NAS_SHARE), ("/", NAS_SHARE + subtree)}
            and {"rw", "vers=4.1", "proto=tcp"} <= set(filesystem[2].split(",")),
            "MPT_PROBE_NAS_MOUNT",
        )
        mounts[area] = tuple(map(int, fields[2].split(":")))
    require(mounts.keys() == NAS_INODES.keys(), "MPT_PROBE_NAS_MOUNT")
    return mounts


def check_persistent_storage(storage):
    private_directory(storage)
    mounts = persistent_mounts(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
    for area, inode in NAS_INODES.items():
        path = storage / area
        info = path.lstat()
        require(
            path.resolve(strict=True) == path
            and stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
            == (os.makedev(*mounts[area]), inode, 1024, 100)
            and stat.S_IMODE(info.st_mode) in (0o700, 0o777),
            "MPT_PROBE_NAS_NODE",
        )


def restrict_network(event, args):
    if event == "socket.connect":
        require(args[1] in (SPEACHES, API), "MPT_PROBE_EGRESS")
    if event == "socket.getaddrinfo":
        require(args[:2] in (SPEACHES, API), "MPT_PROBE_EGRESS")
    if event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
        raise ProbeFailure("MPT_PROBE_EGRESS")


def make_material(path):
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x17243a:s=480x480:r=30:d=12",
            "-an",
            "-threads",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-n",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=25,
    )


def verify_video(path):
    media, _ = regular(path, 32 * 1024**2)
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,duration:format=duration,format_name",
            "-of",
            "json",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=15,
    )
    details = json.loads(completed.stdout)
    streams = {stream["codec_type"]: stream for stream in details["streams"]}
    require(
        len(details["streams"]) == 2 and streams.keys() == {"video", "audio"}, "MPT_PROBE_STREAMS"
    )
    video, audio = streams["video"], streams["audio"]
    duration = float(details["format"]["duration"])
    require(
        (
            video["codec_name"],
            audio["codec_name"],
            video["width"],
            video["height"],
            video["avg_frame_rate"],
        )
        == ("h264", "aac", 1080, 1080, "30/1")
        and "mp4" in details["format"]["format_name"].split(",")
        and 10.9 <= duration <= 12.1
        and 10.9 <= float(audio["duration"]) <= 12.1,
        "MPT_PROBE_VIDEO_CONTRACT",
    )
    return {
        **media,
        "duration_seconds": duration,
        "width": 1080,
        "height": 1080,
        "fps": 30,
        "video_codec": "h264",
        "audio_codec": "aac",
    }


def verify_cues(raw, duration):
    blocks = re.split(r"\r?\n\s*\r?\n", raw.decode("utf-8").strip())
    require(len(blocks) == 28, "MPT_PROBE_CUE_COUNT")
    clock = r"(\d{2}):([0-5]\d):([0-5]\d),(\d{3})"
    previous = 0
    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        require(len(lines) == 3 and lines[0] == str(index), "MPT_PROBE_CUE_NUMBER")
        match = re.fullmatch(clock + r" --> " + clock, lines[1])
        require(match is not None and len(lines[2].split()) == 1, "MPT_PROBE_CUE_FORMAT")
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, match.groups())
        start = ((h1 * 60 + m1) * 60 + s1) * 1000 + ms1
        end = ((h2 * 60 + m2) * 60 + s2) * 1000 + ms2
        require(previous <= start < end <= duration * 1000, "MPT_PROBE_CUE_TIMELINE")
        previous = start
    return len(blocks)


def verify_download(task_id, filename, local, key):
    require(
        TASK_ID.fullmatch(task_id)
        and filename in {"subtitle.srt", "final-1.mp4"}
        and 0 < local["bytes"] <= 32 * 1024**2,
        "MPT_PROBE_DOWNLOAD_INPUT",
    )
    route = f"/tasks/{task_id}/{filename}"
    headers = {"Accept-Encoding": "identity"}
    with closing(http.client.HTTPConnection(*API, timeout=5)) as connection:
        connection.request("GET", route, headers=headers)
        with connection.getresponse() as response:
            anonymous = response.status
            require(anonymous == 401, "MPT_PROBE_DOWNLOAD_ANONYMOUS")
    with closing(http.client.HTTPConnection(*API, timeout=5)) as connection:
        connection.request("GET", route, headers={**headers, "x-api-key": key})
        with connection.getresponse() as response:
            authenticated = response.status
            require(authenticated == 200, "MPT_PROBE_DOWNLOAD_AUTHENTICATED")
            require(
                response.getheader("Content-Length") == str(local["bytes"])
                and response.getheader("Content-Encoding") in (None, "identity"),
                "MPT_PROBE_DOWNLOAD_HEADERS",
            )
            digest = hashlib.sha256()
            remaining = local["bytes"]
            while remaining:
                chunk = response.read(min(remaining, 65536))
                require(0 < len(chunk) <= remaining, "MPT_PROBE_DOWNLOAD_SIZE")
                digest.update(chunk)
                remaining -= len(chunk)
            require(response.read(1) == b"", "MPT_PROBE_DOWNLOAD_SIZE")
            require(digest.hexdigest() == local["sha256"], "MPT_PROBE_DOWNLOAD_HASH")
    return {"anonymous": anonymous, "authenticated": authenticated, "identical": True}


def probe(task_id, *, wav_stream=None):
    persistent = wav_stream is not None
    require(
        sys.platform == "linux"
        and sys.flags.isolated
        and sys.flags.no_site
        and os.geteuid() == os.getegid() == 65532
        and os.environ.get("MPT_HOME") == str(ROOT),
        "MPT_PROBE_RUNTIME",
    )
    task_id_ok(task_id, persistent=persistent)
    os.umask(0o077)
    private_directory(ROOT)
    config_path = ROOT / "config.toml"
    config_info = config_path.lstat()
    require(
        stat.S_ISREG(config_info.st_mode)
        and stat.S_IMODE(config_info.st_mode) == 0o600
        and config_info.st_uid == config_info.st_gid == 65532
        and config_info.st_nlink == 1,
        "MPT_PROBE_PRIVATE_CONFIG",
    )
    for relative, expected in APP_PINS.items():
        require(regular(APP / relative, 128 * 1024)[0]["sha256"] == expected, "MPT_PROBE_APP_PIN")
    if persistent:
        raw = read_wav(wav_stream)
        font = ROOT / "resource/fonts" / FONT
        require(regular(font, 1024**2)[0]["sha256"] == FONT_SHA, "MPT_PROBE_FONT_PIN")
    else:
        wav, raw = regular(WAV, WAV_BYTES, read=True)
        require(wav == {"bytes": WAV_BYTES, "sha256": WAV_SHA}, "MPT_PROBE_WAV_PIN")
    storage = ROOT / "storage"
    tasks, materials = storage / "tasks", storage / "local_videos"
    if persistent:
        check_persistent_storage(storage)
    else:
        for path in (storage, tasks, materials):
            private_directory(path, create=True)
    folder, local = tasks / task_id, materials / task_id
    require(not os.path.lexists(folder) and not os.path.lexists(local), "MPT_PROBE_TASK_EXISTS")
    folder.mkdir(mode=0o700)
    local.mkdir(mode=0o700)
    narration = folder / "narration.wav"
    with narration.open("xb") as stream:
        stream.write(raw)
    material = local / "synthetic.mp4"
    make_material(material)

    # Los temporales y resultados pertenecen sólo a esta tarea.
    os.chdir(folder)
    os.environ.update(
        PATH="/usr/bin:/bin",
        TMPDIR=str(folder),
        TMP=str(folder),
        TEMP=str(folder),
        FFMPEG_BINARY=FFMPEG,
        IMAGEIO_FFMPEG_EXE=FFMPEG,
        PYTHON_DOTENV_DISABLED="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(APP), str(SITE)]
    sys.addaudithook(restrict_network)

    from app.config import config

    key = config.app.get("api_key")
    require(
        isinstance(key, str)
        and 0 < len(key) <= 256
        and key.isascii()
        and key.isprintable()
        and key == key.strip(),
        "MPT_PROBE_DOWNLOAD_CONFIG",
    )
    require(
        config.app.get("subtitle_provider") == "speaches"
        and config.app.get("subtitle_speaches_token_file") == "/run/secrets/moneyprinter-speaches"
        and config.app.get("enable_redis", False) is False
        and config.app.get("upload_post_auto_upload", False) is False
        and (config.app.get("video_codec") or "libx264") == "libx264"
        and config.app.get("ffmpeg_path", "") in {"", FFMPEG},
        "MPT_PROBE_CONFIG",
    )
    from app.models import const
    from app.models.schema import MaterialInfo, VideoParams
    from app.services import state, task
    from app.utils import utils

    font = Path(utils.font_dir()) / FONT
    require(
        font == ROOT / "resource/fonts" / FONT and regular(font, 1024**2)[0]["sha256"] == FONT_SHA,
        "MPT_PROBE_FONT_PIN",
    )
    params = VideoParams(
        video_subject="Local video smoke test",
        video_script="Local narration fixture for the Speaches video pipeline.",
        video_terms=["local synthetic clip"],
        custom_audio_file=narration.name,
        video_source="local",
        video_materials=[MaterialInfo(provider="local", url=f"{task_id}/{material.name}")],
        video_aspect="1:1",
        video_concat_mode="sequential",
        video_clip_duration=12,
        video_count=1,
        n_threads=1,
        bgm_type="",
        bgm_volume=0,
        subtitle_enabled=True,
        subtitle_display_mode="word_by_word",
        subtitle_animation="none",
        subtitle_position="bottom",
        font_name=FONT,
        font_size=42,
    )
    started = time.monotonic()
    result = task.start(task_id, params, stop_at="video")
    recorded = state.state.get_task(task_id) or {}
    if recorded.get("state") != const.TASK_STATE_COMPLETE or recorded.get("progress") != 100:
        stage = recorded.get("failed_stage")
        known = {"preflight", "script", "audio", "subtitle", "materials", "video", "pipeline"}
        error = recorded.get("error")
        public = (
            error
            if isinstance(error, str) and re.fullmatch(r"MPT_SPEACHES_[A-Z_]+", error)
            else None
        )
        return {"success": False, "stage": stage if stage in known else "pipeline", "code": public}

    final, combined, captions = (
        folder / "final-1.mp4",
        folder / "combined-1.mp4",
        folder / "subtitle.srt",
    )
    require(
        result["videos"] == [str(final)]
        and result["combined_videos"] == [str(combined)]
        and result["subtitle_path"] == str(captions)
        and result["audio_file"] == str(narration)
        and result["materials"] == [str(material)]
        and result["cross_post_state"] is None,
        "MPT_PROBE_OUTPUT_PATHS",
    )
    regular(combined, 32 * 1024**2)
    mp4 = verify_video(final)
    srt_info, raw = regular(captions, 64 * 1024, read=True)
    cue_count = verify_cues(raw, mp4["duration_seconds"])
    srt_download = verify_download(task_id, captions.name, srt_info, key)
    mp4_download = verify_download(task_id, final.name, mp4, key)
    return {
        "success": True,
        "task_id": task_id,
        "pipeline": "task.start/local",
        "seconds": round(time.monotonic() - started, 3),
        "uid": os.geteuid(),
        "subtitle_provider": "speaches",
        "word_cues": cue_count,
        "mp4": {**mp4, "path": str(final), "download": mp4_download},
        "srt": {**srt_info, "path": str(captions), "download": srt_download},
    }


def main():
    def timed_out(*_):
        raise ProbeTimeout

    # El JSON es el único stdout. La aplicación y sus procesos no heredan ese descriptor.
    with os.fdopen(os.dup(1), "w", encoding="utf-8") as output, open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)
        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(220)
        try:
            if len(sys.argv) == 3 and sys.argv[1] == "--persistent":
                result = probe(sys.argv[2], wav_stream=sys.stdin.buffer)
            else:
                require(len(sys.argv) == 1, "MPT_PROBE_ARGUMENTS")
                result = probe(TRIAL_TASK)
        except ProbeTimeout:
            result = {"success": False, "code": "MPT_PROBE_TIMEOUT"}
        except ProbeFailure as error:
            result = {"success": False, "code": error.args[0]}
        except Exception as error:
            frame = traceback.extract_tb(error.__traceback__)[-1]
            result = {
                "success": False,
                "error": type(error).__name__,
                "module": Path(frame.filename).name,
                "line": frame.lineno,
            }
        finally:
            signal.alarm(0)
        print(json.dumps({"agency_probe": result}, sort_keys=True), file=output)
    return 0 if result["success"] else 1


def host_command(*args, payload=None, timeout=15):
    return subprocess.run(
        args, input=payload, capture_output=True, env=HOST_ENV, timeout=timeout, check=False
    )


def observe_containers(api, ui):
    command = host_command(*DOCKER, "inspect", "--type", "container", "--format", OBSERVE, api, ui)
    require(
        command.returncode == 0 and not command.stderr and len(command.stdout) <= 4096,
        "MPT_PROBE_INSPECT",
    )
    rows = [json.loads(line) for line in command.stdout.splitlines()]
    require(
        len(rows) == 2 and {row["role"]: row["id"] for row in rows} == {"api": api, "ui": ui},
        "MPT_PROBE_CIDS",
    )
    require(
        all(
            row["image"] == IMAGE
            and row["status"] == "running"
            and row["health"] == "healthy"
            and type(row["pid"]) is int
            and row["pid"] > 0
            and row["project"] == "agency-moneyprinter-lan"
            and row["owner"] == "agencia-digital-marketing-ai:moneyprinter-lan:v1"
            for row in rows
        ),
        "MPT_PROBE_CID_STATE",
    )
    return {row["role"]: row for row in rows}


def persistent_exec(args):
    require(
        sys.platform == "linux" and os.geteuid() == 0 and os.uname().nodename == "vm-media-01",
        "MPT_PROBE_HOST",
    )
    require(
        all(re.fullmatch(r"[a-f0-9]{64}", cid) for cid in (args.cid, args.ui_cid))
        and args.cid != args.ui_cid,
        "MPT_PROBE_CIDS",
    )
    task_id_ok(args.task_id, persistent=True)
    guard = host_command(
        "/usr/bin/python3", "-I", "-S", "-B", str(HOST_GUARD), "--verify-only", timeout=45
    )
    require(
        guard.returncode == 0
        and not guard.stderr
        and json.loads(guard.stdout)
        == {"ok": True, "firewall_changed": False, "verify_only": True},
        "MPT_PROBE_GUARD",
    )
    before = observe_containers(args.cid, args.ui_cid)
    with args.wav.open("rb") as stream:
        wav = read_wav(stream)
    source = Path(__file__).resolve().read_bytes()
    require(0 < len(source) <= 65536, "MPT_PROBE_SOURCE")
    payload = f"{len(source):06d}\n".encode("ascii") + source + wav
    completed = host_command(
        *DOCKER,
        "exec",
        "-i",
        "--user",
        "65532:65532",
        args.cid,
        "/usr/bin/python3.12",
        "-I",
        "-S",
        "-B",
        "-c",
        LOADER,
        "--persistent",
        args.task_id,
        payload=payload,
        timeout=245,
    )
    require(not completed.stderr and 0 < len(completed.stdout) <= 4096, "MPT_PROBE_EXEC")
    row = json.loads(completed.stdout)["agency_probe"]
    require(observe_containers(args.cid, args.ui_cid) == before, "MPT_PROBE_CID_CHANGED")
    if not row["success"]:
        code = row.get("code")
        require(
            isinstance(code, str) and re.fullmatch(r"MPT_[A-Z0-9_]+", code), "MPT_PROBE_PIPELINE"
        )
        raise ProbeFailure(code)
    require(completed.returncode == 0 and row["task_id"] == args.task_id, "MPT_PROBE_RESULT")
    for key, name in (("mp4", "final-1.mp4"), ("srt", "subtitle.srt")):
        physical = HOST_NAS / "tasks" / args.task_id / name
        observed, _ = regular(physical, 32 * 1024**2)
        require(
            all(observed[field] == row[key][field] for field in ("bytes", "sha256")),
            "MPT_PROBE_NAS_OUTPUT",
        )
    return {
        "success": True,
        "mode": "persistent",
        "container_state_stable": True,
        "nas_output_hashes_verified": True,
        "agency_probe": row,
    }


def host_main():
    parser = argparse.ArgumentParser(
        description="Prueba sintética contra el contenedor API vigente de MoneyPrinter."
    )
    parser.add_argument("--persistent-exec", action="store_true", required=True)
    parser.add_argument("--cid", required=True)
    parser.add_argument("--ui-cid", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--wav", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = persistent_exec(args)
    except ProbeFailure as error:
        result = {"success": False, "code": error.args[0]}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result = {"success": False, "error": type(error).__name__}
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(host_main() if sys.argv[1:2] == ["--persistent-exec"] else main())
