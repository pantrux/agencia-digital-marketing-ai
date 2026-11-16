"""Reconciliación acotada de filtros del puente Prometheus en DSM."""

import argparse
import ctypes
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import signal
import stat
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

MULTI = Path("/usr/bin/xtables-legacy-multi")
MULTI_SHA = "7522168bd7c93db722b7c99ed90628c052b05fb97c3e1c2924e9d41532dc6269"
LIBS = {
    4: (35600, "89cefa99066b52148b0663bfd8cf2d90072bbca252ee23992fc26fc74e08eea1"),
    6: (35632, "a9336eb1426a7e678afb5a86776700fc030030b8127b3d944e088eae49ebedff"),
}
LAYOUTS = {4: (84, 112), 6: (136, 168)}
BUILTINS = ("INPUT", "FORWARD", "OUTPUT")
TABLES = ((4, "filter"), (6, "filter"), (4, "nat"))
JOURNAL = Path("/volume2/agency-prometheus-private-v1/firewall-filter-reconcile-v1")
DOCKER = "/usr/local/bin/docker"
CID = "36ff95569d786b8993a4354282005e42274d99f3662f3f246f0da6fe57c32474"
IMAGE = "sha256:12cf849bb9456daf291b25db71089cc8830e4f9881aae290e17d4a3dfd9accb8"
NET = "209b81c18d6d878e180e1033762a309efa456930028ef321be6fdeb43f3f9506"
OWNER = "agencia-digital-marketing-ai:prometheus-nas:bridge:v1"
BRIDGE = "br-apromv1"
NAS, VM, ADDRESS = "192.168.0.8", "192.168.0.85", "172.31.254.2"
NAT_CHAIN = "DEFAULT_PREROUTING"
TCP = ("-p", "tcp", "-m", "tcp")
DNAT = (
    "-s",
    VM + "/32",
    "-d",
    NAS + "/32",
    "-i",
    "eth0",
    *TCP,
    "--dport",
    "9090",
    "-j",
    "DNAT",
    "--to-destination",
    ADDRESS + ":9090",
)
CONNTRACK = ("--ctproto", "6", "--ctorigsrc", VM, "--ctorigdst", NAS, "--ctorigdstport", "9090")
TRANSIT = (
    (
        "-s",
        VM + "/32",
        "-d",
        ADDRESS + "/32",
        "-i",
        "eth0",
        "-o",
        BRIDGE,
        *TCP,
        "--dport",
        "9090",
        "-m",
        "conntrack",
        "--ctstate",
        "NEW,ESTABLISHED",
        *CONNTRACK,
        "--ctdir",
        "ORIGINAL",
        "-j",
        "ACCEPT",
    ),
    (
        "-s",
        ADDRESS + "/32",
        "-d",
        VM + "/32",
        "-i",
        BRIDGE,
        "-o",
        "eth0",
        *TCP,
        "--sport",
        "9090",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED",
        *CONNTRACK,
        "--ctdir",
        "REPLY",
        "-j",
        "ACCEPT",
    ),
)
ENV = {"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
MAXIMUM = 1024**2
ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    subprocess.SubprocessError,
    KeyboardInterrupt,
)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def chains(rows):
    result = {}
    for row in rows:
        if row[0] == "CHAIN":
            require(row[1] not in result, "CADENA_DUPLICADA")
            result[row[1]] = {"policy": row[2], "rules": []}
        else:
            require(row[0] == "-A" and row[1] in result, "REGLA_FISICA")
            result[row[1]]["rules"].append(tuple(row[2:]))
    return result


def table_rows(raw, table):
    lines = [line for line in raw.decode().splitlines() if line and not line.startswith("#")]
    require(len(lines) >= 3 and lines[0] == "*" + table and lines[-1] == "COMMIT", "TABLA")
    rows = []
    for line in lines[1:-1]:
        if line.startswith(":"):
            match = re.fullmatch(r":(\S+) (\S+) \[[0-9]+:[0-9]+\]", line)
            require(match is not None, "POLITICA")
            rows.append(("CHAIN", *match.groups()))
        else:
            row = tuple(shlex.split(line))
            require(len(row) >= 4 and row[0] == "-A", "REGLA_FISICA")
            rows.append(row)
    chains(rows)
    return tuple(rows)


def hashes(tables):
    return {key: sha(encode(chains(rows))) for key, rows in tables.items()}


def owned_chain(hook):
    return "APROM_V1_" + hook


def own_rules(family, hook):
    drops = {"INPUT": ("-i",), "OUTPUT": ("-o",), "FORWARD": ("-i", "-o")}
    prefix = TRANSIT if family == 4 and hook == "FORWARD" else ()
    return prefix + tuple((flag, BRIDGE, "-j", "DROP") for flag in drops[hook])


def plan(tables):
    require(set(tables) == {"4:filter", "6:filter", "4:nat"}, "TABLAS_INCOMPLETAS")
    v4, v6, nat = (chains(tables[key]) for key in ("4:filter", "6:filter", "4:nat"))
    require(
        all(owned_chain(hook) not in table for table in (v4, v6) for hook in BUILTINS),
        "CADENA_PROPIA_EXISTENTE",
    )
    require(
        v4["FORWARD"]["rules"] == [("-j", "PPPOE_FORWARD")] and "DEFAULT_FORWARD" not in v4,
        "FORWARD_TOPOLOGIA",
    )
    previous = v4["PPPOE_FORWARD"]["rules"]
    require(
        len(previous) == 1
        and len(previous[0]) == 6
        and previous[0][0] == "-d"
        and previous[0][2:] == ("-i", "tun0", "-j", "DROP")
        and ipaddress.ip_network(previous[0][1], strict=False).version == 4,
        "PPPOE_ALCANCE",
    )
    require(not v4["INPUT"]["rules"] and not v4["OUTPUT"]["rules"], "V4_HOST_TOPOLOGIA")
    require(not any(row["rules"] for row in v6.values()), "V6_TOPOLOGIA")
    require(
        all(v4[hook]["policy"] == v6[hook]["policy"] == "ACCEPT" for hook in BUILTINS),
        "POLITICA_CAMBIO",
    )
    require(
        nat["PREROUTING"]["rules"] == [("-j", NAT_CHAIN)]
        and nat[NAT_CHAIN]["rules"] == [DNAT]
        and sum(DNAT == rule for chain in nat.values() for rule in chain["rules"]) == 1,
        "DNAT_IDENTIDAD",
    )
    return {
        "tables_before": hashes(tables),
        "expected_filter_rows": {"4": 6, "6": 4},
        "hook_offsets": {
            "4": {"INPUT": 0, "FORWARD": 1, "OUTPUT": 0},
            "6": {hook: 0 for hook in BUILTINS},
        },
        "rules": {
            str(family): {hook: own_rules(family, hook) for hook in BUILTINS} for family in (4, 6)
        },
    }


def jump_entry(family, target):
    ip_size, size = LAYOUTS[family]
    require(re.fullmatch(rb"APROM_V1_(?:INPUT|FORWARD|OUTPUT)", target), "LIB_DESTINO")
    entry = bytearray(size + 40)
    struct.pack_into("=HH", entry, ip_size + 4, size, len(entry))
    struct.pack_into("=H", entry, size, 40)
    entry[size + 2 : size + 2 + len(target)] = target
    return ctypes.create_string_buffer(bytes(entry), len(entry))


def command(argv):
    result = subprocess.run(
        [str(value) for value in argv],
        env=ENV,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    require(result.returncode == 0 and len(result.stdout) <= MAXIMUM, "COMANDO_RECHAZADO")
    return result.stdout


def snapshot():
    return {
        f"{family}:{table}": table_rows(
            command((MULTI, "iptables-save" if family == 4 else "ip6tables-save", "-t", table)),
            table,
        )
        for family, table in TABLES
    }


def verified_file(path, digest, size=None):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022
            and (size is None or size == info.st_size),
            "BINARIO_IDENTIDAD",
        )
        raw = stream.read(4 * MAXIMUM + 1)
        after = os.fstat(stream.fileno())
    require(
        len(raw) == info.st_size
        and (info.st_mtime_ns, info.st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns)
        and sha(raw) == digest,
        "BINARIO_SHA",
    )


def host():
    require(
        os.geteuid() == os.getegid() == 0
        and platform.machine() == "x86_64"
        and sys.byteorder == "little"
        and ctypes.sizeof(ctypes.c_void_p) == 8
        and os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"),
        "HOST_ABI",
    )
    verified_file(MULTI, MULTI_SHA)
    require(Path("/sys/class/net", BRIDGE, "bridge").is_dir(), "BRIDGE_IDENTIDAD")
    argv = (DOCKER, "--host", "unix:///var/run/docker.sock", "inspect", "--type", "container", CID)
    rows = json.loads(command(argv))
    require(len(rows) == 1, "CONTENEDOR_IDENTIDAD")
    row = rows[0]
    state = row["State"]
    networks = row["NetworkSettings"]["Networks"]
    require(
        row["Id"] == CID
        and row["Image"] == IMAGE
        and row["Config"]["Labels"]["agency.owner"] == OWNER
        and state["Running"] is True
        and state["Pid"] > 0
        and state["Health"]["Status"] == "healthy"
        and not row["HostConfig"]["PortBindings"]
        and len(networks) == 1,
        "CONTENEDOR_IDENTIDAD",
    )
    endpoint = next(iter(networks.values()))
    require(endpoint["NetworkID"] == NET and endpoint["IPAddress"] == ADDRESS, "RED_IDENTIDAD")
    return {"cid": CID, "pid": state["Pid"], "started_at": state["StartedAt"]}


def library(family):
    size, digest = LIBS[family]
    path = Path(f"/usr/lib/libip{family}tc.so.2.0.0")
    verified_file(path, digest, size)
    lib = ctypes.CDLL(str(path), use_errno=True)
    pointer, string, integer, uint = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint
    signatures = {
        "init": (pointer, [string]),
        "free": (None, [pointer]),
        "first_rule": (pointer, [string, pointer]),
        "next_rule": (pointer, [pointer, pointer]),
        "get_target": (string, [pointer, pointer]),
        "is_chain": (ctypes.c_bool, [string, pointer]),
        "create_chain": (integer, [string, pointer]),
        "insert_entry": (integer, [string, pointer, uint, pointer]),
        "delete_num_entry": (integer, [string, uint, pointer]),
        "commit": (integer, [pointer]),
    }
    for name, (result, args) in signatures.items():
        prefix = "iptc_" if family == 4 else "ip6tc_"
        function = getattr(lib, prefix + name)
        function.restype, function.argtypes = result, args
        setattr(lib, name, function)
    return lib


@contextmanager
def lock(path, flags=0):
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | flags, 0o600)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and not info.st_mode & 0o022,
            "LOCK_IDENTIDAD",
        )
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "LOCK_TIMEOUT")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def inspect_hooks(lib, family, handle, hooked):
    ip_size, size = LAYOUTS[family]
    for hook in BUILTINS:
        chain = hook.encode()
        targets = [b"PPPOE_FORWARD"] if family == 4 and hook == "FORWARD" else []
        target = owned_chain(hook).encode()
        require(lib.is_chain(target, handle), "LIB_CADENA")
        if hooked:
            targets.append(target)
        entry = lib.first_rule(chain, handle)
        for expected in targets:
            require(entry and lib.get_target(entry, handle) == expected, "LIB_SALTO")
            header = ctypes.string_at(entry, size)
            require(
                not any(header[:ip_size])
                and struct.unpack_from("=HH", header, ip_size + 4) == (size, size + 40)
                and struct.unpack("=H", ctypes.string_at(entry + size, 2))[0] == 40,
                "LIB_LAYOUT",
            )
            entry = lib.next_rule(entry, handle)
        require(not entry, "LIB_SALTOS_EXTRA")


def hooks(family, enabled, before, lib, trial=False):
    with lock(Path("/run/xtables.lock")):
        require(hashes(snapshot()) == before, "CAMBIO_CONCURRENTE")
        handle = lib.init(b"filter")
        require(handle, "LIB_INIT")
        try:
            if trial:
                for hook in BUILTINS:
                    require(
                        lib.create_chain(owned_chain(hook).encode(), handle), "LIB_CADENA_ENSAYO"
                    )
            inspect_hooks(lib, family, handle, not enabled)
            for hook in BUILTINS:
                chain = hook.encode()
                offset = int(family == 4 and hook == "FORWARD")
                if enabled:
                    entry = jump_entry(family, owned_chain(hook).encode())
                    require(lib.insert_entry(chain, entry, offset, handle), "LIB_INSERCION")
                else:
                    require(lib.delete_num_entry(chain, offset, handle), "LIB_RETIRO")
            inspect_hooks(lib, family, handle, enabled)
            if not trial:
                require(lib.commit(handle), "LIB_COMMIT")
        finally:
            lib.free(handle)


def operations():
    for family in (4, 6):
        for hook in BUILTINS:
            yield family, "chain", hook, ()
            for rule in own_rules(family, hook):
                yield family, "rule", hook, rule
    for family in (4, 6):
        yield family, "hooks", "", ()


def predicted_states(tables):
    # Hash policies and per-chain order. DSM may enumerate user chains differently.
    state = {key: chains(rows) for key, rows in tables.items()}
    result = [{key: sha(encode(rows)) for key, rows in state.items()}]
    for family, action, hook, rule in operations():
        table = state[f"{family}:filter"]
        if action == "chain":
            table[owned_chain(hook)] = {"policy": "-", "rules": []}
        elif action == "rule":
            table[owned_chain(hook)]["rules"].append(rule)
        else:
            for chain in BUILTINS:
                table[chain]["rules"].append(("-j", owned_chain(chain)))
        result.append({key: sha(encode(rows)) for key, rows in state.items()})
    return result


def mutate(step, enabled, before, libs):
    family, action, hook, rule = step
    require(hashes(snapshot()) == before, "CAMBIO_CONCURRENTE")
    if action == "hooks":
        hooks(family, enabled, before, libs[family])
        return
    args = (
        ("-N" if enabled else "-X", owned_chain(hook))
        if action == "chain"
        else ("-A" if enabled else "-D", owned_chain(hook), *rule)
    )
    command(
        (
            MULTI,
            "iptables-legacy" if family == 4 else "ip6tables-legacy",
            "-w",
            "5",
            "-t",
            "filter",
            *args,
        )
    )


def write_record(name, value):
    path = JOURNAL / name
    with path.open("xb") as stream:
        stream.write(encode(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(JOURNAL, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_intent():
    path = JOURNAL / "intent.json"
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "JOURNAL_INTENTO",
        )
        raw = stream.read(32769)
    require(len(raw) == info.st_size and 0 < len(raw) <= 32768, "JOURNAL_INTENTO")
    return json.loads(raw)


def rollback(states, libs):
    current = hashes(snapshot())
    require(current in states, "REVERSION_ESTADO_DESCONOCIDO")
    steps = tuple(operations())
    for index in reversed(range(states.index(current))):
        mutate(steps[index], False, states[index + 1], libs)
        require(hashes(snapshot()) == states[index], "REVERSION_DERIVA")


def operation(action, pin):
    instance = host()
    libs = {family: library(family) for family in (4, 6)}
    if action == "plan":
        tables = snapshot()
        proposed = {
            **plan(tables),
            "container": instance,
            "tables_after": predicted_states(tables)[-1],
        }
        require(
            hashes(snapshot()) == proposed["tables_before"] and host() == instance,
            "CAMBIO_CONCURRENTE",
        )
        return {"plan": proposed, "plan_sha256": sha(encode(proposed))}
    root = JOURNAL.parent
    mode = root.lstat()
    require(
        root.resolve(strict=True) == root
        and stat.S_ISDIR(mode.st_mode)
        and mode.st_uid == 0
        and stat.S_IMODE(mode.st_mode) == 0o700,
        "JOURNAL_RAIZ",
    )
    with lock(root / ".firewall-filter-reconcile.lock", os.O_CREAT):
        if action == "rollback":
            info = JOURNAL.lstat()
            require(
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o700,
                "JOURNAL_IDENTIDAD",
            )
            record = read_intent()
            require(
                record["plan_sha256"] == pin and record["container"] == instance, "JOURNAL_PLAN"
            )
            if (JOURNAL / "rollback.json").is_file():
                require(hashes(snapshot()) == record["states"][0], "REVERSION_DERIVA")
                return {"rollback_verified": True, "tables": record["states"][0]}
            rollback(record["states"], libs)
            write_record("rollback.json", {"tables": hashes(snapshot())})
            return {"rollback_verified": True, "tables": hashes(snapshot())}
        tables = snapshot()
        proposed = {**plan(tables), "container": instance}
        states = predicted_states(tables)
        proposed["tables_after"] = states[-1]
        require(sha(encode(proposed)) == pin, "PLAN_SHA")
        for family in (4, 6):
            hooks(family, True, states[0], libs[family], trial=True)
        require(hashes(snapshot()) == states[0] and host() == instance, "ENSAYO_CAMBIO")
        if action == "test":
            return {"test_verified": True, "plan_sha256": pin, "tables": states[0]}
        JOURNAL.mkdir(mode=0o700)
        write_record("intent.json", {"plan_sha256": pin, "container": instance, "states": states})
        try:
            for index, step in enumerate(operations()):
                mutate(step, True, states[index], libs)
                require(hashes(snapshot()) == states[index + 1], "DERIVA_APLICACION")
                write_record(f"step-{index + 1:02}.json", {"after": states[index + 1]})
            require(host() == instance, "CONTENEDOR_CAMBIO")
        except ERRORS:
            rollback(states, libs)
            write_record("rollback.json", {"tables": hashes(snapshot())})
            raise
        write_record("result.json", {"tables": states[-1]})
        return {"rules_verified": 10, "tables": states[-1]}


def interrupted(_signal, _frame):
    raise ValueError("OPERACION_INTERRUMPIDA")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "test", "apply", "rollback"))
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    try:
        require(
            args.action == "plan" or re.fullmatch(r"[a-f0-9]{64}", args.plan_sha256 or ""),
            "PLAN_PIN",
        )
        os.umask(0o077)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        result = {"success": True, **operation(args.action, args.plan_sha256)}
    except ERRORS as error:
        code = str(error) if type(error) is ValueError else "OPERACION_FALLIDA"
        result = {
            "success": False,
            "error": code if re.fullmatch(r"[A-Z0-9_]+", code) else "OPERACION_FALLIDA",
        }
    print(encode(result).decode())
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
