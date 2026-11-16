"""Aplica y coteja las migraciones de Studio en el emulador local de D1."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
WRANGLER_VERSION = "4.162.0"
FIXTURE_ID = "00000000-0000-4000-8000-000000000001"


def check() -> None:
    wrangler = shutil.which("wrangler")
    if wrangler is None:
        raise ValueError(f"Wrangler {WRANGLER_VERSION} debe estar disponible en PATH.")

    sample = ROOT / "cloudflare-saas/studio-api-worker/wrangler.example.jsonc"
    configuration = json.loads(sample.read_text(encoding="utf-8"))
    databases = configuration["d1_databases"]
    if len(databases) != 1 or databases[0] != {
        "binding": "DB",
        "database_name": "studio-saas-db",
        "database_id": FIXTURE_ID,
        "migrations_dir": "../migrations",
    }:
        raise ValueError("La configuración de ejemplo debe identificar la base D1 de prueba.")

    migrations = sorted((sample.parent.parent / "migrations").glob("*.sql"))
    if not migrations:
        raise ValueError("Faltan migraciones SQL de Studio.")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("CLOUDFLARE_", "CF_", "WRANGLER_"))
    }
    env.update(CI="true", FORCE_COLOR="0", WRANGLER_SEND_METRICS="false")

    with TemporaryDirectory(prefix="studio-d1-ci-") as directory:
        workspace = Path(directory)
        (workspace / "migrations").mkdir()
        for migration in migrations:
            shutil.copyfile(migration, workspace / "migrations" / migration.name)
        fixture = {
            "name": "studio-d1-local-ci",
            "compatibility_date": configuration["compatibility_date"],
            "d1_databases": [{**databases[0], "migrations_dir": "./migrations"}],
        }
        (workspace / "wrangler.json").write_text(json.dumps(fixture), encoding="utf-8")

        def run(*arguments: str) -> str:
            process = subprocess.run(
                [wrangler, *arguments],
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                check=False,
            )
            if process.returncode != 0:
                detail = (process.stderr or process.stdout).strip()[-2400:]
                raise ValueError(f"Falló {' '.join(arguments[:3])}. {detail}")
            return process.stdout

        if run("--version").strip() != WRANGLER_VERSION:
            raise ValueError(f"El control requiere exactamente Wrangler {WRANGLER_VERSION}.")
        options = ("DB", "--local", "--persist-to", "./state", "--config", "./wrangler.json")

        def inspect() -> list[list[dict]]:
            sql = (
                "SELECT id,name,applied_at FROM d1_migrations ORDER BY id; "
                "PRAGMA quick_check; PRAGMA foreign_key_check;"
            )
            reply = json.loads(run("d1", "execute", *options, "--command", sql, "--json"))
            if len(reply) != 3 or not all(item.get("success") is True for item in reply):
                raise ValueError("Wrangler no confirmó las tres consultas de comprobación.")
            return [item["results"] for item in reply]

        run("d1", "migrations", "apply", *options)
        ledger, integrity, foreign_keys = inspect()
        if [row["name"] for row in ledger] != [migration.name for migration in migrations]:
            raise ValueError("El registro de D1 no coincide con todas las migraciones ordenadas.")
        if integrity != [{"quick_check": "ok"}] or foreign_keys:
            raise ValueError("La base local no pasó integridad o el cotejo de claves foráneas.")
        run("d1", "migrations", "apply", *options)
        if inspect() != [ledger, integrity, foreign_keys]:
            raise ValueError("La segunda aplicación modificó el registro o la integridad de D1.")
    print(f"D1 local con Wrangler {WRANGLER_VERSION}: {len(migrations)} migraciones cotejadas.")


if __name__ == "__main__":
    try:
        check()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error)) from None
