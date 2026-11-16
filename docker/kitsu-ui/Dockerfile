FROM wolfi
USER 0:0
ENV PATH=/usr/sbin:/usr/bin:/sbin:/bin

RUN --network=none --mount=type=bind,source=apk-inputs,target=/apk-inputs,ro \
    set -eu; \
    cd /apk-inputs; \
    sha256sum -c SHA256SUMS; \
    inventory() { \
        awk '/^P:/{name=substr($0,3)} /^V:/{print name "=" substr($0,3)}' /usr/lib/apk/db/installed | LC_ALL=C sort; \
    }; \
    before="$(inventory)"; \
    test "$(printf '%s\n' "$before" | wc -l)" -eq 19; \
    set -- \
        nginx-mainline=1.31.8-r1 \
        nginx-mainline-config=1.31.8-r1 \
        nginx-mainline-fastcgi-params=1.31.8-r1 \
        nginx-mainline-mime-types=1.31.8-r1 \
        nginx-mainline-scgi-params=1.31.8-r1 \
        nginx-mainline-uwsgi-params=1.31.8-r1 \
        libpcre2-8-0=10.49-r3; \
    /sbin/apk --no-network --no-scripts --no-commit-hooks --no-cache \
        --repositories-file /apk-inputs/repositories --keys-dir /apk-inputs/keys add "$@"; \
    expected="$( { printf '%s\n' "$before"; printf '%s\n' "$@"; } | LC_ALL=C sort)"; \
    test "$(inventory)" = "$expected"; \
    test -x /usr/bin/nginx; \
    test -f /etc/nginx/mime.types

COPY --chown=0:0 --chmod=0444 nginx.conf /etc/nginx/kitsu.conf
COPY --chown=0:0 dist/ /opt/kitsu/dist/
USER 1000:1000
EXPOSE 8081
STOPSIGNAL SIGQUIT
ENTRYPOINT ["/usr/bin/nginx", "-e", "stderr", "-c", "/etc/nginx/kitsu.conf"]
