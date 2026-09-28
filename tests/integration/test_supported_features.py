"""Tests for k_supported_features."""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params


def test_reports_supported_features_for_existing_db(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    msg = diag.k_supported_features(params(target=node))

    print(f"\n    expected: contains 'DatabaseRecord.SupportedFeatures' and 'via {node}'")
    print(f"    actual:   {msg!r}\n")
    assert "DatabaseRecord.SupportedFeatures" in msg
    assert f"via {node}" in msg


def test_raises_when_db_does_not_exist(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises RuntimeError mentioning 'db missing or node unreachable'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        diag.k_supported_features(params(target=node, db_name="does-not-exist"))
    print(f"    actual:   {exc.value!s}\n")
    assert f"{node}/does-not-exist" in str(exc.value)


def test_raises_when_target_and_nodes_are_both_missing():
    print(f"\n    expected: raises ValueError mentioning 'requires `target`'")
    with pytest.raises(ValueError, match="requires `target`") as exc:
        diag.k_supported_features(params())
    print(f"    actual:   {exc.value!s}\n")


def test_raises_when_db_name_is_missing():
    print(f"\n    expected: raises ValueError mentioning 'requires `db_name`'")
    with pytest.raises(ValueError, match="requires `db_name`") as exc:
        diag.k_supported_features(params(target="1a", db_name=None))
    print(f"    actual:   {exc.value!s}\n")
