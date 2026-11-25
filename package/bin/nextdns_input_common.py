"""Splunk-side plumbing shared by both inputs (account lookup, logger, clock).

Privacy: the inputs log counts, window bounds and error status/codes only. They
never log the API key, a URL, a profile name or any part of the DNS data.
"""
import logging
import time

import import_declare_test  # noqa: F401  (sets sys.path for lib/)

from solnlib import conf_manager, log

ADDON_NAME = "TA-nextdns-api"
REST_PATH = "ta_nextdns_api"

# How far behind "now" a window ends, so queries NextDNS is still writing have
# landed before the window is read (checked live 25 Nov 2026; see README).
LAG_MS = 120 * 1000
# A first run collects the last hour; after an outage each run catches up at
# most this much, so a long gap is collected over several runs.
LOOKBACK_MS = 3600 * 1000
MAX_WINDOW_MS = 6 * 3600 * 1000


def now_ms():
    return int(time.time() * 1000)


def logger_for_input(input_name: str) -> logging.Logger:
    return log.Logs().get_logger("{}_{}".format(ADDON_NAME.lower(), input_name))


def setup_logger(logger, session_key):
    try:
        level = conf_manager.get_log_level(
            logger=logger, session_key=session_key, app_name=ADDON_NAME, conf_name="{}_settings".format(REST_PATH)
        )
        logger.setLevel(level)
    except Exception:  # settings not saved yet: keep the default level
        logger.setLevel(logging.INFO)


def get_account_api_key(session_key: str, account_name: str):
    if not account_name:
        raise ValueError("input has no account")
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm="__REST_CREDENTIAL__#{}#configs/conf-{}_account".format(ADDON_NAME, REST_PATH),
    )
    account = cfm.get_conf("{}_account".format(REST_PATH)).get(account_name)
    return (account or {}).get("api_key")
