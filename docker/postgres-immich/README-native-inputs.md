# Verificación estática de APK y clausura del SDK

`native_inputs.py` es una biblioteca sin efectos al importar. No descarga, instala o ejecuta paquetes, scripts, compiladores o SDK. Esta unidad añade el verificador, sus pruebas y dos fixtures públicos; el staging completo de APK/SDK será una unidad posterior. No modifica el manifiesto `8d5a521ef8b2827f5198b9dcb5148c05f0aad743c7ce39912c75491139f5f745` ni el vendor cerrado.

## Firmas e integridad

Implementa la lectura APKv2 documentada por [apk-tools](https://github.com/alpinelinux/apk-tools/blob/master/doc/apk-v2.5.scd): miembros gzip separados para firmas/control/payload, firma del control comprimido y `datahash` SHA-256 del payload comprimido. La identidad `Q1` del índice es el SHA-1 del control. Usa `cryptography` ya disponible en las dependencias del proyecto para verificar RSA, sin implementar primitivas propias ni generar claves privadas.

Acepta RSA/SHA-1, RSA/SHA-256 o RSA/SHA-512 de una clave pública proporcionada explícitamente por el llamador, con tamaño mínimo de 2048 bits. RSA/SHA-1 es el formato legacy real publicado por Alpine, no una reducción de validación introducida por el preparador. El archivo completo se identificará además mediante SHA-256 en el nuevo lock antes de instalar o usar el SDK. Las claves contenidas en un paquete no se adoptan como confiables.

Comprueba firma, nombre, versión, arquitectura, identidad del índice y datahash. Rechaza metadata crítica duplicada, secciones de control especiales/ambiguas y miembros excesivos. No extrae el payload al sistema de archivos ni lo declara verificado por el ejecutor nativo: `payload_tar_checked=false` y `native_apk_verify_pending=true` permanecen explícitos. Los scripts de instalación sólo se enumeran; nunca se ejecutan.

El fixture de pruebas es el metapaquete oficial, sin archivos de aplicación, [build-base 0.5-r4](https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/build-base-0.5-r4.apk), SHA-256 `3ac72e74e96150995e8d797b6c33fc3b46d69e3667c47bd0ff25ce89f9fc4eaf`, junto con la [clave pública oficial 6165ee59](https://alpinelinux.org/keys/alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub), SHA-256 `207e4696d3c05f7cb05966aee557307151f1f00217af4143c1bcaf33b8df733f`. No hay una clave privada de pruebas ni se generó credencial alguna.

## Clausura conservadora

La expansión trabaja únicamente con registros de índices autenticados y el inventario base cuyo SHA-256 está fijado en el manifiesto. Conserva paquetes y proveedores ya instalados, resuelve dependencias transitivas y `install_if`, y comprueba conflictos. Rechaza proveedores ausentes/ambiguos, cambios de versión de la base, Git y capas como gcompat. No ejecuta expresiones de los metadatos.

No sustituye al solver APK. La comparación implementa igualdades exactas y comparaciones numéricas acotadas, incluido el selector observado `python3~3.14`. Otras formas requieren resolución nativa revisada en vez de una aproximación silenciosa. El recibo conserva `native_solver_confirmation_pending=true`: antes de instalar hay que contrastar la simulación nativa sobre la misma base y los mismos APK firmados, sin red ni cambios inesperados.

La inspección real autenticó ambos índices fijados, `main` y `community`, con la clave 6165ee59 y sus hashes SHA-256 originales. Todos los pins directos del SDK pertenecen a `main`; sólo ese repositorio se usa para la clausura. Community contiene versiones alternativas de paquetes ajenos al SDK y no se habilita para resolverlas automáticamente.

La clausura candidata añade **65 paquetes** y reutiliza **20 proveedores de la base**. Requiere el pin suplementario explícito `isl26=0.26-r2`: `isl25` e `isl26` publican `so:libisl.so.23`; `isl-dev` del índice fija la familia `isl26`. Este pin se guardará en un lock separado para revisión/respaldo, sin alterar el manifiesto original.

La dependencia `llvm21-dev` incorpora herramientas de pruebas y, transitivamente, Python 3 y `sqlite-libs 3.53.4-r0`. Eso pertenece al SDK candidato y debe permanecer visible en inventario y auditoría. Esta unidad no acredita SCA cero ni que esa SQLite sea la biblioteca seleccionada para el módulo final; la propuesta corregida del módulo sigue siendo SQLite 3.54.1 por pkg-config.

Evidencias recuperables: `.tools/evidence/phase4-postgres-immich-apk-index-closure.json` y `.tools/postgres-immich-build/apk-sdk-inspection/apk-closure-candidate.json`. Los índices están autenticados; las firmas de los 65 APK y los archivos del SDK todavía deben verificarse durante el staging. No se instaló ni ejecutó SDK, Cargo o compiladores y el build continúa deshabilitado.
