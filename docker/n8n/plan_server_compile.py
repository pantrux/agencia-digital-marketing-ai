"""Deriva el orden y los comandos sin Turbo para el editor y el CLI de n8n."""

import argparse
import hashlib
import json
import shlex
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / ".tools/backup-source-build/n8n-server-source-v1"
ORDER = "cef3944424236246b52c0684a80bb611929016c93e4f4c975177bf8bb29ec868"
CONTEXT = "2dfc977abe83650bdf8756e43c876265c18254d9151b3d9830e9c794604f5aaa"
EDITOR = "packages/frontend/editor-ui"
CLI = "packages/cli"


def pinned(path, digest):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError(f"Pin distinto: {path.name}")


def split(command):
    return [shlex.split(segment) for segment in command.split(" && ")]


def expand(command, scripts, generators, depth=0):
    if depth > 4:
        raise ValueError("Expansión de scripts cíclica")
    steps = []
    for argv in split(command):
        if argv[0] == "pnpm":
            args = argv[1:]
            if args[0] == "run":
                args = args[1:]
            name, *parameters = args
            if name in scripts:
                nested = expand(scripts[name], scripts, generators, depth + 1)
                nested[-1]["argv"].extend(parameters)
                steps.extend(nested)
            elif name in generators:
                steps.append(
                    {
                        "argv": [
                            "node",
                            "$WORKSPACE/packages/core/" + generators[name].removeprefix("./"),
                            *parameters,
                        ],
                        "env": {},
                    }
                )
            else:
                raise ValueError(f"Script sin contrato: {name}")
        else:
            if argv[0] == "cross-env":
                argv = argv[1:]
            env = {}
            while "=" in argv[0]:
                key, value = argv.pop(0).split("=", 1)
                env[key] = value
            steps.append({"argv": argv, "env": env})
    return steps


def closure(commands, target):
    seen = set()
    pending = [target]
    while pending:
        path = pending.pop()
        if path not in seen:
            seen.add(path)
            pending.extend(commands[path]["requires"])
    return seen


def plan():
    order = ROOT / "build-order-v1.json"
    pinned(order, ORDER)
    data = json.loads(order.read_bytes())
    if data["source_context_sha256"] != CONTEXT:
        raise ValueError("Orden no corresponde al contexto")
    context = ROOT / "server-lock-resolver.tar.gz"
    pinned(context, CONTEXT)
    names = data["ordered_workspaces"]
    commands = data["commands"]
    editor = closure(commands, EDITOR)
    cli = closure(commands, CLI)
    with tarfile.open(context, "r:gz") as tar:
        manifests = {name: json.load(tar.extractfile(name + "/package.json")) for name in names}
    generators = manifests["packages/core"]["bin"]
    stages = []
    before = set()
    for name in names:
        row = commands[name]
        manifest = manifests[name]
        command = row["script"]
        if not set(row["requires"]) <= before or command != manifest.get("scripts", {}).get(
            "build"
        ):
            raise ValueError(f"Orden o script distinto: {name}")
        before.add(name)
        if name in cli and command is not None:
            steps = expand(command, manifest["scripts"], generators)
            stages.append(
                {
                    "cwd": name,
                    "package": manifest["name"],
                    "editor_closure": name in editor,
                    "commands": steps,
                }
            )
    if (len(editor), sum(x["editor_closure"] for x in stages), len(cli), len(stages)) != (
        44,
        33,
        78,
        66,
    ):
        raise ValueError("Clausura inesperada")
    return {
        "schema": 1,
        "order_sha256": ORDER,
        "manifests_sha256": CONTEXT,
        "editor_workspaces": len(editor),
        "cli_workspaces": len(cli),
        "editor_commands": sum(x["editor_closure"] for x in stages),
        "cli_commands": len(stages),
        "registry_network": False,
        "pnpm_lifecycle_scripts": False,
        "unset_build_environment": [
            "RELEASE",
            "SENTRY_AUTH_TOKEN",
            "CODECOV_TOKEN",
            "BUILD_WITH_COVERAGE",
        ],
        "runtime_executed": False,
        "stages": stages,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = plan()
    raw = (
        json.dumps(result, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode()
    if args.output:
        with args.output.open("xb") as stream:
            stream.write(raw)
    print(
        json.dumps(
            {
                **{
                    key: result[key]
                    for key in (
                        "editor_workspaces",
                        "cli_workspaces",
                        "editor_commands",
                        "cli_commands",
                    )
                },
                "plan_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
