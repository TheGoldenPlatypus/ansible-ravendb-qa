import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

import conftest  # noqa: F401

sys.path.insert(0, str(Path(__file__).parent))

from ravendb_embedded import EmbeddedServer, ServerOptions


_NODE_LETTERS = "abcdefghi"


def _free_port():
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for(predicate, timeout_secs=20, poll_secs=0.5, what="condition"):
    deadline = time.monotonic() + timeout_secs
    last_err = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as e:
            last_err = e
        time.sleep(poll_secs)
    raise TimeoutError("timed out waiting for %s (last error: %r)" % (what, last_err))


def _http(method, url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _activate_license(url, license_obj):
    status, body = _http("POST", url + "/admin/license/activate", body=license_obj)
    if status not in (200, 201, 204):
        raise RuntimeError("license activation failed on %s: HTTP %d %s"
                           % (url, status, body[:300]))


def _start_node(http_port, tcp_port):
    data_dir = tempfile.mkdtemp(prefix="raven-test-data-")
    logs_dir = tempfile.mkdtemp(prefix="raven-test-logs-")
    s = EmbeddedServer()
    opts = ServerOptions()
    opts.data_directory = data_dir
    opts.logs_path = logs_dir
    opts.server_url = "http://127.0.0.1:%d" % http_port
    opts.command_line_args = ["--ServerUrl.Tcp=tcp://127.0.0.1:%d" % tcp_port]
    s.start_server(opts)
    s._test_dirs = (data_dir, logs_dir)
    return s


def _form_cluster(urls):
    """Join each non-leader URL to node-0's cluster as a Member."""
    leader = urls[0]
    for other in urls[1:]:
        status, body = _http("PUT", "%s/admin/cluster/node?url=%s" % (leader, other))
        if status not in (200, 201):
            raise RuntimeError(
                "cluster join failed (leader=%s other=%s): HTTP %d %s"
                % (leader, other, status, body[:300]))
    def all_members():
        status, body = _http("GET", "%s/cluster/topology" % leader, timeout=5)
        if status != 200:
            return False
        members = json.loads(body).get("Topology", {}).get("Members") or {}
        return len(members) == len(urls)
    _wait_for(all_members, timeout_secs=30, what="all %d nodes to appear as Members" % len(urls))


def _create_database(leader_url, db, replication_factor):
    body = {
        "DatabaseName": db,
        "Settings": {},
        "Disabled": False,
    }
    status, resp = _http(
        "PUT",
        "%s/admin/databases?name=%s&replicationFactor=%d" % (leader_url, db, replication_factor),
        body=body,
    )
    if status not in (200, 201):
        raise RuntimeError("create_database failed: HTTP %d %s" % (status, resp[:300]))


def _wait_for_db_on_all(urls, db):
    def all_have_db():
        for u in urls:
            status, _ = _http("GET", "%s/databases/%s/stats" % (u, db), timeout=5)
            if status != 200:
                return False
        return True
    _wait_for(all_have_db, timeout_secs=30, what="db '%s' to appear on every node" % db)


@pytest.fixture
def ravendb_cluster():
    """Factory fixture returning a dict of {nodes, urls, db} for embedded servers."""
    started = []
    saved_overrides = None

    def _build(n_nodes=1, db="db1", replication_factor=None, cluster=True):
        """Bring up N embedded RavenDB servers, clustered or independent."""
        nonlocal saved_overrides
        if replication_factor is None:
            replication_factor = n_nodes if cluster else 1

        license_path = os.environ.get("RAVEN_TEST_LICENSE")
        if cluster and n_nodes > 1 and not license_path:
            pytest.skip(
                "Multi-node clustered tests need a Developer license.  "
                "Set RAVEN_TEST_LICENSE=/path/to/license.json to enable.")
        license_obj = None
        if license_path:
            with open(license_path) as f:
                license_obj = json.load(f)

        nodes, urls = [], []
        for i in range(n_nodes):
            http_p, tcp_p = _free_port(), _free_port()
            srv = _start_node(http_p, tcp_p)
            started.append(srv)
            url = srv.get_server_uri()
            urls.append(url)
            nodes.append("1" + _NODE_LETTERS[i])
            if license_obj is not None:
                _activate_license(url, license_obj)

        if cluster:
            if n_nodes > 1:
                _form_cluster(urls)
            _create_database(urls[0], db, replication_factor)
            _wait_for_db_on_all(urls, db)
        else:
            for url in urls:
                _create_database(url, db, 1)

        import ravendb_client
        saved_overrides = dict(ravendb_client.TARGET_URL_OVERRIDES)
        ravendb_client.TARGET_URL_OVERRIDES.clear()
        for n, u in zip(nodes, urls):
            ravendb_client.TARGET_URL_OVERRIDES[n] = u

        return {"nodes": nodes, "urls": urls, "db": db}

    yield _build

    if saved_overrides is not None:
        import ravendb_client
        ravendb_client.TARGET_URL_OVERRIDES.clear()
        ravendb_client.TARGET_URL_OVERRIDES.update(saved_overrides)
    for srv in started:
        try:
            srv.close()
        except Exception:
            pass
        for d in getattr(srv, "_test_dirs", ()):
            shutil.rmtree(d, ignore_errors=True)
