import json
import logging

import import_declare_test

from solnlib import conf_manager, log
from splunklib import modularinput as smi
import requests

ADDON_NAME = "TA-nextdns-api"
REST_PATH = "ta_nextdns_api"


def validate_input(definition: smi.ValidationDefinition):
    return


def logger_for_input(input_name: str) -> logging.Logger:
    return log.Logs().get_logger(f"{ADDON_NAME.lower()}_{input_name}")


def get_account_api_key(session_key: str, account_name: str):
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm=f"__REST_CREDENTIAL__#{ADDON_NAME}#configs/conf-{REST_PATH}_account",
    )
    account_conf_file = cfm.get_conf(f"{REST_PATH}_account")
    return account_conf_file.get(account_name).get("api_key")


def get_data_from_api(logger: logging.Logger, api_key: str, profile: str):
    logger.info("Getting data from NextDNS Streaming endpoint")
    url = f"https://api.nextdns.io/profiles/{profile}/logs/stream"
    with requests.get(url, headers={"x-api-key": api_key}, stream=True) as resp:
        resp.raise_for_status()  # Ensure we raise an error for bad responses
        for line in resp.iter_lines():
            if line:
                # Assuming each line is a valid JSON object
                yield line.decode("utf-8")


def stream_events(inputs: smi.InputDefinition, ew: smi.EventWriter):
    # inputs.inputs is a Python dictionary object like:
    # {
    #   "NextDNS_getStats://<input_name>": {
    #     "account": "<account_name>",
    #     "disabled": "0",
    #     "host": "$decideOnStartup",
    #     "index": "<index_name>",
    #     "interval": "<interval_value>",
    #     "python.version": "python3",
    #   },
    # }
    for input_name, input_item in inputs.inputs.items():
        normalized_input_name = input_name.split("/")[-1]
        logger = logger_for_input(normalized_input_name)
        try:
            session_key = inputs.metadata["session_key"]
            log_level = conf_manager.get_log_level(
                logger=logger,
                session_key=session_key,
                app_name=ADDON_NAME,
                conf_name=f"{REST_PATH}_settings",
            )
            logger.setLevel(log_level)
            log.modular_input_start(logger, normalized_input_name)
            api_key = get_account_api_key(session_key, input_item.get("account"))

            sourcetype = "NextDNS_API_Stream"
            for line in get_data_from_api(logger, api_key, input_item.get("profile")):
                if line.startswith("data:"):
                    data = line[6:]
                    log.log_event(logger, {"line": data})
                    ew.write_event(smi.Event(data=data, index=input_item.get("index"), sourcetype=sourcetype, host=input_item.get("profile")))

            log.modular_input_end(logger, normalized_input_name)
        except Exception as e:
            log.log_exception(logger, e, "my custom error type", msg_before="Exception raised while ingesting data for demo_input: ")

    # input_items = [{"count": len(inputs.inputs)}]
    # for input_name, input_item in inputs.inputs.items():
    #     input_item["name"] = input_name
    #     input_items.append(input_item)
    # event = smi.Event(
    #     data=json.dumps(input_items),
    #     sourcetype="NextDNS_API_Stats",
    # )
    # ew.write_event(event)
