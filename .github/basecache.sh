#!/usr/bin/env bash
# Digest-pinned base images held in an OCI layout so builds never resolve them
# against a registry.
#   basecache.sh key            cache key over every pinned FROM
#   basecache.sh fetch          copy the pinned images into the layout
#   basecache.sh up             start buildkitd from the layout, print build contexts
set -euo pipefail
dir=${BASECACHE:-$HOME/basecache}
root=$(dirname "$0")/..
pins() { sed -nE 's/^FROM[[:space:]]+([^[:space:]]+@sha256:[0-9a-f]{64}).*/\1/p' "$@" | sort -u; }
all() { pins "$root/Dockerfile" "$root/.github/buildkit/Dockerfile"; }
src() { local n=${1%@*}; echo "docker://${n%:*}@${1#*@}"; }
case $1 in
key) echo "basecache-$(all | sha256sum | cut -c1-16)" ;;
fetch)
  for r in $(all); do
    skopeo copy -q --retry-times 5 --multi-arch system "$(src "$r")" "oci:$dir:${r##*:}"
  done
  ;;
up)
  bk=$(pins "$root/.github/buildkit/Dockerfile")
  skopeo copy -q "oci:$dir:${bk##*:}" docker-daemon:buildkitd:local
  docker run -d --name buildkitd --privileged buildkitd:local >/dev/null
  docker buildx create --use --driver remote docker-container://buildkitd >/dev/null
  for r in $(pins "$root/Dockerfile"); do echo "$r=oci-layout://$dir:${r##*:}"; done
  ;;
esac
