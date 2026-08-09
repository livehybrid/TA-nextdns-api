#!/usr/bin/env python3
"""
Mock NextDNS API for the integration test.

Serves deterministic, synthetic responses for the endpoints the collectors call,
so the two keyed inputs (NextDNS_API_Stats / NextDNS_API_Stream) can be exercised
end-to-end against a real Splunk with no real API key and no real DNS data. The
collectors are pointed here via the NEXTDNS_API_BASE env var (see the helper
modules). The x-api-key header is accepted but ignored.

Endpoints:
  GET /profiles                                   -> profile list (for the UI select handler)
  GET /profiles/<profile>/analytics/<type>        -> {"data": [ ...rows... ]}
  GET /profiles/<profile>/logs/stream             -> a bounded SSE-style stream

Pure stdlib so the container needs no pip install.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re

ANALYTICS_RE = re.compile(r"^/profiles/([^/]+)/analytics/([^/?]+)")
STREAM_RE = re.compile(r"^/profiles/([^/]+)/logs/stream")

# How many synthetic stream events to emit before closing the connection. The
# real endpoint is an infinite SSE stream; a bounded mock lets the collector's
# iter_lines() loop terminate so the modular-input run completes in the test.
STREAM_EVENTS = 3


def _analytics_rows(profile: str, analytic_type: str):
    # Two rows per analytic type, each tagged with the type so the test can
    # assert the right sourcetype carried the right payload.
    return [
        {"mock_marker": analytic_type, "profile": profile, "domain": f"{analytic_type}.mock.example", "queries": 42},
        {"mock_marker": analytic_type, "profile": profile, "domain": f"{analytic_type}2.mock.example", "queries": 7},
    ]


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        m = ANALYTICS_RE.match(self.path)
        if m:
            profile, analytic_type = m.group(1), m.group(2)
            self._send_json({"data": _analytics_rows(profile, analytic_type)})
            return

        m = STREAM_RE.match(self.path)
        if m:
            profile = m.group(1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(STREAM_EVENTS):
                payload = json.dumps({"mock_marker": "stream", "profile": profile, "domain": f"stream{i}.mock.example", "status": "blocked"})
                # The collector strips the "data: " prefix (line[6:]), so keep the space.
                self.wfile.write(f"data: {payload}\n".encode())
                self.wfile.flush()
            return

        if self.path.rstrip("/") == "/profiles":
            self._send_json({"data": [{"id": "testprofile", "name": "Mock Profile"}]})
            return

        self._send_json({"error": "not found", "path": self.path}, status=404)

    def log_message(self, fmt, *args):
        # One concise line per request to the container log, so `docker compose
        # logs mock` shows whether the collectors actually reached the mock
        # (paths only — the mock never sees real data).
        print(f"MOCK {self.command} {self.path}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
