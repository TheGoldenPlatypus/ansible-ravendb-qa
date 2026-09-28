"""Tests for k_doc_count."""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, write_doc, delete_doc


def test_doc_count_reports_zero_on_empty_db(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    msg = diag.k_doc_count(params(target=node))

    print(f"\n    expected: contains '{node}/db1' and '0 docs'")
    print(f"    actual:   {msg!r}\n")
    assert f"{node}/db1" in msg
    assert "0 docs" in msg


def test_doc_count_reports_n_after_n_writes(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    for i in range(3):
        write_doc(node, f"users/{i}", {"name": f"u{i}"})

    msg = diag.k_doc_count(params(target=node))

    print(f"\n    expected: contains '3 docs'")
    print(f"    actual:   {msg!r}\n")
    assert "3 docs" in msg


def test_doc_count_reflects_deletes(ravendb_cluster):
    """Write 5, delete 2 -> count reports 3."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    for i in range(5):
        write_doc(node, f"users/{i}", {"name": f"u{i}"})
    delete_doc(node, "users/0")
    delete_doc(node, "users/1")

    msg = diag.k_doc_count(params(target=node))

    print(f"\n    expected: contains '3 docs' (5 written, 2 deleted)")
    print(f"    actual:   {msg!r}\n")
    assert "3 docs" in msg


def test_doc_count_raises_when_db_does_not_exist(ravendb_cluster):
    """Missing db -> k_doc_count raises RuntimeError."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises RuntimeError mentioning 'db missing or node unreachable'"
          f" and containing '{node}/does-not-exist'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        diag.k_doc_count(params(target=node, db_name="does-not-exist"))
    print(f"    actual:   {exc.value!s}\n")
    assert f"{node}/does-not-exist" in str(exc.value)
