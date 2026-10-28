#!/bin/sh
# Watermark one image from input/ with the `watermark` Docker image.
# Usage: watermark_image.sh NAME TEXT [--config FILE]
# NAME is a file in input/. TEXT is the watermark. Reads config.json from this folder. Writes to output/.
set -eu

[ $# -ge 2 ] || { echo "usage: $0 NAME TEXT [--config FILE]" >&2; exit 64; }

root=$(cd "$(dirname "$0")" && pwd)
name=$1
text=$2
shift 2
[ -f "$root/input/$name" ] || { echo "not found: input/$name" >&2; exit 66; }

user=
[ "$(uname)" = Linux ] && user="$(id -u):$(id -g)"

exec docker run --rm ${user:+--user "$user"} \
    -v "$root:/data:ro" -v "$root/output:/data/output" \
    "${WATERMARK_IMAGE:-watermark}" watermark "input/$name" --watermark "$text" --output output "$@"
