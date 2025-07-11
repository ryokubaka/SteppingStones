import logging
import re
from typing import Optional

import neo4j
import requests
from background_task import background
from django.db.models.signals import post_save
from django.dispatch import receiver
from neo4j import GraphDatabase, Driver
from django.contrib.auth import get_user_model
from django.db import connections
from django.core.management import call_command
from django.conf import settings

import cobalt_strike_monitor.models
from cobalt_strike_monitor.models import Listener, Beacon, BeaconLog
from cobalt_strike_monitor.poll_team_server import recent_checkin
from event_tracker.cred_extractor.extractor import extract_and_save
from event_tracker.models import Context, Credential, File, HashCatMode, Webhook
from event_tracker.utils import split_path

# Configure logger
logger = logging.getLogger(__name__)

User = get_user_model()

@receiver(post_save, sender=Listener)
def cs_listener_to_context(sender, instance: Listener, **kwargs):
    created = False
    context = None

    if instance.althost:
        context, created = Context.objects.get_or_create(host=instance.althost, user="", process="")
    elif instance.host:
        context, created = Context.objects.get_or_create(host=instance.host, user="", process="")
    # else, may be an SMB listener - do nothing

    if created:
        context.save()

    return context


@receiver(post_save, sender=Beacon)
def cs_beacon_to_context(sender, instance: Beacon, **kwargs):
    context, created = Context.objects.get_or_create(process=f"{instance.process.lower()} (PID: {instance.pid})",
                                                     user=instance.user_human,
                                                     host=instance.computer)
    if created:
        context.save()

    return context


@receiver(post_save, sender=cobalt_strike_monitor.models.Credential)
def cs_credential_listener(sender, instance: cobalt_strike_monitor.models.Credential, **kwargs):
    if len(instance.password) > 50 or re.fullmatch("[0-9a-f]{16,}", instance.password, flags=re.IGNORECASE):
        # The "password" in CS looks like a hash

        if len(instance.password) == 32:
            # Looks like a NTLM hash
            hash_type = HashCatMode.NTLM
            credential, created = Credential.objects.get_or_create(
                system=instance.realm,
                account=instance.user,
                hash=instance.password,
                hash_type=hash_type,
                purpose="Windows Login"
            )
        else:
            # Couldn't determine hash type
            credential, created = Credential.objects.get_or_create(
                system=instance.realm,
                account=instance.user,
                hash=instance.password
            )
    else:
        # The "password" in CS is probably truly a password
        credential, created = Credential.objects.get_or_create(
            system=instance.realm,
            account=instance.user,
            secret=instance.password
        )

    if created:
        credential.source = f"{instance.source} {instance.host}"
        credential.source_time = instance.added
        credential.save()

    return credential


def cs_indicator_archive_to_file(log_data):
    md5_hash, size, path = re.match(r"file: ([a-f0-9]{32}) (\d+) bytes (.*)", log_data).groups()
    directory, sep, filename = split_path(path)
    file, created = File.objects.get_or_create(size=size,
                                               md5_hash=md5_hash,
                                               filename=filename)
    if created:
        file.save()

    return file, directory


@receiver(post_save, sender=BeaconLog)
def cs_beaconlog_parser(sender, instance: BeaconLog, **kwargs):
    if instance.data.startswith("file: "):
        cs_indicator_archive_to_file(instance.data)
    elif instance.type == "output":
        message = instance.data
        extract_creds(message, default_system=instance.beacon.computer)


def extract_creds(input_text: str, default_system: str):
    # Remove CS timestamps
    input_text = re.sub(r'\r?\n\[\d\d\/\d\d \d\d:\d\d:\d\d] \[\+] ', '', input_text)
    # Remove inline execute assembly output noise
    input_text = re.sub(r'received output:\r?\n', '', input_text)

    return extract_and_save(input_text, default_system)


@receiver(post_save, sender=Beacon)
def notify_webhooks_new_beacon(sender, instance: Beacon, **kwargs):
    # Only fire webhooks if the beacon passes exclusion rules
    if Beacon.visible_beacons().filter(id=instance.id).exists():
        if Beacon.objects.filter(user=instance.user, host=instance.host, process=instance.process)\
                .exclude(id=instance.id).exists():
            # We've already seen a beacon for this user, host & process combo:
            for webhook in Webhook.objects.all():
                notify_webhook(webhook.url,
                               "respawned beacon",
                               f"Respawned beacon for {instance} received on {instance.team_server.description}")
        else:
            # This is a new beacon:
            for webhook in Webhook.objects.all():
                notify_webhook_new_beacon(webhook, instance)


def notify_webhook_new_beacon(webhook, beacon: Beacon):
    """
    Used for new beacons and the test notification process, hence a function in its own right.
    """
    notify_webhook(webhook.url,
                   "new beacon",
                   f"New beacon for {beacon} received on {beacon.team_server.description}")


@background(schedule=0)
def notify_webhook(url, type, message):
    requests.post(url=url, json={
        "type": type,
        "message": message
    })


@receiver(recent_checkin)
def checkin_handler(sender, beacon, metadata, **kwargs):
    if beacon.beaconreconnectionwatcher_set.exists():
        for webhook in Webhook.objects.all():
            notify_webhook(webhook.url,
                           "returned beacon",
                           f"Beacon returned: {beacon} on {beacon.team_server.description}")

        # Now we've spawned off some notification tasks, remove the DB entry
        beacon.beaconreconnectionwatcher_set.all().delete()


neo4j_driver_dict = dict()

def get_driver_for(bloodhound_server) -> Optional[Driver]:
    if not bloodhound_server.active:
        if bloodhound_server in neo4j_driver_dict:
            del neo4j_driver_dict[bloodhound_server]
        return None

    if bloodhound_server not in neo4j_driver_dict:
        driver = GraphDatabase.driver(bloodhound_server.neo4j_connection_url,
                                      auth=(bloodhound_server.username, bloodhound_server.password),
                                      connection_acquisition_timeout=2, connection_timeout=2,
                                      max_transaction_retry_time=2, resolver=custom_resolver)
        try:
            driver.verify_connectivity()
        except:
            # Dirty hack to turn off cert validation, required for Ubuntu client for unknown reason
            logging.warning("Falling back to unverified SSL connections to neo4j")
            driver = GraphDatabase.driver(bloodhound_server.neo4j_connection_url.replace("+s://", "+ssc://"),
                                          auth=(bloodhound_server.username, bloodhound_server.password),
                                          connection_acquisition_timeout=2, connection_timeout=2,
                                          max_transaction_retry_time=2, resolver=custom_resolver)

        neo4j_driver_dict[bloodhound_server] = driver

    candidate = neo4j_driver_dict[bloodhound_server]

    try:
        # Ensure the pool connection is still valid
        candidate.verify_connectivity()
        return candidate
    except Exception:
        del neo4j_driver_dict[bloodhound_server]
        return None


def custom_resolver(socket_address):
    # Quickly resolve localhost to avoid timeouts caused by slow DNS failures
    if socket_address[0] == "localhost":
        yield neo4j.Address(("127.0.0.1", socket_address[1]))
    else:
        yield neo4j.Address.parse(format(socket_address))


@receiver(post_save, sender=User)
def sync_user_to_operation_db(sender, instance, created, **kwargs):
    """
    Signal handler to sync user changes to all operation databases.
    This ensures that any user created or modified in the default database
    is also reflected in all operation databases.
    """
    try:
        # Get all operation databases
        ops_data_dir = settings.OPS_DATA_DIR
        if not ops_data_dir.exists():
            return

        # Get all .sqlite3 files in the ops-data directory
        op_dbs = list(ops_data_dir.glob('*.sqlite3'))
        logger.info(f"Found {len(op_dbs)} operation databases to sync users to")

        for db_path in op_dbs:
            try:
                # Update the active_op_db settings for this database
                connections['active_op_db'].close()
                connections.databases['active_op_db']['NAME'] = str(db_path)
                connections['active_op_db'].connection = None

                # Check if the user exists in this operation database
                with connections['active_op_db'].cursor() as cursor:
                    cursor.execute("SELECT id FROM auth_user WHERE id = %s", [instance.id])
                    exists = cursor.fetchone() is not None

                if exists:
                    # Update existing user
                    with connections['active_op_db'].cursor() as cursor:
                        cursor.execute("""
                            UPDATE auth_user 
                            SET username = %s, password = %s, is_superuser = %s,
                                first_name = %s, last_name = %s, email = %s,
                                is_staff = %s, is_active = %s, date_joined = %s
                            WHERE id = %s
                        """, [
                            instance.username, instance.password, instance.is_superuser,
                            instance.first_name, instance.last_name, instance.email,
                            instance.is_staff, instance.is_active, instance.date_joined,
                            instance.id
                        ])
                        logger.info(f"Updated user {instance.username} in operation database {db_path}")
                else:
                    # Insert new user
                    with connections['active_op_db'].cursor() as cursor:
                        cursor.execute("""
                            INSERT INTO auth_user (id, username, password, is_superuser,
                                                first_name, last_name, email, is_staff,
                                                is_active, date_joined)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, [
                            instance.id, instance.username, instance.password,
                            instance.is_superuser, instance.first_name, instance.last_name,
                            instance.email, instance.is_staff, instance.is_active,
                            instance.date_joined
                        ])
                        logger.info(f"Inserted user {instance.username} into operation database {db_path}")

            except Exception as e:
                logger.error(f"Error syncing user to operation database {db_path}: {e}")

    except Exception as e:
        logger.error(f"Error in sync_user_to_operation_db: {e}")
        # Don't prevent the user from being saved in the default database
