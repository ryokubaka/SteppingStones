import ldap
from django_auth_ldap.config import LDAPSearch, GroupOfNamesType
from .models import LDAPSettings
import logging

def get_ldap_config(settings=None):
    """Get LDAP configuration from database settings."""
    logger = logging.getLogger("event_tracker.auth.ldap")
    if settings is None:
        settings = LDAPSettings.get_settings()
    if not settings.enabled:
        logger.info("LDAP is disabled in DB settings.")
        return None

    logger.info(f"LDAPSettings from DB: server_uri={settings.server_uri}, bind_dn={settings.bind_dn}, user_search_base={settings.user_search_base}, user_search_filter={settings.user_search_filter}, enabled={settings.enabled}")

    if not settings.user_search_base or not settings.user_search_filter:
        logger.error("LDAP user_search_base and user_search_filter must be set in LDAPSettings.")
        raise ValueError("LDAP user_search_base and user_search_filter must be set in LDAPSettings.")

    # Common connection options for all LDAP operations
    connection_options = {
        ldap.OPT_X_TLS_REQUIRE_CERT: settings.tls_require_cert,
        ldap.OPT_X_TLS_DEMAND: settings.tls_demand,
        ldap.OPT_X_TLS_CACERTFILE: settings.tls_cacertfile,
        ldap.OPT_X_TLS_NEWCTX: 0,
        ldap.OPT_REFERRALS: 0,  # Disable referrals
        ldap.OPT_NETWORK_TIMEOUT: 30,  # Add timeout
    }

    # Set global LDAP options
    ldap.set_option(ldap.OPT_REFERRALS, 0)

    config = {
        'AUTH_LDAP_CONNECTION_OPTIONS': connection_options,
        'AUTH_LDAP_SERVER_URI': settings.server_uri,
        'AUTH_LDAP_BIND_DN': settings.bind_dn,
        'AUTH_LDAP_BIND_PASSWORD': settings.bind_password,
        'AUTH_LDAP_USER_SEARCH': LDAPSearch(
            settings.user_search_base,
            ldap.SCOPE_SUBTREE,
            settings.user_search_filter,
        ),
        'AUTH_LDAP_USER_ATTR_MAP': settings.user_attr_map,
        'AUTH_LDAP_CACHE_TIMEOUT': settings.cache_timeout,
        'AUTH_LDAP_ALWAYS_UPDATE_USER': True,
    }

    # Add group search if configured
    if settings.group_search_base:
        config['AUTH_LDAP_GROUP_SEARCH'] = LDAPSearch(
            settings.group_search_base,
            ldap.SCOPE_SUBTREE,
            settings.group_search_filter,
        )
        config['AUTH_LDAP_GROUP_TYPE'] = GroupOfNamesType(name_attr='cn')
        config['AUTH_LDAP_FIND_GROUP_PERMS'] = True

    # Add required/denied groups if configured
    if settings.require_group:
        config['AUTH_LDAP_REQUIRE_GROUP'] = settings.require_group
    if settings.deny_group:
        config['AUTH_LDAP_DENY_GROUP'] = settings.deny_group

    # Add user flags by group if configured
    if settings.user_flags_by_group:
        config['AUTH_LDAP_USER_FLAGS_BY_GROUP'] = settings.user_flags_by_group

    # Always update user data from LDAP
    config['AUTH_LDAP_ALWAYS_UPDATE_USER'] = True

    return config 