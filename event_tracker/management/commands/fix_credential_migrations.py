from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
from django.db import connections
from pathlib import Path


class Command(BaseCommand):
    help = 'Fix migration state for credential table in operation databases by removing incorrect migration records'

    def add_arguments(self, parser):
        parser.add_argument(
            '--operation',
            type=str,
            help='Specific operation database to fix (by name, e.g., test412). If not provided, fixes all operation databases.',
        )

    def handle(self, *args, **options):
        ops_data_dir = getattr(settings, 'OPS_DATA_DIR', None)
        if not ops_data_dir:
            self.stdout.write(self.style.WARNING('OPS_DATA_DIR not set in settings. Skipping.'))
            return

        ops_data_dir = Path(ops_data_dir)
        if not ops_data_dir.exists():
            self.stdout.write(self.style.WARNING(f'OPS_DATA_DIR {ops_data_dir} does not exist.'))
            return

        op_dbs = list(ops_data_dir.glob('*.sqlite3'))
        if options['operation']:
            op_dbs = [db for db in op_dbs if db.stem == options['operation']]

        if not op_dbs:
            self.stdout.write(self.style.WARNING('No operation databases found.'))
            return

        for op_db in op_dbs:
            if op_db.name == '_placeholder_op.sqlite3':
                continue

            self.stdout.write(self.style.NOTICE(f'Fixing migrations for: {op_db.name}'))

            # Update the active_op_db connection
            connections.close_all()
            connections.databases['active_op_db']['NAME'] = str(op_db)
            if hasattr(connections['active_op_db'], 'settings_dict'):
                connections['active_op_db'].settings_dict['NAME'] = str(op_db)
            connections['active_op_db'].connection = None

            try:
                from django.db import connections
                op_connection = connections['active_op_db']
                with op_connection.cursor() as cursor:
                    # Check if django_migrations table exists
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_migrations'")
                    if not cursor.fetchone():
                        self.stdout.write(self.style.WARNING(f'  django_migrations table does not exist in {op_db.name}. Running all migrations...'))
                        call_command('migrate', 'event_tracker', database='active_op_db', verbosity=1)
                        continue

                    # Check if credential table exists
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_tracker_credential'")
                    table_exists = cursor.fetchone() is not None

                    # Check what migrations are recorded
                    cursor.execute("SELECT name FROM django_migrations WHERE app='event_tracker' ORDER BY id")
                    recorded_migrations = [row[0] for row in cursor.fetchall()]

                    if not table_exists and recorded_migrations:
                        self.stdout.write(self.style.WARNING(f'  Credential table missing but migrations recorded: {recorded_migrations[:5]}...'))
                        self.stdout.write(self.style.NOTICE(f'  Removing incorrect migration records for event_tracker...'))
                        
                        # Remove all event_tracker migration records
                        cursor.execute("DELETE FROM django_migrations WHERE app='event_tracker'")
                        op_connection.commit()
                        
                        self.stdout.write(self.style.SUCCESS(f'  Removed migration records. Now running migrations from scratch...'))
                        # Now run migrations from the beginning
                        call_command('migrate', 'event_tracker', database='active_op_db', verbosity=1)
                    elif not table_exists:
                        self.stdout.write(self.style.NOTICE(f'  No migration records found. Running all migrations...'))
                        call_command('migrate', 'event_tracker', database='active_op_db', verbosity=1)
                    else:
                        self.stdout.write(self.style.SUCCESS(f'  Credential table exists. Checking if migrations are up to date...'))
                        call_command('migrate', 'event_tracker', database='active_op_db', verbosity=1)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  Error fixing {op_db.name}: {e}'))
                continue

        self.stdout.write(self.style.SUCCESS('Migration fix completed.'))

