# Candidato de construcción: misma base Alpine fijada del proxy ML.
# Sólo copia de seis fuentes; sin instalación, red de RUN ni secreto.
FROM python:3.12.16-alpine3.24@sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918
COPY --chown=0:0 --chmod=0555 r2_client.py sync_http.py sync_ledger.py sync_protocol.py sync_runtime.py sync_storage.py /app/
WORKDIR /app
USER 65532:65532
ENTRYPOINT ["python", "-I", "-B", "-S", "/app/sync_http.py", "serve"]
CMD []
