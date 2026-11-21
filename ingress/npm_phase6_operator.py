"""Gestiona un recurso NPM de Fase 6 por invocación, desde el loopback del NAS."""

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import types
from pathlib import Path

HOSTS = ("api-studio.e-scale.cl", "kitsu.e-scale.cl", "immich.e-scale.cl")
CERTS = "/api/nginx/certificates"
PROXIES = "/api/nginx/proxy-hosts"
CUSTOM_SSL = Path("/volume1/docker/npm/data/custom_ssl")
RENDER_SHA = "e37686eb926fe61ccf989e963b58b6cc95ee6b8dbe376d887aa67242daf9eda0"
NPM_CID = "06ad62ee4666691f882fdff1bd5e11c31c1d86e11f63aee0c33afe368839fc7a"
NPM_IMAGE = "sha256:90522c2d426d9b2a7ae02f60365e812e1990a0dd78870ceaf60186b05e05385b"
OPERATIONS = {
    "inspect": set(),
    "certificate-create": set(),
    "certificate-upload": {
        "certificate_id",
        "certificate_preimage_sha256",
        "certificate",
        "certificate_key",
    },
    "host-create": {"certificate_id", "certificate_preimage_sha256", "inventory"},
    "host-enable": {"host_id", "host_preimage_sha256"},
    "host-disable": {"host_id", "host_preimage_sha256"},
}
ROUTE_FIELDS = {
    "domain_names",
    "certificate_id",
    "enabled",
    "ssl_forced",
    "forward_scheme",
    "forward_host",
    "forward_port",
    "caching_enabled",
    "allow_websocket_upgrade",
    "advanced_config",
    "locations",
    "hsts_enabled",
    "hsts_subdomains",
    "block_exploits",
    "access_list_id",
    "http2_support",
}


class Rejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def unique(pairs):
    value = dict(pairs)
    require(len(value) == len(pairs), "JSON_DUPLICADO")
    return value


def match_id(value):
    return type(value) is int and 0 < value <= 2**31 - 1


def attest_npm():
    proof = subprocess.run(
        [
            "/usr/local/bin/docker",
            "inspect",
            "--format",
            "{{.Id}} {{.Name}} {{.Image}} {{.State.Running}}",
            NPM_CID,
        ],
        capture_output=True,
        check=False,
        timeout=6,
    )
    require(
        proof.returncode == 0
        and proof.stdout.decode().split() == [NPM_CID, "/nginx-proxy-manager", NPM_IMAGE, "true"],
        "NPM_IDENTIDAD",
    )


def data_for(raw, operation):
    maximum = 65536
    require(0 < len(raw) <= maximum, "ENTRADA_TAMANO")
    data = json.loads(raw.decode(), object_pairs_hook=unique)
    required = {"host", "token"} | OPERATIONS[operation]
    require(
        type(data) is dict
        and (
            set(data) == required
            or (
                operation == "certificate-upload"
                and set(data) == required | {"intermediate_certificate"}
            )
        ),
        "ENTRADA_CAMPOS",
    )
    require(data["host"] in HOSTS, "ENTRADA_HOST")
    require(
        isinstance(data["token"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{32,4096}", data["token"]),
        "ENTRADA_TOKEN",
    )
    for key in required:
        if key.endswith("_id"):
            require(match_id(data[key]), "ENTRADA_ID")
        if key.endswith("_sha256"):
            require(
                isinstance(data[key], str) and re.fullmatch(r"[0-9a-f]{64}", data[key]),
                "ENTRADA_SHA",
            )
    return data


class Api:
    def __init__(self, token):
        self.token = token

    def call(self, method, route, status=200, *, data=None, mime="application/json", read=True):
        connection = http.client.HTTPConnection("127.0.0.1", 81, timeout=6)
        try:
            connection.request(
                method,
                route,
                body=data,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": mime,
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            with connection.getresponse() as response:
                require(response.status == status, f"NPM_HTTP_{response.status}")
                if not read:
                    return None
                raw = response.read(1048577)
                require(len(raw) <= 1048576, "NPM_RESPUESTA_TAMANO")
                return json.loads(raw.decode(), object_pairs_hook=unique)
        finally:
            connection.close()


def certificate_name(host):
    return "agency-phase6-" + host


def certificate_row(row, host, identifier=None):
    require(type(row) is dict and match_id(row.get("id")), "NPM_CERTIFICADO_ID")
    require(identifier is None or row["id"] == identifier, "NPM_CERTIFICADO_ID")
    require(
        row.get("provider") == "other" and row.get("domain_names") == [host], "NPM_CERTIFICADO_HOST"
    )
    require(row.get("nice_name") == certificate_name(host), "NPM_CERTIFICADO_NOMBRE")
    meta = row.get("meta")
    require(type(meta) is dict, "NPM_CERTIFICADO_META")
    fields = {
        key: row.get(key)
        for key in (
            "id",
            "owner_user_id",
            "nice_name",
            "domain_names",
            "provider",
            "created_on",
            "modified_on",
            "expires_on",
        )
    }
    fields["material"] = all(bool(meta.get(key)) for key in ("certificate", "certificate_key"))
    return fields


def host_row(row, host, identifier):
    require(
        type(row) is dict and match_id(row.get("id")) and row["id"] == identifier, "NPM_HOST_ID"
    )
    require(row.get("domain_names") == [host] and ROUTE_FIELDS <= set(row), "NPM_HOST_FORMA")
    require(
        match_id(row["certificate_id"]) and row["locations"] == [] and row["access_list_id"] == 0,
        "NPM_HOST_CONFIG",
    )
    require(type(row["enabled"]) is bool and row["ssl_forced"] is True, "NPM_HOST_ESTADO")
    fields = {key: row[key] for key in ROUTE_FIELDS}
    fields["advanced_config"] = sha(fields["advanced_config"].encode())
    return {
        **fields,
        "id": identifier,
        "owner_user_id": row.get("owner_user_id"),
        "modified_on": row.get("modified_on"),
    }


def rows(api, collection):
    values = api.call("GET", collection)
    require(type(values) is list and all(type(row) is dict for row in values), "NPM_LISTA")
    return values


def absent(api, collection, host):
    for row in rows(api, collection):
        require(row.get("nice_name") != certificate_name(host), "NPM_COLISION_NOMBRE")
        names = row.get("domain_names", [])
        require(
            type(names) is list and all(isinstance(name, str) for name in names),
            "NPM_LISTA_DOMINIOS",
        )
        for name in names:
            name = name.lower().rstrip(".")
            require(
                name != host and (not name.startswith("*.") or not host.endswith(name[1:])),
                "NPM_COLISION_DOMINIO",
            )


def verified_preimage(row, expected):
    require(hmac.compare_digest(sha(encode(row)), expected), "NPM_PREIMAGEN")


def private_empty_certificate_directory(identifier):
    path = CUSTOM_SSL / f"npm-{identifier}"
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(
            info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
            "NPM_DIR_CERT_PRIVADO",
        )
        require(not os.listdir(fd), "NPM_DIR_CERT_OCUPADO")
    finally:
        os.close(fd)


def installed_material(identifier, data):
    path = CUSTOM_SSL / f"npm-{identifier}"
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(
            info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
            "NPM_DIR_CERT_PRIVADO",
        )
        require(set(os.listdir(fd)) == {"fullchain.pem", "privkey.pem"}, "NPM_CERTIFICADO_FISICO")
        chain = data["certificate"]
        if "intermediate_certificate" in data:
            chain += "\n" + data["intermediate_certificate"]
        for name, expected in (("fullchain.pem", chain), ("privkey.pem", data["certificate_key"])):
            with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), "rb") as stream:
                item = os.fstat(stream.fileno())
                require(
                    stat.S_ISREG(item.st_mode) and item.st_uid == 0 and item.st_nlink == 1,
                    "NPM_CERTIFICADO_FISICO",
                )
                require(stream.read(32770) == expected.encode("ascii"), "NPM_CERTIFICADO_BYTES")
    finally:
        os.close(fd)


def certificate_body(data):
    boundary = "----agency-ingress-" + os.urandom(16).hex()
    parts = []
    for field in ("certificate", "certificate_key", "intermediate_certificate"):
        if field not in data:
            continue
        label = r"(?:RSA |EC )?PRIVATE KEY" if field == "certificate_key" else "CERTIFICATE"
        value = data[field]
        require(
            isinstance(value, str)
            and len(value) <= 16384
            and re.fullmatch(
                rf"-----BEGIN ({label})-----\r?\n[A-Za-z0-9+/=\r\n]+-----END \1-----\r?\n?",
                value,
            ),
            "NPM_PEM_FORMA",
        )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{field}.pem"\r\n'
            f"Content-Type: application/x-pem-file\r\n\r\n{value}\r\n".encode("ascii")
        )
    return b"".join(parts) + f"--{boundary}--\r\n".encode(
        "ascii"
    ), f"multipart/form-data; boundary={boundary}"


def render_npm(inventory, host):
    source = Path(__file__).with_name("phase6_ingress.py")
    raw = source.read_bytes()
    require(len(raw) <= 32768 and sha(raw) == RENDER_SHA, "NPM_RENDERER_SHA")
    module = types.ModuleType("phase6_ingress_pinned")
    exec(compile(raw, str(source), "exec"), module.__dict__)
    return module.render(inventory)["sites"][host]["npm"]


def receipt(operation, host, row, *, online=None):
    return {
        "operation": operation,
        "host": host,
        "id": row["id"],
        "row_sha256": sha(encode(row)),
        "material": row.get("material"),
        "enabled": row.get("enabled"),
        "nginx_online": online,
    }


def inspect(api, host):
    result = {"host": host, "certificates": [], "proxy_hosts": []}
    for collection, key in ((CERTS, "certificates"), (PROXIES, "proxy_hosts")):
        for row in rows(api, collection):
            if row.get("domain_names") != [host]:
                continue
            if collection == CERTS and row.get("nice_name") == certificate_name(host):
                result[key].append(receipt("inspect", host, certificate_row(row, host)))
            elif collection == PROXIES:
                result[key].append(receipt("inspect", host, host_row(row, host, row.get("id"))))
    return result


def operate(api, operation, data):
    version = api.call("GET", "/api/")
    require(version.get("version") == {"major": 2, "minor": 13, "revision": 5}, "NPM_VERSION")
    host = data["host"]
    if operation == "inspect":
        return inspect(api, host)
    if operation == "certificate-create":
        absent(api, CERTS, host)
        payload = {"provider": "other", "nice_name": certificate_name(host), "domain_names": [host]}
        row = certificate_row(api.call("POST", CERTS, 201, data=encode(payload)), host)
        return receipt(operation, host, row)
    if operation in ("certificate-upload", "host-create"):
        cert = data["certificate_id"]
        route = f"{CERTS}/{cert}"
        row = certificate_row(api.call("GET", route), host, cert)
        verified_preimage(row, data["certificate_preimage_sha256"])
        if operation == "certificate-upload":
            require(not row["material"], "NPM_CERTIFICADO_OCUPADO")
            private_empty_certificate_directory(cert)
            body, mime = certificate_body(data)
            api.call("POST", route + "/upload", data=body, mime=mime, read=False)
            row = certificate_row(api.call("GET", route), host, cert)
            require(row["material"], "NPM_CERTIFICADO_POSTCONDICION")
            installed_material(cert, data)
            return receipt(operation, host, row)
        require(row["material"], "NPM_CERTIFICADO_PENDIENTE")
        absent(api, PROXIES, host)
        for collection in ("/api/nginx/redirection-hosts", "/api/nginx/dead-hosts"):
            absent(api, collection, host)
        payload = {
            **render_npm(data["inventory"], host),
            "certificate_id": cert,
            "locations": [],
            "access_list_id": 0,
            "http2_support": False,
            "hsts_enabled": False,
            "hsts_subdomains": False,
            "block_exploits": False,
        }
        row = api.call("POST", PROXIES, 201, data=encode(payload))
        identifier = row.get("id")
        row = host_row(
            api.call("GET", f"{PROXIES}/{identifier}") if match_id(identifier) else row,
            host,
            identifier,
        )
        expected = {**payload, "advanced_config": sha(payload["advanced_config"].encode())}
        require(all(row[key] == value for key, value in expected.items()), "NPM_HOST_POSTCONDICION")
        return receipt(operation, host, row)
    identifier = data["host_id"]
    route = f"{PROXIES}/{identifier}"
    before = host_row(api.call("GET", route), host, identifier)
    verified_preimage(before, data["host_preimage_sha256"])
    enabled = operation == "host-enable"
    require(before["enabled"] is not enabled, "NPM_HOST_ESTADO_PREVIO")
    api.call("POST", route + ("/enable" if enabled else "/disable"), read=False)
    response = api.call("GET", route)
    after = host_row(response, host, identifier)
    require(
        all(before[key] == after[key] for key in before.keys() - {"enabled", "modified_on"}),
        "NPM_HOST_VARIO",
    )
    online = (response.get("meta") or {}).get("nginx_online") is True
    require(after["enabled"] is enabled and (not enabled or online), "NPM_HOST_POSTCONDICION")
    return receipt(operation, host, after, online=online)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?", choices=("plan", *OPERATIONS), default="plan")
    operation = parser.parse_args(argv).operation
    if operation == "plan":
        print(
            encode(
                {
                    "target": "127.0.0.1:81",
                    "npm_version": "2.13.5",
                    "credential": "stdin.token",
                    "operations": {
                        name: sorted(keys | {"host", "token"}) for name, keys in OPERATIONS.items()
                    },
                    "hosts": HOSTS,
                }
            ).decode()
        )
        return 0
    try:
        data = data_for(sys.stdin.buffer.read(65537), operation)
        attest_npm()
        result = operate(Api(data["token"]), operation, data)
    except Rejected as error:
        print(
            encode(
                {"operation": operation, "error": str(error), "reconcile_before_retry": True}
            ).decode()
        )
        return 1
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        RecursionError,
        subprocess.TimeoutExpired,
        http.client.HTTPException,
    ):
        print(
            encode(
                {"operation": operation, "error": "NPM_OPERACION", "reconcile_before_retry": True}
            ).decode()
        )
        return 1
    print(encode(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
