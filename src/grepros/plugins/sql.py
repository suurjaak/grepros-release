# -*- coding: utf-8 -*-
"""
SQL schema output for search results.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     20.12.2021
@modified    04.02.2022
------------------------------------------------------------------------------
"""
## @namespace grepros.plugins.sql
import atexit
import collections
import datetime
import os
import sys

from .. import rosapi
from .. common import ConsolePrinter, format_bytes, makedirs, plural, unique_path
from .. outputs import SinkBase
from . auto.sqlbase import SqlMixin



class SqlSink(SinkBase, SqlMixin):
    """
    Writes SQL schema file for message type tables and topic views.

    Output will have:
    - table "pkg/MsgType" for each topic message type, with ordinary columns for
      scalar fields, and structured columns for list fields;
      plus underscore-prefixed fields for metadata, like `_topic` as the topic name.

      If launched with nesting-option, tables will also be created for each
      nested message type.

    - view "/full/topic/name" for each topic, selecting from the message type table
    """

    ## Auto-detection file extensions
    FILE_EXTENSIONS = (".sql", )

    ## Default columns for message type tables, as [(column name, ROS type)]
    MESSAGE_TYPE_BASECOLS  = [("_topic",      "string"),
                              ("_timestamp",  "time"), ]


    def __init__(self, args):
        """
        @param   args                 arguments object like argparse.Namespace
        @param   args.WRITE_OPTIONS   {"dialect": SQL dialect if not default,
                                       "nesting": true|false to created nested type tables,
                                       "overwrite": whether to overwrite existing file
                                                    (default false)}
        """
        super(SqlSink, self).__init__(args)
        SqlMixin.__init__(self, args)

        self._filename      = None   # Unique output filename
        self._file          = None   # Open file() object
        self._batch         = None   # Current source batch
        self._nested_types  = {}     # {(typename, typehash): "CREATE TABLE .."}
        self._batch_metas   = []     # [source batch metainfo string, ]
        self._overwrite     = (args.WRITE_OPTIONS.get("overwrite") == "true")
        self._close_printed = False

        # Whether to create tables for nested message types,
        # "array" if to do this only for arrays of nested types, or
        # "all" for any nested type, including those fully flattened into parent fields.
        self._nesting = args.WRITE_OPTIONS.get("nesting")

        atexit.register(self.close)


    def validate(self):
        """
        Returns whether "dialect" and "nesting" and "overwrite" parameters contain supported values.
        """
        ok, sqlconfig_ok = True, SqlMixin.validate(self)
        if self.args.WRITE_OPTIONS.get("nesting") not in (None, "array", "all"):
            ConsolePrinter.error("Invalid nesting option for SQL: %r. "
                                 "Choose one of {array,all}.",
                                 self.args.WRITE_OPTIONS["nesting"])
            ok = False
        if self.args.WRITE_OPTIONS.get("overwrite") not in (None, "true", "false"):
            ConsolePrinter.error("Invalid overwrite option for SQL: %r. "
                                 "Choose one of {true, false}.",
                                 self.args.WRITE_OPTIONS["overwrite"])
            ok = False
        return sqlconfig_ok and ok


    def emit(self, topic, index, stamp, msg, match):
        """Writes out message type CREATE TABLE statements to SQL schema file."""
        batch = self.source.get_batch()
        if not self._batch_metas or batch != self._batch:
            self._batch = batch
            self._batch_metas.append(self.source.format_meta())
        self._ensure_open()
        self._process_type(msg)
        self._process_topic(topic, msg)


    def close(self):
        """Rewrites out everything to SQL schema file, ensuring all source metas."""
        if self._file:
            self._file.seek(0)
            self._write_header()
            for key in sorted(self._types):
                self._write_entity("table", self._types[key])
            for key in sorted(self._topics):
                self._write_entity("view", self._topics[key])
            self._file.close()
            self._file = None
        if not self._close_printed and self._types:
            self._close_printed = True
            ConsolePrinter.debug("Wrote %s and %s to SQL %s (%s).",
                                 plural("message type table",
                                        len(self._types) - len(self._nested_types)),
                                 plural("topic view", self._topics), self._filename,
                                 format_bytes(os.path.getsize(self._filename)))
            if self._nested_types:
                ConsolePrinter.debug("Wrote %s to SQL %s.",
                                     plural("nested message type table", self._nested_types),
                                     self._filename)
        self._nested_types.clear()
        del self._batch_metas[:]
        SqlMixin.close(self)
        super(SqlSink, self).close()


    def _ensure_open(self):
        """Opens output file if not already open, writes header."""
        if self._file: return

        self._filename = self.args.WRITE if self._overwrite else unique_path(self.args.WRITE)
        makedirs(os.path.dirname(self._filename))
        if self.args.VERBOSE:
            sz = os.path.exists(self._filename) and os.path.getsize(self._filename)
            action = "Overwriting" if sz and self._overwrite else "Creating"
            ConsolePrinter.debug("%s %s.", action, self._filename)
        self._file = open(self._filename, "wb")
        self._write_header()


    def _process_topic(self, topic, msg):
        """Builds and writes CREATE VIEW statement for topic if not already built."""
        topickey = rosapi.TypeMeta.make(msg, topic).topickey
        if topickey in self._topics:
            return

        self._topics[topickey] = self._make_topic_data(topic, msg)
        self._write_entity("view", self._topics[topickey])


    def _process_type(self, msg, rootmsg=None):
        """
        Builds and writes CREATE TABLE statement for message type if not already built.

        Builds statements recursively for nested types if configured.

        @return   built SQL, or None if already built
        """
        rootmsg = rootmsg or msg
        typekey = rosapi.TypeMeta.make(msg, root=rootmsg).typekey
        if typekey in self._types:
            return None

        extra_cols = [(c, self._make_column_type(t, fallback="int64" if "time" == t else None))
                      for c, t in self.MESSAGE_TYPE_BASECOLS]
        self._types[typekey] = self._make_type_data(msg, extra_cols, rootmsg)
        self._schema[typekey] = collections.OrderedDict(self._types[typekey].pop("cols"))

        self._write_entity("table", self._types[typekey])
        if self._nesting: self._process_nested(msg, rootmsg)
        return self._types[typekey]["sql"]


    def _process_nested(self, msg, rootmsg):
        """Builds anr writes CREATE TABLE statements for nested types."""
        nesteds = rosapi.iter_message_fields(msg, messages_only=True) if self._nesting else ()
        for path, submsgs, subtype in nesteds:
            scalartype = rosapi.scalar(subtype)
            if subtype == scalartype and "all" != self._nesting: continue  # for path

            subtypehash = self.source.get_message_type_hash(scalartype)
            subtypekey = (scalartype, subtypehash)
            if subtypekey in self._types: continue  # for path

            if not isinstance(submsgs, (list, tuple)): submsgs = [submsgs]
            [submsg] = submsgs[:1] or [self.source.get_message_class(scalartype, subtypehash)()]
            subsql = self._process_type(submsg, rootmsg)
            if subsql: self._nested_types[subtypekey] = subsql


    def _write_header(self):
        """Writes header to current file."""
        args = {
            "dialect":  self._dialect,
            "args":      " ".join(sys.argv[1:]),
            "source":   "\n\n".join("-- Source:\n" +
                                    "\n".join("-- " + x for x in s.strip().splitlines())
                                    for s in self._batch_metas),
            "dt":       datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._file.write((
            "-- SQL dialect: {dialect}.\n"
            "-- Written by grepros on {dt}.\n"
            "-- Command: grepros {args}.\n"
            "\n{source}\n\n"
        ).format(**args).encode("utf-8"))


    def _write_entity(self, category, item):
        """Writes table or view SQL statement to file."""
        self._file.write(b"\n")
        if "table" == category:
            self._file.write(("-- Message type %(type)s (%(md5)s)\n--\n" % item).encode("utf-8"))
            self._file.write(("-- %s\n" % "\n-- ".join(item["definition"].splitlines())).encode("utf-8"))
        else:
            self._file.write(('-- Topic "%(name)s": %(type)s (%(md5)s)\n' % item).encode("utf-8"))
        self._file.write(("%s\n\n" % item["sql"]).encode("utf-8"))



def init(*_, **__):
    """Adds SQL schema output format support."""
    from .. import plugins  # Late import to avoid circular
    plugins.add_write_format("sql", SqlSink, "SQL", [
        ("dialect=" + "|".join(sorted(filter(bool, SqlSink.DIALECTS))),
                                  "use specified SQL dialect in SQL output\n"
                                  '(default "%s")' % SqlSink.DEFAULT_DIALECT),
        ("dialect-file=path/to/dialects.yaml",
                                  "load additional SQL dialects\n"
                                  "for SQL output, from a YAML or JSON file"),
        ("nesting=array|all",     "create tables for nested message types\n"
                                  "in SQL output,\n"
                                  'only for arrays if "array" \n'
                                  "else for any nested types"),
        ("overwrite=true|false",  "overwrite existing file in SQL output\n"
                                  "instead of appending unique counter (default false)")
    ])
