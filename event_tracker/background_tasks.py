import time
from datetime import datetime

import requests
from background_task import background
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Count, Value
from django.db.models.functions import Substr, Upper, Concat
from neo4j.exceptions import ClientError

from event_tracker.models import BloodhoundServer, Credential, HashCatMode, Event, Context, CurrentOperation
from event_tracker.signals import get_driver_for

import logging
from django.db import connections
from django.utils import timezone
from django.conf import settings
from django.contrib.auth.models import User
from django.db.utils import OperationalError

logger = logging.getLogger(__name__)


def _count_disabled_accounts(tx):
    query = ("""MATCH (n:User) where 
             n.enabled=false
             return count(n)"""
             )

    return tx.run(query).single()


def _get_disabled_accounts(tx):
    query = ("""MATCH (n:User) where 
             n.enabled=false
             with split(n.name, '@') as a
             return a[1], a[0]"""
             )

    result = tx.run(query)

    return [record.values() for record in result]

@background(schedule=5)
def sync_pwnedpasswords():
    """
    Syncs pwned passwords. Now iterates over all operation databases since credentials are operation-specific.
    """
    from pathlib import Path
    
    ops_data_dir = getattr(settings, 'OPS_DATA_DIR', None)
    if not ops_data_dir:
        logger.warning("OPS_DATA_DIR not set, skipping sync_pwnedpasswords")
        return
    
    ops_data_dir = Path(ops_data_dir)
    if not ops_data_dir.exists():
        logger.warning(f"OPS_DATA_DIR {ops_data_dir} does not exist, skipping sync_pwnedpasswords")
        return
    
    # Iterate over all operation databases
    op_dbs = list(ops_data_dir.glob('*.sqlite3'))
    for op_db in op_dbs:
        # Skip placeholder database
        if op_db.name == '_placeholder_op.sqlite3':
            continue
        
        # Update the active_op_db connection to point to this operation database
        connections.close_all()
        settings.DATABASES['active_op_db']['NAME'] = str(op_db)
        if hasattr(connections['active_op_db'], 'settings_dict'):
            connections['active_op_db'].settings_dict['NAME'] = str(op_db)
        connections['active_op_db'].connection = None
        
        try:
            # Check if database is ready (has django_migrations table)
            with connections['active_op_db'].cursor() as cursor:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_migrations'")
                if not cursor.fetchone():
                    logger.debug(f"Skipping {op_db.name} - migrations not complete yet")
                    continue
                
                # Check if credential table exists
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_tracker_credential'")
                if not cursor.fetchone():
                    logger.debug(f"Skipping {op_db.name} - credential table not created yet")
                    continue
            
            # Database is ready, query credentials
            query = Credential.objects.using('active_op_db').exclude(hash__isnull=True).exclude(hash="")\
                .filter(hash_type=HashCatMode.NTLM, haveibeenpwned_count__isnull=True)\
                .values("hash")\
                .annotate(group_by=Count("hash"), prefix=Substr("hash", 1, 5), suffix=Upper(Substr("hash", 6)))

            if not query.exists():
                continue
        except OperationalError as e:
            # Database might not be ready yet or is being migrated
            logger.debug(f"Skipping {op_db.name} - database not ready: {e}")
            continue
        except Exception as e:
            logger.warning(f"Error checking database {op_db.name}: {e}")
            continue

    start = time.time()
    print("We don't do no stinking haveibeenpwned checking")
    # print("Starting sync of haveibeenpwned hashes")

    # db_hash_count = 0
    # for db_hash in query.all():
    #     db_hash_count += 1
    #     response = requests.get(f'https://api.pwnedpasswords.com/range/{db_hash["prefix"]}?mode=ntlm')
    #     if response.ok:
    #         count = 0
    #         for line in response.text.split("\n"):
    #             if line.startswith(db_hash["suffix"]):
    #                 suffix, count = line.strip().split(":", 1)
    #                 break
    #         Credential.objects.filter(hash_type=HashCatMode.NTLM, hash=db_hash["hash"]).update(haveibeenpwned_count=count)
    #         print(f"Hash {db_hash['hash']} {'not ' if count == 0 else ''}found at pwnedpasswords.com")
    #     else:
    #         print(f"Error {response.status_code} from pwnedpasswords.com: {response.text}")

    # print(f"Done sync of {db_hash_count:,} haveibeenpwned hashes in {time.time() - start:.2f} seconds")


@background(schedule=5)
def sync_disabled_users():
    """
    Syncs users marked as disabled in Bloodhound with the users in the Credentials table
    This now iterates over all operation databases since credentials are operation-specific.
    """
    from pathlib import Path
    
    ops_data_dir = getattr(settings, 'OPS_DATA_DIR', None)
    if not ops_data_dir:
        logger.warning("OPS_DATA_DIR not set, skipping sync_disabled_users")
        return
    
    ops_data_dir = Path(ops_data_dir)
    if not ops_data_dir.exists():
        logger.warning(f"OPS_DATA_DIR {ops_data_dir} does not exist, skipping sync_disabled_users")
        return
    
    # Collect total users across all operation databases
    total_users_in_all_ops = 0
    op_dbs = list(ops_data_dir.glob('*.sqlite3'))
    for op_db in op_dbs:
        # Skip placeholder database
        if op_db.name == '_placeholder_op.sqlite3':
            continue
        
        # Update the active_op_db connection to point to this operation database
        connections.close_all()
        settings.DATABASES['active_op_db']['NAME'] = str(op_db)
        if hasattr(connections['active_op_db'], 'settings_dict'):
            connections['active_op_db'].settings_dict['NAME'] = str(op_db)
        connections['active_op_db'].connection = None
        
        try:
            # Check if database is ready (has django_migrations table and credential table)
            with connections['active_op_db'].cursor() as cursor:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_migrations'")
                if not cursor.fetchone():
                    logger.debug(f"Skipping {op_db.name} - migrations not complete yet")
                    continue
                
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_tracker_credential'")
                if not cursor.fetchone():
                    logger.debug(f"Skipping {op_db.name} - credential table not created yet")
                    continue
            
            # Database is ready, count users
            total_users_in_all_ops += Credential.objects.using('active_op_db').count()
        except OperationalError as e:
            # Database might not be ready yet or is being migrated
            logger.debug(f"Skipping operation database {op_db.name} for user count - not ready: {e}")
            continue
        except Exception as e:
            logger.warning(f"Error counting users in operation database {op_db.name}: {e}")
            continue
    
    cached_total_users_in_local_database = cache.get("total_users_in_local_database", 0)
    cache.set("total_users_in_local_database", total_users_in_all_ops)

    # Disabled accounts - process for each operation database
    for server in BloodhoundServer.objects.filter(active=True).all():
        driver = get_driver_for(server)

        if driver:
            with driver.session() as session:  # Neo4j Session
                cached_disabled_users_in_neo4j = cache.get(f"disabled_users_in_{server.neo4j_connection_url}", 0)
                actual_disabled_users_in_neo4j = len(session.execute_read(_get_disabled_accounts))
                cache.set(f"disabled_users_in_{server.neo4j_connection_url}", actual_disabled_users_in_neo4j)

            # If the neo4j or local_database has changed since last invocation, or the cache is empty suggesting a restart:
            if cached_disabled_users_in_neo4j != actual_disabled_users_in_neo4j \
                    or cached_total_users_in_local_database != total_users_in_all_ops:
                start = time.time()
                print("Starting local copy of disabled users")
                
                # Process each operation database
                for op_db in op_dbs:
                    # Skip placeholder database
                    if op_db.name == '_placeholder_op.sqlite3':
                        continue
                    
                    # Update the active_op_db connection to point to this operation database
                    connections.close_all()
                    settings.DATABASES['active_op_db']['NAME'] = str(op_db)
                    if hasattr(connections['active_op_db'], 'settings_dict'):
                        connections['active_op_db'].settings_dict['NAME'] = str(op_db)
                    connections['active_op_db'].connection = None
                    
                    try:
                        # Check if database is ready before processing
                        with connections['active_op_db'].cursor() as cursor:
                            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_migrations'")
                            if not cursor.fetchone():
                                logger.debug(f"Skipping {op_db.name} - migrations not complete yet")
                                continue
                            
                            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_tracker_credential'")
                            if not cursor.fetchone():
                                logger.debug(f"Skipping {op_db.name} - credential table not created yet")
                                continue
                        
                        with transaction.atomic(using='active_op_db'):  # SQLite transaction for this op
                            with driver.session() as session:  # Neo4j Session
                                try:
                                    disabled_accounts = session.execute_read(_get_disabled_accounts)

                                    system_account_dict = dict()

                                    for acc in disabled_accounts:
                                        if acc[0] not in system_account_dict:
                                            system_account_dict[acc[0]] = list()

                                        system_account_dict[acc[0]].append(acc[1])

                                    for system in system_account_dict:
                                        system_filter = Credential.objects.using('active_op_db').filter(system__iexact=system, enabled=True)
                                        account_q = Q()
                                        account_count = 0
                                        for account in system_account_dict[system]:
                                            account_count += 1
                                            if account_count % 900 == 0:
                                                # Flush query
                                                system_filter.filter(account_q).update(enabled=False)
                                                account_q = Q()
                                            else:
                                                # Build query
                                                account_q |= Q(account__iexact=account)

                                        # Final flush
                                        system_filter.filter(account_q).update(enabled=False)
                                except ClientError:
                                    pass  # Likely caused by no accounts being enabled for this system
                    except OperationalError as e:
                        # Database might not be ready yet or is being migrated
                        logger.debug(f"Skipping operation database {op_db.name} for disabled users sync - not ready: {e}")
                        continue
                    except Exception as e:
                        logger.warning(f"Error syncing disabled users in operation database {op_db.name}: {e}")
                        continue
                
                print(f"Done local copy of {actual_disabled_users_in_neo4j:,} disabled users in {time.time() - start:.2f} seconds")
            else:
                print(f"No changes in disabled user count detected ({actual_disabled_users_in_neo4j:,} disabled users)")


@background(schedule=5)
def sync_bh_owned():
    from pathlib import Path
    
    bh_servers = BloodhoundServer.objects.filter(active=True).all()
    if not bh_servers.exists():
        return
    
    ops_data_dir = getattr(settings, 'OPS_DATA_DIR', None)
    if not ops_data_dir:
        logger.warning("OPS_DATA_DIR not set, skipping sync_bh_owned")
        return
    
    ops_data_dir = Path(ops_data_dir)
    if not ops_data_dir.exists():
        logger.warning(f"OPS_DATA_DIR {ops_data_dir} does not exist, skipping sync_bh_owned")
        return
    
    # Collect all source hosts, users, and credentials from all operation databases
    all_source_hosts = set()
    all_source_users = set()
    all_credentials = set()
    
    op_dbs = list(ops_data_dir.glob('*.sqlite3'))
    for op_db in op_dbs:
        # Skip placeholder database
        if op_db.name == '_placeholder_op.sqlite3':
            continue
            
        # Update the active_op_db connection to point to this operation database
        connections.close_all()
        settings.DATABASES['active_op_db']['NAME'] = str(op_db)
        if hasattr(connections['active_op_db'], 'settings_dict'):
            connections['active_op_db'].settings_dict['NAME'] = str(op_db)
        connections['active_op_db'].connection = None
        
        try:
            # Check if database is ready before processing
            with connections['active_op_db'].cursor() as cursor:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_migrations'")
                if not cursor.fetchone():
                    logger.debug(f"Skipping {op_db.name} - migrations not complete yet")
                    continue
                
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_tracker_credential'")
                if not cursor.fetchone():
                    logger.debug(f"Skipping {op_db.name} - credential table not created yet")
                    continue
            
            # Mark source host as owned if we are running things in a system context
            source_hosts = (Context.objects.using('active_op_db')
                           .filter(id__in=Event.objects.using('active_op_db').all().values_list("source", flat=True))
                           .filter(user__iexact="system").values_list('host', flat=True).distinct())
            all_source_hosts.update(source_hosts)
            
            # Mark source user as owned if we are running things in that user's context
            source_users = (Context.objects.using('active_op_db')
                           .filter(id__in=Event.objects.using('active_op_db').all().values_list("source", flat=True))
                           .exclude(user__iexact="system").values_list('user', flat=True).distinct())
            all_source_users.update(source_users)
            
            # Collect credentials with plain-text secrets from this operation database
            credentials = (Credential.objects.using('active_op_db')
                          .filter(secret__isnull=False)
                          .annotate(bhname=Upper(Concat('account', Value('@'), 'system')))
                          .values_list('bhname', flat=True)
                          .distinct())
            all_credentials.update(credentials)
        except OperationalError as e:
            # Database might not be ready yet or is being migrated
            logger.debug(f"Skipping operation database {op_db.name} - not ready: {e}")
            continue
        except Exception as e:
            logger.warning(f"Error querying operation database {op_db.name}: {e}")
            continue
    
    # Update BloodHound with collected data
    for bloodhound_server in bh_servers:
        if all_source_hosts:
            # Process in chunks of 1000
            source_hosts_list = list(all_source_hosts)
            for i in range(0, len(source_hosts_list), 1000):
                source_hosts_page = source_hosts_list[i:i+1000]
                update_owned_hosts(bloodhound_server, source_hosts_page)
        
        if all_source_users:
            # Process in chunks of 1000
            source_users_list = list(all_source_users)
            for i in range(0, len(source_users_list), 1000):
                source_users_page = source_users_list[i:i+1000]
                update_owned_users(bloodhound_server, source_users_page)
        
        # Only mark as owned if we have a plain-text secret in the credential, not just a hash
        if all_credentials:
            credentials_list = list(all_credentials)
            for i in range(0, len(credentials_list), 1000):
                credentials_page = credentials_list[i:i+1000]
                update_owned_credentials(bloodhound_server, list(credentials_page))


def update_owned_credentials(bloodhound_server, credentials):
    driver = get_driver_for(bloodhound_server)
    if driver:
        with driver.session() as session:
            session.write_transaction(set_owned_bloodhound_users_with_domain, credentials)


def update_owned_hosts(bloodhound_server, hosts):
    driver = get_driver_for(bloodhound_server)
    if driver:
        with driver.session() as session:
            session.write_transaction(set_owned_bloodhound_hosts_without_domain, hosts)


def update_owned_users(bloodhound_server, users):
    driver = get_driver_for(bloodhound_server)
    if driver:
        with driver.session() as session:
            session.write_transaction(set_owned_bloodhound_users_without_domain, users)


def set_owned_bloodhound_users_with_domain(tx, users: list[str]):
    """
    Bulk mark of Bloodhound users as owned.

    :param users List of uppercase UPNs to mark as owned, e.g. ['USER1@MY.DOMAIN.LOCAL', 'USER2@MY.DOMAIN.LOCAL']
    """
    if not users:
        return

    print(f"Marking {len(users)} users as owned")

    return tx.run(
        f'''unwind $users as ownedUser
        match (u) where (u:User or u:AZUser) and u.name = ownedUser and u.owned = False
        set u.owned=True, u.notes="Marked as Owned by Stepping Stones at {datetime.now():%Y-%m-%d %H:%M:%S%z}"''',
        users=users)


def set_owned_bloodhound_hosts_without_domain(tx, hosts):
    """
    Bulk mark of Bloodhound hosts as owned, regardless of the domain they are on.

    :param hosts List of computer names (case-insensitive) to mark as owned, e.g. ['host1', 'host2']
    """
    if not hosts:
        return

    print(f"Marking {len(hosts)} hosts as owned ignoring domain")

    return tx.run(
        f'''unwind $hosts as ownedHost
        match (n) where (n:Computer or n:AZDevice) and toLower(split(split(n.name, "@")[1], ".")[0]) = toLower(ownedHost) and n.owned=False 
        set n.owned=True, n.notes="Marked as Owned by Stepping Stones at {datetime.now():%Y-%m-%d %H:%M:%S%z}"''',
        hosts=hosts)


def set_owned_bloodhound_users_without_domain(tx, users):
    """
    Bulk mark of Bloodhound users as owned, regardless of the domain they are on.

    :param users List of usernames (case-insensitive) to mark as owned, e.g. ['user1', 'user2']
    """
    if not users:
        return

    print(f"Marking {len(users)} users as owned ignoring domain")

    return tx.run(
        f'''unwind $users as ownedUser
        match (n) where (n:User or n:AZUser) and toLower(split(n.name, "@")[0]) = toLower(ownedUser) and n.owned=False 
        set n.owned=True, n.notes="Marked as Owned by Stepping Stones at {datetime.now():%Y-%m-%d %H:%M:%S%z}"''',
        users=users)


def get_current_active_operation():
    """Get the current active operation, first from the CurrentOperation model."""
    # First try to get it from the CurrentOperation model
    current_op = CurrentOperation.get_current()
    if current_op:
        return current_op
    
    # Don't log warning - this is expected during imports and other operations
    return None

