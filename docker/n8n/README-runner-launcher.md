# Launcher del sidecar n8n 2.46.6

La entrada conserva el launcher 1.5.1 del commit `871cb76bc35c990f368eaa788f0b4ce7b145e143`. El único cambio en la fuente preparada está en `go.mod` y `go.sum`: Go 1.27.3 y `golang.org/x/text v0.41.0`. Se preservan las 30 fuentes Go, los dos textos de licencia y el contrato de la configuración de runners del release n8n 2.46.6. [Launcher fijado](https://github.com/n8n-io/task-runner-launcher/tree/871cb76bc35c990f368eaa788f0b4ce7b145e143) · [Corrección de `x/text`](https://vuln.go.dev/ID/GO-2026-5970.json)

## Preparación de las entradas

`stage_runner_launcher.py` usa el TAR original fijado en `remediation-inputs.json`. Requiere la ruta absoluta de `curl` previamente verificado. Descarga `.info`, `.mod` y `.zip` del proxy oficial por HTTPS y sin redirecciones. Para cada `.mod` y `.zip` compara el `h1` calculado con el `go.sum` original o, para `x/text`, con las dos sumas del registro público ya fijado. Comprueba además el SHA-256 del `.mod` de `x/text`, la versión de `.info` y UTF-8 en las fuentes compilables. La lista y el SHA-256 de cada archivo quedan fijados en `SHA256SUMS`. Las sumas del registro son entradas autenticadas por el inventario previo. El preparador calcula `h1` de manera independiente y no implementa la verificación criptográfica del árbol SumDB. [Formato oficial de módulos y sumas](https://go.dev/ref/mod#authenticating)

```powershell
python -B docker/n8n/stage_runner_launcher.py --archive <tar-original> --curl C:/Windows/System32/curl.exe --output <contexto-nuevo>
```

La preparación publica una raíz nueva y rechaza sobrescrituras. El contexto contiene 20 pares de módulo/versión y 18 ZIP. Algunos pertenecen a pruebas o transitivas y el inventario de la compilación debe obtenerse del binario.

## Compilación Linux amd64

Antes de transferir, cotejar y respaldar el manifiesto de contexto, el TAR de origen y el SDK Linux oficial Go 1.27.3. El TAR del SDK pesa 70.588.678 bytes y tiene SHA-256 `65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40`. Comprobar la extracción del SDK en una raíz propia. Transferir a un contexto nuevo sólo los archivos regulares listados y `SHA256SUMS`, mantenerlo de sólo lectura y usar un directorio de salida nuevo. [Descargas oficiales de Go](https://go.dev/dl/)

```sh
bash /ruta/propia/contexto/build-runner-launcher.sh /ruta/propia/contexto /ruta/propia/go /ruta/propia/salida-nueva
```

La receta usa el ejecutable absoluto del SDK, un entorno vacío de credenciales, un `PATH` privado vacío y cachés nuevas. Con `GOPROXY` apuntando al contexto local, `GOTOOLCHAIN=local`, `GOVCS=*:off` y `-buildvcs=false`, Go compila `./cmd/launcher` para Linux amd64 con `CGO_ENABLED=0`. Una dependencia ausente hace fallar la compilación. Esta receta consume el proxy local directamente y conserva el archivo `go.sum` en modo de sólo lectura. El operador debe aplicar un límite de tiempo y recursos y mantener el proceso sin salida de red. La receta entrega el binario, `buildinfo.txt` y sus hashes.

La aceptación posterior necesita verificar la información de compilación y auditar el binario concreto y la imagen ensamblada. El sidecar espera el ejecutable en `/usr/local/bin/task-runner-launcher` y lo inicia mediante `tini`. Al integrar, fijar el binario como `root:root` con modo `0555` para el UID del runner y conservar los avisos de licencia aplicables. Debe probarse contra un broker controlado con los runners Python y JavaScript y la configuración original. El SDK se usa como herramienta acotada. Su inventario independiente y los riesgos conocidos siguen vigentes. [Dockerfile upstream fijado](https://github.com/n8n-io/n8n/blob/bb72d756104542adb549b6966a57e21459bd52a2/docker/images/runners/Dockerfile)
