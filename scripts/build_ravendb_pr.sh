#!/usr/bin/env bash
# scripts/build_ravendb_pr.sh
#
# Build a RavenDB .deb (Studio included) from a ravendb/ravendb PR. Outputs
# <repo_root>/builds/raven-pr<N>.deb for install_ravendb.yml's custom_build param.
#
# Usage:
#   scripts/build_ravendb_pr.sh <pr-number>
#
# Example:
#   scripts/build_ravendb_pr.sh 22875
#   ansible-playbook playbooks/install_ravendb.yml -K \
#       -e custom_build=$PWD/builds/raven-pr22875.deb --skip-tags download
#
# What it does:
#   Stage 1 -- compile source to a tarball (needs pwsh, .NET SDK, node).
#   Stage 2 -- wrap the tarball into a .deb (containerized).
#
# Prerequisites: git, docker (daemon up), pwsh, dotnet, node, npm

set -euo pipefail

# helpers

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    red "MISSING: $cmd"
    return 1
  fi
  green "  ok: $cmd"
  return 0
}

check_prereqs() {
  echo "==> checking prerequisites"
  local missing=0

  require_cmd git    || missing=1
  require_cmd docker || missing=1
  require_cmd pwsh   || missing=1
  require_cmd dotnet || missing=1
  require_cmd node   || missing=1
  require_cmd npm    || missing=1

  if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    red "MISSING: docker daemon not reachable"
    missing=1
  fi

  if command -v dotnet >/dev/null 2>&1 && ! dotnet --list-sdks 2>/dev/null | grep -q .; then
    red "MISSING: .NET SDK (only the runtime is installed)"
    missing=1
  fi

  if [ "$missing" -ne 0 ]; then
    red "==> prerequisite check failed"
    exit 1
  fi
  green "==> prerequisites ok"
  echo
}

# main

PR="${1:-}"
if [ -z "$PR" ]; then
  echo "Usage: $0 <pr-number>" >&2
  exit 2
fi

check_prereqs

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILDS_DIR="${REPO_ROOT}/builds"
WORKDIR="${HOME}/.cache/ravendb-build/ravendb"
TEMP_DIR="${HOME}/.cache/ravendb-build/cache"
OUTPUT_DIR="${HOME}/.cache/ravendb-build/dist"
FINAL="${BUILDS_DIR}/raven-pr${PR}.deb"

mkdir -p "$BUILDS_DIR"

# clone / fetch / checkout PR
if [ ! -d "$WORKDIR/.git" ]; then
  echo "==> cloning ravendb/ravendb (first run, takes a minute)"
  git clone https://github.com/ravendb/ravendb.git "$WORKDIR"
fi
cd "$WORKDIR"
git fetch origin "refs/pull/${PR}/head"
git checkout -B "pr-${PR}" FETCH_HEAD
git submodule update --init --recursive 2>/dev/null || true
HEAD_SHA=$(git rev-parse --short HEAD)
green "==> PR #${PR} checked out at $HEAD_SHA"
echo

# post-clone prereq check: required .NET SDK from global.json
if [ -f "global.json" ]; then
  REQUIRED_SDK=$(grep -oP '"version"\s*:\s*"\K[0-9.]+' global.json | head -1 || true)
  if [ -n "$REQUIRED_SDK" ]; then
    REQUIRED_MAJOR="${REQUIRED_SDK%%.*}"
    if dotnet --list-sdks 2>/dev/null | awk '{print $1}' | grep -q "^${REQUIRED_SDK}$"; then
      green "==> .NET SDK ${REQUIRED_SDK} present (required by global.json)"
    elif dotnet --list-sdks 2>/dev/null | awk '{print $1}' | grep -q "^${REQUIRED_MAJOR}\."; then
      yellow "==> .NET SDK ${REQUIRED_SDK} not exactly present, but ${REQUIRED_MAJOR}.x SDKs exist:"
      dotnet --list-sdks | sed 's/^/    /'
      yellow "    the build will try the closest match; if it fails, install exactly ${REQUIRED_SDK}"
    else
      red "MISSING: .NET SDK ${REQUIRED_SDK} (required by global.json)"
      red "  installed SDKs:"
      dotnet --list-sdks 2>/dev/null | sed 's/^/    /' >&2 || echo "    (none)" >&2
      exit 1
    fi
  fi
fi
echo

# Stage 1: source build (tarball, Studio included)
yellow "==> Stage 1 -- compiling RavenDB + Studio from source (this is the slow part)"
./build.sh -LinuxX64

TARBALL=$(find artifacts -maxdepth 2 -name 'RavenDB-*-linux-x64.tar.bz2' 2>/dev/null | head -1 || true)
if [ -z "$TARBALL" ]; then
  red "ERROR: Stage 1 finished but no RavenDB-*-linux-x64.tar.bz2 under artifacts/"
  find artifacts -maxdepth 2 2>/dev/null | sed 's/^/  /'
  exit 3
fi

FILENAME=$(basename "$TARBALL")
RAVENDB_VERSION="${FILENAME#RavenDB-}"
RAVENDB_VERSION="${RAVENDB_VERSION%-linux-x64.tar.bz2}"
green "==> Stage 1 produced: $TARBALL  (version: $RAVENDB_VERSION)"
echo

# Place the tarball where the deb-builder expects it.
mkdir -p "$TEMP_DIR" "$OUTPUT_DIR"
EXPECTED="$TEMP_DIR/ravendb-${RAVENDB_VERSION}-linux-x64.tar.bz2"
cp -v "$TARBALL" "$EXPECTED"

if ! tar tjf "$EXPECTED" | grep -q '^RavenDB/runtime.txt$'; then
  yellow "==> RavenDB/runtime.txt missing from tarball; injecting from runtimeconfig"
  PATCH_WORK=$(mktemp -d)
  tar xjf "$EXPECTED" -C "$PATCH_WORK"
  RUNTIME_VER=$(grep -oP '"version"\s*:\s*"\K[^"]+' \
    "$PATCH_WORK/RavenDB/Server/Raven.Server.runtimeconfig.json" 2>/dev/null | head -1 || true)
  if [ -z "$RUNTIME_VER" ]; then
    red "ERROR: could not read framework.version from Raven.Server.runtimeconfig.json"
    exit 5
  fi
  echo ".NET Core Runtime: ${RUNTIME_VER}" > "$PATCH_WORK/RavenDB/runtime.txt"
  (cd "$PATCH_WORK" && tar cjf "$EXPECTED" RavenDB)
  rm -rf "$PATCH_WORK"
  green "==> injected runtime.txt: .NET Core Runtime: ${RUNTIME_VER}"
fi
echo

# Stage 2: deb wrap (containerized)
yellow "==> Stage 2 -- wrapping into .deb via ubuntu_native.Dockerfile"
cd "$WORKDIR/scripts/linux/pkg/deb"

source ./set-ubuntu-jammy.sh                     # DISTRO_NAME=ubuntu, DISTRO_VERSION=22.04, DISTRO_VERSION_NAME=jammy
source ./set-raven-platform-amd64.sh             # RAVEN_PLATFORM=linux-x64, DOCKER_BUILDPLATFORM=linux/amd64, DEB_ARCHITECTURE=amd64

export RAVENDB_VERSION TEMP_DIR OUTPUT_DIR

./build-deb.sh

# copy out
DEB=$(find "$OUTPUT_DIR/${DISTRO_VERSION}" -maxdepth 1 -name "ravendb_*_amd64.deb" 2>/dev/null | head -1 || true)
if [ -z "$DEB" ]; then
  red "ERROR: Stage 2 produced no .deb under $OUTPUT_DIR/${DISTRO_VERSION}/"
  find "$OUTPUT_DIR" 2>/dev/null | sed 's/^/  /'
  exit 4
fi

cp "$DEB" "$FINAL"
echo
green "==> DONE  ${FINAL}  (PR #${PR}, ${HEAD_SHA}, ${RAVENDB_VERSION})"
echo
echo "Use it:"
echo "  ansible-playbook playbooks/install_ravendb.yml -K \\"
echo "      -e custom_build=$FINAL --skip-tags download"
