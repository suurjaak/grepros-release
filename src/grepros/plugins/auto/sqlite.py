# -*- coding: utf-8 -*-
"""
SQLite output for search results.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     03.12.2021
@modified    21.12.2021
------------------------------------------------------------------------------
"""
## @namespace grepros.plugins.auto.sqlite
import collections
import json
import os
import sqlite3

from ... common import ConsolePrinter, format_bytes, makedirs, quote
from ... import rosapi
from . dbbase import DataSinkBase


class SqliteSink(DataSinkBase):
    """
    Writes messages to an SQLite database.

    Output will have:
    - table "messages", with all messages as serialized binary
    - table "types", with message definitions
    - table "topics", with topic information

    plus:
    - table "pkg/MsgType" for each message type, with detailed fields,
      and JSON fields for arrays of nested subtypes,
      with foreign keys if nesting else subtype values as JSON dictionaries;
      plus underscore-prefixed fields for metadata, like `_topic` as the topic name.
      If not nesting, only topic message type tables are created.
    - view "/topic/full/name" for each topic,
      selecting from the message type table

    """

    ## Database engine name
    ENGINE = "SQLite"

    ## Placeholder for positional arguments in SQL statement
    POSARG = "?"

    ## Auto-detection file extensions
    FILE_EXTENSIONS = (".sqlite", ".sqlite3")

    ## Number of emits between commits; 0 is autocommit
    COMMIT_INTERVAL = 1000

    ## SQL statements for populating database base schema
    BASE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS messages (
      id           INTEGER   PRIMARY KEY,
      topic_id     INTEGER   NOT NULL,
      timestamp    INTEGER   NOT NULL,
      data         BLOB      NOT NULL,

      topic        TEXT      NOT NULL,
      type         TEXT      NOT NULL,
      dt           TIMESTAMP NOT NULL,
      yaml         TEXT      NOT NULL
    );

    CREATE TABLE IF NOT EXISTS types (
      id            INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
      type          TEXT    NOT NULL,
      definition    TEXT    NOT NULL,
      md5           TEXT    NOT NULL,
      table_name    TEXT    NOT NULL,
      nested_tables JSON
    );

    CREATE TABLE IF NOT EXISTS topics (
      id                   INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
      name                 TEXT    NOT NULL,
      type                 TEXT    NOT NULL,
      serialization_format TEXT    DEFAULT "cdr",
      offered_qos_profiles TEXT    DEFAULT "",

      table_name           TEXT    NOT NULL,
      view_name            TEXT,
      md5                  TEXT    NOT NULL,
      count                INTEGER NOT NULL DEFAULT 0,
      dt_first             TIMESTAMP,
      dt_last              TIMESTAMP,
      timestamp_first      INTEGER,
      timestamp_last       INTEGER
    );

    CREATE INDEX IF NOT EXISTS timestamp_idx ON messages (timestamp ASC);

    PRAGMA journal_mode = WAL;
    """

    ## SQL statement for inserting messages
    INSERT_MESSAGE = """
    INSERT INTO messages (topic_id, timestamp, data, topic, type, dt, yaml)
    VALUES (:topic_id, :timestamp, :data, :topic, :type, :dt, :yaml)
    """

    ## SQL statement for inserting topics
    INSERT_TOPIC = """
    INSERT INTO topics (name, type, md5, table_name)
    VALUES (:name, :type, :md5, :table_name)
    """

    ## SQL statement for updating topics with latest message
    UPDATE_TOPIC = """
    UPDATE topics SET count = count + 1,
    dt_first = MIN(COALESCE(dt_first, :dt), :dt),
    dt_last  = MAX(COALESCE(dt_last,  :dt), :dt),
    timestamp_first = MIN(COALESCE(timestamp_first, :timestamp), :timestamp),
    timestamp_last  = MAX(COALESCE(timestamp_last,  :timestamp), :timestamp)
    WHERE name = :name AND type = :type
    """

    ## SQL statement for updating view name in topic
    UPDATE_TOPIC_VIEW = """
    UPDATE topics SET view_name = :view_name
    WHERE id = :id
    """

    ## SQL statement for inserting types
    INSERT_TYPE = """
    INSERT INTO types (type, definition, md5, table_name)
    VALUES (:type, :definition, :md5, :table_name)
    """

    ## SQL statement for creating a table for type
    CREATE_TYPE_TABLE = """
    DROP TABLE IF EXISTS %(name)s;

    CREATE TABLE %(name)s (%(cols)s);
    """


    def __init__(self, args):
        """
        @param   args                arguments object like argparse.Namespace
        @param   args.META           whether to print metainfo
        @param   args.DUMP_TARGET    name of SQLite file to write,
                                     will be appended to if exists
        @param   args.DUMP_OPTIONS   {"commit-interval": transaction size (0 is autocommit),
                                      "message-yaml": populate messages.yaml (default true),
                                      "nesting": "array" to recursively insert arrays
                                                 of nested types, or "all" for any nesting)}
        @param   args.VERBOSE        whether to print debug information
        """
        super(SqliteSink, self).__init__(args)

        self._filename    = args.DUMP_TARGET
        self._do_yaml     = (args.DUMP_OPTIONS.get("message-yaml") != "false")
        self._id_counters = {}  # {table next: max ID}


    def validate(self):
        """
        Returns "commit-interval" and "nesting" in args.DUMP_OPTIONS have valid value, if any;
        parses "message-yaml" from args.DUMP_OPTIONS.
        """
        config_ok = super(SqliteSink, self).validate()
        if self._args.DUMP_OPTIONS.get("message-yaml") not in (None, "true", "false"):
            ConsolePrinter.error("Invalid message-yaml option for %s: %r. "
                                 "Choose one of {true, false}.",
                                 self.ENGINE, self._args.DUMP_OPTIONS["message-yaml"])
            config_ok = False
        return config_ok


    def _init_db(self):
        """Opens the database file and populates schema if not already existing."""
        for t in (dict, list, tuple): sqlite3.register_adapter(t, json.dumps)
        sqlite3.register_converter("JSON", json.loads)
        if self._args.VERBOSE:
            sz = os.path.exists(self._filename) and os.path.getsize(self._filename)
            ConsolePrinter.debug("%s %s%s.", "Adding to" if sz else "Creating", self._filename,
                                 (" (%s)" % format_bytes(sz)) if sz else "")
        super(SqliteSink, self)._init_db()


    def _load_schema(self):
        """Populates instance attributes with schema metainfo."""
        super(SqliteSink, self)._load_schema()
        for row in self._db.execute("SELECT name FROM sqlite_master "
                                    "WHERE type = 'table' AND name LIKE '%/%'"):
            cols = self._db.execute("PRAGMA table_info(%s)" % quote(row["name"])).fetchall()
            typerow = next(x for x in self._types.values() if x["table_name"] == row["name"])
            typekey = (typerow["type"], typerow["md5"])
            self._schema[typekey] = collections.OrderedDict([(c["name"], c) for c in cols])


    def _process_message(self, topic, msg, stamp):
        """Inserts message to messages-table, and to pkg/MsgType tables."""
        typename = rosapi.get_message_type(msg)
        typehash = self.source.get_message_type_hash(msg)
        topic_id = self._topics[(topic, typename, typehash)]["id"]
        margs = dict(dt=rosapi.to_datetime(stamp), timestamp=rosapi.to_nsec(stamp),
                     topic=topic, name=topic, topic_id=topic_id, type=typename,
                     yaml=str(msg) if self._do_yaml else "", data=rosapi.get_message_data(msg))
        self._ensure_execute(self.INSERT_MESSAGE, margs)
        self._ensure_execute(self.UPDATE_TOPIC,   margs)
        super(SqliteSink, self)._process_message(topic, msg, stamp)


    def _connect(self):
        """Returns new database connection."""
        makedirs(os.path.dirname(self._filename))
        db = sqlite3.connect(self._filename, check_same_thread=False,
                             detect_types=sqlite3.PARSE_DECLTYPES)
        if not self.COMMIT_INTERVAL: db.isolation_level = None
        db.row_factory = lambda cursor, row: dict(sqlite3.Row(cursor, row))
        return db


    def _execute_insert(self, sql, args):
        """Executes INSERT statement, returns inserted ID."""
        return self._cursor.execute(sql, args).lastrowid


    def _executemany(self, sql, argses):
        """Executes SQL with all args sequences."""
        self._cursor.executemany(sql, argses)


    def _executescript(self, sql):
        """Executes SQL with one or more statements."""
        self._cursor.executescript(sql)


    def _get_next_id(self, table):
        """Returns next ID value for table, using simple auto-increment."""
        if not self._id_counters.get(table):
            sql = "SELECT COALESCE(MAX(_id), 0) AS id FROM %s" % quote(table)
            self._id_counters[table] = self._db.execute(sql).fetchone()["id"]
        self._id_counters[table] += 1
        return self._id_counters[table]



def init(*_, **__):
    """Adds SQLite output format support."""
    from ... import plugins  # Late import to avoid circular
    plugins.add_write_format("sqlite", SqliteSink, "SQLite", [
        ("commit-interval=NUM",      "transaction size for SQLite output\n"
                                     "(default 1000, 0 is autocommit)"),
        ("message-yaml=true|false",  "whether to populate table field messages.yaml\n"
                                     "in SQLite output (default true)"),
        ("nesting=array|all",        "create tables for nested message types\n"
                                     "in SQLite output,\n"
                                     'only for arrays if "array" \n'
                                     "else for any nested types\n"
                                     "(array fields in parent will be populated \n"
                                     " with foreign keys instead of messages as JSON)"),
    ])
    writearg = plugins.get_argument("--write-format")
    if writearg:
        writearg["help"] = writearg["help"].replace("bag will be", "bag or database will be")
