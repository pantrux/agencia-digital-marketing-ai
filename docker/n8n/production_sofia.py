"""Obtiene el primer brief de Sofía para una orden de ingesta acreditada."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from production_handoff import canonical, eligible, load_profile, publish, read_regular, validate
from production_inbox import UUID, matches
from production_intake import fingerprint

BRIEF_FIELDS = {"enfoque", "alcance", "pendiente"}
LIMIT = 16384
CLI = Path("/home/jandrade/.npm-global/bin/openclaw")
JOURNAL = Path("/var/lib/agency-production-intake")
MCP = Path("/home/jandrade/.local/lib/agency-audiovisual-mcp-stage-20261114-v1")
MEDIA = Path("/mnt/producciones")
MAX_TRANSCRIPT = 4 * 1024**2


def brief_reply(run):
    result = run.get("result", run)
    meta = result["meta"]["agentMeta"]
    if run.get("status") != "ok" or (
        meta.get("provider"),
        meta.get("model"),
        meta.get("agentHarnessId"),
    ) != ("openai", "gpt-6.1-terra", "codex"):
        raise ValueError("Sofía no confirmó el modelo ni Codex")
    messages = [p["text"] for p in result["payloads"] if isinstance(p.get("text"), str)]
    if len(messages) != 1 or not 0 < len(messages[0]) <= 6000:
        raise ValueError("Respuesta de Sofía inválida")
    text = messages[0].strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text.removeprefix("```json\n").removesuffix("\n```")
    brief = json.loads(text)
    if (
        type(brief) is not dict
        or brief.keys() != BRIEF_FIELDS
        or any(type(v) is not str or not 0 < len(v.strip()) <= 1500 for v in brief.values())
    ):
        raise ValueError("Brief de Sofía inválido")
    return brief


def validate_transcript(result):
    if (
        type(result) is not dict
        or result.keys() != {"language", "duration", "text", "words"}
        or type(result["text"]) is not str
        or type(result["duration"]) not in (int, float)
        or not 0 < result["duration"] <= 600.1
        or result["language"] is not None
        and (type(result["language"]) is not str or not 1 <= len(result["language"]) <= 32)
        or type(result["words"]) is not list
        or bool(result["text"].strip()) != bool(result["words"])
    ):
        raise ValueError("Transcripción de ingesta inválida")
    previous = 0
    for word in result["words"]:
        if (
            type(word) is not dict
            or word.keys() != {"word", "start", "end"}
            or type(word["word"]) is not str
            or not word["word"].strip()
            or type(word["start"]) not in (int, float)
            or type(word["end"]) not in (int, float)
            or not previous <= word["start"] <= word["end"] <= result["duration"] + 0.1
        ):
            raise ValueError("Marcas de transcripción inválidas")
        previous = word["end"]
    return result


MCP_TRANSCRIBE = """
import asyncio,json,sys
from pathlib import Path
root=Path(sys.executable).parent.parent.parent
sys.path.insert(0,str(root/"mcp_servers"))
import call_stdio
args=json.load(sys.stdin)
settings=call_stdio.runtime_settings()
settings["AGENCY_MEDIA_PROJECTS"]=args["proyecto"]
call_stdio.runtime_settings=lambda: settings
print(json.dumps(asyncio.run(call_stdio.call("mcp-media-inspector","transcribir_audio",args)),ensure_ascii=False,allow_nan=False))
"""


def transcript_for(receipt, output):
    audio = receipt["audio"]
    if audio is None:
        return None
    order = receipt["order"]
    project = order["media"]["project_slug"]
    path = MEDIA / "proyectos" / project / "03_assets/intake-source.wav"
    expected = {"sha256": audio["sha256"], "bytes": audio["bytes"]}
    if (
        audio["path"] != str(path.relative_to(MEDIA))
        or path.resolve(strict=True) != path
        or not re.fullmatch(r"[a-f0-9]{64}", expected["sha256"])
        or fingerprint(path) != expected
    ):
        raise ValueError("El audio no coincide con el recibo de ingesta")
    record = {
        "order_id": order["order_id"],
        "source_sha256": order["media"]["sha256"],
        "audio": audio,
    }
    sidecar = output.with_suffix(".transcripcion.json")
    if sidecar.exists():
        cached = json.loads(read_regular(sidecar, MAX_TRANSCRIPT))
        if cached.keys() != record.keys() | {"result"} or any(
            cached[key] != value for key, value in record.items()
        ):
            raise ValueError("La transcripción guardada pertenece a otro medio")
        return validate_transcript(cached["result"])
    run = subprocess.run(
        [str(MCP / "venv/bin/python"), "-I", "-B", "-c", MCP_TRANSCRIBE],
        input=canonical({"proyecto": project, "ruta": audio["path"]}),
        capture_output=True,
        check=True,
        timeout=310,
    )
    if len(run.stdout) > MAX_TRANSCRIPT:
        raise ValueError("Transcripción de ingesta excede el límite")
    data = json.loads(run.stdout)
    if data.keys() != {"result"} or fingerprint(path) != expected:
        raise ValueError("El inspector no confirmó la transcripción del audio")
    result = validate_transcript(data["result"])
    raw = canonical({**record, "result": result}) + b"\n"
    if len(raw) > MAX_TRANSCRIPT:
        raise ValueError("Recibo de transcripción excede el límite")
    publish(sidecar, raw)
    return result


def start_brief(receipt, output):
    order = receipt["order"]
    duration = receipt["duration_seconds"]
    if type(duration) not in (int, float) or not 0 < duration <= 600:
        raise ValueError("Duración de ingesta inválida")
    facts = {
        "plan": order["plan_id"],
        "duracion_segundos": duration,
        "tiene_audio": receipt["audio"] is not None,
    }
    transcript = transcript_for(receipt, output)
    if transcript is not None:
        facts["transcripcion"] = {
            "idioma": transcript["language"],
            "palabras": len(transcript["words"]),
        }
    prompt = (
        "Eres Sofía. Prepara el brief preliminar de una producción de Studio. Usa los datos de "
        "la ingesta. Aún falta definir objetivo y audiencia en el portal. "
        "No llames herramientas ni inventes imágenes, "
        "voz o marca. "
        "Devuelve solamente JSON con enfoque, alcance y pendiente, cada uno como texto en español. "
        "Metadatos verificados: " + json.dumps(facts, ensure_ascii=False)
    )
    run = subprocess.run(
        [
            str(CLI),
            "agent",
            "--agent",
            "agency-account",
            "--session-id",
            order["order_id"],
            "--message",
            prompt,
            "--json",
            "--timeout",
            "70",
        ],
        capture_output=True,
        check=True,
        timeout=80,
        env=dict(os.environ, PATH="/usr/local/bin:/usr/bin:/bin"),
    )
    if len(run.stdout) > 512 * 1024:
        raise ValueError("Salida de Sofía excede el límite")
    brief = brief_reply(json.loads(run.stdout))
    summary = {
        "order_id": order["order_id"],
        "plan": order["plan_id"],
        "source_sha256": order["media"]["sha256"],
        "brief": brief,
        "estado": "pendiente_objetivo",
        "modelo": "openai/gpt-6.1-terra",
        "runtime": "codex",
    }
    publish(output, canonical(summary) + b"\n")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("profile", type=Path)
    cli.add_argument("out", type=Path)
    opts = cli.parse_args()
    profile = load_profile(opts.profile)
    if JOURNAL.resolve(strict=True) != JOURNAL or opts.out.resolve(strict=True) != opts.out:
        raise ValueError("Directorios de brief inválidos")
    for path in sorted(JOURNAL.glob("*.json")):
        output = opts.out / path.name
        if not matches(UUID, path.stem) or output.exists():
            continue
        receipt = json.loads(read_regular(path, LIMIT))
        order = receipt["order"]
        validate(order, path.stem)
        if eligible(order, profile):
            start_brief(receipt, output)
            break


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise SystemExit("Falló el turno de ingesta de Sofía") from None
