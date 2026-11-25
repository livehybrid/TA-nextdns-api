"""Live: every input against the real NextDNS API, on real Splunk in Docker.

William's DNS data is private and this repo is public. These tests assert and
report counts, booleans and field names only. They never print a domain,
device, IP, profile id or name, and CI runs them with --tb=no -rN so no
assertion text or local variable reaches the log. Expected values come from
the test's own calls to the API (urllib, other page sizes and sort order),
never from the add-on's code.

Needs NEXTDNS_API_KEY. Writes a counts-only summary to LIVE_SUMMARY.
"""
from __future__ import annotations

import calendar
import collections
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ndtest import (  # noqa: E402
    NS,
    container_grep_count,
    create_account,
    create_input,
    delete_input,
    expect_py313,
    failures,
    python_versions,
    runs,
    wait_for_runs,
)

KEY = os.environ.get("NEXTDNS_API_KEY", "").strip()
API = "https://api.nextdns.io"
CONTAINER = os.environ.get("SPLUNK_CONTAINER", "ta_nextdns_api_splunk")
SUMMARY_PATH = os.environ.get("LIVE_SUMMARY")
UA = "Mozilla/5.0 (TA-nextdns-api live test)"  # Cloudflare refuses urllib's UA
ACCOUNT = "live_acct"
STATS = "NextDNS_API_Stats"
LOGS = "NextDNS_API_Stream"
LOOKBACK_S = 3600
# name -> (kind, extra fields)
INPUTS = {
    "l_stats": (STATS, {}),
    "l_logs": (LOGS, {}),
    "l_logs_raw": (LOGS, {"raw": "1"}),
}
STATS_FIELDS = {
    "status": {"status", "queries"},
    "protocols": {"protocol", "queries"},
    "devices": {"id", "queries"},
    "ips": {"ip", "queries"},
    "reasons": {"id", "name", "queries"},
    "domains": {"domain", "queries"},
    "queryTypes": {"type", "name", "queries"},
    "ipVersions": {"version", "queries"},
    "dnssec": {"validated", "queries"},
    "encryption": {"encrypted", "queries"},
}
# Types any profile with traffic has in an hour; the others can be empty.
ALWAYS = ("status", "protocols", "domains", "queryTypes", "ipVersions", "dnssec", "encryption")
LOG_FIELDS = ("timestamp", "domain", "root", "protocol", "clientIp", "encrypted")

pytestmark = pytest.mark.skipif(not KEY, reason="NEXTDNS_API_KEY not set")

SUMMARY: dict = {}


def api(path, **params):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"X-Api-Key": KEY, "User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise AssertionError(f"API status {e.code}") from None


def api_all(path, limit, **params):
    """Every row, following the cursor; desc/analytics end on a null cursor."""
    rows, cursor = [], None
    for _ in range(2000):
        q = dict(params, limit=limit)
        if cursor:
            q["cursor"] = cursor
        body = api(path, **q)
        rows += body.get("data") or []
        cursor = ((body.get("meta") or {}).get("pagination") or {}).get("cursor")
        if not cursor:
            return rows
    raise AssertionError("API pagination did not end")


def ms(iso):
    main, _, frac = iso.rstrip("Z").partition(".")
    return calendar.timegm(time.strptime(main, "%Y-%m-%dT%H:%M:%S")) * 1000 + int((frac + "000")[:3])


def fingerprint(row):
    row = {k: v for k, v in row.items() if k != "status"}  # moved to an indexed field at ingest
    return hashlib.sha1(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.fixture(scope="module")
def profile():
    """The profile with the most raw queries in the last ten minutes."""
    profiles = api("/profiles").get("data") or []
    assert profiles, "the account has no profiles"
    now = int(time.time() * 1000)
    busiest, best = None, -1
    for p in profiles:
        n = len(api(f"/profiles/{p['id']}/logs", raw=1, limit=1000, **{"from": now - 600000, "to": now}).get("data") or [])
        if n > best:
            busiest, best = p["id"], n
    SUMMARY["profiles"] = len(profiles)
    SUMMARY["chosen_profile_raw_queries_10min"] = best
    assert best > 0, "no profile has recent traffic"
    return busiest


@pytest.fixture(scope="module")
def live(splunk, profile):
    create_account(splunk, ACCOUNT, KEY)
    for name, (kind, extra) in INPUTS.items():
        create_input(splunk, kind, name, interval=60, account=ACCOUNT, profile=profile, **extra)
    for name in INPUTS:
        wait_for_runs(splunk, name, 3, timeout=420)
    yield profile
    for name, (kind, _) in INPUTS.items():
        delete_input(splunk, kind, name)
    if SUMMARY_PATH:
        with open(SUMMARY_PATH, "w") as fh:
            json.dump(SUMMARY, fh, indent=1, sort_keys=True)


def source(name):
    return f'source="{INPUTS[name][0]}://{name}"'


# ------------------------------------------------------------- runs ----


@pytest.mark.parametrize("name", list(INPUTS))
def test_runs_complete_and_checkpoints_advance(splunk, live, name):
    done = runs(splunk, name)
    SUMMARY.setdefault("runs", {})[name] = {"runs": len(done), "events": [e for e, _ in done], "window_seconds": [w for _, w in done]}
    assert len(done) >= 3
    assert done[0][1] == LOOKBACK_S
    assert all(w < 200 for _, w in done[1:])
    assert not failures(splunk, name)


# ------------------------------------------------------------- logs ----


def _indexed_logs(splunk, name):
    rows = splunk.search(f"index=main {source(name)} | fields _raw, _time", earliest="-3h", count=0)
    out = []
    for r in rows:
        body = json.loads(r["_raw"])
        out.append((body, float(r["_time"])))
    return out


@pytest.mark.parametrize("name,raw", [("l_logs_raw", True), ("l_logs", False)])
def test_logs_match_the_api(splunk, live, name, raw):
    indexed = _indexed_logs(splunk, name)
    assert indexed, "no events"
    got = collections.Counter(fingerprint(b) for b, _ in indexed)
    stamps = [ms(b["timestamp"]) for b, _ in indexed]
    lo, hi = min(stamps), max(stamps) + 1
    params = {"from": lo, "to": hi, "sort": "desc"}
    if raw:
        params["raw"] = 1
    want = collections.Counter(fingerprint(r) for r in api_all(f"/profiles/{live}/logs", 500, **params))
    dups = sum(c - 1 for c in got.values() if c > 1)
    missing = sum((want - got).values())
    extra = sum((got - want).values())
    n = sum(want.values())
    SUMMARY.setdefault("logs", {})[name] = {
        "indexed": sum(got.values()), "api": n, "duplicates": dups, "missing": missing, "extra": extra,
        "span_seconds": (hi - lo) // 1000,
    }
    assert dups == 0
    assert (hi - lo) >= (LOOKBACK_S - 120) * 1000
    # raw is the complete log; the default view is de-duplicated per request
    # by NextDNS, so a different split into pages and windows differs a little.
    tolerance = max(3, n // 500) if raw else max(10, n // 20)
    assert missing <= tolerance and extra <= tolerance


def test_logs_fields_time_and_indexed_fields(splunk, live):
    rows = splunk.search(
        f"index=main {source('l_logs_raw')} "
        "| eval off=abs(_time - strptime(timestamp, \"%Y-%m-%dT%H:%M:%S.%3NZ\")) "
        "| stats count, max(off) as off, dc(sourcetype) as st, "
        + ", ".join(f'count({f}) as f_{i}' for i, f in enumerate(LOG_FIELDS))
        + ", count(blocked) as blocked, count(action) as action, count(device_name) as device_name, count(device.id) as device_id",
        earliest="-3h",
    )
    r = rows[0]
    n = int(r["count"])
    present = {f: int(r[f"f_{i}"]) for i, f in enumerate(LOG_FIELDS)}
    SUMMARY["log_fields"] = {"events": n, **present, "blocked": int(r["blocked"]), "action": int(r["action"]),
                             "device_name": int(r["device_name"]), "device.id": int(r["device_id"])}
    assert n > 0 and int(r["st"]) == 1
    assert float(r["off"]) < 0.001, "event time is not the query timestamp"
    assert all(v == n for v in present.values())
    assert int(r["blocked"]) == n and int(r["action"]) == n
    assert int(r["device_name"]) == int(r["device_id"])  # named devices only
    vals = splunk.search(f"index=main {source('l_logs_raw')} | stats count by blocked", earliest="-3h")
    assert {v["blocked"] for v in vals} <= {"blocked", "permitted"}


# ------------------------------------------------------------ stats ----


def test_stats_windows_match_the_api(splunk, live):
    rows = splunk.search(
        f"index=main {source('l_stats')} | stats count, sum(queries) as q by sourcetype window_start window_end",
        earliest="-3h", count=0,
    )
    windows = sorted({(r["window_start"], r["window_end"]) for r in rows})
    assert len(windows) >= 3
    for (_, end), (start, _) in zip(windows, windows[1:]):
        assert end == start, "windows overlap or leave a gap"
    assert ms(windows[0][1]) - ms(windows[0][0]) == LOOKBACK_S * 1000
    by = {(r["sourcetype"].split(":", 1)[1], r["window_start"]): r for r in rows}
    first_types = {t for t, s in by if s == windows[0][0]}
    assert set(ALWAYS) <= first_types
    checked = 0
    mismatches = []
    for start, end in (windows[0], windows[-1]):
        for t in STATS_FIELDS:
            want = api_all(f"/profiles/{live}/analytics/{t}", 100, **{"from": ms(start), "to": ms(end)})
            r = by.get((t, start))
            got_n = int(r["count"]) if r else 0
            got_q = int(float(r["q"])) if r else 0
            if (got_n, got_q) != (len(want), sum(x.get("queries", 0) for x in want)):
                mismatches.append(t)
            checked += 1
    SUMMARY["stats"] = {
        "windows": len(windows), "events": sum(int(r["count"]) for r in rows), "types_first_window": len(first_types),
        "checked": checked, "mismatched_types": sorted(set(mismatches)),
    }
    assert not mismatches


def test_stats_fields(splunk, live):
    missing = {}
    for t, fields in STATS_FIELDS.items():
        rows = splunk.search(
            f"index=main {source('l_stats')} sourcetype=\"{STATS}:{t}\" | fieldsummary | fields field",
            earliest="-3h", count=0,
        )
        names = {r["field"] for r in rows}
        if not names and t not in ALWAYS:
            continue
        lack = (fields | {"window_start", "window_end"}) - names
        if lack:
            missing[t] = sorted(lack)
    SUMMARY["stats_missing_fields"] = missing
    assert not missing


# --------------------------------------------------------- REST etc ----


def test_profile_dropdown(splunk, live):
    data = splunk.get_json(f"{NS}/ta_nextdns_api_select_profile", account=ACCOUNT)
    entries = data.get("entry", [])
    ok = sum(1 for e in entries if re.fullmatch(rf"{ACCOUNT} - .* \({re.escape(e['name'])}\)", e["content"].get("profile", "")))
    SUMMARY["dropdown"] = {"entries": len(entries), "well_formed": ok}
    assert len(entries) == SUMMARY["profiles"] == ok


def test_key_never_logged(splunk, live):
    assert container_grep_count(CONTAINER, "l_logs_raw") > 0  # positive control
    hits = container_grep_count(CONTAINER, KEY)
    SUMMARY["key_in_splunk_logs"] = hits
    assert hits == 0


@pytest.mark.parametrize("kind", [STATS, LOGS])
def test_inputs_ran_on_the_expected_python(splunk, live, kind):
    want = expect_py313()
    if want is None:
        pytest.skip("SPLUNK_IMAGE_TAG not set")
    seen = python_versions(splunk, kind)
    SUMMARY.setdefault("python", {})[kind] = sorted(seen)
    assert seen and (seen == {"python3.13"} if want else "python3.13" not in seen)
