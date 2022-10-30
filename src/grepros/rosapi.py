# -*- coding: utf-8 -*-
"""
ROS interface, shared facade for ROS1 and ROS2.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS1 bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     01.11.2021
@modified    17.10.2022
------------------------------------------------------------------------------
"""
## @namespace grepros.rosapi
import collections
import datetime
import decimal
import hashlib
import os
import re
import time

from . common import ConsolePrinter, Decompressor, filter_fields, memoize
#from . import ros1, ros2  # Imported conditionally


## Node base name for connecting to ROS (will be anonymized).
NODE_NAME = "grepros"

## Bagfile extensions to seek, including leading dot, populated after init
BAG_EXTENSIONS  = ()

## Bagfile extensions to skip, including leading dot, populated after init
SKIP_EXTENSIONS = ()

## All built-in numeric types in ROS
ROS_NUMERIC_TYPES = ["byte", "char", "int8", "int16", "int32", "int64", "uint8",
                     "uint16", "uint32", "uint64", "float32", "float64", "bool"]

## All built-in string types in ROS
ROS_STRING_TYPES = ["string", "wstring"]

## All built-in basic types in ROS
ROS_BUILTIN_TYPES = ROS_NUMERIC_TYPES + ROS_STRING_TYPES

## ROS time/duration types, populated after init
ROS_TIME_TYPES = []

## ROS1 time/duration types mapped to type names, populated after init
ROS_TIME_CLASSES = {}

## All built-in basic types plus time types in ROS, populated after init
ROS_COMMON_TYPES = []

## Mapping between type aliases and real types, like {"byte": "int8"} in ROS1
ROS_ALIAS_TYPES = {}

## Module grepros.ros1 or grepros.ros2
realapi = None


class TypeMeta(object):
    """
    Container for caching and retrieving message type metadata.

    All property values are lazy-loaded upon request.
    """

    ## SourceBase instance
    SOURCE = None

    ## Seconds before auto-clearing message from cache
    LIFETIME = 2

    ## {id(msg): MessageMeta()}
    _CACHE = {}

    ## {id(msg): [id(nested msg), ]}
    _CHILDREN = {}

    ## {id(msg): time.time() of registering}
    _TIMINGS = {}

    ## time.time() of last cleaning of stale messages
    _LASTSWEEP = time.time()

    def __init__(self, msg, topic=None, data=None):
        self._msg      = msg
        self._topic    = topic
        self._data     = data
        self._type     = None  # Message typename as "pkg/MsgType"
        self._def      = None  # Message type definition with full subtype definitions
        self._hash     = None  # Message type definition MD5 hash
        self._cls      = None  # Message class object
        self._topickey = None  # (topic, typename, typehash)
        self._typekey  = None  # (typename, typehash)

    def __enter__(self, *_, **__):
        """Allows using instance as a context manager (no other effect)."""
        return self

    def __exit__(self, *_, **__): pass

    @property
    def typename(self):
        """Returns message typename, as "pkg/MsgType"."""
        if not self._type:
            self._type = realapi.get_message_type(self._msg)
        return self._type

    @property
    def typehash(self):
        """Returns message type definition MD5 hash."""
        if not self._hash:
            hash = self.SOURCE and self.SOURCE.get_message_type_hash(self._msg)
            self._hash = hash or realapi.get_message_type_hash(self._msg)
        return self._hash

    @property
    def definition(self):
        """Returns message type definition text with full subtype definitions."""
        if not self._def:
            typedef = self.SOURCE and self.SOURCE.get_message_definition(self._msg)
            self._def = typedef or realapi.get_message_definition(self._msg)
        return self._def

    @property
    def data(self):
        """Returns message serialized binary, as bytes(), or None if not cached."""
        return self._data

    @property
    def typeclass(self):
        """Returns message class object."""
        if not self._cls:
            cls = self.SOURCE and self.SOURCE.get_message_class(self.typename, self.typehash)
            self._cls = cls or realapi.get_message_class(self.typename)
        return self._cls

    @property
    def topickey(self):
        """Returns (topic, typename, typehash) for message."""
        if not self._topickey:
            self._topickey = (self._topic, self.typename, self.typehash)
        return self._topickey

    @property
    def typekey(self):
        """Returns (typename, typehash) for message."""
        if not self._typekey:
            self._typekey = (self.typename, self.typehash)
        return self._typekey

    @classmethod
    def make(cls, msg, topic=None, root=None, data=None):
        """
        Returns TypeMeta instance, registering message in cache if not present.

        Other parameters are only required for first registration.

        @param   topic  topic the message is in if root message
        @param   root   root message that msg is a nested value of, if any
        @param   data   message serialized binary, if any
        """
        msgid = id(msg)
        if msgid not in cls._CACHE:
            cls._CACHE[msgid] = TypeMeta(msg, topic, data)
            if root and root is not msg:
                cls._CHILDREN.setdefault(id(root), set()).add(msgid)
            cls._TIMINGS[msgid] = time.time()
        result = cls._CACHE[msgid]
        cls.sweep()
        return result

    @classmethod
    def discard(cls, msg):
        """Discards message metadata from cache, if any, including nested messages."""
        msgid = id(msg)
        cls._CACHE.pop(msgid, None), cls._TIMINGS.pop(msgid, None)
        for childid in cls._CHILDREN.pop(msgid, []):
            cls._CACHE.pop(childid, None), cls._TIMINGS.pop(childid, None)
        cls.sweep()

    @classmethod
    def sweep(cls):
        """Discards stale messages from cache."""
        now = time.time()
        if not cls.LIFETIME or cls._LASTSWEEP < now - cls.LIFETIME: return

        for msgid, tm in list(cls._TIMINGS.items()):
            drop = (tm > now) or (tm < now - cls.LIFETIME)
            drop and (cls._CACHE.pop(msgid, None), cls._TIMINGS.pop(msgid, None))
            for childid in cls._CHILDREN.pop(msgid, []) if drop else ():
                cls._CACHE.pop(childid, None), cls._TIMINGS.pop(childid, None)
        cls._LASTSWEEP = now

    @classmethod
    def clear(cls):
        """Clears entire cache."""
        cls._CACHE.clear()
        cls._CHILDREN.clear()
        cls._TIMINGS.clear()


class Bag(object):
    """ROS bag creation wrapper."""

    ## Bag reader classes, as {Cls, }
    READER_CLASSES = set()

    ## Bag writer classes, as {Cls, }
    WRITER_CLASSES = set()

    def __new__(cls, filename, mode="r", decompress=False, reindex=False, progress=False):
        """
        Returns an object for reading or writing ROS bags.

        Result is rosbag.Bag in ROS1, or an object with a partially conforming API
        if using embag in ROS1, or if using ROS2.

        Plugins can add their own format support to READER_CLASSES and WRITER_CLASSES.
        Classes can have a static/class method `autodetect(filename)`
        returning whether given file is readable for the plugin class.

        Extra methods compared with rosbag.Bag: get_message_class(),
        get_message_definition(), get_message_type_hash(), and get_topic_info().

        @param   mode         return reader if "r" else writer
        @param   decompress   decompress archived bag to file directory
        @param   reindex      reindex unindexed bag (ROS1 only), making a backup if indexed format
        @param   progress     show progress bar with decompression or reindexing status
        """
        if Decompressor.is_compressed(filename):
            if decompress: filename = Decompressor.decompress(filename, progress)
            else: raise Exception("decompression not enabled")

        if "a" == mode and (not os.path.exists(filename) or not os.path.getsize(filename)):
            mode = "w"  # rosbag raises error on append if no file or empty file
            os.path.exists(filename) and os.remove(filename)
        classes = set(cls.READER_CLASSES if "r" == mode else cls.WRITER_CLASSES)
        for detect, mycls in ((d, c) for d in (True, False) for c in list(classes)):
            use, discard = not detect, False
            try:  # Try auto-detecting suitable class first
                if detect and callable(getattr(mycls, "autodetect", None)):
                    use, discard = mycls.autodetect(filename), True
                if use:
                    result = mycls(filename, mode=mode, reindex=reindex, progress=progress)
                    if result: return result
            except Exception as e:
                discard = True
                ConsolePrinter.warn("Failed to open %r for %s with %s: %s.",
                                    filename, "reading" if "r" == mode else "writing", mycls, e)
            discard and classes.discard(mycls)
        raise Exception("No suitable %s class available" % ("reader" if "r" == mode else "writer"))


def init_node(name=None):
    """
    Initializes a ROS1 or ROS2 node if not already initialized.

    In ROS1, blocks until ROS master available.
    """
    validate() and realapi.init_node(name or NODE_NAME)


def shutdown_node():
    """Shuts down live ROS node."""
    realapi and realapi.shutdown_node()


def validate(live=False):
    """
    Returns whether ROS environment is set, prints error if not.

    @param   live  whether environment must support launching a ROS node
    """
    global realapi, BAG_EXTENSIONS, SKIP_EXTENSIONS, \
           ROS_COMMON_TYPES, ROS_TIME_TYPES, ROS_TIME_CLASSES, ROS_ALIAS_TYPES
    if realapi:
        return True

    success, version = False, os.getenv("ROS_VERSION")
    if "1" == version:
        from . import ros1
        realapi = ros1
        success = realapi.validate()
    elif "2" == version:
        from . import ros2
        realapi = ros2
        success = realapi.validate(live)
    elif not version:
        ConsolePrinter.error("ROS environment not set: missing ROS_VERSION.")
    else:
        ConsolePrinter.error("ROS environment not supported: unknown ROS_VERSION %r.", version)
    if success:
        BAG_EXTENSIONS, SKIP_EXTENSIONS = realapi.BAG_EXTENSIONS, realapi.SKIP_EXTENSIONS
        ROS_COMMON_TYPES = ROS_BUILTIN_TYPES + realapi.ROS_TIME_TYPES
        ROS_TIME_TYPES   = realapi.ROS_TIME_TYPES
        ROS_TIME_CLASSES = realapi.ROS_TIME_CLASSES
        ROS_ALIAS_TYPES = realapi.ROS_ALIAS_TYPES
        Bag.READER_CLASSES.add(realapi.Bag)
        Bag.WRITER_CLASSES.add(realapi.Bag)
    return success


@memoize
def calculate_definition_hash(typename, msgdef, extradefs=()):
    """
    Returns MD5 hash for message type definition.

    @param   extradefs  additional subtype definitions as ((typename, msgdef), )
    """
    # "type name (= constvalue)?" or "type name (defaultvalue)?" (ROS2 format)
    FIELD_RGX = re.compile(r"^([a-z][^\s:]+)\s+([^\s=]+)(\s*=\s*([^\n]+))?(\s+([^\n]+))?", re.I)
    STR_CONST_RGX = re.compile(r"^w?string\s+([^\s=#]+)\s*=")
    lines, pkg = [], typename.rsplit("/", 1)[0]
    subtypedefs = dict(extradefs, **parse_definition_subtypes(msgdef))
    extradefs = tuple(subtypedefs.items())

    # First pass: write constants
    for line in msgdef.splitlines():
        if set(line) == set("="):  # Subtype separator
            break  # for line
        # String constants cannot have line comments
        if "#" in line and not STR_CONST_RGX.match(line): line = line[:line.index("#")]
        match = FIELD_RGX.match(line)
        if match and match.group(3):
            lines.append("%s %s=%s" % (match.group(1), match.group(2), match.group(4).strip()))
    # Second pass: write fields and subtype hashes
    for line in msgdef.splitlines():
        if set(line) == set("="):  # Subtype separator
            break  # for line
        if "#" in line and not STR_CONST_RGX.match(line): line = line[:line.index("#")]
        match = FIELD_RGX.match(line)
        if match and not match.group(3):  # Not constant
            scalartype, namestr = scalar(match.group(1)), match.group(2)
            if scalartype in ROS_COMMON_TYPES:
                typestr = match.group(1)
                if match.group(5): namestr = (namestr + " " + match.group(6)).strip()
            else:
                subtype = scalartype if "/" in scalartype else "std_msgs/Header" \
                          if "Header" == scalartype else "%s/%s" % (pkg, scalartype)
                typestr = calculate_definition_hash(subtype, subtypedefs[subtype], extradefs)
            lines.append("%s %s" % (typestr, namestr))
    return hashlib.md5("\n".join(lines).encode()).hexdigest()


def create_publisher(topic, cls_or_typename, queue_size):
    """Returns a ROS publisher instance, with .get_num_connections() and .unregister()."""
    return realapi.create_publisher(topic, cls_or_typename, queue_size)


def create_subscriber(topic, cls_or_typename, handler, queue_size):
    """
    Returns a ROS subscriber instance.

    Supplemented with .unregister(), .get_message_class(), .get_message_definition(),
    .get_message_type_hash(), and .get_qoses().
    """
    return realapi.create_subscriber(topic, cls_or_typename, handler, queue_size)


def format_message_value(msg, name, value):
    """
    Returns a message attribute value as string.

    Result is at least 10 chars wide if message is a ROS time/duration
    (aligning seconds and nanoseconds).
    """
    return realapi.format_message_value(msg, name, value)


def get_message_class(typename):
    """Returns ROS message class."""
    return realapi.get_message_class(typename)


def get_message_data(msg):
    """Returns ROS message as a serialized binary."""
    return realapi.get_message_data(msg)


def get_message_definition(msg_or_type):
    """Returns ROS message type definition full text, including subtype definitions."""
    return realapi.get_message_definition(msg_or_type)


def get_message_type_hash(msg_or_type):
    """Returns ROS message type MD5 hash."""
    return realapi.get_message_type_hash(msg_or_type)


def get_message_fields(val):
    """Returns OrderedDict({field name: field type name}) if ROS message, else {}."""
    return realapi.get_message_fields(val)


def get_message_type(msg_or_cls):
    """Returns ROS message type name, like "std_msgs/Header"."""
    return realapi.get_message_type(msg_or_cls)


def get_message_value(msg, name, typename):
    """Returns object attribute value, with numeric arrays converted to lists."""
    return realapi.get_message_value(msg, name, typename)


def get_rostime():
    """Returns current ROS time."""
    return realapi.get_rostime()


def get_ros_time_category(typename):
    """Returns "time" or "duration" for time/duration type, else typename."""
    if typename in ROS_TIME_TYPES:
        return "duration" if "duration" in typename.lower() else "time"
    return typename


def get_topic_types():
    """
    Returns currently available ROS topics, as [(topicname, typename)].

    Omits topics that the current ROS node itself has published.
    """
    return realapi.get_topic_types()


def get_type_alias(typename):
    """
    Returns alias like "char" for ROS built-in type, if any; reverse of get_type_alias().

    In ROS1, byte and char are aliases for int8 and uint8; in ROS2 the reverse.
    """
    return next((k for k, v in ROS_ALIAS_TYPES.items() if v == typename), None)


def get_alias_type(typename):
    """
    Returns ROS built-in type for alias like "char", if any; reverse of get_alias_type().

    In ROS1, byte and char are aliases for int8 and uint8; in ROS2 the reverse.
    """
    return ROS_ALIAS_TYPES.get(typename)


def is_ros_message(val, ignore_time=False):
    """
    Returns whether value is a ROS message or special like ROS time/duration.

    @param  ignore_time  whether to ignore ROS time/duration types
    """
    return realapi.is_ros_message(val, ignore_time)


def is_ros_time(val):
    """Returns whether value is a ROS2 time/duration."""
    return realapi.is_ros_time(val)


def iter_message_fields(msg, messages_only=False, scalars=(), top=()):
    """
    Yields ((nested, path), value, typename) from ROS message.

    @param  messages_only  whether to yield only values that are ROS messages themselves
                           or lists of ROS messages, else will yield scalar and list values
    @param  scalars        sequence of ROS types to consider as scalars, like ("time", duration")
    """
    fieldmap = realapi.get_message_fields(msg)
    if fieldmap is msg: return
    if messages_only:
        for k, t in fieldmap.items():
            v, scalart = realapi.get_message_value(msg, k, t), realapi.scalar(t)
            is_sublist = isinstance(v, (list, tuple)) and scalart not in ROS_COMMON_TYPES
            is_forced_scalar = get_ros_time_category(scalart) in scalars
            if not is_forced_scalar and realapi.is_ros_message(v):
                for p2, v2, t2 in iter_message_fields(v, True, scalars, top=top + (k, )):
                    yield p2, v2, t2
            if is_forced_scalar or is_sublist or realapi.is_ros_message(v, ignore_time=True):
                yield top + (k, ), v, t
    else:
        for k, t in fieldmap.items():
            v = realapi.get_message_value(msg, k, t)
            is_forced_scalar = get_ros_time_category(realapi.scalar(t)) in scalars
            if not is_forced_scalar and realapi.is_ros_message(v):
                for p2, v2, t2 in iter_message_fields(v, False, scalars, top=top + (k, )):
                    yield p2, v2, t2
            else:
                yield top + (k, ), v, t


def make_bag_time(stamp, bag):
    """
    Returns timestamp string or datetime instance as ROS time.

    Stamp interpreted as delta from bag start/end time if numeric string with sign prefix.
    """
    if isinstance(stamp, datetime.datetime):
        stamp, shift = time.mktime(stamp.timetuple()) + stamp.microsecond / 1E6, 0
    else:
        stamp, sign = float(stamp), ("+" == stamp[0] if stamp[0] in "+-" else None)
        shift = 0 if sign is None else bag.get_start_time() if sign else bag.get_end_time()
    return make_time(stamp + shift)


def make_live_time(stamp):
    """
    Returns timestamp string or datetime instance as ROS time.

    Stamp interpreted as delta from system time if numeric string with sign prefix.
    """
    if isinstance(stamp, datetime.datetime):
        stamp, shift = time.mktime(stamp.timetuple()) + stamp.microsecond / 1E6, 0
    else:
        stamp, sign = float(stamp), ("+" == stamp[0] if stamp[0] in "+-" else None)
        shift = 0 if sign is None else time.time()
    return make_time(stamp + shift)


def make_duration(secs=0, nsecs=0):
    """Returns a ROS duration."""
    return realapi.make_duration(secs=secs, nsecs=nsecs)


def make_time(secs=0, nsecs=0):
    """Returns a ROS time."""
    return realapi.make_time(secs=secs, nsecs=nsecs)


def make_message_hash(msg, include=(), exclude=()):
    """
    Returns hashcode for ROS message, as a hex digest.

    @param   include   message fields to include if not all, as [((nested, path), re.Pattern())]
    @param   exclude   message fields to exclude, as [((nested, path), re.Pattern())]
    """
    hasher = hashlib.md5()

    def walk_message(obj, top=()):
        fieldmap = get_message_fields(obj)
        fieldmap = filter_fields(fieldmap, include=include, exclude=exclude)
        for k, t in fieldmap.items():
            v, path = get_message_value(obj, k, t), top + (k, )
            if is_ros_message(v):
                walk_message(v, path)
            elif isinstance(v, (list, tuple)) and scalar(t) not in ROS_BUILTIN_TYPES:
                for x in v: walk_message(x, path)
            else:
                s = "%s=%s" % (path, v)
                hasher.update(s.encode("utf-8", errors="backslashreplace"))
        if not hasattr(obj, "__slots__"):
            s = "%s=%s" % (top, obj)
            hasher.update(s.encode("utf-8", errors="backslashreplace"))

    walk_message(msg)
    return hasher.hexdigest()


def message_to_dict(msg, replace=None):
    """
    Returns ROS message as nested Python dictionary.

    @param   replace  mapping of {value: replaced value},
                      e.g. {math.nan: None, math.inf: None}
    """
    result = {} if realapi.is_ros_message(msg) else msg
    for name, typename in realapi.get_message_fields(msg).items():
        v = realapi.get_message_value(msg, name, typename)
        if realapi.is_ros_time(v):
            v = dict(zip(["secs", "nsecs"], realapi.to_sec_nsec(v)))
        elif realapi.is_ros_message(v):
            v = message_to_dict(v)
        elif isinstance(v, (list, tuple)):
            if realapi.scalar(typename) not in ROS_BUILTIN_TYPES:
                v = [message_to_dict(x) for x in v]
            elif replace:
                v = [replace.get(x, x) for x in v]
        elif replace:
            v = replace.get(v, v)
        result[name] = v
    return result


@memoize
def parse_definition_fields(typename, typedef):
    """
    Returns field names and type names from a message definition text.

    Dpes not recurse into subtypes.

    @param   typename  ROS message type name, like "my_pkg/MyCls"
    @param   typedef   ROS message definition, like "Header header\nbool a\nMyCls2 b"
    @return            ordered {field name: type name},
                       like {"header": "std_msgs/Header", "a": "bool", "b": "my_pkg/MyCls2"}
    """
    result = collections.OrderedDict()  # {subtypename: subtypedef}

    FIELD_RGX = re.compile(r"^([a-z][^\s:]+)\s+([^\s=]+)(\s*=\s*([^\n]+))?(\s+([^\n]+))?", re.I)
    STR_CONST_RGX = re.compile(r"^w?string\s+([^\s=#]+)\s*=")
    pkg = typename.rsplit("/", 1)[0]
    for line in filter(bool, typedef.splitlines()):
        if set(line) == set("="):  # Subtype separator
            break  # for line
        if "#" in line and not STR_CONST_RGX.match(line): line = line[:line.index("#")]
        match = FIELD_RGX.match(line)
        if not match or match.group(3):  # Constant or not field
            continue  # for line

        name, typename, scalartype = match.group(2), match.group(1), scalar(match.group(1))
        if scalartype not in ROS_COMMON_TYPES:
            pkg2 = "" if "/" in scalartype else "std_msgs" if "Header" == scalartype else pkg
            typename = "%s/%s" % (pkg2, typename) if pkg2 else typename
        result[name] = typename
    return result


@memoize
def parse_definition_subtypes(typedef, nesting=False):
    """
    Returns subtype names and type definitions from a full message definition.

    @param   typename   message type name
    @param   typedef    message type definition including all subtype definitions
    @param   nesting    whether to additionally return type nesting information as
                        {typename: [typename contained in parent]}
    @return             {"pkg/MsgType": "full definition for MsgType including subtypes"}
                        or ({typedefs}, {nesting}) if nesting
    """
    result  = collections.OrderedDict()      # {subtypename: subtypedef}
    nesteds = collections.defaultdict(list)  # {subtypename: [subtypename2, ]})

    # Parse individual subtype definitions from full definition
    curtype, curlines = "", []
    # Separator line, and definition header like 'MSG: std_msgs/MultiArrayLayout'
    rgx = re.compile(r"^((=+)|(MSG: (.+)))$")  # Group 2: separator, 4: new type
    for line in typedef.splitlines():
        m = rgx.match(line)
        if m and m.group(2) and curtype:  # Separator line between nested definitions
            result[curtype] = "\n".join(curlines)
            curtype, curlines = "", []
        elif m and m.group(4):  # Start of nested definition "MSG: pkg/MsgType"
            curtype, curlines = m.group(4), []
        elif not m and curtype:  # Definition content
            curlines.append(line)
    if curtype:
        result[curtype] = "\n".join(curlines)

    # "type name (= constvalue)?" or "type name (defaultvalue)?" (ROS2 format)
    FIELD_RGX = re.compile(r"^([a-z][^\s]+)\s+([^\s=]+)(\s*=\s*([^\n]+))?(\s+([^\n]+))?", re.I)
    # Concatenate nested subtype definitions to parent subtype definitions
    for subtype, subdef in list(result.items()):
        pkg, seen = subtype.rsplit("/", 1)[0], set()
        for line in subdef.splitlines():
            m = FIELD_RGX.match(line)
            if m and m.group(1):
                scalartype, fulltype = realapi.scalar(m.group(1)), None
                if scalartype not in ROS_COMMON_TYPES:
                    fulltype = scalartype if "/" in scalartype else "std_msgs/Header" \
                               if "Header" == scalartype else "%s/%s" % (pkg, scalartype)
                if fulltype in result and fulltype not in seen:
                    addendum = "%s\nMSG: %s\n%s" % ("=" * 80, fulltype, result[fulltype])
                    result[subtype] = result[subtype].rstrip() + ("\n\n%s\n" % addendum)
                    nesteds[subtype].append(fulltype)
                    seen.add(fulltype)
    return (result, nesteds) if nesting else result


def scalar(typename):
    """
    Returns scalar type from ROS message data type, like "uint8" from uint8-array.

    Returns type unchanged if an ordinary type. In ROS2, returns unbounded type,
    e.g. "string" from "string<=10[<=5]".
    """
    return realapi.scalar(typename)


def set_message_value(obj, name, value):
    """Sets message or object attribute value."""
    realapi.set_message_value(obj, name, value)


def to_datetime(val):
    """Returns value as datetime.datetime if value is ROS time/duration, else value."""
    sec = realapi.to_sec(val)
    return datetime.datetime.fromtimestamp(sec) if sec is not val else val


def to_decimal(val):
    """Returns value as decimal.Decimal if value is ROS time/duration, else value."""
    if realapi.is_ros_time(val):
        return decimal.Decimal("%d.%09d" % realapi.to_sec_nsec(val))
    return val


def to_nsec(val):
    """Returns value in nanoseconds if value is ROS time/duration, else value."""
    return realapi.to_nsec(val)


def to_sec(val):
    """Returns value in seconds if value is ROS time/duration, else value."""
    return realapi.to_sec(val)


def to_sec_nsec(val):
    """Returns value as (seconds, nanoseconds) if value is ROS time/duration, else value."""
    return realapi.to_sec_nsec(val)
