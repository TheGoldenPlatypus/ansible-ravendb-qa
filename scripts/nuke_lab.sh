#!/usr/bin/env bash
# scripts/nuke_lab.sh -- host-wide reset of the chaos lab on this docker daemon.
#
# Wipes every lab container, network, and volume on this docker daemon,
# regardless of which run owns it. Unrelated containers/networks/volumes are
# left alone. Match patterns:
#   containers: name matches ^[0-9]+[a-z]$  (e.g. 1a, 4b, 9i)
#   networks:   rv1net, rp1net, rpv1net, hubsinknet
#   volumes:    anything starting with lab_backups
#
# Usage:
#   scripts/nuke_lab.sh           # quiet mode
#   scripts/nuke_lab.sh -v        # verbose

set -euo pipefail

VERBOSE=0
if [ "${1:-}" = "-v" ]; then VERBOSE=1; fi

log() { [ "$VERBOSE" = 1 ] && echo "$@" || true; }
say() { echo "$@"; }

# 1. kill background workloads on the controller
say "==> killing leftover workload processes + pidfiles"
pkill -f '[w]orkload_w[0-9]*\.sh' 2>/dev/null || true
rm -f /tmp/w[0-9]*-*.pid /tmp/w[0-9]*-*.log 2>/dev/null || true

# 2. remove every container matching the lab naming pattern
say "==> removing lab containers (name pattern: <digit><letter>)"
LAB_CONTAINERS="$(docker ps -aq --filter 'name=^[0-9]+[a-z]$' 2>/dev/null || true)"
if [ -n "$LAB_CONTAINERS" ]; then
  log "    candidates:"
  [ "$VERBOSE" = 1 ] && docker ps -a --filter 'name=^[0-9]+[a-z]$' --format '    {{.Names}} ({{.Status}})'
  docker rm -f $LAB_CONTAINERS >/dev/null 2>&1 || true
  say "    removed $(echo "$LAB_CONTAINERS" | wc -w) container(s)"
else
  say "    none found"
fi

# 3. remove the lab's docker networks
say "==> removing lab networks"
for net in rv1net rp1net rpv1net hubsinknet; do
  if docker network inspect "$net" >/dev/null 2>&1; then
    docker network rm "$net" >/dev/null 2>&1 && say "    removed $net" || say "    failed to remove $net (still in use?)"
  fi
done

# 4. remove the lab's backup volumes
say "==> removing lab volumes (lab_backups*)"
LAB_VOLS="$(docker volume ls -q | grep '^lab_backups' || true)"
if [ -n "$LAB_VOLS" ]; then
  for v in $LAB_VOLS; do
    docker volume rm "$v" >/dev/null 2>&1 && log "    removed $v" || say "    failed to remove $v (still in use?)"
  done
  say "    removed $(echo "$LAB_VOLS" | wc -w) volume(s)"
else
  say "    none found"
fi

# 5. strip the /etc/hosts block (requires sudo)
if grep -q 'ANSIBLE MANAGED CHAOS LAB' /etc/hosts 2>/dev/null; then
  say "==> stripping /etc/hosts block (requires sudo)"
  sudo sed -i '/# BEGIN ANSIBLE MANAGED CHAOS LAB/,/# END ANSIBLE MANAGED CHAOS LAB/d' /etc/hosts
fi

# 6. wipe captures/ at repo root
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$REPO_ROOT/captures" ]; then
  say "==> wiping $REPO_ROOT/captures/"
  rm -rf "$REPO_ROOT/captures"
fi

say "==> done"
say
docker ps -a --format 'remaining containers: {{.Names}} ({{.Status}})' | head -10
echo "    networks: $(docker network ls --format '{{.Name}}' | grep -E '^(rv1net|rp1net|rpv1net|hubsinknet)$' | wc -l)"
echo "    volumes:  $(docker volume ls -q | grep -c '^lab_backups' || true)"
