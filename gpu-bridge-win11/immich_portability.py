"""Portabilidad acotada y verificable del componente oficial de Immich ML."""

import hashlib
import json
import math
import os
import re
import stat
import tarfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

VERSION = "3.3.6"
SOURCE_COMMIT = "587f549f46f1cd1822a06e16dcccecef04ea4565"
PATCH_PATHS = {
    "immich_ml/config.py",
    "immich_ml/allocator.py",
    "immich_ml/main.py",
    "immich_ml/models/base.py",
    "immich_ml/sessions/ort.py",
    "immich_ml/sessions/prepare.py",
}


def load_manifest(path=None):
    path = Path(path) if path else Path(__file__).with_name("immich-artifacts.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema_version") != 1
        or value.get("version") != VERSION
        or value.get("source", {}).get("commit") != SOURCE_COMMIT
    ):
        raise ValueError("El manifiesto no corresponde al release oficial revisado.")
    if {rule["path"] for rule in value["patches"]} != PATCH_PATHS or len(value["patches"]) != len(
        PATCH_PATHS
    ):
        raise ValueError(
            "El manifiesto debe contener exclusivamente los parches de portabilidad revisados."
        )
    records = [value["source"], *value["artifacts"], value["acceptance_image"]]
    for model in value["models"]:
        if model["task"] not in {"clip", "facial-recognition"} or not re.fullmatch(
            r"[A-Za-z0-9_-]+", model["name"]
        ):
            raise ValueError("Identidad de modelo no permitida.")
        for record in model["files"]:
            safe_relative(record["path"])
            safe_relative(record["cache_relative"])
            if "/resolve/" + model["commit"] + "/" not in record["url"]:
                raise ValueError("Los pesos deben usar una revisión inmutable.")
            records.append(record)
    for record in records:
        url = urlsplit(record["url"])
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.hostname
            not in {
                "codeload.github.com",
                "files.pythonhosted.org",
                "aiinfra.pkgs.visualstudio.com",
                "huggingface.co",
            }
        ):
            raise ValueError("El manifiesto contiene un origen no aprobado.")
        if not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
            raise ValueError("Cada artefacto debe tener una huella SHA256 fijada.")
    for rule in value["patches"]:
        if value["source_files"].get(rule["path"]) != rule["before_sha256"] or not re.fullmatch(
            r"[0-9a-f]{64}", rule["after_sha256"]
        ):
            raise ValueError("El parche no coincide con la fuente y resultado fijados.")
    return value


def safe_relative(name):
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != name
    ):
        raise ValueError("Ruta de fuente no permitida.")
    return path


def patch_sources(files, manifest):
    """Aplica sustituciones exactas sin mutar la entrada ni aceptar deriva upstream."""
    patched = dict(files)
    for rule in manifest["patches"]:
        name = rule["path"]
        safe_relative(name)
        if not name.startswith("immich_ml/") or name not in patched:
            raise ValueError("El parche no pertenece al componente esperado.")
        digest = hashlib.sha256(patched[name]).hexdigest()
        if digest == rule["after_sha256"]:
            continue
        if digest != rule["before_sha256"]:
            raise ValueError("La fuente no coincide con la versión upstream fijada.")
        text = patched[name].decode("utf-8")
        for replacement in rule["replacements"]:
            old = replacement["old"]
            if not old or text.count(old) != 1:
                raise ValueError("El parche no tiene una única coincidencia exacta.")
            text = text.replace(old, replacement["new"], 1)
        payload = text.encode("utf-8")
        if hashlib.sha256(payload).hexdigest() != rule["after_sha256"]:
            raise ValueError("El resultado del parche no coincide con su huella revisada.")
        patched[name] = payload
    return patched


def _small_integer_control(args, maximum_elements=64):
    inputs = args.get("input_type_shape")
    if not isinstance(inputs, list) or not inputs:
        return False
    for item in inputs:
        if not isinstance(item, dict) or len(item) != 1:
            return False
        dtype, shape = next(iter(item.items()))
        if dtype not in {"int64", "int32", "bool"} or not isinstance(shape, list):
            return False
        if (
            any(type(size) is not int or not 0 <= size <= maximum_elements for size in shape)
            or math.prod(shape) > maximum_elements
        ):
            return False
    return True


def validate_cuda_profile(events):
    """Exige cómputo CUDA; permite en CPU solo forma/tamaño o control entero pequeño."""
    if not isinstance(events, list):
        raise RuntimeError("El perfil de inferencia no es válido.")
    cuda_nodes = cpu_nodes = copies = 0
    control_ops = {
        "Add",
        "Sub",
        "Mul",
        "Div",
        "Mod",
        "Equal",
        "Less",
        "Greater",
        "Where",
        "Range",
        "Gather",
        "Concat",
        "Cast",
        "Unsqueeze",
        "Squeeze",
        "Slice",
        "Expand",
        "Reshape",
        "ConstantOfShape",
    }
    for event in events:
        if not isinstance(event, dict):
            raise RuntimeError("El perfil contiene un evento inválido.")
        if event.get("cat") != "Node" or not str(event.get("name", "")).endswith("_kernel_time"):
            continue
        args = event.get("args", {})
        if not isinstance(args, dict):
            raise RuntimeError("El perfil no acredita el proveedor del nodo.")
        provider, operation = args.get("provider"), args.get("op_name")
        if provider == "CUDAExecutionProvider":
            if str(operation).startswith("Memcpy"):
                copies += 1
            elif operation not in {"Shape", "Size", "Reshape", "Squeeze", "Unsqueeze"}:
                cuda_nodes += 1
        elif provider == "CPUExecutionProvider":
            # CLIP selecciona el token final entre 77 identificadores enteros;
            # las capas flotantes siguen acreditándose en CUDA.
            token_control = operation == "ArgMax" and _small_integer_control(args, 77)
            if (
                operation not in {"Shape", "Size"}
                and not token_control
                and not (operation in control_ops and _small_integer_control(args))
            ):
                raise RuntimeError("Un nodo de inferencia utilizó CPU fuera del control permitido.")
            cpu_nodes += 1
        else:
            raise RuntimeError("El perfil contiene un proveedor no permitido.")
    if cuda_nodes == 0:
        raise RuntimeError("El perfil no acredita nodos de inferencia CUDA.")
    return {"cuda_nodes": cuda_nodes, "cpu_control_nodes": cpu_nodes, "cuda_copy_nodes": copies}


def validate_provider_order(providers):
    if (
        not isinstance(providers, (list, tuple))
        or not providers
        or providers[0] != "CUDAExecutionProvider"
        or set(providers) - {"CUDAExecutionProvider", "CPUExecutionProvider"}
    ):
        raise RuntimeError("CUDA debe ser el proveedor primario y estar cargado realmente.")


class CudaEvidence:
    """Pruebas por proceso, con caducidad; los fallos requieren revisión o reinicio propio."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.last_good, self.failed = clock, None, False
        self.proofs = {}
        self.lock = threading.Lock()

    def record(self, kind, proof):
        with self.lock:
            self.last_good = self.clock()
            self.proofs[kind] = dict(proof)

    def fail(self):
        with self.lock:
            self.failed = True

    def snapshot(self, max_age=300):
        with self.lock:
            age = None if self.last_good is None else max(0, self.clock() - self.last_good)
            ready = age is not None and age <= max_age and not self.failed
            return {
                "ready": ready,
                "cuda_verified": ready,
                "proof_age_seconds": age,
                "inference_profiles": dict(self.proofs),
                "backend_failed": self.failed,
            }


CUDA_EVIDENCE = CudaEvidence()
_RUNTIME = None
CACHE_MARKER = ".immich-cache-owner.json"
SOURCE_RECEIPT = ".immich-source-receipt.json"
INSTALL_OWNER = "agencia-digital-marketing-ai:gpu-bridge:v4.1"


def _plain_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Se requiere una ruta local absoluta y sin escapes.")
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("No se admiten enlaces ni puntos de reanálisis en la caché.")
    return path


def _regular(path):
    path = _plain_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("El artefacto debe ser un archivo regular sin enlaces adicionales.")
    return info


def _digest(path):
    _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def configure_runtime(cache_root, manifest):
    """Marca únicamente un directorio vacío o verifica el marcador privado existente."""
    global _RUNTIME
    root = _plain_path(Path(cache_root))
    marker = {
        "schema": 1,
        "service": "immich-ml",
        "source_commit": manifest["source"]["commit"],
        "model_revisions": {
            model["task"] + "/" + model["name"]: model["commit"] for model in manifest["models"]
        },
    }
    if root.exists():
        if not root.is_dir():
            raise ValueError("La caché propia no es un directorio.")
        owner = root / CACHE_MARKER
        if owner.exists():
            _regular(owner)
            if json.loads(owner.read_bytes()) != marker:
                raise ValueError("El marcador de caché corresponde a otra instalación.")
        elif any(root.iterdir()):
            raise ValueError("No se adopta una caché existente sin marcador propio.")
        else:
            with owner.open("x", encoding="utf-8") as output:
                json.dump(marker, output, sort_keys=True)
    else:
        root.mkdir(mode=0o700)
        with (root / CACHE_MARKER).open("x", encoding="utf-8") as output:
            json.dump(marker, output, sort_keys=True)
    _RUNTIME = {"root": root.resolve(), "manifest": manifest, "verified": {}}
    return _RUNTIME


def adopt_verified_cache(cache_root, install_root, manifest):
    """Adopta explícitamente pesos propios precargados, tras cotejar recibo y cada byte."""
    root, install = _plain_path(Path(cache_root)), _plain_path(Path(install_root))
    if root != install / "models/immich" or not root.is_dir():
        raise ValueError("La adopción solo admite la caché Immich de la instalación propia.")
    owner, receipt = install / ".agencia-gpu-owner", install / "model-install-receipt.json"
    _regular(owner)
    _regular(receipt)
    if owner.read_text(encoding="utf-8").strip() != INSTALL_OWNER:
        raise ValueError("El marcador de la instalación no corresponde al puente propio.")
    expected = {}
    for model in manifest["models"]:
        for item in model["files"]:
            relative = str(safe_relative(item["cache_relative"]))
            if relative in expected:
                raise ValueError("El manifiesto duplica un artefacto del modelo.")
            expected[relative] = {
                "relative": "immich/" + relative,
                "sha256": item["sha256"],
                "size": item["size"],
            }
    records = json.loads(receipt.read_bytes()).get("model_files")
    if not isinstance(records, list):
        raise ValueError("El recibo de instalación no enumera los pesos verificados.")
    observed = {}
    for record in records:
        relative = str(safe_relative(record["relative"]))
        if relative.startswith("immich/"):
            key = relative.removeprefix("immich/")
            if key in observed:
                raise ValueError("El recibo duplica un artefacto de Immich.")
            observed[key] = record
    if not expected or observed != expected:
        raise ValueError("El recibo no coincide exactamente con los pesos fijados de Immich.")
    allowed_directories = {"."}
    for relative, item in expected.items():
        path = root.joinpath(*safe_relative(relative).parts)
        if _regular(path).st_size != item["size"] or _digest(path) != item["sha256"]:
            raise ValueError("Un peso precargado no coincide con el recibo y manifiesto.")
        allowed_directories.update(str(parent) for parent in PurePosixPath(relative).parents)
    for directory, names, files in os.walk(root, followlinks=False):
        for name in (*names, *files):
            path = _plain_path(Path(directory) / name)
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                if relative not in allowed_directories:
                    raise ValueError("La caché contiene un directorio no declarado.")
            elif relative not in expected and relative != CACHE_MARKER:
                raise ValueError("La caché contiene archivos no declarados; no se adopta.")
    marker = root / CACHE_MARKER
    if marker.exists():
        return configure_runtime(root, manifest)
    # No se borra ni mueve ningún peso. configure_runtime conserva la guarda normal.
    value = {
        "schema": 1,
        "service": "immich-ml",
        "source_commit": manifest["source"]["commit"],
        "model_revisions": {
            model["task"] + "/" + model["name"]: model["commit"] for model in manifest["models"]
        },
    }
    with marker.open("x", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True)
    return configure_runtime(root, manifest)


def _context():
    if _RUNTIME is None:
        manifest = load_manifest()
        base = Path(os.environ.get("GPU_MODEL_CACHE_ROOT", ""))
        configure_runtime(base / "immich", manifest)
    return _RUNTIME


def runtime_profile_directory():
    root = _context()["root"]
    path = _plain_path(root / "_profiles")
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def verify_model_cache(cache_dir, model_name, model_task):
    """Verifica pesos precargados; nunca descarga ni interpreta revision=main en red."""
    context = _context()
    root, directory = context["root"], _plain_path(Path(cache_dir))
    match = next(
        (
            model
            for model in context["manifest"]["models"]
            if model["name"] == model_name and model["task"] == str(model_task)
        ),
        None,
    )
    try:
        if match is None or directory != root / match["task"] / match["name"]:
            raise ValueError("El modelo no está fijado por el manifiesto de esta instalación.")
        for item in match["files"]:
            relative = safe_relative(item["cache_relative"])
            path = root.joinpath(*relative.parts)
            if not path.is_relative_to(directory):
                raise ValueError("El artefacto del modelo sale de su caché permitida.")
            info = _regular(path)
            stamp = (info.st_size, info.st_mtime_ns, info.st_ino)
            if context["verified"].get(str(path)) != stamp:
                if info.st_size != item["size"] or _digest(path) != item["sha256"]:
                    raise ValueError("Los pesos no coinciden con los artefactos fijados.")
                context["verified"][str(path)] = stamp
    except (OSError, ValueError):
        CUDA_EVIDENCE.fail()
        raise ValueError(
            "Modelo no disponible o no verificado; no se descarga durante una petición."
        ) from None


def _owned_model_path(path):
    context = _context()
    path = _plain_path(Path(path))
    roots = [
        context["root"] / model["task"] / model["name"] for model in context["manifest"]["models"]
    ]
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("La operación sale de una caché de modelo propia.")
    return context["root"], path


def _quarantine(path, recreate):
    root, path = _owned_model_path(path)
    moved = None
    if path.exists():
        if not path.is_dir():
            raise ValueError("La caché a preservar no es un directorio.")
        for directory, names, files in os.walk(path, followlinks=False):
            for name in (*names, *files):
                _plain_path(Path(directory) / name)
        quarantine = _plain_path(root / "_quarantine")
        quarantine.mkdir(mode=0o700, exist_ok=True)
        moved = quarantine / f"cache-{os.getpid()}-{time.time_ns()}"
        if moved.exists():
            raise ValueError("La cuarentena ya existe; no se reemplaza.")
        path.rename(moved)
    if recreate:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return moved


def clear_private_model_cache(path):
    return _quarantine(path, recreate=True)


def clear_private_graph_cache(path):
    root, path = _owned_model_path(path)
    if len(path.relative_to(root).parts) < 5:
        raise ValueError("La caché de grafos debe estar dentro del modelo y proveedor propios.")
    return _quarantine(path, recreate=False)


@contextmanager
def portable_graph_lock(path):
    from filelock import FileLock

    _, path = _owned_model_path(path)
    with FileLock(str(path), timeout=60):
        yield


def _patchset_digest(manifest):
    data = {key: manifest[key] for key in ("source_files", "patches")}
    data["source_commit"] = manifest["source"]["commit"]
    data["auxiliary_source_files"] = manifest.get("auxiliary_source_files", {})
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _patched_hashes(manifest):
    files = dict(manifest.get("auxiliary_source_files", {})) | dict(manifest["source_files"])
    for rule in manifest["patches"]:
        files[rule["path"]] = rule["after_sha256"]
    return files


def _source_receipt(manifest):
    return {
        "schema": 1,
        "service": "immich-ml",
        "version": manifest["version"],
        "source_commit": manifest["source"]["commit"],
        "patchset_sha256": _patchset_digest(manifest),
        "files": _patched_hashes(manifest),
    }


def verify_source(target, manifest):
    """Verifica cada archivo y rechaza añadidos o bytecode ajenos antes de importar."""
    target = _plain_path(Path(target))
    receipt = target / SOURCE_RECEIPT
    _regular(receipt)
    if json.loads(receipt.read_bytes()) != _source_receipt(manifest):
        raise ValueError("La fuente cacheada no tiene el recibo propio esperado.")
    expected = _patched_hashes(manifest)
    for name, digest in expected.items():
        relative = safe_relative(name)
        path = target.joinpath(*relative.parts)
        if _digest(path) != digest:
            raise ValueError("La fuente cacheada cambió después de su preparación.")
    for directory, names, files in os.walk(target, followlinks=False):
        for name in (*names, *files):
            path = _plain_path(Path(directory) / name)
            if (
                path.is_file()
                and path != receipt
                and path.relative_to(target).as_posix() not in expected
            ):
                raise ValueError("La fuente cacheada contiene archivos no declarados.")
    return target.resolve(strict=True)


def prepare_source(archive, target, manifest, allow_existing_original=False):
    """Publica solo el árbol ML fijado; nunca instala paquetes ni ejecuta la fuente."""
    archive, target = Path(archive), _plain_path(Path(target))
    if target.exists() and (target / SOURCE_RECEIPT).exists():
        return verify_source(target, manifest)
    if target.exists() and not allow_existing_original:
        raise ValueError("La fuente existente requiere autorización explícita y cotejo completo.")
    if _digest(archive) != manifest["source"]["sha256"]:
        raise ValueError("El archivo HTTPS no coincide con el SHA256 de la fuente fijada.")
    prefix = "immich-" + manifest["source"]["commit"] + "/machine-learning/"
    original = dict(manifest.get("auxiliary_source_files", {})) | dict(manifest["source_files"])
    files = {}
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            if not member.name.startswith(prefix):
                continue
            name = member.name[len(prefix) :]
            if member.isdir():
                continue
            safe_relative(name)
            if (
                name not in original
                or name in files
                or not member.isfile()
                or member.size > 5 * 1024 * 1024
            ):
                raise ValueError("El archivo de fuente contiene una entrada no permitida.")
            with bundle.extractfile(member) as source:
                payload = source.read()
            if hashlib.sha256(payload).hexdigest() != original[name]:
                raise ValueError("Un archivo upstream no coincide con el manifiesto.")
            files[name] = payload
    if set(files) != set(original):
        raise ValueError("El archivo HTTPS no contiene toda la fuente requerida.")
    files = patch_sources(files, manifest)
    for name, payload in files.items():
        if name.endswith(".py"):
            compile(payload, name, "exec")
    if target.exists():
        present = set()
        for directory, names, entries in os.walk(target, followlinks=False):
            for name in (*names, *entries):
                _plain_path(Path(directory) / name)
            for name in entries:
                path = Path(directory) / name
                relative = path.relative_to(target).as_posix()
                if relative not in files or _digest(path) not in {
                    original[relative],
                    hashlib.sha256(files[relative]).hexdigest(),
                }:
                    raise ValueError("La fuente existente contiene un archivo ajeno o modificado.")
                present.add(relative)
        if present != set(files):
            raise ValueError("La fuente existente está incompleta; no se adopta.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _plain_path(target.parent)
    target.mkdir(mode=0o700, exist_ok=allow_existing_original)
    for name, payload in files.items():
        path = target.joinpath(*safe_relative(name).parts)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() == payload:
            continue
        temporary = path.with_name(path.name + ".immich-portability-new")
        with temporary.open("xb") as output:
            output.write(payload)
        temporary.replace(path)
    with (target / SOURCE_RECEIPT).open("x", encoding="utf-8") as output:
        json.dump(_source_receipt(manifest), output, sort_keys=True)
    return verify_source(target, manifest)


def graph_worker_environment():
    """El hijo de preparación no necesita el token HTTP del puente."""
    names = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "GPU_MODEL_CACHE_ROOT",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "ORT_DISABLE_TELEMETRY",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in names or key.startswith("MACHINE_LEARNING_")
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--prepare-source", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--patch-existing-source", action="store_true")
    parser.add_argument("--prepare-cache", type=Path)
    parser.add_argument("--verify-models", type=Path)
    parser.add_argument("--adopt-verified-cache", type=Path)
    parser.add_argument("--install-root", type=Path)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if (
        sum(
            item is not None
            for item in (
                args.prepare_source,
                args.prepare_cache,
                args.verify_models,
                args.adopt_verified_cache,
            )
        )
        != 1
    ):
        parser.error("Elegir una única preparación o verificación explícita.")
    if args.prepare_source:
        if not args.target:
            parser.error("La fuente requiere --target absoluto.")
        target = prepare_source(
            args.prepare_source, args.target, manifest, args.patch_existing_source
        )
        print(
            json.dumps(
                {
                    "prepared": True,
                    "source": str(target),
                    "commit": SOURCE_COMMIT,
                    "patchset_sha256": _patchset_digest(manifest),
                }
            )
        )
    elif args.adopt_verified_cache:
        if not args.install_root:
            parser.error("La adopción requiere --install-root absoluto y su recibo propio.")
        adopt_verified_cache(args.adopt_verified_cache, args.install_root, manifest)
        print(
            json.dumps(
                {
                    "cache": str(args.adopt_verified_cache),
                    "owner_verified": True,
                    "models_verified": True,
                    "adopted": True,
                }
            )
        )
    else:
        root = args.prepare_cache or args.verify_models
        configure_runtime(root, manifest)
        if args.verify_models:
            for model in manifest["models"]:
                verify_model_cache(
                    root / model["task"] / model["name"], model["name"], model["task"]
                )
        print(
            json.dumps(
                {
                    "cache": str(root),
                    "owner_verified": True,
                    "models_verified": bool(args.verify_models),
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


class ProfiledCudaSession:
    def __init__(self, session, directory, evidence, kind):
        self._session, self._directory = session, directory.resolve(strict=True)
        self._evidence, self._kind, self._proof = evidence, kind, None
        self._lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self._session, name)

    def run(self, *args, **kwargs):
        with self._lock:
            try:
                validate_provider_order(self._session.get_providers())
                result = self._session.run(*args, **kwargs)
                if self._proof is None:
                    profile = Path(self._session.end_profiling())
                    info = profile.lstat()
                    if (
                        not profile.resolve().is_relative_to(self._directory)
                        or not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_size > 64 * 1024 * 1024
                        or getattr(info, "st_file_attributes", 0) & 0x400
                    ):
                        raise RuntimeError("El perfil CUDA no pertenece al directorio propio.")
                    self._proof = validate_cuda_profile(json.loads(profile.read_bytes()))
                validate_provider_order(self._session.get_providers())
                self._evidence.record(self._kind, self._proof)
                return result
            except Exception:
                self._evidence.fail()
                raise


def _model_kind(path):
    parts = Path(str(path)).parts
    for task in ("clip", "facial-recognition"):
        if task in parts:
            index = parts.index(task)
            if len(parts) > index + 2:
                return task + "/" + parts[index + 2]
    return "unclassified"


def strict_cuda_session(
    factory,
    *args,
    providers,
    sess_options,
    profile_directory=None,
    evidence=None,
    model_kind=None,
    **kwargs,
):
    """Impide reintentos globales en CPU y acredita la asignación real con profiling."""
    evidence = evidence or CUDA_EVIDENCE
    try:
        validate_provider_order(providers)
        directory = Path(profile_directory) if profile_directory else runtime_profile_directory()
        kind = model_kind or _model_kind(args[0])
        label = re.sub(r"[^A-Za-z0-9_-]", "_", kind)
        sess_options.enable_profiling = True
        sess_options.profile_file_prefix = str(
            directory / f"ort-{label}-{os.getpid()}-{time.time_ns()}"
        )
        session = factory(*args, providers=providers, sess_options=sess_options, **kwargs)
        session.disable_fallback()
        validate_provider_order(session.get_providers())
        return ProfiledCudaSession(session, directory, evidence, kind)
    except Exception:
        evidence.fail()
        raise
