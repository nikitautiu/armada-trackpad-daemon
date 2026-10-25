#!/bin/sh
# Development: copy this checkout to a device over ssh and run install.sh there.
#   dev/deploy.sh HOST
set -eu
if [ $# -ne 1 ]; then
    echo "usage: dev/deploy.sh HOST" >&2
    exit 2
fi
host=$1
root=$(dirname "$(readlink -f "$0")")/..
# --delete-excluded also clears caches copied before they were excluded. The device's venv lives
# outside this tree (~/.local/share/armada-trackpad-daemon), so nothing it needs is touched.
rsync -a --delete --delete-excluded --exclude .venv --exclude .git --exclude __pycache__ \
    --exclude .pytest_cache --exclude .ruff_cache --exclude .mypy_cache "$root/" "$host:armada-trackpad-daemon/"
ssh "$host" armada-trackpad-daemon/install.sh --bootstrap-uv
