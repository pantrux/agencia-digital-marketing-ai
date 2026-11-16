"""Contratos puros de identidad, montaje NFSv4.1 y reconciliación de fstab."""

import argparse
import json
import posixpath
import re
import sys

SOURCE = "192.168.0.8:/volume2/producciones"
TARGET = "/mnt/producciones"
BEGIN_MARKER = "# INICIO AGENCIA AI PRODUCCIONES NFSv4.1"
END_MARKER = "# FIN AGENCIA AI PRODUCCIONES NFSv4.1"
OPTIONS = "rw,vers=4.1,proto=tcp,hard,nosuid,nodev,_netdev,nofail,x-systemd.mount-timeout=30s"
ENTRY = f"{SOURCE} {TARGET} nfs4 {OPTIONS} 0 0\n"


def decode_field(value):
    decoded = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)
    if any(ord(character) < 32 for character in decoded):
        raise ValueError("Un campo fstab contiene caracteres de control inesperados.")
    return decoded


def normalized_target(value):
    value = decode_field(value)
    if value.startswith("/"):
        value = "/" + value.lstrip("/")
    return posixpath.normpath(value)


def fields(line):
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    return line.split()


def render_fstab(text):
    """Preserva bytes de líneas ajenas y reemplaza exclusivamente el bloque propio válido."""
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == BEGIN_MARKER]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == END_MARKER]
    if len(starts) != len(ends) or len(starts) > 1 or (starts and starts[0] >= ends[0]):
        raise ValueError("El bloque administrado de fstab está incompleto o duplicado.")
    start, end = (starts[0], ends[0]) if starts else (-1, -1)
    owned_rows = 0
    for index, line in enumerate(lines):
        row = fields(line)
        if row is None:
            continue
        inside = start < index < end if starts else False
        if inside:
            if len(row) < 6 or (len(row) > 6 and not row[6].startswith("#")):
                raise ValueError("El bloque propio contiene una entrada desconocida.")
            if (
                decode_field(row[0]) != SOURCE
                or normalized_target(row[1]) != TARGET
                or row[2] not in ("nfs", "nfs4")
            ):
                raise ValueError("El bloque propio contiene una entrada ajena; no se adopta.")
            owned_rows += 1
        elif len(row) >= 2 and normalized_target(row[1]) == TARGET:
            raise ValueError("Hay una entrada fstab ajena para /mnt/producciones; no se adopta.")
    if starts and owned_rows != 1:
        raise ValueError("El bloque propio debe contener una única entrada NFS identificada.")
    block = BEGIN_MARKER + "\n" + ENTRY + END_MARKER + "\n"
    if starts:
        return "".join(lines[:start]) + block + "".join(lines[end + 1 :])
    return text + ("\n" if text and not text.endswith("\n") else "") + block


def validate_mount(document):
    mounts = document.get("filesystems", [])
    if len(mounts) != 1:
        raise ValueError("No existe un único montaje en el destino permitido.")
    mount = mounts[0]
    options = set(mount.get("options", "").split(","))
    if (
        mount.get("source") != SOURCE
        or mount.get("target") != TARGET
        or mount.get("fstype") != "nfs4"
        or not {"rw", "vers=4.1", "proto=tcp"}.issubset(options)
        or "ro" in options
    ):
        raise ValueError(
            "El destino tiene un montaje ajeno o no acredita NFSv4.1 TCP de lectura/escritura."
        )


def validate_identity(target_ip, expected_hostname, actual_hostname, addresses):
    if target_ip not in ("192.168.0.85", "192.168.0.7"):
        raise ValueError("Solo se permiten los clientes .85 y .7.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", expected_hostname):
        raise ValueError("El hostname esperado debe ser el nombre real previamente auditado.")
    if expected_hostname != actual_hostname:
        raise ValueError("El hostname real no coincide con el cliente esperado.")
    if target_ip == "192.168.0.85" and actual_hostname != "vm-media-01":
        raise ValueError("El cliente .85 debe ser vm-media-01.")
    actual = [
        (address.get("local"), address.get("prefixlen"))
        for interface in addresses
        for address in interface.get("addr_info", [])
        if address.get("family") == "inet"
    ]
    if actual.count((target_ip, 24)) != 1 or any(
        ip in ("192.168.0.85", "192.168.0.7") and ip != target_ip for ip, _prefix in actual
    ):
        raise ValueError("La IP LAN real no identifica exclusivamente al cliente autorizado.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "render", help="Validar fstab recibido por entrada estándar y emitir el candidato."
    )
    commands.add_parser("mount", help="Validar un montaje findmnt JSON sin ejecutar operaciones.")
    identity = commands.add_parser(
        "identity", help="Validar el cliente real con direcciones ip JSON."
    )
    identity.add_argument("--target-ip", required=True)
    identity.add_argument("--expected-hostname", required=True)
    identity.add_argument("--actual-hostname", required=True)
    arguments = parser.parse_args()
    try:
        incoming = sys.stdin.read()
        if arguments.command == "render":
            sys.stdout.write(render_fstab(incoming))
        elif arguments.command == "mount":
            validate_mount(json.loads(incoming))
        else:
            validate_identity(
                arguments.target_ip,
                arguments.expected_hostname,
                arguments.actual_hostname,
                json.loads(incoming),
            )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
