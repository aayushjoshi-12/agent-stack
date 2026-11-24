#!/bin/bash
# Build the linux/amd64 agent-stack environment once (slow under emulation on Apple silicon) and
# save it as a tarball the Harbor adapter uploads into each task container.
#   bench/prebuild.sh <output.tar>
set -euo pipefail
out=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
repo=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
git -C "$repo" archive -o "$tmp/src.tar" HEAD src pyproject.toml uv.lock README.md
docker run --rm --platform linux/amd64 -v "$tmp":/work ubuntu:24.04 bash -c '
  set -euo pipefail; export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
  mkdir -p /opt/agent-stack && tar -xf /work/src.tar -C /opt/agent-stack
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
  cd /opt/agent-stack && UV_PYTHON_INSTALL_DIR=/opt/agent-stack/python uv sync --frozen --no-dev --python 3.12
  cp /usr/local/bin/uv /opt/agent-stack/uv
  chmod -R a+rX /opt/agent-stack
  tar -C /opt -czf /work/env.tar.gz agent-stack'
mv "$tmp/env.tar.gz" "$out"; rm -rf "$tmp"; echo "built $out"
