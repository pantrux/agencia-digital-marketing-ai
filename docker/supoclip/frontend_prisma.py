"""Proyecta Prisma 6.19.3 sobre las cuatro preimágenes fijadas de SupoClip."""

import hashlib
import re

PINS = {
    "package.json": "0974944cad5c8463778d9d9dd732b551b6bb5216a70f7c4bb76db04035d5bf98",
    "prisma/schema.prisma": "738a75ccd9c70612f82582075fa2a0efc247a4cebb68eef56f1a87958e830615",
    "src/lib/prisma.ts": "16494bbc6d1dee8702e49317dae283c41bab9f575070a8e0b9fff302f835ec7e",
    "e2e/global-setup.ts": "8bcaaebac92e930c6fc7a74ed5a54f5c9eaf7d54d560f4d5eae4d5be26015f19",
}


def replace(raw, before, after):
    if raw.count(before) != 1:
        raise ValueError("La sustitución Prisma requiere una preimagen única.")
    return raw.replace(before, after, 1)


def project(sources):
    """Devuelve sólo las cuatro salidas. Los SQL de migración quedan fuera del parche."""
    if set(sources) != set(PINS) or any(
        hashlib.sha256(sources[name]).hexdigest() != pin for name, pin in PINS.items()
    ):
        raise ValueError("Las preimágenes Prisma no coinciden con SupoClip fijado.")
    files = sources.copy()
    files["package.json"] = replace(
        files["package.json"],
        b'    "@prisma/client": "^6.19.2",\n',
        b'    "@prisma/adapter-pg": "6.19.3",\n    "@prisma/client": "6.19.3",\n',
    )
    files["package.json"] = replace(
        files["package.json"], b'"prisma": "^6.19.2"', b'"prisma": "6.19.3"'
    )
    name = "prisma/schema.prisma"
    before = re.findall(rb"^  binaryTargets = \[[^\n]+\]$", files[name], flags=re.MULTILINE)
    if len(before) != 1:
        raise ValueError("El generador Prisma esperado cambió.")
    files[name] = replace(files[name], before[0], b'  engineType    = "client"')

    name = "src/lib/prisma.ts"
    files[name] = b'import { PrismaPg } from "@prisma/adapter-pg";\n' + files[name]
    files[name] = replace(
        files[name],
        b"const prisma = globalForPrisma.prisma ?? new PrismaClient();",
        b"""function createClient() {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("DATABASE_URL is required for the SupoClip PostgreSQL client.");
  }
  return new PrismaClient({ adapter: new PrismaPg({ connectionString }) });
}

const prisma = globalForPrisma.prisma ?? createClient();""",
    )

    name = "e2e/global-setup.ts"
    files[name] = replace(
        files[name],
        b'import { PrismaClient } from "../src/generated/prisma";',
        b'import { PrismaPg } from "@prisma/adapter-pg";\n'
        b'import { PrismaClient } from "../src/generated/prisma";',
    )
    files[name] = replace(
        files[name],
        b"  const prisma = new PrismaClient();",
        b"""  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("An explicit disposable DATABASE_URL is required for SupoClip e2e.");
  }
  const prisma = new PrismaClient({ adapter: new PrismaPg({ connectionString }) });""",
    )
    return files
