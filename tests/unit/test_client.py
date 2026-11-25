"""Unit tests for package/bin/nextdns_client.py (no Splunk).

The client talks to the mock (docker/mock/mock_nextdns.py) served in-process
on a free port. Expected values are computed here from the mock's documented
timeline (one query every STEP_MS, sequence k = timestamp // STEP_MS, the
de-duplicated view drops k % 3 == 2), not read back through the client.
"""
import importlib.util
import json
import math
import os
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "package", "bin"))
import nextdns_client as nd  # noqa: E402


def _load_mock():
    spec = importlib.util.spec_from_file_location("mock_nextdns", os.path.join(ROOT, "docker", "mock", "mock_nextdns.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mock = _load_mock()
STEP = mock.STEP_MS
LOGS_PAGE = 70
ANALYTICS_PAGE = 40


@pytest.fixture(scope="module")
def server():
    mock.LOGS_PAGE = LOGS_PAGE
    mock.ANALYTICS_PAGE = ANALYTICS_PAGE
    mock.CURSOR_LOSS = True  # the real faults, see mock_nextdns.py
    mock.ANALYTICS_OVERLAP = True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mock.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def requests_seen():
    start = len(mock.REQUESTS)
    return lambda: mock.REQUESTS[start:]


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def client(base, key="mock-key", **kw):
    kw.setdefault("sleep", Sleeps())
    return nd.Client(key, base=base, **kw)


def window(minutes_ago_start=70, minutes_ago_end=10):
    now = nd_now()
    end = (now - minutes_ago_end * 60000) // 1000 * 1000
    return end - (minutes_ago_start - minutes_ago_end) * 60000, end


def expected_seqs(start, end, raw):
    first = math.ceil(start / STEP)
    last = (end - 1) // STEP
    return [k for k in range(first, last + 1) if raw or k % 3 != 2]


# ------------------------------------------------------------- logs ----


@pytest.mark.parametrize("raw", [False, True])
def test_logs_every_query_once_across_many_pages(server, requests_seen, raw):
    start, end = window()
    rows = client(server).logs("mockprofile", start, end, raw=raw)
    want = expected_seqs(start, end, raw)
    got = [r["mock_seq"] for r in rows]
    assert got == want  # complete, oldest first, no duplicates
    reqs = [r for r in requests_seen() if r["path"].endswith("/logs")]
    assert len(reqs) > 2 * len(want) // LOGS_PAGE  # the window was split, many times


def test_logs_never_use_the_lossy_cursor(server, requests_seen):
    start, end = window()
    client(server).logs("mockprofile", start, end)
    reqs = requests_seen()
    assert reqs and not any("cursor" in r["query"] for r in reqs)


def test_pager_stops_on_the_empty_asc_page(server, requests_seen):
    # sort=asc never answers a null cursor; the empty page ends the walk
    start, end = window(20, 10)
    c = client(server)
    pages = list(c.pages("/profiles/mockprofile/logs", {"from": start, "to": end, "sort": "asc", "raw": 1}, 1000))
    n = len(expected_seqs(start, end, True))
    assert len(requests_seen()) == math.ceil(n / LOGS_PAGE) + 1
    assert sum(len(p) for p in pages) < n  # and that is why logs() does not use it


class _DenseSession:
    """Every range answers a full page and a cursor until the cursor walk."""

    def __init__(self):
        self.params = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.params.append(dict(params))
        cursor = params.get("cursor")
        lo = int(params["from"])
        if int(params["to"]) - lo > 1 or cursor is None:
            body = {"data": [{"t": lo, "i": i} for i in range(1000)], "meta": {"pagination": {"cursor": "next"}}}
        else:
            body = {"data": [{"t": lo, "i": 1000 + i} for i in range(5)], "meta": {"pagination": {"cursor": None}}}

        class R:
            status_code = 200
            headers = {}

            def json(self_inner):
                return body

        return R()


def test_logs_fall_back_to_the_cursor_for_a_single_crowded_millisecond():
    s = _DenseSession()
    rows = nd.Client("k", base="http://unused", session=s).logs("p", 1000, 1004)
    # 4 one-millisecond leaves, each 1000 rows plus 5 from the cursor walk
    assert len(rows) == 4 * 1005
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)


def test_logs_send_limits_the_api_accepts(server, requests_seen):
    start, end = window(5, 3)
    client(server).logs("mockprofile", start, end)
    for r in requests_seen():
        assert 10 <= int(r["query"]["limit"]) <= 1000
        assert r["status"] == 200


def test_logs_window_is_half_open(server):
    start = (nd_now() - 600000) // STEP * STEP  # exactly on a query
    end = start + 10 * STEP  # also exactly on one
    rows = client(server).logs("mockprofile", start, end, raw=True)
    assert [r["mock_seq"] for r in rows] == list(range(start // STEP, start // STEP + 10))


def test_consecutive_windows_neither_overlap_nor_gap(server):
    c = client(server)
    start, end = window(30, 10)
    mid = start + 437 * STEP + 123
    a = c.logs("mockprofile", start, mid, raw=True)
    b = c.logs("mockprofile", mid, end, raw=True)
    assert [r["mock_seq"] for r in a + b] == expected_seqs(start, end, True)


def test_empty_window(server, requests_seen):
    start, end = window()
    assert client(server).logs("quietprofile", start, end) == []
    assert client(server).analytics("quietprofile", "domains", start, end) == []
    assert len(requests_seen()) == 2


# -------------------------------------------------------- analytics ----


def test_analytics_follow_the_cursor_to_null(server, requests_seen):
    start, end = window(70, 10)
    rows = client(server).analytics("mockprofile", "domains", start, end)
    ks = expected_seqs(start, end, True)
    want_domains = {"site%d.mock.example" % (k % mock.DOMAINS) for k in ks}
    got = [r["domain"] for r in rows]
    assert len(got) == len(set(got)) == len(want_domains)
    assert set(got) == want_domains
    assert sum(r["queries"] for r in rows) == len(ks)
    pages = requests_seen()
    assert len(pages) == math.ceil(len(want_domains) / ANALYTICS_PAGE)
    assert client_dups(server, start, end) == len(pages) - 1  # one repeat per later page, dropped
    assert all(1 <= int(r["query"]["limit"]) <= 500 for r in pages)


def client_dups(server, start, end):
    c = client(server)
    c.analytics("mockprofile", "domains", start, end)
    return c.duplicates_dropped


def test_analytics_status_counts_the_window(server):
    start, end = window(20, 10)
    rows = client(server).analytics("mockprofile", "status", start, end)
    ks = expected_seqs(start, end, True)
    blocked = sum(1 for k in ks if k % 5 == 0)
    assert {r["status"]: r["queries"] for r in rows} == {"blocked": blocked, "default": len(ks) - blocked}


# ----------------------------------------------------------- errors ----


def test_429_then_503_are_retried(server, requests_seen):
    start, end = window(20, 10)
    sleeps = Sleeps()
    rows = client(server, sleep=sleeps).analytics("flakyprofile", "ipVersions", start, end)
    assert sum(r["queries"] for r in rows) == len(expected_seqs(start, end, True))
    assert [r["status"] for r in requests_seen()] == [429, 503, 200]
    assert sleeps[0] == 1.0  # Retry-After honoured


def test_persistent_503_gives_up(server, requests_seen):
    start, end = window(20, 10)
    c = client(server, max_retries=3)
    with pytest.raises(nd.NextDNSError) as exc:
        c.logs("downprofile", start, end)
    assert exc.value.status == 503
    assert len(requests_seen()) == 4


def test_auth_failure_is_reported_without_secrets(server, requests_seen):
    start, end = window(20, 10)
    with pytest.raises(nd.NextDNSAuthError) as exc:
        client(server, key="mock-bad-key").logs("mockprofile", start, end)
    assert exc.value.status == 403 and exc.value.codes == ["authRequired"]
    text = str(exc.value)
    for secret in ("mock-bad-key", "mockprofile", "127.0.0.1", "/logs"):
        assert secret not in text
    assert len(requests_seen()) == 1  # 403 is not retried


def test_profiles(server):
    ids = [p["id"] for p in client(server).profiles()]
    assert ids == [p["id"] for p in mock.PROFILES]


def test_bad_limit_would_be_rejected(server):
    with pytest.raises(nd.NextDNSError) as exc:
        client(server).get("/profiles/mockprofile/logs", {"limit": 5})
    assert exc.value.status == 400 and exc.value.codes == ["minimum"]


class _LoopSession:
    """Answers every request with one row and the same cursor."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1

        class R:
            status_code = 200
            headers = {}

            def json(self_inner):
                return {"data": [{"n": self.calls}], "meta": {"pagination": {"cursor": "same"}}}

        return R()


def test_a_repeated_cursor_with_rows_is_an_error_not_a_hang():
    s = _LoopSession()
    with pytest.raises(nd.NextDNSError) as exc:
        nd.Client("k", base="http://unused", session=s).analytics("p", "domains", 0, 1000)
    assert exc.value.codes == ["pagination_loop"]
    assert s.calls == 2


# ---------------------------------------------------- windows & misc ----


def nd_now():
    import time

    return int(time.time() * 1000)


def test_next_window_first_run_and_checkpoint():
    now = 1_700_000_000_500
    assert nd.next_window(None, now, 120000, 3600000, 6 * 3600000) == (1_699_996_280_000, 1_699_999_880_000)
    assert nd.next_window(1_699_999_000_000, now, 120000, 3600000, 6 * 3600000) == (1_699_999_000_000, 1_699_999_880_000)
    assert nd.next_window(1_699_999_880_000, now, 120000, 3600000, 6 * 3600000) is None
    # an outage: capped, the rest is collected by later runs
    assert nd.next_window(1_600_000_000_000, now, 120000, 3600000, 6 * 3600000) == (1_600_000_000_000, 1_600_021_600_000)


def test_file_checkpoint(tmp_path):
    cp = nd.FileCheckpoint(str(tmp_path), "NextDNS_API_Stream", "a/b c")
    assert cp.load() is None
    cp.save(1234)
    assert json.loads(open(cp.path).read())["end_ms"] == 1234
    assert nd.FileCheckpoint(str(tmp_path), "NextDNS_API_Stream", "a/b c").load() == 1234
    assert os.path.dirname(cp.path) == str(tmp_path)
    with open(cp.path, "w") as fh:
        fh.write("{not json")
    assert cp.load() is None


def test_selected_types():
    assert nd.selected_types("") == (list(nd.ANALYTIC_TYPES), [])
    assert nd.selected_types(None)[0] == list(nd.ANALYTIC_TYPES)
    assert nd.selected_types("domains,status, bogus") == (["status", "domains"], ["bogus"])


def test_event_time():
    assert nd.event_time("2021-03-18T02:56:14.182Z", 0) == "1616036174.182"
    assert nd.event_time("2021-03-18T02:56:14Z", 0) == "1616036174.000"
    assert nd.event_time(None, 5000) == "5.000"


def test_api_base_resolution(tmp_path):
    assert nd.resolve_api_base({}, str(tmp_path)) == nd.DEFAULT_API_BASE
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "nextdns_api_base").write_text("http://mock:8080/\n")
    assert nd.resolve_api_base({}, str(tmp_path)) == "http://mock:8080"
    assert nd.resolve_api_base({"NEXTDNS_API_BASE": "https://gw/"}, str(tmp_path)) == "https://gw"
