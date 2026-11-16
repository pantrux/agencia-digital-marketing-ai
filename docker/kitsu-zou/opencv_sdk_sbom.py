"""Identidades instaladas y SBOM del SDK real; sólo transforma datos autenticados."""

import re
import shlex
from collections import Counter
from urllib.parse import quote


def os_identity(raw):
    if len(raw) > 16384:
        raise ValueError("os-release excesivo.")
    value = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, text = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z_]+", name) or name in value:
            raise ValueError("os-release ambiguo.")
        tokens = shlex.split(text, comments=False, posix=True)
        if len(tokens) != 1:
            raise ValueError("Valor os-release no literal.")
        value[name] = tokens[0]
    if value.get("ID") != "wolfi" or not re.fullmatch(r"[0-9]{8}", value.get("VERSION_ID", "")):
        raise ValueError("No se acredita la identidad Wolfi real.")
    return value


def installed_packages(raw, profile):
    if len(raw) > 4 * 1024**2:
        raise ValueError("La DB APK excede el límite.")
    expected = dict(profile["base_packages"])
    for row in profile["managed_packages"]:
        if row["name"] in expected:
            raise ValueError("La clausura instalada repite identidades.")
        expected[row["name"]] = row["record"]
    if len(expected) != 89:
        raise ValueError("Se requieren los 19 paquetes base y los 70 del SDK.")
    found = {}
    for block in raw.decode("utf-8").strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if len(line) >= 2 and line[1] == ":" and line[0] in {"P", "V", "A", "L", "o"}:
                if line[0] in fields:
                    raise ValueError("Registro APK con identificador duplicado.")
                fields[line[0]] = line[2:]
        name = fields.get("P")
        if not {"P", "V", "A", "L"} <= fields.keys() or name in found or name not in expected:
            raise ValueError("La DB contiene un paquete ambiguo o ajeno.")
        source = expected[name]
        if any(fields[k] != source[k] for k in ("P", "V", "A", "L")):
            raise ValueError("Versión, arquitectura o licencia instalada diverge del plan.")
        if "o" in fields and fields["o"] != source.get("o", name):
            raise ValueError("El origen instalado diverge del plan.")
        fields["_origin_provenance"] = "db-instalada" if "o" in fields else "indice-autenticado"
        fields["o"] = source.get("o", name)
        found[name] = fields
    if set(found) != set(expected):
        raise ValueError("Faltan paquetes de la clausura instalada.")
    return [found[name] for name in sorted(found)]


def make_sbom(installed, raw_os, manifest, header_records):
    operating = os_identity(raw_os)
    if len(installed) != 89 or len({row["P"] for row in installed}) != 89:
        raise ValueError("Inventario instalado incompleto.")
    components = []

    def apk(row, scope, archive=None, selected=None):
        name, version = row["P"], row["V"]
        properties = [
            {"name": "aquasecurity:trivy:SrcName", "value": row.get("o", name)},
            {"name": "aquasecurity:trivy:SrcVersion", "value": version},
            {"name": "agencia:scope", "value": scope},
            {
                "name": "agencia:origin_provenance",
                "value": row.get("_origin_provenance", "indice-autenticado"),
            },
        ]
        if archive:
            properties.append({"name": "agencia:archive_sha256", "value": archive})
        if selected is not None:
            properties.append({"name": "agencia:selected_headers", "value": str(selected)})
        return {
            "type": "library",
            "name": name,
            "version": version,
            "bom-ref": name + "@" + version,
            "purl": "pkg:apk/wolfi/"
            + quote(name, safe="")
            + "@"
            + quote(version, safe="")
            + "?arch="
            + row["A"]
            + "&distro="
            + operating["VERSION_ID"],
            "licenses": [{"license": {"name": row["L"]}}],
            "properties": properties,
        }

    for row in installed:
        components.append(apk(row, "instalado-verificado"))
    headers = manifest["direct_headers"]
    records = {(row["P"], row["V"]): row for row in header_records}
    if (
        len(headers) != 2
        or len(header_records) != 2
        or set(records) != {(row["name"], row["version"]) for row in headers}
    ):
        raise ValueError("Se requieren los dos paquetes de headers parciales.")
    for row in headers:
        if not row["selection"] or row["name"] in {c["name"] for c in components}:
            raise ValueError("Headers omitidos o confundidos con un paquete instalado.")
        record = records[(row["name"], row["version"])]
        if not record.get("L") or record.get("A") not in {"x86_64", "noarch"}:
            raise ValueError("Registro autenticado de headers sin licencia/arquitectura.")
        components.append(
            apk(record, "headers-parciales-verificados", row["sha256"], len(row["selection"]))
        )
    numpy = manifest["numpy_headers"]
    if numpy["version"] != "2.4.6" or not numpy["selection"]:
        raise ValueError("NumPy parcial no corresponde a los headers acreditados.")
    components.append(
        {
            "type": "library",
            "name": "numpy",
            "version": "2.4.6",
            "bom-ref": "numpy@2.4.6",
            "purl": "pkg:pypi/numpy@2.4.6",
            "licenses": [{"license": {"name": "BSD-3-Clause"}}],
            "properties": [
                {"name": "agencia:scope", "value": "headers-numpy-parciales-verificados"},
                {"name": "agencia:archive_sha256", "value": numpy["sha256"]},
                {"name": "agencia:license_sha256", "value": manifest["numpy_license"]["sha256"]},
                {"name": "agencia:selected_headers", "value": str(len(numpy["selection"]))},
            ],
        }
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "zou-opencv-sdk-real-export-and-ro-headers",
                "version": "1",
            }
        },
        "components": components,
    }


def assess_report(report, sbom):
    expected = {
        "wolfi": Counter(
            (c["name"], c["version"])
            for c in sbom["components"]
            if c["purl"].startswith("pkg:apk/")
        ),
        "python-pkg": Counter({("numpy", "2.4.6"): 1}),
    }
    observed = {name: Counter() for name in expected}
    findings = []
    for result in report.get("Results", []):
        kind = result.get("Type")
        if kind not in observed:
            if result.get("Packages") or result.get("Vulnerabilities"):
                raise ValueError("Trivy reportó un ecosistema fuera del inventario.")
            continue
        if result.get("Class") != ("os-pkgs" if kind == "wolfi" else "lang-pkgs"):
            raise ValueError("Trivy no identificó el sistema operativo/paquetes esperado.")
        observed[kind].update((p["Name"], p["Version"]) for p in result.get("Packages", []))
        findings.extend(result.get("Vulnerabilities", []))
    if observed != expected or sum(expected["wolfi"].values()) != 91:
        raise ValueError("La cobertura Trivy no es una biyección positiva de 91 APK y NumPy.")
    high = [v for v in findings if v.get("Severity") in {"HIGH", "CRITICAL"}]
    return {
        "coverage_complete": True,
        "installed_apk": 89,
        "partial_header_apk": 2,
        "reported_apk": 91,
        "numpy_partial": [["numpy", "2.4.6"]],
        "all_findings_count": len(findings),
        "high_critical": high,
        "findings_by_severity": dict(Counter(v.get("Severity", "UNKNOWN") for v in findings)),
        "scope": "SDK instalado por export real más headers parciales RO; no rueda ni imagen final",
    }
