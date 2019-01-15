from __future__ import absolute_import
from __future__ import print_function
import marshal
import binascii
import os
import socket
import time
import struct
import zlib
from dpark.utils.log import get_logger
from dpark.file_manager import open_file
from dpark.serialize import load_func, dump_func
from contextlib import closing
import six
from six.moves import range, cPickle

logger = get_logger(__name__)
try:
    import quicklz
except ImportError:
    quicklz = None

try:
    from fnv1a import get_hash
    from fnv1a import get_hash_beansdb


    def fnv1a(d):
        return get_hash(d) & 0xffffffff


    def fnv1a_beansdb(d):
        return get_hash_beansdb(d) & 0xffffffff
except ImportError:
    FNV_32_PRIME = 0x01000193
    FNV_32_INIT = 0x811c9dc5


    def fnv1a(d):
        h = FNV_32_INIT
        for c in six.iterbytes(d):
            h ^= c
            h *= FNV_32_PRIME
            h &= 0xffffffff
        return h


    fnv1a_beansdb = fnv1a

FLAG_PICKLE = 0x00000001
FLAG_INTEGER = 0x00000002
FLAG_LONG = 0x00000004
FLAG_BOOL = 0x00000008
FLAG_COMPRESS1 = 0x00000010  # by cmemcached
FLAG_MARSHAL = 0x00000020
FLAG_COMPRESS = 0x00010000  # by beansdb

PADDING = 256
BEANSDB_MAX_KEY_LENGTH = 250
BEANSDB_MAX_VALUE_LENGTH = 500 << 20


def check_size(ksz, vsz):
    if not (0 < ksz <= BEANSDB_MAX_KEY_LENGTH and 0 <= vsz <= BEANSDB_MAX_VALUE_LENGTH):
        return 'bad key/value size len(key)={} len(value)={}'.format(ksz, vsz)


def is_valid_key(key):
    if len(key) > BEANSDB_MAX_KEY_LENGTH:
        return False
    invalid_chars = b' \r\n\0'
    return not any(c in key for c in invalid_chars)


def restore_value(flag, val):
    if flag & FLAG_COMPRESS:
        val = quicklz.decompress(val)
    if flag & FLAG_COMPRESS1:
        val = zlib.decompress(val)

    if flag & FLAG_BOOL:
        val = bool(int(val))
    elif flag & FLAG_INTEGER:
        val = int(val)
    elif flag & FLAG_MARSHAL:
        val = marshal.loads(val)
    elif flag & FLAG_PICKLE:
        val = cPickle.loads(val)
    return val


def prepare_value(val, compress):
    flag = 0
    if isinstance(val, six.binary_type):
        pass
    elif isinstance(val, bool):
        flag = FLAG_BOOL
        val = str(int(val)).encode('utf-8')
    elif isinstance(val, six.integer_types):
        flag = FLAG_INTEGER
        val = str(val).encode('utf-8')
    elif isinstance(val, six.text_type):
        flag = FLAG_MARSHAL
        val = marshal.dumps(val, 2)
    else:
        try:
            val = marshal.dumps(val, 2)
            flag = FLAG_MARSHAL
        except ValueError:
            val = cPickle.dumps(val, -1)
            flag = FLAG_PICKLE

    if compress and len(val) > 1024:
        flag |= FLAG_COMPRESS
        val = quicklz.compress(val)

    return flag, val


def read_record(f, check_crc=False):
    block = f.read(PADDING)
    if len(block) < 24:  #
        return None, "EOF"
    crc, tstamp, flag, ver, ksz, vsz = struct.unpack("IiiiII", block[:24])
    err = check_size(ksz, vsz)
    if err:
        return None, err

    rsize = 24 + ksz + vsz
    if rsize & 0xff:
        rsize = ((rsize >> 8) + 1) << 8
    if rsize > PADDING:
        n = rsize - PADDING
        remain = f.read(n)
        if len(remain) != n:
            return None, "EOF data"
        block += remain
    if check_crc:
        crc32 = binascii.crc32(block[4:24 + ksz + vsz]) & 0xffffffff
        if crc32 != crc:
            return None, "crc wrong"
    key = block[24:24 + ksz]
    value = block[24 + ksz:24 + ksz + vsz]
    return (rsize, key, ((flag, value), ver, tstamp)), None


def write_record(f, key, flag, value, version, ts):
    err = check_size(len(key), len(value))
    if err:
        raise Exception(err)

    header = struct.pack('IIiII', ts, flag, version, len(key), len(value))
    crc32 = binascii.crc32(header)
    crc32 = binascii.crc32(key, crc32)
    crc32 = binascii.crc32(value, crc32) & 0xffffffff
    f.write(struct.pack("I", crc32))
    f.write(header)
    f.write(key)
    f.write(value)
    rsize = 24 + len(key) + len(value)
    if rsize & 0xff:
        f.write(b'\x00' * (PADDING - (rsize & 0xff)))
        rsize = ((rsize >> 8) + 1) << 8
    return rsize


class BeansdbReader(object):

    def __init__(self, path, key_filter=None, fullscan=False, raw=False):
        if key_filter is None:
            fullscan = True
        self.path = path
        self.key_filter = key_filter
        self.fullscan = fullscan
        self.raw = raw
        if not fullscan:
            hint = path[:-5] + '.hint'
            if not os.path.exists(hint) and not os.path.exists(hint + '.qlz'):
                self.fullscan = True

    def __getstate__(self):
        d = dict(self.__dict__)
        del d['key_filter']
        return d, dump_func(self.key_filter)

    def __setstate__(self, state):
        self.__dict__, code = state
        try:
            self.key_filter = load_func(code)
        except Exception:
            raise

    def read(self, begin, end):
        if self.fullscan:
            return self.full_scan(begin, end)
        hint = self.path[:-5] + '.hint.qlz'
        if os.path.exists(hint):
            return self.scan_hint(hint)
        hint = self.path[:-5] + '.hint'
        if os.path.exists(hint):
            return self.scan_hint(hint)
        return self.full_scan(begin, end)

    def scan_hint(self, hint_path):
        with open(hint_path, 'rb') as f:
            hint = f.read()

        if hint_path.endswith('.qlz'):
            try:
                hint = quicklz.decompress(hint)
            except ValueError as e:
                msg = str(e)
                if msg.startswith('compressed length not match'):
                    hint = hint[:int(msg.split('!=')[1])]
                    hint = quicklz.decompress(hint)

        key_filter = self.key_filter or (lambda x: True)
        with open(self.path, 'rb') as dataf:
            p = 0
            while p < len(hint):
                pos, ver, _ = struct.unpack("IiH", hint[p:p + 10])
                p += 10
                ksz = pos & 0xff
                key = hint[p: p + ksz]
                if key_filter(key):
                    dataf.seek(pos & 0xffffff00)
                    r, err = read_record(dataf)
                    if err is not None:
                        logger.error("read failed from %s at %d",
                                     self.path, pos & 0xffffff00)
                    else:
                        rsize, key, value = r
                        value, err = self.restore(value)
                        if not err:
                            yield key, value
                p += ksz + 1  # \x00

    def restore(self, value):
        err = None
        if not self.raw:
            try:
                value = restore_value(*value)
            except Exception as e:
                err = "restore expection: %s value %s" % (e, value)
                logger.error(err)
        return value, err

    def open_file(self):
        return open_file(self.path)

    def full_scan(self, begin, end):
        with closing(self.open_file()) as f:
            # try to find first record
            while True:
                f.seek(begin)
                r, err = read_record(f, check_crc=True)
                if err is None:
                    break
                begin += PADDING
                if begin >= end:
                    break
            if begin >= end:
                return

            f.seek(begin)
            key_filter = self.key_filter or (lambda x: True)
            while begin < end:
                r, err = read_record(f)
                if err:
                    logger.error('read error at %s pos: %d err: %s',
                                 self.path, begin, err)
                    if err == "EOF":
                        return
                    begin += PADDING
                    while begin < end:
                        f.seek(begin)
                        r, err = read_record(f, check_crc=True)
                        if err is not None:
                            break
                        begin += PADDING
                    continue
                size, key, value = r
                if key_filter(key):
                    value, err = self.restore(value)
                    if not err:
                        yield key, value
                begin += size


class BeansdbWriter(object):

    def __init__(self, path, depth, overwrite, compress=False,
                 raw=False, value_with_meta=False):
        self.path = path
        self.depth = depth
        self.overwrite = overwrite
        if not quicklz:
            compress = False
        self.compress = compress
        self.raw = raw
        self.value_with_meta = value_with_meta
        for i in range(16 ** depth):
            if depth > 0:
                ps = list(('%%0%dx' % depth) % i)
                p = os.path.join(path, *ps)
            else:
                p = path
            if os.path.exists(p):
                if overwrite:
                    for n in os.listdir(p):
                        if n[:3].isdigit():
                            os.remove(os.path.join(p, n))
            else:
                os.makedirs(p)

    def prepare(self, val):
        if self.raw:
            return val

        return prepare_value(val, self.compress)

    def write_record(self, f, key, value, now):
        if self.value_with_meta:
            value, version, ts = value
        else:
            version = 1
            ts = now
        if self.raw:
            flag, value = value
        else:
            flag, value = self.prepare(value)
        return write_record(f, key, flag, value, version, ts)

    def write_bucket(self, it, index):
        """ 0 <= index < 256
            yield from it
            write to  "*/%03d.data" % index
        """
        N = 16 ** self.depth
        if self.depth > 0:
            fmt = '%%0%dx' % self.depth
            ds = [os.path.join(self.path, *list(fmt % i)) for i in range(N)]
        else:
            ds = [self.path]
        pname = '%03d.data' % index
        tname = '.%03d.data.%s.tmp' % (index, socket.gethostname())
        p = [os.path.join(d, pname) for d in ds]
        tp = [os.path.join(d, tname) for d in ds]
        pos = [0] * N
        f = [open(t, 'wb', 1 << 20) for t in tp]
        now = int(time.time())
        hint = [[] for d in ds]

        bits = 32 - self.depth * 4
        for key, value in it:
            if not isinstance(key, (six.string_types, six.binary_type)):
                key = str(key)

            if isinstance(key, six.text_type):
                key = key.encode('utf-8')

            if not is_valid_key(key):
                logger.warning("ignored invalid key: %s", [key])
                continue

            i = fnv1a(key) >> bits

            hint[i].append(struct.pack("IIH", pos[i] + len(key), 1, 0) + key + b'\x00')
            pos[i] += self.write_record(f[i], key, value, now)
        for i in f:
            i.close()

        for i in range(N):
            if hint[i] and not os.path.exists(p[i]):
                os.rename(tp[i], p[i])
                hintdata = b''.join(hint[i])
                hint_path = os.path.join(os.path.dirname(p[i]), '%03d.hint' % index)
                if self.compress:
                    hintdata = quicklz.compress(hintdata)
                    hint_path += '.qlz'
                with open(hint_path, 'wb') as f:
                    f.write(hintdata)
            else:
                os.remove(tp[i])

        return sum([([p[i]] if hint[i] else []) for i in range(N)], [])
