# -*- coding: utf-8 -*-
"""
Main outputs for search results.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     23.10.2021
@modified    16.10.2022
------------------------------------------------------------------------------
"""
## @namespace grepros.outputs
from __future__ import print_function
import atexit
import collections
import copy
import os
import re
import sys

import yaml

from . common import ConsolePrinter, MatchMarkers, TextWrapper, filter_fields, \
                     format_bytes, makedirs, merge_spans, plural, wildcard_to_regex
from . import rosapi


class SinkBase(object):
    """Output base class."""

    ## Auto-detection file extensions for subclasses, as (".ext", )
    FILE_EXTENSIONS = ()

    def __init__(self, args):
        """
        @param   args        arguments object like argparse.Namespace
        @param   args.META   whether to print metainfo
        """
        self._batch_meta = {}  # {source batch: "source metadata"}
        self._counts     = {}  # {(topic, typename, typehash): count}

        self.args = copy.deepcopy(args)
        ## inputs.SourceBase instance bound to this sink
        self.source = None

    def emit_meta(self):
        """Prints source metainfo like bag header as debug stream, if not already printed."""
        batch = self.args.META and self.source.get_batch()
        if self.args.META and batch not in self._batch_meta:
            meta = self._batch_meta[batch] = self.source.format_meta()
            meta and ConsolePrinter.debug(meta)

    def emit(self, topic, index, stamp, msg, match):
        """
        Outputs ROS message.

        @param   topic  full name of ROS topic the message is from
        @param   index  message index in topic
        @param   msg    ROS message
        @param   match  ROS message with values tagged with match markers if matched, else None
        """
        topickey = rosapi.TypeMeta.make(msg, topic).topickey
        self._counts[topickey] = self._counts.get(topickey, 0) + 1

    def bind(self, source):
        """Attaches source to sink."""
        self.source = source

    def validate(self):
        """Returns whether sink prerequisites are met (like ROS environment set if TopicSink)."""
        return True

    def close(self):
        """Shuts down output, closing any files or connections."""
        self._batch_meta.clear()
        self._counts.clear()

    def flush(self):
        """Writes out any pending data to disk."""

    def thread_excepthook(self, text, exc):
        """Handles exception, used by background threads."""
        ConsolePrinter.error(text)

    def is_highlighting(self):
        """Returns whether this sink requires highlighted matches."""
        return False

    @classmethod
    def autodetect(cls, target):
        """Returns true if target is recognizable as output for this sink class."""
        ext = os.path.splitext(target or "")[-1].lower()
        return ext in cls.FILE_EXTENSIONS


class TextSinkMixin(object):
    """Provides message formatting as text."""

    ## Default highlight wrappers if not color output
    NOCOLOR_HIGHLIGHT_WRAPPERS = "**", "**"


    def __init__(self, args):
        """
        @param   args                       arguments object like argparse.Namespace
        @param   args.COLOR                 "never" for not using colors in replacements
        @param   args.PRINT_FIELDS          message fields to use in output if not all
        @param   args.NOPRINT_FIELDS        message fields to skip in output
        @param   args.MAX_FIELD_LINES       maximum number of lines to output per field
        @param   args.START_LINE            message line number to start output from
        @param   args.END_LINE              message line number to stop output at
        @param   args.MAX_MESSAGE_LINES     maximum number of lines to output per message
        @param   args.LINES_AROUND_MATCH    number of message lines around matched fields to output
        @param   args.MATCHED_FIELDS_ONLY   output only the fields where match was found
        @param   args.WRAP_WIDTH            character width to wrap message YAML output at
        @param   args.MATCH_WRAPPER         string to wrap around matched values,
                                            both sides if one value, start and end if more than one,
                                            or no wrapping if zero values
        """
        self._prefix       = ""    # Put before each message line (filename if grepping 1+ files)
        self._wrapper      = None  # TextWrapper instance
        self._patterns     = {}    # {key: [(() if any field else ('path', ), re.Pattern), ]}
        self._format_repls = {}    # {text to replace if highlight: replacement text}
        self._styles = collections.defaultdict(str)  # {label: ANSI code string}

        self._configure(args)


    def format_message(self, msg, highlight=False):
        """Returns message as formatted string, optionally highlighted for matches."""
        text = self.message_to_yaml(msg).rstrip("\n")

        if self._prefix or self.args.START_LINE or self.args.END_LINE \
        or self.args.MAX_MESSAGE_LINES or (self.args.LINES_AROUND_MATCH and highlight):
            lines = text.splitlines()

            if self.args.START_LINE or self.args.END_LINE or self.args.MAX_MESSAGE_LINES:
                start = self.args.START_LINE or 0
                start = max(start, -len(lines)) - (start > 0)  # <0 to sanity, >0 to 0-base
                lines = lines[start:start + (self.args.MAX_MESSAGE_LINES or len(lines))]
                lines = lines and (lines[:-1] + [lines[-1] + self._styles["rst"]])

            if self.args.LINES_AROUND_MATCH and highlight:
                spans, NUM = [], self.args.LINES_AROUND_MATCH
                for i, l in enumerate(lines):
                    if MatchMarkers.START in l:
                        spans.append([max(0, i - NUM), min(i + NUM + 1, len(lines))])
                    if MatchMarkers.END in l and spans:
                        spans[-1][1] = min(i + NUM + 1, len(lines))
                lines = sum((lines[a:b - 1] + [lines[b - 1] + self._styles["rst"]]
                             for a, b in merge_spans(spans)), [])

            if self._prefix:
                lines = [self._prefix + l for l in lines]

            text = "\n".join(lines)

        for a, b in self._format_repls.items() if highlight else ():
            text = re.sub(r"(%s)\1+" % re.escape(a), r"\1", text)  # Remove consecutive duplicates
            text = text.replace(a, b)

        return text


    def message_to_yaml(self, val, top=(), typename=None):
        """Returns ROS message or other value as YAML."""
        # Refactored from genpy.message.strify_message().
        unquote = lambda v: v[1:-1] if v[:1] == v[-1:] == '"' else v

        def retag_match_lines(lines):
            """Adds match tags to lines where wrapping separated start and end."""
            PH = self._wrapper.placeholder
            for i, l in enumerate(lines):
                startpos0, endpos0 = l.find (MatchMarkers.START), l.find (MatchMarkers.END)
                startpos1, endpos1 = l.rfind(MatchMarkers.START), l.rfind(MatchMarkers.END)
                if endpos0 >= 0 and (startpos0 < 0 or startpos0 > endpos0):
                    lines[i] = l = re.sub(r"^(\s*)", r"\1" + MatchMarkers.START, l)
                if startpos1 >= 0 and endpos1 < startpos1 and i + 1 < len(lines):
                    lines[i + 1] = re.sub(r"^(\s*)", r"\1" + MatchMarkers.START, lines[i + 1])
                if startpos1 >= 0 and startpos1 > endpos1:
                    CUT, EXTRA = (-len(PH), PH) if PH and l.endswith(PH) else (len(l), "")
                    lines[i] = l[:CUT] + MatchMarkers.END + EXTRA
            return lines

        def truncate(v):
            """Returns text or list/tuple truncated to length used in final output."""
            if self.args.LINES_AROUND_MATCH \
            or (not self.args.MAX_MESSAGE_LINES and (self.args.END_LINE or 0) <= 0): return v

            MAX_CHAR_LEN = 1 + len(MatchMarkers.START) + len(MatchMarkers.END)
            # For list/tuple, account for comma and space
            if isinstance(v, (list, tuple)): textlen = bytelen = 2 + len(v) * (2 + MAX_CHAR_LEN)
            else: textlen, bytelen = self._wrapper.strlen(v), len(v)
            if textlen < 10000: return v

            # Heuristic optimization: shorten superlong texts before converting to YAML
            # if outputting a maximum number of lines per message
            # (e.g. a lidar pointcloud can be 10+MB of text and take 10+ seconds to format).
            MIN_CHARS_PER_LINE = self._wrapper.width
            if MAX_CHAR_LEN != 1:
                MIN_CHARS_PER_LINE = self._wrapper.width // MAX_CHAR_LEN * 2
            MAX_LINES = self.args.MAX_MESSAGE_LINES or self.args.END_LINE
            MAX_CHARS = MAX_LEN = MAX_LINES * MIN_CHARS_PER_LINE * self._wrapper.width + 100
            if bytelen > MAX_CHARS:  # Use worst-case max length plus some extra
                if isinstance(v, (list, tuple)): MAX_LEN = MAX_CHARS // 3
                v = v[:MAX_LEN]
            return v

        indent = "  " * len(top)
        if isinstance(val, (int, float, bool)):
            return str(val)
        if isinstance(val, str):
            if val in ("", MatchMarkers.EMPTY):
                return MatchMarkers.EMPTY_REPL if val else "''"
            # default_style='"' avoids trailing "...\n"
            return yaml.safe_dump(truncate(val), default_style='"', width=sys.maxsize).rstrip("\n")
        if isinstance(val, (list, tuple)):
            if not val:
                return "[]"
            if rosapi.scalar(typename) in rosapi.ROS_STRING_TYPES:
                yaml_str = yaml.safe_dump(truncate(val)).rstrip('\n')
                return "\n" + "\n".join(indent + line for line in yaml_str.splitlines())
            vals = [x for v in truncate(val) for x in [self.message_to_yaml(v, top, typename)] if x]
            if rosapi.scalar(typename) in rosapi.ROS_NUMERIC_TYPES:
                return "[%s]" % ", ".join(unquote(str(v)) for v in vals)
            return ("\n" + "\n".join(indent + "- " + v for v in vals)) if vals else ""
        if rosapi.is_ros_message(val):
            MATCHED_ONLY = self.args.MATCHED_FIELDS_ONLY and not self.args.LINES_AROUND_MATCH
            vals, fieldmap = [], rosapi.get_message_fields(val)
            prints, noprints = self._patterns["print"], self._patterns["noprint"]
            fieldmap = filter_fields(fieldmap, top, include=prints, exclude=noprints)
            for k, t in fieldmap.items():
                v = self.message_to_yaml(rosapi.get_message_value(val, k, t), top + (k, ), t)
                if not v or MATCHED_ONLY and MatchMarkers.START not in v:
                    continue  # for k, t

                if t not in rosapi.ROS_STRING_TYPES: v = unquote(v)
                if rosapi.scalar(t) in rosapi.ROS_BUILTIN_TYPES:
                    is_strlist = t.endswith("]") and rosapi.scalar(t) in rosapi.ROS_STRING_TYPES
                    is_num = rosapi.scalar(t) in rosapi.ROS_NUMERIC_TYPES
                    extra_indent = indent if is_strlist else " " * len(indent + k + ": ")
                    self._wrapper.reserve_width(self._prefix + extra_indent)
                    self._wrapper.drop_whitespace = t.endswith("]") and not is_strlist
                    self._wrapper.break_long_words = not is_num
                    v = ("\n" + extra_indent).join(retag_match_lines(self._wrapper.wrap(v)))
                    if is_strlist and self._wrapper.strip(v) != "[]": v = "\n" + v
                vals.append("%s%s: %s" % (indent, k, rosapi.format_message_value(val, k, v)))
            return ("\n" if indent and vals else "") + "\n".join(vals)

        return str(val)


    def _configure(self, args):
        """Initializes output settings."""
        prints, noprints = args.PRINT_FIELDS, args.NOPRINT_FIELDS
        for key, vals in [("print", prints), ("noprint", noprints)]:
            self._patterns[key] = [(tuple(v.split(".")), wildcard_to_regex(v)) for v in vals]

        if "never" != args.COLOR:
            self._styles.update({"hl0":  ConsolePrinter.STYLE_HIGHLIGHT,
                                 "ll0":  ConsolePrinter.STYLE_LOWLIGHT,
                                 "pfx0": ConsolePrinter.STYLE_SPECIAL,  # Content line prefix start
                                 "sep0": ConsolePrinter.STYLE_SPECIAL2})
            self._styles.default_factory = lambda: ConsolePrinter.STYLE_RESET

        WRAPS = args.MATCH_WRAPPER
        if WRAPS is None and "never" == args.COLOR: WRAPS = self.NOCOLOR_HIGHLIGHT_WRAPPERS
        WRAPS = ((WRAPS or [""]) * 2)[:2]
        self._styles["hl0"] = self._styles["hl0"] + WRAPS[0]
        self._styles["hl1"] = WRAPS[1] + self._styles["hl1"]

        custom_widths = {MatchMarkers.START: len(WRAPS[0]), MatchMarkers.END:     len(WRAPS[1]),
                         self._styles["ll0"]:            0, self._styles["ll1"]:  0,
                         self._styles["pfx0"]:           0, self._styles["pfx1"]: 0,
                         self._styles["sep0"]:           0, self._styles["sep1"]: 0}
        wrapargs = dict(max_lines=args.MAX_FIELD_LINES,
                        placeholder="%s ...%s" % (self._styles["ll0"], self._styles["ll1"]))
        if args.WRAP_WIDTH is not None: wrapargs.update(width=args.WRAP_WIDTH)
        self._wrapper = TextWrapper(custom_widths=custom_widths, **wrapargs)
        self._format_repls = {MatchMarkers.START: self._styles["hl0"],
                              MatchMarkers.END:   self._styles["hl1"]}



class ConsoleSink(SinkBase, TextSinkMixin):
    """Prints messages to console."""

    META_LINE_TEMPLATE   = "{ll0}{sep} {line}{ll1}"
    MESSAGE_SEP_TEMPLATE = "{ll0}{sep}{ll1}"
    PREFIX_TEMPLATE      = "{pfx0}{batch}{pfx1}{sep0}{sep}{sep1}"
    MATCH_PREFIX_SEP     = ":"    # Printed after bag filename for matched message lines
    CONTEXT_PREFIX_SEP   = "-"    # Printed after bag filename for context message lines
    SEP                  = "---"  # Prefix of message separators and metainfo lines


    def __init__(self, args):
        """
        @param   args                       arguments object like argparse.Namespace
        @param   args.META                  whether to print metainfo
        @param   args.PRINT_FIELDS          message fields to print in output if not all
        @param   args.NOPRINT_FIELDS        message fields to skip in output
        @param   args.LINE_PREFIX           print source prefix like bag filename on each message line
        @param   args.MAX_FIELD_LINES       maximum number of lines to print per field
        @param   args.START_LINE            message line number to start output from
        @param   args.END_LINE              message line number to stop output at
        @param   args.MAX_MESSAGE_LINES     maximum number of lines to output per message
        @param   args.LINES_AROUND_MATCH    number of message lines around matched fields to output
        @param   args.MATCHED_FIELDS_ONLY   output only the fields where match was found
        @param   args.WRAP_WIDTH            character width to wrap message YAML output at
        """
        if args.WRAP_WIDTH is None:
            args = copy.deepcopy(args)
            args.WRAP_WIDTH = ConsolePrinter.WIDTH

        super(ConsoleSink, self).__init__(args)
        TextSinkMixin.__init__(self, args)


    def emit_meta(self):
        """Prints source metainfo like bag header, if not already printed."""
        batch = self.args.META and self.source.get_batch()
        if self.args.META and batch not in self._batch_meta:
            meta = self._batch_meta[batch] = self.source.format_meta()
            kws = dict(self._styles, sep=self.SEP)
            meta = "\n".join(x and self.META_LINE_TEMPLATE.format(**dict(kws, line=x))
                             for x in meta.splitlines())
            meta and ConsolePrinter.print(meta)


    def emit(self, topic, index, stamp, msg, match):
        """Prints separator line and message text."""
        self._prefix = ""
        if self.args.LINE_PREFIX and self.source.get_batch():
            sep = self.MATCH_PREFIX_SEP if match else self.CONTEXT_PREFIX_SEP
            kws = dict(self._styles, sep=sep, batch=self.source.get_batch())
            self._prefix = self.PREFIX_TEMPLATE.format(**kws)
        kws = dict(self._styles, sep=self.SEP)
        if self.args.META:
            meta = self.source.format_message_meta(topic, index, stamp, msg)
            meta = "\n".join(x and self.META_LINE_TEMPLATE.format(**dict(kws, line=x))
                             for x in meta.splitlines())
            meta and ConsolePrinter.print(meta)
        elif self._counts:
            sep = self.MESSAGE_SEP_TEMPLATE.format(**kws)
            sep and ConsolePrinter.print(sep)
        ConsolePrinter.print(self.format_message(match or msg, highlight=bool(match)))
        super(ConsoleSink, self).emit(topic, index, stamp, msg, match)


    def is_highlighting(self):
        """Returns True (requires highlighted matches)."""
        return True



class BagSink(SinkBase):
    """Writes messages to bagfile."""

    def __init__(self, args):
        """
        @param   args                 arguments object like argparse.Namespace
        @param   args.META            whether to print metainfo
        @param   args.WRITE           name of ROS bagfile to create or append to
        @param   args.WRITE_OPTIONS   {"overwrite": whether to overwrite existing file
                                                     (default false)}
        @param   args.VERBOSE         whether to print debug information
        """
        super(BagSink, self).__init__(args)
        self._bag = None
        self._overwrite = (args.WRITE_OPTIONS.get("overwrite") == "true")
        self._close_printed = False

        atexit.register(self.close)

    def emit(self, topic, index, stamp, msg, match):
        """Writes message to output bagfile."""
        if not self._bag:
            if self.args.VERBOSE:
                sz = os.path.isfile(self.args.WRITE) and os.path.getsize(self.args.WRITE)
                ConsolePrinter.debug("%s %s%s.",
                                     "Overwriting" if sz and self._overwrite else
                                     "Appending to" if sz else "Creating",
                                     self.args.WRITE, (" (%s)" % format_bytes(sz)) if sz else "")
            makedirs(os.path.dirname(self.args.WRITE))
            self._bag = rosapi.Bag(self.args.WRITE, mode="w" if self._overwrite else "a")

        topickey = rosapi.TypeMeta.make(msg, topic).topickey
        if topickey not in self._counts and self.args.VERBOSE:
            ConsolePrinter.debug("Adding topic %s in bag output.", topic)

        self._bag.write(topic, msg, stamp, self.source.get_message_meta(topic, index, stamp, msg))
        super(BagSink, self).emit(topic, index, stamp, msg, match)

    def validate(self):
        """Returns whether ROS environment is set, prints error if not."""
        result = True
        if self.args.WRITE_OPTIONS.get("overwrite") not in (None, "true", "false"):
            ConsolePrinter.error("Invalid overwrite option for bag: %r. "
                                 "Choose one of {true, false}.",
                                 self.args.WRITE_OPTIONS["overwrite"])
            result = False
        return rosapi.validate() and result

    def close(self):
        """Closes output bagfile, if any."""
        self._bag and self._bag.close()
        if not self._close_printed and self._counts:
            self._close_printed = True
            ConsolePrinter.debug("Wrote %s in %s to %s (%s).",
                                 plural("message", sum(self._counts.values())),
                                 plural("topic", self._counts), self.args.WRITE,
                                 format_bytes(os.path.getsize(self.args.WRITE)))
        super(BagSink, self).close()

    @classmethod
    def autodetect(cls, target):
        """Returns true if target is recognizable as a ROS bag."""
        ext = os.path.splitext(target or "")[-1].lower()
        return ext in rosapi.BAG_EXTENSIONS


class TopicSink(SinkBase):
    """Publishes messages to ROS topics."""

    def __init__(self, args):
        """
        @param   args                   arguments object like argparse.Namespace
        @param   args.LIVE              whether reading messages from live ROS topics
        @param   args.META              whether to print metainfo
        @param   args.QUEUE_SIZE_OUT    publisher queue size
        @param   args.PUBLISH_PREFIX    output topic prefix, prepended to input topic
        @param   args.PUBLISH_SUFFIX    output topic suffix, appended to output topic
        @param   args.PUBLISH_FIXNAME   single output topic name to publish to,
                                        overrides prefix and suffix if given
        @param   args.VERBOSE           whether to print debug information
        """
        super(TopicSink, self).__init__(args)
        self._pubs = {}  # {(intopic, typename, typehash): ROS publisher}
        self._close_printed = False

    def emit(self, topic, index, stamp, msg, match):
        """Publishes message to output topic."""
        with rosapi.TypeMeta.make(msg, topic) as m:
            topickey, cls = (m.topickey, m.typeclass)
        if topickey not in self._pubs:
            topic2 = self.args.PUBLISH_PREFIX + topic + self.args.PUBLISH_SUFFIX
            topic2 = self.args.PUBLISH_FIXNAME or topic2
            if self.args.VERBOSE:
                ConsolePrinter.debug("Publishing from %s to %s.", topic, topic2)

            pub = None
            if self.args.PUBLISH_FIXNAME:
                pub = next((v for (_, c), v in self._pubs.items() if c == cls), None)
            pub = pub or rosapi.create_publisher(topic2, cls, queue_size=self.args.QUEUE_SIZE_OUT)
            self._pubs[topickey] = pub

        self._pubs[topickey].publish(msg)
        super(TopicSink, self).emit(topic, index, stamp, msg, match)

    def bind(self, source):
        """Attaches source to sink and blocks until connected to ROS."""
        SinkBase.bind(self, source)
        rosapi.init_node()

    def validate(self):
        """
        Returns whether ROS environment is set for publishing,
        and output topic configuration is valid, prints error if not.
        """
        result = rosapi.validate(live=True)
        config_ok = True
        if self.args.LIVE and not any((self.args.PUBLISH_PREFIX, self.args.PUBLISH_SUFFIX,
                                        self.args.PUBLISH_FIXNAME)):
            ConsolePrinter.error("Need topic prefix or suffix or fixname "
                                 "when republishing messages from live ROS topics.")
            config_ok = False
        return result and config_ok

    def close(self):
        """Shuts down publishers."""
        if not self._close_printed and self._counts:
            self._close_printed = True
            ConsolePrinter.debug("Published %s to %s.",
                                 plural("message", sum(self._counts.values())),
                                 plural("topic", self._pubs))
        for k in list(self._pubs):
            self._pubs.pop(k).unregister()
        super(TopicSink, self).close()
        rosapi.shutdown_node()


class MultiSink(SinkBase):
    """Combines any number of sinks."""

    ## Autobinding between argument flags and sink classes
    FLAG_CLASSES = {"PUBLISH": TopicSink, "CONSOLE": ConsoleSink}

    ## Autobinding between `--write .. format=FORMAT` and sink classes
    FORMAT_CLASSES = {"bag": BagSink}

    def __init__(self, args):
        """
        @param   args           arguments object like argparse.Namespace
        @param   args.CONSOLE   print matches to console
        @param   args.WRITE     [[target, format=FORMAT, key=value, ], ]
        @param   args.PUBLISH   publish matches to live topics
        """
        super(MultiSink, self).__init__(args)
        self._valid = True

        ## List of all combined sinks
        self.sinks = [cls(args) for flag, cls in self.FLAG_CLASSES.items()
                      if getattr(args, flag, None)]

        for dumpopts in args.WRITE:
            target, kwargs = dumpopts[0], dict(x.split("=", 1) for x in dumpopts[1:])
            cls = self.FORMAT_CLASSES.get(kwargs.pop("format", None))
            if not cls:
                cls = next((c for c in self.FORMAT_CLASSES.values()
                            if callable(getattr(c, "autodetect", None))
                            and c.autodetect(target)), None)
            if not cls:
                ConsolePrinter.error('Unknown output format in "%s"' % " ".join(dumpopts))
                self._valid = False
                continue  # for dumpopts
            clsargs = copy.deepcopy(args)
            clsargs.WRITE, clsargs.WRITE_OPTIONS = target, kwargs
            self.sinks += [cls(clsargs)]

    def emit_meta(self):
        """Outputs source metainfo in one sink, if not already emitted."""
        sink = next((s for s in self.sinks if isinstance(s, ConsoleSink)), None)
        # Print meta in one sink only, prefer console
        sink = sink or self.sinks[0] if self.sinks else None
        sink and sink.emit_meta()

    def emit(self, topic, index, stamp, msg, match):
        """Outputs ROS message to all sinks."""
        for sink in self.sinks:
            sink.emit(topic, index, stamp, msg, match)

    def bind(self, source):
        """Attaches source to all sinks, sets thread_excepthook on all sinks."""
        SinkBase.bind(self, source)
        for sink in self.sinks:
            sink.bind(source)
            sink.thread_excepthook = self.thread_excepthook

    def validate(self):
        """Returns whether prerequisites are met for all sinks."""
        if not self.sinks:
            ConsolePrinter.error("No output configured.")
        return bool(self.sinks) and all([sink.validate() for sink in self.sinks]) and self._valid

    def close(self):
        """Closes all sinks."""
        for sink in self.sinks:
            sink.close()

    def flush(self):
        """Flushes all sinks."""
        for sink in self.sinks:
            sink.flush()

    def is_highlighting(self):
        """Returns whether any sink requires highlighted matches."""
        return any(s.is_highlighting() for s in self.sinks)
