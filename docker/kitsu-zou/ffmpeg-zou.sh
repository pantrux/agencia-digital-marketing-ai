#!/bin/sh
exec /lib64/ld-linux-x86-64.so.2 --library-path /opt/zou-media-v1/lib \
    /opt/zou-media-v1/bin/ffmpeg "$@"
