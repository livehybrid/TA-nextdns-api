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
| **Python runtime** | 3.9, Splunk's long-term-support runtime (pinned) |
| **Expected compatible** | Splunk Enterprise and Cloud 9.3+ and 10.x (any release on the Python 3.9 runtime) |
| **Tested in CI** | AppInspect `cloud`, `future` and `private_victoria` tag sets on every push |
| **Deployment roles** | Standalone, Distributed, Search Head Clustering |

Splunk 9.3 through 10.1 default to Python 3.9, and 3.9 stays the LTS runtime on
10.2 and later, so an add-on that is clean on 3.9 runs unchanged across that
whole range. This add-on pins the runtime to 3.9 (`python.required = 3.9` on
every generated input and REST handler, with `python.version = python3` as the
Splunk <=10.1 fallback). It is not yet validated on the opt-in Python 3.13
runtime introduced in Splunk 10.2.

## Testing

CI runs AppInspect against the `cloud`, `future` and `private_victoria` tag
sets on every push.

A Splunk-in-Docker integration harness lives in `docker/` and can be run
locally with `docker compose up`. Because NextDNS is a keyed upstream, the
harness ships a **mock NextDNS proxy** (`docker/proxy`) that lets the collectors
be exercised end-to-end against real Splunk without a live API key. Wiring this
harness into CI as a gating integration-test job (matching the sibling TA-*
add-ons) is a planned follow-up.
