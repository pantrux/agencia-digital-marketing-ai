FROM scratch
LABEL org.opencontainers.image.title="Alertmanager de la agencia en Synology" \
      org.opencontainers.image.version="0.35.0-agency.1" \
      org.opencontainers.image.revision="8cf67a8e75004bad6c3c6bacc829945cf8a3ee65" \
      cl.e-scale.agencia.owner="agencia-digital-marketing-ai:alertmanager-nas:v1" \
      cl.e-scale.agencia.alertmanager-sha256="5bab5f99d176e99b8887fceb4a4a6a27580aaa65803cb84a3b46cc66cdc48f2f" \
      cl.e-scale.agencia.amtool-sha256="587aefb218c34c05bdf5be017deeb44feaae01ef9fd6f467e794f39d2d189282"
COPY --chown=0:0 alertmanager /bin/alertmanager
COPY --chown=0:0 amtool /bin/amtool
COPY --chown=0:0 LICENSE NOTICE /licenses/
USER 61211:61211
STOPSIGNAL SIGTERM
ENTRYPOINT ["/bin/alertmanager"]
CMD ["--config.file=/etc/alertmanager/alertmanager.yml", "--web.config.file=/etc/alertmanager/web.yml", "--storage.path=/alertmanager", "--web.listen-address=172.31.254.10:9093", "--cluster.listen-address="]
