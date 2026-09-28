"""Tests for k_doc_id_set_parity."""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines, write_doc


def test_passes_when_all_ids_present_on_every_node(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(3):
        write_doc(n1, f"users/{i}", {"i": i})
        write_doc(n2, f"users/{i}", {"i": i})

    lines = diag.k_doc_id_set_parity(
        params(nodes=[n1, n2], id_prefix="users", count=3))
    text = "\n".join(lines)

    print(f"\n    expected: contains 'PASS  every probe id is uniformly present or uniformly absent'")
    print_lines("actual", lines)
    print()
    assert "mismatched: 0" in text
    assert "PASS" in text


def test_passes_when_all_ids_absent_on_every_node(ravendb_cluster):
    """Ids absent on every node -> PASS (uniformly absent)."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    lines = diag.k_doc_id_set_parity(
        params(nodes=[n1, n2], ids=["never-existed/0", "never-existed/1"]))
    text = "\n".join(lines)

    print(f"\n    expected: PASS even though both nodes returned 404 for every probe")
    print_lines("actual", lines)
    print()
    assert "mismatched: 0" in text
    assert "PASS" in text


def test_split_fails_when_id_present_on_one_node_only(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "only-on-n1"})

    lines = diag.k_doc_id_set_parity(
        params(nodes=[n1, n2], ids=["users/0"]))
    text = "\n".join(lines)

    print(f"\n    expected: contains 'FAIL  doc-id-set split' and lists users/0")
    print_lines("actual", lines)
    print()
    assert "FAIL  doc-id-set split" in text
    assert "users/0" in text


def test_assert_mode_raises_on_persisted_split(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "only-on-n1"})

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  doc-id-set split'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_doc_id_set_parity(
            params(nodes=[n1, n2], ids=["users/0"], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  doc-id-set split" in exc.value.lines[-1]


def test_unreachable_node_raises_loud(ravendb_cluster):
    """Any unreachable node -> RuntimeError."""
    info = ravendb_cluster(n_nodes=1)

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_doc_id_set_parity(
            params(nodes=["dead-node"], ids=["users/0"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)


def test_missing_ids_and_prefix_raises(ravendb_cluster):
    """No `ids` and no (`id_prefix` + `count`) -> ValueError."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `ids` OR'")
    with pytest.raises(ValueError, match="requires `ids` OR") as exc:
        diag.k_doc_id_set_parity(params(nodes=[node]))
    print(f"    actual:   {exc.value!s}\n")
