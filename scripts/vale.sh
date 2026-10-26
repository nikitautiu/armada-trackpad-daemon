#!/usr/bin/env bash
# Lint the docs with Vale in a pinned container that sees only this repository.
#
#   scripts/vale.sh sync         # download the styles in .vale.ini (the only command with network access)
#   scripts/vale.sh              # check the tracked Markdown files
#   scripts/vale.sh FILE...      # check these files (options such as --output=line go first)
set -euo pipefail

IMAGE="docker.io/jdkato/vale:v3.24.0@sha256:f5a09410093936d4919d868120786da9789e2652ac321c66a895403eca020ae4"
repo="$(cd "$(dirname "$0")/.." && pwd)"

network=none
if [[ "${1:-}" == sync ]]; then
  network=slirp4netns
elif [[ $# -eq 0 || "${!#}" == -* ]]; then
  mapfile -t files < <(git -C "$repo" ls-files '*.md')
  set -- "$@" "${files[@]}"
fi

exec podman run --rm \
  --userns=keep-id \
  --network "$network" \
  --volume "$repo:/work:z" \
  --workdir /work \
  "$IMAGE" "$@"
