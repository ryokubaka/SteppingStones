import json
import os
import platform
import re
import socket
import subprocess
import traceback
from io import BufferedReader

import psutil
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time_ns

from background_task import background
from background_task.models import Task
from dateutil.tz import UTC
from django.db.models import Q
from django.dispatch import Signal
from django.template import Engine, Context
from django.utils import timezone
from datetime import timedelta
from django.db import OperationalError, connections

from cobalt_strike_monitor.models import TeamServer, Beacon, Archive, BeaconPresence, BeaconLog, CSAction, Listener, Credential, Download

from background_task.admin import TaskAdmin, CompletedTaskAdmin

from django.core.cache import cache  # We use the "default" cache for tracking team server enablement

import logging

from cobalt_strike_monitor.utils import get_current_active_operation

from event_tracker.models import CurrentOperation

# Configure logging
logger = logging.getLogger('cobalt_strike_monitor')

# Add a console handler if none exists
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(levelname)s] %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# Monkey Patch the background_task library so that the function containing kill(PID, 0),
# which kills the background task on Windows, isn't called by the admin UI
fields = ['task_name', 'task_params', 'run_at', 'priority', 'attempts', 'has_error', 'locked_by', ]
TaskAdmin.list_display = fields
CompletedTaskAdmin.list_display = fields


class TeamServerPoller:
    def __init__(self):
        self.database_alias = None  # Will be set in initialise()

    def initialise(self):
        logger.info("[WEB] Initializing TeamServerPoller...")
        
        # Get the current active operation first
        current_op = CurrentOperation.get_current()
        if not current_op:
            logger.warning("[WEB] No active operation found. Cannot initialize poller.")
            return

        logger.info(f"[WEB] Found active operation: {current_op.name}")
        
        # Always use 'active_op_db' as the database alias
        self.database_alias = 'active_op_db'
        
        # Update the database path to use the correct operation database
        from django.conf import settings
        db_path = settings.OPS_DATA_DIR / f"{current_op.name}.sqlite3"
        logger.info(f"[WEB] Setting database path to: {db_path}")
        
        # Update the database connection settings
        if 'active_op_db' in connections.databases:
            connections['active_op_db'].close()  # Close existing connection
        connections.databases['active_op_db']['NAME'] = str(db_path)
        if hasattr(connections['active_op_db'], 'settings_dict'):
            connections['active_op_db'].settings_dict['NAME'] = str(db_path)
        connections['active_op_db'].connection = None  # Force re-evaluation/reconnection
        
        logger.info(f"[WEB] Set database alias to: {self.database_alias}")
        logger.debug(f"[WEB] Database settings: {connections[self.database_alias].settings_dict}")

        # Ensure the database tables exist
        from django.core.management import call_command
        try:
            logger.info("[WEB] Running migrations for active_op_db...")
            call_command('migrate', 'cobalt_strike_monitor', database=self.database_alias)
            logger.info("[WEB] Migrations completed successfully")
        except Exception as e:
            logger.error(f"[WEB] Error running migrations: {e}")
            return

        # Check if the database exists and has the required tables
        try:
            with connections[self.database_alias].cursor() as cursor:
                # First check if the table exists
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cobalt_strike_monitor_teamserver'")
                if not cursor.fetchone():
                    logger.error(f"[WEB] Required table 'cobalt_strike_monitor_teamserver' not found in {self.database_alias}")
                    return
                logger.debug("[WEB] TeamServer table found in database")
                
                # Check the total number of servers in the table
                cursor.execute("SELECT COUNT(*) FROM cobalt_strike_monitor_teamserver")
                total_servers = cursor.fetchone()[0]
                logger.debug(f"[WEB] Total number of servers in database: {total_servers}")
                
                # List all servers and their active status
                cursor.execute("SELECT id, description, active FROM cobalt_strike_monitor_teamserver")
                servers = cursor.fetchall()
                logger.debug(f"[WEB] All servers in database: {servers}")
                
                # Check specifically for active servers
                cursor.execute("SELECT COUNT(*) FROM cobalt_strike_monitor_teamserver WHERE active = 1")
                active_count = cursor.fetchone()[0]
                logger.debug(f"[WEB] Number of active servers: {active_count}")
                
                if active_count == 0:
                    logger.warning("[WEB] No active servers found in database. Checking default database for comparison...")
                    # Check the default database for comparison
                    with connections['default'].cursor() as default_cursor:
                        default_cursor.execute("SELECT id, description, active FROM cobalt_strike_monitor_teamserver")
                        default_servers = default_cursor.fetchall()
                        logger.debug(f"[WEB] Servers in default database: {default_servers}")
        except Exception as e:
            logger.error(f"[WEB] Error checking database tables: {e}")
            return

        # Clear out any orphan tasks
        try:
            for task in Task.objects.all():
                if task.locked_by is not None and not psutil.pid_exists(int(task.locked_by)):
                    task.delete()
        except OperationalError:
            logger.error("[WEB] Task table (default DB) not accessible during initialise. Skipping orphan task cleanup.")

        # Spawn some new tasks
        try:
            # Get all active team servers
            servers = TeamServer.objects.using(self.database_alias).filter(active=True).all()
            logger.info(f"[WEB] Found {servers.count()} active team servers")
            
            # Log details about each server found
            for server in servers:
                logger.debug(f"[WEB] Active server found: ID={server.id}, Description={server.description}, Active={server.active}")
                logger.info(f"[WEB] Adding task for TeamServer ID: {server.id}")
                self.add(server.id)
        except OperationalError as e:
            logger.error(f"[WEB] TeamServer table ({self.database_alias}) not accessible during initialise: {e}")
        except Exception as e:
            logger.error(f"[WEB] Unexpected error during initialise: {e}", exc_info=True)

    def get_current_active_operation(self):
        # Use the function from event_tracker.background_tasks
        current_op = get_current_active_operation()
        if current_op:
            # Create a simple object with the database_alias attribute
            class OpInfo:
                def __init__(self, name, database_alias):
                    self.name = name
                    self.database_alias = database_alias
                    
            # Handle both dict and Operation object
            if isinstance(current_op, dict):
                name = current_op.get('name')
                database_alias = 'active_op_db'  # Always use active_op_db
            elif hasattr(current_op, 'name'):
                name = current_op.name
                database_alias = 'active_op_db'  # Always use active_op_db
                logger.info(f"Using 'active_op_db' as database alias for operation '{name}'")
            else:
                logger.error("Unknown current operation format")
                return None
                
            logger.info(f"Found active operation: {name} with database alias: {database_alias}")
            return OpInfo(name, database_alias)
            
        logger.warning("No active operation found")
        return None

    def add(self, serverid):
        # Always use 'active_op_db' as the database alias
        self.database_alias = 'active_op_db'
        logger.info(f"Using 'active_op_db' as database alias for server {serverid}")
            
        try:
            # Get the current active operation
            current_op = get_current_active_operation()
            if not current_op:
                logger.warning("No active operation found. Cannot add task.")
                return

            logger.info(f"Found active operation: {current_op.name}")
            
            # Use the correct database alias for all queries
            server = TeamServer.objects.using(self.database_alias).get(pk=serverid)
            logger.info(f"Retrieved TeamServer: {server.description} ({server.hostname}:{server.port})")
            
            # Check there's nothing already scheduled for this server:
            if not Task.objects.filter(
                    task_name="cobalt_strike_monitor.poll_team_server.poll_teamserver",
                    task_params__startswith=f"[[{serverid}], ").exists():
                logger.info(f"No existing task found for server {serverid}, scheduling new task")
                poll_teamserver(serverid, schedule=timezone.now())
            else:
                logger.info(f"Task already exists for server {serverid}")
        except OperationalError as e:
            logger.error(f"Database error when adding task for server {serverid}: {e}")
        except Exception as e:
            logger.error(f"Error adding task for server {serverid}: {e}", exc_info=True)


def healthcheck_teamserver(serverid):
    # Always use 'active_op_db' as the database alias
    database_alias = 'active_op_db'
    logger.info(f"Using database alias: {database_alias} for server {serverid}")
    
    try:
        server = TeamServer.objects.using(database_alias).get(pk=serverid)
        logger.info(f"Retrieved TeamServer: {server.description} ({server.hostname}:{server.port})")
    except TeamServer.DoesNotExist:
        logger.error(f"TeamServer with ID {serverid} not found in database {database_alias}")
        return None, "TeamServer not found", None, False
    except Exception as e:
        logger.error(f"Error retrieving TeamServer {serverid}: {e}")
        return None, f"Error retrieving TeamServer: {e}", None, False

    tcp_error = None
    aggressor_output = None

    try:
        with socket.socket() as sock:
            sock.connect((server.hostname, server.port))
    except Exception as e:
        tcp_error = e

    if not tcp_error:
        with NamedTemporaryFile(mode="w", delete=False) as tempfile:
            tempfile.write("""
println("Connected OK. Synchronizing...");
            
on ready {
   println("Synchronized OK.");
   closeClient();
}""")
            tempfile.close()
            jar_path = _get_jar_path()
            try:
                p = subprocess.Popen(["java",
                                      "-XX:ParallelGCThreads=4",
                                      "-XX:+AggressiveHeap",
                                      "-XX:+UseParallelGC",
                                      "-Xmx128M",
                                      "-classpath",
                                      str(jar_path),
                                      "aggressor.headless.Start",
                                      server.hostname,
                                      str(server.port),
                                      f"ssbot{int(time_ns() / 1_000_000_000)}",
                                      server.password,
                                      tempfile.name],
                                     cwd=str(jar_path.parent),
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
                aggressor_output = p.stdout.read().decode("unicode_escape")
            except FileNotFoundError as e:
                aggressor_output = f"Java Virtual Machine not found in $PATH"
            except NotADirectoryError as e:
                aggressor_output = f"No such JAR directory: {jar_path.parent}"
            finally:
                os.unlink(tempfile.name)

            if "Could not find or load main class aggressor.headless.Start" in aggressor_output:
                aggressor_output += "\nTry (re-)running Cobalt Strike's update script"
    
    in_container = os.path.exists("/.dockerenv")
    if not in_container:
        try:
            p = subprocess.Popen(
                ["systemctl", "status", "ssbot"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            ssbot_status = p.stdout.read().decode("unicode_escape")
        except FileNotFoundError:
            ssbot_status = None
    else:
        try:
            # Check if 'process_tasks' is running
            p = subprocess.Popen(
                ["pgrep", "-f", "manage.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            output, _ = p.communicate()

            if output.strip():
                ssbot_status = "Confirm that process_tasks is active (running) via Container"
            else:
                ssbot_status = None
        except Exception as e:
            logger.error(f"Error checking ssbot status: {e}")
            return tcp_error, aggressor_output, None, False

    found_jvm = False
    for p in psutil.process_iter(["cmdline"]):
        if p.info['cmdline'] and \
                "java" in p.info['cmdline'][0] and \
                len(p.info['cmdline']) > 11 and server.password == p.info['cmdline'][11]:
            found_jvm = True

    return tcp_error, aggressor_output, ssbot_status, found_jvm

@background(schedule=5)
def poll_teamserver(server_id):
    """
    Polls a team server for data.
    """
    from event_tracker.models import CurrentOperation
    from django.conf import settings
    from django.db import connections
    import os

    # Check if this task is already running
    task_key = f'poll_teamserver_running_{server_id}'
    if cache.get(task_key):
        logger.debug(f"Task for server {server_id} is already running, skipping")
        return
    cache.set(task_key, True, 60)  # Set a 60-second lock

    try:
        logger.debug(f"[TASKS] Starting poll_teamserver task for server_id={server_id}")

        # Always fetch the current operation from the database (CurrentOperation model)
        current_op = CurrentOperation.get_current()
        if not current_op:
            logger.error("[TASKS] No active operation found in CurrentOperation model")
            return

        logger.debug(f"[TASKS] Found active operation: {current_op.name}")

        # Set the database path BEFORE any database operations
        db_path = settings.OPS_DATA_DIR / f"{current_op.name}.sqlite3"
        logger.debug(f"[TASKS] Setting active_op_db to: {db_path}")

        # Update the database connection settings
        if 'active_op_db' in connections.databases:
            connections['active_op_db'].close()
        connections.databases['active_op_db']['NAME'] = str(db_path)
        if hasattr(connections['active_op_db'], 'settings_dict'):
            connections['active_op_db'].settings_dict['NAME'] = str(db_path)
        connections['active_op_db'].connection = None

        # Verify the database path was updated correctly
        actual_path = connections['active_op_db'].settings_dict.get('NAME')
        logger.debug(f"[TASKS] Verified active_op_db path: {actual_path}")

        if os.path.basename(str(actual_path)) == '_placeholder_op.sqlite3':
            logger.error(f"[TASKS] Still using placeholder DB path! Expected: {db_path}, Got: {actual_path}")
            return

        # Initialize server variable
        server = None

        try:
            # Check if the database exists and has the required table
            with connections['active_op_db'].cursor() as cursor:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cobalt_strike_monitor_teamserver'")
                if not cursor.fetchone():
                    logger.error(f"[TASKS] TeamServer table not found in active_op_db at {actual_path}")
                    return
                logger.debug("[TASKS] TeamServer table found in active_op_db")
                
                # Check if the server exists
                cursor.execute("SELECT id FROM cobalt_strike_monitor_teamserver WHERE id = %s", [server_id])
                if not cursor.fetchone():
                    logger.error(f"[TASKS] TeamServer with ID {server_id} not found in database {actual_path}")
                    # List all servers in the database for debugging
                    cursor.execute("SELECT id, description FROM cobalt_strike_monitor_teamserver")
                    servers = cursor.fetchall()
                    logger.debug(f"[TASKS] Available servers in database: {servers}")
                    return
                logger.debug(f"[TASKS] TeamServer with ID {server_id} found in database")
            
            # Get the server object
            server = TeamServer.objects.using('active_op_db').get(id=server_id)
            logger.debug(f"[TASKS] Retrieved TeamServer object: {server.description} (ID: {server.id})")
            
            # Check if the server is active
            if not server.active:
                logger.info(f"[TASKS] Server {server.description} is not active, skipping poll")
                return
            
            # Poll the server
            logger.info(f"[TASKS] Polling server {server.description} at {server.hostname}:{server.port}")
            
            try:
                logger.debug("[TASKS] Creating temporary file for Aggressor script")
                
                # Get the last timestamps for each type of data
                if server.beacon_set.exists():
                    last_session_timestamp = server.beacon_set.latest("opened").opened.timestamp() * 1000
                else:
                    last_session_timestamp = 0

                if server.archive_set.exists():
                    last_archive_timestamp = server.archive_set.latest("when").when.timestamp() * 1000
                else:
                    last_archive_timestamp = 0

                if server.beaconlog_set.exists():
                    last_beaconlog_timestamp = server.beaconlog_set.latest("when").when.timestamp() * 1000
                else:
                    last_beaconlog_timestamp = 0

                if server.credential_set.exists():
                    last_credential_timestamp = server.credential_set.latest("added").added.timestamp() * 1000
                else:
                    last_credential_timestamp = 0

                if server.download_set.exists():
                    last_download_timestamp = server.download_set.latest("date").date.timestamp() * 1000
                else:
                    last_download_timestamp = 0

                # Create a temporary file for the Aggressor script
                with NamedTemporaryFile(mode="w", delete=False) as tempfile:
                    template = Engine.get_default().get_template("dump.cna")
                    context = Context({
                        "last_session_timestamp": last_session_timestamp,
                        "last_archive_timestamp": last_archive_timestamp,
                        "last_beaconlog_timestamp": last_beaconlog_timestamp,
                        "last_credential_timestamp": last_credential_timestamp,
                        "last_download_timestamp": last_download_timestamp
                    })
                    tempfile.write(template.render(context))
                    tempfile.close()
                    logger.debug(f"[TASKS] Created temporary file: {tempfile.name}")
                    
                    jar_path = _get_jar_path()
                    logger.debug(f"[TASKS] Using JAR path: {jar_path}")
                    
                    if not jar_path.exists():
                        logger.error(f"[TASKS] JAR file not found at path: {jar_path}")
                        return
                    
                    logger.debug("[TASKS] Starting Aggressor process")
                    # Start the Aggressor process
                    p = subprocess.Popen(
                        [
                            "java",
                            "-XX:ParallelGCThreads=4",
                            "-XX:+AggressiveHeap",
                            "-XX:+UseParallelGC",
                            "-Xmx128M",
                            "-classpath",
                            str(jar_path),
                            "aggressor.headless.Start",
                            server.hostname,
                            str(server.port),
                            f"ssbot{int(time_ns() / 1_000_000_000)}",
                            server.password,
                            tempfile.name
                        ],
                        cwd=str(jar_path.parent),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT
                    )
                    
                    logger.debug(f"[TASKS] Started Aggressor process with PID: {p.pid}")
                    
                    # Parse the output
                    logger.debug("[TASKS] Starting to parse Aggressor output")
                    parse(p, server)
                    logger.debug("[TASKS] Finished parsing Aggressor output")
                    
            except FileNotFoundError as e:
                logger.error(f"[TASKS] Java Virtual Machine not found in $PATH: {e}")
            except NotADirectoryError as e:
                logger.error(f"[TASKS] No such JAR directory: {jar_path.parent}: {e}")
            except Exception as e:
                logger.error(f"[TASKS] Error during polling: {e}", exc_info=True)
                logger.error(f"[TASKS] Error type: {type(e).__name__}")
                logger.error(f"[TASKS] Error details: {str(e)}")
            finally:
                # Clean up the temporary file
                try:
                    if 'tempfile' in locals():
                        logger.debug(f"[TASKS] Cleaning up temporary file: {tempfile.name}")
                        os.unlink(tempfile.name)
                except Exception as e:
                    logger.error(f"[TASKS] Error cleaning up temporary file: {e}")
                
                # Ensure the process is terminated
                try:
                    if 'p' in locals() and p.poll() is None:  # Process is still running
                        logger.debug(f"[TASKS] Terminating Aggressor process with PID: {p.pid}")
                        p.terminate()
                        p.wait(timeout=5)  # Wait up to 5 seconds for process to terminate
                except Exception as e:
                    logger.error(f"[TASKS] Error terminating Aggressor process: {e}")
                    try:
                        if 'p' in locals():
                            logger.debug(f"[TASKS] Force killing Aggressor process with PID: {p.pid}")
                            p.kill()  # Force kill if terminate doesn't work
                    except:
                        pass
        except TeamServer.DoesNotExist:
            logger.error(f"[TASKS] TeamServer with ID {server_id} not found in database {actual_path}")
            # Try to list all servers in the database for debugging
            try:
                servers = TeamServer.objects.using('active_op_db').all()
                logger.debug(f"[TASKS] Available servers in database: {[(s.id, s.description) for s in servers]}")
            except Exception as e:
                logger.error(f"[TASKS] Error listing servers: {e}")
            return
        except OperationalError as e:
            logger.error(f"[TASKS] Database error while polling server {server_id}: {e}")
            return
        except Exception as e:
            logger.error(f"[TASKS] Error polling server {server_id}: {e}", exc_info=True)
            return
    finally:
        # Release the lock
        cache.delete(task_key)


def _get_jar_path():
    if platform.system() == "Windows":
        jar_path = Path(r"C:\Tools\cobaltstrike\cobaltstrike.jar")
    else:
        jar_path = Path(r"/opt/cobaltstrike/cobaltstrike.jar")
    cs46_jar_path = jar_path.parent / "cobaltstrike-client.jar"
    if cs46_jar_path.exists():
        jar_path = cs46_jar_path
    cs49_jar_path = jar_path.parent / "client" / "cobaltstrike-client.jar"
    if cs49_jar_path.exists():
        jar_path = cs49_jar_path
    return jar_path


# A signal which will fire no more than once a minute when a beacon checks in
recent_checkin = Signal()

def parse_line(line):
    """
    Returns tuple of ID and parsed representation of data
    """
    line_parts = re.search(r"^\[.\] \[([^\]]+)\] (.*)$", line)
    if not line_parts:
        logger.debug(f"Could not parse: {line}")
        return None, None
    return line_parts.group(1), json.loads(line_parts.group(2))

def parse(p, server):
    try:
        reader = BufferedReader(p.stdout)
        logger.debug(f"[PARSER] Starting to parse output for server: {server.description}")

        pending_beacon_log = None

        for line in iter(reader.readline, b''):
            # We don't need to check the TS is enabled every line, but should do so every few seconds
            # So use Django's caching function...
            ts_state = cache.get(f"TS_STATE_{server.pk}")
            if ts_state is None:  # i.e. never cached, or expired
                server.refresh_from_db(fields=['active'])
                ts_state = server.active
                cache.set(f"TS_STATE_{server.pk}", ts_state, 5)  # Cache this for 5 seconds

            # Check if we're still processing output from this TS
            if not ts_state:
                logger.info(f"[PARSER] Server {server.description} marked inactive - exiting")
                p.kill()
                return

            line = line.decode("ascii").rstrip()
            logger.debug(f"[PARSER] Processing line: {line}")

            # Skip connection status messages
            if any(msg in line for msg in [
                "Loading Windows error codes",
                "Windows error codes loaded",
                "Connected OK",
                "Synchronizing",
                "Synchronized OK",
                "shutting down client",
                "Disconnected from team server"
            ]):
                continue

            try:
                line_id, line_data = parse_line(line)
                if not line_id:  # Skip if no valid data was parsed
                    continue

                logger.debug(f"[PARSER] Successfully parsed line with ID: {line_id}")

                # First, lets flush the pending Beacon Log if we've moved onto a processing different type of line:
                if not line.startswith("[B]"):
                    if pending_beacon_log:
                        logger.debug("[PARSER] Flushing pending beacon log")
                        # Our regexes rely on a \n to find ends of passwords etc, so ensure there's always 1
                        pending_beacon_log.data = pending_beacon_log.data.rstrip("\n") + "\n"
                        pending_beacon_log.save()
                        pending_beacon_log = None

                # Now lets process the current line
                if line.startswith("[L]"):  # Listeners
                    logger.debug("[PARSER] Processing Listener data")
                    # TCP Listeners can be configured to only bind to localhost
                    if "localonly" in line_data:
                        line_data["localonly"] = (line_data["localonly"] == "true")
                    listener = Listener(**dict(filter(
                        lambda elem: elem[0] in ["name", "proxy", "payload", "port", "profile", "host",
                                                 "althost", "strategy", "beacons", "bindto", "status", "maxretry",
                                                 "guards", "localonly"],
                        line_data.items())))
                    listener.team_server = server
                    listener.save()
                    logger.debug(f"[PARSER] Saved Listener: {listener.name}")
                elif line.startswith("[M]"):  # Beacon Metadata
                    logger.debug("[PARSER] Processing Beacon Metadata")
                    delta = timedelta(milliseconds=int(line_data["last"]))
                    approx_last_seen = (datetime.now(tz=UTC)-delta)  # Only approx, as time has passed since
                                                                              # the "X milliseconds ago" figure was
                                                                              # generated
                    # Only update beacons if their last seen was over a minute since the current value to reduce DB load
                    # and compensate for constantly changing values due to the approximation error
                    beacons_to_update = Beacon.objects.filter(Q(pk=int(line_id)),
                                                   Q(last__lte=approx_last_seen-timedelta(minutes=1)) | Q(last__isnull=True))

                    # Sanity check that it's worth locking the DB for
                    if beacons_to_update.exists():
                        update_count = beacons_to_update.update(last=approx_last_seen)
                        logger.debug(f"[PARSER] Updated {update_count} beacons with new last seen time")

                        if update_count > 0 and delta < timedelta(minutes=1):
                            beacon_to_update = Beacon.objects.get(pk=int(line_id))
                            recent_checkin.send_robust(sender="ssbot", beacon=beacon_to_update, metadata=line_data)
                            logger.debug(f"[PARSER] Sent recent checkin signal for beacon {beacon_to_update.id}")

                elif line.startswith("[S]"):  # Beacon sessions
                    logger.debug("[PARSER] Processing Beacon Session")
                    beacon = Beacon(**dict(filter(
                        lambda elem: elem[0] in ["id", "note", "charset", "internal", "external", "computer",
                                                 "host", "process", "pid", "barch", "os", "ver", "build", "arch",
                                                 "user",  "session"],
                        line_data.items())))
                    beacon.is64 = (line_data["is64"] == "1")
                    beacon.opened = datetime.fromtimestamp(int(line_data["opened"]) / 1000, tz=UTC)
                    if "pbid" in line_data and line_data["pbid"] != "":
                        beacon.parent_beacon = get_beacon_for_bid(line_data["pbid"], server)
                        if beacon.session == "beacon":  # SSH sessions also have a pbid, so ensure it's a beacon-beacon connection
                            # A bit of an assumption that the SMB listener in play is the first one configured, but we
                            # don't have anything else to go on.
                            beacon.listener = Listener.objects.filter(team_server=server, payload="windows/beacon_bind_pipe").first()
                    else:
                        beacon.listener = Listener.objects.get(name=line_data["listener"], team_server=server)
                    beacon.team_server = server
                    beacon.save()
                    logger.debug(f"[PARSER] Saved Beacon Session: {beacon.id}")
                elif line.startswith("[A]"):  # Archives
                    logger.debug("[PARSER] Processing Archive")
                    temp_dict = dict()
                    temp_dict.update(line_data)
                    archive = Archive(**dict(filter(
                        lambda elem: elem[0] in ["data", "tactic"],
                        temp_dict.items())))
                    archive.type = clean_type(temp_dict["type"])
                    archive.when = datetime.fromtimestamp(int(temp_dict["when"].rstrip("L")) / 1000, tz=UTC)
                    if "bid" in temp_dict:
                        try:
                            archive.beacon = get_beacon_for_bid(temp_dict["bid"], server)
                        except Exception as e:
                            logger.warning(f"[PARSER] Could not associate archive with beacon {temp_dict['bid']}: {e}")
                    archive.team_server = server
                    archive.save()
                    logger.debug(f"[PARSER] Saved Archive: {archive.id}")
                elif line.startswith("[B]"):  # Beacon Logs
                    logger.debug("[PARSER] Processing Beacon Log")
                    # Example:
                    # [B] [1263] {"data":"received output:\n[+] roborg Runtime Initalized, assembly size 488960, .NET Runtime Version: 4.0.30319.42000 in AppDomain qiBsaBzIc\r\n", "type":"beacon_output", "bid":"270632664", "when":"1741168779890"}
                    # To avoid hammering the DB and rerunning lots of regexes, we first try and buffer sequential output
                    # logs in memory, but this only works for those logs read in a single non-blocking read, so
                    # eventually we also have to attempt a DB level merge too.
                    beacon_log = BeaconLog(**dict(filter(
                        lambda elem: elem[0] in ["data", "operator", "output_job"],
                        line_data.items())))

                    beacon_log.type = clean_type(line_data["type"])
                    try:
                        beacon_log.beacon = get_beacon_for_bid(line_data["bid"], server)
                    except Exception as e:
                        logger.warning(f"[PARSER] Could not associate beacon log with beacon {line_data['bid']}: {e}")
                        continue
                    beacon_log.team_server = server

                    # Work back from the end of line_data, as there may (or may not) be an operator element in the
                    # middle which messes up later offsets
                    beacon_log.when = datetime.fromtimestamp(int(line_data["when"]) / 1000, tz=UTC)

                    if "data" in line_data:
                        # Trim prefix added by NCC custom tooling
                        if beacon_log.data.startswith("received output:"):
                            beacon_log.data = beacon_log.data[17:]

                    # Beacon Logs output types are special in that we try and merge adjacent output lines into a single DB row.
                    # This is done by storing the DB row in a "pending" variable which is either appended to, or
                    # flushed if appending doesn't make sense because the current and pending lines aren't related.
                    # Lines will not be merged if there is too much of a time difference between each line, or if other
                    # concurrent events are occurring on the team server.
                    # So there remains the need to do additional processing to collate the output/errors associated with an input/task.
                    if pending_beacon_log:
                        # Does current entry fit with pending beacon log?
                        if beacon_log.type == pending_beacon_log.type and \
                                beacon_log.output_job == pending_beacon_log.output_job and \
                                beacon_log.beacon_id == pending_beacon_log.beacon_id and \
                                beacon_log.team_server_id == pending_beacon_log.team_server_id and \
                                beacon_log.when - pending_beacon_log.when <= timedelta(milliseconds=15):
                            # Merge current with pending and discard current
                            logger.debug("[PARSER] Merging beacon log with pending log")
                            pending_beacon_log.data += beacon_log.data
                            pending_beacon_log.when = beacon_log.when # Update the time for use in subsequent time comparisons
                        else:
                            # Flush pending beacon log and save current one
                            logger.debug("[PARSER] Flushing pending beacon log and saving new log")
                            # Our regexes rely on a \n to find ends of passwords etc, so ensure there's always 1
                            pending_beacon_log.data = pending_beacon_log.data.rstrip("\n") + "\n"
                            pending_beacon_log.save()
                            pending_beacon_log = None
                            beacon_log.save()
                    else:
                        if beacon_log.type == "output":
                            logger.debug("[PARSER] Setting new pending beacon log")
                            pending_beacon_log = beacon_log
                        else:
                            # There's no pending beacon log, just save this non-output log straight to the DB
                            logger.debug("[PARSER] Saving non-output beacon log directly")
                            beacon_log.save()
                elif line.startswith("[C]"):  # Credentials
                    logger.debug("[PARSER] Processing Credential")
                    credential = Credential(**dict(filter(
                        lambda elem: elem[0] in ["user", "password", "host", "realm", "source"],
                        line_data.items())))
                    credential.added = datetime.fromtimestamp(int(line_data["added"]) / 1000, tz=UTC)
                    credential.team_server = server
                    credential.save()
                    logger.debug(f"[PARSER] Saved Credential: {credential.id}")
                elif line.startswith("[D]"):  # Downloads
                    logger.debug("[PARSER] Processing Download")
                    download = Download(**dict(filter(
                        lambda elem: elem[0] in ["size", "path", "name"],
                        line_data.items())))
                    download.date = datetime.fromtimestamp(int(line_data["date"]) / 1000, tz=UTC)
                    download.team_server = server
                    if "bid" in line_data and line_data["bid"]:
                        try:
                            download.beacon = get_beacon_for_bid(line_data["bid"], server)
                            download.save()
                            logger.debug(f"[PARSER] Saved Download: {download.id}")
                        except Exception as e:
                            logger.warning(f"[PARSER] Could not associate download with beacon {line_data['bid']}: {e}")
                    else:
                        logger.warning(f"[PARSER] Download has no beacon ID, skipping: {line_data}")
                elif "illegal subarray" in line:
                    # Indicator that the DB and TS are out of sync, likely due to a Model Reset on the TS
                    logger.error(f"[PARSER] Deleting local copy of {server} data - we are ahead of it")
                    clear_local_copy(server)
                    return
                elif "read [Manage: unauth'd user]: null" in line:
                    logger.error(f"[PARSER] Error suggests version mismatch between Team Server and local CS Client")
            except BaseException as ex:
                # Only print error for non-connection status messages
                if not any(msg in line for msg in [
                    "Loading Windows error codes",
                    "Windows error codes loaded",
                    "Connected OK",
                    "Synchronizing",
                    "Synchronized OK",
                    "shutting down client",
                    "Disconnected from team server"
                ]):
                    logger.error(f"[PARSER] Error parsing line: {line}")
                    logger.error(f"[PARSER] Error details: {str(ex)}")
                    traceback.print_exc()
                # If things have gone so wrong we need to rebuild the DB, uncomment the following line:
                # clear_local_copy(server)
    except BaseException as e:
        logger.error(f"[PARSER] Exception in background task: {str(e)}")
        traceback.print_exc()
        raise e  # Rethrow the exception so background tasks recognises an error occurred


def get_beacon_for_bid(bid, team_server):
    # Cope with beacon IDs as strings, or wrapped in single item arrays
    # (resulting from a bug in the NCC menu)
    bid = re.match(r"(?:@\(')?(\d+)(?:'\))?", bid).group(1)

    return Beacon.objects.get(id=bid, team_server=team_server)


def clear_local_copy(team_server):
    Archive.objects.filter(team_server=team_server).delete()
    BeaconLog.objects.filter(team_server=team_server).delete()
    Beacon.objects.filter(team_server=team_server).delete()
    Listener.objects.filter(team_server=team_server).delete()
    Credential.objects.filter(team_server=team_server).delete()
    Download.objects.filter(team_server=team_server).delete()


def clean_type(input_string):
    return input_string.removeprefix("beacon_").replace("tasked", "task").removesuffix("_alt")