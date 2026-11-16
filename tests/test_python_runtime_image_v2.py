"""Contratos de la factory: datos TAR/OCI/ELF, sin Docker ni intérprete del runtime."""

import ast
import copy
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


def load(name="runtime_image_inventory_v2"):
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
        path = ROOT / "docker/python-runtime/build_runtime_image_remote_v2.py"
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
        remote = load("build_runtime_image_remote_v2")
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
            "lib/apk/db/installed": {
                **self.parser["file_entry"](db),
                "data": db,
                "mode": 0o644,
                "uid": 0,
                "gid": 0,
            }
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
        base = {
            "files": basefiles,
            "manifest_digest": self.m.BASE_MANIFEST,
            "config_digest": self.m.BASE_CONFIG,
        }
        final = {
            "files": {**basefiles, **additions},
            "manifest_digest": "sha256:" + "a" * 64,
            "config_digest": "sha256:" + "b" * 64,
            "config": config,
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
        changed["files"]["lib/apk/db/installed"]["sha256"] = "0" * 64
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
        collided["files"]["lib/apk/db/installed"] = {
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
        module = load("build_runtime_image_v2")
        with (
            patch.object(module, "prepared_inputs", return_value=None),
            patch.object(module, "SUPPORTS", {}),
            patch.object(module.os, "mkdir", side_effect=AssertionError("no crear")),
        ):
            plan = module.proposal()
        self.assertFalse(plan["readiness"]["ready"])
        self.assertFalse(plan["readiness"]["inputs_ready"])

    def test_argumentos_build_no_admiten_run_pull_ni_base_registry(self):
        remote = load("build_runtime_image_remote_v2")
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
        remote = load("build_runtime_image_remote_v2")
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
        controller = load("build_runtime_image_v2")
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
        controller = load("build_runtime_image_v2")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            target.write_bytes(b"ajeno")
            with self.assertRaises(FileExistsError):
                controller.write_exclusive(target, b"nuevo")
            self.assertEqual(target.read_bytes(), b"ajeno")

    def test_operacion_no_lista_no_lee_boveda(self):
        controller = load("build_runtime_image_v2")
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
        remote = load("build_runtime_image_remote_v2")
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
        remote = load("build_runtime_image_remote_v2")
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


if __name__ == "__main__":
    unittest.main()
