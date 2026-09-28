#!/usr/bin/python
"""ravendb_wait -- Ansible module that polls a RavenDB cluster until a convergence condition holds."""

import json
import random
import time
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import (
    request,
    request_per_node,
)
from ansible.module_utils.polling import poll_until


def format_per_node(per_node, indent="  "):
    if not per_node:
        return [indent + "(empty)"]
    width = max(len(name) for name in per_node)
    lines = []
    for name in sorted(per_node):
        lines.append("%s%-*s  %s" % (indent, width, name, per_node[name]))
    return lines


def now_hms():
    return time.strftime("%H:%M:%S")


def classify_nodes(p, nodes):
    """Probe each node's /stats and return (host_map, skipped); raise if any node is unreachable.

    host_map[target] is None for reachable nodes that host the database;
    skipped holds nodes that returned a non-200.
    """
    domain = p["ravendb_domain"]
    db = p["db_name"]
    if not nodes:
        return {}, []

    host_map = {}
    skipped = []
    unreachable = []
    path = "/databases/%s/stats" % db
    results = request_per_node("GET", nodes, domain, path,
                               p["client_cert"], p["ca_cert"])
    for target, status, body in results:
        if status is None:
            unreachable.append("%s: %s" % (target, body))
        elif status == 200:
            host_map[target] = None
        else:
            skipped.append(target)

    if unreachable:
        raise RuntimeError(
            "classify_nodes: %d/%d probe node(s) unreachable: %s -- "
            "can't proceed with a wait on partial coverage (vacuous PASS risk)"
            % (len(unreachable), len(nodes), unreachable))

    return host_map, skipped


def k_leader(p):
    """Wait until the cluster has a leader (matching `expected_leader` tag if given)."""
    target = p["target"]
    timeout = p["timeout"]
    expected = (p["expected_leader"] or "").upper()
    path = "/cluster/topology"

    def predicate():
        status, body = request("GET", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status != 200:
            return False, "HTTP %d" % status
        leader = json.loads(body).get("Leader") or ""
        if not leader:
            return False, "no leader yet"
        if expected and expected not in leader:
            return False, "leader='%s' (waiting for tag '%s')" % (leader, expected)
        return True, leader

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError("TIMEOUT after %.1fs -- %s" % (elapsed, value))

    if expected:
        return ("LEADER elected -- '%s' contains expected tag '%s' "
                "(queried via %s, took %.1fs)" % (value, expected, target, elapsed))
    return "LEADER elected -- '%s' (queried via %s, took %.1fs)" % (
        value, target, elapsed)


def k_member(p):
    """Wait until `target_tag` is a full Member (not Promotable or Rehab) of the database."""
    cluster_leader = p["cluster_leader"]
    db = p["db_name"]
    target = p["target"]
    target_tag = (p["target_tag"] or target[-1]).upper()
    timeout = p["timeout"]
    path = "/admin/databases?name=%s" % db

    def gather(record):
        flat = record.get("Topology") or {}
        return (list(flat.get("Members") or []),
                list(flat.get("Promotables") or []),
                list(flat.get("Rehabs") or []),
                "flat")

    def predicate():
        status, body = request("GET", cluster_leader, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status != 200:
            return False, "HTTP %d" % status
        record = json.loads(body)
        members, promotables, rehabs, kind = gather(record)
        ready = (target_tag in members
                 and target_tag not in promotables
                 and target_tag not in rehabs)
        if ready:
            return True, "[%s] Members=%s" % (kind, members)
        return False, ("[%s] Members=%s Promotables=%s Rehabs=%s" %
                       (kind, members, promotables, rehabs))

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- tag '%s' never became Member; last: %s" %
            (elapsed, target_tag, value))

    return ("REHAB COMPLETE -- node %s (tag %s) is full Member of %s "
            "(via %s, took %.1fs; %s)" % (target, target_tag, db, cluster_leader, elapsed, value))


def k_rehab(p):
    """Wait until `target_tag` enters Promotables or Rehabs of the database."""
    cluster_leader = p["cluster_leader"]
    db = p["db_name"]
    target = p["target"]
    target_tag = (p["target_tag"] or target[-1]).upper()
    timeout = p["timeout"]
    path = "/admin/databases?name=%s" % db

    def predicate():
        status, body = request("GET", cluster_leader, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status != 200:
            return False, "HTTP %d" % status
        topo = json.loads(body).get("Topology") or {}
        promotables = topo.get("Promotables") or []
        rehabs = topo.get("Rehabs") or []
        if target_tag in promotables or target_tag in rehabs:
            return True, "Promotables=%s Rehabs=%s" % (promotables, rehabs)
        members = topo.get("Members") or []
        return False, "still Member; Members=%s" % members

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- tag '%s' never entered rehab "
            "(chaos action may have failed to destabilise the node); last: %s" %
            (elapsed, target_tag, value))

    return ("REHAB STARTED -- node %s (tag %s) entered Promotables/Rehabs on %s "
            "(via %s, took %.1fs)" % (target, target_tag, db, cluster_leader, elapsed))


def snapshot_stats_field(p, nodes_or_map, field):
    """Return {target: field_value} sampling `field` from /stats on every node; raise on any failure."""
    snap = {}
    failures = []
    targets = list(nodes_or_map)
    path = "/databases/%s/stats" % p["db_name"]
    results = request_per_node("GET", targets, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
    for target, status, body in results:
        if status is None:
            failures.append("%s: connection error %s" % (target, body))
            continue
        if status != 200:
            failures.append("%s: HTTP %d" % (target, status))
            continue
        data = json.loads(body)
        if field not in data:
            failures.append("%s: response has no '%s' field" % (target, field))
            continue
        snap[target] = data[field]

    if failures:
        raise RuntimeError(
            "snapshot_stats_field('%s') failed on %d/%d node(s): %s -- "
            "can't proceed with a wait on partial coverage (would silently "
            "PASS via all([])==True against a dead cluster)"
            % (field, len(failures), len(targets), failures))

    if not snap:
        raise RuntimeError(
            "snapshot_stats_field('%s'): 0 nodes contributed data -- "
            "vacuous PASS would be a silent false negative" % field)

    return snap


def k_etag_parity(p):
    """Wait until LastDatabaseEtag stops changing across all nodes that host the database."""
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    prev = None
    prev_time = None

    def predicate():
        nonlocal prev, prev_time
        current = snapshot_stats_field(p, has_db, "LastDatabaseEtag")
        current_time = now_hms()
        if not has_db:
            raise RuntimeError("k_etag_parity: has_db is empty -- vacuous PASS guard")
        if prev is None:
            prev = current
            prev_time = current_time
            return False, (current_time, current, current_time, current)
        stable = all(prev.get(n) == current.get(n) for n in has_db)
        snapshots = (prev_time, prev, current_time, current)
        if stable:
            return True, snapshots
        prev = current
        prev_time = current_time
        return False, snapshots

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    t1, snap1, t2, snap2 = value

    if not done:
        lines = ["TIMEOUT after %.1fs -- LastDatabaseEtag still moving:" % elapsed]
        lines.append("  sample 1 (%s):" % t1)
        lines.extend(format_per_node(snap1, indent="    "))
        lines.append("  sample 2 (%s):" % t2)
        lines.extend(format_per_node(snap2, indent="    "))
        raise RuntimeError("\n".join(lines))

    header = ("STABLE -- LastDatabaseEtag unchanged across %d node(s) "
              "(skipped=%s, took %.1fs)" % (len(has_db), skipped, elapsed))
    lines = [header, "  sample 1 (%s):" % t1]
    lines.extend(format_per_node(snap1, indent="    "))
    lines.append("  sample 2 (%s):" % t2)
    lines.extend(format_per_node(snap2, indent="    "))
    return lines


def k_docs_drain(p):
    """Wait until DatabaseChangeVector stops changing across all nodes that host the database."""
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    prev = None
    prev_time = None

    def predicate():
        nonlocal prev, prev_time
        current = snapshot_stats_field(p, has_db, "DatabaseChangeVector")
        current_time = now_hms()
        if not has_db:
            raise RuntimeError("k_docs_drain: has_db is empty -- vacuous PASS guard")
        if prev is None:
            prev = current
            prev_time = current_time
            return False, (current_time, current, current_time, current)
        stable = all(prev.get(n) == current.get(n) for n in has_db)
        snapshots = (prev_time, prev, current_time, current)
        if stable:
            return True, snapshots
        prev = current
        prev_time = current_time
        return False, snapshots

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    t1, snap1, t2, snap2 = value

    if not done:
        lines = ["TIMEOUT after %.1fs -- DatabaseChangeVector still moving:" % elapsed]
        lines.append("  sample 1 (%s):" % t1)
        lines.extend(format_per_node(snap1, indent="    "))
        lines.append("  sample 2 (%s):" % t2)
        lines.extend(format_per_node(snap2, indent="    "))
        raise RuntimeError("\n".join(lines))

    header = ("DRAINED -- DatabaseChangeVector unchanged across %d node(s) "
              "(skipped=%s, took %.1fs)" % (len(has_db), skipped, elapsed))
    lines = [header, "  sample 1 (%s):" % t1]
    lines.extend(format_per_node(snap1, indent="    "))
    lines.append("  sample 2 (%s):" % t2)
    lines.extend(format_per_node(snap2, indent="    "))
    return lines


def k_quiescence(p):
    """Wait until every node agrees on a single non-empty DatabaseChangeVector."""
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    def predicate():
        current = snapshot_stats_field(p, has_db, "DatabaseChangeVector")
        if not current:
            raise RuntimeError("k_quiescence: snapshot is empty -- vacuous PASS guard")
        if all((v is None or v == "") for v in current.values()):
            raise RuntimeError(
                "k_quiescence: every node returned a missing/empty "
                "DatabaseChangeVector %s -- can't claim convergence on "
                "empty data" % current)
        unique = set(current.values())
        if len(unique) == 1:
            return True, current
        return False, current

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- DatabaseChangeVectors never converged:\n%s" %
            (elapsed, "\n".join(format_per_node(value))))

    agreed = next(iter(set(value.values())))
    return [
        "QUIESCENT -- %d node(s) agree on DatabaseChangeVector "
        "(skipped=%s, took %.1fs):" % (len(has_db), skipped, elapsed),
        "  " + str(agreed),
    ]


def k_stats_field_parity(p):
    """Wait until the given /stats `fields` match across all nodes that host the database."""
    nodes = p["nodes"]
    fields = p["fields"] or ["CountOfTombstones"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    def predicate():
        per_field = {}
        for field in fields:
            per_field[field] = snapshot_stats_field(p, has_db, field)
            if not per_field[field]:
                raise RuntimeError(
                    "k_stats_field_parity: snapshot for '%s' is empty -- "
                    "vacuous PASS guard" % field)

        drift = []
        for field in fields:
            values = set(per_field[field].values())
            if len(values) > 1:
                drift.append("%s: %s" % (field, per_field[field]))

        if not drift:
            return True, per_field
        return False, drift

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    if not done:
        drift_lines = "\n".join("  " + line for line in value)
        raise RuntimeError(
            "TIMEOUT after %.1fs -- field(s) never converged:\n%s" % (elapsed, drift_lines))

    summary_lines = []
    for field in fields:
        agreed = next(iter(set(value[field].values())))
        summary_lines.append("  %s = %s" % (field, agreed))
    header = ("PARITY -- fields %s match across %d node(s) "
              "(skipped=%s, took %.1fs):" % (fields, len(has_db), skipped, elapsed))
    return [header] + summary_lines


def k_conflicts_resolved(p):
    """Wait until every node reports zero replication conflicts."""
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    path = "/databases/%s/replication/conflicts" % p["db_name"]

    def predicate():
        targets = list(has_db)
        results = request_per_node("GET", targets, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
        counts = {}
        failures = []
        for target, status, body in results:
            if status is None:
                failures.append("%s: connection error %s" % (target, body))
                continue
            if status != 200:
                failures.append("%s: HTTP %d" % (target, status))
                continue
            data = json.loads(body)
            counts[target] = data.get("TotalResults", len(data.get("Results") or []))

        if failures:
            raise RuntimeError(
                "k_conflicts_resolved: %d/%d node(s) failed during conflict "
                "probe: %s -- can't verify resolution on partial coverage"
                % (len(failures), len(targets), failures))

        if not counts:
            raise RuntimeError(
                "k_conflicts_resolved: 0 nodes contributed conflict counts -- "
                "vacuous PASS guard")

        all_zero = all(v == 0 for v in counts.values())
        if all_zero:
            return True, counts
        return False, counts

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- conflicts never resolved; last: %s" %
            (elapsed, value))

    return ("CONFLICTS RESOLVED -- every node reports zero conflicts: %s "
            "(skipped=%s, took %.1fs)" % (value, skipped, elapsed))


def k_marker_propagation(p):
    """PUT a marker doc on `source` and wait until it appears on every target."""
    source = p["source"]
    targets = p["targets"]
    db = p["db_name"]
    timeout = p["timeout"]
    prefix = p["marker_id_prefix"] or "markers/"

    marker_id = "%s%d-%d" % (prefix, time.time_ns(), random.randint(0, 99999))

    put_path = "/databases/%s/docs?id=%s" % (db, quote(marker_id))
    put_body = {"@metadata": {"@collection": "Markers"}}
    status, _ = request("PUT", source, p["ravendb_domain"], put_path,
                        p["client_cert"], p["ca_cert"], body=put_body)
    if status not in (200, 201):
        raise RuntimeError("failed to PUT marker on %s: HTTP %d" % (source, status))

    get_path = "/databases/%s/docs?id=%s" % (db, quote(marker_id))
    pending = set(targets)

    def predicate():
        if not pending:
            return True, "all targets received"
        results = request_per_node("GET", list(pending), p["ravendb_domain"], get_path,
                                   p["client_cert"], p["ca_cert"])
        unreachable = []
        for target, status, body in results:
            if status is None:
                unreachable.append("%s: %s" % (target, body))
                continue
            if status == 200:
                pending.discard(target)
        if unreachable:
            raise RuntimeError(
                "k_marker_propagation: %d target(s) unreachable: %s -- "
                "can't verify marker propagation to dead nodes"
                % (len(unreachable), unreachable))
        if not pending:
            return True, "all targets received"
        return False, "still waiting on %s" % sorted(pending)

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- marker '%s' did not propagate; %s" %
            (elapsed, marker_id, value))

    return ("MARKER PROPAGATED -- '%s' reached every target %s from %s "
            "(took %.1fs)" % (marker_id, targets, source, elapsed))


KINDS = {
    "leader":              k_leader,
    "member":              k_member,
    "rehab":               k_rehab,
    "etag_parity":         k_etag_parity,
    "docs_drain":          k_docs_drain,
    "quiescence":          k_quiescence,
    "stats_field_parity":  k_stats_field_parity,
    "conflicts_resolved":  k_conflicts_resolved,
    "marker_propagation":  k_marker_propagation,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        # cluster / db scope
        db_name=dict(default=None),
        nodes=dict(type="list", elements="str", default=None),
        cluster_leader=dict(default=None),
        target=dict(default=None),
        target_tag=dict(default=None),
        expected_leader=dict(default=None),
        # marker_propagation
        source=dict(default=None),
        targets=dict(type="list", elements="str", default=None),
        marker_id_prefix=dict(default=None),
        # stats_field_parity
        fields=dict(type="list", elements="str", default=None),
        # timing
        timeout=dict(type="int", default=60),
        poll_interval=dict(type="int", default=2),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    if isinstance(message, list):
        message = "\n".join(message)
    module.exit_json(changed=False, msg=message)


if __name__ == "__main__":
    main()
