"""Tests for ravendb_wait: classify_nodes, snapshot_stats_field, and the wait kinds."""

import json
from collections import defaultdict

import pytest

import ravendb_wait as waitmod


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["timeout"] = 5
    p["poll_interval"] = 0       # spin fast in tests
    p.update(kwargs)
    return p


def _stats_body(field_values):
    """Build a /stats response body from a {field: value} dict."""
    return json.dumps(field_values).encode()


def test_classify_nodes_raises_when_a_node_is_unreachable(monkeypatch):
    """One node unreachable (status None) -> classify_nodes raises."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({"CountOfDocuments": 5})),
            ("1b", None, "connection refused"),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning '1b' and 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        waitmod.classify_nodes(params(), ["1a", "1b"])
    print(f"    actual:   {exc.value!s}\n")
    assert "1b" in str(exc.value)


def test_classify_nodes_tolerates_genuine_wrong_cluster_404(monkeypatch):
    """A node returning 404 (does not host the db) -> skipped, no raise."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({})),
            ("1b", 404, b"db not found here"),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    host_map, skipped = waitmod.classify_nodes(params(), ["1a", "1b"])
    print(f"\n    expected: host_map={{'1a': None}}  skipped=['1b']")
    print(f"    actual:   host_map={host_map}  skipped={skipped}\n")
    assert host_map == {"1a": None}
    assert skipped == ["1b"]


def test_snapshot_stats_field_raises_when_a_node_returns_non_200(monkeypatch):
    """One node returns HTTP 500 -> snapshot_stats_field raises."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({"LastDatabaseEtag": 42})),
            ("1b", 500, b'{"error":"fake"}'),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 500' and '1/2 node(s)'")
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        waitmod.snapshot_stats_field(params(), ["1a", "1b"], "LastDatabaseEtag")
    print(f"    actual:   {exc.value!s}\n")
    assert "1b" in str(exc.value)


def test_snapshot_stats_field_raises_when_a_node_is_unreachable(monkeypatch):
    """One node reports a connection error (status None) -> raises."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({"LastDatabaseEtag": 42})),
            ("1b", None, "ConnectionRefusedError('1b')"),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'connection error' and '1b'")
    with pytest.raises(RuntimeError, match="connection error") as exc:
        waitmod.snapshot_stats_field(params(), ["1a", "1b"], "LastDatabaseEtag")
    print(f"    actual:   {exc.value!s}\n")
    assert "1b" in str(exc.value)


def test_snapshot_stats_field_raises_when_field_missing_in_response(monkeypatch):
    """200 response but the requested field is absent -> raises."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [("1a", 200, _stats_body({"SomeOtherField": 1}))]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'no .LastDatabaseEtag. field'")
    with pytest.raises(RuntimeError, match="no 'LastDatabaseEtag' field") as exc:
        waitmod.snapshot_stats_field(params(), ["1a"], "LastDatabaseEtag")
    print(f"    actual:   {exc.value!s}\n")


def test_snapshot_stats_field_happy_path(monkeypatch):
    """Every node responds 200 with the field -> returns the snap dict."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({"LastDatabaseEtag": 1})),
            ("1b", 200, _stats_body({"LastDatabaseEtag": 2})),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    snap = waitmod.snapshot_stats_field(params(), ["1a", "1b"], "LastDatabaseEtag")
    print(f"\n    expected: {{'1a': 1, '1b': 2}}")
    print(f"    actual:   {snap}\n")
    assert snap == {"1a": 1, "1b": 2}


def test_k_quiescence_raises_when_every_node_returns_missing_cv(monkeypatch):
    """Every node returns a missing (None) CV -> raises, not QUIESCENT."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_snapshot(p, host_map, field):
        return {n: None for n in host_map}

    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "snapshot_stats_field", fake_snapshot)

    print(f"\n    expected: RuntimeError mentioning 'missing/empty'")
    with pytest.raises(RuntimeError, match="missing/empty") as exc:
        waitmod.k_quiescence(params(nodes=["1a", "1b"]))
    print(f"    actual:   {exc.value!s}\n")


def test_k_quiescence_raises_when_every_node_returns_empty_string_cv(monkeypatch):
    """Every node returns an empty-string CV -> raises."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_snapshot(p, host_map, field):
        return {n: "" for n in host_map}

    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "snapshot_stats_field", fake_snapshot)

    print(f"\n    expected: RuntimeError mentioning 'missing/empty'")
    with pytest.raises(RuntimeError, match="missing/empty") as exc:
        waitmod.k_quiescence(params(nodes=["1a", "1b"]))
    print(f"    actual:   {exc.value!s}\n")


def test_k_quiescence_happy_path_real_cv_agreement(monkeypatch):
    """Both nodes report the same non-empty CV -> QUIESCENT."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_snapshot(p, host_map, field):
        return {n: "A:5-abc" for n in host_map}

    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "snapshot_stats_field", fake_snapshot)

    lines = waitmod.k_quiescence(params(nodes=["1a", "1b"]))
    text = "\n".join(lines)
    print(f"\n    expected: 'QUIESCENT' + 'A:5-abc'")
    print(f"    actual:   {lines}\n")
    assert "QUIESCENT" in text
    assert "A:5-abc" in text


def test_k_conflicts_resolved_raises_immediately_when_node_unreachable(monkeypatch):
    """Unreachable node -> raises immediately, not 'never resolved' at timeout."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [("1a", None, "ConnectionRefusedError"),
                ("1b", None, "ConnectionRefusedError")]
    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'connection error' (NOT 'never resolved')")
    with pytest.raises(RuntimeError, match="connection error") as exc:
        waitmod.k_conflicts_resolved(params(nodes=["1a", "1b"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "never resolved" not in str(exc.value)


def test_k_conflicts_resolved_happy_path(monkeypatch):
    """Every node reports zero conflicts -> CONFLICTS RESOLVED."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [(t, 200, json.dumps({"TotalResults": 0, "Results": []}).encode())
                for t in targets]
    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    msg = waitmod.k_conflicts_resolved(params(nodes=["1a", "1b"]))
    print(f"\n    expected: 'CONFLICTS RESOLVED'")
    print(f"    actual:   {msg!r}\n")
    assert "CONFLICTS RESOLVED" in msg


def test_k_marker_propagation_raises_on_unreachable_target(monkeypatch):
    """Propagation poll sees unreachable targets -> raises loud."""
    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        return 201, b""

    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [(t, None, "ConnectionRefusedError") for t in targets]

    monkeypatch.setattr(waitmod, "request", fake_request)
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        waitmod.k_marker_propagation(params(source="1a", targets=["2a", "2b"]))
    print(f"    actual:   {exc.value!s}\n")


