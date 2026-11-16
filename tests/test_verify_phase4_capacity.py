import importlib.util
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).parents[1] / "docker" / "verify_phase4_capacity.py"
SPEC = importlib.util.spec_from_file_location("verify_phase4_capacity", SOURCE)
capacity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity)

MOUNTINFO = (
    "20 1 8:2 / / rw - ext4 /dev/sda2 rw\n"
    "21 20 8:3 / /var/lib/docker rw - xfs /dev/sda3 rw\n"
    "22 20 0:17 / /mnt/producciones rw - nfs4 "
    "192.168.0.8:/volume2/producciones rw,vers=4.1,proto=tcp\n"
)


def test_report_counts_shared_local_filesystem_once(monkeypatch):
    descriptors = {str(path): index for index, path in enumerate(capacity.LOCAL_PATHS, 1)}
    devices = {1: 2, 2: 3, 3: 2}
    sampled = []

    def measure(fd):
        sampled.append(fd)
        return SimpleNamespace(
            f_blocks=100,
            f_bfree=30,
            f_bavail=25,
            f_frsize=4096,
            f_files=1000,
            f_ffree=300,
            f_favail=275,
        )

    fs = SimpleNamespace(
        open=lambda path, flags: descriptors[str(path)],
        fstat=lambda fd: SimpleNamespace(st_dev=devices[fd]),
        fstatvfs=measure,
        close=lambda fd: None,
        major=lambda device: 8,
        minor=lambda device: device,
        O_RDONLY=1,
        O_DIRECTORY=2,
        O_NOFOLLOW=4,
    )
    monkeypatch.setattr(capacity, "os", fs)

    result = capacity.report(MOUNTINFO)

    assert result["success"] and result["nas"]["verified"]
    assert sampled == [1, 2]
    assert result["paths"]["/"]["device"] == result["paths"][str(capacity.STAGING)]["device"]
    assert result["paths"]["/var/lib/docker"]["source"] == "/dev/sda3"
    assert result["filesystems"]["8:2"] == {
        "bytes": {"total": 409600, "used": 286720, "available": 102400},
        "inodes": {"total": 1000, "used": 700, "available": 275},
    }


def test_nas_requires_exact_mount_source_and_nfs_version():
    for text in (
        MOUNTINFO.replace("/volume2/producciones", "/volume2/other"),
        MOUNTINFO.replace("vers=4.1", "vers=4.2"),
        MOUNTINFO.replace("/mnt/producciones", "/mnt/producciones/child"),
    ):
        assert not capacity.nas_identity(capacity.parse_mounts(text))["verified"]
