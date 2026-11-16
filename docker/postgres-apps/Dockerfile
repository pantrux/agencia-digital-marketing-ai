# PostgreSQL para aplicaciones; base linux/amd64 inspeccionada por digest.
FROM docker.io/library/postgres:16.15-alpine3.24@sha256:ac838714b7d794185a866e5a6b479b954e7d8a070568aa0acac6fe07c3070bf5

LABEL org.opencontainers.image.title="PostgreSQL para aplicaciones de la agencia" \
      org.opencontainers.image.version="16.15-agencia.1" \
      cl.e-scale.agencia.owner="agencia-digital-marketing-ai:postgres-apps:v4.1"

# Se verifican los bytes reales antes de retirar el elevador innecesario.
# Los scripts oficiales, la base de paquetes y su historial se conservan.
RUN set -eu; \
    test "$(id -u postgres)" = '70'; \
    test "$(id -g postgres)" = '70'; \
    test -f /usr/local/bin/gosu; \
    test ! -L /usr/local/bin/gosu; \
    test -L /usr/local/bin/su-exec; \
    test "$(readlink /usr/local/bin/su-exec)" = 'gosu'; \
    printf '%s  %s\n' \
      '9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5' /usr/local/bin/docker-entrypoint.sh \
      '922ade6b23a312e65023349b36e873752b15ff38ae4c0ae461d74451e104f312' /usr/local/bin/docker-ensure-initdb.sh \
      '52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0' /usr/local/bin/gosu \
      | sha256sum -c -; \
    rm /usr/local/bin/gosu /usr/local/bin/su-exec; \
    test ! -e /usr/local/bin/gosu; test ! -L /usr/local/bin/gosu; \
    test ! -e /usr/local/bin/su-exec; test ! -L /usr/local/bin/su-exec; \
    printf '%s  %s\n' \
      '9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5' /usr/local/bin/docker-entrypoint.sh \
      '922ade6b23a312e65023349b36e873752b15ff38ae4c0ae461d74451e104f312' /usr/local/bin/docker-ensure-initdb.sh \
      | sha256sum -c -

# Evita la rama del entrypoint que necesita gosu y el cambio de dueño al arrancar.
USER 70:70
