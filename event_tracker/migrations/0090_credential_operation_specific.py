# Generated manually to create credential table in operation databases

from django.db import migrations
from django.db import connection


def create_credential_table_if_not_exists(apps, schema_editor):
    """
    Create the credential table in operation databases if it doesn't exist.
    This is needed because credential was previously only in the default database,
    but is now operation-specific.
    """
    # Only run this on operation databases (active_op_db), not on default
    db_alias = schema_editor.connection.alias
    if db_alias == 'default':
        # Skip on default database - table should already exist there
        return
    
    # Use the schema_editor's connection
    with schema_editor.connection.cursor() as cursor:
        # Check if table already exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='event_tracker_credential'
        """)
        table_exists = cursor.fetchone() is not None
        
        if table_exists:
            # Table exists - check if operation_id column exists
            cursor.execute("PRAGMA table_info(event_tracker_credential)")
            columns = [row[1] for row in cursor.fetchall()]  # row[1] is column name
            
            if 'operation_id' not in columns:
                # Table exists but missing operation_id column - add it
                # This handles existing op databases created before migration 0088
                cursor.execute("ALTER TABLE event_tracker_credential ADD COLUMN operation_id INTEGER NULL")
                # Create index for operation_id if it doesn't exist
                try:
                    cursor.execute("CREATE INDEX IF NOT EXISTS event_tracker_credential_operation_id_idx ON event_tracker_credential(operation_id)")
                except Exception:
                    pass  # Index might already exist
                # Update unique constraint to include operation_id
                # First drop old unique constraint if it exists (we'll recreate it)
                try:
                    cursor.execute("DROP INDEX IF EXISTS event_tracker_credential_system_account_hash_hash_type_uniq")
                except Exception:
                    pass
                # Create new unique constraint with operation_id
                try:
                    cursor.execute("""
                        CREATE UNIQUE INDEX IF NOT EXISTS event_tracker_credential_operation_system_account_hash_hash_type_uniq 
                        ON event_tracker_credential(operation_id, system, account, hash, hash_type)
                    """)
                except Exception:
                    pass  # Constraint might already exist
            # Table exists and has operation_id, nothing to do
            return
        
        # Create the table with all fields
        # Note: SQLite doesn't support UNSIGNED or separate BIGINT, uses INTEGER for all integers
        # Note: Foreign key to event_tracker_operation is not included because Operation is in the
        # default database and SQLite cannot enforce cross-database foreign keys. The field is
        # kept for Django ORM compatibility, but the constraint is handled by the router.
        # Use IF NOT EXISTS to be safe
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS event_tracker_credential (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id INTEGER NULL,
                source VARCHAR(200) NULL,
                source_time DATETIME NULL,
                system VARCHAR(200) NULL COLLATE NOCASE,
                account VARCHAR(200) NOT NULL COLLATE NOCASE,
                secret VARCHAR(200) NULL,
                hash VARCHAR(10000) NULL COLLATE NOCASE,
                hash_type INTEGER NULL,
                purpose VARCHAR(100) NULL,
                complexity VARCHAR(30) NULL,
                char_mask VARCHAR(400) NULL,
                char_mask_effort INTEGER NULL,
                structure VARCHAR(100) NULL,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                haveibeenpwned_count INTEGER NULL,
                cracking_parameters VARCHAR(500) NULL
            )
        """)
        
        # Create indexes (using IF NOT EXISTS for safety)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS event_tracker_credential_hash_idx ON event_tracker_credential(hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS event_tracker_credential_account_hash_idx ON event_tracker_credential(account, hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS event_tracker_credential_system_account_idx ON event_tracker_credential(system, account)")
            cursor.execute("CREATE INDEX IF NOT EXISTS event_tracker_credential_secret_hash_idx ON event_tracker_credential(secret, hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS event_tracker_credential_operation_id_idx ON event_tracker_credential(operation_id)")
            
            # Create unique constraint
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS event_tracker_credential_operation_system_account_hash_hash_type_uniq 
                ON event_tracker_credential(operation_id, system, account, hash, hash_type)
            """)
        except Exception as e:
            # If indexes already exist, that's fine - continue
            pass


def reverse_create_credential_table(apps, schema_editor):
    """
    Reverse migration - drop the credential table from operation databases.
    Note: This will delete all credential data in operation databases!
    """
    db_alias = schema_editor.connection.alias
    if db_alias == 'default':
        return
    
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS event_tracker_credential")


class Migration(migrations.Migration):
    """
    This migration creates the credential table in operation databases.
    
    The Credential model has been moved from the shared (default) database 
    to operation-specific databases. The database router has been updated 
    to route Credential queries to 'active_op_db' instead of 'default'.
    
    This migration ensures that the credential table exists in operation
    databases with the correct structure, indexes, and constraints.
    """

    dependencies = [
        ('event_tracker', '0089_alter_credential_hash'),
    ]

    operations = [
        migrations.RunPython(
            create_credential_table_if_not_exists,
            reverse_create_credential_table,
        ),
    ]

