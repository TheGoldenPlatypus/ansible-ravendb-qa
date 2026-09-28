"""Smoke test for the integration harness itself."""

import ravendb_diagnostic as diag


def _params(target, db="db1"):
    return {
        "target": target,
        "db_name": db,
        "ravendb_domain": "ignored-when-override-is-set",
        "client_cert": "",
        "ca_cert": "",
    }


def test_single_node_db_is_reachable_via_diagnostic(ravendb_cluster):
    """One node with empty db1 -> k_doc_count returns target/db with 0 docs."""
    info = ravendb_cluster(n_nodes=1, db="db1")
    target = info["nodes"][0]

    msg = diag.k_doc_count(_params(target))

    assert "%s/db1" % target in msg
    assert "0 docs" in msg
