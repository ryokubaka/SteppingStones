from django.db import migrations


def remove_operation_fk_from_active_op_credentials(apps, schema_editor):
    """
    Some existing operation databases may have an event_tracker_credential table
    that was created with a FOREIGN KEY constraint on operation_id pointing to
    event_tracker_operation.

    In operation-specific databases we only need operation_id as a nullable
    column for ORM compatibility and uniqueness; we DO NOT want an actual FK
    constraint to event_tracker_operation, because:
    - Operation objects live in the default database
    - The operation table in active_op_db is a stub created only to satisfy
      SQLite's requirement that the table exists

    This migration detects such a FK (if present) and rebuilds the
    event_tracker_credential table in active_op_db without it.
    """
    db_alias = schema_editor.connection.alias
    if db_alias == "default":
        # Only touch operation databases
        return

    conn = schema_editor.connection

    with conn.cursor() as cursor:
        # Check if the credential table exists at all
        cursor.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='event_tracker_credential'
            """
        )
        if cursor.fetchone() is None:
            return

        # Inspect foreign keys on the credential table
        cursor.execute("PRAGMA foreign_key_list('event_tracker_credential')")
        fk_rows = cursor.fetchall()

        # Look for any FK that targets event_tracker_operation
        has_operation_fk = any(
            len(row) >= 3 and row[2] == "event_tracker_operation" for row in fk_rows
        )

        if not has_operation_fk:
            # Nothing to do – table already without FK
            return

        # Rebuild the table without the FK.
        # We mirror the schema created in 0090_credential_operation_specific
        # (no FK constraint on operation_id).

        # Disable foreign key enforcement while we reshape the table
        cursor.execute("PRAGMA foreign_keys = OFF")
        try:
            # Create a new table with the desired schema
            cursor.execute(
                """
                CREATE TABLE event_tracker_credential_new (
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
                """
            )

            # Copy all existing data into the new table
            cursor.execute(
                """
                INSERT INTO event_tracker_credential_new (
                    id,
                    operation_id,
                    source,
                    source_time,
                    system,
                    account,
                    secret,
                    hash,
                    hash_type,
                    purpose,
                    complexity,
                    char_mask,
                    char_mask_effort,
                    structure,
                    enabled,
                    haveibeenpwned_count,
                    cracking_parameters
                )
                SELECT
                    id,
                    operation_id,
                    source,
                    source_time,
                    system,
                    account,
                    secret,
                    hash,
                    hash_type,
                    purpose,
                    complexity,
                    char_mask,
                    char_mask_effort,
                    structure,
                    enabled,
                    haveibeenpwned_count,
                    cracking_parameters
                FROM event_tracker_credential
                """
            )

            # Drop the old table and rename the new one into place
            cursor.execute("DROP TABLE event_tracker_credential")
            cursor.execute(
                "ALTER TABLE event_tracker_credential_new RENAME TO event_tracker_credential"
            )

            # Recreate indexes (same as in 0090_credential_operation_specific)
            try:
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS event_tracker_credential_hash_idx "
                    "ON event_tracker_credential(hash)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS event_tracker_credential_account_hash_idx "
                    "ON event_tracker_credential(account, hash)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS event_tracker_credential_system_account_idx "
                    "ON event_tracker_credential(system, account)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS event_tracker_credential_secret_hash_idx "
                    "ON event_tracker_credential(secret, hash)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS event_tracker_credential_operation_id_idx "
                    "ON event_tracker_credential(operation_id)"
                )
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        event_tracker_credential_operation_system_account_hash_hash_type_uniq
                    ON event_tracker_credential(operation_id, system, account, hash, hash_type)
                    """
                )
            except Exception:
                # If indexes already exist or something minor fails, don't abort the migration
                pass
        finally:
            # Re-enable FK enforcement
            cursor.execute("PRAGMA foreign_keys = ON")


def noop_reverse(apps, schema_editor):
    # Reversing this would require reintroducing the FK, which we explicitly
    # do not want, so treat as a no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("event_tracker", "0092_create_operation_table_in_active_op_db"),
    ]

    operations = [
        migrations.RunPython(
            remove_operation_fk_from_active_op_credentials,
            noop_reverse,
        ),
    ]


