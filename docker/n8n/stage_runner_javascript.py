"""Proyecta el código y el lock verificados de n8n para construir su runner JavaScript."""

import argparse
import io
import json
import posixpath
import re
import tarfile
from pathlib import Path

import yaml
from prepare_runner_urllib3 import INPUTS, INPUTS_SHA, read_pinned, require, sha

RUNNER = "packages/@n8n/task-runner"
MOMENT = "2.30.1"
MOMENT_SRI = (
    "sha512-uEmtNhbDOrWPFS+hdjFCBfy9f2YoyzRpwcl+DqpC6taX21FzsTLQVbMV/"
    "W7PzNSX6x/bhC1zA3c2UQ5NzH6how=="
)
FIXES = {"follow-redirects": ("1.16.1", "1.16.2"), "undici": ("7.29.1", "7.30.1")}
RUNTIME_GROUPS = ("dependencies", "optionalDependencies")
BUILD_GROUPS = (*RUNTIME_GROUPS, "devDependencies")
ROOT_FILES = {
    "package.json",
    "pnpm-workspace.yaml",
    "pnpm-lock.yaml",
    "LICENSE.md",
    "LICENSE_EE.md",
}
COMPONENT = re.compile(r"[A-Za-z0-9_.@+ -]+", re.ASCII)


def yaml_documents(raw):
    return list(yaml.load_all(raw, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)))


def render_yaml(documents):
    return yaml.dump_all(
        documents,
        Dumper=getattr(yaml, "CSafeDumper", yaml.SafeDumper),
        sort_keys=False,
        allow_unicode=True,
        width=200,
    ).encode()


def render_lock(original, project):
    # El CLI 12.4.2 reconoce el bootstrap por el primer marcador explícito.
    require(
        original.startswith(b"---\n") and original.count(b"\n---\n") == 1,
        "Framing original del lock distinto.",
    )
    bootstrap, _ = original.split(b"\n---\n", 1)
    return bootstrap + b"\n---\n" + render_yaml([project])


def safe_path(name):
    parts = name.split("/")
    return all(
        COMPONENT.fullmatch(part)
        and part not in {".", ".."}
        and not part.endswith((".", " "))
        and not re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0], re.I)
        for part in parts
    )


def linked_importer(importer, reference):
    path = posixpath.normpath(posixpath.join(importer, reference.removeprefix("link:")))
    require(path.startswith("packages/") and safe_path(path), "Enlace workspace fuera del cierre.")
    return path


def workspace_projects(lock, groups):
    importers = lock["importers"]
    pending = [RUNNER]
    projects = set()
    while pending:
        path = pending.pop()
        if path in projects:
            continue
        require(path in importers, "Enlace workspace sin importer.")
        projects.add(path)
        for group in groups:
            for dependency in importers[path].get(group, {}).values():
                reference = dependency["version"]
                if reference.startswith("link:"):
                    pending.append(linked_importer(path, reference))
    return projects


def package_key(snapshot):
    return snapshot.split("(", 1)[0]


def package_resolution(lock, snapshot):
    identity = package_key(snapshot)
    row = lock["packages"][identity]
    resolution = row["resolution"]
    require(
        set(resolution) <= {"integrity", "tarball"}
        and re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", resolution.get("integrity", "")),
        "El cierre requiere una resolución que no es un tarball con SRI SHA-512.",
    )
    name, version = identity.rsplit("@", 1)
    require(
        re.fullmatch(r"(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_.-]+", name)
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?", version),
        "Identidad npm fuera del contrato del registro.",
    )
    canonical = f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
    require(
        resolution.get("tarball", canonical) == canonical,
        "Resolución fuera del registro npm fijado.",
    )
    return identity, canonical, resolution["integrity"]


def external_closure(lock, projects, groups):
    snapshots = lock["snapshots"]
    pending = []
    for importer in projects:
        for group in groups:
            for name, dep in lock["importers"][importer].get(group, {}).items():
                if not dep["version"].startswith("link:"):
                    pending.append((name, dep["version"]))
    seen = set()
    artifacts = {}
    while pending:
        name, version = pending.pop()
        key = f"{name}@{version}"
        if key not in snapshots and version in snapshots:
            key = version
        require(key in snapshots, f"Snapshot no resoluble: {key}")
        if key in seen:
            continue
        seen.add(key)
        identity, url, integrity = package_resolution(lock, key)
        artifacts[identity] = {"url": url, "integrity": integrity}
        for group in RUNTIME_GROUPS:
            pending.extend(snapshots[key].get(group, {}).items())
    return seen, artifacts


def replace_references(value, name, before, after):
    old_package = re.compile(
        rf"(?<![A-Za-z0-9_.@/-]){re.escape(name)}@{re.escape(before)}(?=\(|\)|$)"
    )
    old_version = re.compile(rf"^{re.escape(before)}(?=\(|$)")
    if isinstance(value, str):
        return old_package.sub(f"{name}@{after}", value)
    if isinstance(value, list):
        return [replace_references(item, name, before, after) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        new_key = old_package.sub(f"{name}@{after}", key)
        require(new_key not in result, "Colisión al aplicar actualización npm.")
        if key == name and isinstance(item, str):
            item = old_version.sub(after, item)
        elif key == name and isinstance(item, dict) and "version" in item:
            item = {**item, "version": old_version.sub(after, item["version"])}
        result[new_key] = replace_references(item, name, before, after)
    return result


def repair_lock(lock, workspace, pins):
    for name, (before, after) in FIXES.items():
        old_key, new_key = f"{name}@{before}", f"{name}@{after}"
        require(
            old_key in lock["packages"] and new_key not in lock["packages"],
            "Preimagen npm distinta.",
        )
        (row,) = [pin for pin in pins if pin["name"] == name and pin["version"] == after]
        require(row["dependencies"] == {}, "La versión de reemplazo requiere dependencias nuevas.")
        # Ambos tarballs de follow-redirects declaran debug opcional en
        # peerDependenciesMeta. pnpm lo representa además como peer '*'.
        optional_debug = {"debug": {"optional": True}} if name == "follow-redirects" else {}
        peers = {"debug": "*"} if optional_debug else {}
        require(
            row["peer_dependencies"] == {}
            and lock["packages"][old_key].get("peerDependencies", {}) == peers
            and lock["packages"][old_key].get("peerDependenciesMeta", {}) == optional_debug
            and lock["packages"][old_key].get("engines", {}) == row["engines"],
            "La versión de reemplazo cambia el contrato del snapshot.",
        )
        lock = replace_references(lock, name, before, after)
        lock["packages"][new_key]["resolution"] = {"integrity": row["integrity"]}
        workspace = replace_references(workspace, name, before, after)
    moment = f"moment@{MOMENT}"
    require(
        moment not in lock["snapshots"] and moment not in lock["packages"],
        "La preimagen del lock ya incluye Moment.",
    )
    lock["packages"][moment] = {"resolution": {"integrity": MOMENT_SRI}, "engines": {"node": "*"}}
    lock["snapshots"][moment] = {}
    deps = lock["importers"][RUNNER]["dependencies"]
    require("moment" not in deps, "El runner original ya declara Moment.")
    deps["moment"] = {"specifier": MOMENT, "version": MOMENT}
    return lock, workspace


def project_lock(lock, workspace, projects, snapshots):
    identities = {package_key(key) for key in snapshots}
    selected_patches = {
        name: digest
        for name, digest in lock["patchedDependencies"].items()
        if any(f"(patch_hash={digest})" in key for key in snapshots)
    }
    workspace = {
        **workspace,
        "packages": sorted(projects),
        "patchedDependencies": {
            name: path
            for name, path in workspace["patchedDependencies"].items()
            if name in selected_patches
        },
    }
    require(
        workspace["patchedDependencies"].keys() == selected_patches.keys(),
        "Patch sin archivo de workspace.",
    )
    projected = {
        **lock,
        "importers": {
            key: value for key, value in lock["importers"].items() if key in projects | {"."}
        },
        "packages": {key: value for key, value in lock["packages"].items() if key in identities},
        "snapshots": {key: value for key, value in lock["snapshots"].items() if key in snapshots},
        "patchedDependencies": selected_patches,
    }
    return projected, workspace


def selected_files(raw, prefix, projects, patches):
    outputs = {}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as archive:
        for member in archive:
            name = member.name.removeprefix(prefix)
            if (
                member.isdir()
                or any(part == ".git" or part.startswith(".env") for part in name.split("/"))
                or (
                    name not in ROOT_FILES
                    and name not in patches
                    and not any(name.startswith(f"{project}/") for project in projects)
                )
            ):
                continue
            require(
                member.name == prefix + name
                and safe_path(name)
                and name.casefold() not in seen
                and member.isfile()
                and 0 <= member.size <= 16 * 1024**2,
                f"Miembro del contexto no admisible: {name}",
            )
            seen.add(name.casefold())
            with archive.extractfile(member) as stream:
                content = stream.read(member.size + 1)
            require(len(content) == member.size, "Archivo fuente truncado.")
            outputs[name] = content
    require(
        {f"{project}/package.json" for project in projects} <= outputs.keys(),
        "Falta un workspace del cierre.",
    )
    return outputs


def stage(archive_path, output):
    raw = INPUTS.read_bytes()
    require(sha(raw) == INPUTS_SHA, "Inventario de entrada distinto del pin.")
    inputs = json.loads(raw)
    release = inputs["release"]
    pin = release["source_archive"]
    archive = read_pinned(archive_path, pin["bytes"], pin["sha256"])
    prefix = f"n8n-{release['commit']}/"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        raw_lock = source.extractfile(prefix + "pnpm-lock.yaml").read()
        raw_workspace = source.extractfile(prefix + "pnpm-workspace.yaml").read()
    _, original = yaml_documents(raw_lock)
    (workspace,) = yaml_documents(raw_workspace)
    lock, workspace = repair_lock(original, workspace, inputs["npm_candidates"])
    runtime_projects = workspace_projects(lock, RUNTIME_GROUPS)
    build_projects = workspace_projects(lock, BUILD_GROUPS)
    runtime_snapshots, runtime = external_closure(lock, runtime_projects, RUNTIME_GROUPS)
    build_snapshots, build = external_closure(lock, build_projects | {"."}, BUILD_GROUPS)
    require(
        runtime_snapshots <= build_snapshots and runtime.keys() <= build.keys(),
        "El cierre de build pierde paquetes runtime.",
    )
    lock, workspace = project_lock(lock, workspace, build_projects, build_snapshots)
    require(
        external_closure(lock, build_projects | {"."}, BUILD_GROUPS) == (build_snapshots, build),
        "La proyección cambia el cierre de instalación.",
    )
    patches = set(workspace["patchedDependencies"].values())
    files = selected_files(archive, prefix, build_projects, patches)
    require(
        ROOT_FILES <= files.keys() and patches <= files.keys(),
        "Faltan entradas originales o patches.",
    )
    runner = json.loads(files[f"{RUNNER}/package.json"])
    require(
        runner["name"] == "@n8n/task-runner"
        and runner["version"] == "2.46.5"
        and runner["main"] == "dist/start.js",
        "Contrato de runner distinto.",
    )
    runner["dependencies"]["moment"] = MOMENT
    files[f"{RUNNER}/package.json"] = (json.dumps(runner, indent=2) + "\n").encode()
    files["pnpm-workspace.yaml"] = render_yaml([workspace])
    files["pnpm-lock.yaml"] = render_lock(raw_lock, lock)
    metadata = {
        "source_archive_sha256": pin["sha256"],
        "upstream_commit": release["commit"],
        "original_lock_sha256": sha(raw_lock),
        "derived_lock_sha256": sha(files["pnpm-lock.yaml"]),
        "runtime_projects": sorted(runtime_projects),
        "build_projects": sorted(build_projects),
        "runtime_artifacts": runtime,
        "build_artifacts": build,
        "runtime_snapshots": len(runtime_snapshots),
        "build_snapshots": len(build_snapshots),
        "upstream_patches": workspace["patchedDependencies"],
        "upstream_pnpm": release["pnpm"],
        "upstream_node": release["runtime_node"],
        "build_performed": False,
        "sca_approved": False,
    }
    files["runner-javascript-inputs.json"] = (
        json.dumps(metadata, sort_keys=True, indent=2) + "\n"
    ).encode()
    manifest = "".join(f"{sha(data)}  {name}\n" for name, data in sorted(files.items())).encode()
    output.mkdir(exist_ok=False)
    for name, data in sorted({**files, "SHA256SUMS": manifest}.items()):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    return {
        "files": len(files),
        "context_sha256sums": sha(manifest),
        "runtime_projects": len(runtime_projects),
        "build_projects": len(build_projects),
        "runtime_artifacts": len(runtime),
        "build_artifacts": len(build),
        "derived_lock_sha256": metadata["derived_lock_sha256"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(stage(args.archive, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
