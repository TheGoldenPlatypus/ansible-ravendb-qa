import json
import urllib.error
import urllib.request
from collections import defaultdict


def params(**kwargs):
    """A params dict where unset keys return None."""
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-when-override-is-set"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


def print_lines(label, lines):
    """Pretty-print a list[str] under an indented label."""
    print(f"    {label}:")
    for line in lines:
        print(f"        {line}")


def _http(method, url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _url_for(node_name):
    import ravendb_client
    return ravendb_client.TARGET_URL_OVERRIDES[node_name]


def write_doc(node_name, doc_id, body, db="db1", collection="Test"):
    """PUT a document into <node>/<db>. Returns the response @change-vector."""
    payload = dict(body)
    payload["@metadata"] = {"@collection": collection}
    status, resp = _http(
        "PUT",
        f"{_url_for(node_name)}/databases/{db}/docs?id={doc_id}",
        body=payload,
    )
    if status not in (200, 201):
        raise RuntimeError(f"write_doc failed: HTTP {status} {resp[:200]}")
    return json.loads(resp).get("Results", [{}])[0].get("@change-vector")


def enable_revisions(node_name, db="db1"):
    """Turn on revisions for the database."""
    body = {
        "Default": {
            "Disabled": False,
            "MinimumRevisionsToKeep": None,
            "MinimumRevisionAgeToKeep": None,
            "PurgeOnDelete": False,
        },
        "Collections": {},
    }
    status, resp = _http(
        "POST",
        f"{_url_for(node_name)}/databases/{db}/admin/revisions/config",
        body=body,
    )
    if status not in (200, 201, 204):
        raise RuntimeError(f"enable_revisions failed: HTTP {status} {resp[:200]}")


def add_attachment(node_name, doc_id, name, content, db="db1"):
    """PUT an attachment onto an existing doc. `content` may be str or bytes."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    url = f"{_url_for(node_name)}/databases/{db}/attachments?id={doc_id}&name={name}"
    req = urllib.request.Request(url, data=content, method="PUT",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status not in (200, 201, 204):
                raise RuntimeError(f"add_attachment failed: HTTP {r.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"add_attachment failed: HTTP {e.code} {e.read()[:200]}")


def delete_doc(node_name, doc_id, db="db1"):
    """DELETE a document by id from a node. Idempotent."""
    status, resp = _http(
        "DELETE",
        f"{_url_for(node_name)}/databases/{db}/docs?id={doc_id}",
    )
    if status not in (200, 204, 404):
        raise RuntimeError(f"delete_doc failed: HTTP {status} {resp[:200]}")


def setup_external_replication(src_node, dst_node, db="db1", name="test-ext-repl",
                                connection_string_name="test-conn"):
    """Wire an external_replication task from src to dst. Returns the task id."""
    src_url = _url_for(src_node)
    dst_url = _url_for(dst_node)

    cs_body = {
        "Type": "Raven",
        "Name": connection_string_name,
        "Database": db,
        "TopologyDiscoveryUrls": [dst_url],
    }
    status, resp = _http(
        "PUT", f"{src_url}/databases/{db}/admin/connection-strings", body=cs_body)
    if status not in (200, 201):
        raise RuntimeError(f"connection-string PUT failed: HTTP {status} {resp[:300]}")

    task_body = {
        "Watcher": {
            "Name": name,
            "ConnectionStringName": connection_string_name,
            "Database": db,
            "Url": dst_url,
            "Disabled": False,
        }
    }
    status, resp = _http(
        "POST", f"{src_url}/databases/{db}/admin/tasks/external-replication",
        body=task_body)
    if status not in (200, 201):
        raise RuntimeError(f"external-replication task POST failed: HTTP {status} {resp[:300]}")

    return json.loads(resp).get("TaskId")


def delete_replication_task(src_node, task_id, db="db1", task_type="Replication"):
    """Delete a previously-created replication task by its id."""
    src_url = _url_for(src_node)
    status, resp = _http(
        "DELETE",
        f"{src_url}/databases/{db}/admin/tasks?id={task_id}&type={task_type}")
    if status not in (200, 204):
        raise RuntimeError(f"task DELETE failed: HTTP {status} {resp[:200]}")


def get_doc_cv(node_name, doc_id, db="db1"):
    """Read a doc and return its @change-vector, or None if not found."""
    status, resp = _http(
        "GET",
        f"{_url_for(node_name)}/databases/{db}/docs?id={doc_id}",
    )
    if status != 200:
        return None
    results = json.loads(resp).get("Results") or []
    if not results:
        return None
    return (results[0].get("@metadata") or {}).get("@change-vector")
