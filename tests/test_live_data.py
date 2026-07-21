"""
Live behaviour test — the keyed collectors actually ingest, end to end.

Both NextDNS inputs need an API key, so they can't be exercised against the real
api.nextdns.io without a secret (and without pulling real DNS data into a public
repo's CI). Instead the docker harness runs a mock upstream (docker/mock) and
points the collectors at it via NEXTDNS_API_BASE. This test configures an
account + both inputs and asserts events land with the mock's markers — proving
the fetch -> parse -> write_event path works on the Splunk 10 / Python 3.9
runtime, with no real key and no real data. Closes the E3a keyed-collector gap.
"""
from __future__ import annotations

import time

import pytest

APP = "TA-nextdns-api"
NS = f"/servicesNS/nobody/{APP}"
ACCOUNT = "mockacct"
PROFILE = "testprofile"
INDEX = "main"

INPUTS = {
    "NextDNS_API_Stats": "mock_stats",
    "NextDNS_API_Stream": "mock_stream",
}


@pytest.fixture(scope="module")
def configured(splunk):
    """Create the account + both inputs pointing at the mock, enabled."""
    st, body = splunk.request(
        "POST", f"{NS}/ta_nextdns_api_account",
        data={"name": ACCOUNT, "api_key": "mock-key-ignored-by-upstream"},
    )
    assert st in (200, 201, 409), f"create account -> {st}: {body[:300]}"

    for kind, name in INPUTS.items():
        st, body = splunk.request(
            "POST", f"{NS}/data/inputs/{kind}",
            data={"name": name, "account": ACCOUNT, "profile": PROFILE,
                  "index": INDEX, "interval": "20"},
        )
        assert st in (200, 201, 409), f"create {kind} -> {st}: {body[:300]}"
        # Idempotent enable (create may leave it disabled).
        splunk.request("POST", f"{NS}/data/inputs/{kind}/{name}/enable")

    yield INPUTS

    # Best-effort cleanup — a no-op on the throwaway CI container, tidy on a
    # shared/live Splunk.
    for kind, name in INPUTS.items():
        splunk.request("DELETE", f"{NS}/data/inputs/{kind}/{name}")
    splunk.request("DELETE", f"{NS}/ta_nextdns_api_account/{ACCOUNT}")


def _wait_for(splunk, spl, timeout=90):
    deadline = time.time() + timeout
    hits = []
    while time.time() < deadline:
        hits = splunk.search(spl, earliest="-15m")
        if hits:
            return hits
        time.sleep(10)
    return hits


def _collector_diag(splunk):
    """Pull the collectors' own log lines from _internal (search API, so no
    file-permission issues) to explain a no-events failure: whether the input
    ran, which URL it hit, and any exception."""
    rows = splunk.search(
        "search index=_internal earliest=-15m "
        "(source=*ta-nextdns* OR source=*ta_nextdns* OR component=ExecProcessor) "
        "(nextdns OR NextDNS OR getStats OR getStream OR api.nextdns.io OR mock) "
        "| head 25 | table _time component log_level _raw",
        earliest="-15m", count=25,
    )
    if not rows:
        return "(no ta-nextdns lines in index=_internal — the modular inputs likely never executed)"
    return "\n".join(f"  {r.get('component','')}/{r.get('log_level','')}: {r.get('_raw','')[:280]}" for r in rows)


def test_stats_input_indexes_events(splunk, configured):
    # `domains` is one of the ten analytic types the Stats collector fetches;
    # each becomes sourcetype NextDNS_API_Stats:<type>. Read fields off `| spath`
    # (the emitted JSON), not sourcetype auto-kv, so the assertion is deterministic.
    hits = _wait_for(splunk, f'index={INDEX} sourcetype="NextDNS_API_Stats:domains" | spath')
    assert hits, (
        "NextDNS_API_Stats produced no events. Collector log lines:\n"
        + _collector_diag(splunk)
    )
    markers = {h.get("mock_marker") for h in hits}
    assert "domains" in markers, f"unexpected Stats payload, mock_marker={markers}: {hits[0]}"


def test_stream_input_indexes_events(splunk, configured):
    hits = _wait_for(splunk, f"index={INDEX} sourcetype=NextDNS_API_Stream | spath")
    assert hits, (
        "NextDNS_API_Stream produced no events. Collector log lines:\n"
        + _collector_diag(splunk)
    )
    assert any(h.get("mock_marker") == "stream" for h in hits), (
        f"unexpected Stream payload: {hits[0]}"
    )
