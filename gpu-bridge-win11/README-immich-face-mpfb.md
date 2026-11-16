# Corpus sintético de retratos para Immich

Usa Blender portable 4.2 o superior y la extensión MPFB 2.0.18 en un perfil de Blender dedicado. Conserva los SHA-256 del ZIP de Blender, la extensión y el paquete `makehuman_system_assets`. La generación usa el ZIP de recursos directamente y lo extrae en un directorio temporal. Los recursos gráficos proceden del núcleo de MPFB y del paquete CC0. El código de la extensión tiene su propia licencia GPL. [MPFB 2.0.18](https://extensions.blender.org/add-ons/mpfb/versions/), [paquete de recursos](https://static.makehumancommunity.org/assets/assetpacks/makehuman_system_assets.html), [licencia](https://www.makehumancommunity.org/about/license.html)

En Blender 4.5, usa `$profilePath` como ruta absoluta a un perfil dedicado y `$mpfbZip` como ruta al ZIP local verificado. Ejecuta los comandos en una sesión de PowerShell exclusiva para el ensayo y conserva `BLENDER_USER_RESOURCES` para los renders. [Instalación por CLI](https://docs.blender.org/manual/en/4.5/advanced/command_line/extension_arguments.html)

```powershell
$env:BLENDER_USER_RESOURCES = $profilePath
& $blender --offline-mode --command extension repo-list
& $blender --offline-mode --command extension install-file -r user_default -e $mpfbZip
if ($LASTEXITCODE -ne 0) { throw 'Falló la instalación local de MPFB' }
```

## Ejecutar la prueba de dos identidades

Desde la raíz del proyecto, asigna `$blender`, `$assetZip`, `$assetSha` y `$output` al ejecutable, al ZIP verificado, a su SHA-256 en minúsculas y a un directorio nuevo cuyo padre exista.

```powershell
$script = (Resolve-Path -LiteralPath 'gpu-bridge-win11/immich_face_mpfb_corpus.py').Path
& $blender --background --factory-startup --offline-mode --python-exit-code 1 --python $script -- --assets-zip $assetZip --assets-sha256 $assetSha --output $output --count 2
if ($LASTEXITCODE -ne 0) { throw 'Falló el render del corpus MPFB' }
```

Si Blender instaló MPFB en otro repositorio, añade `--addon bl_ext.NOMBRE.mpfb` a los argumentos del script. Usa `--save-scenes` si necesitas una escena `.blend` con sus texturas empacadas por identidad. En caso de fallo puede quedar una salida parcial. `fixtures.json` aparece tras terminar el conjunto y verificar las dimensiones y la ausencia de duplicados. Repite en un directorio nuevo.

La prueba produce 12 PNG RGB de 512 × 512. El script genera cada personaje una sola vez con su semilla fija y cambia cámara e iluminación en las seis vistas. `models/` contiene el preset JSON de MPFB de cada personaje. `fixtures.json` registra la procedencia, las seis poses, la geometría, las texturas y los hashes de los PNG. Conserva los hashes junto al resultado de detección, con la versión y el build de Blender indicados por el manifiesto. [Constructor de MPFB 2.0.18](https://github.com/makehumancommunity/mpfb2/blob/d0a32e57a7f915cb2f2b95410e2117648c7bbb7e/src/mpfb/ui/new_human/randomize/characterbuilder.py)

Comprueba la salida con la [sonda del corpus](immich_face_mpfb_probe.py). El modo `--fixtures-only` verifica los archivos localmente.

```powershell
$manifest = Join-Path $output 'fixtures.json'
$manifestSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifest).Hash.ToLowerInvariant()
python gpu-bridge-win11/immich_face_mpfb_probe.py --manifest $manifest --manifest-sha256 $manifestSha --fixtures-only
```

Una vez revisados los 12 PNG y evaluados con el detector, ejecuta la misma receta con `--count 16` en un directorio nuevo. Se conservan las primeras dos semillas y se producen 96 vistas. Los IDs `mh-001` a `mh-008` quedan fijados para calibración y `mh-009` a `mh-016` para validación. Comprueba las sumas de los dos primeros IDs entre corridas en el mismo equipo y build. Los hashes se registran por corrida porque Blender o la CPU pueden cambiar los bytes del render. El manifiesto sólo cubre el corpus positivo. Los negativos y el uso de la API se especifican en la [aceptación facial de Immich](../docker/immich-server/README-ACEPTACION-FACIAL.md).

Para generar otra cohorte, fija antes de renderizar un JSON UTF-8 con `schema: 1`, un `cohort` nuevo y dos listas de semillas, `calibration` y `validation`. Cada lista admite entre 2 y 16 semillas enteras distintas. El generador rechaza repeticiones y las 16 semillas históricas. Sustituye `--count` por `--seed-manifest <ruta> --seed-manifest-sha256 <sha256>`. El recibo conserva la huella del plan. Coteja las semillas, mallas y PNG de la cohorte nueva con todos los ensayos anteriores antes de medir.

## Medición del 13 de noviembre de 2026

El lote de 96 vistas generó el manifiesto `89a749b4…`. Las primeras dos identidades conservaron los mismos presets, mallas y texturas que el lote de 12 vistas. Los 12 PNG cambiaron de hash entre corridas. La sonda verificó los PNG contra el manifiesto de la corrida completa.

La calibración eligió `minScore=0.60`, `maxDistance=0.30` y `minFaces=3`. La validación separada detectó una cara en 47 de 48 retratos. A esa distancia se observaron cero emparejamientos cruzados en 966 pares evaluables y siete rechazos en 115 pares genuinos. Los tres controles sin rostro compartidos tuvieron cero detecciones. Recibo `7867d3fd…`.

La muestra QUB evaluada con esos parámetros detectó 18 de 18 caras, con cero emparejamientos cruzados en 108 pares y 30 rechazos en 45 pares genuinos. En el ensayo aislado de Immich, quedaron asignadas cuatro, cero y cinco fotos por identidad. El candidato no cumplió la condición de un grupo por identidad y el reconocimiento facial de producción sigue desactivado. Los pares comparten identidades y estas cifras describen las muestras sintéticas ensayadas. Recibos `3ddfb0bd…` y `a3b3a853…`.
