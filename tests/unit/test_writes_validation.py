"""
Tests for ravendb_writes: each write kind rejects empty/no-op input and
raises when any individual write returns a non-2xx status.

ravendb_writes.request is monkeypatched with canned statuses -- no real RavenDB.
"""

from collections import defaultdict

import pytest

import ravendb_writes as writes


def params(**kwargs):
    """Params dict (defaultdict(None)) with sensible defaults pre-set."""
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


# ---- helpers ---------------------------------------------------------------

def _fake_ok(status):
    """Return a fake request() function that always returns (status, b'{}')."""
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        return status, b"{}"
    return fake


def _fake_first_n_ok_then_fail(n_ok, fail_status=500):
    """First n_ok calls return 201; subsequent calls return fail_status."""
    state = {"calls": 0}
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        state["calls"] += 1
        if state["calls"] <= n_ok:
            return 201, b"{}"
        return fail_status, b'{"error":"fake"}'
    return fake


def _wp(**kw):
    """Params bag with the common write fields pre-set."""
    return params(target="1a", **kw)


# ---- k_docs ----------------------------------------------------------------

def test_k_docs_raises_on_count_zero():
    print(f"\n    expected: ValueError mentioning '`count` >= 1'")
    with pytest.raises(ValueError, match="`count` >= 1") as exc:
        writes.k_docs(_wp(count=0))
    print(f"    actual:   {exc.value!s}\n")


def test_k_docs_raises_when_any_put_fails(monkeypatch):
    """3 PUTs, one returns 500 -> raises."""
    monkeypatch.setattr(writes, "request", _fake_first_n_ok_then_fail(2))

    print(f"\n    expected: RuntimeError mentioning '1/3 PUTs failed'")
    with pytest.raises(RuntimeError, match="1/3 PUTs failed") as exc:
        writes.k_docs(_wp(count=3))
    print(f"    actual:   {exc.value!s}\n")


def test_k_docs_returns_success_message_when_every_put_ok(monkeypatch):
    monkeypatch.setattr(writes, "request", _fake_ok(201))
    msg = writes.k_docs(_wp(count=3))
    print(f"\n    expected: 'WROTE 3/3 docs to 1a/db1'")
    print(f"    actual:   {msg!r}\n")
    assert "WROTE 3/3 docs to 1a/db1" in msg


# ---- k_docs_freeform -------------------------------------------------------

def test_k_docs_freeform_raises_on_count_zero():
    print(f"\n    expected: ValueError mentioning '`count` >= 1'")
    with pytest.raises(ValueError, match="`count` >= 1") as exc:
        writes.k_docs_freeform(_wp(count=0))
    print(f"    actual:   {exc.value!s}\n")


def test_k_docs_freeform_raises_when_any_put_fails(monkeypatch):
    monkeypatch.setattr(writes, "request", _fake_first_n_ok_then_fail(0))

    print(f"\n    expected: RuntimeError mentioning '2/2 PUTs failed'")
    with pytest.raises(RuntimeError, match="2/2 PUTs failed") as exc:
        writes.k_docs_freeform(_wp(count=2))
    print(f"    actual:   {exc.value!s}\n")


# ---- k_docs_revisions ------------------------------------------------------

def test_k_docs_revisions_raises_on_count_zero():
    print(f"\n    expected: ValueError mentioning '`count` >= 1'")
    with pytest.raises(ValueError, match="`count` >= 1") as exc:
        writes.k_docs_revisions(_wp(count=0, revs_per_doc=2))
    print(f"    actual:   {exc.value!s}\n")


def test_k_docs_revisions_raises_on_revs_zero():
    print(f"\n    expected: ValueError mentioning '`revs_per_doc` >= 1'")
    with pytest.raises(ValueError, match="`revs_per_doc` >= 1") as exc:
        writes.k_docs_revisions(_wp(count=2, revs_per_doc=0))
    print(f"    actual:   {exc.value!s}\n")


def test_k_docs_revisions_raises_when_a_rev_put_fails(monkeypatch):
    """count=2 revs=3 -> 6 expected PUTs.  Fail 1 of them -> raise."""
    monkeypatch.setattr(writes, "request", _fake_first_n_ok_then_fail(5))

    print(f"\n    expected: RuntimeError mentioning '1/6 PUTs failed'")
    with pytest.raises(RuntimeError, match="1/6 PUTs failed") as exc:
        writes.k_docs_revisions(_wp(count=2, revs_per_doc=3))
    print(f"    actual:   {exc.value!s}\n")


# ---- k_docs_interleaved ----------------------------------------------------

def test_k_docs_interleaved_raises_on_missing_prefixes():
    print(f"\n    expected: ValueError mentioning 'requires `prefixes`'")
    with pytest.raises(ValueError, match="requires `prefixes`") as exc:
        writes.k_docs_interleaved(_wp(count=2, prefixes=None))
    print(f"    actual:   {exc.value!s}\n")


def test_k_docs_interleaved_raises_on_count_zero():
    print(f"\n    expected: ValueError mentioning '`count` >= 1'")
    with pytest.raises(ValueError, match="`count` >= 1") as exc:
        writes.k_docs_interleaved(_wp(count=0, prefixes=["a", "b"]))
    print(f"    actual:   {exc.value!s}\n")


# ---- k_attachments ---------------------------------------------------------

def test_k_attachments_raises_on_count_zero():
    print(f"\n    expected: ValueError mentioning '`count` >= 1'")
    with pytest.raises(ValueError, match="`count` >= 1") as exc:
        writes.k_attachments(_wp(count=0))
    print(f"    actual:   {exc.value!s}\n")


def test_k_attachments_raises_when_any_put_fails(monkeypatch):
    monkeypatch.setattr(writes, "request", _fake_first_n_ok_then_fail(1))

    print(f"\n    expected: RuntimeError mentioning '1/2 PUTs failed'")
    with pytest.raises(RuntimeError, match="1/2 PUTs failed") as exc:
        writes.k_attachments(_wp(count=2))
    print(f"    actual:   {exc.value!s}\n")


# ---- k_counters ------------------------------------------------------------

def test_k_counters_raises_on_missing_doc_id():
    print(f"\n    expected: ValueError mentioning 'requires `doc_id`'")
    with pytest.raises(ValueError, match="requires `doc_id`") as exc:
        writes.k_counters(_wp(doc_id=None, repeat=1, delta=1))
    print(f"    actual:   {exc.value!s}\n")


def test_k_counters_raises_on_repeat_zero():
    print(f"\n    expected: ValueError mentioning '`repeat` >= 1'")
    with pytest.raises(ValueError, match="`repeat` >= 1") as exc:
        writes.k_counters(_wp(doc_id="users/0", repeat=0, delta=1))
    print(f"    actual:   {exc.value!s}\n")


def test_k_counters_raises_when_a_post_fails(monkeypatch):
    monkeypatch.setattr(writes, "request", _fake_first_n_ok_then_fail(2))

    print(f"\n    expected: RuntimeError mentioning '1/3 POSTs failed'")
    with pytest.raises(RuntimeError, match="1/3 POSTs failed") as exc:
        writes.k_counters(_wp(doc_id="users/0", repeat=3, delta=1))
    print(f"    actual:   {exc.value!s}\n")


# ---- k_timeseries ----------------------------------------------------------

def test_k_timeseries_append_raises_on_count_zero():
    print(f"\n    expected: ValueError mentioning '`count` >= 1'")
    with pytest.raises(ValueError, match="`count` >= 1") as exc:
        writes.k_timeseries(_wp(doc_id="users/0", count=0, interval_seconds=6,
                                delete_from=None, delete_to=None))
    print(f"    actual:   {exc.value!s}\n")


def test_k_timeseries_append_raises_when_an_entry_fails(monkeypatch):
    monkeypatch.setattr(writes, "request", _fake_first_n_ok_then_fail(1))

    print(f"\n    expected: RuntimeError mentioning '1/2 entries failed'")
    with pytest.raises(RuntimeError, match="1/2 entries failed") as exc:
        writes.k_timeseries(_wp(doc_id="users/0", count=2, interval_seconds=6,
                                delete_from=None, delete_to=None))
    print(f"    actual:   {exc.value!s}\n")


def test_k_timeseries_delete_range_raises_on_non_2xx(monkeypatch):
    """delete-range POST returns HTTP 500 -> raises."""
    monkeypatch.setattr(writes, "request", _fake_ok(500))

    print(f"\n    expected: RuntimeError mentioning 'delete-range' and 'HTTP 500'")
    with pytest.raises(RuntimeError, match="delete-range.*HTTP 500") as exc:
        writes.k_timeseries(_wp(doc_id="users/0",
                                delete_from="2024-01-01T00:00:00.000Z",
                                delete_to="2024-01-02T00:00:00.000Z"))
    print(f"    actual:   {exc.value!s}\n")


# ---- k_delete --------------------------------------------------------------

def test_k_delete_raises_on_no_ids_and_no_prefix():
    print(f"\n    expected: ValueError mentioning 'requires `ids`'")
    with pytest.raises(ValueError, match="requires `ids`") as exc:
        writes.k_delete(_wp(ids=None, id_prefix=None, count=None))
    print(f"    actual:   {exc.value!s}\n")


def test_k_delete_raises_when_any_delete_fails(monkeypatch):
    """3 ids, one returns non-204 -> raises."""
    monkeypatch.setattr(writes, "request", _fake_first_n_ok_then_fail(2, fail_status=500))
    state = {"calls": 0}
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        state["calls"] += 1
        if state["calls"] <= 2:
            return 204, b""
        return 500, b'{"error":"fake"}'
    monkeypatch.setattr(writes, "request", fake)

    print(f"\n    expected: RuntimeError mentioning '2/3 returned 204'")
    with pytest.raises(RuntimeError, match="2/3 returned 204") as exc:
        writes.k_delete(_wp(ids=["a", "b", "c"]))
    print(f"    actual:   {exc.value!s}\n")


# ---- k_restore_revision ----------------------------------------------------

def test_k_restore_revision_raises_on_missing_doc_id():
    print(f"\n    expected: ValueError mentioning 'requires `doc_id`'")
    with pytest.raises(ValueError, match="requires `doc_id`") as exc:
        writes.k_restore_revision(_wp(doc_id=None, revision_cv="A:1-aaa"))
    print(f"    actual:   {exc.value!s}\n")


def test_k_restore_revision_raises_on_missing_revision_cv():
    print(f"\n    expected: ValueError mentioning 'requires `revision_cv`'")
    with pytest.raises(ValueError, match="requires `revision_cv`") as exc:
        writes.k_restore_revision(_wp(doc_id="users/0", revision_cv=None))
    print(f"    actual:   {exc.value!s}\n")


def test_k_restore_revision_raises_on_get_failure(monkeypatch):
    """GET /revisions returns HTTP 500 -> raises."""
    monkeypatch.setattr(writes, "request", _fake_ok(500))

    print(f"\n    expected: RuntimeError mentioning 'GET revision' and 'HTTP 500'")
    with pytest.raises(RuntimeError, match="GET revision.*HTTP 500") as exc:
        writes.k_restore_revision(_wp(doc_id="users/0", revision_cv="A:1-aaa"))
    print(f"    actual:   {exc.value!s}\n")


def test_k_restore_revision_raises_on_put_failure(monkeypatch):
    """GET ok but PUT returned non-2xx -> raise."""
    import json
    state = {"calls": 0}
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        state["calls"] += 1
        if state["calls"] == 1:                # GET /revisions
            return 200, json.dumps({"Results": [{"Name": "Alice"}]}).encode()
        return 500, b'{"error":"fake"}'        # PUT
    monkeypatch.setattr(writes, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'PUT users/0' and 'HTTP 500'")
    with pytest.raises(RuntimeError, match="PUT users/0.*HTTP 500") as exc:
        writes.k_restore_revision(_wp(doc_id="users/0", revision_cv="A:1-aaa"))
    print(f"    actual:   {exc.value!s}\n")
