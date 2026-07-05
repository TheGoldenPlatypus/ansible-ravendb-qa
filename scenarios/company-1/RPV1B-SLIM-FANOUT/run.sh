#!/usr/bin/env bash
# scenarios/company-1/RPV1B-SLIM-FANOUT/run.sh -- teardown + fan-out lab @ v_new + rpv1b_slim_fanout.yml.
# Usage: run.sh <v_new_deb> [cluster_id_start=1] [docker_network=hubsinknet] [-e foo=bar ...]
#        SINK_COUNT=N  (env, default 20)  -- number of 3-node sink clusters
#
# RPV-1B-SLIM-FANOUT is the fan-out variant of RPV-1B-SLIM: 1 hub + N sinks (default 20),
# each a 3-node cluster, all v_new from the start, NO rolling upgrade.  Hub sits at
# cluster id <cid>; sinks occupy <cid+1>..<cid+SINK_COUNT>.  (1+SINK_COUNT)*3 nodes total.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1  # CPython 3.12.0-3.12.3 marshal bug

# Pull positionals up to the first -flag; everything from there on is forwarded -e overrides.
POS=()
while [ $# -gt 0 ]; do case "$1" in -*) break;; *) POS+=("$1"); shift;; esac; done
EXTRA=("$@")

V_NEW="${POS[0]:-}"; CID="${POS[1]:-1}"; NET="${POS[2]:-hubsinknet}"
SINK_COUNT="${SINK_COUNT:-20}"

[ -n "$V_NEW" ] || { echo "Usage: $0 <v_new_deb> [cid=1] [net=hubsinknet] [-e foo=bar ...]   (SINK_COUNT env, default 20)"; exit 2; }
[[ "$SINK_COUNT" =~ ^[0-9]+$ ]] && [ "$SINK_COUNT" -ge 1 ] || { echo "ERROR: SINK_COUNT must be a positive integer (got '$SINK_COUNT')"; exit 2; }

cd "$(dirname "$0")/../../.."
[[ "$V_NEW" = /* ]] || V_NEW="$PWD/$V_NEW"
[ -f "$V_NEW" ] || { echo "ERROR: $V_NEW not found"; exit 3; }

[ -z "${ANSIBLE_BECOME_PASS:-}" ] && { read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo; export ANSIBLE_BECOME_PASS; }

HUB="$CID"
CLUSTERS_COUNT=$(( 1 + SINK_COUNT ))          # 1 hub + N sinks
SINK_LAST=$(( CID + SINK_COUNT ))             # last sink cluster id (for the banner)

# Per-iter controller-side host port for ConsistencyCheck's state-store container.
# Default 8084 collides on parallel iters; derive from CID so each iter binds a
# unique slot (gap of 10 leaves headroom).
PORT_OFFSET=$(( (CID / 100) * 10 ))
CC_STATE_PORT=$(( 8084 + PORT_OFFSET ))

C=( -e cluster_id_start="$CID" -e hub_id="$HUB" -e sink_count="$SINK_COUNT"
    -e docker_network_name="$NET" -e backups_volume_name="lab_backups_$NET"
    -e cc_state_port="$CC_STATE_PORT" )
# Overnight runner sets DOCKER_NETWORK_SUBNET to pin container IPs so post-mortem
# `docker start` returns each container to its original IP.  Single-scenario runs
# leave it unset and fall back to Docker auto-assign.
# NOTE: the pinned-IP formula (cluster_id mod 8) collides past 8 clusters, so this
# fan-out scenario is NOT overnight-harness safe as-is -- run it one-off (IPs auto-assign).
[ -n "${DOCKER_NETWORK_SUBNET:-}" ] && C+=( -e docker_network_subnet="$DOCKER_NETWORK_SUBNET" )

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RPV-1B-SLIM-FANOUT  hub=${HUB}a/b/c  sinks=${SINK_COUNT} (${HUB}+1..${SINK_LAST})  net=$NET  (all v_new, no upgrade)"

step "teardown";     ansible-playbook playbooks/teardown_containers.yml "${C[@]}" -e clusters_count="$CLUSTERS_COUNT"
step "provision";    ansible-playbook playbooks/provision_nodes.yml     "${C[@]}" -e clusters_count="$CLUSTERS_COUNT" -e nodes_per_cluster=3
step "install";      ansible-playbook playbooks/install_ravendb.yml     "${C[@]}" -e custom_build="$V_NEW" --skip-tags download
step "form";         ansible-playbook playbooks/form_clusters.yml       "${C[@]}" -e clusters_count="$CLUSTERS_COUNT" -e nodes_per_cluster=3
step "rpv1b_fanout"; ansible-playbook scenarios/company-1/RPV1B-SLIM-FANOUT/rpv1b_slim_fanout.yml "${C[@]}" "${EXTRA[@]}"

step "RPV-1B-SLIM-FANOUT done"
