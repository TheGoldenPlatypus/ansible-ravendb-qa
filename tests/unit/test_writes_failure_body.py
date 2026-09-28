"""Tests for the failure-body capture in ravendb_writes._format_failure."""

from collections import defaultdict
import json

import pytest

import ravendb_writes as writes


def test_format_failure_extracts_message_from_json_envelope():
    body = json.dumps({
        "Type": "Raven.Server.Documents.MergedDocumentTransactionException",
        "Message": "Transaction merger queue is full; try again later",
    }).encode()
    out = writes._format_failure("users/0", 500, body)
    assert "users/0" in out
    assert "HTTP 500" in out
    assert "Transaction merger queue is full" in out


def test_format_failure_falls_back_to_error_field():
    body = json.dumps({
        "Type": "X",
        "Error": "Internal stack: at ... at ...",
    }).encode()
    out = writes._format_failure("users/0", 500, body)
    assert "Internal stack" in out


def test_format_failure_uses_raw_body_when_not_json():
    body = b"<html><body>503 Service Unavailable</body></html>"
    out = writes._format_failure("users/0", 503, body)
    assert "HTTP 503" in out
    assert "503 Service Unavailable" in out


def test_format_failure_omits_colon_when_body_empty():
    out = writes._format_failure("users/0", 500, b"")
    assert out == "users/0 -> HTTP 500"
    assert out.endswith("500")   # no trailing ": "


def test_format_failure_truncates_long_messages_to_200_chars():
    long = "x" * 500
    body = json.dumps({"Message": long}).encode()
    out = writes._format_failure("users/0", 500, body)
    assert "x" * 200 in out
    assert "x" * 201 not in out


def test_k_docs_failure_message_includes_server_exception(monkeypatch):
    """Server 500 with an error envelope -> k_docs RuntimeError includes the Message text."""
    server_body = json.dumps({
        "Type": "Raven.Server.Documents.SomethingException",
        "Message": "License does not allow more than 10 docs",
    }).encode()

    def fake_put_idempotent(p, target, path, body):
        return 500, server_body
    monkeypatch.setattr(writes, "put_idempotent", fake_put_idempotent)
    monkeypatch.setattr(writes.time, "sleep", lambda _s: None)

    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    p["target"] = "1a"
    p["count"] = 2
    p["id_prefix"] = "users"

    with pytest.raises(RuntimeError, match="License does not allow") as exc:
        writes.k_docs(p)
    assert "HTTP 500" in str(exc.value)
