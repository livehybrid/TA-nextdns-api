import import_declare_test
import splunk.admin as admin
import splunk.clilib.cli_common as scc
import logging

from splunktaucclib.splunk_aoblib.setup_util import Setup_Util
from nextdns_modinput import NextDNSModInput

logger = logging.getLogger()

import splunktaucclib.common.log as stulog

APPNAME = "TA-nextdns-api"
FIELDS = ["account"]


class NextDNSProfileSelect(admin.MConfigHandler):

    def setup(self):
        stulog.logger.error("SETUP")
        for arg in FIELDS:
            self.supportedArgs.addOptArg(arg)

    @staticmethod
    def validate_params(must_params, opt_params, **params):
        pass

    def handleList(self, confInfo):
        if not self.callerArgs or not self.callerArgs.get("account"):
            logger.error("Missing NextDNS credentials")
            raise Exception("Missing NextDNS credentials")
        nextdns_account = self.callerArgs["account"][0]

        api_helper = NextDNSModInput(app_namespace=APPNAME, input_name="NextDNS_API_Stats")
        api_helper.log_warning(nextdns_account)
        api_helper.session_key = self.getSessionKey()
        api_helper.splunk_uri = scc.getMgmtUri()
        api_helper.setup_util = Setup_Util(api_helper.splunk_uri, api_helper.session_key)

        url = "https://api.nextdns.io/profiles"
        response = api_helper.get_api(url, nextdns_account)
        response_json = response.json()
        api_helper.log_warning(response_json)
        for profile in response_json["data"]:
            api_helper.log_warning(profile["id"])
            confInfo[profile["id"]]["profile"] = f"{nextdns_account} - {profile['name']} ({profile['id']})"
            api_helper.log_warning(confInfo)
        return


if __name__ == "__main__":
    admin.init(NextDNSProfileSelect, admin.CONTEXT_APP_AND_USER)
