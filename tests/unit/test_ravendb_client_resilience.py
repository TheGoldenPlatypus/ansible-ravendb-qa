"""Tests for ravendb_client.request() resilience: SSL caching, Connection header, transient retry."""

import http.client
import ssl
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import ravendb_client as rc


@pytest.fixture(autouse=True)
def _reset_ssl_cache():
    rc._SSL_CONTEXT_CACHE.clear()
    yield
    rc._SSL_CONTEXT_CACHE.clear()


@pytest.fixture(autouse=True)
def _stub_ssl_setup(monkeypatch):
    monkeypatch.setattr(
        rc.ssl, "create_default_context",
        lambda *a, **kw: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)


def _fake_response(status=200, body=b'{"ok":true}'):
    """Minimal stand-in for what urlopen returns as a context manager."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_ssl_context_is_built_once_per_cert_pair_and_reused(monkeypatch):
    """Same (ca, client_cert) pair -> context built once and reused from cache."""
    create_calls = {"n": 0}
    stubbed_create = rc.ssl.create_default_context

    def counting_create(*a, **kw):
        create_calls["n"] += 1
        return stubbed_create(*a, **kw)
    monkeypatch.setattr(rc.ssl, "create_default_context", counting_create)

    with patch.object(rc, "urlopen", return_value=_fake_response()) as mocked:
        rc.request("GET", "1a", "test.local", "/x", "/ca.pem", "/client-A.pem")
        rc.request("GET", "1a", "test.local", "/y", "/ca.pem", "/client-A.pem")
        rc.request("GET", "1a", "test.local", "/z", "/ca.pem", "/client-B.pem")

    print(f"\n    expected: 2 context builds (A reused on 2nd call, B fresh)")
    print(f"    actual:   create_default_context called {create_calls['n']} time(s)")
    print(f"              cache size: {len(rc._SSL_CONTEXT_CACHE)}\n")
    assert create_calls["n"] == 2
    assert mocked.call_count == 3
    assert len(rc._SSL_CONTEXT_CACHE) == 2


def test_every_request_carries_connection_close_header(monkeypatch):
    """Every outgoing request carries 'Connection: close'."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    captured = {}

    def capturing_urlopen(req, **kw):
        captured["headers"] = dict(req.header_items())
        return _fake_response()
    monkeypatch.setattr(rc, "urlopen", capturing_urlopen)

    rc.request("PUT", "1a", "test.local", "/path", "/ca.pem", "/c.pem",
               body={"k": "v"})

    print(f"\n    expected: 'Connection: close' on the outgoing request")
    print(f"    actual:   headers={captured['headers']}\n")
    assert captured["headers"].get("Connection") == "close"
    assert captured["headers"].get("Content-type") == "application/json"


def test_retries_on_remote_disconnected_and_succeeds(monkeypatch):
    """A transient RemoteDisconnected -> one retry -> success."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def flaky_urlopen(req, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise http.client.RemoteDisconnected(
                "Remote end closed connection without response")
        return _fake_response(status=201, body=b"ok")
    monkeypatch.setattr(rc, "urlopen", flaky_urlopen)
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    status, body = rc.request("PUT", "1a", "test.local", "/x",
                              "/ca.pem", "/c.pem", body={"k": 1})

    print(f"\n    expected: 2 attempts (1st RemoteDisconnected, 2nd 201); status=201")
    print(f"    actual:   attempts={attempts['n']}  status={status}\n")
    assert attempts["n"] == 2
    assert status == 201
    assert body == b"ok"


def test_retries_on_each_documented_transient_class(monkeypatch):
    """Each transient transport error class -- raised directly or wrapped in URLError -- triggers a retry."""
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    transient_exceptions = [
        http.client.RemoteDisconnected("closed"),
        ConnectionResetError("reset"),
        ConnectionRefusedError("refused"),
        ConnectionAbortedError("aborted"),
        BrokenPipeError("broken pipe"),
        TimeoutError("socket timeout"),
        EOFError("eof before headers"),
        ssl.SSLEOFError("EOF in violation of protocol"),
    ]

    for shape in ("direct", "wrapped"):
        for exc_template in transient_exceptions:
            rc._SSL_CONTEXT_CACHE.clear()
            attempts = {"n": 0}

            def flaky(req, _exc=exc_template, **kw):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    if shape == "wrapped":
                        raise URLError(_exc)
                    raise _exc
                return _fake_response(status=200, body=b"ok")
            monkeypatch.setattr(rc, "urlopen", flaky)

            label = "%s (%s)" % (type(exc_template).__name__, shape)
            status, _ = rc.request("PUT", "1a", "test.local", "/x",
                                   "/ca.pem", "/c.pem", body=b"d")
            print(f"    {label:50}  attempts={attempts['n']}  status={status}")
            assert attempts["n"] == 2, "did not retry on %s" % label
            assert status == 200


def test_retries_on_connection_reset_wrapped_in_urlerror(monkeypatch):
    """ConnectionReset wrapped in URLError is detected as transient and retried."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def flaky_urlopen(req, **kw):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise URLError(ConnectionResetError("Connection reset by peer"))
        return _fake_response(status=200, body=b"")
    monkeypatch.setattr(rc, "urlopen", flaky_urlopen)
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    status, _ = rc.request("GET", "1a", "test.local", "/x",
                           "/ca.pem", "/c.pem")

    print(f"\n    expected: 3 attempts (2 resets then ok); status=200")
    print(f"    actual:   attempts={attempts['n']}  status={status}\n")
    assert attempts["n"] == 3
    assert status == 200


def test_exhausts_retries_then_raises(monkeypatch):
    """Transient failures past the retry budget -> raises the last exception."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def always_resets(req, **kw):
        attempts["n"] += 1
        raise http.client.RemoteDisconnected("not coming back")
    monkeypatch.setattr(rc, "urlopen", always_resets)
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    print(f"\n    expected: raises RemoteDisconnected after {rc._TRANSPORT_RETRY_MAX + 1} attempts")
    with pytest.raises(http.client.RemoteDisconnected, match="not coming back"):
        rc.request("PUT", "1a", "test.local", "/x", "/ca.pem", "/c.pem",
                   body=b"payload")
    print(f"    actual:   attempts={attempts['n']}\n")
    assert attempts["n"] == rc._TRANSPORT_RETRY_MAX + 1


def test_retries_on_bare_sslerror_with_unexpected_eof_reason(monkeypatch):
    """Bare ssl.SSLError with reason UNEXPECTED_EOF_WHILE_READING is retried."""
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    attempts = {"n": 0}

    def flaky(req, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            err = ssl.SSLError("EOF occurred in violation of protocol")
            err.reason = "UNEXPECTED_EOF_WHILE_READING"
            raise URLError(err)
        return _fake_response(status=200, body=b"")
    monkeypatch.setattr(rc, "urlopen", flaky)

    status, _ = rc.request("PUT", "1a", "test.local", "/x",
                           "/ca.pem", "/c.pem", body=b"d")

    print(f"\n    expected: 2 attempts (1st bare-SSLError UNEXPECTED_EOF, 2nd ok)")
    print(f"    actual:   attempts={attempts['n']}  status={status}\n")
    assert attempts["n"] == 2
    assert status == 200


def test_does_not_retry_on_ssl_cert_verification_error(monkeypatch):
    """ssl.SSLError with a non-EOF reason -> not retried, raised on first try."""
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    attempts = {"n": 0}

    def cert_fail(req, **kw):
        attempts["n"] += 1
        err = ssl.SSLError("certificate verify failed")
        err.reason = "CERTIFICATE_VERIFY_FAILED"
        raise URLError(err)
    monkeypatch.setattr(rc, "urlopen", cert_fail)

    print(f"\n    expected: URLError raised on first try, no retries")
    with pytest.raises(URLError, match="certificate verify failed"):
        rc.request("PUT", "1a", "test.local", "/x", "/ca.pem", "/c.pem")
    print(f"    actual:   attempts={attempts['n']}\n")
    assert attempts["n"] == 1


def test_does_not_retry_on_real_http_error(monkeypatch):
    """HTTPError (4xx/5xx) is returned once with its status, not retried."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def always_500(req, **kw):
        attempts["n"] += 1
        raise HTTPError(req.full_url, 500, "Internal Server Error",
                        hdrs=None, fp=None)
    monkeypatch.setattr(HTTPError, "read",
                        lambda self: b"server boom", raising=False)
    monkeypatch.setattr(rc, "urlopen", always_500)

    status, body = rc.request("PUT", "1a", "test.local", "/x",
                              "/ca.pem", "/c.pem", body={"k": 1})

    print(f"\n    expected: 1 attempt only (HTTPError not retried); status=500")
    print(f"    actual:   attempts={attempts['n']}  status={status}\n")
    assert attempts["n"] == 1
    assert status == 500
    assert body == b"server boom"


def test_does_not_retry_on_generic_value_error(monkeypatch):
    """A non-transport error surfaces immediately with no retries."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def boom(req, **kw):
        attempts["n"] += 1
        raise ValueError("not a transport problem")
    monkeypatch.setattr(rc, "urlopen", boom)

    print(f"\n    expected: ValueError raised on first try, no retries")
    with pytest.raises(ValueError, match="not a transport problem"):
        rc.request("PUT", "1a", "test.local", "/x", "/ca.pem", "/c.pem")
    print(f"    actual:   attempts={attempts['n']}\n")
    assert attempts["n"] == 1
