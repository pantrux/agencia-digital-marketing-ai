# Inventario del pip incluido como dato

El preparador Python conserva el wheel que exige su paquete Wolfi. Este módulo
lee sus bytes sin instalarlo ni importar pip o sus dependencias. Comprueba CRC,
rutas, tipos, límites, ausencia de nombres duplicados y cada hash/tamaño RECORD.
METADATA identifica pip; vendor.txt declara versiones de copias vendorizadas.

Las declaraciones vendorizadas son hipótesis para consultar avisos, no prueba
de que las copias sean distribuciones PyPI completas sin modificaciones.
En particular, setuptools corresponde al namespace pkg_resources. Se catalogan
sus archivos y licencias reales; no se atribuye automáticamente cualquier aviso
de setuptools al subconjunto ni se elimina un hallazgo sin revisar aplicabilidad.
Los namespaces sin declaración permanecen expresos, y identity_complete es
siempre false. Un resultado sin avisos no prueba identidad ni seguridad completas.

Root debe fijar fuente, contexto, wheel y recibo del preparador, comprobar ACL y
leer archivos regulares sin enlaces antes de llamar al parser. El módulo es puro:
no lee la bóveda, no publica archivos, no consulta servicios ni ejecuta payloads.
Su SBOM es entrada de una SCA posterior; runtime_approved sigue siendo false.

```powershell
python -B -W error::ResourceWarning -m unittest discover -s tests -p test_python_pip_vendor_inventory.py -v
```
