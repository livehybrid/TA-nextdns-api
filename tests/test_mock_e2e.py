"""End to end on real Splunk against the synthetic mock: what live cannot do.

Many pages (the harness clamps pages to 300 logs / 100 analytics rows), 429
and 503, a persistent outage, a rejected key and empty windows. Real data is
covered by tests/live/.

Inputs run every 30 s. Assertions are searches over what Splunk indexed,
checked against the mock's timeline (one query per STEP_MS, sequence
k = timestamp // STEP_MS, de-duplicated view drops k % 3 == 2), computed here.
"""
from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request

import pytest

from ndtest import (
    NS,
    container_grep_count,
    create_account,
    create_input,
    delete_input,
    expect_py313,
    failures,
    python_versions,
    runs,
    wait_for_failures,
    wait_for_runs,
)

_host = urllib.parse.urlparse(os.environ.get("SPLUNK_MGMT_URL", "https://127.0.0.1:8089")).hostname
pytestmark = pytest.mark.skipif(
    _host not in ("127.0.0.1", "localhost", "::1"), reason="the mock runs beside a local docker Splunk"
)

MOCK = os.environ.get("NEXTDNS_MOCK_URL", "http://127.0.0.1:8080")
CONTAINER = os.environ.get("SPLUNK_CONTAINER", "ta_nextdns_api_splunk")
GOOD_KEY = "mock-good-key-7f3a"
BAD_KEY = "mock-bad-key"
LOOKBACK_S = 3600
STATS = "NextDNS_API_Stats"
LOGS = "NextDNS_API_Stream"
TYPES = ("status", "protocols", "devices", "ips", "reasons", "domains",
         "queryTypes", "ipVersions", "dnssec", "encryption")

# name -> (kind, account, profile, extra fields)
INPUTS = {
    "m_stats": (STATS, "m_good", "mockprofile", {}),
    "m_stats_some": (STATS, "m_good", "mockprofile", {"sections": "status,domains"}),
    "m_stats_flaky": (STATS, "m_good", "flakyprofile", {"sections": "status,domains"}),
    "m_logs": (LOGS, "m_good", "mockprofile", {}),
    "m_logs_raw": (LOGS, "m_good", "mockprofile", {"raw": "1"}),
    "m_logs_flaky": (LOGS, "m_good", "flakyprofile", {"raw": "1"}),
    "m_logs_quiet": (LOGS, "m_good", "quietprofile", {}),
    "m_stats_quiet": (STATS, "m_good", "quietprofile", {}),
    "m_logs_down": (LOGS, "m_good", "downprofile", {}),
    "m_logs_badkey": (LOGS, "m_bad", "mockprofile", {}),
}


def mock_get(path):
    with urllib.request.urlopen(MOCK + path, timeout=30) as r:
        return json.load(r)


@pytest.fixture(scope="module")
def step_ms():
    return mock_get("/__mock/info")["step_ms"]


@pytest.fixture(scope="module")
def inputs(splunk):
    create_account(splunk, "m_good", GOOD_KEY)
    create_account(splunk, "m_bad", BAD_KEY)
    for name, (kind, account, profile, extra) in INPUTS.items():
        create_input(splunk, kind, name, interval=30, account=account, profile=profile, **extra)
    for name in INPUTS:
        if not name.endswith(("_down", "_badkey")):
            wait_for_runs(splunk, name, 3)
    yield
    for name, (kind, *_rest) in INPUTS.items():
        delete_input(splunk, kind, name)


def source(name):
    return f'source="{INPUTS[name][0]}://{name}"'


def expected_seqs(lo, hi, raw):
    return [k for k in range(lo, hi + 1) if raw or k % 3 != 2]


# ------------------------------------------------------------------ logs ----


def _seq_counts(splunk, name):
    rows = splunk.search(f"index=main {source(name)} | stats count by mock_seq", earliest="-3h", count=0)
    return {int(r["mock_seq"]): int(r["count"]) for r in rows}


@pytest.mark.parametrize("name,raw", [("m_logs", False), ("m_logs_raw", True), ("m_logs_flaky", True)])
def test_logs_every_query_once_over_several_runs(splunk, inputs, step_ms, name, raw):
    done = runs(splunk, name)
    assert len(done) >= 3, f"{len(done)} runs completed"
    counts = _seq_counts(splunk, name)
    assert counts, "no events"
    assert max(counts.values()) == 1, f"{sum(1 for c in counts.values() if c > 1)} queries indexed twice"
    lo, hi = min(counts), max(counts)
    assert sorted(counts) == expected_seqs(lo, hi, raw), "gap or filtered rows in the indexed range"
    # the first run looked back an hour; later runs carried on from it
    assert (hi - lo + 1) * step_ms >= LOOKBACK_S * 1000
    assert sum(e for e, _ in done) == len(counts)


def test_logs_later_windows_start_at_the_checkpoint(splunk, inputs):
    done = runs(splunk, "m_logs")
    assert done[0][1] == LOOKBACK_S
    assert all(w < 120 for _, w in done[1:]), [w for _, w in done]


def test_logs_large_windows_were_split_not_paged(splunk, inputs):
    # The mock drops a row per cursor page, as the real API does on tied
    # timestamps, so only the split route can pass the completeness tests.
    assert mock_get("/__mock/info")["cursor_loss"]
    reqs = [r for r in mock_get("/__mock/requests")["data"] if r["path"] == "/profiles/mockprofile/logs"]
    assert len(reqs) >= 15  # the first hour alone does not fit in pages of 300
    assert not any("cursor" in r["query"] for r in reqs)
    assert all(10 <= int(r["query"]["limit"]) <= 1000 for r in reqs)


def test_logs_event_time_sourcetype_host_and_fields(splunk, inputs, step_ms):
    rows = splunk.search(
        f"index=main {source('m_logs')} "
        f"| eval off=abs(round(_time*1000) - mock_seq*{step_ms}) "
        "| eval want_blocked=if(mock_seq % 5 == 0, \"blocked\", \"permitted\") "
        "| eval want_device=\"mock-device-\" . (mock_seq % 4) "
        "| stats count, max(off) as off, values(sourcetype) as st, values(host) as host, "
        "sum(eval(if(blocked==want_blocked,1,0))) as blocked_ok, "
        "sum(eval(if(device_name==want_device,1,0))) as device_ok, "
        "sum(eval(if(client_ip==clientIp,1,0))) as ip_ok, "
        "dc(action) as actions",
        earliest="-3h",
    )
    r = rows[0]
    n = int(r["count"])
    assert n > 0
    assert float(r["off"]) == 0, "event time is not the query timestamp"
    assert r["st"] == "NextDNS_API_Stream" and r["host"] == "mockprofile"
    assert int(r["blocked_ok"]) == n, f"blocked wrong on {n - int(r['blocked_ok'])} of {n}"
    assert int(r["device_ok"]) == n, f"device_name wrong on {n - int(r['device_ok'])} of {n}"
    assert int(r["ip_ok"]) == n
    assert int(r["actions"]) == 2  # default, blocked


def test_logs_empty_windows_advance(splunk, inputs):
    assert splunk.search(f"index=main {source('m_logs_quiet')} | stats count")[0]["count"] == "0"
    done = runs(splunk, "m_logs_quiet")
    assert len(done) >= 3 and all(e == 0 for e, _ in done)
    assert done[0][1] == LOOKBACK_S and all(w < 120 for _, w in done[1:])
    assert not failures(splunk, "m_logs_quiet")


def test_stats_empty_windows_advance(splunk, inputs):
    assert splunk.search(f"index=main {source('m_stats_quiet')} | stats count")[0]["count"] == "0"
    done = runs(splunk, "m_stats_quiet")
    assert len(done) >= 3 and all(e == 0 for e, _ in done)
    assert all(w < 120 for _, w in done[1:])


def test_flaky_upstream_was_retried(splunk, inputs):
    reqs = [r for r in mock_get("/__mock/requests")["data"] if r["path"].startswith("/profiles/flakyprofile/")]
    statuses = [r["status"] for r in reqs]
    assert statuses.count(429) >= 3 and statuses.count(503) >= 3  # logs + 2 analytics paths
    assert not failures(splunk, "m_logs_flaky") and not failures(splunk, "m_stats_flaky")


def test_outage_logs_status_and_writes_nothing(splunk, inputs):
    lines = wait_for_failures(splunk, "m_logs_down", 2)
    assert len(lines) >= 2
    assert all("status=503" in line for line in lines)
    assert splunk.search(f"index=main {source('m_logs_down')} | stats count")[0]["count"] == "0"
    assert not runs(splunk, "m_logs_down")


def test_bad_key_is_reported_and_never_logged(splunk, inputs):
    lines = wait_for_failures(splunk, "m_logs_badkey", 1)
    assert lines and all("status=403 codes=authRequired" in line for line in lines)
    assert splunk.search(f"index=main {source('m_logs_badkey')} | stats count")[0]["count"] == "0"
    # positive control: the grep finds what is there
    assert container_grep_count(CONTAINER, "m_logs_badkey") > 0
    assert container_grep_count(CONTAINER, BAD_KEY) == 0
    assert container_grep_count(CONTAINER, GOOD_KEY) == 0


# ----------------------------------------------------------------- stats ----


def _stats_windows(splunk, name):
    rows = splunk.search(
        f"index=main {source(name)} "
        "| stats count, sum(queries) as q, dc(domain) as dd by sourcetype window_start window_end",
        earliest="-3h", count=0,
    )
    return rows


def _ms(iso):
    import calendar
    import time as _t

    main, _, frac = iso.rstrip("Z").partition(".")
    return calendar.timegm(_t.strptime(main, "%Y-%m-%dT%H:%M:%S")) * 1000 + int((frac + "000")[:3])


def _window_seqs(start_ms, end_ms, step_ms):
    return range(math.ceil(start_ms / step_ms), (end_ms - 1) // step_ms + 1)


def test_stats_every_type_every_window(splunk, inputs, step_ms):
    rows = _stats_windows(splunk, "m_stats")
    assert {r["sourcetype"] for r in rows} == {f"{STATS}:{t}" for t in TYPES}
    windows = sorted({(r["window_start"], r["window_end"]) for r in rows})
    assert len(windows) >= 3
    for (_, end), (start, _) in zip(windows, windows[1:]):
        assert end == start, "windows overlap or leave a gap"
    first = windows[0]
    assert _ms(first[1]) - _ms(first[0]) == LOOKBACK_S * 1000
    by = {(r["sourcetype"], r["window_start"]): r for r in rows}
    for start, end in windows:
        ks = _window_seqs(_ms(start), _ms(end), step_ms)
        status = by[(f"{STATS}:status", start)]
        assert int(float(status["q"])) == len(ks), "status total != queries in the window"
        assert int(status["count"]) == 2
        domains = by[(f"{STATS}:domains", start)]
        want = len({k % 1200 for k in ks})
        assert int(domains["count"]) == want == int(domains["dd"]), "domains missing or duplicated"
        for t in ("ips", "devices", "protocols", "queryTypes", "ipVersions", "dnssec", "encryption"):
            assert int(float(by[(f"{STATS}:{t}", start)]["q"])) == len(ks), t
    assert sum(e for e, _ in runs(splunk, "m_stats")) == sum(int(r["count"]) for r in rows)


def test_stats_analytics_pages_followed(splunk, inputs):
    reqs = [r for r in mock_get("/__mock/requests")["data"] if r["path"] == "/profiles/mockprofile/analytics/domains"]
    assert sum(1 for r in reqs if r["query"].get("cursor")) >= 11  # 1200 domains in the first hour, pages of 100
    assert all(1 <= int(r["query"]["limit"]) <= 500 for r in reqs)


@pytest.mark.parametrize("name", ["m_stats_some", "m_stats_flaky"])
def test_stats_sections_honoured(splunk, inputs, step_ms, name):
    rows = _stats_windows(splunk, name)
    assert {r["sourcetype"] for r in rows} == {f"{STATS}:status", f"{STATS}:domains"}
    for r in rows:
        if r["sourcetype"].endswith(":status"):
            ks = _window_seqs(_ms(r["window_start"]), _ms(r["window_end"]), step_ms)
            assert int(float(r["q"])) == len(ks)


# ---------------------------------------------------------- REST / runtime ----


def test_profile_dropdown_lists_the_accounts_profiles(splunk, inputs):
    data = splunk.get_json(f"{NS}/ta_nextdns_api_select_profile", account="m_good")
    entries = {e["name"]: e["content"].get("profile") for e in data.get("entry", [])}
    want = [p["id"] for p in mock_get_profiles()]
    assert sorted(entries) == sorted(want)
    assert entries["mockprofile"] == "m_good - Mock Profile (mockprofile)"


def test_profile_dropdown_reports_a_bad_key(splunk, inputs):
    st, body = splunk.request("GET", f"{NS}/ta_nextdns_api_select_profile", params={"account": "m_bad", "output_mode": "json"})
    assert st >= 400
    assert "authRequired" in body and BAD_KEY not in body


def mock_get_profiles():
    req = urllib.request.Request(MOCK + "/profiles", headers={"X-Api-Key": "x"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


@pytest.mark.parametrize("kind", [STATS, LOGS])
def test_inputs_ran_on_the_expected_python(splunk, inputs, kind):
    want = expect_py313()
    if want is None:
        pytest.skip("SPLUNK_IMAGE_TAG not set")
    seen = python_versions(splunk, kind)
    assert seen, "no exec lines"
    if want:
        assert seen == {"python3.13"}, seen
    else:
        assert "python3.13" not in seen, seen
