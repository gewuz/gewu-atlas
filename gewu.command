#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$ROOT/.runtime/install-env/venv/bin/python" -I -B "$ROOT/gewu" "$@"
