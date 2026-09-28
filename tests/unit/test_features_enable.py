"""Tests for ravendb_features kind=enable."""

from collections import defaultdict

import pytest

import ravendb_features as feat


def _params(**overrides):
    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    p["target"] = "1a"
    p["add"] = ["PullReplicationCompositeChangeVectors"]
    p["remove"] = []
    for k, v in overrides.items():
        p[k] = v
    return p


def _fake_request(post_status=200, record=None, capture_post=None):
    """request() stand-in returning POST status + a fake DatabaseRecord on GET."""
    import json as _json
    record_bytes = _json.dumps(record or {}).encode()

    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        if method == "POST" and "/admin/features" in path:
            if capture_post is not None:
                capture_post["body"] = body
                capture_post["target"] = target
            return post_status, b""
        if method == "GET" and "/admin/databases" in path:
            return 200, record_bytes
        raise AssertionError("unexpected call: %s %s" % (method, path))
    return fake


def test_enable_happy_path_with_canonical_field(monkeypatch):
    record = {"DatabaseFeatures": ["PullReplicationCompositeChangeVectors"]}
    captured = {}
    monkeypatch.setattr(feat, "request", _fake_request(record=record, capture_post=captured))

    lines = feat.k_enable(_params())

    print("\n    expected: PASS line + posted Add list captured\n    actual:\n%s\n" % "\n".join(lines))
    assert any("PASS" in line for line in lines)
    assert captured["body"]["Add"] == ["PullReplicationCompositeChangeVectors"]
    assert captured["body"]["Remove"] == []


def test_enable_verifies_via_anywhere_scan_when_no_canonical_field(monkeypatch):
    """Feature name present only in a nested field -> verification passes via fallback scan."""
    record = {"Settings": {"Raven.Replication.PullReplicationCompositeChangeVectors": "true"}}
    monkeypatch.setattr(feat, "request", _fake_request(record=record))

    lines = feat.k_enable(_params())

    assert any("PASS" in line for line in lines)


def test_enable_raises_when_feature_not_present_anywhere(monkeypatch):
    record = {"DatabaseFeatures": ["SomeOtherFeature"]}
    monkeypatch.setattr(feat, "request", _fake_request(record=record))

    with pytest.raises(RuntimeError) as exc:
        feat.k_enable(_params())

    msg = str(exc.value)
    print("\n    expected: FAIL mentioning feature NOT present\n    actual:\n%s\n" % msg)
    assert "NOT present after Add" in msg
    assert "PullReplicationCompositeChangeVectors" in msg


def test_enable_raises_when_post_returns_non_2xx(monkeypatch):
    monkeypatch.setattr(feat, "request", _fake_request(post_status=500))

    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        feat.k_enable(_params())

    print("\n    expected: RuntimeError mentioning HTTP 500\n    actual: %s\n" % exc.value)


def test_enable_rejects_empty_add_and_remove():
    with pytest.raises(ValueError, match="non-empty"):
        feat.k_enable(_params(add=[], remove=[]))


def test_enable_remove_path_verifies_feature_gone(monkeypatch):
    record = {"DatabaseFeatures": []}
    monkeypatch.setattr(feat, "request", _fake_request(record=record))

    lines = feat.k_enable(_params(add=[], remove=["LegacyFeature"]))

    assert any("PASS" in line for line in lines)


def test_enable_remove_path_raises_if_feature_still_present(monkeypatch):
    record = {"DatabaseFeatures": ["LegacyFeature"]}
    monkeypatch.setattr(feat, "request", _fake_request(record=record))

    with pytest.raises(RuntimeError, match="STILL present after Remove"):
        feat.k_enable(_params(add=[], remove=["LegacyFeature"]))
