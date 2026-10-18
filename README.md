# TA-nextdns-api

NextDNS API Collector for Splunk. A UCC-based add-on that pulls DNS analytics
from the [NextDNS API](https://nextdns.io) into Splunk via two modular inputs:

- **NextDNS_API_Stats** — periodic analytics snapshots (top domains, blocked
  queries, device and protocol breakdowns).
- **NextDNS_API_Stream** — the near-real-time query log stream.

## Compatibility

| Attribute | Value |
|-----------|-------|
| **Add-on version** | 1.1.x |
| **Python runtime** | 3.13 on Splunk 10.2 and later, 3.9 on Splunk 10.1 and earlier |
| **Expected compatible** | Splunk Enterprise and Cloud 9.3+ and 10.x |
| **Tested in CI** | AppInspect `cloud`, `future` and `private_victoria` tag sets on every push |
| **Deployment roles** | Standalone, Distributed, Search Head Clustering |

Splunk 10.2 and later ship Python 3.9 and 3.13 side by side; this add-on sets
`python.required = 3.13`, so it runs on 3.13 there. Splunk 10.1 and earlier
ignore `python.required` and run it on 3.9 (`python.version = python3`), so the
code and vendored libraries stay 3.9-compatible (`requests < 2.33`,
`splunk-sdk < 3`). CI runs the integration tests on Splunk 10.0 (Python 3.9)
and 10.6 (Python 3.13) to hold both.

## Testing

CI runs AppInspect against the `cloud`, `future` and `private_victoria` tag
sets on every push.

A real-Splunk integration test suite (add-on installs, both modular-input
kinds register and expose their schemes on the Splunk 10.0 (Python 3.9) and 10.6 (Python 3.13) runtimes),
matching the pattern used by the sibling TA-* add-ons, is a planned follow-up.
The `docker/` directory currently holds an unused scaffold (a Splunk container
plus a standalone Squid forward-proxy image) and does not yet run any tests.
