# Imagen PostgreSQL Immich a partir de artefactos detenidos

Unidad de cinco archivos: controlador local, helper remoto, Dockerfile,
pruebas y este procedimiento. El agente prepara fuentes/perfiles y valida
artefactos como datos. No ha conectado infraestructura ni ejecutado Docker,
compiladores, SDK o SQL. La estructura válida no acredita producción.

## Transiciones y evidencia

1. `plan` predeterminado: propuesta local, sin abrir bóveda ni red.
2. `plan --output`: liga el perfil a fuentes y recibos ya respaldados; no conecta.
3. `prepare`: verifica host/layout/SDK detenido, copia artefactos por descriptores,
   exporta localmente la base y publica un layout OCI privado. No construye.
4. Raíz respalda y relee PREPARED_B64, con inodos y hashes, antes de continuar.
5. `build`: revalida prepare, inputs, salidas, base/OCI y ABI; sólo entonces
   construye la imagen de COPY. Exporta y valida el resultado. No arranca una DB.
6. Raíz respalda BUILT_B64/ID antes de la futura aceptación SQL y SCA real.

No se altera el fallo original V3 b460fc78…: Cargo terminó release pero falló
el buscador del build output. El gate exige por separado ese fallo literal,
aceptación postverify V3 positiva con fuente fa397b9b…/perfilf46df24f… y los fallos407e…/0cab… conservados, más STOP canónico
7c2a9fa9… del CID f042555d…. Postverify acredita SQLite/ELF/SQL/licencias y sus
bytes se reobservan antes y después de la promoción. La parada se toma de
`Controller.saved()['stop']`, que fusiona CONTAINER_B64 con los otros grupos.
No se llama al build_profile de compile V3 después de añadir STOP; se conserva
el perfil histórico bffefb81… intacto. Ninguna compilación se presenta como
repetida o convertida retroactivamente en un éxito.

## Rutas fijas ya respaldadas

| Campo | Ruta o valor |
| --- | --- |
| CONTEXT_ROOT | /opt/media-platform/build/contexts/postgres-immich-runtime/fase4-20261106-01 |
| RECORD_ROOT | /var/lib/media-platform/build/postgres-immich-runtime/fase4-20261106-01 |
| BASE_OCI_ROOT | RECORD_ROOT/base-oci |
| OWNER | agencia-digital-marketing-ai:postgres-immich-runtime:v4.1 |
| Prefijo | MEDIA_POSTGRES_IMMICH_RUNTIME_ |

Padres nuevos propios tienen marcadores ligados a dev/inode, root0700; los
ancestros compartidos requieren sus marcadores previos. No se adoptan padres
sin marcador. Contexto y registro son EXCL: no se sobrescribe una ejecución
parcial. Las salidas originales siguen bajo el control del SDK V4, no bajo el
namespace de esta imagen. Ninguna ruta es NAS o PGDATA de aplicaciones.

## Base y proyección

Base local de aplicaciones, usuario PostgreSQL UID/GID70:

- Manifiesto sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a.
- Config sha256:22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c.
- PostgreSQL16.15 Alpine3.24, 45 APK conservados literalmente.
- Los scripts oficiales conservan sus hashes 9c440299… y 922ade6b…; gosu y
  su-exec deben estar realmente ausentes, sin borrar metadatos de paquetes.

Prepare usa `docker image save` local por ID, no pull ni push ni transferencia
Windows de la base. El TAR nuevo queda root0600 dentro del registro root0700.
Se verifican índice, manifiesto, config, cada blob, tamaño y DiffID de las doce
capas. Se aplican whiteouts en un sistema de archivos virtual, sin extractall
ni materializar archivos de la imagen en el host. El layout que consume
BuildKit contiene únicamente blobs y metadatos verificados.

La proyección admite exactamente vector.so, vchord.so y libsqlite3.so.0, los
controles/SQL vector/vchord y cinco licencias/avisos. Se excluyen expresamente
headers/bitcode de compilación; la lista se registra. Un archivo o biblioteca
adicional no aprobado causa rechazo. Las fuentes no se chmod/chown: la copia
nueva es root0444 y directorios root0755. Se verifican después de copiar y al
terminar build, incluyendo ausencia de symlinks/hardlinks.

## Cierre ABI previo y posterior

El parser ELF64 amd64 recorre DT_NEEDED de PostgreSQL y de las tres bibliotecas,
resuelve enlaces dentro del filesystem virtual y exige proveedores en los
45 APK de la base o en las tres adiciones explícitas. Resuelve RUNPATH $ORIGIN
para SQLite adyacente; rechaza variables/rutas relativas desconocidas,
proveedores conflictivos, bibliotecas ausentes, símbolos fuertes ausentes y
versiones ABI no proporcionadas. Los símbolos del ejecutable PostgreSQL se
incluyen para extensiones cargadas por el servidor. No se copian bibliotecas
del SDK para tapar carencias. El informe contiene hashes y aristas resueltas.

Este cierre estructural no sustituye una carga real ni prueba instrucciones
SIMD, funcionalidades SQL o rendimiento; esas pruebas siguen en el gate SQL.
SQLite conserva la prueba específica V3 de sourceid3.54.1, FTS5, símbolos,
symtab, DT_NEEDED libsqlite3.so.0 y RUNPATH $ORIGIN.

## Build sin SDK y sin registro remoto

Se exige Docker29.9.3, Buildx0.38.1 y builder default con driver docker. El
Dockerfile no descarga un frontend y sólo contiene FROM de contexto nombrado,
labels, COPY y USER70:70; no RUN ni compilación en BuildKit. El contexto es el
namespace propio y excluye marcadores mediante .dockerignore.

`--build-context pgbase=oci-layout:///ruta/base-oci@sha256:c908…` enlaza FROM
pgbase a blobs locales verificados. Se usan platform linux/amd64, network none,
pull=false, load, iidfile y metadata-file propios. No hay Git, SSH forwarding,
secret build args o tags remotos. El helper tiene presupuesto total600s y
captura4MiB; exportaciones120s/build300s. El recolector POSIX V4 conserva al
líder con WNOWAIT y cierra sólo el grupo del CLI que creó. Un fallo conserva
logs y datos propios; no elimina imágenes, cachés o contenedores ajenos.

La exportación final se vuelve a analizar. Debe preservar literalmente los
nodos/capas/paquetes de la base, entrypoint, CMD, SIGINT, PGDATA y VOLUME, salvo
labels propias y los archivos proyectados. Se verifican modos y hashes reales,
USER70:70 y cierre ABI. Buildx escribe iidfile como config digest; se valida contra containerimage.config.digest.
Se exporta por containerimage.digest (manifiesto), correlacionando ambos con
los blobs reales. El ID completo, config, iidfile, TAR/log/SHA y metadata quedan en
BUILT_B64. sql_verified y final_sca_verified permanecen false.

Fuentes primarias del contrato:
[Buildx: contextos OCI](https://docs.docker.com/reference/cli/docker/buildx/build/),
[driver docker](https://docs.docker.com/build/builders/drivers/docker/),
[parser OCI de Buildx0.38.1](https://github.com/docker/buildx/blob/v0.38.1/util/ocilayout/parse.go).
La aceptación real del named context en el daemon instalado sigue siendo parte
del primer build de raíz; aquí sólo se preparó el controlador.

## CLI y respaldo previo

```powershell
python -X utf8 -B docker/postgres-immich/final_image.py plan
python -X utf8 -B docker/postgres-immich/final_image.py plan --output '.tools/postgres-immich-build/runtime-profile.json'
```

El segundo comando sólo procede con postverify positivo y STOP ya duraderos.
Raíz respalda PROFILE_B64, PROFILE_SHA256, LOCAL_SHA256, REMOTE_SHA256 y
DOCKERFILE_SHA256 bajo el prefijo runtime antes de prepare. El CLI reutiliza
Paramiko5.1.0 aislado, identidad y pin SSH V4, host .85/hostname vm-media-01 y
layout raíz respaldado. No se aceptan credenciales nuevas ni otros hosts.

```powershell
python -X utf8 -B docker/postgres-immich/final_image.py prepare --profile '.tools/postgres-immich-build/runtime-profile.json' --expected-profile-sha256 '<SHA respaldado>' --root-update-vault
python -X utf8 -B docker/postgres-immich/final_image.py build --profile '.tools/postgres-immich-build/runtime-profile.json' --expected-profile-sha256 '<SHA respaldado>' --root-update-vault
```

Cada invocación es independiente. Sólo el CLI de raíz añade y relee el recibo
PREPARED_B64 o BUILT_B64, incluido un fallo; no altera recibos SDK/compile.
Build exige el prepare positivo respaldado literalmente. Un corte o recibo
previo impide replay y requiere inspección concreta, no reset automático.

## Matriz SQL pendiente: recursos aislados antes de Compose

Raíz deberá preparar otra unidad revisable y respaldar por adelantado nuevas
rutas, secreto propio, CID y hashes. Esta unidad no ejecuta estos pasos:

1. Directorio DB temporal propio bajo RECORD_ROOT, nunca NAS ni PGDATA ajeno,
   inode/device respaldados y dueño70:70 modo0700. Archivos secreto/PGPASSFILE
   distintos, dueño70:70 modo0400, respaldados antes de uso y nunca en argv/log.
2. Docker create de la imagen final por ID, UID70:70, network none, cero puertos,
   rootfs readonly, 2CPU/2GiB/swap adicional0/pids128/shm128MiB, tmpfs /tmp y
   /var/run/postgresql de UID70. Bind PGDATA explícito para enmascarar VOLUME y
   evitar volumen anónimo; NoForeign enumera sólo recursos propios. Respaldar
   CID/inspección antes de start. No tocar el SDK detenido ni otros servicios.
3. Initdb con checksums y auth local/host SCRAM, POSTGRES_PASSWORD_FILE; comprobar
   identidad efectiva70, versión16.15 y datos persistidos. Prueba TCP dentro del
   contenedor, primero sin contraseña (debe fallar), luego con PGPASSFILE.
4. Iniciar `postgres -c shared_preload_libraries=vchord.so`, comprobar librería
   SQLite mapeada desde $ORIGIN y CREATE EXTENSION vector VERSION '0.8.5', vchord
   VERSION '1.1.1', cube y earthdistance. Verificar extversion y funcionalidad,
   no sólo SELECT1 ni presencia en pg_available_extensions.
5. Dataset sintético de al menos1000 vectores512, índice vchordrq con
   vector_cosine_ops, consulta que use el índice y comparación con un oráculo
   secuencial. Semilla y tolerancia se fijan antes; valores finitos/distancias
   coherentes. Hacer también consulta vector L2 y operaciones cube/earthdistance.
   Registrar EXPLAIN y filas; ningún dato de clientes.
6. Sentinel SQL persistido, stop5/kill sólo si el CID propio acreditado sigue
   vivo, segundo start con el mismo bind, versiones/índice/query/sentinel otra
   vez. Inspeccionar logs y ausencia de SIGILL/errores de loader/SIMD. Conservar
   DB y recibos hasta revisión; no borrar datos para ocultar un fallo.
7. Parada final acreditada y respaldada antes de exportación/SCA. La imagen
   final requiere Trivy real completo de todos los paquetes y una auditoría
   explícita del grafo Cargo seleccionado y SQLite compilado. Trivy APK0HIGH /
   CRITICAL por sí solo no demuestra cobertura del .so Rust ni SQLite manual.
   No suprimir hallazgos, alterar umbral ni afirmar imagen limpia sin reportes.

[Immich PostgreSQL independiente](https://docs.immich.app/administration/postgres-standalone/)
y [VectorChord1.1.1](https://github.com/supervc-stack/VectorChord/tree/1.1.1)
sustentan la selección. Compatibilidad, SQL, SCA0HIGH/CRITICAL y rendimiento
siguen sin acreditarse por esta unidad de preparación.

## Delta de recuperación positiva

El gate exige ahora el recibo RECOVER
fc3341c72a1d2c9dfba1f1b25a23116ff457f75fc985a4f6fe5a68c1d3ee8d51 y el
catálogo79b873a93c6cdb7ab6f6628a18a0f9424fe36a4737a72a39465726960117c034,
perfil f46df24f… y fuente fa397b9b…. El perfil nuevo envuelve la historia
anterior; se comprueba que su base conserva exactamente el SDK y compile del
runtime. Se reobservan los tres fallos físicos, sus perfiles y STOP.

Al copiar, read_overlay_source pasa logical_path completo al lector RECOVER
congelado. Sólo las dos bibliotecas0700 observadas pueden usar esa excepción,
con sus UID/GID, nlink, tamaño, device/inode y SHA exactos del manifiesto
bb3bad78…. Las fuentes mantienen sus modos; sólo las copias nuevas son0444.
No hay cambio en Dockerfile, exportación OCI, cierre ABI o build.

El lector de bóveda propio de final_image.py admite8MiB, con el mismo read_source
sin enlaces/irregulares y parser canónico pin
5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda.
Fue ampliado antes de respaldar el perfil329KiB: el B64 más PREPARED superaría
los2MiB del CLI SDK. El módulo V4 no se modifica ni monkeypatchea. El perfil
runtime se lee con read_source(512KiB)+json_object, no con local.decode de256KiB.
Otros controladores congelados conservan sus límites; no se les atribuye esta
ampliación. La regresión usa un env real de3MiB y rechaza más8MiB y un directorio.
