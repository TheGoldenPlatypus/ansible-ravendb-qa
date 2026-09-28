"""Tests for k_stats_parity."""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, write_doc


def test_stats_parity_passes_trivially_on_single_node(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_stats_parity(params(nodes=[node], assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: '/stats parity' header + 'PASS', no MISMATCH")
    print(f"    actual:   {lines[-1]!r}\n")
    assert "/stats parity" in text
    assert "PASS" in text
    assert "MISMATCH" not in text


def test_passes_when_two_nodes_have_identical_stats(ravendb_cluster):
    """Two empty nodes -> every field 0 -> PASS."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    lines = diag.k_stats_parity(params(nodes=[n1, n2], assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: PASS, no MISMATCH")
    print(f"    actual:   {lines[-1]!r}\n")
    assert "PASS" in text
    assert "MISMATCH" not in text


def test_fails_on_count_of_documents_divergence(ravendb_cluster):
    """Divergent CountOfDocuments -> MISMATCH and FAIL line."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(5):
        write_doc(n1, f"users/{i}", {"i": i})
    for i in range(2):
        write_doc(n2, f"users/{i}", {"i": i})

    lines = diag.k_stats_parity(params(nodes=[n1, n2]))
    text = "\n".join(lines)

    print(f"\n    expected: contains 'MISMATCH' and 'FAIL  /stats parity broken on'"
          f" mentioning CountOfDocuments")
    print(f"    actual:   {lines[-1]!r}\n")
    assert "MISMATCH" in text
    assert "FAIL  /stats parity broken on" in text
    assert "CountOfDocuments" in lines[-1]


def test_assert_mode_raises_on_divergence(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(5):
        write_doc(n1, f"users/{i}", {"i": i})
    for i in range(2):
        write_doc(n2, f"users/{i}", {"i": i})

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  /stats parity broken on'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_stats_parity(params(nodes=[n1, n2], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  /stats parity broken on" in exc.value.lines[-1]


def test_assert_fields_override_narrows_what_fails(ravendb_cluster):
    """assert_fields limits checks to CountOfTombstones -> no FAIL, doc drift is info-only."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(5):
        write_doc(n1, f"users/{i}", {"i": i})
    for i in range(2):
        write_doc(n2, f"users/{i}", {"i": i})

    lines = diag.k_stats_parity(params(
        nodes=[n1, n2],
        assert_fields=["CountOfTombstones"],
        assert_mode=True,
    ))
    text = "\n".join(lines)

    print(f"\n    expected: PASS (only CountOfTombstones asserted; both 0 -> uniform);"
          f" CountOfDocuments line says 'DRIFT (info)'")
    print(f"    actual:   {lines[-1]!r}\n")
    assert "PASS" in text
    assert "FAIL" not in text
    assert "DRIFT (info)" in text


def test_all_nodes_unreachable_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises ValueError mentioning 'no probed node has database'")
    with pytest.raises(ValueError, match="no probed node has database") as exc:
        diag.k_stats_parity(params(nodes=["dead-node"]))
    print(f"    actual:   {exc.value!s}\n")
