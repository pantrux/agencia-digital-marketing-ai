"""Aceptación de roles Cargo observados, sin SDK ni infraestructura."""

import copy
import importlib.util
import io
import json
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location(
        "postverify_accept_contract", ROOT / "docker/postgres-immich/postverify_accept_remote.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(remote, changes=None):
    root, build, library = "libsqlite3-sys/", remote.BUILD_HASH, remote.LIB_HASH
    depid = 13519812633353482459
    fingerprint = 4494643329962863696
    flags = [
        "-Dwarnings",
        "-Ctarget-feature=-crt-static",
        "-Cpanic=unwind",
        "-Clink-arg=-Wl,-rpath,$ORIGIN",
    ]
    run = {
        "rustflags": flags,
        "deps": [[depid, "build_script_build", False, 7481212355534181655]],
        "local": [
            {"RerunIfEnvChanged": {"var": k, "val": v}}
            for k, v in {
                "LIBSQLITE3_SYS_USE_PKG_CONFIG": "1",
                "SQLITE3_INCLUDE_DIR": "/opt/agency-sqlite-v3/include",
                "SQLITE3_LIB_DIR": "/opt/agency-sqlite-v3/lib",
                "SQLITE3_STATIC": None,
                "SQLITE3_DYNAMIC": "1",
                "PKG_CONFIG_PATH": "/opt/agency-sqlite-v3/lib/pkgconfig",
            }.items()
        ],
    }
    lib = {
        "rustflags": flags,
        "deps": [[depid, "build_script_build", False, fingerprint]],
        "features": json.dumps(
            [
                "bundled",
                "bundled_bindings",
                "cc",
                "default",
                "min_sqlite_version_3_34_1",
                "pkg-config",
                "vcpkg",
            ]
        ),
        "local": [
            {
                "CheckDepInfo": {
                    "dep_info": "x86_64-unknown-linux-musl/release/build/"
                    + root
                    + library
                    + "/fingerprint/dep-lib-libsqlite3_sys",
                    "fingerprint": "mtime",
                }
            }
        ],
    }
    bindgen = remote.ARCHIVE_PATH + "/" + build + "/out/bindgen.rs"
    out = remote.ARCHIVE_PATH + "/" + library + "/out/"
    contents = {
        name: b"x" for name, row in remote.EXPECTED_MEMBERS.items() if row["kind"] == "file"
    }
    contents.update(
        {
            root
            + build
            + "/fingerprint/run-build-script-build-script-build.json": remote.canonical(run),
            root + build + "/fingerprint/run-build-script-build-script-build": fingerprint.to_bytes(
                8, "little"
            )
            .hex()
            .encode(),
            root + build + "/run/root-output": (
                remote.ARCHIVE_PATH + "/" + build + "/out"
            ).encode(),
            root + build + "/run/stderr": b"",
            root
            + build
            + "/run/stdout": b"cargo:rustc-link-search=native=/opt/agency-sqlite-v3/lib\ncargo:rustc-link-lib=sqlite3\n",
            root + library + "/fingerprint/lib-libsqlite3_sys.json": remote.canonical(lib),
            root + library + "/out/libsqlite3_sys-" + library + ".d": (
                "\n".join(
                    [
                        out
                        + name
                        + ": /work/vendor/libsqlite3-sys-0.36.0/src/lib.rs /work/vendor/libsqlite3-sys-0.36.0/src/error.rs "
                        + bindgen
                        for name in (
                            "libsqlite3_sys-" + library + ".d",
                            "liblibsqlite3_sys-" + library + ".rlib",
                            "liblibsqlite3_sys-" + library + ".rmeta",
                        )
                    ]
                )
                + "\n# env-dep:OUT_DIR="
                + remote.ARCHIVE_PATH
                + "/"
                + build
                + "/out\n"
            ).encode(),
        }
    )
    if changes:
        changes(contents)
    inventory = copy.deepcopy(remote.EXPECTED_MEMBERS)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as target:
        for name, row in inventory.items():
            member = tarfile.TarInfo(name)
            member.mode, member.uid, member.gid = row["mode"], row["uid"], row["gid"]
            if row["kind"] == "directory":
                member.type = tarfile.DIRTYPE
                target.addfile(member)
            else:
                data = contents[name]
                member.size = row["size"] = len(data)
                row["sha256"] = remote.digest(data)
                target.addfile(member, io.BytesIO(data))
    return stream.getvalue(), inventory


class CargoRoles(unittest.TestCase):
    def setUp(self):
        self.remote = load()

    def parse(self, change=None):
        raw, expected = fixture(self.remote, change)
        with patch.object(self.remote, "EXPECTED_MEMBERS", expected):
            return self.remote.extract_build_output(raw)

    def test_roles_distintos_enlazan_fingerprint_dependencias_y_bindgen(self):
        result = self.parse()
        self.assertEqual(result["layout"], "cargo-1.100-roles")
        self.assertEqual(result["build_hash"], self.remote.BUILD_HASH)
        self.assertEqual(result["library_hash"], self.remote.LIB_HASH)
        self.assertTrue(result["relationship_verified"])
        self.assertTrue(result["path"].endswith("/run/stdout"))

    def test_fingerprint_de_biblioteca_no_puede_apuntar_otro_run(self):
        def change(contents):
            name = "libsqlite3-sys/" + self.remote.LIB_HASH + "/fingerprint/lib-libsqlite3_sys.json"
            value = json.loads(contents[name])
            value["deps"][0][3] += 1
            contents[name] = self.remote.canonical(value)

        with self.assertRaises(ValueError):
            self.parse(change)

    def test_depinfo_debe_apuntar_bindgen_del_hash_build(self):
        def change(contents):
            name = (
                "libsqlite3-sys/"
                + self.remote.LIB_HASH
                + "/out/libsqlite3_sys-"
                + self.remote.LIB_HASH
                + ".d"
            )
            contents[name] = contents[name].replace(self.remote.BUILD_HASH.encode(), b"0" * 16)

        with self.assertRaises(ValueError):
            self.parse(change)

    def test_env_sqlite_estatico_rechaza_aunque_inventario_se_recalcule(self):
        def change(contents):
            name = (
                "libsqlite3-sys/"
                + self.remote.BUILD_HASH
                + "/fingerprint/run-build-script-build-script-build.json"
            )
            value = json.loads(contents[name])
            value["local"][3]["RerunIfEnvChanged"]["val"] = "0"
            contents[name] = self.remote.canonical(value)

        with self.assertRaises(ValueError):
            self.parse(change)

    def test_extra_o_contenido_alterado_rechaza(self):
        raw, expected = fixture(self.remote)
        # El inventario de referencia queda intacto: no basta un TAR de estructura parecida.
        changed = raw.replace(b"cargo:rustc-link-lib=sqlite3", b"cargo:rustc-link-lib=evilxx", 1)
        with patch.object(self.remote, "EXPECTED_MEMBERS", expected), self.assertRaises(ValueError):
            self.remote.extract_build_output(changed)


class AcceptanceBoundary(unittest.TestCase):
    def test_framing_llega_al_collector_frozen_para_exito_y_fallo(self):
        spec = importlib.util.spec_from_file_location(
            "prior_postverify_tests", ROOT / "tests/test_postgres_immich_postverify.py"
        )
        prior = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prior)
        with patch.object(prior, "load", return_value=load()):
            prior.FramingContract().test_main_informa_success_que_acepta_collector_v4_real()

    def test_sql_vchord_se_copia_literal_solo_instalacion_actual_y_upgrades(self):
        import zipfile

        remote = load()
        vector = "pgvector-159b79aaad5983fb7459c1e3df2897fbb2d11788/"
        vchord = "VectorChord-f9b5e234c44cee69b266b8431f92b7deed4e99d3/"

        def gz(rows):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as target:
                for name, data in rows.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    target.addfile(member, io.BytesIO(data))
            return stream.getvalue()

        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as target:
            target.writestr("sqlite-amalgamation-3540100/sqlite3.c", b"notice\n" * 30)
        files = {
            "pgvector-source.tar.gz": gz({vector + "LICENSE": b"MIT"}),
            "vectorchord-source.tar.gz": gz(
                {
                    vchord + "vchord.control": b"vchord control",
                    vchord + "sql/install/vchord--1.1.1.sql": b"install current",
                    vchord + "sql/install/vchord--0.1.0.sql": b"old install excluded",
                    vchord + "sql/upgrade/vchord--1.1.0--1.1.1.sql": b"upgrade exact",
                }
            ),
            "sqlite-amalgamation-3540100.zip": zipped.getvalue(),
        }

        class Normal:
            @staticmethod
            def read_file(_fd, name, **_kwargs):
                return files[name]

        class Store:
            descriptors = {"inputs": 123}

        result = remote.source_texts(Normal(), Store())
        prefix = "usr/local/share/postgresql/extension/"
        self.assertEqual(result[prefix + "vchord.control"], b"vchord control")
        self.assertEqual(result[prefix + "vchord--1.1.1.sql"], b"install current")
        self.assertEqual(result[prefix + "vchord--1.1.0--1.1.1.sql"], b"upgrade exact")
        self.assertNotIn(prefix + "vchord--0.1.0.sql", result)

    def test_extra_de_otro_hash_se_rechaza_sin_ignorar_miembros(self):
        remote = load()
        raw, expected = fixture(remote)
        # Se agrega un miembro al TAR, en vez de fabricar un resultado del parser.
        stream = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source,
            tarfile.open(fileobj=stream, mode="w") as target,
        ):
            for member in source:
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
            member = tarfile.TarInfo("libsqlite3-sys/" + "0" * 16)
            member.type = tarfile.DIRTYPE
            target.addfile(member)
        with patch.object(remote, "EXPECTED_MEMBERS", expected), self.assertRaises(ValueError):
            remote.extract_build_output(stream.getvalue())


if __name__ == "__main__":
    unittest.main()
