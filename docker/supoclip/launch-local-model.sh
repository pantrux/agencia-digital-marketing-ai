#!/bin/sh
set -eu

weights=/run/supoclip-model/qwen.gguf
test "$(id -u):$(id -g)" = 1000:1000
test "$#" = 1 && test "$1" = serve
test -f "$weights" && test ! -L "$weights"
test "$(stat -c %s "$weights")" = 1117320736
printf '%s  %s\n' '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e' "$weights" \
    | sha256sum -c -
cd /opt/supoclip-model
export LD_LIBRARY_PATH=/opt/supoclip-model
exec ./llama-server --model "$weights" --host 127.0.0.1 --port 18963 \
    --ctx-size 8192 --threads 3 --parallel 1 --n-gpu-layers 0 \
    --alias qwen-supoclip --no-webui --jinja
