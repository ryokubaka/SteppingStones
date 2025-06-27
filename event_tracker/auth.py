from django.contrib.auth.backends import ModelBackend
from django_auth_ldap.backend import LDAPBackend
from .ldap_config import get_ldap_config
import logging
from django.conf import settings as django_settings

class DynamicLDAPBackend(LDAPBackend):
    """LDAP authentication backend that uses dynamic settings from the database."""
    
    logger = logging.getLogger("event_tracker.auth.ldap")

    def __init__(self):
        super().__init__()

    def _configure_from_db(self):
        """Configure the backend using settings from the database (always from default DB)."""
        # No need to set attributes on self; settings are now patched at runtime

    def authenticate(self, request, username=None, password=None, **kwargs):
        """Authenticate a user using LDAP if enabled."""
        self.logger.info(f"DynamicLDAPBackend: authenticate called for user: {username}")
        self._configure_from_db()  # Still call for debug/logging
        config = get_ldap_config()
        if not config or not config.get("AUTH_LDAP_SERVER_URI"):
            self.logger.info(f"LDAP is disabled. Skipping LDAP authentication for user: {username}")
            return None  # LDAP is disabled

        # Patch Django settings with dynamic config
        for key, value in config.items():
            setattr(django_settings, key, value)

        self.logger.info(f"Attempting LDAP authentication for user: {username}")
        try:
            user = super().authenticate(request, username, password, **kwargs)
            if user is not None:
                self.logger.info(f"LDAP authentication SUCCESS for user: {username}")
            else:
                self.logger.warning(f"LDAP authentication FAILED for user: {username}")
            return user
        except Exception as e:
            self.logger.error(f"LDAP authentication ERROR for user: {username}: {e}", exc_info=True)
            return None

class FallbackBackend(ModelBackend):
    """Fallback to database authentication if LDAP fails."""
    pass 