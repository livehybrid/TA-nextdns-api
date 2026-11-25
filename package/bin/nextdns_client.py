"""NextDNS API client shared by the inputs and the profile select handler.

Kept free of Splunk imports so the unit tests can load it on its own. Python
3.9 compatible (Splunk 10.1 and earlier run the add-on on 3.9).

Privacy: nothing here logs or returns a URL, an API key or response data in an
error. Errors carry the HTTP status and the API's error codes only.
"""
import calendar
import json
import os
import re
import time
import urllib.parse

import requests

DEFAULT_API_BASE = "https://api.nextdns.io"
USER_AGENT = "TA-nextdns-api"
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRY_AFTER = 60.0

# Page sizes the API allows: analytics 1-500 (default 10), logs 10-1000
# (default 100). Larger pages mean fewer requests.
ANALYTICS_PAGE = 500
LOGS_PAGE = 1000
MAX_PAGES = 10000


class NextDNSError(Exception):
    """API call failed. Carries the status and API error codes, nothing else."""

    def __init__(self, status=None, codes=()):
        self.status = status
        self.codes = [str(c) for c in codes]
        super().__init__(
            "NextDNS API error status={} codes={}".format(status if status is not None else "-", ",".join(self.codes) or "-")
        )


class NextDNSAuthError(NextDNSError):
    """401/403: the key is wrong or has no access to the profile."""


def resolve_api_base(environ=None, app_dir=None):
    """Base URL for the NextDNS API.

    1. NEXTDNS_API_BASE env var (an enterprise gateway, for example).
    2. `local/nextdns_api_base` in the app. splunkd does not reliably pass
       environment variables to modular-input processes, so the test harness
       writes this file to point the add-on at a mock upstream.
    3. The real NextDNS API.
    """
    environ = os.environ if environ is None else environ
    env = (environ.get("NEXTDNS_API_BASE") or "").strip()
    if env:
        return env.rstrip("/")
    if app_dir is None:
        app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
    try:
        with open(os.path.join(app_dir, "local", "nextdns_api_base")) as fh:
            val = fh.read().strip()
        if val:
            return val.rstrip("/")
    except OSError:
        pass
    return DEFAULT_API_BASE


def _error_codes(resp):
    try:
        body = resp.json()
    except ValueError:
        return []
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    return [e.get("code", "?") for e in errors if isinstance(e, dict)]


def _retry_after(resp, attempt):
    value = resp.headers.get("Retry-After") if resp is not None else None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = 2.0 ** attempt
    return max(0.0, min(delay, MAX_RETRY_AFTER))


class Client:
    def __init__(self, api_key, base=None, session=None, timeout=30, max_retries=3, sleep=time.sleep):
        if not api_key:
            raise NextDNSAuthError(None, ["missing_api_key"])
        self.base = (base or resolve_api_base()).rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.requests_made = 0
        self.duplicates_dropped = 0
        self._headers = {"X-Api-Key": api_key, "User-Agent": USER_AGENT, "Accept": "application/json"}

    def get(self, path, params=None):
        """GET a JSON document. Retries 429 and 5xx (Retry-After honoured)."""
        url = self.base + path
        for attempt in range(self.max_retries + 1):
            self.requests_made += 1
            try:
                resp = self.session.get(url, params=params, headers=self._headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self.sleep(_retry_after(None, attempt))
                    continue
                raise NextDNSError(None, [type(exc).__name__])
            status = resp.status_code
            if status in RETRY_STATUSES and attempt < self.max_retries:
                self.sleep(_retry_after(resp, attempt))
                continue
            if status in (401, 403):
                raise NextDNSAuthError(status, _error_codes(resp))
            if status >= 400:
                raise NextDNSError(status, _error_codes(resp))
            try:
                body = resp.json()
            except ValueError:
                raise NextDNSError(status, ["invalid_json"])
            if not isinstance(body, dict):
                raise NextDNSError(status, ["unexpected_body"])
            if body.get("errors") and body.get("data") is None:
                raise NextDNSError(status, [e.get("code", "?") for e in body["errors"] if isinstance(e, dict)])
            return body
        raise NextDNSError(None, ["retries_exhausted"])  # pragma: no cover

    def pages(self, path, params, limit):
        """Yield each page's `data` list until the last page.

        The documented end is `meta.pagination.cursor: null`, which is what
        analytics and `sort=desc` logs return. `sort=asc` logs never return
        null (checked live 25 Nov 2026): after the last row they answer an
        empty page carrying the same cursor again, indefinitely, and rows
        logged since keep appearing on it. So an empty page also ends the walk.
        """
        cursor = None
        seen = set()
        while True:
            q = dict(params or {})
            q["limit"] = limit
            if cursor:
                q["cursor"] = cursor
            body = self.get(path, q)
            data = body.get("data")
            data = data if isinstance(data, list) else []
            if data:
                yield data
            cursor = ((body.get("meta") or {}).get("pagination") or {}).get("cursor")
            if not cursor or not data:
                return
            if cursor in seen or len(seen) >= MAX_PAGES:
                raise NextDNSError(None, ["pagination_loop"])
            seen.add(cursor)

    def profiles(self):
        rows = []
        for page in self.pages("/profiles", {}, 100):
            rows.extend(page)
        return rows

    def analytics(self, profile, analytic_type, start_ms, end_ms):
        """Every row of one analytics type for [start_ms, end_ms).

        Rows tied on `queries` are not in a stable order upstream, so a row
        can appear on two pages and another on neither (checked live: 85
        repeats across 58 pages of 20). Pages of 500, the maximum, keep page
        boundaries rare; a repeated row is dropped so it is not counted twice.
        """
        path = "/profiles/{}/analytics/{}".format(_quote(profile), _quote(analytic_type))
        key = ANALYTIC_KEYS.get(analytic_type)
        rows, seen = [], set()
        for page in self.pages(path, {"from": start_ms, "to": end_ms}, ANALYTICS_PAGE):
            for row in page:
                ident = json.dumps(row.get(key) if key else row, sort_keys=True, default=str)
                if ident in seen:
                    self.duplicates_dropped += 1
                    continue
                seen.add(ident)
                rows.append(row)
        return rows

    def logs(self, profile, start_ms, end_ms, raw=False):
        """Every log in [start_ms, end_ms), oldest first.

        The log's cursor loses rows that share a timestamp across a page
        boundary (checked live: 24 of 15,000 rows lost in pages of 100, 1106
        timestamps shared by two or more rows). So no cursor is used: a range
        that does not fit one page (`sort=desc` answers a null cursor when it
        does) is halved until each part fits, and the parts are read in order.

        raw=False is the API default: A, AAAA and HTTPS queries only, with
        repeats and background noise removed. That de-duplication is done per
        request, so the rows for a range depend a little on how it is split.
        raw=True returns every query.
        """
        path = "/profiles/{}/logs".format(_quote(profile))
        extra = {"raw": 1} if raw else {}
        rows = []
        todo = [(start_ms, end_ms)]
        while todo:
            lo, hi = todo.pop()
            body = self.get(path, dict(extra, **{"from": lo, "to": hi, "sort": "desc", "limit": LOGS_PAGE}))
            data = body.get("data") if isinstance(body.get("data"), list) else []
            more = ((body.get("meta") or {}).get("pagination") or {}).get("cursor")
            if more and data and hi - lo > 1:
                mid = (lo + hi) // 2
                todo.append((mid, hi))  # popped after (lo, mid): oldest first
                todo.append((lo, mid))
                continue
            if more and data:
                # one millisecond holds more than a page: only the cursor is left
                data = [r for page in self.pages(path, dict(extra, **{"from": lo, "to": hi, "sort": "desc"}), LOGS_PAGE) for r in page]
            rows.extend(reversed(data))
        return rows


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


# ------------------------------------------------------------ input bits ---

ANALYTIC_TYPES = (
    "status",
    "protocols",
    "devices",
    "ips",
    "reasons",
    "domains",
    "queryTypes",
    "ipVersions",
    "dnssec",
    "encryption",
)


# The field that identifies a row of each analytics type.
ANALYTIC_KEYS = {
    "status": "status",
    "protocols": "protocol",
    "devices": "id",
    "ips": "ip",
    "reasons": "id",
    "domains": "domain",
    "queryTypes": "type",
    "ipVersions": "version",
    "dnssec": "validated",
    "encryption": "encrypted",
}


def selected_types(sections):
    """(types, unknown) for an input's `sections` (comma separated); all if empty."""
    wanted = [s.strip() for s in (sections or "").split(",") if s.strip()]
    if not wanted:
        return list(ANALYTIC_TYPES), []
    return [t for t in ANALYTIC_TYPES if t in wanted], [s for s in wanted if s not in ANALYTIC_TYPES]


def is_true(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def event_time(timestamp, fallback_ms):
    """Epoch seconds (string, ms precision) from the API's ISO 8601 UTC timestamp."""
    try:
        main, _, frac = str(timestamp).rstrip("Z").partition(".")
        secs = calendar.timegm(time.strptime(main, "%Y-%m-%dT%H:%M:%S"))
        ms = int((frac + "000")[:3]) if frac else 0
        return "%d.%03d" % (secs, ms)
    except (ValueError, TypeError):
        return "%.3f" % (fallback_ms / 1000.0)


# ---------------------------------------------------------------- windows ---


def next_window(checkpoint_ms, now_ms, lag_ms, lookback_ms, max_window_ms):
    """The half-open window [start, end) this run should collect, or None.

    `end` trails now by `lag_ms` so late writes upstream have landed, and is
    whole seconds. The first run looks back `lookback_ms`. After an outage the
    window is capped at `max_window_ms`, and later runs catch up.
    """
    end = ((int(now_ms) - int(lag_ms)) // 1000) * 1000
    start = int(checkpoint_ms) if checkpoint_ms is not None else end - int(lookback_ms)
    if start >= end:
        return None
    return start, min(end, start + int(max_window_ms))


def iso_ms(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + ".%03dZ" % (ms % 1000)


# ------------------------------------------------------------- checkpoint ---

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class FileCheckpoint:
    """One JSON file per input in the modular-input checkpoint directory."""

    def __init__(self, directory, kind, name):
        self.path = os.path.join(directory, "{}__{}.json".format(_SAFE.sub("_", kind), _SAFE.sub("_", name)))

    def load(self):
        try:
            with open(self.path) as fh:
                value = json.load(fh).get("end_ms")
        except (OSError, ValueError, AttributeError):
            return None
        return int(value) if isinstance(value, (int, float)) else None

    def save(self, end_ms):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"version": 1, "end_ms": int(end_ms)}, fh)
        os.replace(tmp, self.path)
