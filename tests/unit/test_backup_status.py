"""Tests for get_backup_status in ravendb_backup."""

import json
from collections import defaultdict

import pytest

import ravendb_backup as backup


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


def test_returns_status_dict_on_200_with_data(monkeypatch):
    """200 with a populated Status field -> returns the dict."""
    expected_status = {
        "NodeTag": "A",
        "LastFullBackup": "2024-01-01T00:00:00Z",
        "LocalBackup": {"BackupDirectory": "/backups/db1"},
    }
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, json.dumps({"Status": expected_status}).encode()
    monkeypatch.setattr(backup, "request", fake)

    result = backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"\n    expected: returns the Status dict (NodeTag='A')")
    print(f"    actual:   {result!r}\n")
    assert result == expected_status


def test_returns_none_on_200_with_null_status(monkeypatch):
    """200 with Status=null -> returns None."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, json.dumps({"Status": None}).encode()
    monkeypatch.setattr(backup, "request", fake)

    result = backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"\n    expected: None (task has not produced a backup yet)")
    print(f"    actual:   {result!r}\n")
    assert result is None


def test_raises_on_http_500(monkeypatch):
    """HTTP 500 -> raises RuntimeError."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 500, b'{"error":"internal"}'
    monkeypatch.setattr(backup, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 500'")
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"    actual:   {exc.value!s}\n")


def test_raises_on_http_404(monkeypatch):
    """HTTP 404 -> raises RuntimeError."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 404, b'{"error":"not found"}'
    monkeypatch.setattr(backup, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 404'")
    with pytest.raises(RuntimeError, match="HTTP 404") as exc:
        backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"    actual:   {exc.value!s}\n")
