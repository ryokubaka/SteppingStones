from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.db import connections

class Command(BaseCommand):
    help = 'Run migrations on the default database for event_tracker app'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE('Running migrations on default database...'))
        
        # Close any existing connections to ensure clean state
        connections.close_all()
        
        # Run migrations for event_tracker app on default database
        try:
            call_command('migrate', 'event_tracker', database='default')
            self.stdout.write(self.style.SUCCESS('Successfully migrated event_tracker app on default database'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error running migrations: {e}'))
            raise 