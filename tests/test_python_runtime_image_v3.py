"""Contratos de la factory: datos TAR/OCI/ELF, sin Docker ni intérprete del runtime."""

import ast
import copy
import gzip
import hashlib
import io
import json
import os
import stat
import struct
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name="runtime_image_inventory_v3"):
    path = ROOT / "docker/python-runtime" / (name + ".py")
    module = types.ModuleType(name + "_tests")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
    return module


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for name, raw in entries:
            node = tarfile.TarInfo(name)
            node.mode = 0o644
            node.size = len(raw)
            archive.addfile(node, io.BytesIO(raw))
    return output.getvalue()


def elf(needed=(), soname=None, defined=(), undefined=()):
    """ELF64 real mínimo con .dynamic/.dynsym; sólo se interpreta como bytes."""
    strings = bytearray(b"\0")

    def label(text):
        offset = len(strings)
        strings.extend(text.encode() + b"\0")
        return offset

    dynamic = [(1, label(name)) for name in needed]
    if soname:
        dynamic.append((14, label(soname)))
    symbols = bytes(24) + b"".join(
        struct.pack("<IBBHQQ", label(name), 0x12, 0, 1 if present else 0, 0, 0)
        for present, names in ((True, defined), (False, undefined))
        for name in names
    )
    dynamic_raw = b"".join(struct.pack("<qQ", key, value) for key, value in [*dynamic, (0, 0)])
    offset = 64 + 4 * 64
    sections = [bytes(64)]
    for kind, body, link, width in (
        (3, bytes(strings), 0, 0),
        (6, dynamic_raw, 1, 16),
        (11, symbols, 1, 24),
    ):
        sections.append(
            struct.pack("<IIQQQQIIQQ", 0, kind, 0, 0, offset, len(body), link, 0, 1, width)
        )
        offset += len(body)
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 3, 62)
    struct.pack_into("<Q", header, 40, 64)
    struct.pack_into("<HH", header, 58, 64, 4)
    return bytes(header) + b"".join(sections) + bytes(strings) + dynamic_raw + symbols


def oci(layer, *, config_extra=None):
    config = canonical(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha(layer)]},
            **(config_extra or {}),
        }
    )
    manifest = canonical(
        {
            "schemaVersion": 2,
            "config": {"digest": "sha256:" + sha(config), "size": len(config)},
            "layers": [
                {
                    "digest": "sha256:" + sha(layer),
                    "size": len(layer),
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                }
            ],
        }
    )
    index = canonical(
        {
            "schemaVersion": 2,
            "manifests": [{"digest": "sha256:" + sha(manifest), "size": len(manifest)}],
        }
    )
    return tar_bytes(
        [
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ("index.json", index),
            *(("blobs/sha256/" + sha(raw), raw) for raw in (config, manifest, layer)),
        ]
    ), "sha256:" + sha(manifest)


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.parser = self.m.load_parser(
            (ROOT / "docker/postgres-immich/final_image_remote.py").read_bytes()
        )

    def test_loader_ast_cierra_globals_y_no_incluye_contratos_pg(self):
        parsed = self.parser["elf_info"](elf(soname="libfixture.so", defined=("hello",)))
        self.assertEqual(parsed["defined"], ["hello"])
        self.assertEqual(parsed["soname"], "libfixture.so")
        self.assertEqual(self.parser["MAX_FILE"], 512 * 1024**2)
        self.assertNotIn("apk_inventory", self.parser)
        self.assertNotIn("abi_closure", self.parser)
        with self.assertRaises(ValueError):
            self.m.load_parser(b"changed")

    def test_oci_real_verifica_capas_y_no_extrae(self):
        raw, manifest = oci(tar_bytes([("usr/bin/python", elf())]))
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("no extraer")):
            result = self.m.read_oci(raw, manifest, self.parser)
        self.assertIn("usr/bin/python", result["files"])
        with self.assertRaises(ValueError):
            self.m.read_oci(raw + b"hidden" + bytes(506), manifest, self.parser)
        with self.assertRaises(ValueError):
            self.m.read_oci(raw.replace(b"ELF", b"BAD", 1), manifest, self.parser)

    def test_contexto_derivado_solo_cambia_from_y_rechaza_extra(self):
        dockerfile = (ROOT / "docker/python-runtime/Dockerfile.runtime-python").read_bytes()
        raw = tar_bytes([("Dockerfile", dockerfile), ("rootfs/usr/a", b"payload")])
        derived, proof = self.m.derive_context(raw)
        self.assertEqual(proof["original_sha256"], sha(raw))
        with tarfile.open(fileobj=io.BytesIO(derived), mode="r:") as archive:
            self.assertIn(b"FROM pythonbase\n", archive.extractfile("Dockerfile").read())
            self.assertEqual(archive.extractfile("rootfs/usr/a").read(), b"payload")
        with self.assertRaises(ValueError):
            self.m.derive_context(tar_bytes([("Dockerfile", dockerfile), ("../outside", b"x")]))

    def test_ids_buildkit_distinguen_manifest_config_y_preservan_conocidos(self):
        metadata = {
            "containerimage.digest": "sha256:" + "a" * 64,
            "containerimage.config.digest": "sha256:" + "b" * 64,
        }
        log = f"exporting manifest {metadata['containerimage.digest']} done\nexporting config {metadata['containerimage.config.digest']} done\n"
        self.assertEqual(
            self.m.build_identity(metadata, metadata["containerimage.config.digest"], log)[
                "manifest"
            ],
            metadata["containerimage.digest"],
        )
        with self.assertRaises(ValueError) as context:
            self.m.build_identity(metadata, metadata["containerimage.digest"], log)
        self.assertEqual(
            context.exception.identities["manifest"], metadata["containerimage.digest"]
        )

    def test_docker29_sin_config_metadata_exige_iid_manifest_y_config_log_unico(self):
        manifest, config = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        log = f"#8 exporting config {config} done\n#8 exporting manifest {manifest} done\n"
        result = self.m.build_identity({"containerimage.digest": manifest}, manifest, log)
        self.assertEqual(result["config"], config)
        self.assertFalse(result["config_metadata_present"])
        self.assertEqual(result["iid_interpretation"], "manifest-fallback")
        for iid, changed_log in (
            (config, log),
            (manifest, ""),
            (manifest, log + log),
            (
                manifest,
                log.replace(config, "sha256:" + "c" * 64) + f"exporting config {config} done\n",
            ),
        ):
            with self.assertRaises(ValueError):
                self.m.build_identity({"containerimage.digest": manifest}, iid, changed_log)

    def test_config_explicito_no_puede_usar_iid_manifest_ni_ocultar_config_discrepante(self):
        manifest, config = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        metadata = {"containerimage.digest": manifest, "containerimage.config.digest": config}
        log = f"exporting config {config} done\nexporting manifest {manifest} done\n"
        result = self.m.build_identity(metadata, config, log)
        self.assertTrue(result["config_metadata_present"])
        for changed, iid in (
            (metadata, manifest),
            ({**metadata, "containerimage.config.digest": None}, manifest),
            (metadata, config),
        ):
            with self.assertRaises(ValueError):
                self.m.build_identity(
                    changed,
                    iid,
                    log if iid != config else log.replace(config, "sha256:" + "c" * 64),
                )

    def test_cli_no_impone_rango_virtual_que_impide_arrancar_go(self):
        path = ROOT / "docker/python-runtime/build_runtime_image_remote_v3.py"
        tree = ast.parse(path.read_bytes())
        limits = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "child_limits"
        )
        resource_limits = {
            node.attr
            for node in ast.walk(limits)
            if isinstance(node, ast.Attribute) and node.attr.startswith("RLIMIT_")
        }
        self.assertNotIn("RLIMIT_AS", resource_limits)
        self.assertEqual(resource_limits, {"RLIMIT_CORE", "RLIMIT_FSIZE"})

    def test_ids_finales_se_cotejan_con_oci_real_inspect_y_descriptor(self):
        raw, manifest = oci(
            tar_bytes([("usr/a", b"bytes")]), config_extra={"config": self.m.expected_config()}
        )
        final = self.m.read_oci(raw, manifest, self.parser)
        config = final["config_digest"]
        metadata = {"containerimage.digest": manifest}
        log = f"exporting config {config} done\nexporting manifest {manifest} done\n"
        inspection = {
            "Id": manifest,
            "Descriptor": {"digest": manifest, "annotations": {"config.digest": config}},
            "Architecture": "amd64",
            "Os": "linux",
            "Config": final["config"]["config"],
        }
        result = self.m.build_identity(metadata, manifest, log, inspection=inspection, final=final)
        self.assertTrue(result["final_oci_inspect_verified"])
        for changed in (
            {**inspection, "Id": config},
            {
                **inspection,
                "Descriptor": {
                    "digest": manifest,
                    "annotations": {"config.digest": "sha256:" + "f" * 64},
                },
            },
            {**inspection, "Config": {**inspection["Config"], "Cmd": ["otro"]}},
        ):
            with self.assertRaises(ValueError):
                self.m.build_identity(metadata, manifest, log, inspection=changed, final=final)

    def test_preserva_config_unico_del_log_antes_de_cualquier_aprobacion(self):
        remote = load("build_runtime_image_remote_v3")
        manifest, config = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        files = {
            "build-metadata.json": canonical({"containerimage.digest": manifest}),
            "image-id.txt": manifest.encode(),
            "build.log": f"exporting config {config} done\n".encode(),
        }
        with patch.object(remote, "read_leaf", side_effect=lambda _fd, name, *args: files[name]):
            observed = remote.known_build_ids(123, self.m)
        self.assertEqual(observed, {"manifest": manifest, "config": config, "iidfile": manifest})

    def test_db_19_exactos_sin_fabricar_33_instalados(self):
        raw = "\n\n".join(f"P:base{i}\nV:1-r0\nA:x86_64" for i in range(19)).encode()
        parsed = self.m.apk_records(raw)
        self.assertEqual(len(parsed), 19)
        with self.assertRaises(ValueError):
            self.m.apk_records(raw + b"\n\nP:extra\nV:1\nA:x86_64")

    def test_abi_no_usa_exportaciones_de_otra_extension(self):
        base = {
            "usr/bin/python3.12": self.parser["file_entry"](elf()),
            "usr/lib/libpython3.12.so.1.0": self.parser["file_entry"](
                elf(soname="libpython3.12.so.1.0")
            ),
        }
        files = {
            **base,
            "usr/lib/python3.12/lib-dynload/a.so": self.parser["file_entry"](
                elf(undefined=("missing",))
            ),
            "usr/lib/python3.12/lib-dynload/b.so": self.parser["file_entry"](
                elf(defined=("missing",))
            ),
        }
        with self.assertRaisesRegex(ValueError, "Símbolos"):
            self.m.abi_contexts(files, ["usr/lib/python3.12/lib-dynload/a.so"], self.parser)
        files["usr/lib/libprovider.so"] = self.parser["file_entry"](
            elf(soname="libprovider.so", defined=("missing",))
        )
        files["usr/lib/python3.12/lib-dynload/a.so"] = self.parser["file_entry"](
            elf(needed=("libprovider.so",), undefined=("missing",))
        )
        self.assertTrue(
            self.m.abi_contexts(files, ["usr/lib/python3.12/lib-dynload/a.so"], self.parser)[
                "verified"
            ]
        )

    def test_gate_19_base_14_manuales_y_rechaza_colision_db_alias_y_config(self):
        db = "\n\n".join(f"P:base{i}\nV:1-r0\nA:x86_64" for i in range(19)).encode()
        basefiles = {
            "usr/lib/apk/db/installed": {
                **self.parser["file_entry"](db),
                "data": db,
                "mode": 0o644,
                "uid": 0,
                "gid": 0,
            },
            "lib": {"kind": "symlink", "target": "usr/lib", "mode": 0o777, "uid": 0, "gid": 0},
        }
        payloads = {f"usr/data/p{i}": f"payload{i}".encode() for i in range(11)}
        payloads.update(
            {
                "usr/bin/python3.12": elf(),
                "usr/lib/libpython3.12.so.1.0": elf(soname="libpython3.12.so.1.0"),
                "usr/lib/python3.12/a.py": b"pass\n",
            }
        )
        additions = {
            name: {**self.parser["file_entry"](raw), "mode": 0o644, "uid": 0, "gid": 0}
            for name, raw in payloads.items()
        }
        config = {"architecture": "amd64", "os": "linux", "config": self.m.expected_config()}
        base_layers = [
            {
                "digest": "sha256:" + f"{i:064x}",
                "size": i,
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
            }
            for i in range(11)
        ]
        base_diffids = ["sha256:" + f"{i + 30:064x}" for i in range(11)]
        config["rootfs"] = {"diff_ids": [*base_diffids, "sha256:" + "c" * 64]}
        base = {
            "files": basefiles,
            "manifest_digest": self.m.BASE_MANIFEST,
            "config_digest": self.m.BASE_CONFIG,
            "manifest": {"layers": base_layers},
            "config": {"rootfs": {"diff_ids": base_diffids}},
        }
        final = {
            "files": {**basefiles, **additions},
            "manifest_digest": "sha256:" + "a" * 64,
            "config_digest": "sha256:" + "b" * 64,
            "config": config,
            "manifest": {"layers": [*base_layers, {"digest": "sha256:" + "d" * 64}]},
        }
        expected = {
            "files": {
                name: {
                    "kind": "file",
                    "sha256": row["sha256"],
                    "bytes": row["size"],
                    "mode": 0o644,
                    "uid": 0,
                    "gid": 0,
                }
                for name, row in additions.items()
            },
            "base": {
                "packages": {
                    f"base{i}": {"P": f"base{i}", "V": "1-r0", "A": "x86_64"} for i in range(19)
                }
            },
            "manual_materials": [{"name": f"p{i}", "version": "1-r0"} for i in range(14)],
            "python": {
                "executable": "usr/bin/python3.12",
                "executable_sha256": sha(payloads["usr/bin/python3.12"]),
                "libpython": "usr/lib/libpython3.12.so.1.0",
                "libpython_sha256": sha(payloads["usr/lib/libpython3.12.so.1.0"]),
            },
            "unresolved_aliases": {},
        }
        result = self.m.verify_image(base, final, expected, self.parser)
        self.assertEqual(
            (result["installed_apk_count"], result["manual_component_count"]), (19, 14)
        )
        self.assertFalse(result["runtime_approved"])
        linked_expected = copy.deepcopy(expected)
        linked_final = copy.deepcopy(final)
        linked_expected["files"]["usr/data/duplicate"] = copy.deepcopy(
            expected["files"]["usr/data/p0"]
        )
        linked_final["files"]["usr/data/duplicate"] = copy.deepcopy(final["files"]["usr/data/p0"])
        linked_expected["files"]["usr/data/hard"] = {
            "kind": "hardlink",
            "target": "usr/data/p0",
            "context_hardlink_target": "rootfs/usr/data/p0",
            "mode": 0o644,
            "uid": 0,
            "gid": 0,
        }
        linked_final["files"]["usr/data/hard"] = {
            "kind": "hardlink",
            "target": "/usr/data/duplicate",
            "mode": 0o644,
            "uid": 0,
            "gid": 0,
        }
        with self.assertRaisesRegex(ValueError, "Hardlink"):
            self.m.verify_image(base, linked_final, linked_expected, self.parser)
        changed = copy.deepcopy(final)
        changed["files"]["usr/lib/apk/db/installed"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.m.verify_image(base, changed, expected, self.parser)
        changed = copy.deepcopy(final)
        changed["config"]["config"]["Env"].append("LD_PRELOAD=/evil")
        with self.assertRaises(ValueError):
            self.m.verify_image(base, changed, expected, self.parser)
        changed = copy.deepcopy(final)
        changed["files"]["extra"] = next(iter(additions.values()))
        with self.assertRaises(ValueError):
            self.m.verify_image(base, changed, expected, self.parser)
        collided = copy.deepcopy(expected)
        collided["files"]["usr/lib/apk/db/installed"] = {
            "kind": "file",
            "sha256": sha(db),
            "bytes": len(db),
            "mode": 0o644,
            "uid": 0,
            "gid": 0,
        }
        with self.assertRaisesRegex(ValueError, "colisiona"):
            self.m.verify_image(base, final, collided, self.parser)
        pending = copy.deepcopy(expected)
        pending["files"]["usr/lib/terminfo"] = {
            "kind": "symlink",
            "target": "../share/terminfo",
            "mode": 0o777,
            "uid": 0,
            "gid": 0,
        }
        pending["unresolved_aliases"] = {"usr/lib/terminfo": "../share/terminfo"}
        with_alias = copy.deepcopy(final)
        with_alias["files"]["usr/lib/terminfo"] = pending["files"]["usr/lib/terminfo"]
        result = self.m.verify_image(base, with_alias, pending, self.parser)
        self.assertTrue(result["root_release_blockers"])
        self.assertIsNone(result["alias_observations"]["usr/lib/terminfo"]["resolved"])
        self.assertFalse(result["runtime_approved"])

    def test_plan_sin_prepare_no_lee_boveda_ni_crea_raices(self):
        module = load("build_runtime_image_v3")
        with (
            patch.object(module, "prepared_inputs", return_value=None),
            patch.object(module, "SUPPORTS", {}),
            patch.object(module.os, "mkdir", side_effect=AssertionError("no crear")),
        ):
            plan = module.proposal()
        self.assertFalse(plan["readiness"]["ready"])
        self.assertFalse(plan["readiness"]["inputs_ready"])

    def test_argumentos_build_no_admiten_run_pull_ni_base_registry(self):
        remote = load("build_runtime_image_remote_v3")
        arguments = remote.build_arguments(self.m.BASE_MANIFEST)
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn("--pull=false", arguments)
        self.assertEqual(
            arguments[arguments.index("--build-context") + 1],
            "pythonbase=oci-layout://" + remote.ROOT + "/base-oci@" + self.m.BASE_MANIFEST,
        )
        self.assertEqual(arguments[-1], "-")
        with self.assertRaises(ValueError):
            remote.build_arguments("sha256:" + "f" * 64)

    def test_captura_ids_parcial_no_pierde_manifest_si_iid_falta(self):
        remote = load("build_runtime_image_remote_v3")
        metadata = canonical({"containerimage.digest": "sha256:" + "a" * 64})
        with patch.object(
            remote,
            "read_leaf",
            side_effect=lambda _fd, name, *args: (
                metadata
                if name == "build-metadata.json"
                else (_ for _ in ()).throw(FileNotFoundError())
            ),
        ):
            result = remote.known_build_ids(123, self.m)
        self.assertEqual(result, {"manifest": "sha256:" + "a" * 64})

    def test_pt_interp_ausente_o_header_truncado_no_se_inventa(self):
        self.assertIsNone(self.m.program_interpreter(elf()))
        raw = bytearray(elf())
        struct.pack_into("<Q", raw, 32, len(raw) + 1)
        struct.pack_into("<HH", raw, 54, 56, 1)
        with self.assertRaises(ValueError):
            self.m.program_interpreter(bytes(raw))

    def test_gate_local_rechaza_recibo_de_otro_perfil_antes_de_leer_archives(self):
        controller = load("build_runtime_image_v3")
        checksum = "a" * 64
        info = types.SimpleNamespace(
            st_dev=1, st_ino=2, st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0
        )
        marker = canonical(
            {
                "owner": controller.OWNER,
                "path": str(controller.LOCAL_ROOT),
                "directory": {"device": 1, "inode": 2},
                "profile_sha256": checksum,
            }
        )
        receipt = {
            "success": True,
            "owner": controller.OWNER,
            "stage": "fetch",
            "profile_sha256": checksum,
            "local": {"directory": {"device": 1, "inode": 2}, "marker_sha256": sha(marker)},
        }
        controller.validate_local_proof(receipt, marker, info, checksum)
        with self.assertRaises(ValueError):
            controller.validate_local_proof(
                {**receipt, "profile_sha256": "b" * 64}, marker, info, checksum
            )

    def test_publicacion_local_exclusiva_conserva_archivo_ajeno(self):
        controller = load("build_runtime_image_v3")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            target.write_bytes(b"ajeno")
            with self.assertRaises(FileExistsError):
                controller.write_exclusive(target, b"nuevo")
            self.assertEqual(target.read_bytes(), b"ajeno")

    def test_operacion_no_lista_no_lee_boveda(self):
        controller = load("build_runtime_image_v3")
        with (
            patch.object(
                controller,
                "proposal",
                return_value={"profile_sha256": "a" * 64, "readiness": {"inputs_ready": False}},
            ),
            patch.object(controller, "read_values", side_effect=AssertionError("no bóveda")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(
                controller.main(
                    [
                        "prepare",
                        "--execute",
                        "--root-update-vault",
                        "--expected-profile-sha256",
                        "a" * 64,
                    ]
                ),
                1,
            )


@unittest.skipUnless(
    os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
    "Requiere Linux UID0 para FD/propiedad reales; Windows no lo acredita.",
)
class NativeTests(unittest.TestCase):
    def test_fd_nofollow_excl_y_metadata_644_solo_nombre_exacto(self):
        remote = load("build_runtime_image_remote_v3")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                remote.write_new(descriptor, "owned", b"bytes")
                with self.assertRaises(FileExistsError):
                    remote.write_new(descriptor, "owned", b"otro")
                os.symlink("owned", root / "link")
                with self.assertRaises(ValueError):
                    remote.read_leaf(descriptor, "link")
                remote.write_new(descriptor, "build-metadata.json", b"{}")
                os.chmod(root / "build-metadata.json", 0o644)
                self.assertEqual(remote.read_leaf(descriptor, "build-metadata.json"), b"{}")
                os.chmod(root / "owned", 0o644)
                with self.assertRaises(ValueError):
                    remote.read_leaf(descriptor, "owned")
            finally:
                os.close(descriptor)

    def test_oci_fd_rechaza_archivo_extra_antes_de_build(self):
        remote = load("build_runtime_image_remote_v3")
        core = load()
        raw, _manifest = oci(tar_bytes([("usr/a", b"dato")]))
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                remote.oci_layout(descriptor, raw, core, create=True)
                remote.oci_layout(descriptor, raw, core)
                (Path(directory) / "base-oci/ajeno").write_bytes(b"conservar")
                with self.assertRaises(ValueError):
                    remote.oci_layout(descriptor, raw, core)
                self.assertEqual((Path(directory) / "base-oci/ajeno").read_bytes(), b"conservar")
            finally:
                os.close(descriptor)


class BaseCharacterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()
        cls.parser = cls.m.load_parser(
            (ROOT / "docker/postgres-immich/final_image_remote.py").read_bytes()
        )
        cls.production_authority = {
            name: getattr(cls.m, name)
            for name in ("BASE_MANIFEST", "BASE_CONFIG", "BASE_DEVICE_DIGEST", "BASE_DEVICE_DIFFID")
        }
        layers = [tar_bytes([(f"usr/fixture-{i}", f"{i}".encode())]) for i in range(9)]
        db = "\n\n".join(f"P:base{i}\nV:1-r0\nA:x86_64" for i in range(19)).encode()
        merged = io.BytesIO()
        with tarfile.open(fileobj=merged, mode="w:") as archive:
            alias = tarfile.TarInfo("lib")
            alias.type, alias.linkname = tarfile.SYMTYPE, "usr/lib"
            archive.addfile(alias)
            node = tarfile.TarInfo("usr/lib/apk/db/installed")
            node.mode, node.size = 0o644, len(db)
            archive.addfile(node, io.BytesIO(db))
        layers.extend([merged.getvalue(), cls.character_tar()])
        blobs = [gzip.compress(layer, mtime=0) for layer in layers]
        config = canonical(
            {
                "architecture": "amd64",
                "os": "linux",
                "rootfs": {
                    "type": "layers",
                    "diff_ids": ["sha256:" + sha(layer) for layer in layers],
                },
            }
        )
        manifest = canonical(
            {
                "schemaVersion": 2,
                "config": {"digest": "sha256:" + sha(config), "size": len(config)},
                "layers": [
                    {
                        "digest": "sha256:" + sha(blob),
                        "size": len(blob),
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    }
                    for blob in blobs
                ],
            }
        )
        # Autoridad de fixture sólo en este módulo de prueba aislado, nunca por CLI/env productivo.
        cls.m.BASE_MANIFEST, cls.m.BASE_CONFIG = "sha256:" + sha(manifest), "sha256:" + sha(config)
        cls.m.BASE_DEVICE_DIGEST = "sha256:" + sha(blobs[10])
        cls.m.BASE_DEVICE_DIFFID = "sha256:" + sha(layers[10])
        cls.base_raw = tar_bytes(
            [
                ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                (
                    "index.json",
                    canonical(
                        {
                            "schemaVersion": 2,
                            "manifests": [{"digest": cls.m.BASE_MANIFEST, "size": len(manifest)}],
                        }
                    ),
                ),
                *(("blobs/sha256/" + sha(raw), raw) for raw in [config, manifest, *blobs]),
            ]
        )

    @staticmethod
    def character_tar(change=None, *, missing=None, extra=None):
        expected = {
            "dev/console": (0o620, 5, 1),
            "dev/null": (0o666, 1, 3),
            "dev/random": (0o666, 1, 8),
            "dev/urandom": (0o666, 1, 9),
            "dev/zero": (0o666, 1, 5),
        }
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
            for name, (mode, major, minor) in expected.items():
                if name == missing:
                    continue
                node = tarfile.TarInfo(name)
                node.type, node.mode, node.devmajor, node.devminor = (
                    tarfile.CHRTYPE,
                    mode,
                    major,
                    minor,
                )
                if change:
                    change(node)
                archive.addfile(node)
            if extra:
                archive.addfile(extra)
        return stream.getvalue()

    def test_production_authority_remains_fixed_outside_isolated_fixture(self):
        self.assertEqual(
            self.production_authority,
            {
                "BASE_MANIFEST": "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72",
                "BASE_CONFIG": "sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7",
                "BASE_DEVICE_DIGEST": "sha256:99fb494f3ae0460d24b21586bc40d9e7e5cee3737ac40308485f43dde7cd8027",
                "BASE_DEVICE_DIFFID": "sha256:3244c5804380021c4808c09895132be39a4285bf02029e8e30d830a2ec02d0b7",
            },
        )
        original = load()
        self.assertEqual(
            {name: getattr(original, name) for name in self.production_authority},
            self.production_authority,
        )

    def test_synthetic_oci_accepts_only_five_virtual_special_nodes(self):
        with patch.object(
            tarfile.TarFile, "extractall", side_effect=AssertionError("NO_EXTRACTION")
        ):
            result = self.m.read_oci(self.base_raw, self.m.BASE_MANIFEST, self.parser)
        nodes = {name: row for name, row in result["files"].items() if row["kind"] == "special"}
        self.assertEqual(
            set(nodes), {"dev/console", "dev/null", "dev/random", "dev/urandom", "dev/zero"}
        )
        self.assertEqual(
            nodes["dev/console"],
            {
                "kind": "special",
                "type": "3",
                "major": 5,
                "minor": 1,
                "mode": 0o620,
                "uid": 0,
                "gid": 0,
            },
        )

    def test_database_bytes_require_unique_physical_path_and_exact_alias(self):
        image = self.m.read_oci(self.base_raw, self.m.BASE_MANIFEST, self.parser)
        raw = self.m.database_bytes(image)
        self.assertEqual(len(self.m.apk_records(raw)), 19)
        self.assertNotIn("lib/apk/db/installed", image["files"])
        for target in ("../usr/lib", "/usr/lib", "usr/other"):
            changed = copy.deepcopy(image)
            changed["files"]["lib"]["target"] = target
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "APK_DATABASE"):
                self.m.database_bytes(changed)
        changed = copy.deepcopy(image)
        changed["files"]["lib/apk/db/installed"] = changed["files"]["usr/lib/apk/db/installed"]
        with self.assertRaisesRegex(ValueError, "APK_DATABASE"):
            self.m.database_bytes(changed)

    def test_final_cannot_replace_database_or_alias_from_authenticated_base(self):
        replacements = [
            tar_bytes([("usr/lib/apk/db/installed", b"P:changed\nV:1-r0\nA:x86_64\n")]),
        ]
        for target in ("/usr/lib", "../usr/lib", "usr/other"):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:") as archive:
                node = tarfile.TarInfo("lib")
                node.type, node.linkname = tarfile.SYMTYPE, target
                archive.addfile(node)
            replacements.append(stream.getvalue())
        for extra in replacements:
            raw, final_id = self.final_archive(extra)
            with (
                self.subTest(layer_sha256=sha(extra)),
                self.assertRaisesRegex(ValueError, "APK_DATABASE"),
            ):
                self.m.read_oci(raw, final_id, self.parser, base_raw=self.base_raw)

    def test_real_tar_fixture_validates_closed_device_set_as_data(self):
        rows = self.m._tar_rows(self.character_tar(), base_devices=True)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(member.ischr() and body is None for _, member, body in rows))

    def test_device_name_exact_rejects_different_name(self):
        def change(node):
            if node.name == "dev/null":
                node.name = "dev/other"

        with self.assertRaisesRegex(ValueError, "BASE_DEVICE"):
            self.m._tar_rows(self.character_tar(change), base_devices=True)

    def test_device_major_and_minor_are_exact(self):
        for attr in ("devmajor", "devminor"):

            def change(node, attr=attr):
                if node.name == "dev/null":
                    setattr(node, attr, 22)

            with self.subTest(attr=attr), self.assertRaisesRegex(ValueError, "BASE_DEVICE"):
                self.m._tar_rows(self.character_tar(change), base_devices=True)

    def test_device_mode_is_exact_including_privilege_bits(self):
        for value in (0o600, 0o4620):

            def change(node, value=value):
                if node.name == "dev/console":
                    node.mode = value

            with self.subTest(mode=value), self.assertRaisesRegex(ValueError, "BASE_DEVICE"):
                self.m._tar_rows(self.character_tar(change), base_devices=True)

    def test_device_uid_and_gid_are_exact(self):
        for attr in ("uid", "gid"):

            def change(node, attr=attr):
                if node.name == "dev/null":
                    setattr(node, attr, 1)

            with self.subTest(attr=attr), self.assertRaisesRegex(ValueError, "BASE_DEVICE"):
                self.m._tar_rows(self.character_tar(change), base_devices=True)

    def test_devices_remain_rejected_in_outer_context_and_copy_default(self):
        raw = self.character_tar()
        for outer in (False, True):
            with self.subTest(outer=outer), self.assertRaises(ValueError):
                self.m.tar_rows(raw, outer=outer)
        dockerfile = (ROOT / "docker/python-runtime/Dockerfile.runtime-python").read_bytes()
        with self.assertRaises(ValueError):
            self.m.derive_context(raw + tar_bytes([("Dockerfile", dockerfile)]))

    def test_extra_missing_duplicate_block_and_fifo_are_rejected(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            extra = tarfile.TarInfo("dev/extra")
            extra.type = kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.m._tar_rows(self.character_tar(extra=extra), base_devices=True)
        with self.assertRaisesRegex(ValueError, "BASE_DEVICE_SET"):
            self.m._tar_rows(self.character_tar(missing="dev/null"), base_devices=True)
        duplicate = tarfile.TarInfo("dev/null")
        duplicate.type = tarfile.CHRTYPE
        with self.assertRaises(ValueError):
            self.m._tar_rows(self.character_tar(extra=duplicate), base_devices=True)

    def final_archive(self, extra_layer, *, change=None):
        with tarfile.open(fileobj=io.BytesIO(self.base_raw), mode="r:") as archive:
            blobs = {
                m.name: archive.extractfile(m).read()
                for m in archive
                if m.isfile() and m.name.startswith("blobs/sha256/")
            }
        manifest = json.loads(blobs["blobs/sha256/" + self.m.BASE_MANIFEST[7:]])
        config = json.loads(blobs["blobs/sha256/" + self.m.BASE_CONFIG[7:]])
        kept = {
            "blobs/sha256/" + r["digest"][7:]: blobs["blobs/sha256/" + r["digest"][7:]]
            for r in manifest["layers"]
        }
        config["rootfs"]["diff_ids"].append("sha256:" + sha(extra_layer))
        manifest["layers"].append(
            {
                "digest": "sha256:" + sha(extra_layer),
                "size": len(extra_layer),
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
            }
        )
        if change:
            change(manifest, config)
        encoded_config = canonical(config)
        manifest["config"] = {
            **manifest["config"],
            "digest": "sha256:" + sha(encoded_config),
            "size": len(encoded_config),
        }
        encoded_manifest = canonical(manifest)
        final_id = "sha256:" + sha(encoded_manifest)
        kept.update(
            {
                "blobs/sha256/" + sha(extra_layer): extra_layer,
                "blobs/sha256/" + sha(encoded_config): encoded_config,
                "blobs/sha256/" + sha(encoded_manifest): encoded_manifest,
            }
        )
        raw = tar_bytes(
            [
                ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                (
                    "index.json",
                    canonical(
                        {
                            "schemaVersion": 2,
                            "manifests": [{"digest": final_id, "size": len(encoded_manifest)}],
                        }
                    ),
                ),
                *sorted(kept.items()),
            ]
        )
        return raw, final_id

    def test_layer_position_descriptor_and_diffid_are_all_required(self):
        with tarfile.open(fileobj=io.BytesIO(self.base_raw), mode="r:") as archive:
            blob = archive.extractfile("blobs/sha256/" + self.m.BASE_DEVICE_DIGEST[7:]).read()
        plain = gzip.decompress(blob)
        descriptor = {"digest": self.m.BASE_DEVICE_DIGEST}
        self.assertEqual(
            len(self.m.base_device_rows(plain, 10, descriptor, self.m.BASE_DEVICE_DIFFID)), 5
        )
        for index, row, diffid in (
            (9, descriptor, self.m.BASE_DEVICE_DIFFID),
            (10, {"digest": "sha256:" + "a" * 64}, self.m.BASE_DEVICE_DIFFID),
            (10, descriptor, "sha256:" + "a" * 64),
        ):
            with (
                self.subTest(index=index, row=row),
                self.assertRaisesRegex(ValueError, "BASE_DEVICE_LAYER"),
            ):
                self.m.base_device_rows(plain, index, row, diffid)

    def test_final_requires_complete_base_bytes_before_device_exception(self):
        raw, final_id = self.final_archive(tar_bytes([("usr/acceptance-data", b"fixture")]))
        with self.assertRaisesRegex(ValueError, "Tipo TAR"):
            self.m.read_oci(raw, final_id, self.parser)
        result = self.m.read_oci(raw, final_id, self.parser, base_raw=self.base_raw)
        self.assertIn("usr/acceptance-data", result["files"])
        self.assertEqual(result["files"]["dev/null"]["major"], 1)

    def test_final_prefix_change_rejected_before_final_device_layer(self):
        def change(manifest, _config):
            manifest["layers"][0]["size"] += 1

        raw, final_id = self.final_archive(tar_bytes([("usr/data", b"fixture")]), change=change)
        original = self.m.base_device_rows
        calls = []

        def observed(*args):
            calls.append(args[1])
            return original(*args)

        with patch.object(self.m, "base_device_rows", side_effect=observed):
            with self.assertRaisesRegex(ValueError, "BASE_PREFIX_CHANGED"):
                self.m.read_oci(raw, final_id, self.parser, base_raw=self.base_raw)
        self.assertEqual(calls, [10])  # Sólo se validó la base auténtica; no la capa del final.

    def test_copy_layer_cannot_add_any_special_even_matching_base_names(self):
        raw, final_id = self.final_archive(self.character_tar())
        with self.assertRaisesRegex(ValueError, "Tipo TAR"):
            self.m.read_oci(raw, final_id, self.parser, base_raw=self.base_raw)

    def test_special_catalog_identity_keeps_type_major_minor(self):
        node = {
            "kind": "special",
            "type": "3",
            "major": 1,
            "minor": 3,
            "mode": 0o666,
            "uid": 0,
            "gid": 0,
        }
        self.assertNotEqual(
            self.m.catalog_identity(node), self.m.catalog_identity({**node, "minor": 8})
        )

    def test_explicit_nogit_variables_reach_the_cli_environment(self):
        remote = load("build_runtime_image_remote_v3")
        tree = ast.parse(
            (ROOT / "docker/python-runtime/build_runtime_image_remote_v3.py").read_bytes()
        )
        function = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute"
        )
        assignment = next(
            n
            for n in ast.walk(function)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "env" for t in n.targets)
        )
        # Evalúa sólo el literal de entorno con ROOT fijo, sin cargar operación/credenciales.
        value = eval(
            compile(ast.Expression(assignment.value), "<env-fijo>", "eval"), {"ROOT": remote.ROOT}
        )
        self.assertEqual(
            {
                k: value.get(k)
                for k in ("BUILDX_GIT_INFO", "BUILDX_GIT_LABELS", "BUILDX_GIT_CHECK_DIRTY")
            },
            {
                "BUILDX_GIT_INFO": "false",
                "BUILDX_GIT_LABELS": "false",
                "BUILDX_GIT_CHECK_DIRTY": "false",
            },
        )

    def test_structured_diagnostic_keeps_code_phase_and_never_error_text(self):
        remote = load("build_runtime_image_remote_v3")
        error = ValueError("sensitive exception detail")
        error.code = "BASE_DEVICE_LAYER"
        self.assertEqual(
            remote.failure_details(error, "read_base"),
            {"error_type": "ValueError", "error_code": "BASE_DEVICE_LAYER", "phase": "read_base"},
        )
        error.code = "sensitive exception detail"
        sanitized = remote.failure_details(error, "sensitive phase")
        self.assertEqual(sanitized["error_code"], "VALIDATION_FAILED")
        self.assertEqual(sanitized["phase"], "request")
        self.assertNotIn("sensitive", json.dumps(sanitized))


if __name__ == "__main__":
    unittest.main()
