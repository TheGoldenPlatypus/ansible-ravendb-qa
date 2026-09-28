"""Tests for k_revision_count_parity."""

import pytest

import ravendb_diagnostic as diag
from raven_lab import enable_revisions, params, print_lines, write_doc


def _three_revs(node, doc_id):
    """Write 3 revisions of `doc_id` on `node`."""
    for v in (1, 2, 3):
        write_doc(node, doc_id, {"v": v})


def test_passes_when_two_nodes_have_the_same_per_id_counts(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    enable_revisions(n1)
    enable_revisions(n2)
    _three_revs(n1, "users/0")
    _three_revs(n2, "users/0")

    lines = diag.k_revision_count_parity(
        params(nodes=[n1, n2], ids=["users/0"]))
    text = "\n".join(lines)

    print(f"\n    expected: PASS 'per-id revision counts agree across nodes'")
    print_lines("actual", lines)
    print()
    assert "mismatched: 0" in text
    assert "PASS" in text


def test_fails_when_counts_diverge(ravendb_cluster):
    """Per-id revision counts differ across nodes -> FAIL line."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    enable_revisions(n1)
    enable_revisions(n2)
    _three_revs(n1, "users/0")
    write_doc(n2, "users/0", {"v": 1})

    lines = diag.k_revision_count_parity(
        params(nodes=[n1, n2], ids=["users/0"]))
    text = "\n".join(lines)

    print(f"\n    expected: 'FAIL  revision count drift on:' line mentioning users/0")
    print_lines("actual", lines)
    print()
    assert "FAIL  revision count drift on" in text
    assert "users/0" in lines[-1]


def test_assert_mode_raises_on_drift(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    enable_revisions(n1)
    enable_revisions(n2)
    _three_revs(n1, "users/0")
    write_doc(n2, "users/0", {"v": 1})

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  revision count drift'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_revision_count_parity(
            params(nodes=[n1, n2], ids=["users/0"], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  revision count drift" in exc.value.lines[-1]


def test_expected_count_mismatch_fails(ravendb_cluster):
    """Counts agree across nodes but differ from expected_count -> FAIL line."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    enable_revisions(n1)
    enable_revisions(n2)
    _three_revs(n1, "users/0")
    _three_revs(n2, "users/0")

    lines = diag.k_revision_count_parity(
        params(nodes=[n1, n2], ids=["users/0"], expected_count=5))
    text = "\n".join(lines)

    print(f"\n    expected: contains 'FAIL  expected 5 revs/id' and 'users/0'")
    print_lines("actual", lines)
    print()
    assert "FAIL  expected 5 revs/id" in text
    assert "users/0" in lines[-1]


def test_unreachable_node_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_revision_count_parity(
            params(nodes=["dead-node"], ids=["users/0"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)
