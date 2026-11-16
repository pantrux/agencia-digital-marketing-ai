import datetime as dt
import hashlib
import http.client
import importlib.util
import socket
import ssl
import stat
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MODULE = (
    Path(__file__).resolve().parents[1] / "openclaw-agency-setup/sofia-broker/sofia_mtls_proxy.py"
)
SPEC = importlib.util.spec_from_file_location("sofia_mtls_proxy", MODULE)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)
SERVER = "sofia-fixture.example.invalid"
CLIENT = "npm-fixture.example.invalid"
RAW = b'{"message":"video de prueba"}'
STAMP = "1794037200"
SIGNATURE = "v1=" + "b" * 64


def certificates(root):
    now = dt.datetime.now(dt.UTC)

    def issue(name, ca, purpose=None):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        issuer_key, issuer_dn = (key, subject) if ca is None else (ca[0], ca[1].subject)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer_dn)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=ca is None, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=ca is None,
                    crl_sign=ca is None,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
        )
        if ca is not None:
            builder = (
                builder.add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False
                )
                .add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                    critical=False,
                )
            )
        cert = builder.sign(issuer_key, hashes.SHA256())
        return key, cert

    def write(name, pair):
        key, cert = pair
        (root / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root / f"{name}.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    server_ca = issue("server-test-ca", None)
    client_ca = issue("client-test-ca", None)
    write("server-ca", server_ca)
    write("client-ca", client_ca)
    write("server", issue(SERVER, server_ca, ExtendedKeyUsageOID.SERVER_AUTH))
    write("other-client", issue(CLIENT, client_ca, ExtendedKeyUsageOID.CLIENT_AUTH))
    write("untrusted-client", issue(CLIENT, server_ca, ExtendedKeyUsageOID.CLIENT_AUTH))
    return write("npm-client", issue(CLIENT, client_ca, ExtendedKeyUsageOID.CLIENT_AUTH))


@pytest.fixture
def material(tmp_path):
    pin = certificates(tmp_path)
    return tmp_path, proxy.profile(
        {
            "server_name": SERVER,
            "npm_client_dns": CLIENT,
            "npm_client_sha256": pin,
        }
    )


@contextmanager
def listen(material, allowed_peer="127.0.0.1"):
    directory, settings = material
    requests = []

    def forward(*args):
        requests.append(args)
        return 200, RAW, {"timestamp": STAMP, "signature": SIGNATURE}

    tls = proxy.context(directory, settings)
    with proxy.EdgeServer(
        tls,
        settings,
        forward_request=forward,
        address=("127.0.0.1", 0),
        allowed_peer=allowed_peer,
    ) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_address, requests
        finally:
            server.shutdown()
            thread.join(3)


def query(
    address,
    directory,
    *,
    identity="npm-client",
    server_name=SERVER,
    route=proxy.ROUTE,
    method="POST",
    add_headers=(),
    host=SERVER,
    signature=SIGNATURE,
):
    secure = ssl.create_default_context(cafile=str(directory / "server-ca.pem"))
    if identity is not None:
        secure.load_cert_chain(directory / f"{identity}.pem", directory / f"{identity}.key")
    if server_name is None:
        secure.check_hostname = False
    with socket.create_connection(address, timeout=2) as tcp:
        with secure.wrap_socket(tcp, server_hostname=server_name) as tls:
            headers = [
                ("Host", host),
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(RAW))),
                ("timestamp", STAMP),
            ]
            if signature is not None:
                headers.append(("signature", signature))
            headers.extend(add_headers)
            wire = f"{method} {route} HTTP/1.1\r\n" + "".join(
                f"{name}: {value}\r\n" for name, value in headers
            )
            tls.sendall(wire.encode("ascii") + b"\r\n" + RAW)
            with http.client.HTTPResponse(tls, method=method) as response:
                response.begin()
                return response.status, response.read(), dict(response.headers)


def test_authenticated_tls_preserves_payload_and_signatures(material):
    with listen(material) as (address, requests):
        status, body, headers = query(address, material[0])
    assert (status, body) == (200, RAW)
    assert (headers["timestamp"], headers["signature"]) == (STAMP, SIGNATURE)
    assert requests == [(RAW, STAMP, SIGNATURE)]
    assert set(headers) == {
        "Content-Type",
        "Content-Length",
        "Cache-Control",
        "Connection",
        "timestamp",
        "signature",
    }


@pytest.mark.parametrize("identity", [None, "other-client", "untrusted-client"])
def test_tls_rejects_absent_other_pinned_or_untrusted_leaf(material, identity):
    with listen(material) as (address, requests):
        with pytest.raises((OSError, http.client.HTTPException)):
            query(address, material[0], identity=identity)
        assert not requests


def test_tls_sni_and_tcp_peer_are_independent_gates(material):
    with listen(material) as (address, requests):
        for name in (None, "other-name.example.invalid"):
            with pytest.raises(ssl.SSLError):
                query(address, material[0], server_name=name)
        assert not requests
    with listen(material, allowed_peer="127.0.0.2") as (address, requests):
        with pytest.raises((OSError, http.client.HTTPException)):
            query(address, material[0])
        assert not requests


@pytest.mark.parametrize(
    "kwargs,status",
    [
        ({"route": "/v1/sofia/chat?x=1"}, 404),
        ({"route": "//v1/sofia/chat"}, 404),
        ({"method": "GET"}, 405),
        ({"host": "otro.example.invalid"}, 421),
        ({"add_headers": [("Host", SERVER)]}, 421),
        ({"add_headers": [("Content-Length", str(len(RAW)))]}, 400),
        ({"add_headers": [("Transfer-Encoding", "chunked")]}, 400),
        ({"add_headers": [("signature", SIGNATURE)]}, 401),
        ({"add_headers": [("Expect", "100-continue")]}, 417),
        ({"signature": None}, 401),
    ],
)
def test_tls_http_rejects_ambiguous_framing_and_extra_routes(material, kwargs, status):
    with listen(material) as (address, requests):
        assert query(address, material[0], **kwargs)[0] == status
        assert not requests


def test_tls_header_limit_closes_connection_before_forwarding(material):
    with listen(material) as (address, requests):
        with pytest.raises((OSError, http.client.HTTPException)):
            query(address, material[0], add_headers=[("X-Filler", "a" * (proxy.HEADER_LIMIT + 1))])
        assert not requests


def test_fixed_upstream_reframing_preserves_exact_body_and_ignores_extra_headers(monkeypatch):
    observed = []

    class LocalBroker(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append(
                (
                    self.path,
                    dict(self.headers),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(RAW)))
            self.send_header("timestamp", STAMP)
            self.send_header("signature", SIGNATURE)
            self.send_header("X-Private", "synthetic ignored value")
            self.end_headers()
            self.wfile.write(RAW)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), LocalBroker) as target:
        thread = threading.Thread(target=target.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            monkeypatch.setattr(proxy, "BROKER", target.server_address)
            assert proxy.forward(RAW, STAMP, SIGNATURE) == (
                200,
                RAW,
                {"timestamp": STAMP, "signature": SIGNATURE},
            )
        finally:
            target.shutdown()
            thread.join(3)
    assert len(observed) == 1
    route, headers, raw = observed[0]
    assert (route, raw) == (proxy.ROUTE, RAW)
    assert (headers["timestamp"], headers["signature"]) == (STAMP, SIGNATURE)


@pytest.fixture
def credential_metadata(monkeypatch):
    class FakePath(PurePosixPath):
        def resolve(self, strict=False):
            return self

        def lstat(self):
            return tree[self]

    credentials = FakePath("/run/credentials")
    managed = credentials / proxy.MANAGED_UNIT
    direct = FakePath("/owner-private-fixture")
    names = ("profile.json", "server.pem", "server.key", "client-ca.pem")

    def metadata(uid, gid, mode, kind=stat.S_IFREG):
        return SimpleNamespace(
            st_uid=uid, st_gid=gid, st_mode=kind | mode, st_nlink=1, st_size=1024
        )

    tree = {
        managed: metadata(0, 0, 0o550, stat.S_IFDIR),
        direct: metadata(1000, 1000, 0o700, stat.S_IFDIR),
        **{managed / name: metadata(0, 0, 0o440) for name in names},
        **{direct / name: metadata(1000, 1000, 0o400) for name in names},
    }
    monkeypatch.setattr(proxy.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(proxy.os, "access", lambda path, mode: True)
    monkeypatch.setattr(proxy, "CREDENTIALS_ROOT", credentials)
    return managed, direct, tree, metadata


def test_credential_contract_uses_systemd_acl_or_explicit_private_owner(credential_metadata):
    managed, direct, _, _ = credential_metadata
    proxy.check_credentials(managed)
    proxy.check_credentials(direct)


@pytest.mark.parametrize(
    "filename,uid,gid,mode,kind",
    [
        ("server.key", 1000, 0, 0o440, stat.S_IFREG),
        ("server.key", 0, 1000, 0o440, stat.S_IFREG),
        ("server.key", 0, 0, 0o420, stat.S_IFREG),
        ("server.key", 0, 0, 0o440, stat.S_IFLNK),
        ("", 1000, 0, 0o550, stat.S_IFDIR),
        ("", 0, 0, 0o555, stat.S_IFDIR),
    ],
)
def test_managed_credentials_reject_metadata_drift(
    credential_metadata, filename, uid, gid, mode, kind
):
    managed, _, tree, metadata = credential_metadata
    tree[managed / filename] = metadata(uid, gid, mode, kind)
    with pytest.raises(ValueError, match="SOFIA_TLS_CREDENTIAL"):
        proxy.check_credentials(managed)


def test_managed_credentials_refuse_wrong_unit_and_actual_unreadability(
    credential_metadata, monkeypatch
):
    managed, direct, tree, _ = credential_metadata
    wrong_unit = proxy.CREDENTIALS_ROOT / "another.service"
    tree[wrong_unit] = tree[managed]
    with pytest.raises(ValueError, match="SOFIA_TLS_CREDENTIAL_DIR"):
        proxy.check_credentials(wrong_unit)
    monkeypatch.setattr(proxy.os, "access", lambda path, mode: path.name != "server.key")
    with pytest.raises(ValueError, match="SOFIA_TLS_CREDENTIAL_FILE"):
        proxy.check_credentials(managed)
    with pytest.raises(ValueError, match="SOFIA_TLS_CREDENTIAL_FILE"):
        proxy.check_credentials(direct)
