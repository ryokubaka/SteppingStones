from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
from django.db import connections
import os
from pathlib import Path

class Command(BaseCommand):
    help = 'Run makemigrations background_task, then migrate background_task on the default DB and all op DBs in ops-data.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE('Running makemigrations for background_task...'))
        call_command('makemigrations', 'background_task')

        self.stdout.write(self.style.NOTICE('Migrating background_task on default database...'))
        call_command('migrate', 'background_task', database='default')

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
            self.stdout.write(self.style.NOTICE(f'Migrating background_task on op DB: {op_db}'))
            # Update the active_op_db alias
            connections.close_all()
            settings.DATABASES['active_op_db']['NAME'] = str(op_db)
            if hasattr(connections['active_op_db'], 'settings_dict'):
                connections['active_op_db'].settings_dict['NAME'] = str(op_db)
            connections['active_op_db'].connection = None
            call_command('migrate', 'background_task', database='active_op_db')
        self.stdout.write(self.style.SUCCESS('background_task migrations applied to all databases.')) 