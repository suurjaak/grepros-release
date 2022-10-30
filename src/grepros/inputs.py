# -*- coding: utf-8 -*-
"""
Input sources for search content.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     23.10.2021
@modified    16.10.2022
------------------------------------------------------------------------------
"""
## @namespace grepros.inputs
from __future__ import print_function
import copy
import collections
import datetime
import functools
import itertools
import os
try: import queue  # Py3
except ImportError: import Queue as queue  # Py2
import re
import threading
import time

from . common import ConsolePrinter, Decompressor, ProgressBar, drop_zeros, filter_dict, \
                     find_files, format_bytes, format_stamp, format_timedelta, plural, \
                     wildcard_to_regex
from . import rosapi


class SourceBase(object):
    """Message producer base class."""

    ## Template for message metainfo line
    MESSAGE_META_TEMPLATE = "{topic} #{index} ({type}  {dt}  {stamp})"

    def __init__(self, args):
        """
        @param   args                    arguments object like argparse.Namespace
        @param   args.START_TIME         earliest timestamp of messages to scan
        @param   args.END_TIME           latest timestamp of messages to scan
        @param   args.UNIQUE             emit messages that are unique in topic
        @param   args.SELECT_FIELDS      message fields to use for uniqueness if not all
        @param   args.NOSELECT_FIELDS    message fields to skip for uniqueness
        @param   args.NTH_MESSAGE        scan every Nth message in topic
        @param   args.NTH_INTERVAL       minimum time interval between messages in topic
        """
        # {key: [(() if any field else ('nested', 'path') or re.Pattern, re.Pattern), ]}
        self._patterns = {}
        # {topic: ["pkg/MsgType", ]} searched in current source
        self._topics = collections.defaultdict(list)
        self._counts = collections.Counter()  # {(topic, typename, typehash): count processed}
        # {(topic, typename, typehash): (message hash over all fields used in matching)}
        self._hashes = collections.defaultdict(set)
        self._processables = {}  # {(topic, typename, typehash): (index, stamp) of last processable}

        self.args = copy.deepcopy(args)
        ## outputs.SinkBase instance bound to this source
        self.sink = None
        ## All topics in source, as {(topic, typenane, typehash): total message count or None}
        self.topics = {}
        ## ProgressBar instance, if any
        self.bar = None

        self._parse_patterns()
        rosapi.TypeMeta.SOURCE = self

    def read(self):
        """Yields messages from source, as (topic, msg, ROS time)."""

    def bind(self, sink):
        """Attaches sink to source"""
        self.sink = sink

    def validate(self):
        """Returns whether source prerequisites are met (like ROS environment for TopicSource)."""
        return True

    def close(self):
        """Shuts down input, closing any files or connections."""
        self.topics.clear()
        self._topics.clear()
        self._counts.clear()
        self._hashes.clear()
        self._processables.clear()
        if self.bar:
            self.bar.pulse_pos = None
            self.bar.update(flush=True).stop()
            self.bar = None

    def close_batch(self):
        """Shuts down input batch if any (like bagfile), else all input."""
        self.close()

    def format_meta(self):
        """Returns source metainfo string."""
        return ""

    def format_message_meta(self, topic, index, stamp, msg):
        """Returns message metainfo string."""
        return self.MESSAGE_META_TEMPLATE.format(**self.get_message_meta(topic, index, stamp, msg))

    def get_batch(self):
        """Returns source batch identifier if any (like bagfile name if BagSource)."""

    def get_meta(self):
        """Returns source metainfo data dict."""
        return {}

    def get_message_meta(self, topic, index, stamp, msg):
        """Returns message metainfo data dict."""
        with rosapi.TypeMeta.make(msg, topic) as m:
            return dict(topic=topic, type=m.typename, index=index, hash=m.typehash,
                        dt=drop_zeros(format_stamp(rosapi.to_sec(stamp)), " "),
                        stamp=drop_zeros(rosapi.to_sec(stamp)), schema=m.definition)

    def get_message_class(self, typename, typehash=None):
        """Returns message type class."""
        return rosapi.get_message_class(typename)

    def get_message_definition(self, msg_or_type):
        """Returns ROS message type definition full text, including subtype definitions."""
        return rosapi.get_message_definition(msg_or_type)

    def get_message_type_hash(self, msg_or_type):
        """Returns ROS message type MD5 hash."""
        return rosapi.get_message_type_hash(msg_or_type)

    def is_processable(self, topic, index, stamp, msg):
        """Returns whether specified message in topic is in acceptable time range."""
        if self.args.START_TIME and stamp < self.args.START_TIME:
            return False
        if self.args.END_TIME and stamp > self.args.END_TIME:
            return False
        if self.args.UNIQUE or self.args.NTH_MESSAGE > 1 or self.args.NTH_INTERVAL > 0:
            topickey = rosapi.TypeMeta.make(msg, topic).topickey
            last_accepted = self._processables.get(topickey)
        if self.args.NTH_MESSAGE > 1 and last_accepted:
            if (index - 1) % self.args.NTH_MESSAGE:
                return False
        if self.args.NTH_INTERVAL > 0 and last_accepted:
            if rosapi.to_sec(stamp - last_accepted[1]) < self.args.NTH_INTERVAL:
                return False
        if self.args.UNIQUE:
            include, exclude = self._patterns["select"], self._patterns["noselect"]
            msghash = rosapi.make_message_hash(msg, include, exclude)
            if msghash in self._hashes[topickey]:
                return False
            self._hashes[topickey].add(msghash)
        return True

    def notify(self, status):
        """Reports match status of last produced message."""

    def thread_excepthook(self, text, exc):
        """Handles exception, used by background threads."""
        ConsolePrinter.error(text)

    def _parse_patterns(self):
        """Parses pattern arguments into re.Patterns."""
        selects, noselects = self.args.SELECT_FIELDS, self.args.NOSELECT_FIELDS
        for key, vals in [("select", selects), ("noselect", noselects)]:
            self._patterns[key] = [(tuple(v.split(".")), wildcard_to_regex(v)) for v in vals]


class ConditionMixin(object):
    """
    Provides topic conditions evaluation.

    Evaluates a set of Python expressions, with a namespace of:
    - msg:                current message being checked
    - topic:              current topic being read
    - <topic /any/name>   messages in named or wildcarded topic

    <topic ..> gets replaced with an object with the following behavior:
    - len(obj)  -> number of messages processed in topic
    - bool(obj) -> whether there are any messages in topic
    - obj[pos]  -> topic message at position (from latest if negative, first if positive)
    - obj.x     -> attribute x of last message

    All conditions need to evaluate as true for a message to be processable.
    If a condition tries to access attributes of a message not yet present,
    condition evaluates as false.

    If a condition topic matches more than one real topic (by wildcard or by
    different types in one topic), evaluation is done for each set of
    topics separately, condition passing if any set passes.

    Example condition: `<topic */control_enable>.data and <topic */cmd_vel>.linear.x > 0`
                       `and <topic */cmd_vel>.angular.z < 0.02`.
    """

    TOPIC_RGX = re.compile(r"<topic\s+([^\s><]+)\s*>")  # "<topic /some/thing>"

    class NoMessageException(Exception): pass


    class Topic(object):
        """
        Object for <topic x> replacements in condition expressions.

        - len(topic)  -> number of messages processed in topic
        - bool(topic) -> whether there are any messages in topic
        - topic[x]    -> history at -1 -2 for last and but one, or 0 1 for first and second
        - topic.x     -> attribute x of last message
        """

        def __init__(self, count, firsts, lasts):
            self._count  = count
            self._firsts = firsts
            self._lasts  = lasts

        def __bool__(self):    return bool(self._count)
        def __nonzero__(self): return bool(self._count)
        def __len__(self):     return self._count

        def __getitem__(self, key):
            """Returns message from history at key, or Empty() if no such message."""
            try: return (self._lasts if key < 0 else self._firsts)[key]
            except IndexError: return ConditionMixin.Empty()

        def __getattr__(self, name):
            """Returns attribute value of last message, or raises NoMessageException."""
            if not self._lasts: raise ConditionMixin.NoMessageException()
            return getattr(self._lasts[-1], name)


    class Empty(object):
        """Placeholder falsy object that raises NoMessageException on attribute access."""
        def __getattr__(self, name): raise ConditionMixin.NoMessageException()
        def __bool__(self):          return False
        def __nonzero__(self):       return False


    def __init__(self, args):
        """
        @param   args              arguments object like argparse.Namespace
        @param   args.CONDITIONS   Python expressions that must evaluate as true
                                   for message to be processable
        """
        self._topic_states         = {}  # {topic: whether only used for condition, not matching}
        self._topics_per_condition = []  # [[topics in 1st condition], ]
        self._wildcard_topics      = {}  # {"/my/*/topic": re.Pattern}
        # {(topic, typename, typehash): [1st, 2nd, ..]}
        self._firstmsgs = collections.defaultdict(collections.deque)
        # {(topic, typename, typehash): [.., last]}
        self._lastmsgs  = collections.defaultdict(collections.deque)
        # {topic: (max positive index + 1, max abs(negative index) or 1)}
        self._topic_limits = collections.defaultdict(lambda: [1, 1])

        ## {condition with <topic x> as get_topic("x"): compiled code object}
        self._conditions = collections.OrderedDict()
        self._configure_conditions(args)

    def is_processable(self, topic, index, stamp, msg):
        """Returns whether current state passes conditions, if any."""
        result = True
        if not self._conditions:
            return result
        for i, (expr, code) in enumerate(self._conditions.items()):
            topics = self._topics_per_condition[i]
            wildcarded = [t for t in topics if t in self._wildcard_topics]
            realcarded = {wt: [(t, n, h) for (t, n, h) in self._lastmsgs if p.match(t)]
                          for wt in wildcarded for p in [self._wildcard_topics[wt]]}
            variants = [[(wt, (t, n, h)) for (t, n, h) in tt] or [(wt, (wt, None))]
                        for wt, tt in realcarded.items()]
            variants = variants or [[None]]  # Ensure one iteration if no wildcards to combine

            result = False
            for remaps in itertools.product(*variants):  # [(wildcard1, realname1), (wildcard2, ..]
                if remaps == (None, ): remaps = ()
                getter = functools.partial(self._get_topic_instance, remap=dict(remaps))
                ns = {"topic": topic, "msg": msg, "get_topic": getter}
                try:   result = eval(code, ns)
                except self.NoMessageException: pass
                except Exception as e:
                    ConsolePrinter.error('Error evaluating condition "%s": %s', expr, e)
                    raise
                if result: break  # for remaps
            if not result: break  # for i,
        return result

    def close_batch(self):
        """Clears cached messages."""
        self._firstmsgs.clear()
        self._lastmsgs.clear()

    def has_conditions(self):
        """Returns whether there are any conditions configured."""
        return bool(self._conditions)

    def conditions_get_topics(self):
        """Returns a list of all topics used in conditions (may contain wildcards)."""
        return list(self._topic_states)

    def is_conditions_topic(self, topic, pure=True):
        """
        Returns whether topic is used for checking condition.

        @param   pure  whether use should be solely for condition, not for matching at all
        """
        if not self._conditions: return False
        if topic in self._topic_states:
            return self._topic_states[topic] if pure else True
        wildcarded = [t for t, p in self._wildcard_topics.items() if p.match(topic)]
        if not wildcarded: return False
        return all(map(self._topic_states.get, wildcarded)) if pure else True

    def conditions_set_topic_state(self, topic, pure):
        """Sets whether topic is purely used for conditions not matching."""
        if topic in self._topic_states:
            self._topic_states[topic] = pure

    def conditions_register_message(self, topic, msg):
        """Retains message for condition evaluation if in condition topic."""
        if self.is_conditions_topic(topic, pure=False):
            topickey = rosapi.TypeMeta.make(msg, topic).topickey
            self._lastmsgs[topickey].append(msg)
            if len(self._lastmsgs[topickey]) > self._topic_limits[topic][-1]:
                self._lastmsgs[topickey].popleft()
            if len(self._firstmsgs[topickey]) < self._topic_limits[topic][0]:
                self._firstmsgs[topickey].append(msg)

    def _get_topic_instance(self, topic, remap=None):
        """
        Returns Topic() by name.

        @param   remap  optional remap dictionary as {topic1: (topic2, typename, typehash)}
        """
        if remap and topic in remap:
            topickey = remap[topic]
        else:
            topickey = next(((t, n, h) for (t, n, h) in self._lastmsgs if t == topic), None)
        if topickey not in self._counts:
            return self.Empty()
        c, f, l = (d[topickey] for d in (self._counts, self._firstmsgs, self._lastmsgs))
        return self.Topic(c, f, l)

    def _configure_conditions(self, args):
        """Parses condition expressions and populates local structures."""
        for v in args.CONDITIONS:
            topics = list(set(self.TOPIC_RGX.findall(v)))
            self._topic_states.update({t: True for t in topics})
            self._topics_per_condition.append(topics)
            for t in (t for t in topics if "*" in t):
                self._wildcard_topics[t] = wildcard_to_regex(t, end=True)
            expr = self.TOPIC_RGX.sub(r'get_topic("\1")', v)
            self._conditions[expr] = compile(expr, "", "eval")

        for v in args.CONDITIONS:  # Set history length from <topic x>[index]
            indexexprs = re.findall(self.TOPIC_RGX.pattern + r"\s*\[([^\]]+)\]", v)
            for topic, indexexpr in indexexprs:
                limits = self._topic_limits[topic]
                try:
                    index = eval(indexexpr)  # If integer, set history limits
                    limits[index < 0] = max(limits[index < 0], abs(index) + (index >= 0))
                except Exception: continue  # for topic



class BagSource(SourceBase, ConditionMixin):
    """Produces messages from ROS bagfiles."""

    ## Template for message metainfo line
    MESSAGE_META_TEMPLATE = "{topic} {index}/{total} ({type}  {dt}  {stamp})"
    ## Template for bag metainfo header
    META_TEMPLATE         = "\nFile {file} ({size}), {tcount} topics, {mcount:,d} messages\n" \
                            "File period {startdt} - {enddt}\n" \
                            "File span {delta} ({start} - {end})"

    def __init__(self, args):
        """
        @param   args               arguments object like argparse.Namespace
        @param   args.FILES         names of ROS bagfiles to scan if not all in directory
        @param   args.PATHS         paths to scan if not current directory
        @param   args.RECURSE       recurse into subdirectories when looking for bagfiles
        @param   args.TOPICS        ROS topics to scan if not all
        @param   args.TYPES         ROS message types to scan if not all
        @param   args.SKIP_TOPICS   ROS topics to skip
        @param   args.SKIP_TYPES    ROS message types to skip
        @param   args.START_TIME    earliest timestamp of messages to scan
        @param   args.END_TIME      latest timestamp of messages to scan
        @param   args.START_INDEX   message index within topic to start from
        @param   args.END_INDEX     message index within topic to stop at
        @param   args.CONDITIONS    Python expressions that must evaluate as true
                                    for message to be processable
        @param   args.AFTER         emit NUM messages of trailing context after match
        @param   args.ORDERBY       "topic" or "type" if any to group results by
        @param   args.DECOMPRESS    decompress archived bags to file directory
        @param   args.REINDEX       make a copy of unindexed bags and reindex them (ROS1 only)
        @param   args.WRITE         outputs, to skip in input files
        @param   args.PROGRESS      whether to print progress bar
        """
        super(BagSource, self).__init__(args)
        ConditionMixin.__init__(self, args)
        self._args0     = copy.deepcopy(args)  # Original arguments
        self._status    = None   # Match status of last produced message
        self._sticky    = False  # Scanning a single topic until all after-context emitted
        self._totals_ok = False  # Whether message count totals have been retrieved
        self._running   = False
        self._bag       = None   # Current bag object instance
        self._filename  = None   # Current bagfile path
        self._meta      = None   # Cached get_meta()

    def read(self):
        """Yields messages from ROS bagfiles, as (topic, msg, ROS time)."""
        self._running = True
        names, paths = self.args.FILES, self.args.PATHS
        exts, skip_exts = rosapi.BAG_EXTENSIONS, rosapi.SKIP_EXTENSIONS
        exts = list(exts) + ["%s%s" % (a, b) for a in exts for b in Decompressor.EXTENSIONS]

        encountereds = set()
        for filename in find_files(names, paths, exts, skip_exts, self.args.RECURSE):
            if not self._running:
                continue  # for filename

            fullname = os.path.realpath(os.path.abspath(filename))
            skip = Decompressor.make_decompressed_name(fullname) in encountereds
            encountereds.add(fullname)

            if skip or not self._configure(filename):
                continue  # for filename

            topicsets = [self._topics]
            if "topic" == self.args.ORDERBY:  # Group output by sorted topic names
                topicsets = [{n: tt} for n, tt in sorted(self._topics.items())]
            elif "type" == self.args.ORDERBY:  # Group output by sorted type names
                typetopics = {}
                for n, tt in self._topics.items():
                    for t in tt: typetopics.setdefault(t, []).append(n)
                topicsets = [{n: [t] for n in nn} for t, nn in sorted(typetopics.items())]

            self._init_progress()
            for topics in topicsets:
                for topic, msg, stamp in self._produce(topics) if topics else ():
                    self.conditions_register_message(topic, msg)
                    if not self.is_conditions_topic(topic, pure=True):
                        yield topic, msg, stamp
                if not self._running:
                    break  # for topics
            self._counts and self.sink.flush()
            self.close_batch()
        self._running = False

    def validate(self):
        """Returns whether ROS environment is set, prints error if not."""
        result = rosapi.validate()
        if self.args.ORDERBY and self.conditions_get_topics():
            ConsolePrinter.error("Cannot use topics in conditions and bag order by %s.",
                                 self.args.ORDERBY)
            result = False
        return result

    def close(self):
        """Closes current bag, if any."""
        self._running = False
        self._bag and self._bag.close()
        ConditionMixin.close_batch(self)
        super(BagSource, self).close()

    def close_batch(self):
        """Closes current bag, if any."""
        self._bag and self._bag.close()
        self._bag = None
        if self.bar:
            self.bar.update(flush=True)
            self.bar = None
        ConditionMixin.close_batch(self)

    def format_meta(self):
        """Returns bagfile metainfo string."""
        return self.META_TEMPLATE.format(**self.get_meta())

    def format_message_meta(self, topic, index, stamp, msg):
        """Returns message metainfo string."""
        return self.MESSAGE_META_TEMPLATE.format(**self.get_message_meta(topic, index, stamp, msg))

    def get_batch(self):
        """Returns name of current bagfile."""
        return self._filename

    def get_meta(self):
        """Returns bagfile metainfo data dict."""
        if self._meta is not None:
            return self._meta
        mcount = self._bag.get_message_count()
        start, end = (self._bag.get_start_time(), self._bag.get_end_time()) if mcount else ("", "")
        delta = format_timedelta(datetime.timedelta(seconds=(end or 0) - (start or 0)))
        self._meta = dict(file=self._filename, size=format_bytes(self._bag.size),
                          mcount=mcount, tcount=len(self.topics),
                          start=drop_zeros(start), end=drop_zeros(end),
                          startdt=drop_zeros(format_stamp(start)) if start != "" else "",
                          enddt=drop_zeros(format_stamp(end)) if end != "" else "", delta=delta)
        return self._meta

    def get_message_meta(self, topic, index, stamp, msg):
        """Returns message metainfo data dict."""
        self._ensure_totals()
        result = super(BagSource, self).get_message_meta(topic, index, stamp, msg)
        result.update(total=self.topics[(topic, result["type"], result["hash"])])
        if callable(getattr(self._bag, "get_qoses", None)):
            result.update(qoses=self._bag.get_qoses(topic, result["type"]))
        return result

    def get_message_class(self, typename, typehash=None):
        """Returns ROS message type class."""
        return self._bag.get_message_class(typename, typehash) or \
               rosapi.get_message_class(typename)

    def get_message_definition(self, msg_or_type):
        """Returns ROS message type definition full text, including subtype definitions."""
        return self._bag.get_message_definition(msg_or_type) or \
               rosapi.get_message_definition(msg_or_type)

    def get_message_type_hash(self, msg_or_type):
        """Returns ROS message type MD5 hash."""
        return self._bag.get_message_type_hash(msg_or_type) or \
               rosapi.get_message_type_hash(msg_or_type)

    def notify(self, status):
        """Reports match status of last produced message."""
        self._status = bool(status)
        if status and not self._totals_ok:
            self._ensure_totals()

    def is_processable(self, topic, index, stamp, msg):
        """
        Returns whether specified message in topic is in acceptable range,
        and all conditions, if any, evaluate as true.
        """
        topickey = rosapi.TypeMeta.make(msg, topic).topickey
        if self.args.START_INDEX:
            self._ensure_totals()
            START = self.args.START_INDEX
            MIN = max(0, START + (self.topics[topickey] if START < 0 else 0))
            if MIN >= index:
                return False
        if self.args.END_INDEX:
            self._ensure_totals()
            END = self.args.END_INDEX
            MAX = END + (self.topics[topickey] if END < 0 else 0)
            if MAX < index:
                return False
        if not super(BagSource, self).is_processable(topic, index, stamp, msg):
            return False
        return ConditionMixin.is_processable(self, topic, index, stamp, msg)

    def _produce(self, topics, start_time=None):
        """Yields messages from current ROS bagfile, as (topic, msg, ROS time)."""
        counts = collections.Counter()
        for topic, msg, stamp in self._bag.read_messages(list(topics), start_time):
            if not self._running or not self._bag:
                break  # for topic
            typename = rosapi.get_message_type(msg)
            if topics and typename not in topics[topic]:
                continue  # for topic

            topickey = rosapi.TypeMeta.make(msg, topic).topickey
            counts[topickey] += 1; self._counts[topickey] += 1
            # Skip messages already processed during sticky
            if not self._sticky and counts[topickey] != self._counts[topickey]:
                continue  # for topic

            self._status = None
            self.bar and self.bar.update(value=sum(self._counts.values()))
            yield topic, msg, stamp

            if self.args.NTH_MESSAGE > 1 or self.args.NTH_INTERVAL > 0:
                self._processables[topickey] = (self._counts[topickey], stamp)
            if self._status and self.args.AFTER and not self._sticky \
            and not self.has_conditions() \
            and (len(self._topics) > 1 or len(next(iter(self._topics.values()))) > 1):
                # Stick to one topic until trailing messages have been emitted
                self._sticky = True
                continue_from = stamp + rosapi.make_duration(nsecs=1)
                for entry in self._produce({topic: typename}, continue_from):
                    yield entry
                self._sticky = False

    def _init_progress(self):
        """Initializes progress bar, if any, for current bag."""
        if self.args.PROGRESS and not self.bar:
            self._ensure_totals()
            self.bar = ProgressBar(aftertemplate=" {afterword} ({value:,d}/{max:,d})")
            self.bar.afterword = os.path.basename(self._filename)
            self.bar.max = sum(sum(c for (t, n, _), c in self.topics.items()
                                   if c and t == t_ and n in nn)
                               for t_, nn in self._topics.items())
            self.bar.update(value=0)

    def _ensure_totals(self):
        """Retrieves total message counts if not retrieved."""
        if not self._totals_ok:  # Must be ros2.Bag
            for (t, n, h), c in self._bag.get_topic_info(counts=True).items():
                self.topics[(t, n, h)] = c
            self._totals_ok = True

    def _configure(self, filename):
        """Opens bag and populates bag-specific argument state, returns success."""
        self._meta      = None
        self._bag       = None
        self._filename  = None
        self._sticky    = False
        self._totals_ok = False
        self._counts.clear()
        self._processables.clear()
        self._hashes.clear()
        self.topics.clear()
        if self.args.WRITE \
        and any(os.path.realpath(x[0]) == os.path.realpath(filename)
                for x in self.args.WRITE):
            return False
        try:
            bag = rosapi.Bag(filename, mode="r", decompress=self.args.DECOMPRESS,
                             reindex=self.args.REINDEX, progress=self.args.PROGRESS)
        except Exception as e:
            ConsolePrinter.error("\nError opening %r: %s", filename, e)
            return False

        self._bag      = bag
        self._filename = bag.filename

        dct = fulldct = {}  # {topic: [typename, ]}
        for (t, n, h), c in bag.get_topic_info().items():
            dct.setdefault(t, []).append(n)
            self.topics[(t, n, h)] = c
        self._totals_ok = not any(v is None for v in self.topics.values())
        for topic in self.conditions_get_topics():
            self.conditions_set_topic_state(topic, True)

        dct = filter_dict(dct, self.args.TOPICS, self.args.TYPES)
        dct = filter_dict(dct, self.args.SKIP_TOPICS, self.args.SKIP_TYPES, reverse=True)
        for topic in self.conditions_get_topics():  # Add topics used in conditions
            matches = [t for p in [wildcard_to_regex(topic, end=True)] for t in fulldct
                       if t == topic or "*" in topic and p.match(t)]
            for topic in matches:
                dct.setdefault(topic, fulldct[topic])
                self.conditions_set_topic_state(topic, topic not in dct)
        self._topics = dct
        self._meta   = self.get_meta()

        args = self.args = copy.deepcopy(self._args0)
        if args.START_TIME is not None:
            args.START_TIME = rosapi.make_bag_time(args.START_TIME, bag)
        if args.END_TIME is not None:
            args.END_TIME = rosapi.make_bag_time(args.END_TIME, bag)
        return True


class TopicSource(SourceBase, ConditionMixin):
    """Produces messages from live ROS topics."""

    ## Seconds between refreshing available topics from ROS master.
    MASTER_INTERVAL = 2

    def __init__(self, args):
        """
        @param   args                 arguments object like argparse.Namespace
        @param   args.TOPICS          ROS topics to scan if not all
        @param   args.TYPES           ROS message types to scan if not all
        @param   args.SKIP_TOPICS     ROS topics to skip
        @param   args.SKIP_TYPES      ROS message types to skip
        @param   args.START_TIME      earliest timestamp of messages to scan
        @param   args.END_TIME        latest timestamp of messages to scan
        @param   args.START_INDEX     message index within topic to start from
        @param   args.END_INDEX       message index within topic to stop at
        @param   args.CONDITIONS      Python expressions that must evaluate as true
                                      for message to be processable
        @param   args.QUEUE_SIZE_IN   subscriber queue size
        @param   args.ROS_TIME_IN     stamp messages with ROS time instead of wall time
        @param   args.PROGRESS        whether to print progress bar
        """
        super(TopicSource, self).__init__(args)
        ConditionMixin.__init__(self, args)
        self._running = False  # Whether is in process of yielding messages from topics
        self._queue   = None   # [(topic, msg, ROS time)]
        self._subs    = {}     # {(topic, typename, typehash): ROS subscriber}

        self._configure()

    def read(self):
        """Yields messages from subscribed ROS topics, as (topic, msg, ROS time)."""
        if not self._running:
            self._running = True
            self._queue = queue.Queue()
            self.refresh_topics()
            t = threading.Thread(target=self._run_refresh)
            t.daemon = True
            t.start()

        total = 0
        self._init_progress()
        while self._running:
            topic, msg, stamp = self._queue.get()
            total += bool(topic)
            self._update_progress(total, running=self._running and bool(topic))
            if topic:
                topickey = rosapi.TypeMeta.make(msg, topic).topickey
                self._counts[topickey] += 1
                self.conditions_register_message(topic, msg)
                if self.is_conditions_topic(topic, pure=True): continue  # while

                yield topic, msg, stamp
                if self.args.NTH_MESSAGE > 1 or self.args.NTH_INTERVAL > 0:
                    self._processables[topickey] = (self._counts[topickey], stamp)
        self._queue = None
        self._running = False

    def bind(self, sink):
        """Attaches sink to source and blocks until connected to ROS live."""
        SourceBase.bind(self, sink)
        rosapi.init_node()

    def validate(self):
        """Returns whether ROS environment is set, prints error if not."""
        return rosapi.validate(live=True)

    def close(self):
        """Shuts down subscribers and stops producing messages."""
        self._running = False
        for k in list(self._subs):
            self._subs.pop(k).unregister()
        self._queue and self._queue.put((None, None, None))  # Wake up iterator
        self._queue = None
        ConditionMixin.close_batch(self)
        super(TopicSource, self).close()
        rosapi.shutdown_node()

    def get_meta(self):
        """Returns source metainfo data dict."""
        ENV = {k: os.getenv(k) for k in ("ROS_MASTER_URI", "ROS_DOMAIN_ID") if os.getenv(k)}
        return dict(ENV, tcount=len(self.topics))

    def get_message_meta(self, topic, index, stamp, msg):
        """Returns message metainfo data dict."""
        result = super(TopicSource, self).get_message_meta(topic, index, stamp, msg)
        topickey = (topic, result["type"], result["hash"])
        if topickey in self._subs:
            result.update(qoses=self._subs[topickey].get_qoses())
        return result

    def get_message_class(self, typename, typehash=None):
        """Returns message type class, from active subscription if available."""
        sub = next((s for (t, n, h), s in self._subs.items()
                    if n == typename and typehash in (s.get_message_type_hash(), None)), None)
        return sub and sub.get_message_class() or rosapi.get_message_class(typename)

    def get_message_definition(self, msg_or_type):
        """Returns ROS message type definition full text, including subtype definitions."""
        if rosapi.is_ros_message(msg_or_type):
            return rosapi.get_message_definition(msg_or_type)
        sub = next((s for (t, n, h), s in self._subs.items() if n == msg_or_type), None)
        return sub and sub.get_message_definition() or rosapi.get_message_definition(msg_or_type)

    def get_message_type_hash(self, msg_or_type):
        """Returns ROS message type MD5 hash."""
        if rosapi.is_ros_message(msg_or_type):
            return rosapi.get_message_type_hash(msg_or_type)
        sub = next((s for (t, n, h), s in self._subs.items() if n == msg_or_type), None)
        return sub and sub.get_message_type_hash() or rosapi.get_message_type_hash(msg_or_type)

    def format_meta(self):
        """Returns source metainfo string."""
        metadata = self.get_meta()
        result = "\nROS%s live" % os.getenv("ROS_VERSION")
        if "ROS_MASTER_URI" in metadata:
            result += ", ROS master %s" % metadata["ROS_MASTER_URI"]
        if "ROS_DOMAIN_ID" in metadata:
            result += ", ROS domain ID %s" % metadata["ROS_DOMAIN_ID"]
        result += ", %s initially" % plural("topic", metadata["tcount"])
        return result

    def is_processable(self, topic, index, stamp, msg):
        """Returns whether specified message in topic is in acceptable range."""
        if self.args.START_INDEX:
            if max(0, self.args.START_INDEX) >= index:
                return False
        if self.args.END_INDEX:
            if 0 < self.args.END_INDEX < index:
                return False
        if not super(TopicSource, self).is_processable(topic, index, stamp, msg):
            return False
        return ConditionMixin.is_processable(self, topic, index, stamp, msg)

    def refresh_topics(self):
        """Refreshes topics and subscriptions from ROS live."""
        for topic, typename in rosapi.get_topic_types():
            dct = filter_dict({topic: [typename]}, self.args.TOPICS, self.args.TYPES)
            if not filter_dict(dct, self.args.SKIP_TOPICS, self.args.SKIP_TYPES, reverse=True):
                continue  # for topic, typename
            try: rosapi.get_message_class(typename)  # Raises error in ROS2
            except Exception as e:
                ConsolePrinter.warn("Error loading type %s in topic %s: %%s" %
                                    (typename, topic), e, __once=True)
                continue  # for topic, typename
            topickey = (topic, typename, None)
            if topickey in self.topics:
                continue  # for topic, typename

            handler = functools.partial(self._on_message, topic)
            try:
                sub = rosapi.create_subscriber(topic, typename, handler,
                                               queue_size=self.args.QUEUE_SIZE_IN)
            except Exception as e:
                ConsolePrinter.warn("Error subscribing to topic %s: %%r" % topic,
                                    e, __once=True)
                continue  # for topic, typename
            self._subs[topickey] = sub
            self.topics[topickey] = None

    def _init_progress(self):
        """Initializes progress bar, if any."""
        if self.args.PROGRESS and not self.bar:
            self.bar = ProgressBar(afterword="ROS%s live" % os.getenv("ROS_VERSION"),
                                   aftertemplate=" {afterword}", pulse=True)
            self.bar.start()

    def _update_progress(self, count, running=True):
        """Updates progress bar, if any."""
        if self.bar:
            afterword = "ROS%s live, %s" % (os.getenv("ROS_VERSION"), plural("message", count))
            self.bar.afterword, self.bar.max = afterword, count
            if not running:
                self.bar.pause, self.bar.pulse_pos = True, None
            self.bar.update(count)

    def _configure(self):
        """Adjusts start/end time filter values to current time."""
        if self.args.START_TIME is not None:
            self.args.START_TIME = rosapi.make_live_time(self.args.START_TIME)
        if self.args.END_TIME is not None:
            self.args.END_TIME = rosapi.make_live_time(self.args.END_TIME)

    def _run_refresh(self):
        """Periodically refreshes topics and subscriptions from ROS live."""
        time.sleep(self.MASTER_INTERVAL)
        while self._running:
            try: self.refresh_topics()
            except Exception as e: self.thread_excepthook("Error refreshing live topics: %r" % e, e)
            time.sleep(self.MASTER_INTERVAL)

    def _on_message(self, topic, msg):
        """Subscription callback handler, queues message for yielding."""
        stamp = rosapi.get_rostime() if self.args.ROS_TIME_IN else \
                rosapi.make_time(time.time())
        self._queue and self._queue.put((topic, msg, stamp))
