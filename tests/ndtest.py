"""Helpers shared by the mock and live end-to-end suites.

Nothing here prints event data: helpers return counts and field names, and
callers assert on those.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

APP = "TA-nextdns-api"
NS = f"/servicesNS/nobody/{APP}"
ACCOUNT_PATH = f"{NS}/ta_nextdns_api_account"
SPLUNK_VERSION = os.environ.get("SPLUNK_IMAGE_TAG", "")

_RUN_RE = re.compile(r"run complete events=(\d+).*window_seconds=(\d+)")


def create_account(splunk, name, key):
    st, body = splunk.request("POST", ACCOUNT_PATH, data={"name": name, "api_key": key})
    # never echo the body: it can carry the key back
    assert st in (200, 201, 409), f"create account -> {st}"


def create_input(splunk, kind, name, interval=30, index="main", **fields):
    data = {"name": name, "index": index, "interval": str(interval), **fields}
    st, _ = splunk.request("POST", f"{NS}/data/inputs/{kind}", data=data)
    assert st in (200, 201, 409), f"create {kind} input -> {st}"
    splunk.request("POST", f"{NS}/data/inputs/{kind}/{name}/enable")


def delete_input(splunk, kind, name):
    splunk.request("POST", f"{NS}/data/inputs/{kind}/{name}/disable")
    splunk.request("DELETE", f"{NS}/data/inputs/{kind}/{name}")


def log_lines(splunk, input_name, terms, earliest="-2h"):
    """_raw of the add-on's own log lines for one input (never printed)."""
    rows = splunk.search(
        f'search index=_internal source="*ta-nextdns-api_{input_name}.log" {terms} | sort 0 _time | fields _raw',
        earliest=earliest, count=0,
    )
    return [r.get("_raw", "") for r in rows]


def runs(splunk, input_name):
    """[(events, window_seconds)] for each completed run, oldest first."""
    out = []
    for raw in log_lines(splunk, input_name, '"run complete"'):
        m = _RUN_RE.search(raw)
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def wait_for_runs(splunk, input_name, n, timeout=300):
    deadline = time.time() + timeout
    got = []
    while time.time() < deadline:
        got = runs(splunk, input_name)
        if len(got) >= n:
            return got
        time.sleep(10)
    return got


def failures(splunk, input_name):
    return log_lines(splunk, input_name, '"run failed"')


def wait_for_failures(splunk, input_name, n, timeout=240):
    deadline = time.time() + timeout
    got = []
    while time.time() < deadline:
        got = failures(splunk, input_name)
        if len(got) >= n:
            return got
        time.sleep(10)
    return got


def container_grep_count(container, needle, path="/opt/splunk/var/log/splunk"):
    """Lines containing `needle` under `path` in the container.

    The needle goes in on stdin, so it never appears on a command line.
    """
    p = subprocess.run(
        ["docker", "exec", "-i", "-u", "root", container, "bash", "-c", f"grep -rhoF -f /dev/stdin {path} | wc -l"],
        input=needle + "\n", capture_output=True, text=True, timeout=120,
    )
    assert p.returncode == 0, "grep in container failed"
    return int(p.stdout.strip() or 0)


def python_versions(splunk, kind):
    """Interpreters splunkd used to launch `kind` (from splunkd.log)."""
    rows = splunk.search(
        f'search index=_internal sourcetype=splunkd component=ExecProcessor "New scheduled exec process" "{kind}" '
        '| rex "exec process: (?<py>\\S*python[0-9.]*)" | stats count by py',
        earliest="-2h",
    )
    return {r.get("py", "").rsplit("/", 1)[-1] for r in rows if r.get("py")}


def expect_py313():
    """Splunk 10.2+ honours python.required = 3.13; older images run 3.9."""
    m = re.match(r"(\d+)\.(\d+)", SPLUNK_VERSION)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2))) >= (10, 2)
