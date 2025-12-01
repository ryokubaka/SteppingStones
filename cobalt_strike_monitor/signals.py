import re
from datetime import timedelta

from django.db.models import Q
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from cobalt_strike_monitor.models import TeamServer, BeaconPresence, BeaconLog, CSAction, Archive
from cobalt_strike_monitor.poll_team_server import TeamServerPoller, recent_checkin
from cobalt_strike_monitor.utils import get_current_active_operation
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=TeamServer)
def team_server_listener(sender, instance: TeamServer, **kwargs):
    """
    Handle TeamServer active status changes - start or stop pollers accordingly.
    """
    try:
        # Determine which operation this TeamServer belongs to
        # Check if instance was saved with .using() - Django sets instance._state.db
        using_db = getattr(instance._state, 'db', None)
        
        operation_name = None
        
        if using_db:
            # Extract operation name from database path
            from django.db import connections
            from django.conf import settings
            import os
            
            db_path = connections.databases.get(using_db, {}).get('NAME', '')
            if db_path and 'ops-data' in str(db_path):
                # Extract operation name from database path (e.g., /path/to/ops-data/operation_name.sqlite3)
                db_basename = os.path.basename(str(db_path))
                if db_basename.endswith('.sqlite3'):
                    operation_name = db_basename[:-8]  # Remove .sqlite3 extension
        
        # If we couldn't determine from _state.db, search all operations
        if not operation_name:
            from event_tracker.models import Operation
            from django.conf import settings
            from django.db import connections
            
            all_operations = Operation.objects.using('default').all()
            for operation in all_operations:
                db_path = settings.OPS_DATA_DIR / f"{operation.name}.sqlite3"
                if not db_path.exists():
                    continue
                
                # Create a temporary connection to check
                temp_conn_name = f'temp_signal_{operation.name}'
                try:
                    if temp_conn_name not in connections.databases:
                        connections.databases[temp_conn_name] = connections.databases['active_op_db'].copy()
                    connections.databases[temp_conn_name]['NAME'] = str(db_path)
                    # Close existing connection if it exists
                    if temp_conn_name in connections:
                        connections[temp_conn_name].close()
                        del connections[temp_conn_name]
                    
                    # Check if this server exists in this operation's database
                    # Django will create the connection automatically when we access it
                    if TeamServer.objects.using(temp_conn_name).filter(pk=instance.pk).exists():
                        operation_name = operation.name
                        break
                except:
                    pass
                finally:
                    if temp_conn_name in connections:
                        try:
                            connections[temp_conn_name].close()
                        except:
                            pass
                    if temp_conn_name in connections.databases:
                        del connections.databases[temp_conn_name]
        
        if not operation_name:
            logger.warning(f"Could not determine operation for TeamServer {instance.pk}, cannot start/stop poller")
            return
        
        # Start or stop poller based on active status
        poller = TeamServerPoller()
        if instance.active:
            logger.info(f"[SIGNAL] TeamServer {instance.pk} marked active in operation '{operation_name}', starting poller")
            poller.add(instance.pk, operation_name=operation_name)
        else:
            logger.info(f"[SIGNAL] TeamServer {instance.pk} marked inactive in operation '{operation_name}', stopping poller")
            poller.remove(instance.pk, operation_name=operation_name)
            
    except Exception as e:
        logger.error(f"[SIGNAL] Error handling TeamServer active status change: {e}", exc_info=True)


sleep_regex = re.compile(r"Tasked beacon to sleep for (?P<sleep>\d+)s(?: \((?P<jitter>\d+)% jitter\))?")
sleep_metadata_regex = re.compile(r"@\((?P<sleep>[-\d]+)L?, (?P<jitter>[-\d]+)L?, ([-\d]+)L?\)")


@receiver(recent_checkin)
def checkin_handler(sender, beacon, metadata, **kwargs):
    if "sleep" in metadata and metadata["sleep"]:  # Parse the new sleep metadata in CS 4.7
        for match in sleep_metadata_regex.finditer(metadata["sleep"]):
            sleep = int(match.group("sleep") or '0')
            jitter = int(match.group("jitter") or '0') / 100

            if sleep < 0 or jitter < 0:
                return  # This happens when a beacon is deemed to have gone away by CS, lets not overwrite our data
    else:  # Try and determine the sleep params from log entries
        # Relies on beacon logs being ingested before this signal fires
        last_acknowledged_sleep = BeaconLog.objects\
            .filter(beacon=beacon)\
            .filter(Q(data__startswith="Tasked beacon to sleep for ", type="task")
                    | Q(data="Tasked beacon to become interactive", type="task")
                    | Q(data__startswith="started SOCKS4a server on: ", type="output")
                    | Q(data__startswith="started SOCKS5 server on: ", type="output"))\
            .order_by("when").last()

        sleep = 0
        jitter = 0.0

        if not last_acknowledged_sleep:
            # New beacons will use the sleep params from the CS Profile, but we can't see those settings
            print(f"Can not find previous sleep command for {beacon}, assuming its interactive")
        else:
            print(f"{beacon.user} {last_acknowledged_sleep.data}")
            # This won't match if there's an explict interactive tasking or SOCKS start, but that's fine as interactive is
            # our default assumption
            for match in sleep_regex.finditer(last_acknowledged_sleep.data):
                sleep = int(match.group("sleep") or '0')
                jitter = int(match.group("jitter") or '0') / 100

    last_presence = beacon.beaconpresence_set.last()

    # The maximum amount of time between checkins we would expect based on the previously configured sleep params.
    if last_presence:
        max_sleep_fuzzy = last_presence.max_sleep + timedelta(seconds=60)  # Plus 60 seconds to allow for inherent jitter
    else:
        # If no prior config is found, set max_sleep_period to 0 to let the missing previous checkin result in a
        # new presence tracker.
        max_sleep_fuzzy = timedelta(seconds=60)

    # Update a presence tracker if it's recent (i.e. 2 * max_sleep_periods ago)
    active_presence = BeaconPresence.objects.filter(beacon=beacon,
                                                    last_checkin__gte=beacon.last
                                                                      - max_sleep_fuzzy
                                                                      - max_sleep_fuzzy).last()

    if active_presence:
        # This beacon has been active recently, extend its activity window upto now
        active_presence.last_checkin = beacon.last
        active_presence.save()

    if not active_presence or active_presence.sleep_seconds != sleep or active_presence.sleep_jitter != jitter:
        # Create a new presence tracker because there wasn't one, or sleep params have changed
        BeaconPresence(beacon=beacon,
                       first_checkin=beacon.last,
                       last_checkin=beacon.last,
                       sleep_seconds=sleep,
                       sleep_jitter=jitter).save()


@receiver(pre_save, sender=BeaconLog)
def beaconlog_action_correlator(sender, instance: BeaconLog, **kwargs):
    # We dump the beacon log before the archives, so use beacon logs to determine when to start new actions.

    # IMPORTANT:
    # Use the *_id field as the guard, not instance.cs_action. The related
    # object may not be loaded, so instance.cs_action can be falsy even when
    # cs_action_id is already set. If we don't check the ID directly, we end
    # up creating duplicate CSAction entries every time this BeaconLog is
    # saved again.
    if getattr(instance, "cs_action_id", None):
        # We have already processed / correlated this BeaconLog
        # logger.debug(f"[CORRELATOR] BeaconLog already has cs_action_id={instance.cs_action_id}, skipping")
        return
    
    # Determine which database to use - check if instance was saved with .using()
    # When using .create(using='active_op_db') or .save(using='active_op_db'),
    # Django sets instance._state.db before the signal fires
    using = getattr(instance._state, 'db', None)
    if using is None:
        # Fallback: cobalt_strike_monitor models should use active_op_db
        # since they're operation-specific
        using = 'active_op_db'
    
    # Additional safety check: if this is an existing instance (has pk), check the database
    # to see if it already has a cs_action_id set. This prevents duplicate actions when
    # the same BeaconLog is saved multiple times.
    if instance.pk:
        try:
            existing_log = BeaconLog.objects.using(using).get(pk=instance.pk)
            if existing_log.cs_action_id:
                # logger.info(f"[CORRELATOR] BeaconLog {instance.pk} already has cs_action_id={existing_log.cs_action_id} in DB, using it")
                instance.cs_action_id = existing_log.cs_action_id
                return
        except BeaconLog.DoesNotExist:
            pass  # New instance, continue with correlation

    # If task_id is present (Cobalt Strike 4.12+), use it *exclusively* for correlation.
    # This ensures that when task_ids exist, we don't fall back to timing-based guesses
    # which can mis-assign outputs when many commands are run in a single check-in.
    if instance.task_id:
        # CRITICAL: Always check for an existing CSAction with this task_id first,
        # regardless of log type. This prevents duplicate actions when the same task_id
        # appears in multiple logs (e.g., input + task, or duplicate processing).
        # We look for actions that have ANY BeaconLog with the same task_id.
        # logger.info(f"[CORRELATOR] Processing {instance.type} with task_id={instance.task_id} for beacon {instance.beacon.pk}")
        existing_action = CSAction.objects.using(using).filter(
            beacon__pk=instance.beacon.pk,
            beaconlog__task_id=instance.task_id
        ).distinct().first()
        
        if existing_action:
            # Found an existing action with the same task_id, associate this log with it
            # logger.info(f"[CORRELATOR] ✓ Found existing CSAction {existing_action.pk} for task_id={instance.task_id}, associating {instance.type}")
            instance.cs_action_id = existing_action.pk
        elif instance.type == "input" or instance.type == "task":
            # This is an input or task log with a task_id, and no existing action found.
            # Create a new action for it.
            # Note: The log will be saved with this action, so future output logs with the same
            # task_id will be able to find this action via the beaconlog__task_id query above
            # logger.info(f"[CORRELATOR] ✗ No existing CSAction found for task_id={instance.task_id}, creating new one for {instance.type}")
            new_action = CSAction(start=instance.when, beacon_id=instance.beacon.pk)
            # When commands are run in quick succession the output can get assigned to the wrong action. There are some
            # commands which we know won't product output, and therefore we can defend against this a bit
            if instance.data.startswith("sleep ") or instance.data.startswith("note ") or \
               instance.data.startswith("Tasked beacon to sleep ") or instance.data.startswith("Tasked beacon to become interactive"):
                new_action.accept_output = False
            new_action.save(using=using)
            instance.cs_action_id = new_action.pk
            # logger.info(f"[CORRELATOR] ✓ Created CSAction {new_action.pk} for task_id {instance.task_id} ({instance.type})")
        else:
            # This is an output / error / note / checkin with a task_id but no matching action yet.
            # We deliberately do NOT fall back to timing-based correlation here; if the task/input
            # log hasn't been seen yet, we prefer to leave this BeaconLog uncorrelated rather than
            # risk attaching it to the wrong CSAction.
            # logger.warning(
            #     f"[CORRELATOR] ✗ {instance.type} with task_id={instance.task_id} has no matching CSAction; "
            #     f"leaving cs_action unset (input/task log may not have been processed yet)"
            # )
            pass
    else:
        # No task_id present, use backwards-compatible timing-based correlation
        # If there's an input, it will always signify the start of a new action
        if instance.type == "input":
            new_action = CSAction(start=instance.when, beacon_id=instance.beacon.pk)

            # When commands are run in quick succession the output can get assigned to the wrong action. There are some
            # commands which we know won't product output, and therefore we can defend against this a bit
            if instance.data.startswith("sleep ") or instance.data.startswith("note "):
                new_action.accept_output = False

            new_action.save(using=using)
            instance.cs_action_id = new_action.pk

        # A task with no input log within the last second, relating to sleep, is also the start of a new action
        elif instance.type == "task" and "Tasked beacon to sleep " in instance.data and\
                not CSAction.objects.using(using).filter(beacon__pk=instance.beacon.pk, start__gte=instance.when - timedelta(seconds=1), start__lte=instance.when).exists():
            new_action = CSAction(start=instance.when, beacon_id=instance.beacon.pk)
            new_action.accept_output = False
            new_action.save(using=using)
            instance.cs_action_id = new_action.pk

        # For everything else, associate it with the most recent action on the beacon
        else:
            most_recent_action_query = CSAction.objects.using(using).filter(beacon__pk=instance.beacon.pk, start__lte=instance.when).order_by(
                "-start")
            if instance.type.startswith("output") or instance.type == "error":
                most_recent_action_query = most_recent_action_query.filter(accept_output=True)
            instance.cs_action_id = most_recent_action_query.values_list("id", flat=True).first()


@receiver(pre_save, sender=Archive)
def archive_action_correlator(sender, instance: Archive, **kwargs):
    if instance.beacon is None:
        # Can occur for webhits or notify types, nothing to do, so exit early
        return

    # Determine which database to use - check if instance was saved with .using()
    # When using .create(using='active_op_db') or .save(using='active_op_db'),
    # Django sets instance._state.db before the signal fires
    using = getattr(instance._state, 'db', None)
    if using is None:
        # Fallback: cobalt_strike_monitor models should use active_op_db
        # since they're operation-specific
        using = 'active_op_db'

    most_recent_action_id = CSAction.objects.using(using).filter(beacon__pk=instance.beacon.pk, start__lte=instance.when).order_by(
        "-start").values_list("id", flat=True).first()
    instance.cs_action_id = most_recent_action_id
