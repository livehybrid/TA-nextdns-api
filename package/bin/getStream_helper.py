"""NextDNS_API_Stream: the query log, collected in consecutive windows.

Each run reads every log in [checkpoint, now - LAG_MS) from
`/profiles/<profile>/logs` oldest first, following the pagination cursor to
the end, writes one event per query (event time = the query's timestamp) and
then moves the checkpoint to the window end. `from` is inclusive and `to`
exclusive upstream, so consecutive windows neither overlap nor leave gaps.

Until 1.1.x this input held `/logs/stream` (server-sent events) open with no
timeout and no resume point: anything logged while it was reconnecting was
lost, and a stalled connection hung the input for good. Polling the log with a
checkpoint has neither problem; the delay is the input interval plus LAG_MS.
The events are the same JSON objects the stream sent.
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

KIND = "NextDNS_API_Stream"
SOURCETYPE = "NextDNS_API_Stream"


def validate_input(definition: smi.ValidationDefinition):
    return


def stream_events(inputs: smi.InputDefinition, ew: smi.EventWriter):
    checkpoint_dir = inputs.metadata.get("checkpoint_dir", ".")
    session_key = inputs.metadata["session_key"]
    for input_name, item in inputs.inputs.items():
        name = input_name.split("://")[-1]
        logger = logger_for_input(name)
        setup_logger(logger, session_key)
        log.modular_input_start(logger, name)
        try:
            profile = item.get("profile")
            if not profile:
                raise ValueError("input has no profile")
            checkpoint = nd.FileCheckpoint(checkpoint_dir, KIND, name)
            window = nd.next_window(checkpoint.load(), now_ms(), LAG_MS, LOOKBACK_MS, MAX_WINDOW_MS)
            if window is None:
                logger.info("input=%s nothing to collect yet", name)
                continue
            start, end = window
            client = nd.Client(get_account_api_key(session_key, item.get("account")))
            # Fetch the whole window before writing, so a failure part way
            # through writes nothing and the retry cannot duplicate events.
            rows = client.logs(profile, start, end, raw=nd.is_true(item.get("raw")))
            for row in rows:
                # Do not log `row`: it is the user's DNS query.
                ew.write_event(
                    smi.Event(
                        data=json.dumps(row, ensure_ascii=False, default=str),
                        time=nd.event_time(row.get("timestamp"), start),
                        index=item.get("index"),
                        sourcetype=SOURCETYPE,
                        host=profile,
                    )
                )
            checkpoint.save(end)
            logger.info(
                "input=%s run complete events=%d requests=%d window_seconds=%d",
                name, len(rows), client.requests_made, (end - start) // 1000,
            )
        except nd.NextDNSError as exc:
            logger.error("input=%s run failed: %s", name, exc)
        except Exception:
            logger.exception("input=%s run failed", name)
        finally:
            log.modular_input_end(logger, name)
