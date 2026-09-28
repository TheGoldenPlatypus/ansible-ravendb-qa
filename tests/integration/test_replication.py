"""Tests for k_replication."""

import time

import pytest

import ravendb_diagnostic as diag
from raven_lab import (
    delete_replication_task,
    params,
    print_lines,
    setup_external_replication,
    write_doc,
)


def test_outgoing_section_shows_destination_after_replication_is_wired(ravendb_cluster):
    """Repl wired n1 -> n2 -> k_replication on n1 lists n2's URL under OUTGOING."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]
    n2_url = info["urls"][1]

    setup_external_replication(src_node=n1, dst_node=n2)
    write_doc(n1, "users/1", {"name": "trigger"})

    lines = _wait_for(n1, n2_url)
    text = "\n".join(lines)

    print(f"\n    expected: OUTGOING section contains '{n2_url}'")
    print_lines("actual", lines)
    print()
    assert f"replication on {n1}/db1" in lines[0]
    assert "OUTGOING (1):" in text
    assert n2_url in text


def test_incoming_section_on_destination_shows_source(ravendb_cluster):
    """Repl wired n1 -> n2 -> k_replication on n2 lists the source under INCOMING."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    setup_external_replication(src_node=n1, dst_node=n2)
    write_doc(n1, "users/1", {"name": "trigger"})

    lines = _wait_for(n2, "INCOMING (1):")
    text = "\n".join(lines)

    print(f"\n    expected: INCOMING (1) on {n2}")
    print_lines("actual", lines)
    print()
    assert f"replication on {n2}/db1" in lines[0]
    assert "INCOMING (1):" in text


def test_teardown_brings_outgoing_back_to_zero(ravendb_cluster):
    """Deleting the repl task -> OUTGOING drops back to 0."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]
    n2_url = info["urls"][1]

    task_id = setup_external_replication(src_node=n1, dst_node=n2)
    write_doc(n1, "users/1", {"name": "trigger"})
    _wait_for(n1, n2_url)

    delete_replication_task(n1, task_id)

    lines = _wait_for(n1, "OUTGOING (0):")
    text = "\n".join(lines)

    print(f"\n    expected: OUTGOING (0) after task delete")
    print_lines("actual", lines)
    print()
    assert "OUTGOING (0):" in text



def test_zero_connections_when_no_tasks_configured(ravendb_cluster):
    """Fresh db with no replication tasks -> INCOMING (0), OUTGOING (0)."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_replication(params(target=node))
    text = "\n".join(lines)

    print(f"\n    expected: header for {node}/db1, INCOMING (0), OUTGOING (0)")
    print_lines("actual", lines)
    print()
    assert f"replication on {node}/db1" in lines[0]
    assert "INCOMING (0):" in text
    assert "OUTGOING (0):" in text


def test_raises_when_db_does_not_exist(ravendb_cluster):
    """Missing db -> k_replication raises RuntimeError."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises RuntimeError mentioning 'db missing or node unreachable'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        diag.k_replication(params(target=node, db_name="does-not-exist"))
    print(f"    actual:   {exc.value!s}\n")
    assert f"{node}/does-not-exist" in str(exc.value)


def _wait_for(node, substring, timeout=10):
    """Poll k_replication(node) until a line contains `substring`. Returns the last lines."""
    deadline = time.monotonic() + timeout
    lines = []
    while time.monotonic() < deadline:
        lines = diag.k_replication(params(target=node))
        if substring in "\n".join(lines):
            return lines
        time.sleep(0.5)
    return lines
