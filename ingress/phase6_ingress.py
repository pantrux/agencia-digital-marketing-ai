"""Renderiza únicamente los tres hosts de ingreso de la agencia."""

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path

API = "api-studio.e-scale.cl"
APPS = {"kitsu.e-scale.cl": 8081, "immich.e-scale.cl": 2283}
BUNKERWEB = "https://192.168.0.8:9443"
NPM = "https://nginx-proxy-manager:443"
ORIGIN_RELAY_SOCKET = "unix:/run/agency-studio-origin-relay/origin.sock"
NPM_IMAGE = "sha256:90522c2d426d9b2a7ae02f60365e812e1990a0dd78870ceaf60186b05e05385b"
WAF_FILES = "/var/log/bunkerweb/agency-phase6"
SCHEDULER_TLS = "/data/agency-phase6"
NPM_FILES = "/data/agency-phase6"
NAS_MATERIAL_DIRECTORIES = {
    "staging_host": "/volume2/agency-studio-ingress-private-v2",
    "bunkerweb_host": "/volume1/docker/bunkerweb/data/logs/agency-phase6",
    "bunkerweb_container": WAF_FILES,
    "scheduler_host": "/volume1/docker/bunkerweb/data/storage/agency-phase6",
    "scheduler_container": SCHEDULER_TLS,
    "npm_host": "/volume1/docker/npm/data/agency-phase6",
    "npm_container": NPM_FILES,
}
MAX_INVENTORY_BYTES = 4096
SYNC_URI = "/v1/sync"
SOFIA_URI = "/v1/sofia/chat"
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
FIELDS = {
    "schema",
    "observed_bunkerweb_version",
    "observed_npm_image_id",
    "preflight_sha256",
    "tunnel_peer_ipv4",
    "sofia_tls",
}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class InputError(ValueError):
    """El inventario no corresponde al contrato local."""


def pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("INVENTARIO_CLAVE_DUPLICADA")
        result[key] = value
    return result


def valid_dns_name(value):
    return (
        isinstance(value, str)
        and len(value) <= 253
        and len(value.split(".")) >= 2
        and all(NAME.fullmatch(label) for label in value.split("."))
        and not value.split(".")[-1].isdigit()
    )


def validate_inventory(value):
    schema = value.get("schema") if type(value) is dict else None
    fields = FIELDS | {"origin_client_auth"} if schema == 2 else FIELDS
    if (
        type(value) is not dict
        or set(value) != fields
        or type(schema) is not int
        or schema not in (1, 2)
    ):
        raise InputError("INVENTARIO_ESQUEMA")
    if schema == 2 and value["origin_client_auth"] != "studio-origin-relay-v1":
        raise InputError("INVENTARIO_ORIGEN_MTLS")
    if value["observed_bunkerweb_version"] != "1.6.9":
        raise InputError("INVENTARIO_BUNKERWEB_VERSION")
    if value["observed_npm_image_id"] != NPM_IMAGE:
        raise InputError("INVENTARIO_IMAGEN_NPM")
    if not isinstance(value["preflight_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["preflight_sha256"]
    ):
        raise InputError("INVENTARIO_HUELLA")
    try:
        if not isinstance(value["tunnel_peer_ipv4"], str):
            raise ValueError
        peer = ipaddress.IPv4Address(value["tunnel_peer_ipv4"])
    except ValueError as error:
        raise InputError("INVENTARIO_PEER_IPV4") from error
    if not any(peer in network for network in PRIVATE_NETWORKS):
        raise InputError("INVENTARIO_PEER_IPV4")
    sofia = value["sofia_tls"]
    if sofia is None:
        return value
    if (
        type(sofia) is not dict
        or set(sofia) != {"ipv4", "port", "server_name"}
        or sofia["ipv4"] != "192.168.0.7"
    ):
        raise InputError("INVENTARIO_SOFIA")
    port = sofia["port"]
    if type(port) is not int or not 1024 <= port <= 65535 or port in (3107, 3108, 18789):
        raise InputError("INVENTARIO_SOFIA_PUERTO")
    if not valid_dns_name(sofia["server_name"]) or sofia["server_name"] in (
        API,
        *APPS,
        "studio.e-scale.cl",
    ):
        raise InputError("INVENTARIO_SOFIA_SNI")
    return value


def inventory(raw):
    if len(raw) > MAX_INVENTORY_BYTES:
        raise InputError("INVENTARIO_TAMANO")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs_without_duplicates)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InputError("INVENTARIO_JSON") from error
    return validate_inventory(value)


def plan():
    return {
        "schema": 1,
        "candidate_rendered": False,
        "activation_verified": False,
        "sofia_enabled": False,
        "observed_npm_version_required": "2.13.5",
        "public_hosts": [API, *APPS],
        "nas_material_directories": dict(NAS_MATERIAL_DIRECTORIES),
        "inventory_fields": sorted(FIELDS),
        "activation_evidence_required": [
            "Inventario NAS y conector autorizado con prueba del peer TCP y permisos de rutas del túnel",
            "TLS de origen en BunkerWeb y mTLS en NPM",
            "nginx -t y configuración generada en versiones instaladas, alcance de red y pruebas de vhost",
            "Pruebas de WAF/HMAC, reenvío de cabeceras y cuerpo, y registros con fixtures saneadas",
            "Pruebas de aplicación Kitsu/Immich y reversión por los tres hosts nuevos",
        ],
    }


def guard_host(host):
    return [
        f"if ($ssl_server_name != {host}) {{ return 421; }}",
        f"if ($host != {host}) {{ return 421; }}",
        f"if ($http_host != {host}) {{ return 421; }}",
    ]


def guard_machine_request(path, limit):
    return [
        f"if ($request_uri != {path}) {{ return 404; }}",
        "if ($request_method != POST) { return 405; }",
        'if ($http_transfer_encoding != "") { return 400; }',
        'if ($content_length = "") { return 411; }',
        f"client_max_body_size {limit};",
        "proxy_next_upstream off;",
        "access_log off;",
        "error_log /dev/null crit;",
    ]


def guard_machine_server(sofia_enabled):
    uri = "^/v1/(sync|sofia/chat)$" if sofia_enabled else f"^{SYNC_URI}$"
    return [f'if ($request_uri !~ "{uri}") {{ return 404; }}', "access_log off;"]


def client_tls(directory, name):
    return [
        "proxy_ssl_verify on;",
        "proxy_ssl_verify_depth 2;",
        "proxy_ssl_protocols TLSv1.2 TLSv1.3;",
        f"proxy_ssl_trusted_certificate {directory}/{name}-server-ca.pem;",
        f"proxy_ssl_certificate {directory}/{name}-client.pem;",
        f"proxy_ssl_certificate_key {directory}/{name}-client.key;",
    ]


def reverse_proxy_settings(suffix, uri, include, timeout, application):
    fields = {
        "HOST": NPM,
        "URL": uri,
        "INCLUDES": f"{WAF_FILES}/{include}",
        "WS": "yes" if application else "no",
        "KEEPALIVE": "no" if application else "yes",
        "CONNECT_TIMEOUT": "5s",
        "SEND_TIMEOUT": timeout,
        "READ_TIMEOUT": timeout,
        "BUFFERING": "no",
        "REQUEST_BUFFERING": "no" if application else "yes",
    }
    return {f"REVERSE_PROXY_{name}{suffix}": value for name, value in fields.items()}


def bunkerweb_settings(host, application, peer):
    max_body = "128m" if application else "12288"
    return {
        "SERVER_NAME": host,
        "LISTEN_HTTP": "no",
        "USE_CUSTOM_SSL": "yes",
        "CUSTOM_SSL_CERT_PRIORITY": "file",
        "CUSTOM_SSL_CERT": f"{SCHEDULER_TLS}/{host}.pem",
        "CUSTOM_SSL_KEY": f"{SCHEDULER_TLS}/{host}.key",
        "SSL_PROTOCOLS": "TLSv1.2 TLSv1.3",
        "USE_REAL_IP": "yes",
        "REAL_IP_FROM": peer,
        "REAL_IP_FROM_URLS": "",
        "REAL_IP_HEADER": "CF-Connecting-IP",
        "REAL_IP_RECURSIVE": "no",
        "SERVE_FILES": "no",
        "USE_REVERSE_PROXY": "yes",
        "REVERSE_PROXY_SSL_SNI": "yes",
        "REVERSE_PROXY_SSL_SNI_NAME": host,
        "REVERSE_PROXY_INTERCEPT_ERRORS": "no",
        "USE_PROXY_CACHE": "no",
        "SECURITY_MODE": "block",
        "USE_MODSECURITY": "yes",
        "USE_MODSECURITY_CRS": "yes",
        "MODSECURITY_SEC_RULE_ENGINE": "On",
        "MODSECURITY_SEC_AUDIT_ENGINE": "Off",
        "MAX_CLIENT_SIZE": max_body,
        "MODSECURITY_REQ_BODY_NO_FILES_LIMIT": max_body,
        "ALLOWED_METHODS": "GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS" if application else "POST",
        **({} if application else {"USE_ANTIBOT": "no"}),
    }


def nginx_location(uri, directives):
    return [f"location {uri} {{", *(f"    {directive}" for directive in directives), "}"]


def npm_location_pass(target, timeout, host="$host"):
    return [
        f"proxy_pass {target};",
        "proxy_http_version 1.1;",
        f"proxy_set_header Host {host};",
        "proxy_set_header X-Forwarded-Host $host;",
        "proxy_set_header X-Forwarded-Proto https;",
        "proxy_set_header X-Forwarded-Scheme https;",
        "proxy_set_header X-Forwarded-Protocol https;",
        "proxy_set_header X-Forwarded-Port 443;",
        "proxy_set_header X-Real-IP $http_x_real_ip;",
        "proxy_set_header X-Forwarded-For $http_x_real_ip;",
        'proxy_set_header Forwarded "";',
        "proxy_redirect off;",
        "proxy_cache off;",
        "proxy_buffering off;",
        "proxy_connect_timeout 5s;",
        f"proxy_send_timeout {timeout};",
        f"proxy_read_timeout {timeout};",
    ]


def nginx_config(lines):
    return "\n".join(lines) + "\n"


def npm_site(host, locations, machine_guard):
    return {
        "domain_names": [host],
        "enabled": False,
        "ssl_forced": True,
        "caching_enabled": False,
        "forward_scheme": "http",
        "forward_host": "127.0.0.1",
        "forward_port": 1,
        "allow_websocket_upgrade": False,
        "advanced_config": nginx_config(
            [
                "if ($scheme != https) { return 403; }",
                *guard_host(host),
                f"ssl_client_certificate {NPM_FILES}/bunkerweb-client-ca.pem;",
                "ssl_verify_client on;",
                "ssl_verify_depth 2;",
                *machine_guard,
                *locations,
            ]
        ),
    }


def site_origin(host, use_relay=False):
    if use_relay:
        return {
            "hostname": host,
            "service": ORIGIN_RELAY_SOCKET,
            "originRequest": {"http2Origin": False, "disableChunkedEncoding": False},
        }
    return {
        "hostname": host,
        "service": BUNKERWEB,
        "originRequest": {
            "caPool": "/etc/agency-studio-tunnel-public/bunkerweb-server-ca.pem",
            "originServerName": host,
            "httpHostHeader": host,
            "noTLSVerify": False,
            "http2Origin": False,
            "disableChunkedEncoding": False,
        },
    }


def render(value):
    value = validate_inventory(value)
    sofia = value["sofia_tls"]
    peer = value["tunnel_peer_ipv4"]
    use_relay = value["schema"] == 2
    waf_tls = [
        "resolver 127.0.0.11 valid=15s ipv6=off;",
        "resolver_timeout 5s;",
        *client_tls(WAF_FILES, "npm"),
    ]
    include_files = {
        "application.conf": nginx_config([*waf_tls, "proxy_next_upstream off;", "access_log off;"])
    }
    sites = {}
    for host in (API, *APPS):
        port = APPS.get(host)
        application = port is not None
        settings = bunkerweb_settings(host, application, peer)
        server = [*guard_host(host), f"if ($realip_remote_addr != {peer}) {{ return 403; }}"]
        if use_relay:
            server.extend(
                [
                    f"ssl_client_certificate {WAF_FILES}/studio-origin-relay-ca.pem;",
                    "ssl_verify_client on;",
                    "ssl_verify_depth 1;",
                    "if ($ssl_client_verify != SUCCESS) { return 403; }",
                ]
            )
        machine_guard = []
        if application:
            settings.update(
                reverse_proxy_settings("", "/", "application.conf", "600s", application=True)
            )
            locations = nginx_location(
                "/",
                [
                    *npm_location_pass(f"http://192.168.0.85:{port}", "600s"),
                    "client_max_body_size 128m;",
                    "proxy_request_buffering off;",
                    "proxy_next_upstream off;",
                    "proxy_set_header Upgrade $http_upgrade;",
                    "proxy_set_header Connection $http_connection;",
                    'proxy_set_header X-Forwarded-Prefix "";',
                    "access_log off;",
                ],
            )
        else:
            closed = "location / { return 404; }"
            machine_guard = guard_machine_server(sofia is not None)
            server.extend(machine_guard)
            server.append(closed)
            locations = [closed]
            routes = [("", "sync", SYNC_URI, 4096, "35s", "http://192.168.0.85:3010", None)]
            if sofia is not None:
                routes.append(
                    (
                        "_1",
                        "sofia",
                        SOFIA_URI,
                        12288,
                        "60s",
                        f"https://{sofia['ipv4']}:{sofia['port']}",
                        sofia["server_name"],
                    )
                )
            for suffix, name, uri, limit, timeout, target, server_name in routes:
                filename = f"{name}.conf"
                guard = guard_machine_request(uri, limit)
                settings.update(
                    reverse_proxy_settings(suffix, f"= {uri}", filename, timeout, application=False)
                )
                include_files[filename] = nginx_config([*waf_tls, *guard])
                proxy = [
                    *guard,
                    *npm_location_pass(target, timeout, server_name or "$host"),
                    "proxy_request_buffering on;",
                    'proxy_set_header Connection "";',
                    "proxy_set_header Content-Length $content_length;",
                ]
                if server_name is not None:
                    proxy.extend(
                        [
                            *client_tls(NPM_FILES, "sofia"),
                            "proxy_ssl_server_name on;",
                            f"proxy_ssl_name {server_name};",
                        ]
                    )
                locations.extend(nginx_location(f"= {uri}", proxy))
        server_config = nginx_config(server)
        sites[host] = {
            "cloudflared_origin": site_origin(host, use_relay),
            "bunkerweb": {
                "settings": settings,
                "server_http_path": f"/etc/bunkerweb/configs/server-http/{host}/agency-phase6.conf",
                "server_http": server_config,
                "service_draft_payload": {
                    "server_name": host,
                    "is_draft": True,
                    "variables": {
                        key: item for key, item in settings.items() if key != "SERVER_NAME"
                    },
                },
                "custom_config_payload": {
                    "service": host,
                    "type": "server_http",
                    "name": "agency-phase6",
                    "data": server_config,
                },
            },
            "npm": npm_site(host, locations, machine_guard),
        }
    result = {
        **plan(),
        "candidate_rendered": True,
        "sofia_enabled": sofia is not None,
        "inventory": value,
        "bunkerweb_includes": include_files,
        "sites": sites,
    }
    if sofia is not None:
        result["activation_evidence_required"].append("mTLS de NPM al listener dedicado de Sofía")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", type=Path, metavar="JSON", help="Inventario local sin credenciales"
    )
    args = parser.parse_args(argv)
    result = plan()
    if args.inventory is not None:
        try:
            with args.inventory.open("rb") as stream:
                result = render(inventory(stream.read(MAX_INVENTORY_BYTES + 1)))
        except InputError as error:
            print(str(error), file=sys.stderr)
            return 2
        except OSError:
            print("INVENTARIO_LECTURA", file=sys.stderr)
            return 2
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
