"""Renderiza configuración del relay local y rutas propuestas del túnel."""

import argparse
import json
import sys

SOCKET = "/run/agency-studio-origin-relay/origin.sock"
HOSTS = ("api-studio.e-scale.cl", "kitsu.e-scale.cl", "immich.e-scale.cl")


def tunnel_origins():
    return [
        {
            "hostname": host,
            "service": f"unix:{SOCKET}",
            "originRequest": {"http2Origin": False, "disableChunkedEncoding": False},
        }
        for host in HOSTS
    ]


def server_config(host, sofia=False):
    if host not in HOSTS:
        raise ValueError("Host fuera del alcance del relay.")
    proxy = f"""            proxy_set_header Host {host};
            proxy_pass https://192.168.0.8:9443;"""
    if host == HOSTS[0]:
        guard = (
            'if ($request_uri !~ "^/v1/(sync|sofia/chat)$") { return 404; }'
            if sofia
            else "if ($request_uri != /v1/sync) { return 404; }"
        )
        routes = [("/v1/sync", 4096, 45)]
        if sofia:
            routes.append(("/v1/sofia/chat", 12288, 60))
        locations = f"        {guard}\n        location / {{ return 404; }}"
        for path, size, timeout in routes:
            locations += f"""
        location = {path} {{
            if ($request_method != POST) {{ return 405; }}
            if ($http_transfer_encoding != "") {{ return 400; }}
            if ($content_length = "") {{ return 411; }}
            if ($http_upgrade != "") {{ return 400; }}
            client_max_body_size {size};
            proxy_request_buffering on;
            proxy_set_header Content-Length $content_length;
            proxy_set_header Connection "";
            proxy_send_timeout {timeout}s;
            proxy_read_timeout {timeout}s;
{proxy}
        }}"""
    else:
        locations = f"""        location / {{
            client_max_body_size 128m;
            proxy_request_buffering off;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $relay_connection;
            proxy_send_timeout 600s;
            proxy_read_timeout 600s;
{proxy}
        }}"""
    return f"""    server {{
        listen unix:{SOCKET};
        server_name {host};
        if ($http_host != {host}) {{ return 421; }}
        proxy_ssl_name {host};
{locations}
    }}"""


def nginx_config(sofia=False):
    servers = "\n\n".join(server_config(host, sofia) for host in HOSTS)
    return f"""daemon off;
worker_processes 1;
worker_shutdown_timeout 10s;
pid /tmp/agency-relay.pid;
error_log /dev/null crit;

events {{
    worker_connections 256;
}}

http {{
    access_log off;
    server_tokens off;
    client_body_temp_path /tmp/client-body;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    client_header_timeout 10s;
    client_body_timeout 30s;
    send_timeout 30s;

    map $http_upgrade $relay_connection {{
        default upgrade;
        '' close;
    }}

    proxy_http_version 1.1;
    proxy_connect_timeout 5s;
    proxy_next_upstream off;
    proxy_buffering off;
    proxy_cache off;
    proxy_redirect off;
    proxy_intercept_errors off;
    proxy_ssl_server_name on;
    proxy_ssl_verify on;
    proxy_ssl_verify_depth 2;
    proxy_ssl_session_reuse off;
    proxy_ssl_protocols TLSv1.2 TLSv1.3;
    proxy_ssl_trusted_certificate /etc/agency-relay/tls/bunkerweb-server-ca.pem;
    proxy_ssl_certificate /etc/agency-relay/tls/relay-client.pem;
    proxy_ssl_certificate_key /etc/agency-relay/tls/relay-client.key;

    server {{
        listen unix:{SOCKET} default_server;
        server_name _;
        return 421;
    }}

{servers}
}}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("format", choices=("nginx", "tunnel-origins"))
    parser.add_argument("--sofia", action="store_true")
    args = parser.parse_args()
    if args.format == "nginx":
        # Los mismos bytes, incluso al redirigir stdout desde Windows.
        sys.stdout.buffer.write(nginx_config(args.sofia).encode("ascii"))
    else:
        output = json.dumps(tunnel_origins(), sort_keys=True, indent=2) + "\n"
        sys.stdout.buffer.write(output.encode("ascii"))


if __name__ == "__main__":
    main()
