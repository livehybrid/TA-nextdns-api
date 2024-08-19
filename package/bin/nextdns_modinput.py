import import_declare_test
from splunktaucclib.modinput_wrapper.base_modinput import BaseModInput
from splunktaucclib.splunk_aoblib.setup_util import Setup_Util
import splunk.Intersplunk as si
from solnlib import conf_manager
from splunklib import client as client
import json


class NextDNSModInput(BaseModInput):
    appName = "TA-nextdns-api"
    restPath = "ta_nextdns_api"

    def collect_data(self, inputs, ew):
        pass

    def stream_events(self, inputs, ew):
        self.context_meta = inputs.metadata
        self.session_key = inputs.metadata["session_key"]
        self.splunk_uri = inputs.metadata["server_uri"]
        self.setup_util = Setup_Util(self.splunk_uri, self.session_key)
        self.collect_data(inputs, ew)

    def get_api_key(self, accountName):
        return self.get_account_credentials(accountName=accountName)["api_key"]

    def get_account_credentials(self, accountName):
        account_cfm = conf_manager.ConfManager(self.session_key, self.appName, realm=f"__REST_CREDENTIAL__#{self.appName}#configs/conf-{self.restPath}_account")

        # Check if account is empty
        if not accountName:  # pylint: disable=E1101
            si.generateErrorResults("Enter NextDNS account name.")
            raise Exception(
                "Account name cannot be empty. Enter a configured account name or " "create new account by going to Configuration page of the Add-on."
            )
        # Get account details

        self.log_info("Getting details for account '{}'".format(accountName))
        self.account_conf = account_cfm.get_conf(f"{self.restPath}_account")
        account_details = self.account_conf.get(accountName)
        return account_details

    def get_api(self, url: str, account_name: str):
        api_key = self.get_api_key(account_name)
        request = {"verify": True, "headers": {"x-api-key": api_key, "Content-Type": "application/json"}}
        response = self.send_http_request(url, "GET", **request)
        try:
            response.raise_for_status()
        except:
            uri = f"{self.splunk_uri}/services/messages/new"
            headers = {}
            headers["Authorization"] = "Splunk " + self.session_key
            data = {"name": "NextDNS App", "value": "Unable to request to nextDNS", "severity": "warn"}
            message_req = {"headers": headers, "payload": data, "verify": True}
            r = self.send_http_request(uri, "POST", headers=headers, payload=data, verify=False)
            if r.status_code < 300:
                self.log_info("Logged message to UI")
            else:
                self.log_info(f"Resp from message API - status_code={r.status_code}")
            # {"error":"invalid bearer token"}
            self.log_warning("Error from API")
        return response
