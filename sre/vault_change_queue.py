"""Cola privada por generación; no lee la bóveda ni crea raíces al importar."""

import ast
import hashlib
import json
import os
import re
import socket
import stat
import sys
import time
import types
import uuid
from contextlib import contextmanager
from pathlib import Path

OWNER = "agencia-digital-marketing-ai:vault-changes:v1"
MAXIMUM_VAULT = 8 * 1024**2
IDLE_AUDIT_SECONDS = 10
WORKSPACE = Path(r"D:\Proyectos\Agencia Digital de Marketing AI")
QUEUE_ROOT = Path(r"C:\GPU-Bridge\vault-changes-v1")
BOOT_LOCKS = (
    ("operations/uploader.lock.json", "uploader"),
    (".active.json", "active"),
)
CODE_ROOT = WORKSPACE / ".tools/backup-source-build/vault-changes-code-v1"
CODE_OWNER = "agencia-digital-marketing-ai:vault-changes-code:v1"
LEGACY_ROOT = Path(r"C:\GPU-Bridge\vault-backup-r2-v1")
LEGACY_CODE = WORKSPACE / ".tools/backup-source-build/vault-controller-v1"
LEGACY_PROFILE_SHA = "96d171fc9e822ebad90406dba6c1aee5b3738e5fdb359fd1582027ecacdd89a4"
STATE_SHA = "aa8fc9a38f1707f9a27289b4fe1efb6b6643eddd0ed672deb6b4502b13215df7"
PARSER_SHA = "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda"
BACKUP_SHA = "a682d3fa4e79aa48a869fb92c16ae42edecae1c26709a911228807060ecbe9fc"
DOC_SHA = "1a52555de27a51403da07bdad35091b1a7fd88ee6b9888a79b3d9215fb046c18"
RESTIC_SHA = "78735dd1c7c841724a3c9d469dac12ffaf79ab6236bdb0ca9b32ca4fe2a07427"
JOB_SHA = "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44"
REPOSITORY_ID = "83a5af3624f05aa209e30ed81f75d225d265330fc3d13086cf21f297b5e12241"
GENERATION_FILES = {
    "candidate.env",
    "prepared.json",
    "committed.json",
    "attempt.json",
    "claim.json",
    "snapshot-recorded.json",
    "result.json",
    "uploaded.json",
    "cas-conflict.json",
    "template-error.json",
}


def require(value, code):
    if not value:
        raise ValueError(code)


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def decoded(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _value: require(False, "JSON_CONSTANT")
    )


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class Queue:
    """Compone Store/read_regular congelados; los tests usan otro backend explícito."""

    def __init__(
        self,
        store,
        reader,
        parse_env,
        validate_comments,
        *,
        maximum_bytes=2 * 1024**3,
        maximum_generations=1024,
    ):
        self.store, self.reader = store, reader
        self.parse_env, self.validate_comments = parse_env, validate_comments
        self.maximum_bytes, self.maximum_generations = maximum_bytes, maximum_generations
        self._idle_observation = None
        self._idle_upload = None

    def path(self, generation, leaf):
        require(
            isinstance(generation, str) and re.fullmatch(r"[a-f0-9]{32}", generation),
            "GENERATION_ID",
        )
        require(leaf in GENERATION_FILES, "GENERATION_FILE")
        return "captures/" + generation + "/" + leaf

    def metadata(self, generation):
        metadata = self.store.json(self.path(generation, "prepared.json"))
        require(
            metadata.get("owner") == OWNER
            and metadata.get("generation_id") == generation
            and metadata.get("source_kind") in {"ROOT_CANDIDATE", "OBSERVED"}
            and type(metadata.get("bytes")) is int
            and 0 < metadata["bytes"] <= MAXIMUM_VAULT
            and re.fullmatch(r"[a-f0-9]{64}", str(metadata.get("sha256", ""))),
            "PREPARED_IDENTITY",
        )
        return metadata

    def candidate(self, generation):
        metadata = self.metadata(generation)
        raw = self.store.read(self.path(generation, "candidate.env"), MAXIMUM_VAULT)
        require(
            len(raw) == metadata["bytes"] and digest(raw) == metadata["sha256"], "CANDIDATE_CHANGED"
        )
        values, errors = self.parse_env(raw.decode("utf-8-sig", errors="strict"))
        require(values and not errors, "CANDIDATE_INVALID")
        return raw, metadata

    def generations(self):
        result = []
        for directory in (self.store.root / "captures").iterdir():
            require(
                directory.is_dir()
                and not directory.is_symlink()
                and not getattr(directory.lstat(), "st_file_attributes", 0) & 0x400
                and re.fullmatch(r"[a-f0-9]{32}", directory.name),
                "QUEUE_FOREIGN",
            )
            self.store.access.check(directory)
            require(
                set(item.name for item in directory.iterdir()) <= GENERATION_FILES, "QUEUE_FOREIGN"
            )
            require((directory / "prepared.json").is_file(), "INCOMPLETE_GENERATION")
            metadata = self.metadata(directory.name)
            info = (directory / "candidate.env").lstat()
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_size == metadata["bytes"]
                and not getattr(info, "st_file_attributes", 0) & 0x400,
                "CANDIDATE_TYPE",
            )
            self.store.access.check(directory / "candidate.env")
            status = "OBSERVED" if metadata["source_kind"] == "OBSERVED" else "PREPARED"
            if (directory / "committed.json").exists():
                committed = self.store.json(self.path(directory.name, "committed.json"))
                require(
                    committed.get("generation_id") == directory.name
                    and committed.get("sha256") == metadata["sha256"]
                    and committed.get("kind") == "COMMITTED",
                    "COMMIT_IDENTITY",
                )
                status = "COMMITTED"
            result.append({**metadata, "status": status, "published": status == "COMMITTED"})
        return sorted(result, key=lambda row: (row["prepared_ns"], row["generation_id"]))

    def enqueue(self, raw, source_kind, *, base=None):
        """El llamador mantiene el lock corto; candidata y PREPARED se fsync antes de volver."""
        require(
            source_kind in {"ROOT_CANDIDATE", "OBSERVED"} and 0 < len(raw) <= MAXIMUM_VAULT,
            "CANDIDATE_SCOPE",
        )
        values, errors = self.parse_env(raw.decode("utf-8-sig", errors="strict"))
        require(values and not errors, "CANDIDATE_INVALID")
        return self._record_capture(raw, source_kind, base, self.generations())

    def _record_capture(self, raw, source_kind, base, prior):
        require(
            len(prior) < self.maximum_generations
            and sum(row["bytes"] for row in prior) + len(raw) <= self.maximum_bytes,
            "QUEUE_QUOTA",
        )
        generation = uuid.uuid4().hex
        self.store.mkdir("captures/" + generation)
        self.store.write(self.path(generation, "candidate.env"), raw)
        metadata = {
            "schema_version": 1,
            "owner": OWNER,
            "generation_id": generation,
            "source_kind": source_kind,
            "sha256": digest(raw),
            "bytes": len(raw),
            "prepared_ns": time.time_ns(),
            "base": base,
            "comments_valid": not self.validate_comments(raw.decode("utf-8-sig")),
            "published": False,
        }
        self.store.write(self.path(generation, "prepared.json"), encoded(metadata))
        self.candidate(generation)
        return metadata

    def mark_committed(self, generation, source_info):
        _raw, metadata = self.candidate(generation)
        require(
            source_info["sha256"] == metadata["sha256"]
            and source_info["bytes"] == metadata["bytes"],
            "COMMIT_SOURCE",
        )
        self.store.write(
            self.path(generation, "committed.json"),
            encoded(
                {
                    "kind": "COMMITTED",
                    "generation_id": generation,
                    "sha256": metadata["sha256"],
                    "published_ns": time.time_ns(),
                    "source": source_info,
                }
            ),
        )

    def record(self, generation, leaf, value):
        self.store.write(self.path(generation, leaf), encoded(value))

    def promotion_temp(self, parent, generation, raw, suffix):
        return private_temp(parent, generation, raw, self.store.access, suffix)

    def _capture_directory_identity(self):
        directory = self.store.root / "captures"
        info = directory.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "QUEUE_FOREIGN",
        )
        self.store.access.check(directory)
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns

    def observe(self, source, *, idle_cache=False):
        with self.store.lock("observe"):
            raw, info = self.reader(source, MAXIMUM_VAULT)
            time.sleep(0.1)
            stable, stable_info = self.reader(source, MAXIMUM_VAULT)
            require(stable == raw and stable_info == info, "OBSERVATION_NOT_STABLE")
            source_sha = digest(raw)
            if idle_cache:
                key = (source, source_sha, self._capture_directory_identity())
                previous = self._idle_observation
                if previous and previous[0] == key and time.monotonic() < previous[1]:
                    return None
            self._idle_observation = None
            values, errors = self.parse_env(raw.decode("utf-8-sig", errors="strict"))
            require(values and not errors, "OBSERVATION_INVALID")
            prior = self.generations()
            if prior and prior[-1]["sha256"] == source_sha:
                if idle_cache:
                    self._idle_observation = (key, time.monotonic() + IDLE_AUDIT_SECONDS)
                return None
            require(0 < len(raw) <= MAXIMUM_VAULT, "CANDIDATE_SCOPE")
            return self._record_capture(raw, "OBSERVED", info, prior)

    def next_pending(self, *, idle_cache=False, prefer_latest=False):
        selected = self.pending_uploads(limit=1, idle_cache=idle_cache, prefer_latest=prefer_latest)
        return selected[0] if selected else None

    def pending_uploads(
        self, *, limit, idle_cache=False, prefer_latest=False, active_attempts=None
    ):
        """Coteja la cola y omite sólo los intentos del cargador que tiene el bloqueo."""
        active_attempts = {} if active_attempts is None else active_attempts
        with self.store.lock("select-upload"):
            if idle_cache:
                key = self._capture_directory_identity(), tuple(active_attempts)
                previous = self._idle_upload
                if previous and previous[0] == key and time.monotonic() < previous[1]:
                    return []
            self._idle_upload = None
            pending = []
            unseen = set(active_attempts)
            for row in self.generations():
                generation = row["generation_id"]
                directory = self.store.root / "captures" / generation
                if generation in active_attempts:
                    require(
                        (directory / "attempt.json").is_file()
                        and self.store.json(self.path(generation, "attempt.json"))
                        == active_attempts[generation],
                        "UPLOAD_RECONCILIATION_REQUIRED",
                    )
                    unseen.remove(generation)
                    continue
                if (directory / "uploaded.json").exists():
                    receipt = self.store.json(self.path(row["generation_id"], "uploaded.json"))
                    require(
                        receipt.get("success") is True
                        and receipt.get("generation_id") == row["generation_id"]
                        and receipt.get("sha256") == row["sha256"]
                        and re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("snapshot_id", ""))),
                        "UPLOAD_RECEIPT",
                    )
                    continue
                require(not (directory / "attempt.json").exists(), "UPLOAD_RECONCILIATION_REQUIRED")
                pending.append(row)
            require(not unseen, "UPLOAD_RECONCILIATION_REQUIRED")
            if not pending and idle_cache:
                self._idle_upload = (key, time.monotonic() + IDLE_AUDIT_SECONDS)
            selected = []
            while pending and len(selected) < limit:
                row = pending[0]
                if prefer_latest:
                    row = next(
                        (
                            item
                            for item in reversed(pending)
                            if item["status"] in {"COMMITTED", "OBSERVED"}
                        ),
                        row,
                    )
                selected.append(row)
                pending.remove(row)
                prefer_latest = not prefer_latest
            return selected

    def begin_attempt(self, generation, subject):
        with self.store.lock("reserve-upload"):
            _raw, metadata = self.candidate(generation)
            attempt = {
                "owner": OWNER,
                "generation_id": generation,
                "child_nonce": uuid.uuid4().hex,
                "sha256": metadata["sha256"],
                "started_ns": time.time_ns(),
                "subject": subject,
            }
            self.store.write(self.path(generation, "attempt.json"), encoded(attempt))
            return attempt

    def archive_previous_boot_locks(self, boot_ns):
        """Conserva los locks antiguos bajo el mutex de recuperación del observador."""
        moves = []
        for name, label in BOOT_LOCKS:
            path = self.store._path(name)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if max(info.st_mtime_ns, getattr(info, "st_birthtime_ns", info.st_mtime_ns)) >= boot_ns:
                continue
            raw, identity = self.reader(path, 4096)
            if identity["mtime_ns"] >= boot_ns or (identity["device"], identity["inode"]) != (
                info.st_dev,
                info.st_ino,
            ):
                continue
            mark = decoded(raw)
            keys = {"owner", "nonce", "operation"} if label == "active" else {"owner", "nonce"}
            require(
                isinstance(mark, dict)
                and mark.keys() == keys
                and mark["owner"] == OWNER
                and re.fullmatch(r"[a-f0-9]{32}", str(mark["nonce"]))
                and (label != "active" or isinstance(mark["operation"], str)),
                "BOOT_LOCK_IDENTITY",
            )
            archive = self.store._path(
                f"operations/recovered-boot-lock-{label}-{mark['nonce']}.json"
            )
            require(not os.path.lexists(archive), "BOOT_LOCK_ARCHIVE_OCCUPIED")
            moves.append((path, archive, raw, identity))
        # El bloqueo corto sale al final, después de acreditar todos los candidatos.
        for path, archive, raw, identity in moves:
            require(self.reader(path, 4096) == (raw, identity), "BOOT_LOCK_CHANGED")
            path.rename(archive)
        return len(moves)

    @contextmanager
    def uploader_lock(self):
        name = "operations/uploader.lock.json"
        raw = encoded({"owner": OWNER, "nonce": uuid.uuid4().hex})
        self.store.write(name, raw)
        path = self.store.root / name
        _before, identity = self.reader(path, 4096)
        try:
            yield
        finally:
            observed, now = self.reader(path, 4096)
            require(observed == raw and now == identity, "UPLOADER_LOCK_CHANGED")
            path.unlink()


def private_temp(parent, generation, raw, access, suffix):
    """Un archivo EXCL en el mismo volumen que la bóveda; la candidata no se mueve."""
    name = ".vault-v2-" + generation + "-" + suffix + ".tmp"
    path = Path(parent) / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        access.check(path)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    access.check(path)
    return path


def read_pinned(path, pin, maximum=1024**2):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "PIN_PATH")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "PIN_LINK",
        )
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
        "PIN_TYPE_SIZE",
    )
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    final = path.lstat()

    def signature(row):
        return (
            row.st_dev,
            row.st_ino,
            row.st_size,
            row.st_mtime_ns,
            row.st_nlink,
            stat.S_IFMT(row.st_mode),
        )

    require(
        signature(info) == signature(before) == signature(after) == signature(final)
        and info.st_ctime_ns == final.st_ctime_ns
        and before.st_ctime_ns == after.st_ctime_ns
        and len(raw) == info.st_size
        and digest(raw) == pin,
        "PIN_CHANGED",
    )
    return raw


def frozen(name, path, pin):
    raw = read_pinned(path, pin)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def validate_config(config, *, preparing=False):
    require(
        config.get("kind") == "vault-changes-config-v1"
        and config.get("owner") == OWNER
        and config.get("queue_root") == str(QUEUE_ROOT)
        and config.get("code_root") == str(CODE_ROOT)
        and config.get("vault_path") == str(WORKSPACE / ".env")
        and config.get("template_path") == str(WORKSPACE / ".env.example")
        and config.get("publish_parent") == str(WORKSPACE / ".tools/backup-source-build")
        and config.get("repository_id") == REPOSITORY_ID,
        "CONFIG_SCOPE",
    )
    require(
        config.get("legacy_profile")
        == {"path": str(LEGACY_ROOT / "profile.json"), "sha256": LEGACY_PROFILE_SHA},
        "CONFIG_LEGACY",
    )
    require(
        set(config.get("sources", {}))
        == {
            "vault_updates_v2.py",
            "vault_change_queue.py",
            "vault_change_worker.py",
            "document_vault.py",
        }
        and all(re.fullmatch(r"[a-f0-9]{64}", str(pin)) for pin in config["sources"].values())
        and config["sources"]["document_vault.py"] == DOC_SHA,
        "CONFIG_SOURCES",
    )
    require(
        isinstance(config.get("code_identity"), dict)
        and (preparing or isinstance(config.get("queue_identity"), dict)),
        "CONFIG_IDENTITIES",
    )
    require(
        config.get("poll_seconds") == 1
        and config.get("maximum_vault_bytes") == MAXIMUM_VAULT
        and config.get("outer_timeout_seconds") == 390
        and config.get("snapshot_ids_in_vault") is False
        and type(config.get("maximum_queue_bytes")) is int
        and MAXIMUM_VAULT <= config["maximum_queue_bytes"] <= 64 * 1024**3
        and type(config.get("maximum_generations")) is int
        and 1 <= config["maximum_generations"] <= 100000,
        "CONFIG_LIMITS",
    )
    python = config.get("python", {})
    require(
        isinstance(python.get("path"), str)
        and Path(python["path"]).is_absolute()
        and Path(python["path"]).suffix.lower() == ".exe"
        and re.fullmatch(r"[a-f0-9]{64}", str(python.get("sha256", ""))),
        "CONFIG_PYTHON",
    )


class Runtime:
    """Se construye sólo por una operación explícita con perfil físico fijado por Root."""

    def __init__(self, config, path, pin, actor, *, preparing=False):
        raw = read_pinned(Path(path), pin, 256 * 1024)
        require(decoded(raw) == config, "CONFIG_CHANGED")
        validate_config(config, preparing=preparing)
        self.config, self.config_path, self.config_sha256 = config, str(path), pin
        self.state = frozen("vault_changes_state", LEGACY_CODE / "vault-r2-state.py", STATE_SHA)
        self.access = self.state.NativeAccess()
        self.access.check(Path(path))
        code = self.state.Store(CODE_ROOT, CODE_OWNER, self.access)
        code.open(config["code_identity"])
        require(
            Path(actor).absolute().parent == CODE_ROOT and Path(actor).name in config["sources"],
            "ACTOR_SEALED",
        )
        for name, checksum in config["sources"].items():
            self.access.check(CODE_ROOT / name)
            read_pinned(CODE_ROOT / name, checksum)
        self.parser = frozen("vault_changes_parser", LEGACY_CODE / "check_env.py", PARSER_SHA)
        self.profile = decoded(read_pinned(LEGACY_ROOT / "profile.json", LEGACY_PROFILE_SHA))
        self.access.check(LEGACY_ROOT / "profile.json")
        require(
            self.profile["binary_sha256"] == RESTIC_SHA
            and self.profile["snapshot_ids_in_vault"] is False,
            "LEGACY_PROFILE_SCOPE",
        )
        require(
            socket.gethostname().casefold() == self.profile["hostname"].casefold(), "HOST_IDENTITY"
        )
        for name, checksum in self.profile["sources"].items():
            self.access.check(Path(name))
            read_pinned(Path(name), checksum)
        self.store = self.state.Store(QUEUE_ROOT, OWNER, self.access)
        if not preparing:
            self.store.open(config["queue_identity"])
        self.queue = Queue(
            self.store,
            lambda path, maximum: self.state.read_regular(Path(path), self.access, maximum),
            self.parser.parse_env,
            self.parser.validate_comments,
            maximum_bytes=config["maximum_queue_bytes"],
            maximum_generations=config["maximum_generations"],
        )
        self.access.check(Path(config["publish_parent"]))
        read_pinned(
            Path(config["publish_parent"]) / ".agency-backup-build-owner",
            "78925844fd312b9ce3be220ee9d2fe41442908101af8c14416895811110bcff6",
            128,
        )
        self.state.node(Path(config["template_path"]))

    def documentation(self):
        raw = read_pinned(CODE_ROOT / "document_vault.py", DOC_SHA)
        nodes = [
            node
            for node in ast.parse(raw).body
            if (isinstance(node, ast.FunctionDef) and node.name == "documentation")
            or (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "GROUPS"
                    for target in node.targets
                )
            )
        ]
        require(len(nodes) == 2, "DOCUMENTATION_AST")
        namespace = {}
        exec(
            compile(
                ast.Module(body=nodes, type_ignores=[]),
                str(CODE_ROOT / "document_vault.py"),
                "exec",
            ),
            namespace,
        )
        return namespace["documentation"]

    def backup_helpers(self):
        provider = frozen(
            "vault_changes_old_backup", LEGACY_CODE / "vault-r2-backup.py", BACKUP_SHA
        )
        provider.validate_profile(self.profile)
        return provider

    def job(self):
        return frozen("vault_changes_native_job", LEGACY_CODE / "windows_build_job.py", JOB_SHA)

    def current_values(self):
        raw, _metadata = self.queue.reader(Path(self.config["vault_path"]), MAXIMUM_VAULT)
        values, errors = self.parser.parse_env(raw.decode("utf-8-sig", errors="strict"))
        require(
            not errors and values.get("BACKUP_VAULT_R2_REPOSITORY_ID") == REPOSITORY_ID,
            "VAULT_REPOSITORY",
        )
        return values

    def verify_binary(self):
        self.access.check(Path(self.profile["binary_path"]))
        read_pinned(Path(self.profile["binary_path"]), RESTIC_SHA, 128 * 1024**2)
