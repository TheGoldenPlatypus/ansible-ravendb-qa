#!/usr/bin/python
"""ravendb_writes -- Ansible module that writes documents and related data into a RavenDB database."""

import datetime
import json
import time
import uuid
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request


def resolve_write_target(p):
    """Return the node to send writes to."""
    return p["target"]


_TRANSIENT_HTTP_STATUSES = frozenset([500, 502, 503, 504])
_HTTP_RETRY_MAX = 3
_HTTP_RETRY_BACKOFF_SECS = (0.2, 0.5, 1.0)


def _format_failure(label, status, body):
    """Format a per-write failure as `label -> HTTP <status>: <server message>`."""
    snippet = ""
    if body:
        try:
            data = json.loads(body)
            msg = data.get("Message") or data.get("Error") or ""
            if isinstance(msg, str) and msg.strip():
                snippet = msg.strip()[:200]
        except Exception:
            pass
        if not snippet:
            try:
                snippet = body.decode("utf-8", errors="replace").strip()[:200]
            except Exception:
                snippet = repr(body)[:200]
    if snippet:
        return "%s -> HTTP %s: %s" % (label, status, snippet)
    return "%s -> HTTP %s" % (label, status)


def put_idempotent(p, target, path, body):
    """PUT with a bounded retry on transient server-overload statuses; return (status, body)."""
    status = None
    resp = None
    for attempt in range(_HTTP_RETRY_MAX + 1):
        status, resp = request("PUT", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body=body)
        if status not in _TRANSIENT_HTTP_STATUSES:
            return status, resp
        if attempt < _HTTP_RETRY_MAX:
            time.sleep(_HTTP_RETRY_BACKOFF_SECS[attempt])
    return status, resp


def k_docs(p):
    """Write `count` documents with sequential ids under `id_prefix`."""
    target = resolve_write_target(p)
    db = p["db_name"]
    prefix = p["id_prefix"] or "micro/doc"
    count = p["count"]
    body_tag = p["body_tag"]
    phase = p["phase"]

    if not count or count < 1:
        raise ValueError("kind=docs requires `count` >= 1 (got %r) -- "
                         "writing 0 docs and reporting success would be a vacuous PASS"
                         % count)

    failures = []
    for n in range(count):
        doc_id = "%s/%d" % (prefix, n)
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        body = {"@metadata": {"@collection": "MicroDocs"}}
        if body_tag:
            body["tag"] = body_tag
        if phase:
            body["Phase"] = phase
        status, resp = put_idempotent(p, target, path, body)
        if status not in (200, 201):
            failures.append(_format_failure(doc_id, status, resp))

    if failures:
        raise RuntimeError("k_docs: %d/%d PUTs failed on %s/%s: %s" %
                           (len(failures), count, target, db, failures[:10]))

    suffix = ", body_tag='%s'" % body_tag if body_tag else ""
    return "WROTE %d/%d docs to %s/%s (id prefix '%s'%s)" % (
        count, count, target, db, prefix, suffix)


def k_docs_freeform(p):
    """Write `count` documents with random uuid ids and no collection."""
    target = resolve_write_target(p)
    db = p["db_name"]
    count = p["count"]
    phase = p["phase"]

    if not count or count < 1:
        raise ValueError("kind=docs_freeform requires `count` >= 1 (got %r)" % count)

    failures = []
    for n in range(count):
        doc_id = uuid.uuid4().hex
        path = "/databases/%s/docs?id=%s" % (db, doc_id)
        body = {
            "Name": "freeform-%d-on-%s" % (n, target),
            "@metadata": {"@collection": None},
        }
        if phase:
            body["Phase"] = phase
        status, resp = request("PUT", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body=body)
        if status not in (200, 201):
            failures.append(_format_failure(doc_id, status, resp))

    if failures:
        raise RuntimeError("k_docs_freeform: %d/%d PUTs failed on %s/%s: %s" %
                           (len(failures), count, target, db, failures[:10]))

    return "WROTE %d/%d freeform docs (random uuid ids) to %s/%s" % (
        count, count, target, db)


def k_docs_revisions(p):
    """Write `count` documents, PUTting each `revs_per_doc` times to build up revisions."""
    target = resolve_write_target(p)
    db = p["db_name"]
    count = p["count"]
    revs = p["revs_per_doc"]
    prefix = p["id_prefix"] or "seed"
    collection = p["collection"] or "MicroDocs"
    phase = p["phase"]

    if not count or count < 1:
        raise ValueError("kind=docs_revisions requires `count` >= 1 (got %r)" % count)
    if not revs or revs < 1:
        raise ValueError("kind=docs_revisions requires `revs_per_doc` >= 1 (got %r)" % revs)

    total = count * revs
    failures = []
    for v in range(1, revs + 1):
        for n in range(count):
            doc_id = "%s/%d" % (prefix, n)
            path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
            body = {
                "v": v,
                "src": "write_docs_revisions",
                "@metadata": {"@collection": collection},
            }
            if phase:
                body["Phase"] = phase
            status, resp = put_idempotent(p, target, path, body)
            if status not in (200, 201):
                failures.append(_format_failure(
                    "%s rev=%d" % (doc_id, v), status, resp))

    if failures:
        raise RuntimeError("k_docs_revisions: %d/%d PUTs failed on %s/%s: %s" %
                           (len(failures), total, target, db, failures[:10]))

    return ("WROTE %d/%d PUTs to %s/%s "
            "(%d docs x %d revisions each, id prefix '%s', collection '%s')" % (
        total, total, target, db, count, revs, prefix, collection))


def k_docs_interleaved(p):
    """Write `count` documents round-robin across `prefixes`."""
    target = resolve_write_target(p)
    db = p["db_name"]
    count = p["count"]
    prefixes = p["prefixes"]
    phase = p["phase"]
    if not prefixes:
        raise ValueError("kind=docs_interleaved requires `prefixes` (non-empty list)")
    if not count or count < 1:
        raise ValueError("kind=docs_interleaved requires `count` >= 1 (got %r)" % count)

    failures = []
    for n in range(count):
        which = prefixes[n % len(prefixes)]
        seq = n // len(prefixes)
        doc_id = "%s-%d" % (which, seq)
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        body = {"@metadata": {"@collection": "MicroDocs"}}
        if phase:
            body["Phase"] = phase
        status, resp = request("PUT", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body=body)
        if status not in (200, 201):
            failures.append(_format_failure(doc_id, status, resp))

    if failures:
        raise RuntimeError("k_docs_interleaved: %d/%d PUTs failed on %s/%s: %s" %
                           (len(failures), count, target, db, failures[:10]))

    return "WROTE %d/%d docs to %s/%s (round-robin across prefixes %s)" % (
        count, count, target, db, prefixes)


def k_attachments(p):
    """Write `count` attachments, one per sequential document id."""
    target = resolve_write_target(p)
    db = p["db_name"]
    count = p["count"]
    doc_prefix = p["doc_id_prefix"] or "micro/doc"
    att_name = p["attachment_name"] or "data"

    if not count or count < 1:
        raise ValueError("kind=attachments requires `count` >= 1 (got %r)" % count)

    failures = []
    for n in range(count):
        doc_id = "%s/%d" % (doc_prefix, n)
        name = "%s/%d" % (att_name, n)
        payload = p["payload"] if p["payload"] is not None else "blob-%d" % n
        path = "/databases/%s/attachments?id=%s&name=%s" % (
            db, quote(doc_id), quote(name))
        status, resp = request("PUT", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body=payload)
        if status not in (200, 201, 204):
            failures.append(_format_failure(name, status, resp))

    if failures:
        raise RuntimeError("k_attachments: %d/%d PUTs failed on %s/%s: %s" %
                           (len(failures), count, target, db, failures[:10]))

    return ("WROTE %d/%d attachments to %s/%s "
            "(docs '%s/0..%d', names '%s/0..%d')" % (
        count, count, target, db,
        doc_prefix, count - 1, att_name, count - 1))


def k_counters(p):
    """Increment a counter on `doc_id` `repeat` times by `delta` each."""
    target = resolve_write_target(p)
    db = p["db_name"]
    doc_id = p["doc_id"]
    name = p["counter_name"] or "Likes"
    delta = p["delta"]
    repeat = p["repeat"]

    if not doc_id:
        raise ValueError("kind=counters requires `doc_id`")
    if not repeat or repeat < 1:
        raise ValueError("kind=counters requires `repeat` >= 1 (got %r) -- "
                         "0 calls = no counter change = vacuous PASS" % repeat)

    body = {"Documents": [{
        "DocumentId": doc_id,
        "Operations": [{
            "Type": "Increment",
            "CounterName": name,
            "Delta": delta,
        }],
    }]}
    path = "/databases/%s/counters" % db

    failures = []
    for i in range(repeat):
        status, resp = request("POST", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body=body)
        if status not in (200, 201, 204):
            failures.append(_format_failure("call #%d" % i, status, resp))

    if failures:
        raise RuntimeError("k_counters: %d/%d POSTs failed on %s/%s/%s: %s" %
                           (len(failures), repeat, target, db, doc_id, failures[:10]))

    total_change = delta * repeat
    return ("INCREMENTED counter '%s' on %s by %+d "
            "(%d call(s) of delta %d, total change %+d) on %s/%s" % (
        name, doc_id, total_change, repeat, delta, total_change, target, db))


def k_timeseries(p):
    """Append time-series entries to `doc_id`, or delete a range when `delete_from`/`delete_to` are set."""
    target = resolve_write_target(p)
    db = p["db_name"]
    doc_id = p["doc_id"]
    name = p["ts_name"] or "Heartrate"

    if not doc_id:
        raise ValueError("kind=timeseries requires `doc_id`")

    # Delete-range mode.
    if p["delete_from"] and p["delete_to"]:
        body = {"Name": name, "Deletes": [{
            "From": p["delete_from"],
            "To": p["delete_to"],
        }]}
        path = "/databases/%s/timeseries?docId=%s" % (db, quote(doc_id))
        status, resp = request("POST", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body=body)
        if status not in (200, 201, 204):
            raise RuntimeError(
                "k_timeseries delete-range on %s/%s/%s [%s..%s] failed: HTTP %s body=%s"
                % (target, db, doc_id, p["delete_from"], p["delete_to"],
                   status, (resp or b"")[:300]))
        return ("DELETED timeseries '%s' range [%s .. %s] on doc %s "
                "(HTTP %d) on %s/%s" % (
            name, p["delete_from"], p["delete_to"], doc_id, status, target, db))

    # Append mode.
    count = p["count"]
    interval = p["interval_seconds"]
    if not count or count < 1:
        raise ValueError("kind=timeseries (append) requires `count` >= 1 (got %r)" % count)

    if p["start_timestamp"]:
        start = datetime.datetime.strptime(
            p["start_timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
    else:
        start = datetime.datetime.utcnow() - datetime.timedelta(hours=1)

    path = "/databases/%s/timeseries?docId=%s" % (db, quote(doc_id))

    failures = []
    for n in range(count):
        ts = start + datetime.timedelta(seconds=n * interval)
        body = {"Name": name, "Appends": [{
            "Timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "Values": [72.0],
            "Tag": None,
        }]}
        status, resp = request("POST", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body=body)
        if status not in (200, 201, 204):
            failures.append(_format_failure("entry #%d" % n, status, resp))

    if failures:
        raise RuntimeError("k_timeseries append: %d/%d entries failed on %s/%s/%s: %s" %
                           (len(failures), count, target, db, doc_id, failures[:10]))

    return ("APPENDED %d/%d timeseries '%s' entries to doc %s "
            "(every %ds starting %s) on %s/%s" % (
        count, count, name, doc_id, interval,
        start.strftime("%Y-%m-%dT%H:%M:%SZ"), target, db))


def k_delete(p):
    """Delete documents given by `ids` or by `id_prefix` + `count`."""
    target = resolve_write_target(p)
    db = p["db_name"]

    if p["ids"]:
        ids = p["ids"]
    elif p["id_prefix"] and p["count"]:
        ids = []
        for n in range(p["count"]):
            ids.append("%s/%d" % (p["id_prefix"], n))
    else:
        raise ValueError("kind=delete requires `ids` OR (`id_prefix` and `count`)")

    if not ids:
        raise ValueError("kind=delete got an empty `ids` list -- 0 deletes = vacuous PASS")

    ok = 0
    errors = []
    for doc_id in ids:
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        status, resp = request("DELETE", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status == 204:
            ok += 1
        else:
            errors.append(_format_failure(doc_id, status, resp))

    if errors:
        raise RuntimeError(
            "k_delete on %s/%s: %d/%d returned 204; %d unexpected: %s"
            % (target, db, ok, len(ids), len(errors), errors[:10]))

    return "DELETE on %s/%s: %d/%d returned 204 (Raven does not distinguish missing vs deleted)" % (
        target, db, ok, len(ids))


def k_restore_revision(p):
    """Fetch the revision at `revision_cv` and PUT it back as the live `doc_id`."""
    target = resolve_write_target(p)
    db = p["db_name"]
    doc_id = p["doc_id"]
    revision_cv = p["revision_cv"]

    if not doc_id:
        raise ValueError("kind=restore_revision requires `doc_id`")
    if not revision_cv:
        raise ValueError("kind=restore_revision requires `revision_cv`")

    cv_encoded = quote(revision_cv, safe="")
    get_path = "/databases/%s/revisions?changeVector=%s" % (db, cv_encoded)
    get_status, body_bytes = request("GET", target, p["ravendb_domain"], get_path,
                                     p["client_cert"], p["ca_cert"])
    if get_status != 200:
        raise RuntimeError(
            "k_restore_revision: GET revision cv=%s on %s/%s failed: HTTP %s"
            % (revision_cv, target, db, get_status))
    results = json.loads(body_bytes).get("Results", [])
    if not results:
        raise ValueError("no revision found for cv=%s on %s" % (revision_cv, doc_id))
    revision_body = results[0]

    put_path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
    put_status, _ = request("PUT", target, p["ravendb_domain"], put_path,
                            p["client_cert"], p["ca_cert"], body=revision_body)
    if put_status not in (200, 201):
        raise RuntimeError(
            "k_restore_revision: PUT %s on %s/%s failed: HTTP %s"
            % (doc_id, target, db, put_status))

    return "RESTORED revision %s as live doc %s on %s/%s (PUT HTTP %d)" % (
        revision_cv, doc_id, target, db, put_status)


KINDS = {
    "docs":             k_docs,
    "docs_freeform":    k_docs_freeform,
    "docs_revisions":   k_docs_revisions,
    "docs_interleaved": k_docs_interleaved,
    "attachments":      k_attachments,
    "counters":         k_counters,
    "timeseries":       k_timeseries,
    "delete":           k_delete,
    "restore_revision": k_restore_revision,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        target=dict(required=True),
        db_name=dict(required=True),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        # docs / freeform / interleaved
        count=dict(type="int", default=0),
        id_prefix=dict(default=None),
        prefixes=dict(type="list", elements="str", default=None),
        body_tag=dict(default=None),
        # optional cohort marker added as a top-level "Phase" field on each doc
        phase=dict(default=None),
        # docs_revisions
        revs_per_doc=dict(type="int", default=1),
        collection=dict(default=None),
        # attachments
        doc_id_prefix=dict(default=None),
        attachment_name=dict(default=None),
        payload=dict(default=None),
        # counters / timeseries / restore_revision
        doc_id=dict(default=None),
        counter_name=dict(default=None),
        delta=dict(type="int", default=1),
        repeat=dict(type="int", default=1),
        ts_name=dict(default=None),
        start_timestamp=dict(default=None),
        interval_seconds=dict(type="int", default=6),
        delete_from=dict(default=None),
        delete_to=dict(default=None),
        # delete
        ids=dict(type="list", elements="str", default=None),
        # restore_revision
        revision_cv=dict(default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
