import os.path
import duckdb

if os.path.exists("db.sqlite3"):
    duckdb.sql("SET global extension_directory='/opt/steppingstones/duckdb_extensions'")
    duckdb.sql("INSTALL sqlite; LOAD sqlite; SET GLOBAL sqlite_all_varchar=true;")
    duckdb.sql("CALL sqlite_attach('db.sqlite3');")
