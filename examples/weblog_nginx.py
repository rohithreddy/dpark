#!/usr/bin/env python
import sys, os, os.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date,timedelta
from dpark import DparkContext
import pickle
dpark = DparkContext()

webservers = ['bifur', 'bofur', 'faramir']
log_path = [
    "/mfs/log/nginx-log/current/%s/access_log-%s.bz2",
    "/mfs/log/nginx-log/current/%s/mobile_access_log-%s.bz2",
]

def peek(day):
    after = (day+timedelta(days=1)).strftime('%d/%b/%Y')
    day = day.strftime('%d/%b/%Y')
    def func(lines):
        for line in lines:
            t = line.split(' ', 3)[2]
            d = t[1:12]
            if d == day:
                yield line
            elif d == after:
                return
    return func


today = date.today()
for i in range(3, 15):
    day = today - timedelta(days=i)
    path = '/mfs/log/weblog/%s/' % day.strftime("%Y/%m/%d")
    #print 'target', path
    if not os.path.exists(path):
        os.makedirs(path)
    target = dpark.textFile(path)
    if len(target) > 80:
        continue
    try:
        logs = [dpark.bzip2File(p % (h,d.strftime("%Y%m%d")), splitSize=51<<20)
                 for d in (day, day+timedelta(days=1))
                 for p in log_path
                 for h in webservers]
        rawlog = dpark.union(logs)
        rawlog = rawlog.glom().flatMap(peek(day))
        weblog = rawlog.pipe('/mfs/log/nginx-log/format_access_log --stream')
        weblog.saveAsTextFile(path)
    except IOError:
        pass