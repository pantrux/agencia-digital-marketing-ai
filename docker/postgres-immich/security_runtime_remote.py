"""Imagen propia desde catálogo V7; importar no ejecuta infraestructura."""

import ast
import base64
import hashlib
import json
import os
import posixpath
import re
import signal
import stat
import struct
import types
from datetime import UTC, datetime

PURE_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
BASE_MANIFEST = "c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a"
BASE_CONFIG = "22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c"
CID = "f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d"
OWNER = "agencia-digital-marketing-ai:postgres-immich-security-runtime:v1"
CONTEXT = (
    "/opt/media-platform/build/contexts/postgres-immich-security-runtime/security-v7-20261106-01"
)
RECORD = "/var/lib/media-platform/build/postgres-immich-security-runtime/security-v7-20261106-01"
OVERLAY = "/outputs/security-rebuild-v7/runtime-root"
CHANGED = "usr/local/lib/postgresql/vchord.so"
SDK_SHA = "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed"
SECURITY_SHA = "7206217b7933b65038a361c1c0dbda4df16d442a28d25daca21fff931e6b1d12"
SECURITY_PROFILE = "af3aee69455287b71f6d7387c1d95456f6d046455bdb39d199d41677f05c324c"
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
MARKER = ".security-runtime-owner.json"
MAX_FILE = 512 * 1024**2
MAX_ARCHIVE = 768 * 1024**2
MAX_CAPTURE = 4 * 1024**2
MAX_REQUEST = 2 * 1024**2
OLD_RECORD = "/var/lib/media-platform/build/postgres-immich-runtime/fase4-20261106-01"
LABELS = {
    "org.opencontainers.image.title": "PostgreSQL Immich de la agencia",
    "org.opencontainers.image.version": "16.15-vchord1.1.1-vector0.8.5-sqlite3.54.1-agencia.security-v7",
    "cl.e-scale.agencia.owner": OWNER,
}
FROZEN_REFS = {
    "sdk_profile": (
        ".tools/postgres-immich-build/compile-v3-profile.json",
        "bffefb81179fd767d640faf106965ba7d465e90c3a789b4e7800b988a0152153",
    ),
    "baseline_recovered": (
        ".tools/evidence/phase4-postgres-immich-postverify-v3-recover-real-result.json",
        "df3913b08f92f41c85243f5621db65b12562a31160da1562f0cdd48add794108",
    ),
    "baseline_prepared": (
        ".tools/evidence/phase4-postgres-immich-runtime-prepared-real.json",
        "37a30b082638f2943c2a1f264c1940d5c017f9c177503c26b801c104c90fe129",
    ),
    "baseline_finished": (
        ".tools/evidence/phase4-postgres-immich-runtime-finish-real-result.json",
        "1d68960e8c477fb25b19ba3169237a4c6d05ab4df5db86c88efcfee2206f568f",
    ),
}
DYNAMIC_REFS = {"v7_profile", "v7_prepare", "v7_restart", "v7_catalog", "v7_stop"}


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, code):
    if not condition:
        raise ValueError(code)


def json_object(raw):
    def unique(rows):
        value = {}
        for key, item in rows:
            require(key not in value, "JSON_DUPLICATE")
            value[key] = item
        return value

    def invalid(_value):
        raise ValueError("JSON_CONSTANT")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def load_pure(raw):
    require(digest(raw) == PURE_SHA, "PURE_SOURCE_PIN")
    names = {
        "canonical",
        "digest",
        "part",
        "elf_info",
        "file_entry",
        "virtual_path",
        "follow",
        "abi_closure",
        "json_object",
        "safe_name",
        "apply_layer",
        "layer_bytes",
        "apk_inventory",
        "read_oci",
        "project",
        "validate_base",
        "parents",
    }
    constants = {"BASE_MANIFEST", "BASE_CONFIG", "LICENSES", "LIBRARIES", "MAX_ARCHIVE"}
    nodes = []
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Import) and all(
            alias.name in {"hashlib", "json", "posixpath", "struct"} for alias in node.names
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in constants
        ):
            nodes.append(node)
    require({n.name for n in nodes if isinstance(n, ast.FunctionDef)} == names, "PURE_EXPORTS")
    module = types.ModuleType("security_runtime_pure_parsers")
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), "<pure-9e0-fijado>", "exec"),
        module.__dict__,
    )
    return module


def projection_delta(baseline, candidate, pure):
    projected, excluded = pure.project(candidate)
    require(
        len(baseline) == len(projected) == 66 and set(baseline) == set(projected),
        "PROJECTION_NAMES",
    )
    require(CHANGED in baseline and projected[CHANGED] != baseline[CHANGED], "VCHORD_NOT_CHANGED")
    require(
        all(v == projected[n] for n, v in baseline.items() if n != CHANGED),
        "FOREIGN_OVERLAY_CHANGE",
    )
    for row in projected.values():
        require(
            set(row) == {"sha256", "size"}
            and re.fullmatch(r"[a-f0-9]{64}", str(row["sha256"]))
            and type(row["size"]) is int
            and 0 < row["size"] <= MAX_FILE,
            "PROJECTION_ENTRY",
        )
    return projected, excluded


def build_identity(metadata, iidfile, inspection, log, final):
    image, config = final["manifest_digest"], final["config_digest"]
    require(
        all(
            isinstance(v, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", v) for v in (image, config)
        ),
        "IMAGE_DIGESTS",
    )
    require(
        metadata.get("containerimage.digest") == image and inspection.get("id") == image,
        "MANIFEST_IDENTITY",
    )
    descriptor = inspection.get("descriptor") or {}
    require(
        descriptor.get("digest", image) == image
        and (descriptor.get("annotations") or {}).get("config.digest", config) == config,
        "DESCRIPTOR_IDENTITY",
    )
    require(
        inspection.get("user") == "70:70"
        and inspection.get("entrypoint") == ["docker-entrypoint.sh"]
        and inspection.get("cmd") == ["postgres"]
        and inspection.get("labels", {}).get("cl.e-scale.agencia.owner") == OWNER
        and inspection.get("os") == "linux"
        and inspection.get("architecture") == "amd64",
        "IMAGE_OWNERSHIP",
    )
    explicit = "containerimage.config.digest" in metadata
    require(
        (metadata["containerimage.config.digest"] == config and iidfile == config)
        if explicit
        else iidfile == image,
        "IID_INTERPRETATION",
    )
    require(
        re.findall(r"exporting manifest (sha256:[a-f0-9]{64}) done", log) == [image]
        and re.findall(r"exporting config (sha256:[a-f0-9]{64}) done", log) == [config],
        "LOG_IMAGE_IDS",
    )
    return {
        "image_id": image,
        "config_digest": config,
        "iidfile": iidfile,
        "iid_interpretation": "config" if explicit else "manifest-fallback",
        "config_metadata_present": explicit,
    }


def proposal():
    return {
        "schema_version": 1,
        "owner": OWNER,
        "context": CONTEXT,
        "record": RECORD,
        "base_manifest": BASE_MANIFEST,
        "base_config": BASE_CONFIG,
        "cid": CID,
        "overlay": OVERLAY,
        "platform": "linux/amd64",
        "builder": "default",
        "budget_seconds": 600,
        "capture_limit": MAX_CAPTURE,
        "network": "none",
        "ready": False,
        "pending": ["catalog-v7-positive", "stop-v7-backed"],
        "release_approved": False,
        "sql_verified": False,
        "final_sca_verified": False,
    }


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def checked_envelope(value, stage, *, owner=OWNER, profile_sha=None, legacy=False):
    row = value.get("receipt", {})
    encoding = (
        json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if legacy
        else canonical(row)
    )
    require(
        set(value) == {"receipt", "receipt_sha256"}
        and value["receipt_sha256"] == digest(encoding)
        and row.get("stage") == stage
        and row.get("owner") == owner
        and row.get("success") is True,
        "POSITIVE_STAGE_ENVELOPE",
    )
    if profile_sha is not None:
        require(row.get("profile_sha256") == profile_sha, "STAGE_PROFILE")
    return row


def validate_profile(profile):
    expected = proposal()
    for key, value in expected.items():
        if key not in {"ready", "pending"}:
            require(
                profile.get(key) == value and type(profile.get(key)) is type(value),
                "PROFILE_CONTRACT",
            )
    require(profile.get("ready") is True and profile.get("pending") == [], "PROFILE_NOT_READY")
    require(set(profile) == set(expected) | {"references", "sources"}, "PROFILE_FIELDS")
    require(
        set(profile["sources"]) == {"local", "remote", "dockerfile"}
        and all(re.fullmatch(r"[a-f0-9]{64}", str(v)) for v in profile["sources"].values()),
        "SOURCE_PINS",
    )
    refs = profile["references"]
    require(set(refs) == set(FROZEN_REFS) | DYNAMIC_REFS, "REFERENCE_SET")
    for name, row in refs.items():
        require(
            set(row) == {"path", "sha256", "size"}
            and re.fullmatch(r"[a-f0-9]{64}", str(row["sha256"]))
            and type(row["size"]) is int
            and 0 < row["size"] <= 1024**2,
            "REFERENCE_PIN",
        )
        if name in FROZEN_REFS:
            require((row["path"], row["sha256"]) == FROZEN_REFS[name], "HISTORY_REFERENCE")
        else:
            require(isinstance(row["path"], str) and len(row["path"]) < 512, "REFERENCE_PATH")
    require(refs["v7_profile"]["sha256"] == SECURITY_PROFILE, "V7_PROFILE_PIN")
    require(len(canonical(profile)) <= 32768, "SMALL_PROFILE")
    return digest(canonical(profile))


def validate_gates(data, pure):
    profile = data["v7_profile"]
    require(
        digest(canonical(profile)) == SECURITY_PROFILE
        and profile["sources"]["remote"] == SECURITY_SHA
        and profile["cid"] == CID
        and profile["paths"]["output"] == "/outputs/security-rebuild-v7",
        "V7_PROFILE_IDENTITY",
    )
    stages = {
        mode: checked_envelope(
            data["v7_" + mode],
            mode,
            owner="agencia-digital-marketing-ai:postgres-immich-security-rebuild:v7",
            profile_sha=SECURITY_PROFILE,
        )
        for mode in ("prepare", "restart", "catalog", "stop")
    }
    catalog, stopped, epoch = stages["catalog"], stages["stop"], stages["restart"]["container"]
    daemon = stages["prepare"].get("daemon_id")
    require(
        isinstance(daemon, str) and re.fullmatch(r"[A-Za-z0-9:+.-]{1,128}", daemon),
        "SDK_DAEMON_IDENTITY",
    )
    birth = profile["original_container"]
    require(
        all(epoch.get(key) == birth.get(key) for key in ("cid", "created", "image_id"))
        and all(
            stages["prepare"]["container"].get(key) == birth.get(key)
            for key in ("cid", "created", "image_id")
        ),
        "SDK_BIRTH_IDENTITY",
    )
    require(
        stopped["cleanup"].get("stopped") is True
        and stopped["container"].get("running") is False
        and stopped["container"].get("pid") == 0
        and stopped["container"].get("no_foreign") is True,
        "SDK_NOT_STOPPED",
    )
    for row in (catalog["container"], stopped["container"]):
        require(
            all(
                row.get(k) == epoch.get(k)
                for k in ("cid", "created", "image_id", "started_at", "limits", "no_foreign")
            )
            and row.get("cid") == CID
            and row.get("no_foreign") is True,
            "SDK_EPOCH",
        )
    require(
        type(epoch["pid"]) is int
        and epoch["pid"] > 0
        and catalog["container"]["pid"] == epoch["pid"]
        and catalog["container"]["running"] is True,
        "CATALOG_EPOCH",
    )
    artifact = catalog["artifact"]
    require(
        artifact.get("success") is True
        and artifact.get("stage") == "catalog"
        and artifact.get("profile_sha256") == SECURITY_PROFILE
        and artifact.get("sdk_executed") is False
        and artifact.get("sql_executed") is False
        and artifact.get("security_clean") is False,
        "CATALOG_ARTIFACT",
    )
    baseline = checked_envelope(
        data["baseline_prepared"],
        "prepare",
        owner="agencia-digital-marketing-ai:postgres-immich-runtime:v4.1",
        legacy=True,
    )
    finished = checked_envelope(
        data["baseline_finished"],
        "finish-image-v1",
        owner="agencia-digital-marketing-ai:postgres-immich-runtime:v4.1",
        legacy=True,
    )
    require(
        finished.get("prepared_receipt_sha256") == data["baseline_prepared"]["receipt_sha256"]
        and finished.get("image_id")
        == "sha256:96c6cce5d1efe176fe5b22e17c3c4c7bca926c94c403096bce9aa663df04a953",
        "BASELINE_CHAIN",
    )
    recovered = data["baseline_recovered"]["receipt"]
    require(
        data["baseline_recovered"]["receipt_sha256"]
        == "fc3341c72a1d2c9dfba1f1b25a23116ff457f75fc985a4f6fe5a68c1d3ee8d51"
        and recovered.get("success") is True,
        "RECOVERED_HISTORY",
    )
    old_files = recovered["artifact"]["runtime_files"]
    require(
        set(artifact["files"]) == set(old_files)
        and all(
            {k: row[k] for k in ("sha256", "size")} == artifact["files"][n]
            for n, row in old_files.items()
            if n != CHANGED
        ),
        "CATALOG_REUSE",
    )
    projected, excluded = projection_delta(baseline["projected"], artifact["files"], pure)
    require(
        len(excluded) == 23
        and artifact.get("reused_sqlite_sha256")
        == "78711a51904d2ce47d4c16cb53365e85d73bc15cd4aa6d10c4d2ef3754d81c53"
        and artifact.get("reused_pgvector_sha256")
        == "050bd2625e0efcf8f1b5bebd32135b1958a6f9b98e2c73d13137152488451d3b",
        "CATALOG_LIBRARY_PINS",
    )
    return {
        "stages": stages,
        "projected": projected,
        "excluded": excluded,
        "baseline": baseline,
        "catalog_artifact_sha256": digest(canonical(artifact)),
        "daemon_id": daemon,
    }


def read_owned(normal, directory, name, maximum, *, metadata=False, overlay=False):
    require("/" not in name and name not in {"", ".", ".."}, "OWNED_NAME")
    modes = (
        {0o600, 0o644}
        if metadata and name == "build-metadata.json"
        else ({0o444} if overlay else {0o600})
    )
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == before.st_gid == 0
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) in modes
            and before.st_size <= maximum,
            "OWNED_FILE_MODE",
        )
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        require(
            normal.identity(before) == normal.identity(after) == normal.identity(named)
            and before.st_mtime_ns == after.st_mtime_ns == named.st_mtime_ns
            and before.st_ctime_ns == after.st_ctime_ns == named.st_ctime_ns
            and len(raw) == before.st_size <= maximum,
            "OWNED_FILE_CHANGED",
        )
    return raw


def open_child_chain(root, parts, *, create=False):
    fd = os.dup(root)
    try:
        for part in parts:
            require(
                re.fullmatch(r"[A-Za-z0-9_+.-]+", part) and part not in {".", ".."}, "OVERLAY_PATH"
            )
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    created = True
                except FileExistsError:
                    created = False
            else:
                created = False
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if created:
                os.fchmod(child, 0o755)
            info = os.fstat(child)
            if not (
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == info.st_gid == 0
                and stat.S_IMODE(info.st_mode) in ({0o755} if create else {0o700})
            ):
                os.close(child)
                raise ValueError("OVERLAY_DIRECTORY_MODE")
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_overlay(normal, context_fd, source_fd, projected):
    os.mkdir("runtime-root", 0o755, dir_fd=context_fd)
    root = os.open("runtime-root", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=context_fd)
    try:
        os.fchmod(root, 0o755)
        for name, expected in sorted(projected.items()):
            parts = name.split("/")
            source = open_child_chain(source_fd, parts[:-1])
            target = None
            try:
                raw = read_owned(normal, source, parts[-1], MAX_FILE)
                require(
                    len(raw) == expected["size"] and digest(raw) == expected["sha256"],
                    "OVERLAY_SOURCE_PIN",
                )
                target = open_child_chain(root, parts[:-1], create=True)
                fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o444,
                    dir_fd=target,
                )
                with os.fdopen(fd, "wb") as writer:
                    os.fchmod(writer.fileno(), 0o444)
                    writer.write(raw)
                    writer.flush()
                    os.fsync(writer.fileno())
            finally:
                os.close(source)
                if target is not None:
                    os.close(target)
    finally:
        os.close(root)


def snapshot_overlay(normal, context_fd, projected, pure):
    allowed_dirs = {p for n in projected for p in pure.parents(posixpath.dirname(n))}
    found_dirs, entries, contents = set(), {}, {}

    def walk(fd, prefix=""):
        info = os.fstat(fd)
        require(
            info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o755,
            "OVERLAY_DIR_MODE",
        )
        for name in os.listdir(fd):
            path = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                require(path in allowed_dirs and path not in found_dirs, "OVERLAY_EXTRA_DIRECTORY")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    found_dirs.add(path)
                    walk(child, path + "/")
                finally:
                    os.close(child)
            else:
                require(path in projected and path not in entries, "OVERLAY_EXTRA_FILE")
                raw = read_owned(normal, fd, name, MAX_FILE, overlay=True)
                entries[path] = {"sha256": digest(raw), "size": len(raw)}
                contents[path] = pure.file_entry(raw)

    root = os.open("runtime-root", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=context_fd)
    try:
        walk(root)
    finally:
        os.close(root)
    require(entries == projected and found_dirs == allowed_dirs, "OVERLAY_SNAPSHOT")
    return contents


def publish_oci(normal, record_fd, path, pure):
    import tarfile

    # El parser fijo valida todos los blobs antes de publicar un solo dato.
    base = pure.read_oci(path, BASE_MANIFEST)
    pure.validate_base(base)
    os.mkdir("base-oci", 0o700, dir_fd=record_fd)
    root = os.open("base-oci", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=record_fd)
    try:
        os.mkdir("blobs", 0o700, dir_fd=root)
        blobs = os.open("blobs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            os.mkdir("sha256", 0o700, dir_fd=blobs)
            leaf = os.open("sha256", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=blobs)
            try:
                with tarfile.open(path, "r:") as archive:
                    for member in archive:
                        name = pure.safe_name(member.name)
                        if member.isdir() or name == "manifest.json":
                            continue
                        raw = archive.extractfile(member).read(MAX_ARCHIVE + 1)
                        require(len(raw) == member.size <= MAX_ARCHIVE, "OCI_PUBLISH_SIZE")
                        target, basename = (
                            (leaf, name.rsplit("/", 1)[1])
                            if name.startswith("blobs/sha256/")
                            else (root, name)
                        )
                        normal.write_file(target, basename, raw)
            finally:
                os.close(leaf)
        finally:
            os.close(blobs)
    finally:
        os.close(root)
    return base


def verify_final(pure, base, final, projected):
    pure.validate_base(base)
    wanted = dict(base["config"]["config"])
    wanted["Labels"] = {**wanted.get("Labels", {}), **LABELS}
    require(final["config"]["config"] == wanted, "FINAL_CONFIG")
    layers = base["manifest"]["layers"]
    require(
        final["manifest"]["layers"][: len(layers)] == layers
        and final["config"]["rootfs"]["diff_ids"][: len(layers)]
        == base["config"]["rootfs"]["diff_ids"],
        "FINAL_BASE_LAYERS",
    )
    for name, row in projected.items():
        actual = final["files"].get(name, {})
        require(
            all(actual.get(k) == row[k] for k in ("sha256", "size"))
            and actual.get("mode") == 0o444
            and actual.get("uid") == actual.get("gid") == 0,
            "FINAL_PROJECTED_FILE",
        )
    require(
        all(final["files"].get(n) == row for n, row in base["files"].items()),
        "FINAL_FOREIGN_BASE_CHANGE",
    )
    allowed = {p for n in projected for p in pure.parents(posixpath.dirname(n))}
    for name in set(final["files"]) - set(base["files"]) - set(projected):
        require(
            name in allowed
            and final["files"][name] == {"kind": "directory", "mode": 0o755, "uid": 0, "gid": 0},
            "FINAL_EXTRA_NODE",
        )
    require(
        final["files"]["lib/apk/db/installed"] == base["files"]["lib/apk/db/installed"],
        "FINAL_APK_DATABASE",
    )
    return pure.abi_closure(base["files"], {n: final["files"][n] for n in projected})


def build_arguments():
    return [
        "buildx",
        "build",
        "--builder",
        "default",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--pull=false",
        "--provenance=false",
        "--sbom=false",
        "--build-context",
        "pgbase=oci-layout://" + RECORD + "/base-oci@sha256:" + BASE_MANIFEST,
        "--iidfile",
        RECORD + "/image-id.txt",
        "--metadata-file",
        RECORD + "/build-metadata.json",
        "--file",
        CONTEXT + "/Dockerfile",
        "--load",
        CONTEXT,
    ]


def module_bytes(raw, expected, name, flag):
    require(digest(raw) == expected, "SUPPORT_SOURCE_PIN")
    module = types.ModuleType(name)
    module.__dict__[flag] = expected
    exec(compile(raw, "<" + name + ">", "exec"), module.__dict__)
    return module


def reference_data(profile, encoded, pure):
    require(set(encoded) == set(profile["references"]), "REFERENCE_FRAME_SET")
    result = {}
    for name, row in profile["references"].items():
        raw = base64.b64decode(encoded[name], validate=True)
        require(len(raw) == row["size"] and digest(raw) == row["sha256"], "REFERENCE_BYTES")
        result[name] = pure.json_object(raw)
    return result


def verify_sdk_gates(normal, security, store, docker, data, gates):
    prepared = store.verify()
    require(prepared == data["sdk_profile"]["backed"]["prepare"], "SDK_ROOTS_HISTORY")
    require(security.validate_plan(data["v7_profile"]) == SECURITY_PROFILE, "V7_PLAN")
    require(security.daemon_identity(docker) == gates["daemon_id"], "SDK_CURRENT_DAEMON")
    control = security.control_open(normal)
    try:
        security.check_control(
            normal,
            control,
            {"profile_sha256": SECURITY_PROFILE},
            {k: v for k, v in gates["stages"].items() if k != "stop"},
        )
        require(
            normal.read_file(control, "profile.json", maximum=32768)
            == canonical(data["v7_profile"]),
            "V7_PHYSICAL_PROFILE",
        )
        matches = []
        names = os.listdir(control)
        require(len(names) <= 256, "V7_CONTROL_COUNT")
        for name in names:
            if re.fullmatch(r"stop-[0-9]+\.json", name):
                raw = normal.read_file(control, name, maximum=1024**2)
                if raw == canonical(gates["stages"]["stop"]):
                    matches.append(name)
        require(len(matches) == 1, "STOP_PHYSICAL_RECEIPT")
    finally:
        os.close(control)
    observed = normal.check_container(docker.inspect(CID), CID)
    require(
        observed == gates["stages"]["stop"]["container"]
        and observed["running"] is False
        and observed["pid"] == 0
        and observed["no_foreign"] is True,
        "SDK_CURRENT_STOPPED_EPOCH",
    )
    # La proyección de 96c está respaldada y también se contrasta con sus nodos físicos.
    old = normal.open_directory(OLD_RECORD)
    try:
        before = data["baseline_prepared"]["receipt"]
        expected = before["directories"]["record"]
        require(
            normal.identity(os.fstat(old)) == expected["directory"]
            and digest(normal.read_file(old, ".runtime-image-owner.json"))
            == expected["marker_sha256"],
            "BASELINE_RECORD_IDENTITY",
        )
        require(
            normal.read_file(old, "prepare.json", maximum=1024**2)
            == json.dumps(before, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
            "BASELINE_PHYSICAL_PREPARE",
        )
        finish = os.open(
            "finish-image-v1", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=old
        )
        try:
            normal.checked_node(os.fstat(finish), 0, directory=True)
            marker = normal.json_object(normal.read_file(finish, ".runtime-image-owner.json"))
            require(
                marker
                == {
                    "owner": "agencia-digital-marketing-ai:postgres-immich-runtime:v4.1",
                    "path": OLD_RECORD + "/finish-image-v1",
                    "directory": normal.identity(os.fstat(finish)),
                },
                "BASELINE_FINISH_IDENTITY",
            )
            require(
                normal.read_file(finish, "receipt.json", maximum=1024**2)
                == json.dumps(
                    data["baseline_finished"]["receipt"],
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode(),
                "BASELINE_PHYSICAL_FINISH",
            )
        finally:
            os.close(finish)
    finally:
        os.close(old)
    return observed


def verify_context(normal, descriptors, projected, pure, dockerfile):
    context = descriptors["context"]
    require(
        set(os.listdir(context)) == {MARKER, "Dockerfile", ".dockerignore", "runtime-root"},
        "CONTEXT_NO_FOREIGN",
    )
    require(
        normal.read_file(context, "Dockerfile") == dockerfile
        and normal.read_file(context, ".dockerignore")
        == b"*\n!Dockerfile\n!runtime-root/\n!runtime-root/**\n",
        "CONTEXT_SOURCES",
    )
    return snapshot_overlay(normal, context, projected, pure)


def image_inspection(docker, image):
    template = '{"id":{{json .Id}},"descriptor":{{json .Descriptor}},"user":{{json .Config.User}},"entrypoint":{{json .Config.Entrypoint}},"cmd":{{json .Config.Cmd}},"labels":{{json .Config.Labels}},"os":{{json .Os}},"architecture":{{json .Architecture}}}'
    return json.loads(docker.capture(["image", "inspect", "--format", template, image], timeout=5))


def observed_ids(metadata, iidfile, log):
    manifest = metadata.get("containerimage.digest")
    require(
        isinstance(manifest, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", manifest),
        "METADATA_MANIFEST",
    )
    configs = re.findall(r"exporting config (sha256:[a-f0-9]{64}) done", log)
    result = {"image_id": manifest, "iidfile": iidfile, "approval": False}
    if len(configs) == 1:
        result["config_digest"] = configs[0]
    return result


def apply(request):
    profile, mode = request["profile"], request.get("mode")
    require(mode in {"prepare", "build"}, "RUNTIME_MODE")
    profile_sha = validate_profile(profile)
    require(
        profile_sha == request.get("profile_sha256")
        and globals().get("_SECURITY_RUNTIME_SOURCE_SHA256") == profile["sources"]["remote"],
        "RUNTIME_SOURCE_PROFILE_PIN",
    )
    normal = module_bytes(
        base64.b64decode(request["sdk_source_b64"], validate=True),
        SDK_SHA,
        "runtime_security_sdk",
        "_SOURCE_SHA256",
    )
    security = module_bytes(
        base64.b64decode(request["security_source_b64"], validate=True),
        SECURITY_SHA,
        "runtime_security_gates",
        "_REBUILD_SOURCE_SHA256",
    )
    pure = load_pure(base64.b64decode(request["pure_source_b64"], validate=True))
    layout = base64.b64decode(request["layout_source_b64"], validate=True)
    require(digest(layout) == LAYOUT_SHA, "LAYOUT_SOURCE_PIN")
    data = reference_data(profile, request["references_b64"], pure)
    gates = validate_gates(data, pure)
    host_request = {
        "plan": data["sdk_profile"]["sdk_plan"],
        "layout_source_b64": request["layout_source_b64"],
    }
    normal.check_request(host_request)
    normal.verify_host(host_request)
    store = normal.Store(host_request)
    descriptors, armed, partial = {}, False, {}
    step = "no-foreign"
    process = None
    try:
        os.umask(0o077)
        store.open()
        docker = store.docker()
        verify_sdk_gates(normal, security, store, docker, data, gates)
        versions = check_docker(docker)
        step = "namespace"
        descriptors = open_runtime(normal, create=mode == "prepare")
        context, record = descriptors["context"], descriptors["record"]
        os.mkdir("." + mode + "-started", 0o700, dir_fd=record)
        armed = True
        dockerfile = base64.b64decode(request["dockerfile_b64"], validate=True)
        require(
            digest(dockerfile) == profile["sources"]["dockerfile"]
            and dockerfile == dockerfile_bytes(),
            "DOCKERFILE_PIN",
        )
        projected = gates["projected"]
        if mode == "prepare":
            step = "promote-overlay"
            normal.write_file(record, "profile.json", canonical(profile))
            normal.write_file(context, "Dockerfile", dockerfile)
            normal.write_file(
                context, ".dockerignore", b"*\n!Dockerfile\n!runtime-root/\n!runtime-root/**\n"
            )
            source = normal.open_directory(normal.OUTPUTS + "/security-rebuild-v7/runtime-root")
            try:
                write_overlay(normal, context, source, projected)
            finally:
                os.close(source)
            overlay = verify_context(normal, descriptors, projected, pure, dockerfile)
            step = "local-base-oci"
            archive = export_image(normal, docker, record, "sha256:" + BASE_MANIFEST, "base.tar")
            base = publish_oci(normal, record, RECORD + "/base.tar", pure)
            require(hash_archive(normal, record, "base.tar") == archive, "BASE_ARCHIVE_CHANGED")
            base_proof = pure.validate_base(base)
            require(len(base_proof["packages"]) == 45, "BASE_APK_COUNT")
            abi = pure.abi_closure(base["files"], overlay)
            fields = {
                "directories": tree_identity(normal, descriptors),
                "archive": archive,
                "base": base_proof,
                "oci_files": verify_oci_tree(normal, record, None),
                "projected": projected,
                "excluded": gates["excluded"],
                "abi": abi,
                "catalog_artifact_sha256": gates["catalog_artifact_sha256"],
            }
        else:
            step = "prepared-no-drift"
            prepared = checked_envelope(request["prepared"], "prepare", profile_sha=profile_sha)
            require(
                prepared["source_sha256"] == profile["sources"]["remote"]
                and normal.read_file(record, "prepare.json", maximum=1024**2) == canonical(prepared)
                and normal.read_file(record, "profile.json", maximum=32768) == canonical(profile),
                "PREPARE_PHYSICAL_PIN",
            )
            require(
                prepared["directories"] == tree_identity(normal, descriptors)
                and prepared["projected"] == projected
                and prepared["excluded"] == gates["excluded"]
                and prepared["catalog_artifact_sha256"] == gates["catalog_artifact_sha256"],
                "PREPARE_DATA_DRIFT",
            )
            require(
                set(os.listdir(record))
                == {
                    MARKER,
                    ".prepare-started",
                    ".build-started",
                    "profile.json",
                    "prepare.json",
                    "base.tar",
                    "base-oci",
                },
                "RECORD_NO_FOREIGN",
            )
            overlay = verify_context(normal, descriptors, projected, pure, dockerfile)
            require(
                hash_archive(normal, record, "base.tar") == prepared["archive"], "BASE_ARCHIVE_PIN"
            )
            verify_oci_tree(normal, record, prepared["oci_files"])
            base = pure.read_oci(RECORD + "/base.tar", BASE_MANIFEST)
            require(
                pure.validate_base(base) == prepared["base"]
                and pure.abi_closure(base["files"], overlay) == prepared["abi"],
                "PREPARE_ABI_DRIFT",
            )
            step = "buildx-copy-only"
            fd = os.open(
                "build.log",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=record,
            )
            with os.fdopen(fd, "wb", buffering=0) as sink:
                outcome = docker.command(build_arguments(), timeout=300, sink=sink)
                process = outcome["process"]
                sink.flush()
                os.fsync(sink.fileno())
            require(outcome["returncode"] == 0, "BUILD_PROCESS_FAILED")
            step = "observed-image-ids"
            log_raw = read_owned(normal, record, "build.log", MAX_CAPTURE)
            iid = read_owned(normal, record, "image-id.txt", 128).decode("ascii").strip()
            require(re.fullmatch(r"sha256:[a-f0-9]{64}", iid), "IID_DIGEST")
            metadata_raw = read_owned(normal, record, "build-metadata.json", 65536, metadata=True)
            metadata = pure.json_object(metadata_raw)
            partial.update(observed_ids(metadata, iid, log_raw.decode("utf-8", "strict")))
            normal.write_file(record, "observed-image-ids.json", canonical(partial))
            step = "image-oci-export"
            final_archive = export_image(normal, docker, record, partial["image_id"], "image.tar")
            final = pure.read_oci(RECORD + "/image.tar", partial["image_id"][7:])
            partial["config_digest"] = final["config_digest"]
            ids = build_identity(
                metadata,
                iid,
                image_inspection(docker, partial["image_id"]),
                log_raw.decode("utf-8"),
                final,
            )
            step = "final-abi"
            abi = verify_final(pure, base, final, projected)
            require(
                verify_context(normal, descriptors, projected, pure, dockerfile) == overlay,
                "BUILD_OVERLAY_DRIFT",
            )
            fields = {
                **ids,
                "archive": final_archive,
                "archive_path": RECORD + "/image.tar",
                "metadata_sha256": digest(metadata_raw),
                "prepare_receipt_sha256": request["prepared"]["receipt_sha256"],
                "abi": abi,
                "packages": prepared["base"]["packages"],
                "process": process,
                "log_sha256": digest(log_raw),
                "observed_ids_preserved": True,
            }
        step = "final-stopped-epoch"
        verify_sdk_gates(normal, security, store, docker, data, gates)
        receipt = {
            "success": True,
            "stage": mode,
            "owner": OWNER,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "docker_versions": versions,
            "release_approved": False,
            "sql_verified": False,
            "final_sca_verified": False,
            "sdk_executed": False,
            **fields,
        }
        normal.write_file(record, mode + ".json", canonical(receipt))
        return envelope(receipt)
    except BaseException as error:
        receipt = {
            "success": False,
            "stage": mode,
            "owner": OWNER,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "step": step,
            "error_type": type(error).__name__,
            "detail": str(error)[:400],
            "process": process,
            "observed": partial,
            "partial_evidence_preserved": True,
            "release_approved": False,
            "sql_verified": False,
            "final_sca_verified": False,
            "sdk_executed": False,
        }
        if armed:
            try:
                normal.write_file(descriptors["record"], mode + ".json", canonical(receipt))
            except BaseException:
                receipt["receipt_written"] = False
        return envelope(receipt)
    finally:
        for fd in descriptors.values():
            os.close(fd)
        store.close()


def dockerfile_bytes():
    return (
        b"# Fuente local autenticada: sin frontend remoto, RUN ni descargas.\n"
        b"FROM pgbase\n"
        b'LABEL org.opencontainers.image.title="PostgreSQL Immich de la agencia" \\\n'
        b'      org.opencontainers.image.version="16.15-vchord1.1.1-vector0.8.5-sqlite3.54.1-agencia.security-v7" \\\n'
        b'      cl.e-scale.agencia.owner="agencia-digital-marketing-ai:postgres-immich-security-runtime:v1"\n'
        b"COPY --chown=0:0 runtime-root/ /\n"
        b"USER 70:70\n"
    )


def main():
    import sys

    def expired(_signum, _frame):
        raise TimeoutError("Presupuesto runtime600s agotado.")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(600)
    try:
        header = sys.stdin.buffer.read(4)
        require(len(header) == 4, "FRAME_HEADER")
        size = struct.unpack(">I", header)[0]
        require(0 < size <= MAX_REQUEST, "FRAME_LIMIT")
        raw = sys.stdin.buffer.read(size)
        require(len(raw) == size and not sys.stdin.buffer.read(1), "FRAME_LENGTH")
        result = apply(json_object(raw))
        print(json.dumps({"success": result["receipt"]["success"], "result": result}), flush=True)
        return 0 if result["receipt"]["success"] else 1
    except BaseException as error:
        print(
            json.dumps(
                {"success": False, "error_type": type(error).__name__, "values_hidden": True}
            ),
            flush=True,
        )
        return 1
    finally:
        signal.alarm(0)


# Helpers de IO copiados de la fuente autenticada 9e0; no se muta el módulo histórico.


def runtime_directory(normal, path, *, create=False):
    import os

    parent = normal.open_directory(posixpath.dirname(path))
    try:
        name = posixpath.basename(path)
        if create:
            os.mkdir(name, 0o700, dir_fd=parent)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            normal.checked_node(os.fstat(fd), 0, directory=True)
            marker = {"owner": OWNER, "path": path, "directory": normal.identity(os.fstat(fd))}
            if create:
                normal.write_file(fd, MARKER, canonical(marker))
            elif normal.json_object(normal.read_file(fd, MARKER)) != marker:
                raise ValueError("Marcador runtime no acredita el inodo propio.")
            if (
                normal.identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
                != marker["directory"]
            ):
                raise ValueError("La ruta runtime cambió de inodo.")
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(parent)


def open_runtime(normal, *, create=False):
    import os

    result = {}
    try:
        for shared in (
            "/opt/media-platform/build",
            "/opt/media-platform/build/contexts",
            "/var/lib/media-platform/build",
        ):
            fd = normal.marked_directory(shared, shared=True)
            os.close(fd)
        for name, path in (("context", CONTEXT), ("record", RECORD)):
            parent = posixpath.dirname(path)
            try:
                os.stat(parent, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                exists = False
            fd = runtime_directory(normal, parent, create=create and not exists)
            os.close(fd)
            result[name] = runtime_directory(normal, path, create=create)
        return result
    except BaseException:
        for fd in result.values():
            os.close(fd)
        raise


def tree_identity(normal, descriptors):
    import os

    result = {}
    for name, path in (("context", CONTEXT), ("record", RECORD)):
        current = runtime_directory(normal, path)
        try:
            info = normal.identity(os.fstat(descriptors[name]))
            if normal.identity(os.fstat(current)) != info:
                raise ValueError("El namespace runtime cambió de inodo.")
            result[name] = {
                "path": path,
                "directory": info,
                "marker_sha256": digest(normal.read_file(current, MARKER)),
            }
        finally:
            os.close(current)
    return result


def check_docker(docker):
    if (
        docker.capture(["version", "--format", "{{.Server.Version}}"], timeout=5).decode().strip()
        != "29.9.3"
    ):
        raise ValueError("Se exige el daemon Docker29.9.3 revisado.")
    version = docker.capture(["buildx", "version"], timeout=5).decode().strip()
    if not version.startswith("github.com/docker/buildx v0.38.1 "):
        raise ValueError("Se exige Buildx0.38.1 revisado.")
    inspection = docker.capture(["buildx", "inspect", "default"], timeout=5).decode()
    drivers = [
        line.split(":", 1)[1].strip()
        for line in inspection.splitlines()
        if line.startswith("Driver:")
    ]
    if drivers != ["docker"]:
        raise ValueError("Sólo se admite el builder default con driver docker.")
    return {"daemon": "29.9.3", "buildx": version, "driver": "docker", "builder": "default"}


def hash_archive(normal, record_fd, name):
    import os

    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=record_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        normal.checked_node(info, 0)
        if info.st_size > MAX_ARCHIVE:
            raise ValueError("Exportación OCI excesiva.")
        sha = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
        if normal.identity(
            os.stat(name, dir_fd=record_fd, follow_symlinks=False)
        ) != normal.identity(info):
            raise ValueError("La exportación cambió mientras se leía.")
        return {"size": info.st_size, "sha256": sha.hexdigest()}


def export_image(normal, docker, record_fd, image_id, name):
    import os

    try:
        os.stat(name, dir_fd=record_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("Una exportación anterior no se sobrescribe.")
    result = docker.command(
        ["image", "save", "--output", RECORD + "/" + name, image_id], timeout=120
    )
    if result["returncode"] != 0:
        raise ValueError("La exportación local Docker falló.")
    # umask077 y padre root0700; Docker crea el archivo regular privado.
    evidence = hash_archive(normal, record_fd, name)
    return evidence


def verify_oci_tree(normal, record_fd, expected):
    import os

    root = normal.open_directory(RECORD + "/base-oci")
    try:
        actual = {}

        def walk(fd, prefix=""):
            normal.checked_node(os.fstat(fd), 0, directory=True)
            for entry in os.scandir(fd):
                import stat

                name = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        walk(child, name + "/")
                    finally:
                        os.close(child)
                else:
                    normal.checked_node(info, 0)
                    data = normal.read_file(fd, entry.name, maximum=MAX_ARCHIVE)
                    actual[name] = {"sha256": digest(data), "size": len(data)}

        walk(root)
        if expected is not None and actual != expected:
            raise ValueError("Los blobs OCI publicados cambiaron desde prepare.")
        return actual
    finally:
        os.close(root)


if __name__ == "__main__":
    raise SystemExit(main())
