# Base oficial auditada el 2026-11-06; build y comprobación Linux propios pendientes.
FROM python:3.12.16-alpine3.24@sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918

WORKDIR /app
COPY --chown=65532:65532 proxy.py /app/proxy.py
USER 65532:65532

HEALTHCHECK --interval=30s --timeout=7s --start-period=10s --retries=3 CMD ["python", "-I", "/app/proxy.py", "--healthcheck"]
CMD ["python", "-I", "/app/proxy.py"]
