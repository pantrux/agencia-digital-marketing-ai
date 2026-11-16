# Consulta de avisos pip y vendorizados

El scanner acepta sólo el inventario preparado y fijado de 19 hipótesis PyPI.
Revalida recibo, ACL, marcador, dev/ino, SBOM, Trivy y DB antes y después. No
ejecuta pip ni copia dependencias dentro de un intérprete. La consulta es offline
con DB vigente; el proceso tiene plazo, límite de salida y Job cerrado propios.

Root respalda plan/fuentes/ruta antes del flag explícito de ejecución. Un
namespace existente no se adopta. Reporte, diagnósticos y fallos se conservan.
La validación exige nombre/versión/PURL para las 19 hipótesis exactamente una
vez. Cero paquetes analizados jamás equivale a cero avisos. HIGH/CRITICAL,
UNKNOWN y todas las demás severidades permanecen en el reporte bruto.

Las versiones vendor.txt son declaraciones de copias modificadas: incluso una
consulta completa sin avisos mantiene identity_complete=false y
runtime_approved=false. La aplicabilidad requiere revisión aparte, especialmente
el subconjunto pkg_resources de setuptools. La auditoría no cubre la totalidad
del runtime Python ni reemplaza la SCA de sus 19+14 componentes nativos.

```powershell
python -B docker/python-runtime/scan_pip_vendors.py
python -B docker/python-runtime/scan_pip_vendors.py --execute --expected-plan-sha256 SHA_RESPALDADO
python -B -m unittest discover -s tests -p test_python_pip_vendor_sca.py -v
```
