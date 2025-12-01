from django.db import migrations


def create_operation_table_in_active_op_db(apps, schema_editor):
    """
    Ensure that a minimal event_tracker_operation table exists in operation databases.

    Background:
    - The Credential model has a ForeignKey to Operation.
    - In the default DB, Operation lives in the event_tracker app as normal.
    - In operation-specific DBs (active_op_db), we do NOT actually use Operation objects,
      but SQLite will still try to enforce the FK and will error if the referenced table
      doesn't exist at all.

    To avoid "no such table: main.event_tracker_operation" errors when inserting into
    event_tracker_credential in active_op_db, we create a minimal stub
    event_tracker_operation table there. We don't rely on its contents; it only exists
    to satisfy SQLite's FK machinery.
    """
    db_alias = schema_editor.connection.alias
    if db_alias == "default":
        # Only touch operation-specific databases
        return

    with schema_editor.connection.cursor() as cursor:
        # Check if the table already exists
        cursor.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='event_tracker_operation'
            """
        )
        exists = cursor.fetchone() is not None
        if exists:
            return

        # Create a minimal Operation table. This matches the core fields used in the
        # default DB model but we don't add any extra constraints here.
        cursor.execute(
            """
            CREATE TABLE event_tracker_operation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                display_name VARCHAR(200) NOT NULL
            )
            """
        )


def reverse_create_operation_table_in_active_op_db(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    if db_alias == "default":
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS event_tracker_operation")


class Migration(migrations.Migration):

    dependencies = [
        ("event_tracker", "0091_add_operation_id_to_existing_credential_tables"),
    ]

    operations = [
        migrations.RunPython(
            create_operation_table_in_active_op_db,
            reverse_create_operation_table_in_active_op_db,
        ),
    ]


