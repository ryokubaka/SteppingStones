from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import connections

User = get_user_model()

class Command(BaseCommand):
    help = 'Synchronizes users from the default database to the active operation database'

    def handle(self, *args, **options):
        # Get all users from the default database
        with connections['default'].cursor() as cursor:
            cursor.execute("SELECT id, username, password, is_superuser, first_name, last_name, email, is_staff, is_active, date_joined FROM auth_user")
            users = cursor.fetchall()

        # Insert users into the active operation database
        with connections['active_op_db'].cursor() as cursor:
            # First, clear existing users in the operation database
            cursor.execute("DELETE FROM auth_user")
            
            # Insert users from default database
            for user in users:
                cursor.execute("""
                    INSERT INTO auth_user (id, username, password, is_superuser, first_name, last_name, 
                                         email, is_staff, is_active, date_joined)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, user)

        self.stdout.write(self.style.SUCCESS('Successfully synchronized users between databases')) 