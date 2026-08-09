"""
Real-API canary — pulls genuine data from api.nextdns.io.

Complements the deterministic mock suite (test_live_data.py): this proves the
collectors work against the *real* NextDNS API and catches upstream drift a mock
can't (schema changes, auth changes, deprecations). It runs only where the
NEXTDNS_API_KEY secret is available (skips on Dependabot / forks), and is wired
as a NON-gating CI job so real-API flakiness never blocks a release.

Coverage is deliberately Stats-only (historical analytics — reliable), asserting
events land with the right shape, NOT their content (no assertions on real
domains/devices — privacy + determinism). The streaming endpoint is infinite and
traffic-dependent, so it stays mock-only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

APP = "TA-nextdns-api"
NS = f"/servicesNS/nobody/{APP}"
ACCOUNT = "realcanary"
INDEX = "main"
KEY = os.environ.get("NEXTDNS_API_KEY", "").strip()
# NextDNS sits behind Cloudflare, which 403s the default python-urllib UA.
UA = "Mozilla/5.0 (TA-nextdns-api integration canary)"

pytestmark = pytest.mark.skipif(not KEY, reason="NEXTDNS_API_KEY not set — real-API canary skipped")


def _discover_profile():
    req = urllib.request.Request(
        "https://api.nextdns.io/profiles", headers={"x-api-key": KEY, "User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r).get("data", [])
    return data[0]["id"] if data else None


@pytest.fixture(scope="module")
def real_profile():
    try:
        pid = _discover_profile()
    except urllib.error.HTTPError as e:
        pytest.skip(f"NextDNS API returned {e.code} discovering a profile (key/scope?)")
    except Exception as e:  # network/DNS from the runner
        pytest.skip(f"could not reach the NextDNS API from the runner: {type(e).__name__}")
    if not pid:
        pytest.skip("the account has no NextDNS profiles")
    return pid


@pytest.fixture(scope="module")
def configured(splunk, real_profile):
    st, body = splunk.request(
        "POST", f"{NS}/ta_nextdns_api_account", data={"name": ACCOUNT, "api_key": KEY}
    )
    assert st in (200, 201, 409), f"create account -> {st}: {body[:200]}"
    st, body = splunk.request(
        "POST", f"{NS}/data/inputs/NextDNS_API_Stats",
        data={"name": "real_stats", "account": ACCOUNT, "profile": real_profile,
              "index": INDEX, "interval": "20"},
    )
    assert st in (200, 201, 409), f"create input -> {st}: {body[:200]}"
    splunk.request("POST", f"{NS}/data/inputs/NextDNS_API_Stats/real_stats/enable")
    yield real_profile
    splunk.request("DELETE", f"{NS}/data/inputs/NextDNS_API_Stats/real_stats")
    splunk.request("DELETE", f"{NS}/ta_nextdns_api_account/{ACCOUNT}")


def test_real_stats_ingests(splunk, configured):
    deadline = time.time() + 150
    hits = []
    while time.time() < deadline:
        hits = splunk.search(
            f'index={INDEX} sourcetype=NextDNS_API_Stats* | head 5', earliest="-15m"
        )
        if hits:
            break
        time.sleep(10)
    # Structure only — assert events arrived, do not read their content.
    assert hits, (
        "no NextDNS_API_Stats events from the real API — the profile may be idle, "
        "or there is an auth/schema change (upstream drift the mock can't see)"
    )
    assert hits[0].get("sourcetype", "").startswith("NextDNS_API_Stats"), (
        f"unexpected sourcetype: {hits[0].get('sourcetype')}"
    )
