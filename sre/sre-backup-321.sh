#!/usr/bin/env bash
# El orquestador incorpora el volcado de capture-postgres-backup.sh como postgres-apps.sql.zst.
# Los productores completan los otros tres archivos y SHA256SUMS antes de invocar la copia.
# El orquestador conserva el recibo de un destino antes de invocar el siguiente.
set +x
set -euo pipefail
umask 077

exec /usr/bin/python3 -I -B - "$@" <<'PYTHON'
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

USO = (
    "Uso: sre-backup-321.sh nas|r2 STAGING RUN_ID RESTIC_BIN RESTIC_SHA256 "
    "REPOSITORIO REPO_ID DIRECTORIO_RECIBO_NUEVO [--d1-clock|--openclaw-clock]\n"
    "Entorno: RESTIC_PASSWORD_FILE y, para r2, R2_PLATFORM_ACCESS_KEY_ID "
    "y R2_PLATFORM_SECRET_ACCESS_KEY.\n"
    "Si NFS proyecta 0777, BACKUP_NAS_DSM_RECEIPT_FILE apunta a un JSON Root:Root "
    '0600 sin enlaces con schema=1, path físico DSM, inode, uid, gid y mode="0700".'
)
RAIZ = Path("/var/lib/media-platform")
ARCHIVOS = ("postgres-apps.sql.zst", "postgres-immich.sql.zst", "d1.sql.zst", "openclaw.tar.zst")
ALCANCES = {
    "--d1-clock": (("d1.sql.zst",), "agency-d1-clock"),
    "--openclaw-clock": (("openclaw.tar.zst",), "agency-openclaw-clock"),
}
ENV_LOCAL = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
SHA256 = re.compile(r"[a-f0-9]{64}")
MODOS_BINARIO = (0o500, 0o555, 0o700, 0o755)
MODOS_SECRETO = (0o400, 0o600)


def exigir(condicion, codigo):
    if not condicion:
        raise ValueError(codigo)


def canonica(ruta):
    ruta = Path(ruta)
    exigir(ruta.is_absolute() and ruta.resolve(strict=True) == ruta, "RUTA_NO_CANONICA")
    return ruta


def directorio_privado(ruta):
    ruta = canonica(ruta)
    exigir(ruta.is_relative_to(RAIZ), "DIRECTORIO_FUERA_DE_STAGING")
    actual = ruta
    while True:
        info = actual.lstat()
        exigir(stat.S_ISDIR(info.st_mode) and (info.st_uid, info.st_gid) == (0, 0)
                and stat.S_IMODE(info.st_mode) == 0o700, "DIRECTORIO_NO_PRIVADO")
        if actual == RAIZ:
            return ruta
        actual = actual.parent


def sello(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
            info.st_gid, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


def archivo_privado(ruta, modos=(0o600,)):
    info = ruta.lstat()
    exigir(stat.S_ISREG(info.st_mode) and (info.st_uid, info.st_gid) == (0, 0)
            and stat.S_IMODE(info.st_mode) in modos and info.st_nlink == 1
            and info.st_size > 0, "ARCHIVO_NO_PRIVADO")
    return sello(info)


def sha_archivo(ruta, esperado):
    fd = os.open(ruta, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as entrada:
        exigir(sello(os.fstat(entrada.fileno())) == esperado, "ARCHIVO_CAMBIO")
        digest = hashlib.file_digest(entrada, "sha256").hexdigest()
        exigir(sello(os.fstat(entrada.fileno())) == sello(ruta.lstat()) == esperado,
                "ARCHIVO_CAMBIO")
    return digest


def verificar_staging(staging, archivos):
    entradas = (*archivos, "SHA256SUMS")
    exigir(staging != RAIZ and {ruta.name for ruta in staging.iterdir()} == set(entradas),
            "STAGING_INCOMPLETO_O_CON_EXTRAS")
    identidades = {nombre: archivo_privado(staging / nombre) for nombre in entradas}
    ruta_manifiesto = staging / "SHA256SUMS"
    fd = os.open(ruta_manifiesto, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as archivo:
        exigir(sello(os.fstat(archivo.fileno())) == identidades["SHA256SUMS"], "MANIFIESTO_CAMBIO")
        manifiesto = archivo.read(1025)
        exigir(sello(os.fstat(archivo.fileno())) == sello(ruta_manifiesto.lstat())
                == identidades["SHA256SUMS"], "MANIFIESTO_CAMBIO")
    exigir(len(manifiesto) <= 1024, "MANIFIESTO_EXCESIVO")
    sumas = {}
    for linea in manifiesto.splitlines(keepends=True):
        registro = re.fullmatch(rb"([a-f0-9]{64})  ([a-z0-9.-]+)\n", linea)
        exigir(registro is not None, "MANIFIESTO_INVALIDO")
        digest, nombre = (campo.decode("ascii") for campo in registro.groups())
        exigir(nombre in archivos and nombre not in sumas, "MANIFIESTO_NO_EXACTO")
        sumas[nombre] = digest
    exigir(set(sumas) == set(archivos), "MANIFIESTO_INCOMPLETO")
    for nombre in archivos:
        exigir(sha_archivo(staging / nombre, identidades[nombre]) == sumas[nombre], "SHA_STAGING")
    return identidades, hashlib.sha256(manifiesto).hexdigest()


def staging_estable(staging, identidades):
    exigir({ruta.name for ruta in staging.iterdir()} == set(identidades)
            and all(archivo_privado(staging / nombre) == identidad
                    for nombre, identidad in identidades.items()),
            "STAGING_CAMBIO")


def nas_acreditado(repositorio):
    ruta = canonica(repositorio)
    info = ruta.lstat()
    modo = stat.S_IMODE(info.st_mode)
    exigir(stat.S_ISDIR(info.st_mode) and (info.st_uid, info.st_gid) == (1024, 100)
            and modo in (0o700, 0o777), "REPOSITORIO_NAS_NO_PRIVADO")
    montaje = subprocess.check_output(
        ["/usr/bin/findmnt", "-rn", "-T", str(ruta), "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"],
        env=ENV_LOCAL, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
    ).decode("ascii").split()
    exigir(len(montaje) == 4 and montaje[:3] == ["192.168.0.8:/volume2/producciones",
            "/mnt/producciones", "nfs4"]
            and {"rw", "vers=4.1", "proto=tcp"} <= set(montaje[3].split(",")), "MONTAJE_NAS")
    if modo == 0o777:
        ruta_recibo = os.environ.get("BACKUP_NAS_DSM_RECEIPT_FILE", "")
        exigir(ruta_recibo, "RECIBO_DSM_REQUERIDO")
        recibo = canonica(ruta_recibo)
        identidad = archivo_privado(recibo)
        fd = os.open(recibo, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as archivo:
            exigir(sello(os.fstat(archivo.fileno())) == identidad, "RECIBO_DSM_CAMBIO")
            datos = archivo.read(4097)
            exigir(sello(os.fstat(archivo.fileno())) == sello(recibo.lstat()) == identidad,
                    "RECIBO_DSM_CAMBIO")
        exigir(len(datos) <= 4096, "RECIBO_DSM_EXCESIVO")
        esperado = {
            "schema": 1,
            "path": str(Path("/volume2/producciones") / ruta.relative_to("/mnt/producciones")),
            "inode": info.st_ino,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": "0700",
        }
        hechos = json.loads(datos, object_pairs_hook=tuple)
        exigir(isinstance(hechos, tuple) and len(hechos) == len(esperado), "RECIBO_DSM_INVALIDO")
        hechos = dict(hechos)
        exigir(hechos == esperado and type(hechos["schema"]) is int, "RECIBO_DSM_DISTINTO")
    return info.st_dev, info.st_ino


def resumen_restic(ruta, archivos_esperados):
    resumenes = []
    ultimo = None
    errores = False
    with ruta.open("rb") as salida:
        while linea := salida.readline(65537):
            exigir(len(linea) <= 65536, "SALIDA_RESTIC_EXCESIVA")
            if not linea.strip():
                continue
            ultimo = json.loads(linea)
            exigir(isinstance(ultimo, dict), "SALIDA_RESTIC_INVALIDA")
            tipo = ultimo.get("message_type")
            if tipo == "summary":
                resumenes.append(ultimo)
            errores |= tipo in ("error", "exit_error")
    exigir(len(resumenes) == 1, "RESUMEN_RESTIC_NO_UNICO")
    resumen = resumenes[0]
    snapshot = resumen.get("snapshot_id")
    exigir(isinstance(snapshot, str) and SHA256.fullmatch(snapshot), "SNAPSHOT_NO_ACREDITADO")
    return snapshot, (ultimo is resumen and not errores and resumen.get("dry_run", False) is False
                      and resumen.get("total_files_processed") == archivos_esperados)


def main(args):
    if args == ["--help"]:
        print(USO)
        return 0
    alcance = args[-1] if len(args) == 9 and args[-1] in ALCANCES else None
    if alcance:
        args = args[:-1]
    if len(args) != 8:
        print(USO, file=sys.stderr)
        return 1
    destino, origen, corrida, ejecutable, pin, repo, repo_id, recibos = args
    recibo = {"schema": 1, "success": False, "backup_attempted": False}
    archivos, etiqueta = ALCANCES.get(alcance, (ARCHIVOS, "agency-backup-321"))
    if alcance:
        recibo["scope"] = alcance[2:]
    registro = None
    etapa = "entrada"
    try:
        exigir(destino in ("nas", "r2") and SHA256.fullmatch(pin) and SHA256.fullmatch(repo_id)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", corrida), "ENTRADA_INVALIDA")
        exigir(os.uname().sysname == "Linux" and os.uname().nodename == "vm-media-01"
                and os.geteuid() == os.getegid() == 0, "HOST_O_IDENTIDAD")
        recibo.update(destination=destino, run_id=corrida, repository_id=repo_id, restic_sha256=pin)
        etapa = "staging"
        staging = directorio_privado(origen)
        identidades, digest = verificar_staging(staging, archivos)
        recibo["manifest_sha256"] = digest
        destino_recibo = Path(recibos)
        exigir(destino_recibo.is_absolute() and destino_recibo.name not in ("", ".", "..")
                and not destino_recibo.is_relative_to(staging), "RUTA_RECIBO")
        directorio_privado(destino_recibo.parent)
        etapa = "credenciales_y_binario"
        secreto = canonica(os.environ.get("RESTIC_PASSWORD_FILE", ""))
        exigir(not secreto.is_relative_to(staging), "PASSFILE_EN_STAGING")
        identidad_secreto = archivo_privado(secreto, MODOS_SECRETO)
        binario = canonica(ejecutable)
        identidad_binario = archivo_privado(binario, MODOS_BINARIO)
        exigir(sha_archivo(binario, identidad_binario) == pin, "SHA_RESTIC")
        env = {**ENV_LOCAL, "HOME": str(destino_recibo), "TMPDIR": str(destino_recibo),
               "RESTIC_REPOSITORY": repo, "RESTIC_PASSWORD_FILE": str(secreto)}
        opciones = ["--json", "--no-cache"]
        etapa = "repositorio"
        if destino == "nas":
            exigir(re.fullmatch(r"/mnt/producciones/_backups(/[A-Za-z0-9][A-Za-z0-9._-]*)+", repo),
                    "RUTA_NAS")
            identidad_nas = nas_acreditado(repo)
        else:
            exigir(re.fullmatch(r"s3:https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com/"
                               r"studio-backups-bucket(/[A-Za-z0-9][A-Za-z0-9._-]*)+", repo), "URL_R2")
            for variable, fuente in (("AWS_ACCESS_KEY_ID", "R2_PLATFORM_ACCESS_KEY_ID"),
                                      ("AWS_SECRET_ACCESS_KEY", "R2_PLATFORM_SECRET_ACCESS_KEY")):
                valor = os.environ.get(fuente, "")
                exigir(re.fullmatch(r"[!-~]{1,256}", valor) and valor != "CHANGE_ME", "CREDENCIAL_R2")
                env[variable] = valor
            env.update(AWS_DEFAULT_REGION="auto", AWS_EC2_METADATA_DISABLED="true")
            opciones += ["-o", "s3.region=auto", "-o", "s3.bucket-lookup=path"]
        etapa = "recibo"
        destino_recibo.mkdir(mode=0o700)
        registro = destino_recibo / "result.json"

        def restic(nombre, argumentos, timeout=None):
            exigir(archivo_privado(binario, MODOS_BINARIO) == identidad_binario
                    and sha_archivo(binario, identidad_binario) == pin, "BINARIO_CAMBIO")
            exigir(archivo_privado(secreto, MODOS_SECRETO) == identidad_secreto, "PASSFILE_CAMBIO")
            with (destino_recibo / f"{nombre}.stdout").open("xb") as salida, (
                destino_recibo / f"{nombre}.stderr"
            ).open("xb") as error:
                if nombre == "backup":
                    recibo["backup_attempted"] = True
                return subprocess.run([str(binario), *opciones, *argumentos],
                    cwd=staging, env=env, stdin=subprocess.DEVNULL, stdout=salida, stderr=error,
                    check=False, timeout=timeout).returncode

        etapa = "identidad_repositorio"
        exigir(restic("cat-config", ["cat", "config"], timeout=60) == 0, "CAT_CONFIG_FALLO")
        ruta_config = destino_recibo / "cat-config.stdout"
        exigir(ruta_config.stat().st_size <= 65536, "CONFIG_EXCESIVA")
        config = json.loads(ruta_config.read_bytes())
        exigir(isinstance(config, dict) and config.get("id") == repo_id
                and type(config.get("version")) is int and config["version"] in (1, 2), "REPO_ID_DISTINTO")
        if destino == "nas":
            exigir(nas_acreditado(repo) == identidad_nas, "NAS_CAMBIO")

        etapa = "backup"
        staging_estable(staging, identidades)
        codigo = restic("backup", ["backup", "--tag", etiqueta, "--tag", f"run:{corrida}",
                                  "--", *identidades])
        recibo["restic_exit_code"] = codigo
        snapshot, completo = resumen_restic(destino_recibo / "backup.stdout", len(identidades))
        recibo["snapshot_id"] = snapshot
        exigir(codigo == 0 and completo, "BACKUP_INCOMPLETO")
        etapa = "verificacion_final"
        staging_estable(staging, identidades)
        if destino == "nas":
            exigir(nas_acreditado(repo) == identidad_nas, "NAS_CAMBIO")
        recibo["success"] = True
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        codigo = str(error)
        recibo.update(stage=etapa, error_code=codigo if re.fullmatch(r"[A-Z0-9_]{1,64}", codigo)
                      else type(error).__name__)
    recibo["reconciliation_required"] = recibo["backup_attempted"] and not recibo["success"]
    if registro is not None:
        try:
            with registro.open("x", encoding="utf-8") as archivo:
                archivo.write(json.dumps(recibo, sort_keys=True) + "\n")
                archivo.flush()
                os.fsync(archivo.fileno())
            for directorio in (registro.parent, registro.parent.parent):
                fd = os.open(directorio, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError:
            recibo.update(success=False, error_code="RECIBO_NO_PRESERVADO",
                          reconciliation_required=recibo["backup_attempted"])
    print(json.dumps(recibo, sort_keys=True))
    return 0 if recibo["success"] else 1


raise SystemExit(main(sys.argv[1:]))
PYTHON
