import hashlib
import io
import subprocess
import sys

import pytest

from docker.moneyprinter import functional_wolfi_video_probe as probe


def mounts(*, subtree_source=False):
    return "\n".join(
        f"{index} 1 0:70 {root} /run/mpt/storage/{area} rw - nfs4 {source} rw,vers=4.1,proto=tcp"
        for index, area in enumerate(probe.NAS_INODES, 10)
        for subtree in (f"{probe.NAS_ROOT}/{area}",)
        for root, source in (
            ("/", probe.NAS_SHARE + subtree) if subtree_source else (subtree, probe.NAS_SHARE),
        )
    )


def test_persistent_mount_requires_exact_api_nfs_bind():
    assert set(probe.persistent_mounts(mounts())) == set(probe.NAS_INODES)
    assert probe.persistent_mounts(mounts(subtree_source=True)) == probe.persistent_mounts(mounts())
    for wrong in (
        mounts().replace("/api/", "/ui/"),
        mounts().replace("vers=4.1", "vers=4.0"),
        mounts(subtree_source=True).replace("/api/", "/ui/"),
        mounts().replace("proto=tcp", "proto=udp"),
        mounts().replace("192.168.0.8", "192.168.0.6"),
        mounts() + "\n" + mounts(),
    ):
        with pytest.raises(probe.ProbeFailure, match="MPT_PROBE_NAS_MOUNT"):
            probe.persistent_mounts(wrong)


def test_framing_accepts_exact_wav_and_rejects_extra_input(monkeypatch):
    raw = b"a" * probe.WAV_BYTES
    monkeypatch.setattr(probe, "WAV_SHA", hashlib.sha256(raw).hexdigest())
    assert probe.read_wav(io.BytesIO(raw)) == raw
    for invalid in (raw + b"!", raw[:-1], b"b" + raw[1:]):
        with pytest.raises(probe.ProbeFailure, match="MPT_PROBE_WAV_PIN"):
            probe.read_wav(io.BytesIO(invalid))


def test_persistent_mode_uses_new_id_and_preserves_trial():
    with pytest.raises(probe.ProbeFailure, match="MPT_PROBE_TASK_ID"):
        probe.task_id_ok("wolfi-video-probe-v1", persistent=True)
    with pytest.raises(probe.ProbeFailure, match="MPT_PROBE_TASK_ID"):
        probe.task_id_ok("../name", persistent=True)
    probe.task_id_ok("wolfi-video-probe-v1", persistent=False)
    probe.task_id_ok("wolfi-video-probe-v2", persistent=True)


def test_loader_keeps_framed_wav_on_same_stdin_for_child():
    source = b"import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    payload = b"RIFF\x00\x01\x02"
    frame = f"{len(source):06d}\n".encode("ascii") + source + payload
    child = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", probe.LOADER],
        input=frame,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert child.stdout == payload and not child.stderr
