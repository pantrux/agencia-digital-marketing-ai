"""Mide bloques e inodos disponibles en los volúmenes locales de vm-media-01."""

import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import NamedTuple

HOST = "vm-media-01"
STAGING = PurePosixPath("/var/lib/media-platform/backup-321-production-staging-v1")
LOCAL_PATHS = (PurePosixPath("/"), PurePosixPath("/var/lib/docker"), STAGING)
NAS = PurePosixPath("/mnt/producciones")
NAS_SOURCE = "192.168.0.8:/volume2/producciones"
NAS_OPTIONS = {"rw", "vers=4.1", "proto=tcp"}
MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class Mount(NamedTuple):
    device: str
    root: str
    point: PurePosixPath
    kind: str
    source: str
    options: frozenset[str]
    super_options: frozenset[str]


def unescape(value):
    return MOUNT_ESCAPE.sub(lambda match: chr(int(match[1], 8)), value)


def parse_mounts(text):
    mounts = []
    for line in text.splitlines():
        first, last = line.split(" - ", 1)
        left, right = first.split(), last.split()
        mounts.append(
            Mount(
                left[2],
                unescape(left[3]),
                PurePosixPath(unescape(left[4])),
                right[0],
                unescape(right[1]),
                frozenset(left[5].split(",")),
                frozenset(right[2].split(",")),
            )
        )
    return mounts


def nas_identity(mounts):
    exact = [mount for mount in mounts if mount.point == NAS]
    if len(exact) == 1:
        mount = exact[0]
        if (
            mount.root == "/"
            and mount.kind == "nfs4"
            and mount.source == NAS_SOURCE
            and "rw" in mount.options
            and NAS_OPTIONS <= mount.super_options
        ):
            return {
                "verified": True,
                "target": str(NAS),
                "source": NAS_SOURCE,
                "filesystem": "nfs4",
                "options": sorted(NAS_OPTIONS),
            }
    return {"verified": False, "target": str(NAS), "error": "NAS_IDENTIDAD"}


def usage(fs):
    return {
        "bytes": {
            "total": fs.f_blocks * fs.f_frsize,
            "used": (fs.f_blocks - fs.f_bfree) * fs.f_frsize,
            "available": fs.f_bavail * fs.f_frsize,
        },
        "inodes": {"total": fs.f_files, "used": fs.f_files - fs.f_ffree, "available": fs.f_favail},
    }


def local_mount(path, mounts):
    candidates = [mount for mount in mounts if path.is_relative_to(mount.point)]
    depth = max((len(mount.point.parts) for mount in candidates), default=0)
    closest = [mount for mount in candidates if len(mount.point.parts) == depth]
    if len(closest) != 1 or not closest[0].source.startswith("/dev/"):
        raise ValueError("MONTAJE_LOCAL")
    return closest[0]


def report(text):
    mounts = parse_mounts(text)
    nas = nas_identity(mounts)
    paths, filesystems = {}, {}
    for path in LOCAL_PATHS:
        try:
            mount = local_mount(path, mounts)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                device = os.fstat(fd).st_dev
                if mount.device != f"{os.major(device)}:{os.minor(device)}":
                    raise ValueError("MONTAJE_CAMBIO")
                if mount.device not in filesystems:
                    filesystems[mount.device] = usage(os.fstatvfs(fd))
            finally:
                os.close(fd)
            paths[str(path)] = {
                "device": mount.device,
                "mount": str(mount.point),
                "source": mount.source,
                "filesystem": mount.kind,
            }
        except (OSError, ValueError) as error:
            paths[str(path)] = {
                "error": str(error) if type(error) is ValueError else "RUTA_CAPACIDAD"
            }
    return {
        "success": nas["verified"] and all("error" not in row for row in paths.values()),
        "host": HOST,
        "nas": nas,
        "paths": paths,
        "filesystems": filesystems,
    }


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    if sys.platform != "linux" or os.uname().nodename != HOST:
        result = {"success": False, "error": "HOST_IDENTIDAD"}
    else:
        try:
            result = report(os.fsdecode(Path("/proc/self/mountinfo").read_bytes()))
        except (OSError, ValueError, IndexError):
            result = {"success": False, "error": "MONTAJES_LECTURA"}
    print(json.dumps({"schema": 1, **result}, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
