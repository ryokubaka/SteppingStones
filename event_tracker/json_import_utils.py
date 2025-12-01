"""
Utility functions for importing Cobalt Strike beacon logs from JSON format.
Can be used both as a Django utility module and as a standalone command-line script.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# Django imports will be done at function level when needed
# This allows the module to be imported in Django context without issues


def parse_timestamp(timestamp_str):
    """
    Parse timestamp string from JSON format to datetime object.
    Expected format: "2023-05-02 09:02:04 EDT"
    """
    import dateparser
    from django.utils.timezone import make_aware
    from datetime import timezone as tz
    
    # Remove timezone abbreviation and parse
    # dateparser can handle various formats
    dt = dateparser.parse(timestamp_str)
    if dt is None:
        raise ValueError(f"Could not parse timestamp: {timestamp_str}")
    
    # Make timezone aware (convert to UTC)
    if dt.tzinfo is None:
        # If no timezone info, assume UTC
        dt = make_aware(dt, tz.utc)
    else:
        # Convert to UTC
        dt = dt.astimezone(tz.utc)
    
    return dt


def ensure_correct_database(db_path, operation_name="import"):
    """
    CRITICAL: Ensure the active_op_db connection is pointing to the correct database.
    This must be called before EVERY database operation during import to prevent
    the middleware from switching us back to the active operation's database.
    
    The middleware runs on every HTTP request (including progress polling), which can
    reset the database connection back to the active operation. This function ensures
    we're always writing to the NEW database being imported.
    
    Args:
        db_path: The Path object for the database we want to write to
        operation_name: Optional name for logging
    
    Raises:
        RuntimeError: If the database path cannot be fixed
    """
    from django.db import connections
    import logging
    logger = logging.getLogger(__name__)
    
    current_db = connections.databases['active_op_db'].get('NAME', '')
    expected_db = str(db_path)
    
    if str(current_db) != expected_db:
        logger.warning(f"[{operation_name}] Database path mismatch! Expected: {expected_db}, Got: {current_db}. Fixing...")
        
        # Aggressively close and reset the connection
        if 'active_op_db' in connections:
            try:
                conn = connections['active_op_db']
                try:
                    conn.close()
                except:
                    pass
                try:
                    conn.connection = None
                except:
                    pass
                # Clear thread-local storage
                try:
                    if hasattr(conn, '_thread_local'):
                        delattr(conn, '_thread_local')
                except:
                    pass
            except:
                pass
        
        # Update the database path in settings
        connections.databases['active_op_db']['NAME'] = expected_db
        
        # Also update settings_dict if connection exists
        if 'active_op_db' in connections:
            try:
                connections['active_op_db'].settings_dict['NAME'] = expected_db
            except:
                pass
        
        # Verify it's fixed
        current_db = connections.databases['active_op_db'].get('NAME', '')
        if str(current_db) != expected_db:
            raise RuntimeError(f"[{operation_name}] Failed to fix database path! Expected: {expected_db}, Got: {current_db}")
        
        logger.info(f"[{operation_name}] Database path fixed: {expected_db}")


def normalize_type(type_str):
    """
    Normalize the type string from JSON to match Stepping Stones format.
    Maps JSON types to database types, stripping "beacon_" prefix if present.
    Signals expect types without the prefix (e.g., "input", "output", not "beacon_input").
    """
    type_mapping = {
        "    INPUT": "input",
        "INPUT": "input",
        "OUTPUT": "output",
        "OUTPUT_ALT": "output_alt",
        "OUTPUT_PS": "output_ps",
        "OUTPUT_LS": "output_ls",
        "OUTPUT_JOBS": "output_jobs",
        "OUTPUT_TOKEN_STORE": "output_token_store",
        "OUTPUT_TOKEN_STORE_STEAL": "output_token_store_steal",
        "ERROR": "error",
        "TASKED": "task",
        "CHECKIN": "checkin",
        "NOTE": "note",
        "INDICATOR": "indicator",
    }
    
    type_str = type_str.strip()
    normalized = type_mapping.get(type_str, type_str.lower())
    # Strip "beacon_" prefix if present (for compatibility with existing data)
    if normalized.startswith("beacon_"):
        normalized = normalized[7:]  # Remove "beacon_" prefix (7 characters)
    return normalized


def get_or_create_team_server(hostname="Imported Data", description=None, expected_db_path=None):
    """Get or create a TeamServer entry for imported data."""
    from django.db import connections
    from cobalt_strike_monitor.models import TeamServer
    from datetime import timezone as tz
    import logging
    
    logger = logging.getLogger(__name__)
    
    # Verify we're using the correct database, and fix if needed
    if expected_db_path:
        ensure_correct_database(expected_db_path, "get_or_create_team_server")
    
    try:
        team_server, created = TeamServer.objects.using('active_op_db').get_or_create(
            hostname=hostname,
            defaults={
                'port': 50050,
                'password': '',
                'description': description or f"Imported from JSON on {datetime.now(tz.utc)}",
                'active': False
            }
        )
        return team_server
    except Exception as e:
        logger.error(f"ERROR in get_or_create_team_server: {e}", exc_info=True)
        raise


def get_or_create_listener(team_server, name="Imported Listener", expected_db_path=None):
    """Get or create a Listener entry."""
    from django.db import connections
    from cobalt_strike_monitor.models import Listener
    import logging
    
    logger = logging.getLogger(__name__)
    
    # Verify we're using the correct database, and fix if needed
    if expected_db_path:
        ensure_correct_database(expected_db_path, "get_or_create_listener")
    
    try:
        listener, created = Listener.objects.using('active_op_db').get_or_create(
            team_server=team_server,
            name=name,
            defaults={
                'proxy': '',
                'payload': 'windows/beacon_http/reverse_http',
                'port': '80',
                'profile': '',
                'host': '',
                'althost': '',
                'strategy': '',
                'beacons': '',
                'bindto': '',
                'status': 'active',
                'maxretry': '',
                'localonly': False,
                'guards': ''
            }
        )
        return listener
    except Exception as e:
        logger.error(f"ERROR in get_or_create_listener: {e}", exc_info=True)
        raise


def get_or_create_beacon(team_server, listener, beacon_id, beacon_data, expected_db_path=None):
    """
    Get or create a Beacon entry from JSON data.
    beacon_data should be a dict with fields from the JSON entries.
    """
    import sys
    from django.db import connections
    import logging
    
    logger = logging.getLogger(__name__)
    
    # Verify we're using the correct database, and fix if needed
    if expected_db_path:
        ensure_correct_database(expected_db_path, "get_or_create_beacon")
    
    # Extract beacon information from the first entry with this beacon_id
    hostname = beacon_data.get('hostname', 'Unknown')
    user = beacon_data.get('user', 'Unknown')
    process_name = beacon_data.get('process_name', 'unknown.exe')
    pid = beacon_data.get('pid', '0')
    
    # Try to get existing beacon - check by ID first (ID is the primary key)
    from cobalt_strike_monitor.models import Beacon
    from datetime import timezone as tz
    try:
        # First check if beacon with this ID exists (ID is primary key, so unique)
        beacon = Beacon.objects.using('active_op_db').get(id=beacon_id)
        # Update last seen time if this entry is newer
        if beacon_data.get('timestamp'):
            entry_time = parse_timestamp(beacon_data['timestamp'])
            if beacon.last is None or entry_time > beacon.last:
                beacon.last = entry_time
            if entry_time < beacon.opened:
                beacon.opened = entry_time
            beacon.save(using='active_op_db')
        return beacon
    except Beacon.DoesNotExist:
        pass
    except Beacon.MultipleObjectsReturned:
        # Multiple beacons with same ID shouldn't happen, but handle it
        beacon = Beacon.objects.using('active_op_db').filter(id=beacon_id).first()
        if beacon:
            if beacon_data.get('timestamp'):
                entry_time = parse_timestamp(beacon_data['timestamp'])
                if beacon.last is None or entry_time > beacon.last:
                    beacon.last = entry_time
                if entry_time < beacon.opened:
                    beacon.opened = entry_time
                beacon.save(using='active_op_db')
            return beacon
    
    # Create new beacon - handle IntegrityError in case of race condition
    entry_time = parse_timestamp(beacon_data['timestamp']) if beacon_data.get('timestamp') else datetime.now(tz.utc)
    
    # CRITICAL: Ensure we're using the correct database before re-fetching
    if expected_db_path:
        ensure_correct_database(expected_db_path, "get_or_create_beacon_before_create")
    
    # CRITICAL: Re-fetch team_server and listener from the target database to ensure they exist there
    # The objects passed in might have been created in a different database
    from cobalt_strike_monitor.models import TeamServer, Listener
    
    # Store original values before re-fetching
    team_server_hostname = team_server.hostname
    listener_name = listener.name
    
    try:
        # Re-fetch team_server from the target database
        team_server = TeamServer.objects.using('active_op_db').get(hostname=team_server_hostname)
    except TeamServer.DoesNotExist:
        # If it doesn't exist, try to get_or_create it (might have been created in wrong DB)
        logger.warning(f"TeamServer '{team_server_hostname}' not found in target database, attempting to get_or_create...")
        team_server, created = TeamServer.objects.using('active_op_db').get_or_create(
            hostname=team_server_hostname,
            defaults={
                'port': 50050,
                'password': '',
                'description': f"Imported from JSON",
                'active': False
            }
        )
        if created:
            logger.info(f"Re-created TeamServer '{team_server_hostname}' in target database")
    
    try:
        # Re-fetch listener from the target database
        listener = Listener.objects.using('active_op_db').get(name=listener_name, team_server=team_server)
    except Listener.DoesNotExist:
        # If it doesn't exist, try to get_or_create it (might have been created in wrong DB)
        logger.warning(f"Listener '{listener_name}' not found in target database, attempting to get_or_create...")
        listener, created = Listener.objects.using('active_op_db').get_or_create(
            team_server=team_server,
            name=listener_name,
            defaults={
                'proxy': '',
                'payload': 'windows/beacon_http/reverse_http',
                'port': '80',
                'profile': '',
                'host': '',
                'althost': '',
                'strategy': '',
                'beacons': '',
                'bindto': '',
                'status': '',
                'maxretry': '',
                'localonly': False,
                'guards': ''
            }
        )
        if created:
            logger.info(f"Re-created Listener '{listener_name}' in target database")
    
    try:
        beacon = Beacon.objects.using('active_op_db').create(
            team_server=team_server,
            id=beacon_id,
            listener=listener,
            note='',
            charset='windows-1252',
            internal='0.0.0.0',
            external='0.0.0.0',
            computer=hostname,
            host='0.0.0.0',
            session='beacon',
            process=process_name,
            pid=pid,
            barch='x64',
            os='Windows',
            ver='10.0',
            build='19044',
            arch='x64',
            user=user,
            opened=entry_time,
            last=entry_time
        )
        return beacon
    except Exception as e:
        # If creation fails (e.g., UNIQUE constraint), try to get it again
        # This handles race conditions where another thread/process created it
        if 'UNIQUE constraint' in str(e) or 'IntegrityError' in str(type(e).__name__):
            try:
                beacon = Beacon.objects.using('active_op_db').get(id=beacon_id)
                # Update last seen time if this entry is newer
                if beacon_data.get('timestamp'):
                    entry_time = parse_timestamp(beacon_data['timestamp'])
                    if beacon.last is None or entry_time > beacon.last:
                        beacon.last = entry_time
                    if entry_time < beacon.opened:
                        beacon.opened = entry_time
                    beacon.save(using='active_op_db')
                return beacon
            except Beacon.DoesNotExist:
                pass
        # Re-raise if it's not a UNIQUE constraint error
        raise


def create_beacon_log(team_server, beacon, log_entry, log_id, expected_db_path=None):
    """
    Create a BeaconLog entry and corresponding Archive entry from JSON log data.
    Uses get_or_create to avoid duplicates based on timestamp, beacon, and type.
    """
    import sys
    from django.db import connections
    
    # Verify we're using the correct database, and fix if needed
    if expected_db_path:
        ensure_correct_database(expected_db_path, "create_beacon_log")
    
    timestamp = parse_timestamp(log_entry['timestamp'])
    log_type = normalize_type(log_entry.get('type', 'output'))
    
    # Determine data field - use 'result' if available, otherwise 'command'
    data = log_entry.get('result', '')
    if not data and log_entry.get('command'):
        data = log_entry['command']
    
    # Get operator if present
    operator = log_entry.get('operator', '').strip() or None
    
    # Extract tactic from phase if present (for Archive entries)
    tactic = log_entry.get('phase', '').strip() or None
    
    # Use get_or_create to avoid duplicates
    # We match on team_server, when, beacon, and type to identify unique log entries
    from cobalt_strike_monitor.models import BeaconLog, Archive
    
    # Check if a log with this ID already exists - if so, update it
    # Otherwise, create a new one with this ID
    # CRITICAL: Don't check for duplicates by team_server/when/beacon/type because
    # multiple entries can legitimately have the same timestamp/beacon/type
    try:
        beacon_log = BeaconLog.objects.using('active_op_db').get(id=log_id)
        # Update existing log
        beacon_log.team_server = team_server
        beacon_log.when = timestamp
        beacon_log.beacon = beacon
        beacon_log.type = log_type
        beacon_log.data = data
        beacon_log.operator = operator
        beacon_log.output_job = None
        beacon_log.save(using='active_op_db')
        created = False
    except BeaconLog.DoesNotExist:
        # ID doesn't exist, create new log with specified ID
        # But first check if ID is available (in case of race condition)
        max_retries = 3
        actual_log_id = log_id
        for attempt in range(max_retries):
            try:
                # Check if ID is taken
                BeaconLog.objects.using('active_op_db').get(id=actual_log_id)
                # ID is taken, find next available ID
                max_id = BeaconLog.objects.using('active_op_db').aggregate(
                    max_id=models.Max('id')
                )['max_id'] or 0
                actual_log_id = max_id + 1
                if attempt < max_retries - 1:
                    continue
            except BeaconLog.DoesNotExist:
                # ID is available, use it
                break
        
        beacon_log = BeaconLog.objects.using('active_op_db').create(
            id=actual_log_id,
            team_server=team_server,
            when=timestamp,
            beacon=beacon,
            type=log_type,
            data=data,
            operator=operator,
            output_job=None
        )
        created = True
    
    # Create corresponding Archive entry for inputs, tasks, and other types
    # Archive is used by CSAction.input property to display inputs in the UI
    # Archive.data is limited to 100 chars, so truncate if necessary
    archive_data = data[:100] if len(data) > 100 else data
    
    # Only create Archive entries for certain types (input, task, indicator, etc.)
    # Output types don't need Archive entries as they're read from BeaconLog
    if log_type in ['input', 'task', 'indicator', 'checkin', 'initial']:
        # Use the same ID as the BeaconLog we just created/updated
        archive_log_id = beacon_log.id
        
        # Check if archive with this ID exists
        try:
            archive = Archive.objects.using('active_op_db').get(id=archive_log_id)
            # Update existing archive
            archive.team_server = team_server
            archive.when = timestamp
            archive.beacon = beacon
            archive.type = log_type
            archive.data = archive_data
            archive.tactic = tactic
            archive.save(using='active_op_db')
            archive_created = False
        except Archive.DoesNotExist:
            # Try to get by team_server/when/beacon/type
            # Use filter().first() to handle potential duplicates
            try:
                existing_archives = Archive.objects.using('active_op_db').filter(
                    team_server=team_server,
                    when=timestamp,
                    beacon=beacon,
                    type=log_type
                )
                if existing_archives.exists():
                    # If multiple exist, use the first one and delete the rest
                    archive = existing_archives.first()
                    if existing_archives.count() > 1:
                        # Delete duplicates, keeping only the first
                        for dup in existing_archives[1:]:
                            dup.delete()
                else:
                    raise Archive.DoesNotExist
                # Update existing archive
                archive.data = archive_data
                if tactic:
                    archive.tactic = tactic
                archive.save(using='active_op_db')
                archive_created = False
            except Archive.DoesNotExist:
                # Create new archive - check if ID is available
                try:
                    Archive.objects.using('active_op_db').get(id=archive_log_id)
                    # ID taken, find next available
                    max_archive_id = Archive.objects.using('active_op_db').aggregate(
                        max_id=models.Max('id')
                    )['max_id'] or 0
                    archive_log_id = max_archive_id + 1
                except Archive.DoesNotExist:
                    pass  # ID is available
                
                archive = Archive.objects.using('active_op_db').create(
                    id=archive_log_id,
                    team_server=team_server,
                    when=timestamp,
                    beacon=beacon,
                    type=log_type,
                    data=archive_data,
                    tactic=tactic
                )
                archive_created = True
    
    return beacon_log


def import_json_to_database(json_file_or_path, db_path_or_name, operation_name=None, progress_id=None):
    """
    Main function to import JSON data into a Stepping Stones SQLite database.
    
    Can be called from:
    - UI: import_json_to_database(uploaded_file, db_path, operation_name, progress_id)
    - CLI: import_json_to_database(json_file_path, db_name, operation_name)
    
    Args:
        json_file_or_path: Either a Django UploadedFile object or a file path (Path/str)
        db_path_or_name: Either a Path object to the database file, or a string name for the database
        operation_name: Optional operation name for description
        progress_id: Optional UUID string for progress tracking (UI only)
    
    Returns:
        tuple: (success: bool, message: str, stats: dict) when called from UI
        None: when called from CLI (prints to stdout)
    """
    import logging
    import sys
    
    logger = logging.getLogger(__name__)
    logger.info("Starting JSON import function")
    
    try:
        logger.info("Importing Django components...")
        
        # Import all Django components needed for this function - MUST happen first
        from django.db import connections, models
        from django.core.management import call_command
        from django.conf import settings
        from cobalt_strike_monitor.models import TeamServer, Beacon, BeaconLog, Listener, Archive
        logger.info("All Django imports completed")
        
        # Determine if this is being called from UI (UploadedFile + Path) or CLI (path + name)
        is_ui_call = hasattr(json_file_or_path, 'read') and isinstance(db_path_or_name, Path)
        
        if is_ui_call:
            # Called from UI: json_file_or_path is UploadedFile, db_path_or_name is Path
            json_file = json_file_or_path
            db_path = db_path_or_name
            
            # Read JSON from uploaded file
            json_file.seek(0)  # Reset file pointer
            data = json.load(json_file)
            logger.info(f"JSON loaded successfully, keys: {list(data.keys())}")
        else:
            # Called from CLI: json_file_or_path is file path, db_path_or_name is db name
            json_file_path = Path(json_file_or_path)
            db_name = db_path_or_name
            
            # Read JSON file
            print(f"Reading JSON file: {json_file_path}")
            with open(json_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Setup database path for CLI
            # When called from CLI, calculate ops-data directory relative to project root
            # The file is in event_tracker/, so go up one level to project root
            project_root = Path(__file__).resolve().parent.parent
            ops_data_dir = project_root / 'ops-data'
            ops_data_dir.mkdir(exist_ok=True)
            db_path = ops_data_dir / f"{db_name}.sqlite3"
            operation_name = operation_name or db_name
        
        # Helper function to update progress
        def update_progress(status, message, progress=0, total=0, current=0):
            if progress_id and is_ui_call:
                try:
                    from django.core.cache import cache
                    progress_data = {
                        'status': status,
                        'message': message,
                        'progress': int(progress),  # Ensure it's an integer
                        'total': int(total) if total else 0,
                        'current': int(current) if current else 0
                    }
                    # Use set with a longer timeout and ensure it's written immediately
                    cache.set(f'import_progress_{progress_id}', progress_data, timeout=3600)
                    # Force cache to persist by accessing it (some cache backends need this)
                    cache.get(f'import_progress_{progress_id}')
                    # Debug logging
                    if progress > 0 or status != 'running':
                        logger.debug(f"Progress update: {status} - {progress}% - {message}")
                except Exception as e:
                    logger.warning(f"Failed to update progress: {e}", exc_info=True)
        
        # Update progress: starting
        update_progress('running', 'Validating JSON structure...', 0, 0, 0)
        
        if 'data' not in data:
            error_msg = "JSON file must have a 'data' key containing an array of log entries"
            logger.error(error_msg)
            if is_ui_call:
                return False, error_msg, {}
            raise ValueError(error_msg)
        
        log_entries = data['data']
        total_entries = len(log_entries)  # Define total_entries early for progress tracking
        logger.info(f"Found {total_entries} log entries to process")
        if not is_ui_call:
            print(f"Found {total_entries} log entries")
        
        # Update progress: JSON loaded
        update_progress('running', f'Found {total_entries} log entries. Setting up database...', 5, total_entries, 0)
        
        # Update progress: Configuring database connection
        update_progress('running', 'Configuring database connection...', 6, total_entries, 0)
        
        # Configure the active_op_db connection
        # Ensure settings is available before using it
        if 'settings' not in locals():
            error_msg = "settings variable not found in local scope!"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        
        timezone_value = getattr(settings, 'TIME_ZONE', 'UTC')
        
        # CRITICAL: Close any existing connection FIRST and completely remove it
        # This ensures we're not using a connection from the active operation
        # Do this more aggressively to prevent reuse of old connections
        if 'active_op_db' in connections.databases:
            try:
                if 'active_op_db' in connections:
                    conn = connections['active_op_db']
                    try:
                        conn.close()
                    except:
                        pass
                    try:
                        conn.connection = None
                    except:
                        pass
                    # Clear thread-local storage
                    try:
                        if hasattr(conn, '_thread_local'):
                            delattr(conn, '_thread_local')
                    except:
                        pass
            except:
                pass
            # Remove from connections dict to force complete recreation
            try:
                del connections['active_op_db']
            except KeyError:
                pass
            try:
                del connections.databases['active_op_db']
            except KeyError:
                pass
        
        # Ensure parent directory exists - use absolute path FIRST
        db_path = db_path.resolve()  # Convert to absolute path
        
        # CRITICAL: Completely recreate the database connection configuration
        # This ensures the connection is isolated from the active operation
        connections.databases['active_op_db'] = {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(db_path),
            'OPTIONS': {'timeout': 20},
            'ATOMIC_REQUESTS': False,
            'CONN_MAX_AGE': 0,  # Don't reuse connections - force new connection each time
            'AUTOCOMMIT': True,
            'TIME_ZONE': timezone_value,
            'CONN_HEALTH_CHECKS': False,
        }
        
        # Verify the connection is pointing to the correct database
        current_db = connections.databases['active_op_db'].get('NAME', '')
        if str(current_db) != str(db_path):
            raise RuntimeError(f"Failed to configure database connection! Expected: {db_path}, Got: {current_db}")
        logger.info(f"Database connection configured for import: {db_path}")
        
        # Create database if it doesn't exist and run migrations
        parent_dir = db_path.parent
        
        # CRITICAL: Ensure parent directory exists and is writable
        try:
            if not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)
            
            # Verify directory exists after creation
            if not parent_dir.exists():
                raise RuntimeError(f"Parent directory {parent_dir} still does not exist after creation attempt")
            
            # Check if directory is writable
            import os
            if not os.access(str(parent_dir), os.W_OK):
                raise RuntimeError(f"Parent directory {parent_dir} is not writable")
        except (PermissionError, OSError) as e:
            error_msg = f"Error creating/accessing parent directory {parent_dir}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e
        except Exception as e:
            error_msg = f"Unexpected error creating parent directory {parent_dir}: {e}"
            logger.error(error_msg, exc_info=True)
            raise RuntimeError(error_msg) from e
        
        if not db_path.exists():
            print(f"JSON IMPORT: Creating new database: {db_path}", file=sys.stderr)
            sys.stderr.flush()
            # Create empty database file
            import sqlite3
            import os
            
            # Double-check we can write to the directory with a test file
            test_file = parent_dir / '.test_write'
            try:
                test_file.touch()
                test_file.unlink()
                print("JSON IMPORT: Directory write test successful", file=sys.stderr)
                sys.stderr.flush()
            except Exception as e:
                error_msg = f"Cannot write to directory {parent_dir}: {e}. Check permissions."
                print(f"JSON IMPORT: ERROR - {error_msg}", file=sys.stderr)
                sys.stderr.flush()
                raise RuntimeError(error_msg) from e
            
            # Try to create the database file
            try:
                print(f"JSON IMPORT: Attempting to create database file: {db_path}", file=sys.stderr)
                sys.stderr.flush()
                
                # Check if file already exists and is locked
                if db_path.exists():
                    print(f"JSON IMPORT: Database file already exists, checking if it's accessible...", file=sys.stderr)
                    sys.stderr.flush()
                    try:
                        # Try to open it in read-only mode to check if it's locked
                        test_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                        test_conn.close()
                        print(f"JSON IMPORT: Existing database file is accessible, will use it", file=sys.stderr)
                        sys.stderr.flush()
                    except sqlite3.OperationalError as lock_error:
                        error_msg = f"Database file {db_path} exists but is locked or inaccessible: {lock_error}"
                        print(f"JSON IMPORT: ERROR - {error_msg}", file=sys.stderr)
                        sys.stderr.flush()
                        raise RuntimeError(error_msg) from lock_error
                else:
                    # File doesn't exist, create it
                    conn = sqlite3.connect(str(db_path))
                    conn.close()
                    print("JSON IMPORT: Empty database file created successfully", file=sys.stderr)
                    sys.stderr.flush()
                
                # Verify the file was actually created
                if not db_path.exists():
                    raise RuntimeError(f"Database file {db_path} was not created despite no error")
                print(f"JSON IMPORT: Database file verified to exist: {db_path.exists()}, size: {db_path.stat().st_size if db_path.exists() else 0} bytes", file=sys.stderr)
                sys.stderr.flush()
            except sqlite3.OperationalError as e:
                error_msg = f"SQLite error creating database file {db_path}: {e}"
                print(f"JSON IMPORT: ERROR - {error_msg}", file=sys.stderr)
                if parent_dir.exists():
                    import stat
                    import os
                    dir_stat = parent_dir.stat()
                    print(f"JSON IMPORT: Parent directory permissions: {oct(dir_stat.st_mode)}", file=sys.stderr)
                    print(f"JSON IMPORT: Parent directory owner: UID={dir_stat.st_uid}, GID={dir_stat.st_gid}", file=sys.stderr)
                    print(f"JSON IMPORT: Current process UID: {os.getuid() if hasattr(os, 'getuid') else 'N/A'}, GID: {os.getgid() if hasattr(os, 'getgid') else 'N/A'}", file=sys.stderr)
                sys.stderr.flush()
                raise RuntimeError(error_msg) from e
            except Exception as e:
                error_msg = f"Unexpected error creating database file {db_path}: {e}"
                print(f"JSON IMPORT: ERROR - {error_msg}", file=sys.stderr)
                import traceback
                print(traceback.format_exc(), file=sys.stderr)
                sys.stderr.flush()
                raise RuntimeError(error_msg) from e
            
            # Run migrations
            print("JSON IMPORT: Running migrations on new database...", file=sys.stderr)
            sys.stderr.flush()
            try:
                # CRITICAL: Completely remove and recreate the connection to force Django to use the new database
                if 'active_op_db' in connections:
                    try:
                        connections['active_op_db'].close()
                    except:
                        pass
                    try:
                        connections['active_op_db'].connection = None
                    except:
                        pass
                    # Remove from connections dict to force recreation
                    try:
                        del connections['active_op_db']
                    except:
                        pass
                
                # CRITICAL: Completely recreate the connection configuration
                # This ensures we're using the new database, not the active operation's database
                connections.databases['active_op_db'] = {
                    'ENGINE': 'django.db.backends.sqlite3',
                    'NAME': str(db_path),
                    'OPTIONS': {'timeout': 20},
                    'ATOMIC_REQUESTS': False,
                    'CONN_MAX_AGE': 0,  # Don't reuse connections
                    'AUTOCOMMIT': True,
                    'TIME_ZONE': timezone_value,
                    'CONN_HEALTH_CHECKS': False,
                }
                
                # Verify the connection is pointing to the correct database
                current_db = connections.databases['active_op_db'].get('NAME', '')
                if str(current_db) != str(db_path):
                    raise RuntimeError(f"Failed to configure database connection for migrations! Expected: {db_path}, Got: {current_db}")
                
                # Use the same migration approach as _initialize_operation_db in middleware
                # First run migrations for core Django apps
                core_apps = [
                    'contenttypes',  # For django_content_type
                    'auth',          # For auth_permission
                    'admin',         # For admin_log
                    'sessions',      # For session management
                ]
                
                # Then run migrations for our apps
                our_apps = [
                    'event_tracker',
                    'cobalt_strike_monitor',
                    'taggit',
                    'djangoplugins',
                    'reversion',
                    'background_task'
                ]
                
                from io import StringIO
                from django.core.management import CommandError
                
                # CRITICAL: First run migrate without app name to run ALL migrations
                # This ensures all tables are created
                update_progress('running', 'Applying all database migrations (this may take a moment)...', 8, total_entries, 0)
                all_migrations_buffer = StringIO()
                try:
                    # Ensure connection is deleted before running migrations
                    if 'active_op_db' in connections:
                        try:
                            connections['active_op_db'].close()
                        except:
                            pass
                        del connections['active_op_db']
                    connections.databases['active_op_db']['NAME'] = str(db_path)
                    
                    call_command(
                        'migrate',
                        database='active_op_db',
                        verbosity=2,
                        interactive=False,
                        stdout=all_migrations_buffer,
                        stderr=all_migrations_buffer
                    )
                    update_progress('running', 'Migrations completed. Verifying tables...', 9, total_entries, 0)
                    all_output = all_migrations_buffer.getvalue()
                    if "Error" in all_output or "Traceback" in all_output:
                        raise RuntimeError(f"Migration failed: {all_output[:500]}")
                except CommandError as ce:
                    logger.error(f"CommandError running all migrations: {ce}")
                    raise
                finally:
                    all_migrations_buffer.close()
                
                # Also run migrations per app to ensure everything is applied (redundant but safe)
                update_progress('running', 'Verifying migrations per app...', 9, total_entries, 0)
                
                total_apps = len(core_apps) + len(our_apps)
                app_idx = 0
                
                # Run migrations for core apps first
                for app_name in core_apps:
                    # Update progress during per-app migrations (9-10% of total)
                    progress_pct = 9 + int((app_idx / total_apps) * 1) if total_apps > 0 else 9
                    update_progress('running', f'Verifying migrations for {app_name}...', progress_pct, total_apps, app_idx + 1)
                    app_idx += 1
                    migration_output_buffer = StringIO()
                    try:
                        # Ensure connection is deleted before each migration to force fresh connection
                        if 'active_op_db' in connections:
                            try:
                                connections['active_op_db'].close()
                            except:
                                pass
                            del connections['active_op_db']
                        connections.databases['active_op_db']['NAME'] = str(db_path)
                        
                        call_command(
                            'migrate',
                            app_name,
                            database='active_op_db',
                            verbosity=2,
                            interactive=False,
                            stdout=migration_output_buffer,
                            stderr=migration_output_buffer
                        )
                        output = migration_output_buffer.getvalue()
                        if "Error" in output or "Traceback" in output:
                            raise RuntimeError(f"Migration failed for core app {app_name}: {output[:200]}")
                    except CommandError as ce:
                        logger.error(f"CommandError for core app '{app_name}': {ce}")
                        raise
                    finally:
                        migration_output_buffer.close()
                
                # Then run migrations for our apps
                for app_name in our_apps:
                    # Update progress during per-app migrations (9-10% of total)
                    progress_pct = 9 + int((app_idx / total_apps) * 1) if total_apps > 0 else 9
                    update_progress('running', f'Verifying migrations for {app_name}...', progress_pct, total_apps, app_idx + 1)
                    app_idx += 1
                    migration_output_buffer = StringIO()
                    try:
                        # Ensure connection is deleted before each migration to force fresh connection
                        if 'active_op_db' in connections:
                            try:
                                connections['active_op_db'].close()
                            except:
                                pass
                            del connections['active_op_db']
                        connections.databases['active_op_db']['NAME'] = str(db_path)
                        
                        call_command(
                            'migrate',
                            app_name,
                            database='active_op_db',
                            verbosity=2,
                            interactive=False,
                            stdout=migration_output_buffer,
                            stderr=migration_output_buffer
                        )
                        output = migration_output_buffer.getvalue()
                        if "Error" in output or "Traceback" in output:
                            raise RuntimeError(f"Migration failed for app {app_name}: {output[:200]}")
                    except CommandError as ce:
                        logger.error(f"CommandError for app '{app_name}': {ce}")
                        raise
                    finally:
                        migration_output_buffer.close()
                
                # Reconnect to verify tables
                connections['active_op_db'].close()
                connections['active_op_db'].connection = None
                
                # CRITICAL: Verify we're still pointing to the correct database
                current_db_path = connections.databases['active_op_db']['NAME']
                if str(current_db_path) != str(db_path):
                    # Try to fix it - middleware might have changed it
                    logger.warning(f"Database path mismatch detected! Expected: {db_path}, Got: {current_db_path}. Attempting to fix...")
                    connections['active_op_db'].close()
                    connections['active_op_db'].connection = None
                    connections.databases['active_op_db']['NAME'] = str(db_path)
                    # Verify again
                    current_db_path = connections.databases['active_op_db']['NAME']
                    if str(current_db_path) != str(db_path):
                        raise RuntimeError(f"Database path changed after migrations and could not be fixed! Expected: {db_path}, Got: {current_db_path}")
                    logger.info(f"Database path fixed: {db_path}")
                
                # Verify tables were created
                cursor = connections['active_op_db'].cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cobalt_strike_monitor%'")
                tables = [row[0] for row in cursor.fetchall()]
                
                if 'cobalt_strike_monitor_teamserver' not in tables:
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    all_tables = [row[0] for row in cursor.fetchall()]
                    raise RuntimeError(f"Required table 'cobalt_strike_monitor_teamserver' not found after migrations. Found tables: {all_tables}")
                
                update_progress('running', 'Database setup completed. Starting data import...', 10, total_entries, 0)
            except Exception as e:
                logger.error(f"ERROR during migrations: {e}", exc_info=True)
                raise
        else:
            # Update progress: Running migrations on existing database
            update_progress('running', 'Running migrations on existing database...', 8, total_entries, 0)
            # Ensure migrations are up to date
            try:
                call_command('migrate', '--database=active_op_db', verbosity=1, interactive=False)
                update_progress('running', 'Migrations completed. Starting data import...', 10, total_entries, 0)
            except Exception as e:
                logger.error(f"ERROR during migrations: {e}", exc_info=True)
                raise
        
        # Group entries by beacon_id to create beacon records
        update_progress('running', 'Grouping entries by beacon_id...', 10, total_entries, 0)
        
        beacons_data = defaultdict(dict)
        
        for idx, entry in enumerate(log_entries):
            # Update progress more frequently during grouping (every 1000 entries instead of 5000)
            if idx == 0 or (idx % 1000 == 0 and idx > 0) or idx == total_entries - 1:
                progress_pct = int((idx / total_entries) * 10) + 10  # 10-20% for grouping
                update_progress('running', f'Grouping entries: {idx}/{total_entries}', progress_pct, total_entries, idx)
            beacon_id = entry.get('beacon_id')
            if beacon_id:
                # Convert beacon_id to integer if it's a string
                try:
                    beacon_id = int(beacon_id)
                except (ValueError, TypeError):
                    if not is_ui_call:
                        print(f"  Warning: Invalid beacon_id '{beacon_id}', skipping entry")
                    continue
                # Store the first entry for each beacon to extract metadata
                if 'timestamp' not in beacons_data[beacon_id] or not beacons_data[beacon_id].get('timestamp'):
                    beacons_data[beacon_id] = entry
        
        print(f"JSON IMPORT: Grouped into {len(beacons_data)} unique beacons", file=sys.stderr)
        sys.stderr.flush()
        update_progress('running', f'Grouped into {len(beacons_data)} unique beacons. Creating TeamServer and Listener...', 20, total_entries, 0)
        
        # Create TeamServer and Listener
        print("JSON IMPORT: Creating TeamServer and Listener...", file=sys.stderr)
        sys.stderr.flush()
        if not is_ui_call:
            print("Creating TeamServer and Listener...")
        
        file_name = json_file.name if is_ui_call else Path(json_file_or_path).name
        print(f"JSON IMPORT: About to create/get TeamServer and Listener", file=sys.stderr)
        sys.stderr.flush()
        
        team_server = get_or_create_team_server(
            hostname="Imported Data",
            description=f"Imported from {file_name} - Operation: {operation_name or 'Unknown'}",
            expected_db_path=db_path
        )
        
        listener = get_or_create_listener(
            team_server, 
            name=f"Listener for {operation_name or 'Imported'}",
            expected_db_path=db_path
        )
        
        # CRITICAL: Verify we're using the correct database and fix if needed
        current_db = connections.databases['active_op_db'].get('NAME', '')
        if str(current_db) != str(db_path):
            # Close and reconfigure
            if 'active_op_db' in connections:
                try:
                    connections['active_op_db'].close()
                except:
                    pass
                connections['active_op_db'].connection = None
            # Reconfigure with correct path
            connections.databases['active_op_db']['NAME'] = str(db_path)
            # Verify again
            current_db = connections.databases['active_op_db'].get('NAME', '')
            if str(current_db) != str(db_path):
                raise RuntimeError(f"Failed to fix database path! Expected: {db_path}, Got: {current_db}")
        
        # Create Beacons
        if not is_ui_call:
            print(f"Creating {len(beacons_data)} beacon(s)...")
        beacons = {}
        total_beacons = len(beacons_data)
        for idx, (beacon_id, beacon_data) in enumerate(beacons_data.items()):
            # Update progress during beacon creation (20-30% of total progress)
            if idx == 0 or (idx % 10 == 0 and idx > 0) or idx == total_beacons - 1:
                progress_pct = 20 + int((idx / total_beacons) * 10) if total_beacons > 0 else 20
                update_progress('running', f'Creating beacons: {idx + 1}/{total_beacons}', progress_pct, total_beacons, idx + 1)
            try:
                beacon = get_or_create_beacon(team_server, listener, beacon_id, beacon_data, expected_db_path=db_path)
                beacons[beacon_id] = beacon
                if not is_ui_call and idx % 50 == 0:
                    print(f"  Created/updated beacon {beacon_id}: {beacon.computer} - {beacon.user}")
            except Exception as e:
                logger.error(f"ERROR creating beacon {beacon_id}: {e}", exc_info=True)
                raise
        
        update_progress('running', f'Created {len(beacons)} beacons. Creating log entries...', 30, total_entries, 0)
        
        # Create BeaconLog entries
        if not is_ui_call:
            print(f"Creating {len(log_entries)} beacon log entries...")
        
        # Find the highest existing log ID to avoid conflicts
        try:
            max_log_id = BeaconLog.objects.using('active_op_db').filter(team_server=team_server).aggregate(
                max_id=models.Max('id')
            )['max_id'] or 0
            log_id = max_log_id + 1
        except Exception:
            # If query fails (e.g., table doesn't exist yet), start from 1
            log_id = 1
        
        # CRITICAL: Verify we're using the correct database before bulk operations
        ensure_correct_database(db_path, "before_bulk_creation")
        
        # Prepare all log entries for bulk creation
        from cobalt_strike_monitor.models import BeaconLog, Archive
        beacon_logs_to_create = []
        archives_to_create = []
        existing_log_ids = set()
        
        # Get existing log IDs in batches to avoid memory issues
        try:
            existing_log_ids = set(BeaconLog.objects.using('active_op_db').filter(
                team_server=team_server
            ).values_list('id', flat=True))
        except Exception:
            pass
        
        # Process entries and prepare bulk objects
        valid_entries = []
        for entry in log_entries:
            beacon_id = entry.get('beacon_id')
            if not beacon_id:
                continue
            
            # Convert beacon_id to integer if it's a string
            try:
                beacon_id = int(beacon_id)
            except (ValueError, TypeError):
                continue
            
            if beacon_id not in beacons:
                continue
            
            valid_entries.append((entry, beacon_id, log_id))
            log_id += 1
        
        # Create in batches for better performance
        batch_size = 1000
        created_count = 0
        
        for batch_start in range(0, len(valid_entries), batch_size):
            batch_end = min(batch_start + batch_size, len(valid_entries))
            batch = valid_entries[batch_start:batch_end]
            
            # Update progress (use total_entries for percentage calculation)
            progress_pct = int((batch_start / total_entries) * 60) + 30 if total_entries > 0 else 30
            update_progress('running', f'Creating log entries: {batch_start}/{total_entries} ({created_count} created)', progress_pct, total_entries, batch_start)
            
            # Prepare batch objects
            beacon_logs_batch = []
            archives_batch = []
            
            # CRITICAL: Verify database connection before re-fetching beacons
            ensure_correct_database(db_path, f"before_beacon_refetch_batch_{batch_start}")
            
            # CRITICAL: Re-fetch team_server from target database to ensure it exists and get its PK
            from cobalt_strike_monitor.models import TeamServer, Beacon
            try:
                team_server_obj = TeamServer.objects.using('active_op_db').get(hostname=team_server.hostname)
                team_server_id = team_server_obj.pk
            except TeamServer.DoesNotExist:
                logger.error(f"TeamServer '{team_server.hostname}' does not exist in target database! This should not happen.")
                # Try to get_or_create it
                team_server_obj, _ = TeamServer.objects.using('active_op_db').get_or_create(
                    hostname=team_server.hostname,
                    defaults={
                        'port': 50050,
                        'password': '',
                        'description': f"Imported from JSON",
                        'active': False
                    }
                )
                team_server_id = team_server_obj.pk
            
            # CRITICAL: Re-fetch all beacons from the target database to ensure they exist there
            # Store beacon IDs (PKs) instead of model instances to avoid database binding issues
            beacon_ids_in_batch = {beacon_id for _, beacon_id, _ in batch}
            beacon_pks_in_target_db = {}
            for bid in beacon_ids_in_batch:
                if bid in beacons:
                    try:
                        # Re-fetch from target database and get PK
                        beacon_obj = Beacon.objects.using('active_op_db').get(id=bid)
                        beacon_pks_in_target_db[bid] = beacon_obj.pk
                    except Beacon.DoesNotExist:
                        # If not found, the beacon should exist - log and skip this entry
                        logger.error(f"Beacon {bid} does not exist in target database! Skipping log entries for this beacon.")
                        continue
            
            for entry, beacon_id, entry_log_id in batch:
                # Use the beacon PK from target database
                if beacon_id not in beacon_pks_in_target_db:
                    continue  # Skip if beacon doesn't exist in target DB
                
                beacon_pk = beacon_pks_in_target_db[beacon_id]
                timestamp = parse_timestamp(entry['timestamp'])
                log_type = normalize_type(entry.get('type', 'output'))
                
                # Determine data field
                data = entry.get('result', '')
                if not data and entry.get('command'):
                    data = entry['command']
                
                operator = entry.get('operator', '').strip() or None
                tactic = entry.get('phase', '').strip() or None
                
                # Only create if ID doesn't exist
                if entry_log_id not in existing_log_ids:
                    # CRITICAL: Use primary key IDs instead of model instances to avoid database binding issues
                    beacon_logs_batch.append(BeaconLog(
                        id=entry_log_id,
                        team_server_id=team_server_id,  # Use ID instead of model instance
                        when=timestamp,
                        beacon_id=beacon_pk,  # Use ID instead of model instance
                        type=log_type,
                        data=data,
                        operator=operator,
                        output_job=None
                    ))
                    existing_log_ids.add(entry_log_id)
                    
                    # Create Archive entry for certain types
                    if log_type in ['input', 'task', 'indicator', 'checkin', 'initial']:
                        archive_data = data[:100] if len(data) > 100 else data
                        archives_batch.append(Archive(
                            id=entry_log_id,
                            team_server_id=team_server_id,  # Use ID instead of model instance
                            when=timestamp,
                            beacon_id=beacon_pk,  # Use ID instead of model instance
                            type=log_type,
                            data=archive_data,
                            tactic=tactic
                        ))
            
            # CRITICAL: Verify database connection before each batch
            # The middleware may have reset it during progress polling
            ensure_correct_database(db_path, f"batch_{batch_start}")
            
            # Bulk create BeaconLog entries
            if beacon_logs_batch:
                try:
                    # Explicitly use the active_op_db connection
                    BeaconLog.objects.using('active_op_db').bulk_create(
                        beacon_logs_batch,
                        ignore_conflicts=True  # Skip if ID already exists
                    )
                    created_count += len(beacon_logs_batch)
                    
                    # Verify the connection is still correct after bulk_create
                    ensure_correct_database(db_path, f"post_bulk_create_{batch_start}")
                except Exception as e:
                    logger.error(f"ERROR bulk creating log entries batch {batch_start}-{batch_end}: {e}", exc_info=True)
                    # Fallback to individual creates for this batch
                    for log_obj in beacon_logs_batch:
                        try:
                            # Verify connection before each individual save
                            current_db = connections.databases['active_op_db'].get('NAME', '')
                            if str(current_db) != str(db_path):
                                connections['active_op_db'].close()
                                connections['active_op_db'].connection = None
                                connections.databases['active_op_db']['NAME'] = str(db_path)
                            log_obj.save(using='active_op_db')
                            created_count += 1
                        except Exception:
                            pass
            
            # Bulk create Archive entries
            if archives_batch:
                try:
                    # Explicitly use the active_op_db connection
                    Archive.objects.using('active_op_db').bulk_create(
                        archives_batch,
                        ignore_conflicts=True  # Skip if ID already exists
                    )
                    
                    # Verify the connection is still correct after bulk_create
                    ensure_correct_database(db_path, f"post_archive_bulk_create_{batch_start}")
                except Exception as e:
                    logger.error(f"ERROR bulk creating archive entries batch {batch_start}-{batch_end}: {e}", exc_info=True)
                    # Fallback to individual creates for this batch
                    for archive_obj in archives_batch:
                        try:
                            # Verify connection before each individual save
                            ensure_correct_database(db_path, f"individual_archive_save_{batch_start}")
                            archive_obj.save(using='active_op_db')
                        except Exception:
                            pass
        
        update_progress('running', f'Created {created_count} log entries. Extracting credentials...', 85, total_entries, total_entries)
        
        # CRITICAL: Extract credentials from output-type logs
        # Signals don't fire during bulk_create, so we need to manually extract credentials
        try:
            # CRITICAL: Verify database connection before credential extraction
            ensure_correct_database(db_path, "credential_extraction")
            
            # Get the operation for credential association
            # CRITICAL: Do NOT use get_current_active_operation() - we want the operation being imported, not the active one
            operation = None
            if operation_name:
                try:
                    from event_tracker.models import Operation
                    # Try to find operation by name
                    operation = Operation.objects.using('default').filter(name=operation_name).first()
                    if operation:
                        logger.info(f"Found operation for credentials: {operation.name}")
                except Exception as e:
                    logger.debug(f"Error looking up operation by name '{operation_name}': {e}")
            
            # If operation not found by name, try to infer from database path
            if not operation:
                try:
                    from event_tracker.models import Operation
                    # Infer from database path (e.g., "2023-0004.sqlite3" -> "2023-0004")
                    db_name = Path(db_path).stem
                    operation = Operation.objects.using('default').filter(name=db_name).first()
                    if operation:
                        logger.info(f"Found operation from database path: {operation.name}")
                except Exception as e:
                    logger.debug(f"Error inferring operation from database path: {e}")
            
            if not operation:
                logger.warning(f"Could not find operation for credentials. operation_name={operation_name}, db_path={db_path}")
            
            # Extract credentials from output-type logs
            # Group by system to batch process and reduce transaction overhead
            from event_tracker.signals import extract_creds
            
            output_logs = BeaconLog.objects.using('active_op_db').filter(
                team_server=team_server,
                type__in=['output', 'beacon_output']
            ).select_related('beacon').order_by('when')
            
            # Group logs by system for batch processing
            logs_by_system = defaultdict(list)
            for log_entry in output_logs:
                if log_entry.data:
                    system = log_entry.beacon.computer if log_entry.beacon else 'Unknown'
                    logs_by_system[system].append(log_entry.data)
            
            total_output_logs = sum(len(logs) for logs in logs_by_system.values())
            creds_extracted = 0
            processed_count = 0
            
            for system_idx, (system, log_data_list) in enumerate(logs_by_system.items()):
                if processed_count > 0 and processed_count % 100 == 0:
                    update_progress('running', f'Extracting credentials: {processed_count}/{total_output_logs} ({creds_extracted} systems processed)', 85, total_output_logs, processed_count)
                
                try:
                    # Concatenate all log data for this system and process once
                    combined_data = '\n'.join(log_data_list)
                    if combined_data:
                        extract_creds(combined_data, default_system=system, operation=operation)
                        creds_extracted += 1
                        processed_count += len(log_data_list)
                except Exception as e:
                    logger.debug(f"Error extracting credentials from system {system}: {e}")
                    # Fallback to individual processing if batch fails
                    for log_data in log_data_list:
                        try:
                            extract_creds(log_data, default_system=system, operation=operation)
                            processed_count += 1
                        except Exception as e2:
                            logger.debug(f"Error extracting credentials from individual log: {e2}")
            
            if creds_extracted > 0:
                logger.info(f"Extracted credentials from {creds_extracted} systems ({processed_count} total logs)")
        except Exception as e:
            logger.warning(f"Error during credential extraction: {e}", exc_info=True)
        
        update_progress('running', f'Credential extraction completed. Committing changes...', 88, total_entries, total_entries)
        
        # CRITICAL: Force commit all changes and close WAL file to ensure data is persisted
        try:
            # Get the connection and commit explicitly
            conn = connections['active_op_db']
            if conn.connection:
                conn.connection.commit()
            # Close connection to force WAL checkpoint
            conn.close()
            conn.connection = None
            # Reopen connection for user sync
            connections.databases['active_op_db']['NAME'] = str(db_path)
        except Exception as e:
            logger.warning(f"Error during commit/flush: {e}", exc_info=True)
        
        # Sync users from default database to operation database
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        # Get all users from default database
        default_users = User.objects.using('default').all()
        
        # Copy each user to the operation database
        for user in default_users:
            # Check if user already exists
            if not User.objects.using('active_op_db').filter(id=user.id).exists():
                new_user = User(
                    id=user.id,
                    username=user.username,
                    password=user.password,
                    is_superuser=user.is_superuser,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    email=user.email,
                    is_staff=user.is_staff,
                    is_active=user.is_active,
                    date_joined=user.date_joined
                )
                new_user.save(using='active_op_db')
        
        update_progress('running', 'Syncing users completed. Creating CSAction entries...', 90, total_entries, total_entries)
        
        # CRITICAL: Retroactively create CSAction entries for BeaconLog entries that don't have them
        # This handles cases where signals didn't fire or failed during import
        try:
            # CRITICAL: Verify database connection before CSAction creation
            ensure_correct_database(db_path, "csaction_creation")
            
            from cobalt_strike_monitor.models import CSAction
            from django.db.models import Max
            
            # Get all BeaconLog entries that don't have a cs_action
            logs_without_action = BeaconLog.objects.using('active_op_db').filter(cs_action__isnull=True).order_by('when')
            total_logs_to_process = logs_without_action.count()
            logger.info(f"JSON IMPORT: Found {total_logs_to_process} BeaconLog entries without CSAction")
            
            actions_created = 0
            logs_associated = 0
            
            # Pre-fetch existing CSActions grouped by beacon to avoid repeated queries
            # This maps beacon_id -> list of (action_id, start_time, accept_output) sorted by start desc
            existing_actions_by_beacon = {}
            for action in CSAction.objects.using('active_op_db').select_related('beacon').order_by('-start'):
                beacon_id = action.beacon_id
                if beacon_id not in existing_actions_by_beacon:
                    existing_actions_by_beacon[beacon_id] = []
                existing_actions_by_beacon[beacon_id].append((action.id, action.start, action.accept_output))
            
            # Process logs in batches for bulk operations
            batch_size = 1000
            actions_to_create = []
            logs_to_update = []
            # Track mapping: (beacon_id, when) -> log_entry for actions we're creating
            action_to_log_map = []
            # Track most recent action per beacon as we process (beacon_id -> (action_id, start_time, accept_output))
            most_recent_actions = {}
            
            # Process logs in chronological order so actions are created before outputs try to associate
            for idx, log_entry in enumerate(logs_without_action):
                # Update progress during CSAction creation (90-98% of total progress)
                if total_logs_to_process > 0 and (idx == 0 or idx % 100 == 0 or idx == total_logs_to_process - 1):
                    progress_pct = 90 + int((idx / total_logs_to_process) * 8) if total_logs_to_process > 0 else 90
                    update_progress('running', f'Creating CSAction entries: {idx}/{total_logs_to_process} ({actions_created} actions, {logs_associated} associated)', progress_pct, total_logs_to_process, idx)
                
                # Manually trigger the signal logic to create CSAction
                # This mimics what the signal does
                # NOTE: Types should be stored without "beacon_" prefix (e.g., "input", "output")
                # but existing data may have the prefix, so we normalize it
                log_type = log_entry.type
                # Remove "beacon_" prefix if present for comparison (signals expect types without prefix)
                normalized_type = log_type[7:] if log_type.startswith("beacon_") else log_type
                
                beacon_id = log_entry.beacon_id
                
                if normalized_type == "input":
                    # Create new action for input
                    new_action = CSAction(start=log_entry.when, beacon_id=beacon_id)
                    if log_entry.data and (log_entry.data.startswith("sleep ") or log_entry.data.startswith("note ")):
                        new_action.accept_output = False
                    actions_to_create.append(new_action)
                    action_to_log_map.append((beacon_id, log_entry.when, log_entry))
                    # Track this as the most recent action for this beacon
                    most_recent_actions[beacon_id] = (None, log_entry.when, new_action.accept_output)  # ID will be set after bulk_create
                    actions_created += 1
                    logs_associated += 1
                elif normalized_type == "task" and log_entry.data and "Tasked beacon to sleep " in log_entry.data:
                    # Check if action already exists for this time window
                    existing_action = None
                    if beacon_id in existing_actions_by_beacon:
                        for action_id, action_start, action_accept_output in existing_actions_by_beacon[beacon_id]:
                            if action_start >= log_entry.when - timedelta(seconds=1) and action_start <= log_entry.when:
                                existing_action = (action_id, action_start, action_accept_output)
                                break
                    
                    if not existing_action:
                        new_action = CSAction(start=log_entry.when, beacon_id=beacon_id)
                        actions_to_create.append(new_action)
                        action_to_log_map.append((beacon_id, log_entry.when, log_entry))
                        most_recent_actions[beacon_id] = (None, log_entry.when, True)
                        actions_created += 1
                        logs_associated += 1
                else:
                    # Associate with most recent action
                    most_recent_action = None
                    
                    # First check in-memory tracking (but only if action has an actual ID, not None)
                    if beacon_id in most_recent_actions:
                        action_id, action_start, action_accept_output = most_recent_actions[beacon_id]
                        # CRITICAL: Only use actions that have actual IDs (not None)
                        # Actions with None IDs haven't been created yet and will be handled after bulk_create
                        if action_id is not None and action_start <= log_entry.when:
                            most_recent_action = (action_id, action_start, action_accept_output)
                    
                    # If not found in memory, check existing actions
                    if not most_recent_action and beacon_id in existing_actions_by_beacon:
                        for action_id, action_start, action_accept_output in existing_actions_by_beacon[beacon_id]:
                            if action_start <= log_entry.when:
                                most_recent_action = (action_id, action_start, action_accept_output)
                                break
                    
                    if most_recent_action:
                        action_id, action_start, action_accept_output = most_recent_action
                        # Only associate if it accepts output (for output/error types)
                        is_output_or_error = (
                            normalized_type.startswith("output") or 
                            normalized_type == "error"
                        )
                        if is_output_or_error:
                            if action_accept_output:
                                log_entry.cs_action_id = action_id
                                logs_to_update.append(log_entry)
                                logs_associated += 1
                        else:
                            log_entry.cs_action_id = action_id
                            logs_to_update.append(log_entry)
                            logs_associated += 1
                
                # Bulk create and update in batches
                if len(actions_to_create) >= batch_size or (idx == total_logs_to_process - 1 and actions_to_create):
                    # CRITICAL: Verify database connection before bulk create
                    ensure_correct_database(db_path, f"csaction_bulk_create_{idx}")
                    
                    # Bulk create CSActions
                    if actions_to_create:
                        created_actions = CSAction.objects.using('active_op_db').bulk_create(actions_to_create)
                        # Match created actions back to log entries
                        for action_idx, (beacon_id, when_time, log_entry) in enumerate(action_to_log_map):
                            if action_idx < len(created_actions):
                                created_action = created_actions[action_idx]
                                log_entry.cs_action_id = created_action.id
                                logs_to_update.append(log_entry)
                                # Update most_recent_actions with actual ID
                                if beacon_id in most_recent_actions:
                                    old_id, start, accept_output = most_recent_actions[beacon_id]
                                    if old_id is None and start == when_time:
                                        most_recent_actions[beacon_id] = (created_action.id, start, accept_output)
                                else:
                                    # If not in most_recent_actions, add it
                                    most_recent_actions[beacon_id] = (created_action.id, when_time, created_action.accept_output)
                        actions_to_create = []
                        action_to_log_map = []
                        
                        # CRITICAL: After creating actions, check if any pending logs need to be associated
                        # This handles logs that came after an action was created but before bulk_create
                        # We need to re-process logs that might have been skipped because action_id was None
                        # Actually, this is handled by the next iteration - logs processed after this will see the updated IDs
                
                # Bulk update BeaconLog entries
                if len(logs_to_update) >= batch_size or (idx == total_logs_to_process - 1 and logs_to_update):
                    # CRITICAL: Verify database connection before bulk update
                    ensure_correct_database(db_path, f"beaconlog_bulk_update_{idx}")
                    
                    if logs_to_update:
                        # Filter out any with None cs_action_id (shouldn't happen, but safety check)
                        valid_logs = [log for log in logs_to_update if log.cs_action_id is not None]
                        if valid_logs:
                            BeaconLog.objects.using('active_op_db').bulk_update(valid_logs, ['cs_action_id'])
                        logs_to_update = []
            
            if actions_created > 0 or logs_associated > 0:
                logger.info(f"Created {actions_created} new CSAction entries and associated {logs_associated} BeaconLog entries")
            
            # CRITICAL: Second pass - associate any remaining logs that don't have a cs_action
            # This handles logs that were processed before their action was created
            update_progress('running', 'Associating remaining logs with CSActions...', 96, total_entries, total_entries)
            try:
                ensure_correct_database(db_path, "csaction_second_pass")
                
                # Get all logs that still don't have a cs_action
                remaining_logs = BeaconLog.objects.using('active_op_db').filter(
                    team_server=team_server,
                    cs_action__isnull=True
                ).order_by('when')
                
                if remaining_logs.exists():
                    logger.info(f"JSON IMPORT: Second pass - found {remaining_logs.count()} logs still without CSAction")
                    
                    # Get all CSActions grouped by beacon, sorted by start time DESCENDING
                    all_actions_by_beacon = {}
                    for action in CSAction.objects.using('active_op_db').select_related('beacon').order_by('-start'):
                        beacon_id = action.beacon_id
                        if beacon_id not in all_actions_by_beacon:
                            all_actions_by_beacon[beacon_id] = []
                        all_actions_by_beacon[beacon_id].append((action.id, action.start, action.accept_output))
                    
                    # Associate each remaining log with the most recent action for its beacon
                    logs_to_update_second_pass = []
                    for log_entry in remaining_logs:
                        beacon_id = log_entry.beacon_id
                        if beacon_id in all_actions_by_beacon:
                            # Find the most recent action that started before or at this log's time
                            # Actions are sorted by start DESCENDING, so first match is most recent
                            most_recent_action = None
                            for action_id, action_start, action_accept_output in all_actions_by_beacon[beacon_id]:
                                if action_start <= log_entry.when:
                                    most_recent_action = (action_id, action_start, action_accept_output)
                                    break  # First match is most recent (sorted DESC)
                            
                            if most_recent_action:
                                action_id, action_start, action_accept_output = most_recent_action
                                # Only associate if it accepts output (for output/error types)
                                normalized_type = log_entry.type[7:] if log_entry.type.startswith("beacon_") else log_entry.type
                                is_output_or_error = (
                                    normalized_type.startswith("output") or 
                                    normalized_type == "error"
                                )
                                if is_output_or_error:
                                    if action_accept_output:
                                        log_entry.cs_action_id = action_id
                                        logs_to_update_second_pass.append(log_entry)
                                else:
                                    log_entry.cs_action_id = action_id
                                    logs_to_update_second_pass.append(log_entry)
                    
                    # Bulk update in batches
                    if logs_to_update_second_pass:
                        batch_size_second = 1000
                        for i in range(0, len(logs_to_update_second_pass), batch_size_second):
                            batch = logs_to_update_second_pass[i:i + batch_size_second]
                            ensure_correct_database(db_path, f"csaction_second_pass_batch_{i}")
                            valid_logs = [log for log in batch if log.cs_action_id is not None]
                            if valid_logs:
                                BeaconLog.objects.using('active_op_db').bulk_update(valid_logs, ['cs_action_id'])
                        
                        logger.info(f"Second pass: Associated {len(logs_to_update_second_pass)} additional logs with CSActions")
            except Exception as e:
                logger.warning(f"Error in second pass CSAction association: {e}", exc_info=True)
            
            # CRITICAL: Link Archive entries to CSActions
            # Archive entries share the same ID as their corresponding BeaconLog entries
            # So we can match them up and link Archives to the same CSAction as their BeaconLog
            update_progress('running', 'Linking Archive entries to CSActions...', 97, total_entries, total_entries)
            try:
                # CRITICAL: Verify database connection before Archive linking
                ensure_correct_database(db_path, "archive_linking")
                
                # Get all Archive entries that don't have a cs_action
                archives_without_action = Archive.objects.using('active_op_db').filter(
                    team_server=team_server,
                    cs_action__isnull=True
                )
                
                # Get all BeaconLog entries with cs_action_id set, indexed by ID
                beacon_logs_with_action = {
                    log.id: log.cs_action_id 
                    for log in BeaconLog.objects.using('active_op_db').filter(
                        team_server=team_server,
                        cs_action__isnull=False
                    ).only('id', 'cs_action_id')
                }
                
                # Update Archive entries in batches
                archive_batch_size = 1000
                archives_to_update = []
                archives_linked = 0
                
                for archive in archives_without_action:
                    # Find corresponding BeaconLog by ID
                    if archive.id in beacon_logs_with_action:
                        archive.cs_action_id = beacon_logs_with_action[archive.id]
                        archives_to_update.append(archive)
                        archives_linked += 1
                    
                    # Bulk update in batches
                    if len(archives_to_update) >= archive_batch_size:
                        # Verify database connection before bulk update
                        ensure_correct_database(db_path, f"archive_bulk_update_{archives_linked}")
                        Archive.objects.using('active_op_db').bulk_update(archives_to_update, ['cs_action_id'])
                        archives_to_update = []
                
                # Update remaining archives
                if archives_to_update:
                    # Verify database connection before final bulk update
                    ensure_correct_database(db_path, "archive_final_bulk_update")
                    Archive.objects.using('active_op_db').bulk_update(archives_to_update, ['cs_action_id'])
                
                if archives_linked > 0:
                    logger.info(f"Linked {archives_linked} Archive entries to CSActions")
            except Exception as e:
                logger.warning(f"Error linking Archive entries to CSActions: {e}", exc_info=True)
            
            # Final progress update for CSAction creation
            if total_logs_to_process > 0:
                update_progress('running', f'CSAction creation completed: {actions_created} actions, {logs_associated} associated', 98, total_logs_to_process, total_logs_to_process)
        except Exception as e:
            logger.warning(f"Error creating missing CSAction entries: {e}", exc_info=True)
        
        stats = {
            'beacons': len(beacons),
            'log_entries': created_count,
            'team_server': team_server.hostname
        }
        
        # Update progress: completed
        update_progress('completed', f'Import completed! {created_count} log entries for {len(beacons)} beacon(s)', 100, total_entries, total_entries)
        
        if is_ui_call:
            # Return tuple for UI
            return True, f"Successfully imported {created_count} log entries for {len(beacons)} beacon(s)", stats
        else:
            # Print for CLI
            print(f"\nImport complete!")
            print(f"  Database: {db_path}")
            print(f"  TeamServer: {team_server.hostname}")
            print(f"  Beacons: {len(beacons)}")
            print(f"  Log entries: {created_count}")
            print(f"\nTo import this database into Stepping Stones:")
            print(f"  1. Copy {db_path} to your Stepping Stones ops-data directory")
            print(f"  2. Create an operation with the name '{db_path_or_name}' in Stepping Stones")
            print(f"  3. The database will be automatically associated with the operation")
    
    except Exception as e:
        # If called from UI, return error tuple
        import traceback
        import logging
        
        logger = logging.getLogger(__name__)
        logger.error(f"Error in import_json_to_database: {str(e)}", exc_info=True)
        
        # Update progress: error
        if progress_id:
            try:
                from django.core.cache import cache
                cache.set(f'import_progress_{progress_id}', {
                    'status': 'error',
                    'message': f'Import failed: {str(e)}',
                    'progress': 0,
                    'total': 0,
                    'current': 0
                }, timeout=300)
            except:
                pass
        
        if hasattr(json_file_or_path, 'read') and isinstance(db_path_or_name, Path):
            import traceback
            error_traceback = traceback.format_exc()
            error_msg = f"Error importing JSON: {str(e)}\n{error_traceback}"
            logger.error(f"Full traceback: {error_traceback}")
            return False, error_msg, {}
        # If called from CLI, re-raise
        raise


def _setup_django_for_cli():
    """Setup Django environment for command-line usage."""
    # Add the project directory to the path so we can import Django settings
    BASE_DIR = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(BASE_DIR))
    
    # Setup minimal environment variables if not set
    if 'DJANGO_SECRET_KEY' not in os.environ:
        os.environ['DJANGO_SECRET_KEY'] = 'django-insecure-temporary-key-for-import-script-only'
    if 'DJANGO_DEBUG' not in os.environ:
        os.environ['DJANGO_DEBUG'] = 'False'
    if 'DJANGO_ALLOWED_HOSTS' not in os.environ:
        os.environ['DJANGO_ALLOWED_HOSTS'] = '*'
    
    # Work around environ module conflict
    import site
    import importlib.util
    
    # First, try to find and import django-environ package directly
    django_environ_imported = False
    for site_packages in site.getsitepackages():
        django_environ_init = Path(site_packages) / 'django_environ' / '__init__.py'
        if django_environ_init.exists():
            try:
                spec = importlib.util.spec_from_file_location("django_environ", django_environ_init)
                if spec and spec.loader:
                    django_environ_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(django_environ_module)
                    sys.modules['environ'] = django_environ_module
                    django_environ_imported = True
                    break
            except Exception:
                pass
    
    # If that didn't work, try importing it normally
    if not django_environ_imported:
        try:
            import django_environ
            sys.modules['environ'] = django_environ
        except ImportError:
            # Create a minimal environ mock
            class EnvInstance:
                def __call__(self, key, default=None, cast=None):
                    value = os.environ.get(key, default)
                    if cast:
                        return cast(value)
                    return value
                @staticmethod
                def read_env():
                    pass
            
            class EnvClass:
                def __new__(cls):
                    return EnvInstance()
                @staticmethod
                def read_env():
                    pass
            
            class MinimalEnviron:
                Env = EnvClass
            
            sys.modules['environ'] = MinimalEnviron()
    
    # Setup Django environment
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'stepping_stones.settings')
    
    import django
    django.setup()
    
    # Now import Django components
    global connections, models, call_command, make_aware, tz, dateparser
    global TeamServer, Beacon, BeaconLog, Listener, Archive, CSAction
    from django.db import connections, models
    from django.core.management import call_command
    from django.utils.timezone import make_aware
    from datetime import timezone as tz
    import dateparser
    from cobalt_strike_monitor.models import TeamServer, Beacon, BeaconLog, Listener, Archive, CSAction


def main():
    """Main entry point for command-line usage."""
    # Setup Django for CLI usage
    _setup_django_for_cli()
    
    parser = argparse.ArgumentParser(
        description='Convert Cobalt Strike beacon logs from JSON to Stepping Stones SQLite database'
    )
    parser.add_argument('json_file', type=Path, help='Path to input JSON file')
    parser.add_argument('db_name', type=str, help='Name for the output database (without .sqlite3 extension)')
    parser.add_argument('--operation-name', type=str, help='Operation name for description (defaults to db_name)')
    
    args = parser.parse_args()
    
    if not args.json_file.exists():
        print(f"Error: JSON file not found: {args.json_file}")
        sys.exit(1)
    
    try:
        import_json_to_database(
            args.json_file,
            args.db_name,
            args.operation_name or args.db_name
        )
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

