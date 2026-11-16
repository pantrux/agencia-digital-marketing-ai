"""Prepara el árbol servidor/runner y la entrada de resolución pnpm sin Git."""

import argparse
import io
import json
import tarfile
from pathlib import Path

from apply_flatted_async_security import PATCH, PATCH_SHA, RELATIVE, SOURCE_SHA, derive
from apply_flatted_async_security import read_pinned as read_flatted
from prepare_runner_urllib3 import INPUTS, INPUTS_SHA, read_pinned, require, sha
from stage_runner_javascript import (
    BUILD_GROUPS,
    RUNNER,
    RUNTIME_GROUPS,
    linked_importer,
    package_resolution,
    project_lock,
    render_lock,
    render_yaml,
    yaml_documents,
)
from stage_server_wa_sqlite import WA_LEAF, WA_SRI, WA_URL, safe, sri, tar_gz

SERVER = "packages/cli"
EDITOR = "packages/frontend/editor-ui"
WA_KEY = f"wa-sqlite@{WA_URL}"
MAX_TOTAL = 300 * 1024**2
V5 = Path(__file__).resolve().parents[2] / ".tools/backup-source-build/n8n-js-npm-tarballs-v5"
V5_PINS = {
    "runtime-receipt.json": "7e7a3fb0a790ead52e7f37b24bc7acc814b94d0729799fa42237688333042a71",
    "build-additional-receipt.json": "f13b584bc33f15d1568aac4fa95ab8ed9dcd2237fe1d7c28c98864bb28f4d137",
}
ROOT_FILES = {"package.json", "LICENSE.md", "LICENSE_EE.md", "tsconfig.json", "turbo.json"}
SOURCE_PREFIXES = (
    "scripts/",
    "docker/images/n8n/",
    "docker/images/n8n-base/",
    "docker/images/runners/",
)


def registry_plan(lock, seeds, groups):
    projects, pending = set(), sorted(seeds)
    while pending:
        importer = pending.pop()
        if importer in projects:
            continue
        projects.add(importer)
        for group in groups:
            for row in lock["importers"][importer].get(group, {}).values():
                if row["version"].startswith("link:"):
                    pending.append(linked_importer(importer, row["version"]))
    pending = [
        (name, row["version"])
        for importer in projects
        for group in groups
        for name, row in lock["importers"][importer].get(group, {}).items()
        if not row["version"].startswith("link:")
    ]
    snapshots, registry = set(), {}
    while pending:
        name, version = pending.pop()
        key = f"{name}@{version}"
        if key not in lock["snapshots"]:
            key = version
        if key in snapshots:
            continue
        snapshot = lock["snapshots"][key]
        snapshots.add(key)
        if key == WA_KEY:
            require(
                lock["packages"][key]["resolution"]
                == {"gitHosted": True, "integrity": WA_SRI, "tarball": WA_URL}
                and snapshot == {},
                "Preimagen Git de wa-sqlite inesperada.",
            )
            continue
        identity, url, integrity = package_resolution(lock, key)
        registry[identity] = {"url": url, "integrity": integrity}
        for group in RUNTIME_GROUPS:
            pending.extend(snapshot.get(group, {}).items())
    return projects, snapshots, registry


def select_source(archive, prefix, projects, patches):
    files, total = {}, 0
    scope = tuple(f"{project}/" for project in sorted(projects - {"."})) + SOURCE_PREFIXES
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as source:
        for member in source:
            name = member.name.removeprefix(prefix)
            if member.isdir() or name not in ROOT_FILES | patches and not name.startswith(scope):
                continue
            if any(part.startswith(".env") or part == ".git" for part in name.split("/")):
                continue
            require(
                member.name == prefix + name
                and safe(name)
                and name not in files
                and member.isfile()
                and 0 <= member.size <= 16 * 1024**2,
                f"Entrada servidor no regular: {name}",
            )
            total += member.size
            require(
                total <= MAX_TOTAL and len(files) < 30000,
                "Tamaño del árbol servidor fuera del límite.",
            )
            raw = source.extractfile(member).read(member.size + 1)
            require(len(raw) == member.size, "Fuente del servidor truncada.")
            files[name] = (raw, 0o755 if member.mode & 0o111 else 0o644)
    require(
        {f"{project}/package.json" for project in projects - {"."}} | ROOT_FILES | patches
        <= files.keys(),
        "Faltan manifiestos, configuración o parches originales.",
    )
    return files


def version_overlay(workspace):
    for group, changes in (
        (
            workspace["catalog"],
            {
                "@modelcontextprotocol/node": ("2.0.0", "2.1.0"),
                "@modelcontextprotocol/sdk": ("1.31.0", "1.32.0"),
                "@types/markdown-it": ("^13.0.2", "14.1.2"),
                "markdown-it": ("^13.0.2", "14.3.2"),
                "qs": ("6.16.0", "6.16.1"),
            },
        ),
        (
            workspace["overrides"],
            {
                "multer": ("^2.4.0", "2.5.0"),
                "body-parser": ("2.3.0", "2.4.0"),
                "follow-redirects": ("1.16.1", "1.16.2"),
            },
        ),
        (workspace["catalogs"]["frontend"], {"vue-i18n": ("^11.4.4", "11.4.14")}),
        (workspace["catalogs"]["undici-v6"], {"undici": ("^6.28.1", "6.29.1")}),
        (workspace["catalogs"]["undici-v7"], {"undici": ("^7.29.1", "7.30.1")}),
    ):
        for name, (before, after) in changes.items():
            require(group[name] == before, f"Preimagen de versión distinta: {name}")
            group[name] = after
    overrides = {
        "@microsoft/agents-hosting@1.6.1>@azure/msal-node": "7.1.0",
        "@microsoft/teams.apps@2.0.16>@azure/msal-node": "7.1.0",
        "snowflake-sdk@3.3.0>toml": "5.0.1",
        "markdown-it@<14.3.2": "14.3.2",
        "moment@<2.31.0": "2.31.0",
    }
    require(not overrides.keys() & workspace["overrides"].keys(), "Override servidor duplicado.")
    workspace["overrides"].update(overrides)


def encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def project_material(archive_path, materials):
    pinned = INPUTS.read_bytes()
    require(sha(pinned) == INPUTS_SHA, "Inventario de remediación distinto.")
    release = json.loads(pinned)["release"]
    record = release["source_archive"]
    archive = read_pinned(archive_path, record["bytes"], record["sha256"])
    prefix = f"n8n-{release['commit']}/"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        raw_lock = source.extractfile(prefix + "pnpm-lock.yaml").read()
        raw_workspace = source.extractfile(prefix + "pnpm-workspace.yaml").read()
    _, lock = yaml_documents(raw_lock)
    (workspace,) = yaml_documents(raw_workspace)
    runtime_projects, runtime_snapshots, runtime = registry_plan(
        lock, {SERVER, RUNNER}, RUNTIME_GROUPS
    )
    projects, snapshots, registry = registry_plan(lock, {SERVER, RUNNER, "."}, BUILD_GROUPS)
    require(
        (
            len(runtime_projects),
            len(runtime_snapshots),
            len(runtime),
            len(projects),
            len(snapshots),
            len(registry),
        )
        == (51, 1898, 1889, 80, 3520, 3478)
        and WA_KEY in snapshots
        and WA_KEY not in runtime_snapshots
        and runtime_snapshots <= snapshots,
        "Cobertura del lock original distinta.",
    )
    projected, workspace = project_lock(lock, workspace, projects - {"."}, snapshots)
    del projected["packages"][WA_KEY]
    del projected["snapshots"][WA_KEY]
    del projected["importers"][EDITOR]["devDependencies"]["wa-sqlite"]
    version_overlay(workspace)
    files = select_source(archive, prefix, projects, set(workspace["patchedDependencies"].values()))
    package_pin = json.loads((materials / "wa-sqlite-materials-receipt.json").read_bytes())[
        "outputs"
    ][WA_LEAF]
    wa = read_pinned(materials / WA_LEAF, package_pin["bytes"], package_pin["sha256"])
    require(sri(wa) == package_pin["integrity"], "Paquete local wa-sqlite alterado.")
    files[f"vendor/{WA_LEAF}"] = (wa, 0o644)
    changes = {
        f"{EDITOR}/package.json": (
            "devDependencies",
            "wa-sqlite",
            "github:rhashimoto/wa-sqlite#779219540f66cecaa159da32b3b8936697ba10a7",
            f"file:../../../vendor/{WA_LEAF}",
        ),
        f"{RUNNER}/package.json": ("dependencies", "moment", None, "2.31.0"),
        f"{SERVER}/package.json": ("dependencies", "multer", "^2.4.0", "2.5.0"),
    }
    for name, (group, dependency, before, after) in changes.items():
        package = json.loads(files[name][0])
        require(package[group].get(dependency) == before, f"Manifiesto de origen distinto: {name}")
        package[group][dependency] = after
        files[name] = (
            (json.dumps(package, ensure_ascii=False, indent="\t") + "\n").encode(),
            0o644,
        )
    key = RELATIVE.as_posix()
    files[key] = (derive(files[key][0], read_flatted(PATCH, PATCH_SHA)), 0o644)
    files["pnpm-workspace.yaml"] = (render_yaml([workspace]), 0o644)
    files["pnpm-lock.yaml"] = (render_lock(raw_lock, projected), 0o644)
    require(
        WA_URL.encode() not in files["pnpm-lock.yaml"][0],
        "Resolución Git quedó en la entrada de resolución.",
    )

    known = {}
    for name, expected in V5_PINS.items():
        raw = (V5 / name).read_bytes()
        require(sha(raw) == expected, "Recibo anterior del store distinto.")
        for row in json.loads(raw)["tarballs"]:
            known[row["identity"]] = row["integrity"]
    reusable = {k: v for k, v in registry.items() if known.get(k) == v["integrity"]}
    require(len(reusable) == 1448, "Delta de reutilización del store distinto.")
    plan = {
        "commit": release["commit"],
        "original_tar_sha256": record["sha256"],
        "original_lock_sha256": sha(raw_lock),
        "resolver_seed_lock_sha256": sha(files["pnpm-lock.yaml"][0]),
        "build_workspaces": sorted(projects),
        "runtime_workspaces": sorted(runtime_projects),
        "baseline_runtime_registry": len(runtime),
        "baseline_build_registry": len(registry),
        "baseline_build_registry_missing_from_v5": {
            k: v for k, v in registry.items() if k not in reusable
        },
        "baseline_build_registry_reusable_from_v5": sorted(reusable),
        "build_scripts": {
            p: json.loads(files[f"{p}/package.json"][0]).get("scripts", {}).get("build")
            for p in sorted(projects - {"."})
        },
        "wa_sqlite_baseline_only": True,
        "flatted_original_sha256": SOURCE_SHA,
        "pnpm_resolution_final": False,
        "built": False,
        "production_approved": False,
    }
    files["server-build-inputs.json"] = (encode(plan), 0o644)
    resolver_names = (
        {f"{p}/package.json" for p in projects - {"."}}
        | set(workspace["patchedDependencies"].values())
        | {
            "package.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            f"vendor/{WA_LEAF}",
            "server-build-inputs.json",
        }
    )
    return files, {name: files[name] for name in resolver_names}


def add_manifest(files):
    listing = "".join(
        f"{sha(data)}  {path}\n" for path, (data, _) in sorted(files.items())
    ).encode()
    return tar_gz({**files, "SHA256SUMS": (listing, 0o644)}), sha(listing)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--materials", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()
    source, resolver = project_material(args.archive, args.materials)
    outputs, receipt = {}, {}
    for kind, files in (("server-source-linux-x64", source), ("server-lock-resolver", resolver)):
        raw, manifest_sha = add_manifest(files)
        name = kind + ".tar.gz"
        outputs[name] = raw
        receipt[name] = {
            "sha256": sha(raw),
            "bytes": len(raw),
            "files": len(files) + 1,
            "manifest_sha256": manifest_sha,
        }
    if args.write:
        outputs["server-source-receipt.json"] = encode(receipt)
        require(
            not any(
                (args.materials / name).exists() or (args.materials / name).is_symlink()
                for name in outputs
            ),
            "Salida servidor ya existente.",
        )
        for name, raw in outputs.items():
            with (args.materials / name).open("xb") as stream:
                stream.write(raw)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
