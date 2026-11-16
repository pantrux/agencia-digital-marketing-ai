"""Da de alta y comprueba el datasource Prometheus propio en Grafana del NAS."""

import argparse
import http.client
import json
import math
import os
import re
import secrets
import ssl
import stat
import sys
from pathlib import Path
from urllib.parse import urlencode

GRAFANA_IP = "192.168.48.3"
GRAFANA_PORT = 3000
GRAFANA_VERSION = "12.3.1"
UID = "agency-prometheus-nas"
PROMETHEUS_URL = "https://172.31.254.2:9090"
UID_PATH = "/api/datasources/uid/" + UID
NAME_PATH = "/api/datasources/name/" + UID
QUERY = 'up{job=~"agency-node|agency-cgroups"}'
JOBS = {"agency-node", "agency-cgroups"}
QUERY_PATH = "/api/datasources/proxy/uid/" + UID + "/api/v1/query?" + urlencode({"query": QUERY})
MARKER = "agencyProvisioningNonce"
MAX_RESPONSE = 1024 * 1024
MAX_PEM = 64 * 1024
MAX_TOKEN = 4096
TLS_FIELDS = ("tlsCACert", "tlsClientCert", "tlsClientKey")


class Rejected(Exception):
    """Error apto para un recibo que excluye cuerpos, credenciales y rutas de secretos."""


def require(condition, code):
    if not condition:
        raise Rejected(code)


def positive_id(value):
    return type(value) is int and value > 0


def read_token(stream):
    raw = stream.read(MAX_TOKEN + 3)
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    require(re.fullmatch(rb"[A-Za-z0-9._~+/=-]{16,4096}", raw), "GRAFANA_BEARER")
    return raw.decode("ascii")


class Grafana:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, *, allowed=(200,), payload=None):
        # http.client mantiene el destino fijo y no sigue redirecciones ni usa proxies.
        connection = http.client.HTTPConnection(GRAFANA_IP, GRAFANA_PORT, timeout=15)
        headers = {"Accept": "application/json", "Authorization": "Bearer " + self.token}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")
        try:
            connection.request(method, path, body=body, headers=headers)
            with connection.getresponse() as reply:
                status = reply.status
                require(status in allowed, "GRAFANA_HTTP_" + str(status))
                if status == 404:
                    return None
                raw = reply.read(MAX_RESPONSE + 1)
            require(len(raw) <= MAX_RESPONSE, "GRAFANA_RESPUESTA_TAMANO")
            value = json.loads(raw)
            require(isinstance(value, dict), "GRAFANA_RESPUESTA_JSON")
            return value
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            raise Rejected("GRAFANA_TRANSPORTE") from None
        finally:
            connection.close()


def plan(client, out):
    health = client.request("GET", "/api/health")
    require(
        health.get("version") == GRAFANA_VERSION and health.get("database") == "ok",
        "GRAFANA_VERSION_SALUD",
    )
    org = client.request("GET", "/api/org")
    require(positive_id(org.get("id")), "GRAFANA_ORGANIZACION")
    out.update(grafana_version=GRAFANA_VERSION, org_id=org["id"], datasource_uid=UID)
    uid = client.request("GET", UID_PATH, allowed=(200, 404))
    name = client.request("GET", NAME_PATH, allowed=(200, 404))
    out["conflicts"] = {"uid": uid is not None, "name": name is not None}


def read_pem(path, *, private=False):
    require(path.is_absolute() and ".." not in path.parts, "GRAFANA_TLS_RUTA")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= MAX_PEM,
            "GRAFANA_TLS_ARCHIVO",
        )
        require(not info.st_mode & (0o077 if private else 0o022), "GRAFANA_TLS_PERMISOS")
        raw = stream.read(MAX_PEM + 1)
        final = os.fstat(stream.fileno())
        require(
            len(raw) == info.st_size
            and (info.st_mtime_ns, info.st_ctime_ns) == (final.st_mtime_ns, final.st_ctime_ns),
            "GRAFANA_TLS_CAMBIO",
        )
    return raw.decode("ascii")


def tls_material(ca, certificate, key):
    ca_pem, cert_pem, key_pem = read_pem(ca), read_pem(certificate), read_pem(key, private=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca_pem)
    ctx.load_cert_chain(str(certificate), str(key), password="")
    require(
        read_pem(certificate) == cert_pem and read_pem(key, private=True) == key_pem,
        "GRAFANA_TLS_CAMBIO",
    )
    return {"tlsCACert": ca_pem, "tlsClientCert": cert_pem, "tlsClientKey": key_pem}


def matching_source(row, org_id, marker, created_id=None):
    return (
        isinstance(row, dict)
        and positive_id(row.get("id"))
        and (created_id is None or row["id"] == created_id)
        and row.get("orgId") == org_id
        and row.get("uid") == UID
        and row.get("name") == UID
        and row.get("type") == "prometheus"
        and row.get("access") == "proxy"
        and row.get("url") == PROMETHEUS_URL
        and row.get("isDefault") is False
        and isinstance(row.get("jsonData"), dict)
        and row["jsonData"].get(MARKER) == marker
    )


def verify_source(client, row, out):
    data = row["jsonData"]
    fields = row.get("secureJsonFields")
    require(
        data.get("tlsAuth") is True
        and data.get("tlsAuthWithCACert") is True
        and data.get("tlsSkipVerify") is False
        and isinstance(fields, dict)
        and all(fields.get(field) is True for field in TLS_FIELDS),
        "GRAFANA_TLS_DATASOURCE",
    )
    health = client.request("GET", UID_PATH + "/health")
    require(health.get("status") == "OK", "GRAFANA_SALUD_DATASOURCE")
    out["datasource_health"] = "OK"
    query = client.request("GET", QUERY_PATH)
    data = query.get("data")
    require(
        query.get("status") == "success"
        and isinstance(data, dict)
        and data.get("resultType") == "vector"
        and isinstance(data.get("result"), list),
        "GRAFANA_PROMQL",
    )
    jobs = set()
    for item in data["result"]:
        require(
            isinstance(item, dict)
            and isinstance(item.get("metric"), dict)
            and isinstance(item.get("value"), list)
            and len(item["value"]) == 2,
            "GRAFANA_PROMQL",
        )
        job = item["metric"].get("job")
        timestamp, value = item["value"]
        require(
            isinstance(job, str)
            and job in JOBS
            and job not in jobs
            and type(timestamp) in (int, float)
            and math.isfinite(timestamp)
            and value == "1",
            "GRAFANA_PROMQL",
        )
        jobs.add(job)
    require(jobs == JOBS, "GRAFANA_PROMQL")
    out["jobs_up"] = sorted(jobs)


def cleanup(client, org_id, marker, created_id):
    row = client.request("GET", UID_PATH, allowed=(200, 404))
    if row is None:
        return "absent"
    if not matching_source(row, org_id, marker, created_id):
        return "preserved"
    # Borrar por ID numérico evita afectar una fuente que reutilice el UID.
    client.request("DELETE", "/api/datasources/" + str(row["id"]), allowed=(200, 404))
    current = client.request("GET", UID_PATH, allowed=(200, 404))
    if current is None:
        return "removed"
    require(not matching_source(current, org_id, marker, row["id"]), "GRAFANA_REVERSIÓN")
    return "preserved"


def apply(client, out, org_id, material):
    require(out["org_id"] == org_id, "GRAFANA_ORGANIZACION_DISTINTA")
    require(not any(out["conflicts"].values()), "GRAFANA_UID_O_NOMBRE_OCUPADO")
    tls = material()
    marker = secrets.token_hex(16)
    payload = {
        "uid": UID,
        "name": UID,
        "type": "prometheus",
        "access": "proxy",
        "url": PROMETHEUS_URL,
        "isDefault": False,
        "jsonData": {
            "tlsAuth": True,
            "tlsAuthWithCACert": True,
            "tlsSkipVerify": False,
            MARKER: marker,
        },
        "secureJsonData": tls,
    }
    created_id = None
    try:
        response = client.request("POST", "/api/datasources", allowed=(200, 201), payload=payload)
        result = response.get("datasource")
        require(
            isinstance(result, dict) and positive_id(result.get("id")) and result.get("uid") == UID,
            "GRAFANA_RECIBO_CREACION",
        )
        created_id = result["id"]
        row = client.request("GET", UID_PATH)
        require(matching_source(row, org_id, marker, created_id), "GRAFANA_IDENTIDAD_DATASOURCE")
        verify_source(client, row, out)
    except (Rejected, KeyboardInterrupt):
        try:
            out["cleanup"] = cleanup(client, org_id, marker, created_id)
        except (Rejected, KeyboardInterrupt):
            out["cleanup"] = "unverified"
        raise
    out["datasource_id"] = created_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--org-id", type=int)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--client-cert-file", type=Path)
    parser.add_argument("--client-key-file", type=Path)
    args = parser.parse_args(argv)
    paths = (args.ca_file, args.client_cert_file, args.client_key_file)
    out = {"action": args.action, "success": False}
    try:
        require(
            (args.action == "plan" and args.org_id is None and not any(paths))
            or (args.action == "apply" and positive_id(args.org_id) and all(paths)),
            "GRAFANA_ARGUMENTOS",
        )
        client = Grafana(read_token(sys.stdin.buffer))
        plan(client, out)
        if args.action == "apply":
            apply(client, out, args.org_id, lambda: tls_material(*paths))
        out["success"] = True
    except Rejected as error:
        out["error"] = str(error)
    except (OSError, ValueError, UnicodeError, AttributeError):
        out["error"] = "GRAFANA_ENTRADA_O_TLS"
    except KeyboardInterrupt:
        out["error"] = "GRAFANA_INTERRUMPIDA"
    print(json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
