"""Prepara inventarios GPU completos y exige auditorías OSV separadas y verificables."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "requirements-metrics.lock",
    "requirements-immich.in",
    "immich-artifacts.json",
    "requirements-whisper.lock",
    "whisper-artifacts.json",
)
COUNTS = {"metrics": 4, "immich-runtime": 70, "immich-build": 8, "whisper": 94}
SOURCE_EXCEPTION = {
    "name": "immich-model",
    "version": "0.2.0",
    "commit": "8bf3fd1b44b0c8608f52fb6e50ddff52826576d8",
    "url": "https://codeload.github.com/immich-app/ml-models/tar.gz/8bf3fd1b44b0c8608f52fb6e50ddff52826576d8",
    "sha256": "8c42ff838e035e9415b56b6df7921529553a8b15f6acc9c16956e8ef389cc612",
}
ORT = {
    "name": "onnxruntime-gpu",
    "version": "1.27.1",
    "url": "https://aiinfra.pkgs.visualstudio.com/2692857e-05ef-43b4-ba9c-ccf1c22c437c/_packaging/9387c3aa-d9ad-4513-968c-383f6f7f53b8/pypi/download/onnxruntime-gpu/1.27.1/onnxruntime_gpu-1.27.1-cp312-cp312-win_amd64.whl",
    "sha256": "1f4a0b56de1510db3d418bf00760224a00dbbbbb52114ff6a85bb3a5e2e279d4",
}


def digest(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


def load_json(file):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Un JSON contiene claves duplicadas.")
            result[key] = value
        return result

    return json.loads(file.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def normalized(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("Una dependencia carece de nombre válido.")
    return re.sub(r"[-_.]+", "-", name).lower()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_]*", value):
        raise ValueError("Cada dependencia requiere una versión exacta.")
    return value


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError("Falta una URL de artefacto.")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
        or parsed.hostname
        not in {"files.pythonhosted.org", "aiinfra.pkgs.visualstudio.com", "codeload.github.com"}
    ):
        raise ValueError("La procedencia del artefacto no es HTTPS oficial fijada.")
    return unquote(parsed.path.rsplit("/", 1)[-1])


def wheel_identity(url):
    filename = validate_url(url)
    parts = filename[:-4].split("-") if filename.endswith(".whl") else []
    if len(parts) < 5:
        raise ValueError("Se requiere una rueda con identidad y versión explícitas.")
    return normalized(parts[0]), version(parts[1])


def validate_source(source, project, archive):
    if not isinstance(source, dict) or not re.fullmatch(r"[a-f0-9]{40}", str(source.get("commit"))):
        raise ValueError("La fuente upstream requiere commit completo.")
    expected = f"https://codeload.github.com/{project}/{archive}/{source['commit']}"
    if source.get("url") != expected or not re.fullmatch(
        r"[a-f0-9]{64}", str(source.get("sha256"))
    ):
        raise ValueError("La fuente upstream carece de URL o SHA256 fijados.")
    if type(source.get("size")) is not int or source["size"] <= 0:
        raise ValueError("La fuente upstream requiere tamaño positivo.")


def artifact(entry):
    if not isinstance(entry, dict):
        raise ValueError("El manifiesto contiene un artefacto inválido.")
    name, pin = normalized(entry.get("name")), version(entry.get("version"))
    if not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256"))):
        raise ValueError("Un artefacto carece de SHA256 válido.")
    if type(entry.get("size")) is not int or not 0 < entry["size"] <= 4 * 1024**3:
        raise ValueError("Un artefacto carece de tamaño acotado.")
    filename = validate_url(entry.get("url"))
    if name == ORT["name"] and any(entry.get(key) != expected for key, expected in ORT.items()):
        raise ValueError("ORT debe conservar versión, URL y SHA256 exactos del feed Microsoft.")
    if name == SOURCE_EXCEPTION["name"]:
        if any(entry.get(key) != expected for key, expected in SOURCE_EXCEPTION.items()):
            raise ValueError("La única excepción de fuente no coincide con el contrato fijado.")
        if entry.get("kind") != "sdist" or entry.get("role") != "runtime":
            raise ValueError("La excepción solo pertenece a la fuente runtime fijada.")
    elif entry.get("kind", "wheel") == "wheel":
        if wheel_identity(entry["url"]) != (name, pin):
            raise ValueError("La URL de rueda difiere del nombre o versión declarados.")
    elif entry.get("kind") == "sdist":
        ending = next((suffix for suffix in (".tar.gz", ".zip") if filename.endswith(suffix)), None)
        if (
            not ending
            or not filename[: -len(ending)].endswith("-" + pin)
            or normalized(filename[: -len(ending) - len(pin) - 1]) != name
        ):
            raise ValueError("La URL de fuente difiere del pin declarado.")
    else:
        raise ValueError("Tipo de artefacto no admitido.")
    if (
        urlsplit(entry["url"]).hostname != "files.pythonhosted.org"
        and name != SOURCE_EXCEPTION["name"]
    ):
        if any(entry.get(key) != expected for key, expected in ORT.items()):
            raise ValueError("El único wheel de feed externo debe coincidir con ORT fijado.")
    return {"name": name, "version": pin, "sha256": entry["sha256"], "url": entry["url"]}


def lock_entries(file):
    result = {}
    for raw in file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:==([^\s]+)| @ (https://[^\s]+)) --hash=sha256:([a-f0-9]{64})",
            line,
        )
        if not match:
            raise ValueError(
                "El lock exige un pin exacto o URL HTTPS, un SHA256 y ninguna opción externa."
            )
        name, pin, url, sha = match.groups()
        name = normalized(name)
        if name in result:
            raise ValueError("El lock repite una dependencia.")
        if url:
            if name == SOURCE_EXCEPTION["name"]:
                if url != SOURCE_EXCEPTION["url"] or sha != SOURCE_EXCEPTION["sha256"]:
                    raise ValueError("La fuente del lock difiere de la excepción fijada.")
                pin = SOURCE_EXCEPTION["version"]
            else:
                wheel_name, pin = wheel_identity(url)
                if wheel_name != name:
                    raise ValueError("El nombre del lock difiere de su rueda.")
                if urlsplit(url).hostname != "files.pythonhosted.org" and any(
                    actual != ORT[key]
                    for key, actual in {
                        "name": name,
                        "version": pin,
                        "url": url,
                        "sha256": sha,
                    }.items()
                ):
                    raise ValueError("El lock contiene un feed externo distinto del ORT fijado.")
        result[name] = {"name": name, "version": version(pin), "sha256": sha, "url": url}
    return result


def collect_inventory(root):
    source = root / "gpu-bridge-win11"
    metrics = lock_entries(source / FILES[0])
    if set(metrics) != {"uvicorn", "click", "h11", "colorama"}:
        raise ValueError("El entorno de métricas no cubre sus cuatro paquetes.")
    immich = load_json(source / "immich-artifacts.json")
    if (
        not isinstance(immich, dict)
        or immich.get("schema_version") != 1
        or immich.get("platform") != "win_amd64"
        or immich.get("python") != "3.12"
        or not isinstance(immich.get("artifacts"), list)
    ):
        raise ValueError("El esquema o plataforma del manifiesto Immich no está implementado.")
    validate_source(immich.get("source"), "immich-app/immich", "tar.gz")
    roles = {"runtime": {}, "build": {}}
    for entry in immich.get("artifacts", []):
        if not isinstance(entry, dict) or entry.get("role") not in roles:
            raise ValueError("Un artefacto Immich no pertenece a runtime o build.")
        item = artifact(entry)
        target = roles[entry["role"]]
        if item["name"] in target:
            raise ValueError("Un grafo Immich repite una dependencia.")
        target[item["name"]] = item
    declared = lock_entries(source / "requirements-immich.in")
    if set(declared) != set(roles["runtime"]):
        raise ValueError("El lock Immich y el manifiesto runtime no forman una biyección.")
    for name, item in declared.items():
        expected = roles["runtime"][name]
        if (
            any(item[key] != expected[key] for key in ("version", "sha256"))
            or item["url"] is not None
            and item["url"] != expected["url"]
        ):
            raise ValueError("Un pin o hash del lock Immich difiere del manifiesto.")
    if (
        "immich-model" not in roles["runtime"]
        or roles["runtime"].get("onnxruntime-gpu", {}).get("version") != ORT["version"]
    ):
        raise ValueError("Falta la fuente o el runtime ORT fijado.")
    whisper = lock_entries(source / "requirements-whisper.lock")
    manifest = load_json(source / "whisper-artifacts.json")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("platform") != "win_amd64"
        or manifest.get("python") != "3.12"
    ):
        raise ValueError("El esquema o plataforma del manifiesto Whisper no está implementado.")
    if any(
        item["url"] is None or urlsplit(item["url"]).hostname != "files.pythonhosted.org"
        for item in whisper.values()
    ):
        raise ValueError("El lock Whisper solo admite sus ruedas oficiales PyPI fijadas.")
    validate_source(manifest.get("upstream"), "speaches-ai/speaches", "zip")
    direct = manifest.get("wheels", [])
    if not isinstance(direct, list) or len(direct) != 26:
        raise ValueError("El manifiesto Whisper debe conservar sus 26 ruedas directas fijadas.")
    names = set()
    for entry in direct:
        item = artifact(entry)
        if item["name"] in names or whisper.get(item["name"]) != item:
            raise ValueError("Una rueda directa Whisper no coincide con el lock completo.")
        names.add(item["name"])
    entries = {
        "metrics": metrics,
        "immich-runtime": roles["runtime"],
        "immich-build": roles["build"],
        "whisper": whisper,
    }
    graphs = {}
    for name, packages in entries.items():
        if len(packages) != COUNTS[name]:
            raise ValueError(
                f"El grafo {name} no contiene sus {COUNTS[name]} dependencias completas."
            )
        pins = {key: item["version"] for key, item in sorted(packages.items())}
        audit = {
            key: value
            for key, value in pins.items()
            if not (name == "immich-runtime" and key == "immich-model")
        }
        text = "".join(f"{key}=={value}\n" for key, value in audit.items())
        graphs[name] = {
            "packages": pins,
            "audit": audit,
            "source_exceptions": [SOURCE_EXCEPTION] if name == "immich-runtime" else [],
            "requirements_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    return {
        "schema_version": 1,
        "vulnerability_service": "osv",
        "inputs_sha256": {f"gpu-bridge-win11/{name}": digest(source / name) for name in FILES},
        "graphs": graphs,
    }


def prepare(root, output):
    (output / "receipt.json").unlink(missing_ok=True)
    inventory = collect_inventory(root)
    output.mkdir(parents=True, exist_ok=True)
    for name, graph in inventory["graphs"].items():
        (output / f"{name}.requirements.txt").write_text(
            "".join(f"{key}=={value}\n" for key, value in graph["audit"].items()),
            encoding="utf-8",
            newline="\n",
        )
    (output / "inventory.json").write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return inventory


def validate_report(report, expected):
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("dependencies"), list)
        or not report["dependencies"]
    ):
        return ["Falta una lista completa de dependencias auditadas."]
    errors, observed = [], set()
    for entry in report["dependencies"]:
        if not isinstance(entry, dict):
            errors.append("Una fila auditada carece de identidad.")
            continue
        try:
            name = normalized(entry.get("name"))
        except ValueError:
            errors.append("Una fila auditada carece de identidad.")
            continue
        if name in observed or name not in expected or entry.get("version") != expected.get(name):
            errors.append(f"Identidad, versión, duplicado o paquete extra: {name}")
        observed.add(name)
        if "skip_reason" in entry or entry.get("vulns") != []:
            errors.append(f"Dependencia omitida, vulnerable o sin resultado válido: {name}")
    if observed != set(expected):
        errors.append("El informe no cubre exactamente todos los pines esperados.")
    return errors


def validate_reports(root, inventory_file, reports_dir):
    # Un intento nuevo nunca conserva como vigente el recibo exitoso de un intento anterior.
    (reports_dir / "receipt.json").unlink(missing_ok=True)
    inventory = load_json(inventory_file)
    if inventory != collect_inventory(root):
        raise ValueError("Los inputs o el inventario cambiaron después de preparar la auditoría.")
    reports, diagnostics = {}, {}
    for name, graph in inventory["graphs"].items():
        if (
            digest(inventory_file.parent / f"{name}.requirements.txt")
            != graph["requirements_sha256"]
        ):
            raise ValueError(
                "Los requisitos auditados cambiaron después de preparar el inventario."
            )
        report = reports_dir / f"{name}.json"
        errors = validate_report(load_json(report), graph["audit"])
        diagnostic = reports_dir / f"{name}.stderr.txt"
        text = diagnostic.read_text(encoding="utf-8-sig")
        if "Unsupported OSV schema version" in text or "missing 'affected' list" in text:
            errors.append("OSV omitió un aviso por esquema no compatible o incompleto.")
        if errors:
            raise ValueError(f"Auditoría {name} rechazada: " + "; ".join(errors))
        reports[name] = digest(report)
        diagnostics[name] = digest(diagnostic)
    receipt = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "vulnerability_service": "osv",
        "tool": "pip-audit==2.10.1",
        "inventory_sha256": digest(inventory_file),
        "inputs_sha256": inventory["inputs_sha256"],
        "reports_sha256": reports,
        "diagnostics_sha256": diagnostics,
        "audited_counts": {
            name: len(graph["audit"]) for name, graph in inventory["graphs"].items()
        },
        "source_exceptions": [SOURCE_EXCEPTION],
        "known_vulnerabilities": 0,
        "gpu_test_performed": False,
    }
    (reports_dir / "receipt.json").write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    generator = commands.add_parser(
        "prepare", help="Genera cuatro inventarios auditables sin instalar nada."
    )
    generator.add_argument("--output", type=Path, required=True)
    validator = commands.add_parser(
        "validate", help="Exige cobertura exacta y genera recibo con hashes."
    )
    validator.add_argument("--inventory", type=Path, required=True)
    validator.add_argument("--reports-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            value = prepare(ROOT, args.output)
            print(
                json.dumps(
                    {
                        "prepared": {
                            name: len(graph["audit"]) for name, graph in value["graphs"].items()
                        }
                    },
                    ensure_ascii=False,
                )
            )
        else:
            value = validate_reports(ROOT, args.inventory, args.reports_dir)
            print(
                json.dumps(
                    {"audited": value["audited_counts"], "known_vulnerabilities": 0},
                    ensure_ascii=False,
                )
            )
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"No se acreditaron las dependencias GPU: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
