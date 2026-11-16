"""Verifica entradas APK/SDK como datos; nunca instala ni ejecuta contenido."""

import base64
import collections
import hashlib
import io
import re
import tarfile
import zlib


def gzip_part(raw, maximum=64 * 1024**2):
    decoder = zlib.decompressobj(31)
    try:
        plain = decoder.decompress(raw, maximum + 1)
    except zlib.error as error:
        raise ValueError("El miembro gzip está truncado o dañado.") from error
    if len(plain) > maximum or not decoder.eof:
        raise ValueError("El miembro gzip excede su límite o no terminó.")
    consumed = len(raw) - len(decoder.unused_data)
    return raw[:consumed], plain, decoder.unused_data


def tar_files(raw, maximum=64 * 1024**2):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for index, member in enumerate(archive):
            if (
                index >= 10000
                or not member.isfile()
                or member.size > maximum
                or member.name in result
                or "/" in member.name
                or "\\" in member.name
            ):
                raise ValueError("La sección de control tiene entradas ambiguas o especiales.")
            data = archive.extractfile(member).read(maximum + 1)
            if len(data) != member.size:
                raise ValueError("Entrada de control truncada.")
            result[member.name] = data
    return result


def verified_control(raw, trusted_keys):
    """APK v2: firma sobre control gzip; no usa claves incluidas en el paquete."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    if not isinstance(raw, bytes) or len(raw) > 512 * 1024**2:
        raise ValueError("Archivo APK ausente o excesivo.")
    _signature_raw, signature_tar, remaining = gzip_part(raw, 1024**2)
    control_raw, control_tar, payload = gzip_part(remaining)
    signatures = tar_files(signature_tar, 65536)
    algorithms = {"RSA": hashes.SHA1, "RSA256": hashes.SHA256, "RSA512": hashes.SHA512}
    verified = None
    for name, signature in signatures.items():
        match = re.fullmatch(r"\.SIGN\.(RSA|RSA256|RSA512)\.([A-Za-z0-9@_.+-]+)", name)
        if not match:
            raise ValueError("Algoritmo o nombre de firma no admitido.")
        algorithm, key_id = match.groups()
        if key_id not in trusted_keys:
            continue
        try:
            key = serialization.load_pem_public_key(trusted_keys[key_id])
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
                raise ValueError("La clave pública no cumple el contrato RSA.")
            key.verify(signature, control_raw, padding.PKCS1v15(), algorithms[algorithm]())
        except (ValueError, InvalidSignature) as error:
            raise ValueError("La firma APK no acredita una clave confiable.") from error
        verified = {
            "signing_key": key_id,
            "key_sha256": hashlib.sha256(trusted_keys[key_id]).hexdigest(),
            "signature_scheme": "RSA-" + algorithms[algorithm]().name.upper(),
        }
        break
    if verified is None:
        raise ValueError("No existe una firma de una clave confiable.")
    return {
        **verified,
        "files": tar_files(control_tar),
        "payload": payload,
        "control_identity": "Q1" + base64.b64encode(hashlib.sha1(control_raw).digest()).decode(),
    }


def verify_apk(raw, trusted_keys, expected):
    result = verified_control(raw, trusted_keys)
    if ".PKGINFO" not in result["files"] or not result["payload"].startswith(b"\x1f\x8b"):
        raise ValueError("El APK no contiene metadata y payload separados.")
    values, repeated = {}, {"depend", "provides", "install_if", "triggers", "replaces"}
    for line in result["files"][".PKGINFO"].decode().splitlines():
        if not line or line.startswith("#"):
            continue
        if " = " not in line:
            raise ValueError("Metadata APK mal formada.")
        name, value = line.split(" = ", 1)
        if name in values and name not in repeated:
            raise ValueError("Campo crítico repetido en metadata APK.")
        if name in repeated:
            values.setdefault(name, []).append(value)
        else:
            values[name] = value
    payload_hash = hashlib.sha256(result.pop("payload")).hexdigest()
    if (
        values.get("pkgname") != expected["P"]
        or values.get("pkgver") != expected["V"]
        or expected["A"] not in {"x86_64", "noarch"}
        or values.get("arch") not in {expected["A"], "noarch"}
        or result["control_identity"] != expected["C"]
        or payload_hash != values.get("datahash")
    ):
        raise ValueError("Identidad, arquitectura o datahash distintos del índice autenticado.")
    scripts = sorted(name for name in result["files"] if name != ".PKGINFO")
    result.pop("files")
    return {
        **result,
        "name": values["pkgname"],
        "version": values["pkgver"],
        "arch": values["arch"],
        "payload_sha256": payload_hash,
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "depends": values.get("depend", []),
        "provides": values.get("provides", []),
        "scripts": scripts,
        "scripts_executed": False,
        "payload_hash_verified": True,
        "payload_tar_checked": False,
        "native_apk_verify_pending": True,
    }


def parse_records(text):
    records = {}
    selected = set("PVA DpiCSoLk".replace(" ", ""))
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if len(line) < 2 or line[1] != ":" or line[0] not in selected:
                continue
            key, value = line[0], line[2:]
            if key in fields:
                raise ValueError("Campo crítico repetido en el índice APK.")
            fields[key] = value
        if not fields:
            continue
        if not fields.get("P") or not fields.get("V") or fields["P"] in records:
            raise ValueError("Registro APK sin identidad única.")
        records[fields["P"]] = fields
    return records


def selector(value):
    match = re.fullmatch(r"([^<>=~\s]+)([<>=~]*)([^\s]*)", value)
    if not match:
        raise ValueError("Selector APK no admitido.")
    return match.groups()


def version_match(value, operator, wanted):
    if not operator:
        return True
    if operator == "=":
        return value == wanted
    if value is None:
        return False
    if operator == "~":
        match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-r\d+)?", value)
        if not match or not re.fullmatch(r"\d+(?:\.\d+)*", wanted):
            raise ValueError("Fuzzy APK no numérico: requiere resolución nativa revisada.")
        prefix = [int(x) for x in wanted.split(".")]
        return [int(x) for x in match[1].split(".")][: len(prefix)] == prefix
    values = []
    for text in (value, wanted):
        match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-r(\d+))?", text)
        if not match:
            raise ValueError("Comparación APK no simple: requiere resolución nativa revisada.")
        values.append(([int(x) for x in match[1].split(".")], int(match[2] or 0)))
    size = max(len(v[0]) for v in values)
    left, right = [(tuple(v + [0] * (size - len(v))), revision) for v, revision in values]
    operations = {">=": left >= right, ">": left > right, "<=": left <= right, "<": left < right}
    if operator not in operations:
        raise ValueError("Operador APK requiere resolución nativa revisada.")
    return operations[operator]


def capabilities(record):
    result = [(record["P"], record["V"])]
    for value in record.get("p", "").split():
        name, separator, version = value.partition("=")
        result.append((name, version if separator else None))
    return result


def resolve_closure(records, seeds, base):
    """Expansión conservadora; rehúsa ambigüedad y no sustituye al solver APK."""
    selected, pending, edges, reused, conflicts = dict(base), [], [], set(), []
    providers = collections.defaultdict(set)
    for record in [*records.values(), *base.values()]:
        for cap, version in capabilities(record):
            providers[cap].add((record["P"], record["V"], version))

    def add(name, record):
        if name == "git" or name.startswith("git-") or name in {"gcompat", "libc6-compat"}:
            raise ValueError("La clausura incorpora Git o una capa de compatibilidad prohibida.")
        if name in selected:
            if selected[name]["V"] != record["V"]:
                raise ValueError("La clausura intenta actualizar o sustituir un paquete existente.")
            if name in base:
                reused.add(name)
            return
        selected[name] = record
        pending.append(name)

    def candidates(requirement):
        cap, operator, wanted = selector(requirement)
        return [
            (name, version)
            for name, version, supplied in providers.get(cap, ())
            if version_match(supplied, operator, wanted)
        ]

    def satisfied(requirement):
        if requirement.startswith("!"):
            return not satisfied(requirement[1:])
        return any(
            name in selected and selected[name]["V"] == version
            for name, version in candidates(requirement)
        )

    for name, version in seeds.items():
        if name not in records or records[name]["V"] != version:
            raise ValueError("El pin directo no coincide con el índice.")
        add(name, records[name])
    while True:
        while pending:
            name = pending.pop()
            for requirement in selected[name].get("D", "").split():
                if requirement.startswith("!"):
                    conflicts.append(requirement[1:])
                    continue
                options = candidates(requirement)
                existing = [(n, v) for n, v in options if n in selected and selected[n]["V"] == v]
                options = existing or options
                options = list(dict.fromkeys(options))
                if len(options) != 1:
                    raise ValueError(
                        "La clausura tiene un proveedor ausente o ambiguo: " + requirement
                    )
                target, version = options[0]
                record = (
                    base[target]
                    if target in base and base[target]["V"] == version
                    else records[target]
                )
                add(target, record)
                edges.append({"from": name, "requirement": requirement, "to": target})
        for name, record in records.items():
            conditions = record.get("i", "").split()
            if name not in selected and conditions and all(satisfied(c) for c in conditions):
                add(name, record)
        if not pending:
            break
    if any(satisfied(requirement) for requirement in conflicts):
        raise ValueError("La clausura satisface un conflicto declarado.")
    return {
        "packages": {n: r for n, r in selected.items() if n not in base},
        "base_reused": sorted(reused),
        "edges": edges,
        "native_solver_confirmation_pending": True,
        "installed": False,
    }
