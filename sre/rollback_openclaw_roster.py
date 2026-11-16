"""Restaura por RPC las etiquetas declaradas de los agentes de Studio."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

GATEWAY = "ws://127.0.0.1:18789"
MODELS = {
    "agency-account": "terra",
    "agency-critic": "sol",
    "agency-director": "sol",
    "agency-editor": "terra",
    "agency-strategist": "sol",
}


def call(method, params=None):
    cli = Path.home() / ".npm-global/bin/openclaw"
    command = [
        str(cli),
        "gateway",
        "call",
        method,
        "--json",
        "--expect-url",
        GATEWAY,
        "--timeout",
        "6000",
    ]
    if params is not None:
        command += ["--params", json.dumps(params, ensure_ascii=False, separators=(",", ":"))]
    result = subprocess.run(
        command,
        env={**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True,
        timeout=8,
        check=True,
    )
    return json.loads(result.stdout)


def config():
    status = call("config.get")
    if status.get("valid") is not True or not isinstance(status.get("hash"), str):
        raise ValueError("Configuración OpenClaw sin revisión válida")
    return status, status["config"]["agents"]["entries"]


def confirm(expected, desired):
    for _ in range(5):
        try:
            roster = call("agents.list")["agents"]
            agents = {item["id"]: item for item in roster}
            if names(agents, expected) == expected and set(MODELS).issubset(agents):
                _, after = config()
                if after == desired:
                    return True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(0.5)
    return False


def names(rows, keys):
    return {key: rows[key]["name"] for key in keys}


def apply(payload):
    expected, previous = payload["expected"], payload["previous"]
    if not expected or set(expected) != set(previous) or not set(expected).issubset(MODELS):
        return False
    before, entries = config()
    if any(entries[name]["model"] != "openai/gpt-6.1-" + model for name, model in MODELS.items()):
        return False
    current = names(entries, expected)
    if current != previous:
        if current != expected:
            return False
        update = {
            "agents": {"entries": {agent: {"name": name} for agent, name in previous.items()}}
        }
        try:
            call(
                "config.patch",
                {
                    "raw": json.dumps(update, ensure_ascii=False),
                    "baseHash": before["hash"],
                    "note": "Reversión de etiquetas de Studio",
                },
            )
        except (subprocess.SubprocessError, ValueError):
            pass
    desired = {**entries, **{key: {**entries[key], "name": name} for key, name in previous.items()}}
    return confirm(previous, desired)


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(16385)
        result = len(raw) <= 16384 and apply(json.loads(raw))
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        result = False
    sys.exit(0 if result else 1)
