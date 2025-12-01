from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
from django.db import connections
from pathlib import Path

class Command(BaseCommand):
    help = 'Run makemigrations for background_task, then migrate all apps on the default DB and all op DBs in ops-data.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE('Running makemigrations for background_task...'))
        call_command('makemigrations', 'background_task')

        # First run migrations for core Django apps on default database
        core_apps = [
            'contenttypes', # For django_content_type
            'auth',         # For auth_permission (needed by contenttypes and others)
            'admin',        # For admin_log (if used per-op, router allows logentry)
            'sessions',     # For session management
        ]

        # Then run migrations for our apps in dependency order on default database
        our_apps = [
            'event_tracker',
            'cobalt_strike_monitor',
            'taggit',
            'djangoplugins',
            'reversion',
            'background_task'
        ]

        self.stdout.write(self.style.NOTICE('Migrating all apps on default database...'))
        for app_name in core_apps + our_apps:
            try:
                call_command('migrate', app_name, database='default', verbosity=1)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'Error migrating {app_name} on default DB: {e}'))

        ops_data_dir = getattr(settings, 'OPS_DATA_DIR', None)
        if not ops_data_dir:
            self.stdout.write(self.style.WARNING('OPS_DATA_DIR not set in settings. Skipping op DB migration.'))
            return

        ops_data_dir = Path(ops_data_dir)
        if not ops_data_dir.exists():
            self.stdout.write(self.style.WARNING(f'OPS_DATA_DIR {ops_data_dir} does not exist.'))
            return

        op_dbs = list(ops_data_dir.glob('*.sqlite3'))
        if not op_dbs:
            self.stdout.write(self.style.WARNING('No op DBs found in ops-data.'))
            return

        for op_db in op_dbs:
            # Skip placeholder database
            if op_db.name == '_placeholder_op.sqlite3':
                continue
                
            self.stdout.write(self.style.NOTICE(f'Migrating all apps on op DB: {op_db.name}'))
            # Update the active_op_db alias
            connections.close_all()
            settings.DATABASES['active_op_db']['NAME'] = str(op_db)
            if hasattr(connections['active_op_db'], 'settings_dict'):
                connections['active_op_db'].settings_dict['NAME'] = str(op_db)
            connections['active_op_db'].connection = None

            # Run migrations for core apps first
            for app_name in core_apps:
                try:
                    call_command('migrate', app_name, database='active_op_db', verbosity=1)
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f'Error migrating {app_name} on {op_db.name}: {e}'))

            # Then run migrations for our apps
            for app_name in our_apps:
                try:
                    call_command('migrate', app_name, database='active_op_db', verbosity=1)
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f'Error migrating {app_name} on {op_db.name}: {e}'))
        
        self.stdout.write(self.style.SUCCESS('All migrations applied to all databases.')) 