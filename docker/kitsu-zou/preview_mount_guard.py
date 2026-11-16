"""Comprueba el NFS de previews y los contenedores propios antes de arrancar Zou."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

MOUNT = "/mnt/producciones"
SOURCE = "192.168.0.8:/volume2/producciones"
ROOT_INODE = 105971713
KITSU_INODE = 106037387
MARKER_INODE = 106037391
PREVIEWS_INODE = 106037392
NAS_OWNER = (1024, 100)
OWNER_MARKER = b"agencia-digital-marketing-ai:kitsu-previews:v1"
KITSU = MOUNT + "/_kitsu"
PREVIEWS = KITSU + "/previews"
WORKDIR = "/opt/media-platform/kitsu-base-v1"
STATE = "/var/lib/media-platform/kitsu-base-v1/zou"
PROJECT = "agency-media"
DEPENDENCIES = {"postgres-apps", "valkey"}
TARGETS = {"zou-api", "zou-events"}
DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "DOCKER_CONFIG": WORKDIR + "/docker-client",
}
INSPECT = (
    '{"id":{{json .Id}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"role":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"workdir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
    '"files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
    '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
    '"state":{{json .State.Status}},"pid":{{json .State.Pid}},'
    '"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}},'
    '"restart":{{json .HostConfig.RestartPolicy.Name}},"mounts":{{json .Mounts}}}'
)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def mount_identity(raw):
    roots = []
    for line in raw.splitlines():
        left, right = (value.split() for value in line.split(" - ", 1))
        path = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), left[4])
        require(path != KITSU and not path.startswith(KITSU + "/"), "NAS_SUBMONTAJE")
        if path == MOUNT:
            roots.append((left, right))
    require(len(roots) == 1, "NAS_MONTAJE")
    left, right = roots[0]
    mount_options, fs_options = set(left[5].split(",")), set(right[2].split(","))
    require(
        left[3] == "/"
        and right[:2] == ["nfs4", SOURCE]
        and "rw" in mount_options
        and {"rw", "vers=4.1", "proto=tcp", "hard"} <= fs_options
        and "ro" not in mount_options | fs_options,
        "NAS_ORIGEN",
    )
    major, minor = map(int, left[2].split(":"))
    return left[0], os.makedev(major, minor)


def check_node(info, device, inode, mode):
    require(
        (info.st_dev, info.st_ino) == (device, inode)
        and (info.st_uid, info.st_gid) == NAS_OWNER
        and stat.S_IMODE(info.st_mode) == mode,
        "NAS_NODO",
    )


def verify_storage(device):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    held = []
    try:
        held.append(os.open("/mnt", flags))
        held.append(os.open("producciones", flags, dir_fd=held[-1]))
        root = os.fstat(held[-1])
        require((root.st_dev, root.st_ino) == (device, ROOT_INODE), "NAS_SHARE_IDENTIDAD")
        held.append(os.open("_kitsu", flags, dir_fd=held[-1]))
        kitsu = os.fstat(held[-1])
        check_node(kitsu, device, KITSU_INODE, 0o700)
        held.append(os.open("previews", flags, dir_fd=held[-1]))
        previews = os.fstat(held[-1])
        check_node(previews, device, PREVIEWS_INODE, 0o700)
        marker_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        with os.fdopen(os.open(".agency-owner", marker_flags, dir_fd=held[-2]), "rb") as stream:
            marker = os.fstat(stream.fileno())
            check_node(marker, device, MARKER_INODE, 0o400)
            require(
                stat.S_ISREG(marker.st_mode)
                and marker.st_nlink == 1
                and marker.st_size == len(OWNER_MARKER)
                and stream.read(len(OWNER_MARKER) + 1) == OWNER_MARKER,
                "NAS_MARCADOR",
            )
            after = os.stat(".agency-owner", dir_fd=held[-2], follow_symlinks=False)
            require(
                (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_uid,
                    after.st_gid,
                    after.st_nlink,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                == (
                    marker.st_dev,
                    marker.st_ino,
                    marker.st_mode,
                    marker.st_uid,
                    marker.st_gid,
                    marker.st_nlink,
                    marker.st_size,
                    marker.st_mtime_ns,
                    marker.st_ctime_ns,
                ),
                "NAS_MARCADOR_CAMBIO",
            )
        for parent, name, original in zip(
            held[:-1], ("producciones", "_kitsu", "previews"), (root, kitsu, previews), strict=True
        ):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(
                (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid)
                == (
                    original.st_dev,
                    original.st_ino,
                    original.st_mode,
                    original.st_uid,
                    original.st_gid,
                ),
                "NAS_NODO_CAMBIO",
            )
    finally:
        for fd in reversed(held):
            os.close(fd)


def docker(*args):
    result = subprocess.run(
        [*DOCKER, *args],
        env=ENV,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    require(
        result.returncode == 0 and not result.stderr and len(result.stdout) <= 65536,
        "DOCKER_LECTURA",
    )
    return result.stdout


def check_containers(rows):
    seen = set()
    for row in rows:
        role = row["role"]
        require(
            role in DEPENDENCIES | TARGETS
            and role not in seen
            and row["project"] == PROJECT
            and row["workdir"] == WORKDIR
            and row["files"] == WORKDIR + "/compose.yml"
            and row["oneoff"] == "False",
            "DOCKER_IDENTIDAD",
        )
        seen.add(role)
        ready = row["state"] == "running" and row["pid"] > 0 and row["health"] == "healthy"
        if role in DEPENDENCIES:
            require(ready, "DOCKER_DEPENDENCIA_NO_SALUDABLE")
            continue
        require(
            row["restart"] == "no"
            and (ready or row["state"] in {"created", "exited"} and row["pid"] == 0),
            "DOCKER_ZOU_ESTADO",
        )
        binds = [
            (
                mount["Destination"],
                mount["Source"],
                mount["Type"],
                mount["RW"],
                mount["Propagation"],
            )
            for mount in row["mounts"]
            if mount["Destination"] == "/var/lib/zou"
            or mount["Destination"].startswith("/var/lib/zou/")
        ]
        require(
            sorted(binds)
            == [
                ("/var/lib/zou", STATE, "bind", True, "rprivate"),
                ("/var/lib/zou/previews", PREVIEWS, "bind", True, "rprivate"),
            ],
            "DOCKER_PREVIEWS_BIND",
        )
    require(seen == DEPENDENCIES | TARGETS, "DOCKER_ROLES")


def preflight():
    require(
        os.uname().nodename == "vm-media-01" and os.geteuid() == os.getegid() == 0, "HOST_IDENTIDAD"
    )
    mountinfo = Path("/proc/self/mountinfo")
    identity = mount_identity(mountinfo.read_text(encoding="utf-8"))
    verify_storage(identity[1])
    ids = (
        docker("ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}")
        .decode()
        .splitlines()
    )
    require(
        len(ids) == len(set(ids)) == 4 and all(re.fullmatch(r"[a-f0-9]{64}", cid) for cid in ids),
        "DOCKER_CIDS",
    )
    rows = [
        json.loads(line)
        for line in docker("inspect", "--type", "container", "--format", INSPECT, *ids).splitlines()
    ]
    require(len(rows) == len(ids) and {row["id"] for row in rows} == set(ids), "DOCKER_CIDS_CAMBIO")
    check_containers(rows)
    require(mount_identity(mountinfo.read_text(encoding="utf-8")) == identity, "NAS_MONTAJE_CAMBIO")


def main():
    try:
        preflight()
    except (
        OSError,
        ValueError,
        IndexError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        code = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error))
            else "PREFLIGHT_FALLO"
        )
        print(json.dumps({"success": False, "code": code}), flush=True)
        return 1
    print(json.dumps({"success": True, "previews": PREVIEWS}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
