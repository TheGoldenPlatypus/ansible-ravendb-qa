"""ravendb_client -- HTTP helpers for talking to RavenDB nodes with client-cert TLS and transient-error retry."""

import errno
import http.client
import json
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Test-only escape hatch: a mapped target URL is used verbatim instead of
# composing https://<target>.<domain>:443.  When it is http://, SSL is skipped.
TARGET_URL_OVERRIDES: dict = {}


# SSL context cache: one context per (ca_cert, client_cert) pair, reused.
_SSL_CONTEXT_CACHE: dict = {}


def _ssl_context_for(client_cert, ca_cert):
    """Return a cached (or freshly built) SSL context for the given cert pair."""
    key = (ca_cert, client_cert)
    ctx = _SSL_CONTEXT_CACHE.get(key)
    if ctx is None:
        ctx = ssl.create_default_context(cafile=ca_cert)
        ctx.load_cert_chain(certfile=client_cert)
        _SSL_CONTEXT_CACHE[key] = ctx
    return ctx


# Retry budget for transient transport errors: 4 retries with escalating backoff.
_TRANSPORT_RETRY_MAX = 4
_TRANSPORT_RETRY_BACKOFF_SECS = (0.2, 0.5, 1.0, 2.0)

# ssl.SSLError.reason strings treated as transient; everything else bubbles up.
_TRANSIENT_SSL_REASONS = frozenset([
    "UNEXPECTED_EOF_WHILE_READING",
    "WRONG_VERSION_NUMBER",
    "SSL_ERROR_SYSCALL",
    "APPLICATION_DATA_AFTER_CLOSE_NOTIFY",
    "TLSV1_ALERT_INTERNAL_ERROR",
    "TLSV1_ALERT_USER_CANCELLED",
])

# OSError.errno values worth retrying.
_TRANSIENT_ERRNOS = frozenset([
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.EPIPE,
    errno.ETIMEDOUT,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ENETRESET,
    errno.EAGAIN,
])
_eho = getattr(errno, "EHOSTDOWN", None)
if _eho is not None:
    _TRANSIENT_ERRNOS = _TRANSIENT_ERRNOS | {_eho}


# Direct transport-failure exception classes.
_TRANSIENT_DIRECT_TYPES = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.IncompleteRead,
    http.client.LineTooLong,
    ConnectionResetError,
    ConnectionRefusedError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
    socket.timeout,
    socket.gaierror,
    EOFError,
    ssl.SSLEOFError,
)


def _is_transient_transport_error(exc):
    """Return True if `exc` is a transport-level failure worth retrying."""
    if isinstance(exc, _TRANSIENT_DIRECT_TYPES):
        return True

    if isinstance(exc, ssl.SSLError):
        reason = getattr(exc, "reason", "") or ""
        if reason in _TRANSIENT_SSL_REASONS:
            return True

    if isinstance(exc, OSError) and not isinstance(exc, _TRANSIENT_DIRECT_TYPES):
        if getattr(exc, "errno", None) in _TRANSIENT_ERRNOS:
            return True

    # URLError commonly wraps the above; unwrap one level (recursively).
    if isinstance(exc, URLError):
        inner = getattr(exc, "reason", None)
        if inner is not None and _is_transient_transport_error(inner):
            return True

    return False


def request(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
    """Send one HTTP request to `target` with transient-error retry; return (status, body)."""
    override = TARGET_URL_OVERRIDES.get(target)
    if override:
        url = override.rstrip("/") + path
        ctx = None
        if url.startswith("https://"):
            ctx = _ssl_context_for(client_cert, ca_cert)
    else:
        url = "https://" + target + "." + domain + ":443" + path
        ctx = _ssl_context_for(client_cert, ca_cert)

    data = None
    headers = {"Connection": "close"}
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type or "application/json"
    elif isinstance(body, str):
        data = body.encode()
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif isinstance(body, bytes):
        data = body
        headers["Content-Type"] = content_type or "application/octet-stream"

    req = Request(url, data=data, method=method, headers=headers)

    last_exc = None
    for attempt in range(_TRANSPORT_RETRY_MAX + 1):
        try:
            if ctx is not None:
                with urlopen(req, context=ctx, timeout=timeout) as response:
                    return response.status, response.read()
            else:
                with urlopen(req, timeout=timeout) as response:
                    return response.status, response.read()
        except HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            if attempt < _TRANSPORT_RETRY_MAX and _is_transient_transport_error(e):
                time.sleep(_TRANSPORT_RETRY_BACKOFF_SECS[attempt])
                last_exc = e
                continue
            raise

    raise last_exc if last_exc is not None else RuntimeError(
        "request() exhausted retries without an exception -- this is a bug")


def request_per_node(method, targets, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
    """Send the same request to each target in parallel; return a list of (target, status, body)."""
    def call_one(target):
        try:
            status, response = request(method, target, domain, path,
                                       client_cert, ca_cert,
                                       body=body,
                                       content_type=content_type,
                                       timeout=timeout)
            return (target, status, response)
        except Exception as e:
            return (target, None, repr(e))

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        for result in pool.map(call_one, targets):
            results.append(result)
    return results


