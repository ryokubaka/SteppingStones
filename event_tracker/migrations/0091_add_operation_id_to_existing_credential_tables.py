# Generated manually to add operation_id column to existing credential tables in operation databases

from django.db import migrations


def add_operation_id_column_if_missing(apps, schema_editor):
    """
    Add operation_id column to existing credential tables in operation databases if it's missing.
    This handles operation databases that were created before migration 0088 added the operation field.
    """
    # Only run this on operation databases (active_op_db), not on default
    db_alias = schema_editor.connection.alias
    if db_alias == 'default':
        # Skip on default database - column should already exist there from migration 0088
        return
    
    # Use the schema_editor's connection
    with schema_editor.connection.cursor() as cursor:
        # Check if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='event_tracker_credential'
        """)
        if not cursor.fetchone():
            # Table doesn't exist, nothing to do (migration 0090 should have created it)
            return
        
        # Check if operation_id column exists
        cursor.execute("PRAGMA table_info(event_tracker_credential)")
        columns = [row[1] for row in cursor.fetchall()]  # row[1] is column name
        
        if 'operation_id' not in columns:
            # Table exists but missing operation_id column - add it
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


def reverse_add_operation_id_column(apps, schema_editor):
    """
    Reverse migration - remove operation_id column.
    Note: SQLite doesn't support DROP COLUMN directly, so this is a no-op.
    In practice, you'd need to recreate the table, but we won't do that in reverse.
    """
    # SQLite doesn't support DROP COLUMN, so we can't easily reverse this
    # This is acceptable since operation_id is needed for the new model structure
    pass


class Migration(migrations.Migration):
    """
    This migration adds the operation_id column to existing credential tables in operation databases.
    
    Some operation databases were created before migration 0088 added the operation field.
    This migration ensures those databases have the operation_id column added.
    """

    dependencies = [
        ('event_tracker', '0090_credential_operation_specific'),
    ]

    operations = [
        migrations.RunPython(
            add_operation_id_column_if_missing,
            reverse_add_operation_id_column,
        ),
    ]

