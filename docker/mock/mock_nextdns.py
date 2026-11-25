#!/usr/bin/env python3
"""Mock NextDNS API for the integration tests. Synthetic data only.

Covers what the real API cannot produce on demand: many pages, 429 and 5xx,
auth failure and empty windows. Everything else is tested live.

Pagination follows the real API (checked live 25 Nov 2026):
  * `meta.pagination.cursor` is a short opaque string passed back as `cursor`.
  * analytics and `sort=desc` logs: the cursor is null on the last page.
  * `sort=asc` logs: the cursor is never null. Past the last row the API answers
    an empty page with the same cursor again, indefinitely.
  * limits: logs 10-1000 (400 `minimum`/`maximum` outside), analytics 1-500.
The harness clamps pages further (MOCK_LOGS_PAGE, MOCK_ANALYTICS_PAGE) so an
hour of data spans many pages; the real API returns exactly `limit` rows.

Data is a pure function of time, so a test can compute what any window holds:
the "mockprofile" profile logs one query every STEP_MS, sequence number
k = timestamp_ms // STEP_MS. raw=1 returns every k; the default (de-duplicated)
view drops k % 3 == 2. Analytics count the raw queries in [from, to).

Profiles:
  mockprofile   normal
  quietprofile  no queries at all (empty windows)
  flakyprofile  as mockprofile, but the first request for each path answers
                429 (Retry-After: 1) and the second 503
  downprofile   every request answers 503
Any key other than "mock-bad-key" is accepted; that one gets 403 authRequired.

Test helpers: GET /__mock/requests (every request: path, query, status),
GET /__mock/info (STEP_MS, page clamps).
"""
import calendar
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STEP_MS = int(os.environ.get("MOCK_STEP_MS", "1000"))
LOGS_PAGE = int(os.environ.get("MOCK_LOGS_PAGE", "1000"))
ANALYTICS_PAGE = int(os.environ.get("MOCK_ANALYTICS_PAGE", "500"))
DOMAINS = 1200
IPS = 250
BAD_KEY = "mock-bad-key"
DATA_PROFILES = ("mockprofile", "flakyprofile")
PROFILES = [
    {"id": "mockprofile", "name": "Mock Profile", "role": "owner", "fingerprint": "fp0000000001"},
    {"id": "quietprofile", "name": "Quiet Profile", "role": "owner", "fingerprint": "fp0000000002"},
    {"id": "flakyprofile", "name": "Flaky Profile", "role": "owner", "fingerprint": "fp0000000003"},
    {"id": "downprofile", "name": "Down Profile", "role": "owner", "fingerprint": "fp0000000004"},
]
ANALYTICS_TYPES = (
    "status", "protocols", "devices", "ips", "reasons", "domains",
    "queryTypes", "ipVersions", "dnssec", "encryption",
)
PATH_RE = re.compile(r"^/profiles/([^/]+)/(logs|analytics/([A-Za-z]+))$")

REQUESTS = []
FLAKY_SEEN = {}
LOCK = threading.Lock()


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + ".%03dZ" % (ms % 1000)


def parse_date(value, now_ms):
    """ms epoch from the forms the collectors and tests send (ms, s, ISO, now)."""
    if value is None or value == "":
        return None
    if value == "now":
        return now_ms
    if re.fullmatch(r"-?\d+", value):
        n = int(value)
        return n if abs(n) > 10 ** 11 else n * 1000
    main, _, frac = value.rstrip("Z").partition(".")
    secs = calendar.timegm(time.strptime(main, "%Y-%m-%dT%H:%M:%S"))
    return secs * 1000 + (int((frac + "000")[:3]) if frac else 0)


def log_row(k):
    ts = k * STEP_MS
    blocked = k % 5 == 0
    dev = k % 4
    return {
        "timestamp": iso(ts),
        "domain": "site%d.mock.example" % (k % DOMAINS),
        "root": "mock.example",
        "tracker": None,
        "encrypted": k % 2 == 0,
        "protocol": "DNS-over-HTTPS" if k % 2 == 0 else "UDP",
        "clientIp": "192.0.2.%d" % (k % IPS),
        "client": "mock-client",
        "device": {"id": "DEV%d" % dev, "name": "mock-device-%d" % dev, "model": "mock", "localIp": "198.51.100.%d" % dev},
        "status": "blocked" if blocked else "default",
        "reasons": [{"id": "blocklist:mock", "name": "Mock Blocklist"}] if blocked else [],
        "mock_seq": k,
    }


def seqs(start_ms, end_ms, now_ms):
    """Sequence numbers of the raw queries logged in [start_ms, end_ms) by now."""
    end_ms = min(end_ms, now_ms + 1)
    first = -(-start_ms // STEP_MS)
    last = (end_ms - 1) // STEP_MS
    return range(first, last + 1) if last >= first else range(0)


def analytics_rows(analytic_type, ks):
    counts = {}

    def add(key, row):
        if key not in counts:
            counts[key] = dict(row, queries=0)
        counts[key]["queries"] += 1

    for k in ks:
        r = log_row(k)
        if analytic_type == "status":
            add(r["status"], {"status": r["status"]})
        elif analytic_type == "protocols":
            add(r["protocol"], {"protocol": r["protocol"]})
        elif analytic_type == "devices":
            d = r["device"]
            add(d["id"], {"id": d["id"], "name": d["name"], "model": d["model"]})
        elif analytic_type == "ips":
            add(r["clientIp"], {"ip": r["clientIp"], "network": {"cellular": False, "vpn": False, "isp": "Mock ISP", "asn": 64500},
                                "geo": {"latitude": 0.0, "longitude": 0.0, "countryCode": "ZZ", "country": "Mockland", "city": "Mock"}})
        elif analytic_type == "reasons":
            for reason in r["reasons"]:
                add(reason["id"], reason)
        elif analytic_type == "domains":
            add(r["domain"], {"domain": r["domain"], "root": r["root"]})
        elif analytic_type == "queryTypes":
            t = (1, "A") if k % 3 else (28, "AAAA")
            add(t, {"type": t[0], "name": t[1]})
        elif analytic_type == "ipVersions":
            v = 4 if k % 3 else 6
            add(v, {"version": v})
        elif analytic_type == "dnssec":
            add(k % 7 == 0, {"validated": k % 7 == 0})
        elif analytic_type == "encryption":
            add(r["encrypted"], {"encrypted": r["encrypted"]})
    return sorted(counts.values(), key=lambda row: (-row["queries"], json.dumps(row, sort_keys=True)))


def cursor_for(offset):
    return "c%x" % offset


def offset_of(cursor):
    if not cursor:
        return 0
    if not re.fullmatch(r"c[0-9a-f]+", cursor):
        raise ValueError("cursor")
    return int(cursor[1:], 16)


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, path, query, status):
        with LOCK:
            REQUESTS.append({"path": path, "query": query, "status": status, "t": int(time.time() * 1000)})

    def _error(self, path, query, status, code, parameter=None, headers=None):
        self._record(path, query, status)
        err = {"code": code}
        if parameter:
            err["source"] = {"parameter": parameter}
        self._send({"errors": [err]}, status, headers)

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        if path == "/__mock/requests":
            with LOCK:
                return self._send({"data": list(REQUESTS)})
        if path == "/__mock/info":
            return self._send({"step_ms": STEP_MS, "logs_page": LOGS_PAGE, "analytics_page": ANALYTICS_PAGE, "domains": DOMAINS})
        if path == "/__mock/health":
            return self._send({"ok": True})

        if self.headers.get("X-Api-Key", "") == BAD_KEY or not self.headers.get("X-Api-Key"):
            return self._error(path, query, 403, "authRequired")
        if path.rstrip("/") == "/profiles":
            self._record(path, query, 200)
            return self._send({"data": PROFILES})

        m = PATH_RE.match(path)
        if not m or m.group(1) not in {p["id"] for p in PROFILES}:
            return self._error(path, query, 404, "notFound")
        profile, kind, analytic_type = m.group(1), m.group(2), m.group(3)

        if profile == "downprofile":
            return self._error(path, query, 503, "unavailable")
        if profile == "flakyprofile" and "cursor" not in query:
            with LOCK:
                seen = FLAKY_SEEN.get(path, 0)
                FLAKY_SEEN[path] = seen + 1
            if seen == 0:
                return self._error(path, query, 429, "tooManyRequests", headers={"Retry-After": "1"})
            if seen == 1:
                return self._error(path, query, 503, "unavailable")

        now = int(time.time() * 1000)
        try:
            limit = int(query.get("limit", "100" if kind == "logs" else "10"))
        except ValueError:
            return self._error(path, query, 400, "invalid", "limit")
        lo, hi = (10, 1000) if kind == "logs" else (1, 500)
        if limit < lo:
            return self._error(path, query, 400, "minimum", "limit")
        if limit > hi:
            return self._error(path, query, 400, "maximum", "limit")
        try:
            start = parse_date(query.get("from"), now)
            end = parse_date(query.get("to"), now)
            offset = offset_of(query.get("cursor"))
        except ValueError:
            return self._error(path, query, 400, "invalid", "from/to/cursor")
        start = start if start is not None else now - 30 * 86400 * 1000
        end = end if end is not None else now
        ks = seqs(start, end, now) if profile in DATA_PROFILES else range(0)

        if kind == "logs":
            raw = query.get("raw") in ("1", "true")
            rows = [log_row(k) for k in ks if raw or k % 3 != 2]
            sort = query.get("sort", "desc")
            if sort not in ("asc", "desc"):
                return self._error(path, query, 400, "invalid", "sort")
            if sort == "desc":
                rows.reverse()
            size = min(limit, LOGS_PAGE)
        else:
            if analytic_type not in ANALYTICS_TYPES:
                return self._error(path, query, 404, "notFound")
            rows = analytics_rows(analytic_type, ks)
            size = min(limit, ANALYTICS_PAGE)
            sort = "desc"

        page = rows[offset:offset + size]
        if kind == "logs" and sort == "asc":
            # Never null: past the end, the same cursor comes back on an empty page.
            cursor = cursor_for(offset + len(page)) if page else (query.get("cursor") or cursor_for(offset))
        else:
            cursor = cursor_for(offset + size) if offset + size < len(rows) else None
        self._record(path, query, 200)
        self._send({"data": page, "meta": {"pagination": {"cursor": cursor}}})

    def log_message(self, fmt, *args):
        pass  # /__mock/requests holds the request log


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
