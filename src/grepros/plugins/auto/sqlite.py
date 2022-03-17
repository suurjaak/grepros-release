# -*- coding: utf-8 -*-
"""
SQLite output for search results.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     03.12.2021
@modified    06.02.2022
------------------------------------------------------------------------------
"""
## @namespace grepros.plugins.auto.sqlite
import collections
import json
import os
import sqlite3
import sys

from ... common import ConsolePrinter, format_bytes, makedirs
from ... import rosapi
from . dbbase import DataSinkBase, quote


class SqliteSink(DataSinkBase):
    """
    Writes messages to an SQLite database.

    Output will have:
    - table "messages", with all messages as serialized binary data
    - table "types", with message definitions
    - table "topics", with topic information

    plus:
    - table "pkg/MsgType" for each message type, with detailed fields,
      and JSON fields for arrays of nested subtypes,
      with foreign keys if nesting else subtype values as JSON dictionaries;
      plus underscore-prefixed fields for metadata, like `_topic` as the topic name.

      If launched with nesting-option, tables will also be created for each
      nested message type.

    - view "/topic/full/name" for each topic,
      selecting from the message type table

    """

    ## Database engine name
    ENGINE = "SQLite"

    ## Auto-detection file extensions
    FILE_EXTENSIONS = (".sqlite", ".sqlite3")

    ## Maximum integer size supported in SQLite, higher values inserted as string
    MAX_INT = 2**63 - 1


    def __init__(self, args):
        """
        @param   args                 arguments object like argparse.Namespace
        @param   args.META            whether to print metainfo
        @param   args.WRITE           name of SQLite file to write, will be appended to if exists
        @param   args.WRITE_OPTIONS   {"commit-interval": transaction size (0 is autocommit),
                                       "message-yaml": populate messages.yaml (default true),
                                       "nesting": "array" to recursively insert arrays
                                                  of nested types, or "all" for any nesting),
                                       "overwrite": whether to overwrite existing file
                                                    (default false)}
        @param   args.VERBOSE         whether to print debug information
        """
        super(SqliteSink, self).__init__(args)

        self._filename    = args.WRITE
        self._do_yaml     = (args.WRITE_OPTIONS.get("message-yaml") != "false")
        self._overwrite   = (args.WRITE_OPTIONS.get("overwrite") == "true")
        self._id_counters = {}  # {table next: max ID}


    def validate(self):
        """
        Returns "commit-interval" and "nesting" in args.WRITE_OPTIONS have valid value, if any;
        parses "message-yaml" from args.WRITE_OPTIONS.
        """
        config_ok = super(SqliteSink, self).validate()
        if self.args.WRITE_OPTIONS.get("message-yaml") not in (None, "true", "false"):
            ConsolePrinter.error("Invalid message-yaml option for %s: %r. "
                                 "Choose one of {true, false}.",
                                 self.ENGINE, self.args.WRITE_OPTIONS["message-yaml"])
            config_ok = False
        if self.args.WRITE_OPTIONS.get("overwrite") not in (None, "true", "false"):
            ConsolePrinter.error("Invalid overwrite option for %s: %r. "
                                 "Choose one of {true, false}.",
                                 self.ENGINE, self.args.WRITE_OPTIONS["overwrite"])
            config_ok = False
        return config_ok


    def _init_db(self):
        """Opens the database file and populates schema if not already existing."""
        for t in (dict, list, tuple): sqlite3.register_adapter(t, json.dumps)
        sqlite3.register_adapter(int, lambda x: str(x) if abs(x) > self.MAX_INT else x)
        if sys.version_info < (3, ):
            sqlite3.register_adapter(long, lambda x: str(x) if abs(x) > self.MAX_INT else x)
        sqlite3.register_converter("JSON", json.loads)
        if self.args.VERBOSE:
            sz = os.path.exists(self._filename) and os.path.getsize(self._filename)
            action = "Overwriting" if sz and self._overwrite else \
                     "Appending to" if sz else "Creating"
            ConsolePrinter.debug("%s %s%s.", action, self._filename,
                                 (" (%s)" % format_bytes(sz)) if sz else "")
        super(SqliteSink, self)._init_db()


    def _load_schema(self):
        """Populates instance attributes with schema metainfo."""
        super(SqliteSink, self)._load_schema()
        for row in self._db.execute("SELECT name FROM sqlite_master "
                                    "WHERE type = 'table' AND name LIKE '%/%'"):
            cols = self._db.execute("PRAGMA table_info(%s)" % quote(row["name"])).fetchall()
            typerow = next((x for x in self._types.values()
                            if x["table_name"] == row["name"]), None)
            if not typerow: continue  # for row
            typekey = (typerow["type"], typerow["md5"])
            self._schema[typekey] = collections.OrderedDict([(c["name"], c) for c in cols])


    def _process_message(self, topic, msg, stamp):
        """Inserts message to messages-table, and to pkg/MsgType tables."""
        with rosapi.TypeMeta.make(msg, topic) as m:
            topic_id, typename = self._topics[m.topickey]["id"], m.typename
        margs = dict(dt=rosapi.to_datetime(stamp), timestamp=rosapi.to_nsec(stamp),
                     topic=topic, name=topic, topic_id=topic_id, type=typename,
                     yaml=str(msg) if self._do_yaml else "", data=rosapi.get_message_data(msg))
        self._ensure_execute(self._get_dialect_option("insert_message"), margs)
        super(SqliteSink, self)._process_message(topic, msg, stamp)


    def _connect(self):
        """Returns new database connection."""
        makedirs(os.path.dirname(self._filename))
        if self._overwrite: open(self._filename, "w").close()
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
        ("dialect-file=path/to/dialects.yaml",
                                     "load additional SQL dialect options\n"
                                     "for SQLite output\n"
                                     "from a YAML or JSON file"),
        ("message-yaml=true|false",  "whether to populate table field messages.yaml\n"
                                     "in SQLite output (default true)"),
        ("nesting=array|all",        "create tables for nested message types\n"
                                     "in SQLite output,\n"
                                     'only for arrays if "array" \n'
                                     "else for any nested types\n"
                                     "(array fields in parent will be populated \n"
                                     " with foreign keys instead of messages as JSON)"),
        ("overwrite=true|false",     "overwrite existing file in SQLite output\n"
                                     "instead of appending to file (default false)")
    ])
