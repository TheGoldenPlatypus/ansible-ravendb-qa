"""Tests for k_schema_version."""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines


def test_schema_version_reports_one_line_per_node(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_schema_version(params(target=node, nodes=[node]))

    print(f"\n    expected: lines[0] == 'schema/build version per node:'"
          f" and lines[1] contains '{node}'")
    print_lines("actual", lines)
    print()
    assert lines[0] == "schema/build version per node:"
    assert node in lines[1]


def test_expected_version_match_does_not_raise(ravendb_cluster):
    """assert_mode + a matching expected_version substring -> no raise."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_schema_version(params(nodes=[node]))
    actual_version_line = lines[1].strip()
    version_prefix = actual_version_line.split()[-1].split(".")[0]

    print(f"\n    expected: no raise; expected_version='{version_prefix}' matches '{actual_version_line}'")
    result = diag.k_schema_version(
        params(nodes=[node], expected_version=version_prefix, assert_mode=True))
    print(f"    actual:   returned {result}\n")
    assert lines[0] == result[0]


def test_expected_version_mismatch_raises(ravendb_cluster):
    """assert_mode + an absent expected_version -> raises DiagnosticViolation."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises DiagnosticViolation mentioning 'nodes missing expected'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_schema_version(
            params(nodes=[node], expected_version="9.99.99", assert_mode=True))
    text = "\n".join(exc.value.lines)
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "nodes missing expected '9.99.99'" in text
    assert node in text


def test_all_nodes_unreachable_raises(ravendb_cluster):
    """No node responds to /build/version -> RuntimeError."""
    info = ravendb_cluster(n_nodes=1)

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'no /build/version response from any node'")
    with pytest.raises(RuntimeError, match="no /build/version response from any node") as exc:
        diag.k_schema_version(params(nodes=["dead-node"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)
