import re

from event_tracker.cred_extractor import CredentialExtractorGenerator, valid_windows_domain
from event_tracker.models import Credential, HashCatMode

#domain_cached_credential2_regex = re.compile(
#    r'(?P<system>' + valid_windows_domain + ')/[^\s/\:]+:(?P<hash>\$DCC2\$\d+#(?P<account>[^#]+)#[0-9a-f]{32})')

domain_cached_credential2_regex = re.compile(
    rf'(?P<system>{valid_windows_domain})/[^\s/:]+:(?P<hash>\$DCC2\$\d+#(?P<account>[^#]+)#[0-9a-f]{{32}})')

class DCC2Extractor(CredentialExtractorGenerator):
    def cred_generator(self, input_text: str, default_system: str):
        for match in domain_cached_credential2_regex.finditer(input_text):
            yield Credential(**match.groupdict(), purpose="Windows Login",
                             hash_type=HashCatMode.Domain_Cached_Credentials_2)
