"""Recreate only the SupoClip frontend with its verified Prisma compiler image."""

import argparse
import importlib.util
import json
import os
import time
from pathlib import Path

BASE = "sha256:aec042d9f6589e5f5fad61cb5e481a57320569cda23f50bba1f2befe493d8c05"
BASE_CONFIG = "sha256:8d4eefb264d202029e60460a25ad0b58f07292748efc126ff4d40ae33b520198"
WASM = "/opt/supoclip/frontend/src/generated/prisma/query_compiler_bg.wasm"
WASM_SHA = "8ad65dab5c22d8ce70db4bcb6c8c0a5069feb9841d9154cfb59f2003c2bd0236"


def load_operator():
    path = Path(__file__).resolve().with_name("trial_operator.py")
    spec = importlib.util.spec_from_file_location("trial_operator", path)
    operator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operator)
    return operator


def replace(operator, root, image):
    need = operator.need
    plan = root / "plan.json"
    receipt = root / "containers.json"
    state = json.loads(plan.read_bytes())
    containers = operator.validate(root, state)
    before = operator.collect(root, state)
    need(
        set(state["verified_images"]["frontend"]) & {BASE, BASE_CONFIG},
        "Frontend original distinto",
    )
    need(operator.IMAGE.fullmatch(image), "Falta el ID físico de la imagen candidata")
    [base] = operator.get_json(
        "docker", "image", "inspect", state["env"]["MEDIA_SUPOCLIP_TRIAL_FRONTEND_IMAGE"]
    )
    [candidate] = operator.get_json("docker", "image", "inspect", image)
    candidate_ids = operator.image_ids(candidate)
    need(
        image in candidate_ids and not candidate_ids & {BASE, BASE_CONFIG}, "ID candidato distinto"
    )
    need(
        candidate["Os"] == "linux" and candidate["Architecture"] == "amd64",
        "Plataforma candidata distinta",
    )
    need(candidate["Config"] == base["Config"], "Configuración de imagen distinta")
    need(
        candidate["RootFS"]["Layers"][:-1] == base["RootFS"]["Layers"], "Capas de imagen distintas"
    )
    proof = operator.run(
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--entrypoint",
        "/usr/bin/node",
        image,
        "-e",
        "const fs=require('node:fs'),c=require('node:crypto'),p=process.argv[1],b=fs.readFileSync(p); new WebAssembly.Module(b); const s=fs.statSync(p); if(s.uid!==0||s.gid!==0||(s.mode&511)!==292)process.exit(1); console.log(c.createHash('sha256').update(b).digest('hex'))",
        WASM,
    ).stdout.strip()
    need(proof == WASM_SHA.encode(), "WASM candidato distinto")
    history = root / f"frontend-replacement-{time.time_ns()}"
    history.mkdir(mode=0o700)
    operator.write_json(history / "plan-before.json", state)
    operator.write_json(history / "containers-before.json", before)
    operator.write_json(history / "requested.json", {"image": image, "wasm_sha256": WASM_SHA})
    state["env"]["MEDIA_SUPOCLIP_TRIAL_FRONTEND_IMAGE"] = image
    state["verified_images"]["frontend"] = sorted(candidate_ids)
    proxy = containers["proxy"]["id"]
    operator.run("docker", "stop", "--time", "30", proxy)
    result = operator.compose(
        root,
        state,
        "up",
        "--no-start",
        "--no-deps",
        "--force-recreate",
        "--no-build",
        "--pull",
        "never",
        "frontend",
        check=False,
    )
    (history / "compose.log").write_bytes(result.stdout + result.stderr)
    after = operator.collect(root, state)
    operator.write_json(history / "containers-after-create.json", after)
    need(
        result.returncode == 0 and set(after) == set(containers),
        "Recreación incompleta. Revisar el historial del intento",
    )
    need(
        all(
            containers[name]["id"] == row["id"] for name, row in after.items() if name != "frontend"
        ),
        "Otro CID cambió",
    )
    frontend = after["frontend"]
    need(
        frontend["id"] != containers["frontend"]["id"]
        and frontend["state"] == "created"
        and frontend["image"] in candidate_ids,
        "Frontend nuevo distinto",
    )
    operator.write_json(history / "plan-after.json", state)
    operator.validate(root, state, containers=after)
    for destination, value in ((plan, state), (receipt, after)):
        stage = root / (destination.name + ".replacement")
        operator.write_json(stage, value)
        stage.replace(destination)
    operator.run("docker", "start", frontend["id"])
    operator.healthy(frontend["id"])
    operator.run("docker", "start", proxy)
    operator.run(
        "docker",
        "exec",
        proxy,
        "/usr/bin/nginx",
        "-t",
        "-e",
        "stderr",
        "-c",
        "/etc/nginx/supoclip.conf",
    )
    health = operator.run(
        "curl",
        "--noproxy",
        "*",
        "--fail",
        "--silent",
        "--max-time",
        "5",
        f"http://{operator.HOST}:{operator.PORT}/healthz",
    ).stdout
    need(health == b"ok\n", "Origen HTTP no saludable tras la sustitución")
    result = {
        "frontend": operator.collect(root, state)["frontend"],
        "retained_proxy": proxy,
        "http": 200,
        "signup_pending": True,
    }
    operator.write_json(history / "receipt.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    operator = load_operator()
    operator.need(
        os.geteuid() == 0
        and not os.environ.get("DOCKER_HOST")
        and not os.environ.get("DOCKER_CONTEXT"),
        "Requiere root y Docker local",
    )
    operator.need(args.root.resolve(strict=True) == args.root, "Raíz no física")
    os.umask(0o077)
    print(json.dumps(replace(operator, args.root, args.image), sort_keys=True))
