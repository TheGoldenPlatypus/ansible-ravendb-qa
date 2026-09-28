"""Tests for the zero-gap-drain fix in k_etag_parity and k_docs_drain."""

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
    p["poll_interval"] = 0    # spin fast in tests
    p.update(kwargs)
    return p


def _stats(field, value):
    return json.dumps({field: value}).encode()


def _patch_steady(monkeypatch, field, value, snapshot_counter):
    """Patch request_per_node and request to return the same steady value."""
    rounds = {"n": 0}

    def fake_rpn(method, targets, domain, path, *a, **kw):
        rounds["n"] += 1
        snapshot_counter.append(rounds["n"])
        return [(t, 200, _stats(field, value)) for t in targets]

    def fake_req(method, target, domain, path, *a, **kw):
        return 200, _stats(field, value)

    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)
    monkeypatch.setattr(waitmod, "request", fake_req)


def test_etag_parity_requires_at_least_two_predicate_iterations(monkeypatch):
    """Steady etag -> STABLE only after at least two predicate snapshots."""
    iters = {"n": 0}
    original_snap = waitmod.snapshot_stats_field

    def counting_snap(p, has_db, field):
        iters["n"] += 1
        return {t: 5456 for t in (has_db.keys() if isinstance(has_db, dict) else has_db)}
    monkeypatch.setattr(waitmod, "snapshot_stats_field", counting_snap)
    _patch_steady(monkeypatch, "LastDatabaseEtag", 5456, [])

    lines = waitmod.k_etag_parity(params(nodes=["2a", "2b", "2c"]))
    text = "\n".join(lines)

    print(f"\n    expected: STABLE, AND >=2 predicate snapshots (seed + compare)")
    print(f"    actual:   predicate snapshots={iters['n']}")
    print(f"              header: {lines[0]!r}\n")
    assert "STABLE" in text
    assert iters["n"] >= 2


def test_etag_parity_fails_loud_when_etag_still_moving(monkeypatch):
    """Etag keeps advancing -> RuntimeError (still moving), not STABLE."""
    advancing = iter([100, 101, 102, 103, 104, 105, 106, 107, 108])

    def fake_rpn(method, targets, domain, path, *a, **kw):
        return [(t, 200, _stats("LastDatabaseEtag", 0)) for t in targets]
    def fake_snap(p, has_db, field):
        v = next(advancing)
        return {t: v for t in (has_db.keys() if isinstance(has_db, dict) else has_db)}
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)
    monkeypatch.setattr(waitmod, "snapshot_stats_field", fake_snap)

    print(f"\n    expected: RuntimeError mentioning 'still moving'")
    with pytest.raises(RuntimeError, match="still moving") as exc:
        waitmod.k_etag_parity(params(
            nodes=["2a"], timeout=0.2, poll_interval=0.05))
    print(f"    actual:   first line = {str(exc.value).splitlines()[0]!r}\n")


def test_docs_drain_requires_at_least_two_predicate_iterations(monkeypatch):
    """Steady change vector -> DRAINED only after at least two predicate snapshots."""
    iters = {"n": 0}

    def counting_snap(p, has_db, field):
        iters["n"] += 1
        return {t: "A:10-x" for t in (has_db.keys() if isinstance(has_db, dict) else has_db)}
    monkeypatch.setattr(waitmod, "snapshot_stats_field", counting_snap)
    _patch_steady(monkeypatch, "DatabaseChangeVector", "A:10-x", [])

    lines = waitmod.k_docs_drain(params(nodes=["1a", "1b", "1c"]))
    text = "\n".join(lines)

    print(f"\n    expected: DRAINED, AND >=2 predicate snapshots (seed + compare)")
    print(f"    actual:   predicate snapshots={iters['n']}")
    print(f"              header: {lines[0]!r}\n")
    assert "DRAINED" in text
    assert iters["n"] >= 2
