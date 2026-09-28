"""Tests for k_doc_count_parity."""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines, write_doc


def test_single_node_passes_trivially(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_doc_count_parity(params(nodes=[node]))
    text = "\n".join(lines)

    print(f"\n    expected: contains 'PASS' and not 'FAIL'")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "FAIL" not in text


def test_passes_when_two_independent_nodes_have_the_same_count(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(3):
        write_doc(n1, f"users/{i}", {"i": i})
        write_doc(n2, f"users/{i}", {"i": i})

    lines = diag.k_doc_count_parity(params(nodes=[n1, n2]))
    text = "\n".join(lines)

    print(f"\n    expected: contains 'PASS  every node reports 3' (both nodes wrote 3)")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "every node reports 3" in text


def test_fails_when_counts_diverge(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(5):
        write_doc(n1, f"users/{i}", {"i": i})
    for i in range(2):
        write_doc(n2, f"users/{i}", {"i": i})

    lines = diag.k_doc_count_parity(params(nodes=[n1, n2]))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: contains 'FAIL  doc counts differ'")
    print_lines("actual", lines)
    print()
    assert "FAIL  doc counts differ" in text


def test_assert_mode_raises_on_divergence(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(5):
        write_doc(n1, f"users/{i}", {"i": i})
    for i in range(2):
        write_doc(n2, f"users/{i}", {"i": i})

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  doc counts differ'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_doc_count_parity(params(nodes=[n1, n2], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  doc counts differ" in exc.value.lines[-1]


def test_all_nodes_unreachable_raises(ravendb_cluster):
    """No node has the database -> ValueError."""
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises ValueError mentioning 'no probed node has database'")
    with pytest.raises(ValueError, match="no probed node has database") as exc:
        diag.k_doc_count_parity(params(nodes=["dead-node"]))
    print(f"    actual:   {exc.value!s}\n")
