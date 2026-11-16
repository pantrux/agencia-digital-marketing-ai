import json

import pytest

from ingress.phase6_ingress import InputError, inventory, main, plan, render


@pytest.fixture
def values():
    return {
        "schema": 1,
        "observed_bunkerweb_version": "1.6.9",
        "observed_npm_image_id": "sha256:90522c2d426d9b2a7ae02f60365e812e1990a0dd78870ceaf60186b05e05385b",
        "preflight_sha256": "b" * 64,
        "tunnel_peer_ipv4": "192.168.0.19",
        "sofia_tls": {
            "ipv4": "192.168.0.7",
            "port": 3443,
            "server_name": "sofia.example.test",
        },
    }


def test_default_plan_has_no_routes_or_implied_approval(capsys):
    assert main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == plan()
    assert result["candidate_rendered"] is False
    assert result["activation_verified"] is False
    assert "tunnel_update" not in result


def test_public_names_are_exact_and_tunnel_origin_verifies_host(values):
    result = render(values)
    assert result["candidate_rendered"] is True
    assert result["activation_verified"] is False
    assert result["inventory"] == values
    assert set(result["sites"]) == {
        "api-studio.e-scale.cl",
        "kitsu.e-scale.cl",
        "immich.e-scale.cl",
    }
    for host, site in result["sites"].items():
        origin = site["cloudflared_origin"]
        assert origin["hostname"] == host
        assert origin["service"] == "https://192.168.0.8:9443"
        assert origin["originRequest"]["originServerName"] == host
        assert origin["originRequest"]["httpHostHeader"] == host
        assert origin["originRequest"]["noTLSVerify"] is False
        assert origin["originRequest"]["caPool"] == (
            "/etc/agency-studio-tunnel-public/bunkerweb-server-ca.pem"
        )
        guard = site["bunkerweb"]["server_http"]
        settings = site["bunkerweb"]["settings"]
        assert settings["REAL_IP_FROM"] == "192.168.0.19"
        assert settings["REAL_IP_HEADER"] == "CF-Connecting-IP"
        assert f"$ssl_server_name != {host}" in guard
        assert f"$host != {host}" in guard
        assert "$realip_remote_addr != 192.168.0.19" in guard
        assert site["npm"]["ssl_forced"] is True
        assert site["npm"]["enabled"] is False
        assert "trust_forwarded_proto" not in site["npm"]
        assert site["npm"]["caching_enabled"] is False
        assert site["npm"]["forward_host"] == "127.0.0.1"
        assert site["npm"]["forward_port"] == 1


def test_machine_api_is_exact_at_both_hops(values):
    site = render(values)["sites"]["api-studio.e-scale.cl"]
    settings = site["bunkerweb"]["settings"]
    assert settings["USE_MODSECURITY"] == "yes"
    assert settings["REVERSE_PROXY_URL"] == "= /v1/sync"
    assert settings["REVERSE_PROXY_URL_1"] == "= /v1/sofia/chat"
    waf = site["bunkerweb"]["server_http"]
    npm = site["npm"]["advanced_config"]
    assert "location / { return 404; }" in waf and "location / { return 404; }" in npm
    assert all('$request_uri !~ "^/v1/(sync|sofia/chat)$"' in config for config in (waf, npm))
    waf_includes = render(values)["bunkerweb_includes"]
    assert site["cloudflared_origin"]["originRequest"]["disableChunkedEncoding"] is False
    for route, limit, backend in (
        ("/v1/sync", 4096, "http://192.168.0.85:3010"),
        ("/v1/sofia/chat", 12288, "https://192.168.0.7:3443"),
    ):
        assert f"location = {route}" in npm
        assert f"$request_uri != {route}" in npm
        assert f"client_max_body_size {limit};" in npm
        assert f"proxy_pass {backend};" in npm
        waf_location = waf_includes[f"{route.split('/')[2]}.conf"]
        assert f"$request_uri != {route}" in waf_location
        assert f"client_max_body_size {limit};" in waf_location
        for config in (waf_location, npm):
            assert "$request_method != POST" in config
            assert "$http_transfer_encoding" in config
            assert '$content_length = ""' in config
            assert "proxy_next_upstream off;" in config
            assert "proxy_set_header timestamp" not in config
            assert "proxy_set_header signature" not in config
    assert "/healthz" not in npm + waf


def test_origin_relay_requires_client_certificate_at_all_three_hosts(values):
    values.update(schema=2, origin_client_auth="studio-origin-relay-v1")
    result = render(values)
    for host, site in result["sites"].items():
        assert site["cloudflared_origin"] == {
            "hostname": host,
            "service": "unix:/run/agency-studio-origin-relay/origin.sock",
            "originRequest": {"http2Origin": False, "disableChunkedEncoding": False},
        }
        config = site["bunkerweb"]["server_http"]
        assert "studio-origin-relay-ca.pem;" in config
        assert "ssl_verify_client on;" in config
        assert "$ssl_client_verify != SUCCESS" in config
        assert "$realip_remote_addr != 192.168.0.19" in config

    values["origin_client_auth"] = "desconocido"
    with pytest.raises(InputError, match="INVENTARIO_ORIGEN_MTLS"):
        render(values)


def test_sofia_null_closes_chat_without_changing_the_other_hosts(values):
    complete = render(values)
    result = render(values | {"sofia_tls": None})
    assert result["candidate_rendered"] is True
    assert result["sofia_enabled"] is False
    assert complete["sofia_enabled"] is True
    assert result["activation_verified"] is False
    for host in ("kitsu.e-scale.cl", "immich.e-scale.cl"):
        assert result["sites"][host] == complete["sites"][host]
    api = result["sites"]["api-studio.e-scale.cl"]
    assert set(result["bunkerweb_includes"]) == {"application.conf", "sync.conf"}
    assert not any(name.endswith("_1") for name in api["bunkerweb"]["settings"])
    for config in (api["bunkerweb"]["server_http"], api["npm"]["advanced_config"]):
        assert '$request_uri !~ "^/v1/sync$"' in config
        assert "location / { return 404; }" in config
        assert "/v1/sofia/chat" not in config
        assert "192.168.0.7" not in config
    assert "location = /v1/sync" in api["npm"]["advanced_config"]


def test_bunkerweb_draft_payload_is_only_targeted_at_new_service(values):
    result = render(values)
    for host, site in result["sites"].items():
        waf = site["bunkerweb"]
        draft = waf["service_draft_payload"]
        assert draft == {
            "server_name": host,
            "is_draft": True,
            "variables": {k: v for k, v in waf["settings"].items() if k != "SERVER_NAME"},
        }
        assert waf["custom_config_payload"] == {
            "service": host,
            "type": "server_http",
            "name": "agency-phase6",
            "data": waf["server_http"],
        }


def test_mtls_and_application_host_paths(values):
    result = render(values)
    assert result["nas_material_directories"] == {
        "staging_host": "/volume2/agency-studio-ingress-private-v2",
        "bunkerweb_host": "/volume1/docker/bunkerweb/data/logs/agency-phase6",
        "bunkerweb_container": "/var/log/bunkerweb/agency-phase6",
        "scheduler_host": "/volume1/docker/bunkerweb/data/storage/agency-phase6",
        "scheduler_container": "/data/agency-phase6",
        "npm_host": "/volume1/docker/npm/data/agency-phase6",
        "npm_container": "/data/agency-phase6",
    }
    for host, site in result["sites"].items():
        settings = site["bunkerweb"]["settings"]
        assert settings["REVERSE_PROXY_SSL_SNI_NAME"] == host
        assert settings["REVERSE_PROXY_HOST"] == "https://nginx-proxy-manager:443"
        assert settings["LISTEN_HTTP"] == "no"
        assert settings["CUSTOM_SSL_CERT"] == f"/data/agency-phase6/{host}.pem"
        npm = site["npm"]["advanced_config"]
        assert "ssl_client_certificate /data/agency-phase6/bunkerweb-client-ca.pem;" in npm
        assert "ssl_verify_client on;" in npm
    for body in result["bunkerweb_includes"].values():
        assert "resolver 127.0.0.11 valid=15s ipv6=off;" in body
        assert "proxy_ssl_verify on;" in body
        assert (
            "proxy_ssl_trusted_certificate /var/log/bunkerweb/agency-phase6/npm-server-ca.pem;"
            in body
        )
    api = result["sites"]["api-studio.e-scale.cl"]["npm"]["advanced_config"]
    assert "proxy_ssl_name sofia.example.test;" in api
    assert "proxy_set_header Host sofia.example.test;" in api
    assert "proxy_set_header X-Forwarded-Host $host;" in api
    assert "proxy_ssl_verify on;" in api
    assert "proxy_ssl_certificate_key /data/agency-phase6/sofia-client.key;" in api
    for host, port in (("kitsu", 8081), ("immich", 2283)):
        site = result["sites"][f"{host}.e-scale.cl"]
        settings = site["bunkerweb"]["settings"]
        assert settings["REVERSE_PROXY_URL"] == "/"
        assert settings["REVERSE_PROXY_WS"] == "yes"
        advanced = site["npm"]["advanced_config"]
        assert f"proxy_pass http://192.168.0.85:{port};" in advanced
        assert "proxy_set_header Upgrade $http_upgrade;" in advanced
        assert "proxy_request_buffering off;" in advanced


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("observed_bunkerweb_version", "latest"),
        ("observed_npm_image_id", "latest"),
        ("observed_npm_image_id", "sha256:" + "a" * 64),
        ("preflight_sha256", "a" * 63),
        ("tunnel_peer_ipv4", "192.168.0.0/24"),
        ("tunnel_peer_ipv4", "8.8.8.8"),
        ("tunnel_peer_ipv4", "127.0.0.1"),
        ("sofia_tls", {}),
        ("sofia_tls", False),
        ("sofia_tls", {"ipv4": "192.168.0.85", "port": 3443, "server_name": "sofia.example.test"}),
        ("sofia_tls", {"ipv4": "192.168.0.7", "port": 18789, "server_name": "sofia.example.test"}),
        ("sofia_tls", {"ipv4": "192.168.0.7", "port": 3107, "server_name": "sofia.example.test"}),
        ("sofia_tls", {"ipv4": "192.168.0.7", "port": 3108, "server_name": "sofia.example.test"}),
        (
            "sofia_tls",
            {"ipv4": "192.168.0.7", "port": 3443, "server_name": "sofia.example.test; x"},
        ),
        ("sofia_tls", {"ipv4": "192.168.0.7", "port": True, "server_name": "sofia.example.test"}),
    ],
)
def test_inventory_rejects_wrong_closed_inputs(values, field, value):
    values[field] = value
    with pytest.raises(InputError):
        render(values)


def test_inventory_rejects_duplicates_overlimit_and_unknown_fields(values):
    assert inventory(json.dumps(values).encode()) == values
    with pytest.raises(InputError):
        inventory(b'{"schema":1,"schema":1}')
    with pytest.raises(InputError):
        inventory(b" " * 4097)
    with pytest.raises(InputError):
        render(values | {"studio": "alternate"})


def test_cli_emits_deterministic_output_and_sanitizes_bad_input(values, tmp_path, capsys):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(values), encoding="ascii")
    assert main(["--inventory", str(path)]) == 0
    first = capsys.readouterr().out
    assert main(["--inventory", str(path)]) == 0
    assert capsys.readouterr().out == first
    path.write_text('{"item":"sensitive-fixture"}', encoding="ascii")
    assert main(["--inventory", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sensitive-fixture" not in captured.err
