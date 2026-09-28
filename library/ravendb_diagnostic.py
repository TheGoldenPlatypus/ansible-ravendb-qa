#!/usr/bin/python
"""ravendb_diagnostic -- Ansible module running one-shot diagnostic probes against a RavenDB cluster.

Each probe is a "kind" dispatched through `KINDS[<name>](params)` and returns
a str or list[str] that becomes `result["msg"]`.  When `assert_mode=true` and a
check fails, the kind raises `DiagnosticViolation` and `main()` converts it into
`module.fail_json(msg=<lines>)`.
"""

import json
import os
import re
import time
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import (
    request,
    request_per_node,
)


# Stats fields rendered by the `stats_parity` table.
_STATS_ALL_FIELDS = [
    "CountOfAttachments", "CountOfConflicts", "CountOfCounterEntries",
    "CountOfDocuments", "CountOfDocumentsConflicts", "CountOfRemoteAttachments",
    "CountOfRevisionDocuments", "CountOfTimeSeriesDeletedRanges",
    "CountOfTimeSeriesSegments", "CountOfTombstones", "CountOfUniqueAttachments",
]

# Default subset that `stats_parity` actually asserts on (the rest are info-only).
_STATS_DEFAULT_ASSERTED = [
    "CountOfAttachments", "CountOfConflicts", "CountOfDocuments",
    "CountOfDocumentsConflicts", "CountOfRemoteAttachments",
    "CountOfRevisionDocuments", "CountOfTimeSeriesDeletedRanges",
    "CountOfTombstones", "CountOfUniqueAttachments",
]

# CV entry regexes for entries shaped like `A:123-<dbid>`.
_CV_DBID_RE = re.compile(r"([A-Za-z0-9]+):\d+-([A-Za-z0-9+/=_\-]+)")
_CV_ENTRY_FULL_RE = re.compile(r"([A-Za-z0-9]+):(\d+)-([A-Za-z0-9+/=_\-]+)")

# doc_id_set_parity: retry budget for transiently split docs.
_DOC_ID_PARITY_SETTLE_RETRIES = 3
_DOC_ID_PARITY_SETTLE_SECS = 2

# orphan_revisions: how often to poll /operations/state while waiting for adopt.
_ORPHAN_POLL_SECS = 2


class DiagnosticViolation(Exception):
    """Raised by a kind when assert_mode is on and a check failed; carries the diagnostic lines."""

    def __init__(self, lines):
        self.lines = lines
        super().__init__("\n".join(lines))


def violation(assert_mode, lines):
    """Raise DiagnosticViolation with `lines` when assert_mode is on, else return them."""
    if assert_mode:
        raise DiagnosticViolation(lines)
    return lines


def expand_id_set(p):
    """Build the doc-id list from either explicit `ids` or `id_prefix` + `count`."""
    if p["ids"]:
        return list(p["ids"])
    if p["id_prefix"] and p["count"]:
        return ["%s/%d" % (p["id_prefix"], n) for n in range(p["count"])]
    raise ValueError("requires `ids` OR (`id_prefix` and `count`)")


def classify_nodes(p, nodes):
    """Probe each node's /stats and return ({target: None}, skipped)."""
    domain = p["ravendb_domain"]
    db = p["db_name"]
    if not nodes:
        return {}, []

    host_map = {}
    skipped = []
    path = "/databases/%s/stats" % db
    results = request_per_node("GET", nodes, domain, path,
                               p["client_cert"], p["ca_cert"])
    for target, status, body in results:
        if status == 200:
            host_map[target] = None
        else:
            skipped.append(target)
    return host_map, skipped


def get_stats(p, target):
    """Return the parsed /stats dict for `target` (or None on failure)."""
    db = p["db_name"]
    path = "/databases/%s/stats" % db
    try:
        status, body = request("GET", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
    except Exception:
        return None
    return json.loads(body) if status == 200 else None


def per_node_field(p, nodes_or_map, field):
    """Return {target: stats[field]} for each reachable node."""
    out = {}
    path = "/databases/%s/stats" % p["db_name"]
    results = request_per_node("GET", list(nodes_or_map), p["ravendb_domain"],
                               path, p["client_cert"], p["ca_cert"])
    for target, status, body in results:
        if status == 200:
            out[target] = json.loads(body).get(field)
    return out


def format_per_node(per_node, indent="  "):
    """Render a {name: value} dict as aligned `  name   value` lines."""
    if not per_node:
        return [indent + "(empty)"]
    width = max(len(n) for n in per_node)
    lines = []
    for name in sorted(per_node):
        lines.append("%s%-*s  %s" % (indent, width, name, per_node[name]))
    return lines


def k_doc_count(p):
    """Return a one-line CountOfDocuments report for a single node."""
    target = p["target"]
    db = p["db_name"]
    stats = get_stats(p, target)
    if stats is None:
        raise RuntimeError(
            "%s/%s: /stats returned non-200 -- db missing or node unreachable"
            % (target, db))
    return "%s/%s  ->  %s docs" % (target, db, stats.get("CountOfDocuments"))


def k_replication(p):
    """List incoming and outgoing active replication connections for `target`."""
    target = p["target"]
    db = p["db_name"]
    path = "/databases/%s/replication/active-connections" % db
    status, body = request("GET", target, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "%s/%s: /replication/active-connections returned HTTP %d -- "
            "db missing or node unreachable" % (target, db, status))
    data = json.loads(body)
    incoming = data.get("IncomingConnections") or []
    outgoing = data.get("OutgoingConnections") or []
    lines = ["replication on %s/%s:" % (target, db)]
    lines.append("  INCOMING (%d):" % len(incoming))
    for conn in incoming:
        lines.append("    %s" % conn.get("SourceUrl"))
    lines.append("  OUTGOING (%d):" % len(outgoing))
    for conn in outgoing:
        dest = conn.get("DestinationUrl") or conn.get("DestinationDatabase") or repr(conn)[:120]
        lines.append("    %s" % dest)
    return lines


_REPL_TASK_TYPES = frozenset([
    "Replication",
    "ExternalReplication",
    "PullReplicationAsHub",
    "PullReplicationAsSink",
    "RavenEtl",
])


def k_replication_health(p):
    """Assert no replication-flavored ongoing task on `target/db` is Faulted, errored, or reconnecting."""
    target = p["target"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])

    path = "/databases/%s/tasks" % db
    status, body = request("GET", target, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "%s/%s: /tasks returned HTTP %d -- db missing or node unreachable"
            % (target, db, status))

    ongoing = json.loads(body).get("OngoingTasks") or []
    repl_tasks = [t for t in ongoing if t.get("TaskType") in _REPL_TASK_TYPES]

    lines = ["replication health on %s/%s  (%d replication task(s)):"
             % (target, db, len(repl_tasks))]
    bad = []
    for t in repl_tasks:
        ttype = t.get("TaskType")
        tname = t.get("TaskName") or "<no-name>"
        state = t.get("TaskState")
        conn = t.get("TaskConnectionStatus")
        err = t.get("Error")
        lines.append("  %-22s  %-30s  state=%-10s  conn=%-10s  err=%s"
                     % (ttype, tname, state, conn, err if err else "-"))
        is_stuck = (state == "Faulted") or (conn == "Reconnect") or bool(err)
        if is_stuck:
            bad.append("%s/%s state=%s conn=%s err=%s"
                       % (ttype, tname, state, conn, err))

    if bad:
        return violation(assert_mode, lines + ["FAIL  stuck task(s): %s" % bad])
    lines.append("PASS  no stuck replication task")
    return lines


def k_schema_version(p):
    """Report /build/version per node, optionally asserting parity or an expected substring."""
    nodes = p["nodes"]
    expected = p["expected_version"]
    require_parity = bool(p["require_parity"])
    assert_mode = bool(p["assert_mode"])

    version_map = {}
    unreachable = []
    for target in nodes:
        try:
            status, body = request("GET", target, p["ravendb_domain"], "/build/version",
                                   p["client_cert"], p["ca_cert"])
        except Exception:
            unreachable.append(target)
            continue
        if status != 200:
            unreachable.append(target)
            continue
        version_map[target] = (json.loads(body).get("FullVersion") or "?")

    lines = ["schema/build version per node:"]
    lines.extend(format_per_node(version_map))
    if unreachable:
        lines.append("  unreachable: %s" % unreachable)

    if not version_map:
        raise RuntimeError(
            "k_schema_version: no /build/version response from any node "
            "(nodes=%s, unreachable=%s)" % (nodes, unreachable))

    distinct = set(version_map.values())
    if require_parity and assert_mode and len(distinct) > 1:
        return violation(True, lines + ["FAIL  version mismatch across nodes"])

    if expected and assert_mode:
        bad = [n for n, v in version_map.items() if expected not in v]
        if bad:
            return violation(True, lines + ["FAIL  nodes missing expected '%s': %s" % (expected, bad)])

    return lines


def k_supported_features(p):
    """Report DatabaseRecord.SupportedFeatures for one db on one node."""
    target = p["target"] or (p["nodes"] and p["nodes"][0])
    db = p["db_name"]
    if not target:
        raise ValueError("k_supported_features requires `target` (or `nodes` with >=1 entry)")
    if not db:
        raise ValueError("k_supported_features requires `db_name`")

    rec_path = "/admin/databases?name=%s" % quote(db)
    status, body = request("GET", target, p["ravendb_domain"], rec_path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "%s/%s: /admin/databases returned HTTP %d -- db missing or node unreachable"
            % (target, db, status))
    features = json.loads(body).get("SupportedFeatures") or {}
    return "DatabaseRecord.SupportedFeatures (via %s, db=%s): %s" % (target, db, features)


def k_size_envelope(p):
    """Baseline-or-compare per-node SizeOnDisk in bytes.

    First call (baseline missing) captures and returns BASELINE CAPTURED.  Subsequent
    calls compute %-growth per node and FAIL when any node exceeds `max_growth_pct`."""
    nodes = p["nodes"]
    baseline_file = p["baseline_file"]
    max_growth_pct = float(p["max_growth_pct"] or 300)
    assert_mode = bool(p["assert_mode"])

    sizes = {}
    for target in nodes:
        stats = get_stats(p, target)
        if not stats:
            raise RuntimeError(
                "k_size_envelope: /stats returned nothing for %s (db=%s) -- "
                "cannot capture or compare size" % (target, p.get("db_name")))
        sizes[target] = int((stats.get("SizeOnDisk") or {}).get("SizeInBytes") or 0)

    if not os.path.exists(baseline_file):
        os.makedirs(os.path.dirname(baseline_file), exist_ok=True)
        with open(baseline_file, "w") as f:
            json.dump(sizes, f, indent=2)
        lines = ["BASELINE CAPTURED  %s" % baseline_file]
        lines.extend(format_per_node({n: "%d bytes" % v for n, v in sizes.items()}))
        return lines

    with open(baseline_file, "r") as f:
        baseline = json.load(f)

    lines = ["size envelope check vs %s:" % baseline_file]
    growths = {}
    zero_baseline_with_growth = []
    for target in nodes:
        base = int(baseline.get(target, 0))
        curr = sizes.get(target, 0)
        if base <= 0 and curr > 0:
            zero_baseline_with_growth.append(target)
            lines.append("  %-6s  baseline=%d  current=%d  growth=N/A (baseline=0)" %
                         (target, base, curr))
            continue
        pct = ((curr - base) * 100.0 / base) if base > 0 else 0.0
        growths[target] = pct
        lines.append("  %-6s  baseline=%d  current=%d  growth=%+.1f%%" % (target, base, curr, pct))

    if zero_baseline_with_growth:
        raise RuntimeError(
            "k_size_envelope: baseline=0 but current>0 on %s -- baseline file "
            "is stale or was captured before any data existed; can't compute growth"
            % zero_baseline_with_growth)

    over = [n for n, pct in growths.items() if pct > max_growth_pct]
    if over and assert_mode:
        return violation(True, lines + ["FAIL  growth > %.1f%% on %s" % (max_growth_pct, over)])

    if over:
        lines.append("INFO  growth exceeds %.1f%% on %s (assert_mode=false; not failing)" % (
            max_growth_pct, over))
    return lines


def k_doc_count_parity(p):
    """Assert every reachable node reports the same CountOfDocuments."""
    nodes = p["nodes"]
    assert_mode = bool(p["assert_mode"])

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    counts = per_node_field(p, has_db, "CountOfDocuments")
    lines = ["doc count parity on %s (skipped=%s):" % (list(has_db), skipped)]
    lines.extend(format_per_node(counts))

    if len(set(counts.values())) != 1:
        return violation(assert_mode, lines + ["FAIL  doc counts differ"])
    lines.append("PASS  every node reports %s" % next(iter(set(counts.values()))))
    return lines


def k_doc_id_set_parity(p):
    """Assert each probe doc is uniformly present (200) or uniformly absent on every node.

    Transient splits are retried with a settle window before failing."""
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    probe_ids = expand_id_set(p)

    unreachable_nodes = set()

    def probe_all(ids):
        """Return {id: [status_per_node]}, using None for a node's status on connection error."""
        out = {}
        for doc_id in ids:
            statuses = []
            for target in nodes:
                path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
                try:
                    status, _ = request("GET", target, p["ravendb_domain"], path,
                                        p["client_cert"], p["ca_cert"])
                except Exception:
                    unreachable_nodes.add(target)
                    status = None
                statuses.append(status)
            out[doc_id] = statuses
        return out

    def split_ids(id_status):
        bad = []
        for doc_id, statuses in id_status.items():
            present = sum(1 for s in statuses if s == 200)
            if 0 < present < len(statuses):
                bad.append(doc_id)
        return bad

    id_status = probe_all(probe_ids)
    mismatched = split_ids(id_status)

    for _ in range(_DOC_ID_PARITY_SETTLE_RETRIES):
        if not mismatched:
            break
        time.sleep(_DOC_ID_PARITY_SETTLE_SECS)
        retry_status = probe_all(mismatched)
        id_status.update(retry_status)
        mismatched = split_ids({i: id_status[i] for i in mismatched})

    if unreachable_nodes:
        raise RuntimeError(
            "k_doc_id_set_parity: %s unreachable -- can't determine doc-id-set "
            "parity" % sorted(unreachable_nodes))

    lines = ["doc-id-set parity: %d id(s) probed across %s" % (len(probe_ids), nodes),
             "  mismatched: %d" % len(mismatched)]
    if mismatched:
        lines.append("  mismatched ids (first 20):")
        for i in mismatched[:20]:
            pairs = ", ".join("%s=%s" % (n, s) for n, s in zip(nodes, id_status[i]))
            lines.append("    %s  %s" % (i, pairs))
        return violation(assert_mode, lines +
                         ["FAIL  doc-id-set split (persisted across %d retries x %ds settle)" %
                          (_DOC_ID_PARITY_SETTLE_RETRIES, _DOC_ID_PARITY_SETTLE_SECS)])
    lines.append("PASS  every probe id is uniformly present or uniformly absent")
    return lines


def k_revision_count_parity(p):
    """Assert per-id revision counts agree across nodes (and equal `expected_count` if given)."""
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    expected = p["expected_count"]
    page_size = int(p["page_size"] or 1024)
    probe_ids = expand_id_set(p)

    id_counts = {}
    unreachable_nodes = set()
    for doc_id in probe_ids:
        counts = []
        for target in nodes:
            if target in unreachable_nodes:
                counts.append(None)
                continue
            path = "/databases/%s/revisions?id=%s&pageSize=%d" % (db, quote(doc_id), page_size)
            try:
                status, body = request("GET", target, p["ravendb_domain"], path,
                                       p["client_cert"], p["ca_cert"])
            except Exception:
                unreachable_nodes.add(target)
                counts.append(None)
                continue
            if status == 200:
                counts.append(len(json.loads(body).get("Results") or []))
            else:
                counts.append(None)
        id_counts[doc_id] = counts

    if unreachable_nodes:
        raise RuntimeError(
            "k_revision_count_parity: %s unreachable -- can't compute revision-count parity"
            % sorted(unreachable_nodes))

    mismatched = [i for i, c in id_counts.items() if len(set(c)) != 1]
    lines = ["revision-count parity: %d id(s) across %s" % (len(probe_ids), nodes)]
    lines.append("  per-id counts (first 10):")
    for i, c in list(id_counts.items())[:10]:
        lines.append("    %-20s  %s" % (i, c))
    lines.append("  mismatched: %d" % len(mismatched))

    if mismatched:
        return violation(assert_mode, lines +
                         ["FAIL  revision count drift on: %s" % mismatched[:20]])

    if expected is not None:
        wrong = [i for i, c in id_counts.items() if c != [int(expected)] * len(nodes)]
        if wrong:
            return violation(assert_mode, lines +
                             ["FAIL  expected %d revs/id but mismatched on: %s" %
                              (int(expected), wrong[:20])])
        lines.append("PASS  every id has exactly %d revisions on every node" % int(expected))
    else:
        lines.append("PASS  per-id revision counts agree across nodes")
    return lines


_FORM_EXPECT_CHOICES = ("any-raw", "all-split", "any-split", "all-raw")


def k_extension_stats_parity(p):
    """Assert selected `aspects` (attachments/counters/timeseries) match across nodes."""
    nodes = p["nodes"]
    assert_mode = bool(p["assert_mode"])
    aspects_csv = p["aspects"] or "attachments,counters,timeseries"
    aspects = [s.strip() for s in aspects_csv.split(",")]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    fields = []
    if "attachments" in aspects:
        fields += ["CountOfUniqueAttachments", "CountOfAttachments"]
    if "counters" in aspects:
        fields += ["CountOfCounterEntries"]
    if "timeseries" in aspects:
        fields += ["CountOfTimeSeriesSegments"]

    lines = ["extension-stats parity (aspects=%s) on %s (skipped=%s):" %
             (aspects, list(has_db), skipped)]
    drift = []
    for field in fields:
        per = per_node_field(p, has_db, field)
        lines.append("  %s:" % field)
        lines.extend(format_per_node(per, indent="    "))
        if len(set(per.values())) != 1:
            drift.append(field)

    if drift:
        return violation(assert_mode, lines + ["FAIL  drift on: %s" % drift])
    lines.append("PASS  every selected aspect matches across nodes")
    return lines


def k_stats_parity(p):
    """Print a per-node stats table and assert uniformity on `assert_fields`.

    Fields not in `assert_fields` are printed as info only.  When `settle_secs > 0`,
    poll the asserted fields until they agree across all has_db nodes before the
    final assertion.
    """
    nodes = p["nodes"]
    assert_mode = bool(p["assert_mode"])
    assert_fields = p["assert_fields"] or _STATS_DEFAULT_ASSERTED
    settle_secs = int(p["settle_secs"] or 0)
    settle_interval = int(p["settle_interval"] or 3)

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    settle_elapsed = 0.0
    settle_polls = 0
    if settle_secs > 0:
        settle_start = time.time()
        deadline = settle_start + settle_secs
        last_drift = None
        while True:
            settle_polls += 1
            per_field = {}
            for field in assert_fields:
                per_field[field] = per_node_field(p, has_db, field)
            drift = [(f, per_field[f]) for f in assert_fields
                     if len(set(per_field[f].values())) > 1]
            if not drift:
                settle_elapsed = time.time() - settle_start
                break
            last_drift = drift
            if time.time() >= deadline:
                settle_elapsed = time.time() - settle_start
                raise RuntimeError(
                    "stats_parity: pre-settle TIMEOUT after %.1fs (%d poll(s)) -- "
                    "asserted field(s) never converged across %s: %s"
                    % (settle_elapsed, settle_polls, list(has_db), last_drift))
            time.sleep(settle_interval)

    per_node = {}
    for target in has_db:
        s = get_stats(p, target)
        if s is None:
            continue
        row = {}
        for field in _STATS_ALL_FIELDS:
            row[field] = s.get(field) or 0
        row["SizeOnDisk"] = (s.get("SizeOnDisk") or {}).get("SizeInBytes") or 0
        per_node[target] = row

    node_order = list(has_db)

    # Column widths sized to the widest cell per node (or the node name, min 8).
    node_widths = {}
    for n in node_order:
        widest = max(len(str(per_node[n][f])) for f in _STATS_ALL_FIELDS + ["SizeOnDisk"])
        node_widths[n] = max(widest, len(n), 8)
    field_width = max(len(f) for f in _STATS_ALL_FIELDS + ["SizeOnDisk (info only)"])

    if settle_secs > 0:
        lines = ["/stats parity  db=%s  nodes=%s  pre-settle=%.1fs (%d poll(s))"
                 % (p["db_name"], node_order, settle_elapsed, settle_polls)]
    else:
        lines = ["/stats parity  db=%s  nodes=%s  pre-settle=disabled"
                 % (p["db_name"], node_order)]
    if skipped:
        lines.append("  SKIPPED (DB not present): %s" % skipped)

    def fmt_node_columns(values):
        cells = ["%*s" % (node_widths[n], values[i]) for i, n in enumerate(node_order)]
        return " , ".join(cells)

    header = "%-*s  %s  status" % (field_width, "field", fmt_node_columns(node_order))
    lines.append(header)

    mismatched = []
    flagged_info = []
    for field in _STATS_ALL_FIELDS:
        values = [per_node[n][field] for n in node_order]
        is_uniform = len(set(values)) == 1
        is_asserted = field in assert_fields
        if not is_uniform and is_asserted:
            mismatched.append(field)
            status_label = "MISMATCH"
        elif not is_uniform:
            flagged_info.append(field)
            status_label = "DRIFT (info)"
        elif is_asserted:
            status_label = "OK"
        else:
            status_label = "OK (info)"
        lines.append("%-*s  %s  %s" % (field_width, field, fmt_node_columns(values), status_label))

    size_values = [per_node[n]["SizeOnDisk"] for n in node_order]
    lines.append("%-*s  %s  %s" %
                 (field_width, "SizeOnDisk", fmt_node_columns(size_values), "info"))

    if flagged_info:
        lines.append("WARN  informational drift on per-node-intrinsic fields: %s" % flagged_info)

    if mismatched:
        return violation(assert_mode, lines +
                         ["FAIL  /stats parity broken on: %s" % mismatched])
    if assert_mode:
        lines.append("PASS  every asserted field is parity-equal across %s" % node_order)
    return lines


def k_orphan_revisions(p):
    """Trigger orphaned-revision adopt on every node, wait, and FAIL on any non-zero AdoptedCount."""
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    budget = int(p["budget_secs"] or 60)

    op_ids = {}
    unreachable_nodes = []
    for target in nodes:
        path = "/databases/%s/admin/revisions/orphaned/adopt" % db
        try:
            status, body = request("POST", target, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"], body={})
        except Exception as e:
            unreachable_nodes.append(target)
            continue
        if status not in (200, 201):
            raise RuntimeError("trigger adopt on %s failed: HTTP %d" % (target, status))
        op_ids[target] = int(json.loads(body)["OperationId"])

    if unreachable_nodes:
        raise RuntimeError(
            "k_orphan_revisions: %s unreachable -- can't determine orphan state"
            % sorted(unreachable_nodes))

    adopted = {}
    inconclusive = []
    deadline = time.monotonic() + budget
    pending = dict(op_ids)
    while pending and time.monotonic() < deadline:
        done_now = []
        for target, op_id in pending.items():
            state_path = "/databases/%s/operations/state?id=%d" % (db, op_id)
            status, body = request("GET", target, p["ravendb_domain"], state_path,
                                   p["client_cert"], p["ca_cert"])
            if status != 200:
                continue
            state = json.loads(body)
            if state.get("Status") not in ("Completed", "Faulted", "Canceled"):
                continue
            done_now.append(target)
            result = state.get("Result") or {}
            result_type = result.get("$type") or ""
            if "AdoptOrphanedRevisionsResult" in result_type:
                adopted[target] = result.get("AdoptedCount", 0)
            else:
                adopted[target] = "?"
                inconclusive.append(target)
        for t in done_now:
            pending.pop(t, None)
        if pending:
            time.sleep(_ORPHAN_POLL_SECS)

    if pending:
        raise RuntimeError("adopt operation never finished on %s within %ds" %
                           (list(pending), budget))

    lines = ["orphan revisions adopted per node:"]
    lines.extend(format_per_node(adopted))
    if inconclusive:
        lines.append("INCONCLUSIVE on %s -- /operations/state returned a stale non-adopt result "
                     "(RavenDB operation-id recycling on fast operations)" % inconclusive)

    real_counts = [c for c in adopted.values() if c != "?"]
    if any(c != 0 for c in real_counts):
        return violation(assert_mode, lines + ["FAIL  orphans were present on at least one node"])

    if inconclusive:
        lines.append("OK (partial)  no orphans on readable nodes; %s inconclusive" % inconclusive)
    else:
        lines.append("PASS  no orphan revisions on any node")
    return lines


KINDS = {
    "doc_count":                  k_doc_count,
    "replication":                k_replication,
    "replication_health":         k_replication_health,
    "schema_version":             k_schema_version,
    "size_envelope":              k_size_envelope,
    "supported_features":         k_supported_features,
    "doc_count_parity":           k_doc_count_parity,
    "doc_id_set_parity":          k_doc_id_set_parity,
    "extension_stats_parity":     k_extension_stats_parity,
    "revision_count_parity":      k_revision_count_parity,
    "stats_parity":               k_stats_parity,
    "orphan_revisions":           k_orphan_revisions,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),

        # transport
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),

        # universal
        assert_mode=dict(type="bool", default=False),

        # cluster / db scope
        db_name=dict(default=None),
        target=dict(default=None),
        nodes=dict(type="list", elements="str", default=None),
        cluster_leader=dict(default=None),

        # id sets (used by doc_id_set_parity, revision_count_parity)
        ids=dict(type="list", elements="str", default=None),
        id_prefix=dict(default=None),
        count=dict(type="int", default=None),

        # schema_version
        require_parity=dict(type="bool", default=False),
        expected_version=dict(default=None),

        # size_envelope
        baseline_file=dict(type="path", default=None),
        max_growth_pct=dict(type="float", default=None),

        # revision_count_parity
        expected_count=dict(type="int", default=None),
        page_size=dict(type="int", default=None),

        # orphan_revisions
        budget_secs=dict(type="int", default=None),

        # extension_stats_parity
        aspects=dict(default=None),

        # stats_parity
        assert_fields=dict(type="list", elements="str", default=None),
        # stats_parity pre-settle (0 = disabled)
        settle_secs=dict(type="int", default=None),
        settle_interval=dict(type="int", default=None),

    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except DiagnosticViolation as e:
        module.fail_json(msg="\n".join(e.lines) if isinstance(e.lines, list) else str(e.lines))
    except Exception as e:
        module.fail_json(msg=str(e))

    if isinstance(message, list):
        message = "\n".join(message)
    module.exit_json(changed=False, msg=message)


if __name__ == "__main__":
    main()
