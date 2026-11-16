"""Plan cerrado para el recurso propio DSM; el modo predeterminado no modifica el NAS."""

import argparse
import copy
import hashlib
import hmac
import json
import re
import shlex
from pathlib import Path

TARGET = "/volume2/producciones"
OWNER = "agencia-digital-marketing-ai:producciones:v4.1"
BACKUP_PATH = "/volume2/homes/jandrade/.agencia-media-config-backups/fase2-20261105"
EXECUTION_VALUES = {
    "SYNOLOGY_CONFIG_BACKUP_PATH": BACKUP_PATH,
    "SYNOLOGY_NFS_ANON_UID": "1024",
    "SYNOLOGY_NFS_ANON_GID": "100",
    "SYNOLOGY_NFS_SQUASH_MODE": "all_admin",
    "SYNOLOGY_SHARE_OWNER_MARKER": OWNER,
}
DIRECTORIES = [
    "_biblioteca_global",
    "_backups",
    "proyectos/sansevieras/01_raw",
    "proyectos/sansevieras/02_brief",
    "proyectos/sansevieras/03_assets",
    "proyectos/sansevieras/04_edicion",
    "proyectos/sansevieras/05_revision",
    "proyectos/sansevieras/06_renders",
]


class StorageError(Exception):
    """Rechazo cerrado del contrato o de la propiedad del destino."""

    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


def api_data(response):
    if response.get("codigo_salida") != 0 or response.get("truncado"):
        raise StorageError("El comando DSM no devolvió una respuesta completa.")
    try:
        payload = json.loads(response["salida"])
    except (ValueError, KeyError, TypeError) as error:
        raise StorageError("La respuesta DSM no es JSON válido.") from error
    if not isinstance(payload, dict) or payload.get("success") is not True:
        code = (
            payload.get("error", {}).get("code")
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict)
            else None
        )
        raise StorageError(
            "DSM rechazó la petición; no se considera éxito el código shell cero.",
            {"codigo_dsm": code if type(code) is int else None},
        )
    return payload.get("data", {})


def desired_rules():
    return [
        {
            "client": client,
            "privilege": "rw",
            "root_squash": "all_admin",
            "async": False,
            "crossmnt": False,
            "insecure": False,
            "security_flavor": {
                "sys": True,
                "kerberos": False,
                "kerberos_integrity": False,
                "kerberos_privacy": False,
            },
        }
        for client in ("192.168.0.85", "192.168.0.7")
    ]


def api_command(api, method, version=1, **parameters):
    arguments = [
        "/usr/syno/bin/synowebapi",
        "--exec",
        f"api={api}",
        f"method={method}",
        f"version={version}",
    ]
    arguments.extend(
        f"{key}={json.dumps(value, separators=(',', ':'))}" for key, value in parameters.items()
    )
    return shlex.join(arguments)


def nfs_rules_command():
    # Contrato del frontend AdminCenter DSM 7.3.2: load/save, share_name y rule.
    return api_command(
        "SYNO.Core.FileServ.NFS.SharePrivilege",
        "save",
        share_name="producciones",
        rule=desired_rules(),
    )


def nfs_protocol_command():
    # Solo los tres parámetros del formulario; las capacidades no son parámetros de escritura.
    return api_command(
        "SYNO.Core.FileServ.NFS", "set", 3, enable_nfs=True, enable_nfs_v4=True, enabled_minor_ver=1
    )


def foreign_exports(text):
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not re.match(r'^"?/volume2/producciones"?(?:\s|$)', line)
    ).removesuffix("\n")


def verify_foreign_exports(before, after):
    if foreign_exports(before) != foreign_exports(after):
        raise StorageError("Las exportaciones ajenas cambiaron; se detiene el procedimiento.")


def parse_share(response):
    if response.get("truncado"):
        raise StorageError("El registro del share está truncado.")
    text = response.get("salida", "")
    if response.get("codigo_salida") == 255 and "synoerr=[0x1400]" in text:
        return None
    if response.get("codigo_salida") != 0:
        raise StorageError("No se pudo comprobar el registro del share.")
    values = {}
    for key, label in (("name", "Name"), ("path", "Path"), ("desc", "Comment")):
        matches = re.findall(rf"^\s*{label}\s+\.*\[([^\r\n]*)\]\s*$", text, re.MULTILINE)
        if len(matches) != 1:
            raise StorageError("El registro del share no tiene el formato auditado.")
        values[key] = matches[0]
    return values


def build_plan(snapshot):
    if not isinstance(snapshot, dict):
        raise StorageError("El snapshot debe ser un objeto.")
    if (
        snapshot.get("model"),
        snapshot.get("dsm"),
        snapshot.get("hostname"),
        snapshot.get("volume"),
    ) != ("DVA3221", "7.3.2", "NAS", "/volume2"):
        raise StorageError(
            "El destino no coincide con el modelo, DSM, hostname y volumen auditados."
        )
    if type(snapshot.get("free_kib")) is not int or snapshot["free_kib"] < 16 * 1024 * 1024:
        raise StorageError("El volumen elegido no acredita al menos 16 GiB disponibles.")
    share = snapshot.get("share")
    if share is not None and (
        not isinstance(share, dict)
        or (share.get("name"), share.get("path"), share.get("desc"))
        != (
            "producciones",
            TARGET,
            OWNER,
        )
    ):
        raise StorageError(
            "No se adopta una carpeta compartida ajena o de propiedad no demostrada."
        )
    if share is None and snapshot.get("target_exists") is not False:
        raise StorageError("No se adopta un directorio previo sin registro propio.")
    nfs, smb = snapshot.get("nfs", {}), snapshot.get("smb", {})
    if (
        not isinstance(nfs, dict)
        or not isinstance(smb, dict)
        or not isinstance(snapshot.get("nfs_versions"), str)
    ):
        raise StorageError("Los metadatos de servicios deben tener el formato auditado.")
    if nfs.get("support_major_ver") != 4 or nfs.get("support_minor_ver", -1) < 1:
        raise StorageError("El backend no acredita soporte NFSv4.1.")
    if smb.get("enable_samba") is not True or smb.get("smb_max_protocol", 0) < 3:
        raise StorageError("El backend no acredita SMB3 disponible.")
    if (
        "+2" not in snapshot.get("nfs_versions", "").split()
        or "+3" not in snapshot.get("nfs_versions", "").split()
    ):
        raise StorageError("No se acredita la base NFS2/3 que debe preservarse.")
    exports = snapshot.get("exports")
    if not isinstance(exports, str):
        raise StorageError("Falta la copia de exportaciones que debe preservarse.")
    desired = copy.deepcopy(nfs)
    desired.update(enable_nfs=True, enable_nfs_v4=True, enabled_minor_ver=1)
    return {
        "aplicado": False,
        "ruta": TARGET,
        "propietario": OWNER,
        "crear_share": share is None,
        "directorios": DIRECTORIES.copy(),
        "exports_ajenos": foreign_exports(exports),
        "exports_ajenos_sha256": hashlib.sha256(foreign_exports(exports).encode()).hexdigest(),
        "nfs_deseado": desired,
        "reglas_nfs": desired_rules(),
        "actualizar_reglas_nfs": snapshot.get("rules", []) != desired_rules(),
        "requiere_interrupcion_nfs": nfs.get("enable_nfs_v4") is not True
        or nfs.get("enabled_minor_ver") != 1,
        "smb_global_cambia": False,
        "acl_usuarios": ["admin", "jandrade"],
        "uid_nfs_mapeado": 1024,
        "gid_nfs_mapeado": 100,
    }


def plan_hash(plan):
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def checked_result(response, step="lectura"):
    if response.get("codigo_salida") != 0 or response.get("truncado"):
        detail = response.get("error", "")
        detail = "\n".join(
            "Una herramienta gestionada terminó con error; comando omitido."
            if "Command " in line or "python3 -c" in line or "sudo -S" in line
            else line
            for line in detail.splitlines()
        )[-2000:]
        raise StorageError(
            "Una operación no terminó correctamente; no se ejecutan pasos posteriores.",
            {
                "paso": step,
                "codigo_salida": response.get("codigo_salida"),
                "truncado": bool(response.get("truncado")),
                "detalle": detail,
            },
        )
    return response


def python_command(source, *arguments):
    return shlex.join(["python3", "-c", source, *arguments])


def validate_execution_values(values):
    if any(values.get(key) != expected for key, expected in EXECUTION_VALUES.items()):
        raise StorageError(
            "El respaldo y la política deben coincidir con los valores ya persistidos en la bóveda."
        )


PRIVATE_MODE_SOURCE = r"""
import os,stat,subprocess
PRIVATE_ROOT='/volume2/homes/jandrade/.agencia-media-config-backups'
PRIVATE_NAMES={PRIVATE_ROOT,PRIVATE_ROOT+'/.owner',PRIVATE_ROOT+'/fase2-20261105'}
PRIVATE_NAMES.update(PRIVATE_ROOT+'/fase2-20261105/'+name for name in ['exports','syno_nfs_conf','smb.share.conf','snapshot-before.json','manifest.json'])
def linux_acl(path):
 result=subprocess.run(['/usr/syno/bin/synoacltool','-get',str(path)],capture_output=True,text=True)
 if result.returncode==255 and "It's Linux mode" in result.stdout:return True
 if result.returncode==0 and 'ACL version:' in result.stdout:return False
 raise RuntimeError('No se pudo comprobar el formato de permisos de una ruta propia.')
def private(path,mode,directory=False):
 st=path.lstat()
 assert str(path) in PRIVATE_NAMES
 assert st.st_uid==0 and stat.S_IMODE(st.st_mode)==mode and not path.is_symlink()
 assert path.is_dir() if directory else path.is_file()
 assert linux_acl(path)
def make_private(path,mode,directory=False):
 assert str(path) in PRIVATE_NAMES and path.lstat().st_uid==0 and not path.is_symlink()
 os.chmod(path,mode)
 if not linux_acl(path):
  subprocess.run(['/usr/syno/bin/synoacltool','-del',str(path)],check=True,capture_output=True,text=True)
 os.chmod(path,mode)
 private(path,mode,directory)
"""


BACKUP_SOURCE = (
    PRIVATE_MODE_SOURCE
    + r"""
import pathlib,os,stat,sys,json,hashlib
assert os.geteuid()==0
os.umask(0o077)
root=pathlib.Path(sys.argv[1])
assert str(root)=='/volume2/homes/jandrade/.agencia-media-config-backups/fase2-20261105'
assert str(root.resolve())==str(root)
verify=len(sys.argv)>2 and sys.argv[2]=='--verify'
def digest(data):return hashlib.sha256(data).hexdigest()
if not verify:
 snapshot=json.loads(sys.argv[2])
 assert pathlib.Path('/etc/exports').read_text()==snapshot['exports']
 parent=root.parent
 if parent.exists():
  private(parent,0o700,True)
  marker=parent/'.owner';private(marker,0o600)
  assert marker.read_text()=='agencia-digital-marketing-ai:config-backups:v4.1\n'
 else:
  parent.mkdir(mode=0o700)
  make_private(parent,0o700,True)
  marker=parent/'.owner'
  with marker.open('x') as stream:stream.write('agencia-digital-marketing-ai:config-backups:v4.1\n')
  make_private(marker,0o600)
 assert not root.exists() and not root.is_symlink()
 root.mkdir(mode=0o700)
 make_private(root,0o700,True)
 files={}
 for source,name in [('/etc/exports','exports'),('/etc/nfs/syno_nfs_conf','syno_nfs_conf'),('/etc/samba/smb.share.conf','smb.share.conf')]:
  data=pathlib.Path(source).read_bytes()
  with (root/name).open('xb') as stream:stream.write(data)
  make_private(root/name,0o600)
  files[name]={'source':source,'sha256':digest(data)}
 data=(json.dumps(snapshot,sort_keys=True,ensure_ascii=False,indent=2)+'\n').encode()
 with (root/'snapshot-before.json').open('xb') as stream:stream.write(data)
 make_private(root/'snapshot-before.json',0o600)
 files['snapshot-before.json']={'source':'auditoria previa','sha256':digest(data)}
 manifest={'owner':'agencia-digital-marketing-ai:config-backups:v4.1','files':files}
 with (root/'manifest.json').open('x') as stream:json.dump(manifest,stream,sort_keys=True,indent=2)
 make_private(root/'manifest.json',0o600)
private(root.parent,0o700,True)
marker=root.parent/'.owner';private(marker,0o600)
assert marker.read_text()=='agencia-digital-marketing-ai:config-backups:v4.1\n'
private(root,0o700,True)
mp=root/'manifest.json';private(mp,0o600)
raw=mp.read_bytes();manifest=json.loads(raw)
assert manifest['owner']=='agencia-digital-marketing-ai:config-backups:v4.1'
assert set(manifest['files'])=={'exports','syno_nfs_conf','smb.share.conf','snapshot-before.json'}
for name,item in manifest['files'].items():
 p=root/name;private(p,0o600)
 assert digest(p.read_bytes())==item['sha256']
if verify:assert digest(raw)==sys.argv[3]
print(json.dumps({'backup_path':str(root),'manifest_sha256':digest(raw),'archivos_verificados':list(manifest['files'])}))
"""
)


def backup_command(path, snapshot):
    if path != BACKUP_PATH:
        raise StorageError(
            "El respaldo debe permanecer en la subruta privada respaldada del share homes."
        )
    return python_command(
        BACKUP_SOURCE, path, json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)
    )


def verify_backup_command(path, manifest_hash):
    if path != BACKUP_PATH or not re.fullmatch("[a-f0-9]{64}", manifest_hash):
        raise StorageError("El manifiesto respaldado no tiene un hash SHA256 válido.")
    return python_command(BACKUP_SOURCE, path, "--verify", manifest_hash)


REPAIR_PERMISSIONS_SOURCE = (
    PRIVATE_MODE_SOURCE
    + r"""
import pathlib,sys,json,hashlib
assert os.geteuid()==0
root=pathlib.Path(sys.argv[1])
assert str(root)=='/volume2/homes/jandrade/.agencia-media-config-backups/fase2-20261105'
assert str(root.resolve())==str(root)
expected=sys.argv[2]
mp=root/'manifest.json'
raw=mp.read_bytes()
assert hashlib.sha256(raw).hexdigest()==expected
manifest=json.loads(raw)
assert manifest['owner']=='agencia-digital-marketing-ai:config-backups:v4.1'
sources={'exports':'/etc/exports','syno_nfs_conf':'/etc/nfs/syno_nfs_conf','smb.share.conf':'/etc/samba/smb.share.conf','snapshot-before.json':'auditoria previa'}
assert set(manifest['files'])==set(sources)
marker=root.parent/'.owner'
assert marker.read_text()=='agencia-digital-marketing-ai:config-backups:v4.1\n'
targets=[(root.parent,0o700,True),(root,0o700,True),(marker,0o600,False),(mp,0o600,False)]
targets.extend((root/name,0o600,False) for name in sources)
before={}
for p,mode,directory in targets:
 assert str(p.resolve())==str(p) and not p.is_symlink() and p.lstat().st_uid==0
 assert p.is_dir() if directory else p.is_file()
 before[str(p)]=None if directory else hashlib.sha256(p.read_bytes()).hexdigest()
for name,source in sources.items():
 item=manifest['files'][name]
 assert item['source']==source and hashlib.sha256((root/name).read_bytes()).hexdigest()==item['sha256']
 if source!='auditoria previa':assert hashlib.sha256(pathlib.Path(source).read_bytes()).hexdigest()==item['sha256']
for p,mode,directory in targets:make_private(p,mode,directory)
after={}
for p,mode,directory in targets:
 private(p,mode,directory)
 current=None if directory else hashlib.sha256(p.read_bytes()).hexdigest()
 assert before[str(p)]==current
 after[str(p)]={'uid':p.lstat().st_uid,'modo':oct(mode),'sha256':current,'sin_synoacl':linux_acl(p)}
assert hashlib.sha256(mp.read_bytes()).hexdigest()==expected
print(json.dumps({'reparado':True,'manifest_sha256':expected,'contenido_identico':True,'rutas_verificadas':after}))
"""
)


def repair_permissions_command(path, manifest_hash):
    if path != BACKUP_PATH or not re.fullmatch("[a-f0-9]{64}", manifest_hash):
        raise StorageError("La reparación exige la ruta y el SHA256 de manifiesto ya respaldados.")
    return python_command(REPAIR_PERMISSIONS_SOURCE, path, manifest_hash)


ACL_SOURCE = r"""
import os,pathlib,re,subprocess,json,sys
p=pathlib.Path('/volume2/producciones')
def tool_failure(result,operation):
 print(json.dumps({'fallo_herramienta':{'operacion':operation,'codigo_salida':result.returncode,'salida':result.stdout[-1000:],'error':result.stderr[-1000:]}}),file=sys.stderr)
 raise RuntimeError('Una herramienta gestionada no terminó correctamente.')
def run(*args):
 result=subprocess.run(args,capture_output=True,text=True)
 if result.returncode!=0:tool_failure(result,args[1])
 return result.stdout
record=run('/usr/syno/sbin/synoshare','--get','producciones')
assert 'Comment ....[agencia-digital-marketing-ai:producciones:v4.1]' in record
assert p.is_dir() and not p.is_symlink() and str(p.resolve())==str(p)
acl='/usr/syno/bin/synoacltool'
def read_entries():
 result=subprocess.run([acl,'-get',str(p)],capture_output=True,text=True)
 if result.returncode==255 and "It's Linux mode" in result.stdout:return []
 if result.returncode!=0:tool_failure(result,'leer_acl')
 text=result.stdout
 assert 'ACL version:' in text
 entries=re.findall(r'^\s*\[(\d+)\]\s*([^ \r\n]+)',text,re.M)
 assert len(re.findall(r'^\s*\[',text,re.M))==len(entries)
 assert len({int(i) for i,_ in entries})==len(entries)
 return entries
entries=read_entries()
desired=['user:admin:allow:rwxpdDaARWcCo:fd--','user:jandrade:allow:rwxpdDaARWcCo:fd--']
if sorted(entry for _,entry in entries)!=sorted(desired):
 os.chmod(p,0o700)
 entries=read_entries()
 for index,_ in sorted(entries,key=lambda item:int(item[0]),reverse=True):
  run(acl,'-del',str(p),index)
 for entry in desired:run(acl,'-add',str(p),entry)
if (p.stat().st_uid,p.stat().st_gid)!=(1024,100):
 run(acl,'-set-owner',str(p),'user','admin')
 os.chown(p,1024,100)
assert (p.stat().st_uid,p.stat().st_gid)==(1024,100)
after=run(acl,'-get',str(p))
actual=re.findall(r'^\s*\[\d+\]\s*([^ \r\n]+)',after,re.M)
assert sorted(actual)==sorted(desired)
for user in ['admin','jandrade']:
 permission=run(acl,'-get-perm',str(p),user)
 assert 'rwxpdDaARWcCo' in permission
print(json.dumps({'acl_verificada':True,'usuarios':['admin','jandrade'],'uid':p.stat().st_uid,'gid':p.stat().st_gid}))
"""


DIRECTORIES_SOURCE = r"""
import pathlib,os,json
root=pathlib.Path('/volume2/producciones')
assert root.is_dir() and not root.is_symlink() and str(root.resolve())==str(root)
names=['_biblioteca_global','_backups','proyectos/sansevieras/01_raw','proyectos/sansevieras/02_brief','proyectos/sansevieras/03_assets','proyectos/sansevieras/04_edicion','proyectos/sansevieras/05_revision','proyectos/sansevieras/06_renders']
created=[]
for name in names:
 path=root
 for part in pathlib.PurePosixPath(name).parts:
  path=path/part
  if path.exists() or path.is_symlink():
   assert path.is_dir() and not path.is_symlink()
  else:
   path.mkdir(mode=0o770)
   os.chown(path,1024,100)
   created.append(str(path.relative_to(root)))
print(json.dumps({'directorios_verificados':names,'directorios_creados':created}))
"""


def execute_plan(plan, runner, backup_verified, approved_hash):
    if not backup_verified or not hmac.compare_digest(plan_hash(plan), approved_hash):
        raise StorageError("Falta el respaldo verificado o el hash del plan revisado.")
    operations = []
    if plan["crear_share"]:
        command = shlex.join(
            [
                "/usr/syno/sbin/synoshare",
                "--add",
                "producciones",
                OWNER,
                TARGET,
                "",
                "admin,jandrade",
                "",
                "1",
                "0",
            ]
        )
        operations.append(("crear_share", command, False))
    operations.extend(
        [
            ("acl_propia", python_command(ACL_SOURCE), False),
            ("directorios", python_command(DIRECTORIES_SOURCE), False),
        ]
    )
    if plan["actualizar_reglas_nfs"]:
        operations.append(("reglas_nfs", nfs_rules_command(), True))
    if plan["requiere_interrupcion_nfs"]:
        operations.append(("protocolo_nfs41", nfs_protocol_command(), True))
    result = {}
    for name, command, is_api in operations:
        response = checked_result(runner(command), name)
        if is_api:
            try:
                api_data(response)
            except StorageError as error:
                error.diagnostic.update(paso=name, codigo_salida=response["codigo_salida"])
                raise
        result[name] = response
    return result


SNAPSHOT_SOURCE = r"""
import pathlib,os,socket,json
v={}
for line in pathlib.Path('/etc/VERSION').read_text().splitlines():
 if '=' in line:
  key,value=line.split('=',1);v[key]=value.strip('"')
p=pathlib.Path('/volume2/producciones');space=os.statvfs('/volume2')
print(json.dumps({'model':pathlib.Path('/proc/sys/kernel/syno_hw_version').read_text().strip(),'dsm':v.get('productversion'),'hostname':socket.gethostname(),'volume':'/volume2','free_kib':space.f_bavail*space.f_frsize//1024,'target_exists':p.exists() or p.is_symlink(),'target_is_symlink':p.is_symlink(),'exports':pathlib.Path('/etc/exports').read_text(),'nfs_versions':pathlib.Path('/proc/fs/nfsd/versions').read_text().strip(),'nfs_threads':pathlib.Path('/proc/fs/nfsd/threads').read_text().strip()}))
"""


def collect_snapshot(runner):
    response = checked_result(runner(python_command(SNAPSHOT_SOURCE)))
    try:
        snapshot = json.loads(response["salida"])
    except (KeyError, ValueError, TypeError) as error:
        raise StorageError("No se pudo leer un snapshot completo.") from error
    snapshot["share"] = parse_share(runner("/usr/syno/sbin/synoshare --get producciones"))
    if snapshot.get("target_is_symlink"):
        raise StorageError("La ruta del share no puede ser un enlace simbólico.")
    snapshot["nfs"] = api_data(runner(api_command("SYNO.Core.FileServ.NFS", "get", 3)))
    snapshot["smb"] = api_data(runner(api_command("SYNO.Core.FileServ.SMB", "get", 3)))
    snapshot["rules"] = (
        []
        if snapshot["share"] is None
        else api_data(
            runner(
                api_command(
                    "SYNO.Core.FileServ.NFS.SharePrivilege", "load", share_name="producciones"
                )
            )
        )["rule"]
    )
    return snapshot


def verify_postconditions(before, after):
    verify_foreign_exports(before["exports"], after["exports"])
    if after["share"] != {"name": "producciones", "path": TARGET, "desc": OWNER}:
        raise StorageError("El share final no acredita la propiedad y ruta esperadas.")
    if after["rules"] != desired_rules():
        raise StorageError("Las reglas NFS finales difieren de las dos reglas propias.")
    expected_nfs = dict(before["nfs"])
    expected_nfs.update(enable_nfs=True, enable_nfs_v4=True, enabled_minor_ver=1)
    if after["nfs"] != expected_nfs or after["smb"] != before["smb"]:
        raise StorageError("La configuración final alteró parámetros que debían preservarse.")
    versions = after["nfs_versions"].split()
    if (
        not {"+2", "+3", "+4", "+4.1", "-4.2"}.issubset(versions)
        or not after["nfs_threads"].isdigit()
        or int(after["nfs_threads"]) < 1
    ):
        raise StorageError("NFS no acredita las versiones preservadas y sus hilos activos.")


def live_operation(vault_path, mode, plan_path, approved_hash="", timeout=30):
    import synology_storage_audit as audit

    settings = audit.load_settings(vault_path, "key", timeout)
    values = audit.read_vault(vault_path)
    if mode in ("backup", "apply"):
        validate_execution_values(values)
    client = audit.connect_client(settings)
    try:
        reader = audit.RemoteReader(client, settings)
        sudo_mode, _ = reader.prepare_sudo()
        if sudo_mode == "none":
            raise StorageError("No se acreditó sudo; no se ejecutan cambios.")

        def runner(command):
            return reader.run(command, sudo_mode)

        snapshot = collect_snapshot(runner)
        current = build_plan(snapshot)
        if mode == "plan":
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return {
                "modo": "Plan de solo lectura",
                "plan_path": str(plan_path),
                "plan_sha256": plan_hash(current),
            }
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        if (
            not hmac.compare_digest(plan_hash(plan), approved_hash)
            or plan_hash(current) != approved_hash
        ):
            raise StorageError("El plan revisado o el estado crítico del NAS cambiaron.")
        if mode == "backup":
            result = checked_result(
                runner(backup_command(values["SYNOLOGY_CONFIG_BACKUP_PATH"], snapshot)), "respaldo"
            )
            return {
                "modo": "Respaldo privado",
                "respaldo": json.loads(result["salida"]),
                "plan_sha256": approved_hash,
            }
        manifest_hash = values.get("SYNOLOGY_CONFIG_BACKUP_MANIFEST_SHA256", "")
        receipt = checked_result(
            runner(verify_backup_command(values["SYNOLOGY_CONFIG_BACKUP_PATH"], manifest_hash)),
            "verificar_respaldo",
        )
        backup = json.loads(receipt["salida"])
        if backup.get("manifest_sha256") != manifest_hash:
            raise StorageError("El hash del manifiesto no coincide con su respaldo en la bóveda.")

        def guarded_runner(command):
            response = runner(command)
            if response["codigo_salida"] == 0:
                exports_response = checked_result(
                    runner(
                        python_command(
                            "import pathlib,json;print(json.dumps(pathlib.Path('/etc/exports').read_text()))"
                        )
                    )
                )
                verify_foreign_exports(snapshot["exports"], json.loads(exports_response["salida"]))
            return response

        operations = execute_plan(plan, guarded_runner, True, approved_hash)
        final = collect_snapshot(runner)
        verify_postconditions(snapshot, final)
        return {
            "modo": "Aplicación verificada",
            "aplicado": True,
            "respaldo": backup,
            "operaciones": operations,
            "estado_final": final,
        }
    finally:
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Auditar o preparar Synology; backup y aplicación son modos explícitos separados"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--snapshot", type=Path)
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--backup", action="store_true")
    modes.add_argument("--apply", action="store_true")
    project = Path(__file__).resolve().parents[1]
    parser.add_argument("--vault-path", type=Path, default=project / ".env")
    parser.add_argument(
        "--plan-path", type=Path, default=project / ".tools/evidence/synology-storage-plan.json"
    )
    parser.add_argument("--expected-plan-sha256", default="")
    parser.add_argument("--timeout", type=int, choices=range(2, 61), default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    protected = {args.vault_path.resolve(), args.plan_path.resolve()}
    if args.snapshot:
        protected.add(args.snapshot.resolve())
    if (
        args.output.name.startswith(".env")
        or args.output.resolve() in protected
        or args.plan_path.name.startswith(".env")
    ):
        print(
            json.dumps(
                {
                    "error": {
                        "codigo": "SALIDA_INVALIDA",
                        "mensaje": "La salida no puede sobrescribir la bóveda, el plan ni su entrada.",
                    }
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        if args.snapshot:
            result = build_plan(json.loads(args.snapshot.read_text(encoding="utf-8-sig")))
        else:
            mode = "apply" if args.apply else ("backup" if args.backup else "plan")
            if mode != "plan" and not re.fullmatch("[a-f0-9]{64}", args.expected_plan_sha256):
                raise StorageError(
                    "Debe indicarse el SHA256 del plan revisado antes de backup o apply."
                )
            result = live_operation(
                args.vault_path, mode, args.plan_path, args.expected_plan_sha256, args.timeout
            )
    except StorageError as error:
        result = {
            "error": {"codigo": "OPERACION_DETENIDA", "mensaje": str(error)},
            "verificado": False,
        }
        if error.diagnostic:
            result["error"]["diagnostico"] = error.diagnostic
    except Exception as error:
        result = {
            "error": {
                "codigo": "ACCESO_O_LECTURA_FALLIDA",
                "tipo": type(error).__name__,
                "mensaje": "La operación se detuvo sin reintentar credenciales ni publicar detalles sensibles.",
            },
            "verificado": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
