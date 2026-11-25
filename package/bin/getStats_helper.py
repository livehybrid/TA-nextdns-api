"""NextDNS_API_Stats: analytics for consecutive time windows.

Each run collects the analytics for the window since the previous run (from
the checkpoint) up to LAG_MS before now, every page of every selected type, and
writes one event per row with `window_start` / `window_end` added (ISO 8601,
UTC, end exclusive) and the event time at the window start. Windows never
overlap, so `sum(queries)` over any time range counts each query once.

The checkpoint advances only after every selected type was fetched, so a
failed run writes nothing and the next run retries the same window (larger).
"""
import json

import import_declare_test  # noqa: F401

from splunklib import modularinput as smi

import nextdns_client as nd
from nextdns_input_common import (
    LAG_MS,
    LOOKBACK_MS,
    MAX_WINDOW_MS,
    get_account_api_key,
    logger_for_input,
    now_ms,
    setup_logger,
)
from solnlib import log

KIND = "NextDNS_API_Stats"


def validate_input(definition: smi.ValidationDefinition):
    return


def collect(client, profile, types, window):
    """{type: rows} for the window. Raises on any failure (nothing is written)."""
    start, end = window
    return {t: client.analytics(profile, t, start, end) for t in types}


def stream_events(inputs: smi.InputDefinition, ew: smi.EventWriter):
    checkpoint_dir = inputs.metadata.get("checkpoint_dir", ".")
    session_key = inputs.metadata["session_key"]
    for input_name, item in inputs.inputs.items():
        name = input_name.split("://")[-1]
        logger = logger_for_input(name)
        setup_logger(logger, session_key)
        log.modular_input_start(logger, name)
        try:
            types, unknown = nd.selected_types(item.get("sections"))
            if unknown:
                logger.warning("input=%s ignoring unknown sections count=%d", name, len(unknown))
            profile = item.get("profile")
            if not profile:
                raise ValueError("input has no profile")
            checkpoint = nd.FileCheckpoint(checkpoint_dir, KIND, name)
            window = nd.next_window(checkpoint.load(), now_ms(), LAG_MS, LOOKBACK_MS, MAX_WINDOW_MS)
            if window is None:
                logger.info("input=%s nothing to collect yet", name)
                continue
            client = nd.Client(get_account_api_key(session_key, item.get("account")))
            data = collect(client, profile, types, window)
            start, end = window
            extra = {"window_start": nd.iso_ms(start), "window_end": nd.iso_ms(end)}
            written = 0
            for analytic_type in types:
                for row in data[analytic_type]:
                    body = dict(row)
                    body.update(extra)
                    ew.write_event(
                        smi.Event(
                            data=json.dumps(body, ensure_ascii=False, default=str),
                            time="%.3f" % (start / 1000.0),
                            index=item.get("index"),
                            sourcetype="{}:{}".format(KIND, analytic_type),
                            host=profile,
                        )
                    )
                    written += 1
            checkpoint.save(end)
            logger.info(
                "input=%s run complete events=%d types=%d requests=%d window_seconds=%d",
                name, written, len(types), client.requests_made, (end - start) // 1000,
            )
        except nd.NextDNSError as exc:
            logger.error("input=%s run failed: %s", name, exc)
        except Exception:
            logger.exception("input=%s run failed", name)
        finally:
            log.modular_input_end(logger, name)
