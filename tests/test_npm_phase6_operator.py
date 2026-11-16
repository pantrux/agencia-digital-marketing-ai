import ast
import json
from pathlib import Path

import pytest

from ingress import npm_phase6_operator as operator

HOST = "kitsu.e-scale.cl"
VERSION = {"version": {"major": 2, "minor": 13, "revision": 5}}


def test_sources_parse_in_dsm_python_38_without_dict_union_syntax():
    candidate = Path(operator.__file__)
    for source in (candidate, candidate.with_name("phase6_ingress.py")):
        root = ast.parse(source.read_bytes(), feature_version=(3, 8))
        for node in ast.walk(root):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                assert not isinstance(node.left, (ast.Dict, ast.DictComp))
                assert not isinstance(node.right, (ast.Dict, ast.DictComp))


class FakeApi:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def call(self, method, route, status=200, **kwargs):
        self.calls.append((method, route, status, kwargs))
        return next(self.responses)


def certificate(material=False):
    return {
        "id": 11,
        "provider": "other",
        "domain_names": [HOST],
        "nice_name": operator.certificate_name(HOST),
        "meta": {"certificate": material, "certificate_key": material},
        "owner_user_id": 2,
        "created_on": "created",
        "modified_on": "modified",
        "expires_on": "expiry",
    }


def inventory():
    return {
        "schema": 1,
        "observed_bunkerweb_version": "1.6.9",
        "observed_npm_image_id": operator.NPM_IMAGE,
        "preflight_sha256": "a" * 64,
        "tunnel_peer_ipv4": "192.168.0.85",
        "sofia_tls": None,
    }


def host_row(enabled=False, online=False):
    return operator.render_npm(inventory(), HOST) | {
        "id": 21,
        "certificate_id": 11,
        "owner_user_id": 2,
        "modified_on": "date",
        "enabled": enabled,
        "locations": [],
        "access_list_id": 0,
        "http2_support": False,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "block_exploits": False,
        "meta": {"nginx_online": online},
    }


def test_plan_and_reject_foreign_inputs(capsys):
    assert operator.main([]) == 0
    assert json.loads(capsys.readouterr().out)["hosts"] == list(operator.HOSTS)
    with pytest.raises(operator.Rejected, match="ENTRADA_HOST"):
        operator.data_for(
            operator.encode({"host": "studio.e-scale.cl", "token": "x" * 40}), "inspect"
        )


def test_certificate_lookup_rejects_wildcard_collision():
    api = FakeApi([VERSION, [{"id": 9, "domain_names": ["*.e-scale.cl"]}]])
    with pytest.raises(operator.Rejected, match="NPM_COLISION_DOMINIO"):
        operator.operate(api, "certificate-create", {"host": HOST})
    assert all(method == "GET" for method, *_ in api.calls)


def test_certificate_returns_only_identity_and_requires_snapshot_for_upload(monkeypatch):
    create = FakeApi([VERSION, [], certificate()])
    proof = operator.operate(create, "certificate-create", {"host": HOST})
    assert proof["id"] == 11 and proof["material"] is False
    assert list(json.loads(create.calls[-1][3]["data"])) == [
        "domain_names",
        "nice_name",
        "provider",
    ]
    upload = {
        "host": HOST,
        "certificate_id": 11,
        "certificate_preimage_sha256": proof["row_sha256"],
        "certificate": "-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----\n",
        "certificate_key": "-----BEGIN PRIVATE KEY-----\nYWJj\n-----END PRIVATE KEY-----\n",
    }
    begun = []
    monkeypatch.setattr(operator, "private_empty_certificate_directory", begun.append)
    monkeypatch.setattr(operator, "installed_material", lambda ident, _: begun.append(ident))
    rejected = FakeApi([VERSION, certificate()])
    with pytest.raises(operator.Rejected, match="NPM_PREIMAGEN"):
        operator.operate(
            rejected, "certificate-upload", upload | {"certificate_preimage_sha256": "a" * 64}
        )
    assert not begun and all(method == "GET" for method, *_ in rejected.calls)
    api = FakeApi([VERSION, certificate(), None, certificate(True)])
    output = operator.operate(api, "certificate-upload", upload)
    assert output["material"] is True and b"PRIVATE KEY" not in operator.encode(output)
    request = api.calls[-2]
    assert request[1].endswith("/11/upload") and request[3]["read"] is False
    assert b'name="certificate_key"' in request[3]["data"] and begun == [11, 11]


def test_proxy_created_disabled_and_activation_requires_post_state():
    cert = operator.certificate_row(certificate(True), HOST, 11)
    api = FakeApi([VERSION, certificate(True), [], [], [], host_row(), host_row()])
    proof = operator.operate(
        api,
        "host-create",
        {
            "host": HOST,
            "certificate_id": 11,
            "certificate_preimage_sha256": operator.sha(operator.encode(cert)),
            "inventory": inventory(),
        },
    )
    post = [call for call in api.calls if call[0] == "POST"]
    assert len(post) == 1 and json.loads(post[0][3]["data"])["enabled"] is False
    enable = {"host": HOST, "host_id": proof["id"], "host_preimage_sha256": proof["row_sha256"]}
    wrong = FakeApi([VERSION, host_row(), None, host_row(enabled=True)])
    with pytest.raises(operator.Rejected, match="NPM_HOST_POSTCONDICION"):
        operator.operate(wrong, "host-enable", enable)
    working = FakeApi([VERSION, host_row(), None, host_row(enabled=True, online=True)])
    after = operator.operate(working, "host-enable", enable)
    assert after["enabled"] is True and after["nginx_online"] is True
    wrong_id = FakeApi([VERSION, host_row() | {"id": 22}])
    with pytest.raises(operator.Rejected, match="NPM_HOST_ID"):
        operator.operate(wrong_id, "host-enable", enable)
    assert all(method == "GET" for method, *_ in wrong_id.calls)
    reverse = FakeApi([VERSION, host_row(enabled=True, online=True), None, host_row()])
    before = operator.operate(
        reverse, "host-disable", enable | {"host_preimage_sha256": after["row_sha256"]}
    )
    assert before["enabled"] is False
