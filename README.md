# TA-nextdns-api

NextDNS API Collector for Splunk. A UCC-based add-on that pulls data from the
[NextDNS API](https://nextdns.io) into Splunk via two modular inputs:

- **NextDNS_API_Stats** (NextDNS Analytics): status, protocols, devices, IPs,
  reasons, domains, query types, IP versions, DNSSEC and encryption, one
  sourcetype per type (`NextDNS_API_Stats:<type>`). "Data to log" picks types;
  none selected means all.
- **NextDNS_API_Stream** (NextDNS Query Log): every DNS query, sourcetype
  `NextDNS_API_Stream`, event time = the query's timestamp. "All queries (raw)"
  adds the queries NextDNS hides by default (other record types, repeats,
  background noise).

## How collection works

Both inputs collect consecutive time windows. A run reads from where the last
run stopped (a checkpoint file per input) up to two minutes before now, so
queries NextDNS is still writing have landed. The first run looks back one
hour; after an outage a run catches up at most six hours, and later runs do
the rest. Every page is read (`meta.pagination.cursor`). Nothing is written and
the checkpoint stays put if any request fails, so the next run retries. 429 and
5xx are retried up to three times (Retry-After honoured). A rejected key is
logged as `status=403 codes=authRequired`; the add-on never logs the key, URLs,
profile names or query data.

- Analytics events carry `window_start` and `window_end` (ISO 8601 UTC, end
  exclusive) and the event time is the window start. Windows do not overlap,
  so `sum(queries)` over any period counts each query once.
- The query log is polled from `/profiles/<id>/logs`. Up to 1.1.x the input
  held `/logs/stream` open with no timeout and no resume point, so a dropped
  connection lost queries and a stalled one hung the input.
- With raw off, NextDNS de-duplicates per request, so the events for a period
  can differ by a few from what one request for the whole period returns.

Changes after 1.1.6 that affect searches: analytics are per window instead of a
repeated all-time top 10 (the API's default page size) on every run; "Data to
log" is honoured; `blocked` and `action` are now set from the query status
(`blocked` was always "permitted"); `device_name` is the NextDNS device name
(it was the profile id).

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

| Tier | Runs | What |
|---|---|---|
| `tests/unit` | every push, Python 3.9 and 3.13 | client, pagination, retries, windows, checkpoints, against the mock in process |
| `tests/test_*.py` | every push except Dependabot, Splunk 10.0 and 10.6.0 in Docker | install smokes, and every input end to end against `docker/mock`: many pages, 429/503, an outage, a rejected key, empty windows, field extractions, the profile dropdown |
| `tests/live` | push to main, weekly schedule, manual; never pull requests or Dependabot | every input against the real API on Splunk 10.6.0: events indexed with the expected sourcetypes and fields, matching the API's own answer, checkpoints advancing, no duplicates, key never logged |

The live tier asserts and prints counts and field names only (the account's
DNS data is private and this repository is public) and runs pytest with
`--tb=no -rN`. AppInspect runs against the `cloud`, `future` and
`private_victoria` tag sets on every push.

The mock follows the real API's pagination: a short opaque
`meta.pagination.cursor`, null on the last page for analytics and
`sort=desc` logs; for `sort=asc` logs the cursor is never null and an empty
page repeating the same cursor marks the end (checked live, November 2026).
