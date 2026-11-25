"""REST handler behind the inputs' Profile dropdown: lists the account's profiles.

Uses the same API base as the inputs (so a gateway override or the test mock
applies here too). Logs counts and error codes only: never the key, the
profile list or a profile name.
"""
import import_declare_test  # noqa: F401

import splunk.admin as admin

import nextdns_client as nd
from nextdns_input_common import get_account_api_key, logger_for_input

logger = logger_for_input("select_profile")
FIELDS = ["account"]


def profile_label(account, profile):
    return "{} - {} ({})".format(account, profile.get("name", ""), profile.get("id", ""))


class NextDNSProfileSelect(admin.MConfigHandler):
    def setup(self):
        for arg in FIELDS:
            self.supportedArgs.addOptArg(arg)

    def handleList(self, confInfo):
        if not self.callerArgs or not self.callerArgs.get("account"):
            raise admin.ArgValidationException("Select an account first")
        account = self.callerArgs["account"][0]
        try:
            api_key = get_account_api_key(self.getSessionKey(), account)
            profiles = nd.Client(api_key).profiles()
        except nd.NextDNSError as exc:
            logger.error("profile list failed: %s", exc)
            raise admin.InternalException("NextDNS API: {}".format(exc))
        for profile in profiles:
            pid = profile.get("id")
            if pid:
                confInfo[pid]["profile"] = profile_label(account, profile)
        logger.info("profile list returned count=%d", len(profiles))


if __name__ == "__main__":
    admin.init(NextDNSProfileSelect, admin.CONTEXT_APP_AND_USER)
