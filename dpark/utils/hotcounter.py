from __future__ import absolute_import
from __future__ import print_function
import operator
import six
from six.moves import range


class HotCounter(object):
    def __init__(self, vs=None, limit=20):
        if vs is None:
            vs = []
        self.limit = limit
        self.total = {}
        self.updates = {}
        self._max = 0
        for v in vs:
            self.add(v)

    def add(self, v):
        c = self.updates.get(v, 0) + 1
        self.updates[v] = c
        if c > self._max:
            self._max = c

        if len(self.updates) > self.limit * 5 and self._max > 5:
            self._merge()

    def _merge(self):
        for k, c in six.iteritems(self.updates):
            if c > 1:
                self.total[k] = self.total.get(k, 0) + c
        self._max = 0
        self.updates = {}

        if len(self.total) > self.limit * 5:
            self.total = dict(self.top(self.limit * 3))

    def update(self, o):
        self._merge()
        if isinstance(o, HotCounter):
            o._merge()
        for k, c in six.iteritems(o.total):
            self.total[k] = self.total.get(k, 0) + c

    def top(self, limit):
        return sorted(list(self.total.items()), key=operator.itemgetter(1), reverse=True)[:limit]


def test():
    import random
    import math

    t = HotCounter()
    for j in range(10):
        c = HotCounter()
        for i in range(10000):
            v = int(math.sqrt(random.randint(0, 1000000)))
            c.add(v)
        t.update(c)
    for k, v in t.top(20):
        print(k, v)


if __name__ == '__main__':
    test()
