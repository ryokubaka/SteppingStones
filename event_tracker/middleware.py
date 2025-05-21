from django.contrib.auth import get_user
from django.contrib.auth.models import AnonymousUser, User
from django.core.management import execute_from_command_line, CommandError
from django.shortcuts import redirect
from django.urls import reverse, resolve
from django.urls.exceptions import Resolver404
from django.utils import timezone
from django.conf import settings
from django.db import connections, DEFAULT_DB_ALIAS
import os
import logging
from django.contrib import messages
import sqlite3
from django.db import transaction

from event_tracker.fixtures import gen_mitre_fixture
# Operation model will be imported inside the middleware to avoid circular imports at startup
# from event_tracker.models import UserPreferences, Task, AttackTactic, Operation
from event_tracker.models import UserPreferences, AttackTactic # Task and Operation removed for now

logger = logging.getLogger(__name__)

# Define a list of URLs that should be accessible without an active operation
# (e.g., login, logout, operation selection/creation, admin)
ACCESSIBLE_WITHOUT_OP_URL_NAMES = [
    'login', 'logout', 'admin:index', 'admin:login', 'admin:logout',
    'event_tracker:select_operation', 'event_tracker:create_operation',
    'event_tracker:initial-config-admin',
    'event_tracker:initial-config-task', # Needs to be accessible if op active, no tasks
    'event_tracker:user-preferences',
    'event_tracker:activate_operation', # This view activates and then redirects
]

# URLs allowed when an operation is active but has NO tasks yet.
# User MUST create a task before accessing other op-specific parts of the site.
ALLOWED_URLS_WHEN_NO_TASKS = [
    'event_tracker:initial-config-task',
    'event_tracker:user-preferences', # In case TimezoneMiddleware redirects here
    'logout', # Allow logout
    'admin:logout',
    # Consider if any admin pages are essential here, or if task creation is paramount.
    # For strict enforcement, limit to as few as possible.
]

def _initialize_operation_db(operation_name, ops_data_dir):
    """
    Checks if the operation database file exists. If not, creates it and runs migrations.
    Returns: (bool: success, str: message)
    """
    db_path = ops_data_dir / f"{operation_name}.sqlite3"
    new_db_created = False # Flag to track if we are initializing a brand new DB

    logger.debug(f"Initializing operation database:")
    logger.debug(f"- Operation name: {operation_name}")
    logger.debug(f"- Database path: {db_path}")
    logger.debug(f"- Current active_op_db settings: {connections['active_op_db'].settings_dict}")
    logger.debug(f"- Database file exists: {db_path.exists()}")

    # First, ensure we're not in any atomic transactions and have clean connections
    try:
        # Close any existing connections
        if 'active_op_db' in connections:
            connections['active_op_db'].close()
            connections['active_op_db'].connection = None
        
        # Update database settings
        connections.databases['active_op_db'] = {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(db_path),
            'OPTIONS': {'timeout': 20},
            'ATOMIC_REQUESTS': False,
            'CONN_MAX_AGE': 0,
            'AUTOCOMMIT': True,
            'TIME_ZONE': settings.TIME_ZONE,  # Add TIME_ZONE setting
            'CONN_HEALTH_CHECKS': False,  # Disable connection health checks
        }
        logger.debug(f"Updated active_op_db settings to: {connections['active_op_db'].settings_dict}")
    except Exception as e:
        logger.error(f"Error setting up database connections: {e}")
        return False, f"Error setting up database connections: {e}"

    if not db_path.exists():
        logger.info(f"Database for operation '{operation_name}' not found at {db_path}. Will be initialized.")
        new_db_created = True
        try:
            # Ensure the directory exists (it should, but defensive)
            ops_data_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Placeholder for database '{db_path}' ensured (or will be created by Django). ")

            from django.core.management import call_command
            from io import StringIO
            
            # First run migrations for core Django apps
            core_apps = [
                'contenttypes', # For django_content_type
                'auth',         # For auth_permission (needed by contenttypes and others)
                'admin',        # For admin_log (if used per-op, router allows logentry)
                'sessions',     # For session management
            ]

            # Then run migrations for our apps in dependency order
            our_apps = [
                'event_tracker',
                'cobalt_strike_monitor',
                'taggit',
                'djangoplugins',
                'reversion',
                'background_task'
            ]

            # Run migrations for core apps first
            for app_name in core_apps:
                migration_output_buffer = StringIO()
                logger.info(f"Running migrations for core app '{app_name}' on new op database: {db_path}")
                try:
                    call_command(
                        'migrate',
                        app_name,
                        database='active_op_db',
                        verbosity=1,
                        stdout=migration_output_buffer,
                        stderr=migration_output_buffer
                    )
                    output = migration_output_buffer.getvalue()
                    logger.info(f"Migrations output for core app '{app_name}' on '{operation_name}' (db: {db_path}):\n{output}")
                    if "Error" in output or "Traceback" in output:
                        logger.error(f"Migrations for core app '{app_name}' on '{operation_name}' failed. Output:\n{output}")
                        return False, f"Migration failed for core app {app_name}: {output[:200]}..."
                except CommandError as ce:
                    logger.error(f"CommandError during migration of core app '{app_name}' for op '{operation_name}': {ce}")
                    logger.error(f"Output buffer for '{app_name}':\n{migration_output_buffer.getvalue()}")
                    return False, f"CommandError during migration for core app {app_name}: {ce}"
                finally:
                    migration_output_buffer.close()

            # Then run migrations for our apps
            for app_name in our_apps:
                migration_output_buffer = StringIO()
                logger.info(f"Running migrations for app '{app_name}' on new op database: {db_path}")
                try:
                    # First run showmigrations to check what migrations are pending
                    showmigrations_buffer = StringIO()
                    call_command(
                        'showmigrations',
                        app_name,
                        database='active_op_db',
                        verbosity=1,
                        stdout=showmigrations_buffer,
                        stderr=showmigrations_buffer
                    )
                    showmigrations_output = showmigrations_buffer.getvalue()
                    logger.info(f"Pending migrations for app '{app_name}':\n{showmigrations_output}")

                    # Then run the actual migration
                    call_command(
                        'migrate',
                        app_name,
                        database='active_op_db',
                        verbosity=1,
                        stdout=migration_output_buffer,
                        stderr=migration_output_buffer
                    )
                    output = migration_output_buffer.getvalue()
                    logger.info(f"Migrations output for app '{app_name}' on '{operation_name}' (db: {db_path}):\n{output}")
                    if "Error" in output or "Traceback" in output:
                        logger.error(f"Migrations for app '{app_name}' on '{operation_name}' failed. Output:\n{output}")
                        return False, f"Migration failed for {app_name}: {output[:200]}..."
                except CommandError as ce:
                    logger.error(f"CommandError during migration of app '{app_name}' for op '{operation_name}': {ce}")
                    logger.error(f"Output buffer for '{app_name}':\n{migration_output_buffer.getvalue()}")
                    return False, f"CommandError during migration for {app_name}: {ce}"
                finally:
                    migration_output_buffer.close()
                    showmigrations_buffer.close()

            # Sync users from default database to operation database
            try:
                logger.info("Syncing users from default database to operation database")
                from django.contrib.auth import get_user_model
                User = get_user_model()
                
                # Get all users from default database
                default_users = User.objects.using('default').all()
                logger.debug(f"Found {default_users.count()} users in default database")
                
                # Copy each user to the operation database
                for user in default_users:
                    logger.debug(f"Copying user {user.username} (ID: {user.id}) to operation database")
                    # Create a new user object for the operation database
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
                
                # Verify the copy was successful
                op_users = User.objects.using('active_op_db').all()
                logger.debug(f"Verification: Found {op_users.count()} users in operation database after sync")
                for user in op_users:
                    logger.debug(f"Operation database user: {user.username} (ID: {user.id})")
            except Exception as e:
                logger.error(f"Error syncing users to operation database: {e}")
                return False, f"Error syncing users: {e}"

            logger.info(f"Successfully initialized and explicitly migrated apps for new database '{operation_name}'.")
            return True, f"New database for {operation_name} initialized and migrated."

        except Exception as e:
            logger.error(f"Unexpected error initializing new database for operation '{operation_name}': {e}", exc_info=True)
            return False, f"Unexpected error during new DB init: {e}"
    else: # Database already exists
        logger.debug(f"Database for operation '{operation_name}' already exists at {db_path}. Verifying connection setup.")
        # Try verification, and if CONN_HEALTH_CHECKS error, recover and retry once
        for attempt in range(2):
            try:
                logger.debug("Verifying database connection")
                with connections['active_op_db'].cursor() as cursor:
                    # First check if CONN_HEALTH_CHECKS table exists
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='CONN_HEALTH_CHECKS'")
                    if not cursor.fetchone():
                        if attempt == 0:
                            logger.warning("CONN_HEALTH_CHECKS table missing, creating it now for self-recovery and retrying verification.")
                            cursor.execute("""
                                CREATE TABLE IF NOT EXISTS CONN_HEALTH_CHECKS (
                                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    checked_at TEXT
                                )
                            """)
                            # Commit and close connection to ensure table is visible
                            connections['active_op_db'].commit()
                            connections['active_op_db'].close()
                            continue  # Retry verification
                        else:
                            logger.error("CONN_HEALTH_CHECKS table still missing after creation attempt.")
                            return False, "CONN_HEALTH_CHECKS table missing after creation attempt."
                    
                    # Now safe to select from the table
                    cursor.execute("SELECT COUNT(*) FROM CONN_HEALTH_CHECKS")
                    
                    # Check for teamserver table
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cobalt_strike_monitor_teamserver'")
                    if not cursor.fetchone():
                        logger.error(f"Required table 'cobalt_strike_monitor_teamserver' not found in existing database")
                        return False, "Required table not found in existing database"
                    logger.debug("TeamServer table found in existing database")
                    
                    # List all servers in the database
                    cursor.execute("SELECT id, description FROM cobalt_strike_monitor_teamserver")
                    servers = cursor.fetchall()
                    logger.debug(f"Found {len(servers)} team servers in existing database: {servers}")

                    # Check for users
                    cursor.execute("SELECT COUNT(*) FROM auth_user")
                    user_count = cursor.fetchone()[0]
                    logger.debug(f"Found {user_count} users in operation database")
                    
                    if user_count == 0:
                        logger.info("No users found in operation database, syncing from default database")
                        from django.contrib.auth import get_user_model
                        User = get_user_model()
                        default_users = User.objects.using('default').all()
                        logger.debug(f"Found {default_users.count()} users in default database")
                        for user in default_users:
                            logger.debug(f"Copying user {user.username} (ID: {user.id}) to operation database")
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
                        op_users = User.objects.using('active_op_db').all()
                        logger.debug(f"Verification: Found {op_users.count()} users in operation database after sync")
                        for user in op_users:
                            logger.debug(f"Operation database user: {user.username} (ID: {user.id})")
                
                logger.info("Successfully verified database")
                return True, "Database already exists."
            except Exception as e:
                logger.error(f"Error verifying existing database: {e}", exc_info=True)
                if attempt == 0:
                    logger.warning("First verification attempt failed, will retry once")
                    # Close and reopen connection before retry
                    connections['active_op_db'].close()
                    connections['active_op_db'].connection = None
                    continue
                return False, f"Error verifying existing database: {e}"

class OperationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        logger.info("[DEBUG] OperationMiddleware: INIT")
        # One-time configuration and initialization.
        # Ensure ops-data directory exists
        if not settings.OPS_DATA_DIR.exists():
            try:
                settings.OPS_DATA_DIR.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created ops-data directory at {settings.OPS_DATA_DIR}")
            except OSError as e:
                logger.error(f"Error creating ops-data directory at {settings.OPS_DATA_DIR}: {e}")
                # Potentially raise an ImproperlyConfigured error or handle as critical

    def __call__(self, request):
        from event_tracker.models import Operation, Task # Ensure Task is imported
        logger.info(f"[DEBUG] OperationMiddleware: CALL for path {request.path}")

        current_url_name = None
        if request.resolver_match:
            current_url_name = request.resolver_match.view_name
            logger.info(f"[DEBUG] OperationMiddleware: Using request.resolver_match.view_name: '{current_url_name}' for path {request.path}")
        else:
            logger.info(f"[DEBUG] OperationMiddleware: request.resolver_match is None for path {request.path}. Attempting manual resolve using request.path_info.")
            try:
                match = resolve(request.path_info)
                current_url_name = match.view_name
                logger.info(f"[DEBUG] OperationMiddleware: Manual resolve for '{request.path_info}' set view_name to: '{current_url_name}'")
            except Resolver404:
                logger.warning(f"[DEBUG] OperationMiddleware: Manual resolve for '{request.path_info}' FAILED with Resolver404. current_url_name remains None.")
            except Exception as e:
                logger.error(f"[DEBUG] OperationMiddleware: Manual resolve for '{request.path_info}' FAILED with an unexpected Exception: {e}. current_url_name remains None.")

        active_operation_name = request.session.get('active_operation_name')
        logger.info(f"[DEBUG] OperationMiddleware: active_operation_name='{active_operation_name}', current_url_name='{current_url_name}'")
        request.current_operation = None

        if active_operation_name:
            logger.info(f"[DEBUG] OperationMiddleware: Active operation is '{active_operation_name}'.")
            try:
                operation = Operation.objects.using(DEFAULT_DB_ALIAS).get(name=active_operation_name)
                request.current_operation = operation
                db_path = settings.OPS_DATA_DIR / f"{operation.name}.sqlite3"
                
                # Update connection settings for 'active_op_db' for this request
                connections['active_op_db'].settings_dict['NAME'] = str(db_path)
                
                # Call the standalone initialization function
                initialized, message = _initialize_operation_db(operation.name, settings.OPS_DATA_DIR)
                if not initialized:
                    # Log the error and potentially clear the session or redirect
                    logger.error(f"Failed to initialize DB for op '{operation.name}' during middleware: {message}")
                    request.session.pop('active_operation_name', None)
                    request.session.pop('active_operation_display_name', None)
                    messages.error(request, f"Database initialization failed for {operation.name}: {message}")
                    return redirect(reverse('event_tracker:select_operation')) 

                logger.info(f"[DEBUG] OperationMiddleware: DB initialized for '{operation.name}'. Set active_op_db to '{db_path}'.")

                # === Task Creation Enforcement ===
                if not Task.objects.using('active_op_db').exists():
                    logger.info(f"[DEBUG] OperationMiddleware: Op '{operation.name}' is active but has NO tasks.")
                    if current_url_name not in ALLOWED_URLS_WHEN_NO_TASKS:
                        # Check if it's an admin URL part of ACCESSIBLE_WITHOUT_OP_URL_NAMES (like admin:index)
                        # to prevent redirect loops if admin is trying to fix something.
                        # However, initial-config-task is the priority.
                        if not (current_url_name and current_url_name.startswith('admin:') and current_url_name in ACCESSIBLE_WITHOUT_OP_URL_NAMES):
                            messages.warning(request, "An initial task must be created for the active operation before proceeding.")
                            logger.info(f"[DEBUG] OperationMiddleware: Redirecting to 'initial-config-task'. Current URL '{current_url_name}' not in ALLOWED_URLS_WHEN_NO_TASKS.")
                            return redirect(reverse('event_tracker:initial-config-task'))
                        else:
                            logger.info(f"[DEBUG] OperationMiddleware: Allowing admin URL '{current_url_name}' even with no tasks, as it is in ACCESSIBLE_WITHOUT_OP_URL_NAMES.") 
                    else:
                        logger.info(f"[DEBUG] OperationMiddleware: Current URL '{current_url_name}' is in ALLOWED_URLS_WHEN_NO_TASKS. Passing request through.")
                else:
                    logger.info(f"[DEBUG] OperationMiddleware: Op '{operation.name}' has tasks. Proceeding normally.")
                # === End Task Creation Enforcement ===

            except Operation.DoesNotExist:
                logger.warning(f"[DEBUG] OperationMiddleware: Operation '{active_operation_name}' DoesNotExist. Clearing from session.")
                request.session.pop('active_operation_name', None) # Clear invalid op name
                # Check if redirect is needed (similar logic to 'else' block below)
                if current_url_name not in ACCESSIBLE_WITHOUT_OP_URL_NAMES and not (current_url_name and current_url_name.startswith('admin:')):
                    logger.info(f"[DEBUG] OperationMiddleware: Redirecting to 'event_tracker:select_operation' because current_url_name '{current_url_name}' not whitelisted after invalid op.")
                    return redirect(reverse('event_tracker:select_operation'))
                logger.info(f"[DEBUG] OperationMiddleware: Current URL '{current_url_name}' is whitelisted or admin after invalid op. Passing request through.")
            except Exception as e:
                logger.error(f"[DEBUG] OperationMiddleware: Error setting up operation DB for '{active_operation_name}': {e}. Clearing from session.")
                request.session.pop('active_operation_name', None)
                if current_url_name not in ACCESSIBLE_WITHOUT_OP_URL_NAMES and not (current_url_name and current_url_name.startswith('admin:')):
                    logger.info(f"[DEBUG] OperationMiddleware: Redirecting to 'event_tracker:select_operation' because current_url_name '{current_url_name}' not whitelisted after DB error.")
                    return redirect(reverse('event_tracker:select_operation'))
                logger.info(f"[DEBUG] OperationMiddleware: Current URL '{current_url_name}' is whitelisted or admin after DB error. Passing request through.")
        else: # No active operation in session
            logger.info(f"[DEBUG] OperationMiddleware: No active operation in session.")
            # Allow access to specific URLs, otherwise redirect to select operation page
            is_admin_path = current_url_name and current_url_name.startswith('admin:')
            if current_url_name not in ACCESSIBLE_WITHOUT_OP_URL_NAMES and not is_admin_path:
                logger.info(f"[DEBUG] OperationMiddleware: current_url_name '{current_url_name}' is not in ACCESSIBLE_WITHOUT_OP_URL_NAMES and not admin path.")
                if request.user.is_authenticated:
                    logger.info(f"[DEBUG] OperationMiddleware: User is authenticated. Redirecting to 'event_tracker:select_operation'.")
                    return redirect(reverse('event_tracker:select_operation'))
                else:
                    logger.info(f"[DEBUG] OperationMiddleware: User is not authenticated. Passing request through for potential login redirect.")
            else:
                logger.info(f"[DEBUG] OperationMiddleware: current_url_name '{current_url_name}' is whitelisted or admin path. Passing request through.")

        response = self.get_response(request)
        return response

class TimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        logger.info("[DEBUG] TimezoneMiddleware: INIT")

    def __call__(self, request):
        logger.info(f"[DEBUG] TimezoneMiddleware: CALL for path {request.path}")
        user = get_user(request)

        if isinstance(user, AnonymousUser):
            logger.info("[DEBUG] TimezoneMiddleware: User is Anonymous. Deactivating timezone. Passing request through.")
            timezone.deactivate()
        else:
            logger.info(f"[DEBUG] TimezoneMiddleware: User is '{user.username}'. Checking preferences.")
            preferences = UserPreferences.objects.filter(user=user).first()
            if preferences and preferences.timezone:
                logger.info(f"[DEBUG] TimezoneMiddleware: Found preferences with timezone '{preferences.timezone}'. Activating. Passing request through.")
                timezone.activate(preferences.timezone)
            else:
                if preferences:
                    logger.info(f"[DEBUG] TimezoneMiddleware: Found preferences object, but preferences.timezone is '{preferences.timezone}'.")
                else:
                    logger.info("[DEBUG] TimezoneMiddleware: No preferences object found.")
                
                logger.info("[DEBUG] TimezoneMiddleware: Deactivating timezone.")
                timezone.deactivate()
                
                user_prefs_url = reverse('event_tracker:user-preferences')
                logger.info(f"[DEBUG] TimezoneMiddleware: Current path is '{request.get_full_path()}', user_prefs_url is '{user_prefs_url}'.")
                if request.get_full_path() != user_prefs_url:
                    logger.info(f"[DEBUG] TimezoneMiddleware: Redirecting to '{user_prefs_url}'.")
                    return redirect(user_prefs_url)
                else:
                    logger.info("[DEBUG] TimezoneMiddleware: Already on user-preferences page. Passing request through.")

        response = self.get_response(request)
        return response


class InitialConfigMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        print("[DEBUG] InitialConfigMiddleware: INIT")

    def __call__(self, request):
        print(f"[DEBUG] InitialConfigMiddleware: CALL for path {request.path}")

        # Attempt manual resolution for debugging
        try:
            manual_match = resolve(request.path_info) # Use request.path_info
            print(f"[DEBUG] InitialConfigMiddleware: Manual resolve for {request.path_info} SUCCESS: view_name='{manual_match.view_name}', func={manual_match.func}")
        except Resolver404 as e:
            print(f"[DEBUG] InitialConfigMiddleware: Manual resolve for {request.path_info} FAILED with Resolver404: {e}")
        except Exception as e:
            print(f"[DEBUG] InitialConfigMiddleware: Manual resolve for {request.path_info} FAILED with other Exception: {e}")

        current_url_name = None
        if request.resolver_match:
            current_url_name = request.resolver_match.view_name
            print(f"[DEBUG] InitialConfigMiddleware: Using request.resolver_match.view_name: {current_url_name} for path {request.path}")
        else:
            # request.resolver_match is None, attempt to resolve manually
            print(f"[DEBUG] InitialConfigMiddleware: request.resolver_match is None for path {request.path}. Attempting manual resolve using request.path_info.")
            try:
                match = resolve(request.path_info)
                current_url_name = match.view_name
                print(f"[DEBUG] InitialConfigMiddleware: Manual resolve for '{request.path_info}' set view_name to: '{current_url_name}'")
            except Resolver404:
                print(f"[DEBUG] InitialConfigMiddleware: Manual resolve for '{request.path_info}' FAILED with Resolver404. current_url_name remains None.")
            except Exception as e:
                print(f"[DEBUG] InitialConfigMiddleware: Manual resolve for '{request.path_info}' FAILED with an unexpected Exception: {e}. current_url_name remains None.")

        if current_url_name in ['event_tracker:initial-config-admin', 'event_tracker:initial-config-task']:
            print(f"[DEBUG] InitialConfigMiddleware: Allowing {current_url_name} to pass for path {request.path}.")
            return self.get_response(request)

        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            users_exist = User.objects.using(DEFAULT_DB_ALIAS).exists()
            user_count = User.objects.using(DEFAULT_DB_ALIAS).count()
            print(f"[DEBUG] InitialConfigMiddleware: Users exist check for {request.path}: {users_exist}, Count: {user_count}")
            if not users_exist:
                print(f"[DEBUG] InitialConfigMiddleware: No users found (count={user_count}), redirecting {request.path} to initial-config-admin.")
                return redirect(reverse('event_tracker:initial-config-admin'))
        except Exception as e:
            print(f"[DEBUG] InitialConfigMiddleware: EXCEPTION checking users for {request.path}: {e}")
            print(f"[DEBUG] InitialConfigMiddleware: EXCEPTION, redirecting {request.path} to initial-config-admin due to error.")
            return redirect(reverse('event_tracker:initial-config-admin'))

        print(f"[DEBUG] InitialConfigMiddleware: Users exist. Proceeding normally for {request.path} (MITRE check skipped for this debug).")
        return self.get_response(request)

    def _initialize_operation_db(self, request):
        """Initialize the operation database if needed."""
        try:
            # Get the current operation
            current_op = CurrentOperation.objects.using('default').first()
            if not current_op:
                return

            # Get the operation database path
            op_db_path = current_op.operation.db_path
            if not op_db_path:
                return

            # Check if the database exists
            if not os.path.exists(op_db_path):
                # Create the database directory if it doesn't exist
                os.makedirs(os.path.dirname(op_db_path), exist_ok=True)
                
                # Create a new SQLite database
                conn = sqlite3.connect(op_db_path)
                conn.close()
                
                # Run migrations for the new database
                call_command('migrate', '--database=active_op_db')
                
                # Sync users from default database to operation database
                call_command('sync_users')
                
                logger.info(f"Created new operation database at {op_db_path}")
            else:
                # Database exists, ensure it's up to date
                call_command('migrate', '--database=active_op_db')
                # Sync users to ensure they're up to date
                call_command('sync_users')
                
            # Set the database path in settings
            settings.DATABASES['active_op_db']['NAME'] = op_db_path
            
            # Set the current operation in the request
            request.current_operation = current_op.operation
            
        except Exception as e:
            logger.error(f"Error initializing operation database: {e}")
            raise