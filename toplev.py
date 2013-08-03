#!/usr/bin/python
# Copyright (c) 2012-2013, Intel Corporation
# Author: Andi Kleen
#
# This program is free software; you can redistribute it and/or modify it
# under the terms and conditions of the GNU General Public License,
# version 2, as published by the Free Software Foundation.
#
# This program is distributed in the hope it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# Do cycle decomposition on a workload: estimate on which part of the
# CPU pipeline it bottlenecks.
#
# must find ocperf in python module path. add to paths below if needed.
# Handles a variety of perf versions, but older ones have various limitations.

import sys, os, re, itertools, textwrap, types, platform, pty, subprocess
import exceptions
from collections import defaultdict, Counter
#sys.path.append("../pmu-tools")
import ocperf

ingroup_events = frozenset(["cycles", "instructions", "ref-cycles"])

def usage():
    print >>sys.stderr, """
Do cycle decomposition on a workload: estimate on which part of the
CPU pipeline it bottlenecks. The bottlenecks are expressed as a tree
with different levels (max 4).

Requires an Intel Sandy, Ivy Bridge, Haswell CPU.
It works best on Ivy Bridge currently.

Usage:
./toplev.py [-lX] [-v] [-d] [-o logfile] program
measure program
./toplev.py [-lX] [-v] [-d] [-o logfile] -a sleep X
measure whole system for X seconds
./toplev.py [-lX] [-v] [-d] [-o logfile] -p PID
measure pid PID

-o set output file
-v print everything
-d use detailed model if available
-lLEVEL only use events upto max level (max 4)
-x, CSV mode with separator ,
-Inum  Enable interval mode, printing output every num ms
--kernel Measure kernel code only
--user   Measure user code only
-g  Print group assignments

Other perf arguments allowed (see the perf documentation)
After -- perf arguments conflicting with toplevel can be used.

Some caveats:
The lower levels of the measurement tree are less reliable
than the higher levels.  They also rely on counter multi-plexing
and cannot use groups, which can cause larger measurement errors
with non steady state workloads.
(If you don't understand this terminology; it means measurements
are much less accurate and it works best with programs that primarily
do the same thing over and over)

In this case it's recommended to measure the program only after
the startup phase by profiling globally or attaching later.
level 1 or running without -d is generally the most reliable.

One of the events (even used by level 1) requires a recent enough
kernel that understands its counter constraints.  3.10+ is safe.
"""
    sys.exit(1)

def works(x):
    return os.system(x + " >/dev/null 2>/dev/null") == 0

class PerfFeatures:
    "Adapt to the quirks of various perf versions."
    group_support = False
    def __init__(self):
        self.output_supported = works("perf stat --output /dev/null true")
        self.logfd_supported = works("perf stat --log-fd 3 3>/dev/null true")
        # some broken perf versions log on stdin
        if not self.output_supported and not self.logfd_supported:
	    print >>sys.stderr, "perf binary is too old. please upgrade"
	    sys.exit(1)
        if self.output_supported:
            self.group_support = works("perf stat --output /dev/null -e '{cycles,branches}' true")

    def perf_output(self, plog):
        if self.logfd_supported:
            return "--log-fd X"
        elif self.output_supported:
            return "--output %s " % (plog,)

    def event_group(self, evlist):
        need_counters = set(evlist) - add_filter(ingroup_events)
	e = ",".join(evlist)
        if 1 < len(need_counters) <= cpu.counters and self.group_support:
            e = "{%s}" % (e,)
        return e
 
feat = PerfFeatures()
emap = ocperf.find_emap()
if not emap:
    sys.exit("Unknown CPU or CPU event map not found.")

logfile = None
print_all = False
dont_hide = False
detailed_model = False
max_level = 2
csv_mode = None
interval_mode = None
force = False
ring_filter = None
print_group = False

first = 1
while first < len(sys.argv):
    if sys.argv[first] == '-o':
        first += 1
        if first >= len(sys.argv):
            usage()
        logfile = sys.argv[first]
    elif sys.argv[first] == '-v':
        print_all = True
        dont_hide = True
    elif sys.argv[first] == '--force':
        force = True
    elif sys.argv[first] in ('--kernel', '--user'):
        ring_filter = sys.argv[first][2:]
    elif sys.argv[first] == '-d':
        detailed_model = True
    elif sys.argv[first] == '-g':
        print_group = True
    elif sys.argv[first].startswith("-l"):
        max_level = int(sys.argv[first][2:])
    elif sys.argv[first].startswith("-x"):
        csv_mode = sys.argv[first][2:]
        print_all = True
    elif sys.argv[first].startswith("-I"):
        interval_mode = sys.argv[first]
    elif sys.argv[first] == '--':
        first += 1
        break
    else:
        break
    first += 1

if len(sys.argv) - first <= 0:
    usage()

MAX_ERROR = 0.05

def check_ratio(l):
    if print_all:
        return True
    return 0 - MAX_ERROR < l < 1 + MAX_ERROR

class Output:
    "Generate output human readable or as CSV."
    def __init__(self, logfile, csv):
        self.csv = csv
        if logfile:
            self.logf = open(logfile, "w")
            self.terminal = False
        else:
            self.logf = sys.stderr
            self.terminal = self.logf.isatty()

    def s(self, area, hdr, s, remark="", desc=""):
        if self.csv:
            remark = self.csv + remark
            desc = self.csv + desc
            desc = re.sub(r"\s+", " ", desc)
            print >>self.logf,"%s%s%s%s%s" % (hdr, self.csv, s.strip(), remark, desc)
        else:
            if area:
                hdr = "%-7s %s" % (area, hdr)
            print >>self.logf, "%-42s\t%s %s" % (hdr + ":", s, remark)
            if desc:
                print >>self.logf, "\t" + desc

    def p(self, area, name, l, timestamp, remark, desc, title):
        fmtnum = lambda l: "%5s%%" % ("%2.2f" % (100.0 * l))

        if timestamp:
            sep = " "
            if self.csv:
                sep = self.csv
            self.logf.write("%6.9f%s" % (timestamp, sep))
        if title:
            if self.csv:
                self.logf.write(title + self.csv)
            else:
                self.logf.write("%-5s" % (title))
        if check_ratio(l):
	    self.s(area, name, fmtnum(l), remark, desc)
	else:
	    self.s(area, name, fmtnum(0), "mismeasured", "")

    def bold(self, s):
        if (not self.terminal) or self.csv:
            return s
        return '\033[1m' + s + '\033[0m'

    def warning(self, s):
        if not self.csv:
            print >>sys.stderr, self.bold("warning:") + " " + s

known_cpus = (
    ("snb", (42, )),
    ("jkt", (45, )),
    ("ivb", (58, )),
    ("ivt", (62, )),
    ("hsw", (60, 70, 71, 69 )),
    ("hsx", (63, )),
)

class CPU:
    counters = 0

    # overrides for easy regression tests
    def force_cpu(self):
        force = os.getenv("FORCECPU")
        if not force:
            return False
        self.cpu = None
        for i in known_cpus:
            if force == i[0]:
                self.cpu = i[0]
                break
        if self.cpu == None:
            print "Unknown FORCECPU ",force
        return True
       
    def force_counters(self):
        cnt = os.getenv("FORCECOUNTERS")
        if cnt:
            self.counters = int(cnt)

    def __init__(self):
        self.model = 0
        self.threads = 0 
        self.cpu = None
        self.ht = False
        forced_cpu = self.force_cpu()
        self.force_counters()
        cores = {}
        with open("/proc/cpuinfo", "r") as f:
            ok = 0
            for l in f:
                n = l.split()
                if len(n) < 3:
                    continue
                if (n[0], n[2]) == ("vendor_id", "GenuineIntel") and ok == 0:
                    ok += 1
                elif (len(n) > 3 and 
                        (n[0], n[1], n[3]) == ("cpu", "family", "6") and 
                        ok == 1):
                    ok += 1
                elif (n[0], n[1]) == ("model", ":") and ok == 2:
                    ok += 1
                    self.model = int(n[2])
                elif n[0] == "processor":
                    self.threads += 1
                elif (n[0], n[1]) == ("physical", "id"):
                    physid = int(n[3])
                elif (n[0], n[1]) == ("core", "id"):
                    key = (physid, int(n[3]),)
                    if key in cores:
                        cores[key] += 1
                    else:
                        cores[key] = 1
                    if cores[key] > 1:
                        self.ht = True
        if ok >= 3 and not forced_cpu:
            for i in known_cpus:
                if self.model in i[1]:
                    self.cpu = i[0]
                    break
        if self.counters == 0:
            if self.ht:
                self.counters = 4
            else:
                self.counters = 8

cpu = CPU()

class PerfRun:
    def execute(self, logfile, r):
	# XXX handle non logfd
	self.logfile = None
	if "--log-fd" in r:
	    outp, inp = pty.openpty()
	    n = r.index("--log-fd")
	    r[n + 1] = "%d" % (inp)
        l = map(lambda x: "'" + x + "'" if x.find("{") >= 0 else x,  r)
        if "--log-fd" in r:
            i = l.index('--log-fd')
            del l[i:i+2]
        print " ".join(l)
	self.perf = subprocess.Popen(r)
	if "--log-fd" not in r:
	     self.perf.wait()
	     self.perf = None
	     self.logfile = logfile
	     return open(logfile, "r")
        else:
	     os.close(inp)
             return os.fdopen(outp, 'r')

    def wait(self):
        ret = 0
	if self.perf:
	    ret = self.perf.wait()
        if self.logfile:	
            os.remove(self.logfile)
        return ret

filter_to_perf = {
    "kernel": "k",
    "user": "u",
}

fixed_counters = {
    "CPU_CLK_UNHALTED.THREAD": "cycles",
    "INST_RETIRED.ANY": "instructions",
    "CPU_CLK_UNHALTED.REF_TSC": "ref-cycles"
}

fixed_set = frozenset(fixed_counters.keys())

def filter_string():
    if ring_filter:
        return filter_to_perf[ring_filter]
    return ""

def add_filter(s):
    f = filter_string()
    if f:
        s = set(map(lambda x: x + ":" + f, s))
    return s

def raw_event(i):
    if i.count(".") > 0:
	if i in fixed_counters:
	    i = fixed_counters[i]
            if filter_string():
                i += ":" + filter_string()
            return i
        e = emap.getevent(i)
        if e == None:
            print >>sys.stderr, "%s not found" % (i,)
            if not force:
                sys.exit(1)
            return "cycles" # XXX 
        i = e.output(True, filter_string())
        emap.update_event(i, e)
    return i

# generate list of converted raw events from events string
def raw_events(evlist):
    return map(lambda x: raw_event(x), evlist)

def pwrap(s):
    print "\n".join(textwrap.wrap(s, 60))

def print_header(work, evlist):
    evnames0 = map(lambda obj: obj.evlist, work)
    evnames = set(itertools.chain(*evnames0))
    names = map(lambda obj: obj.__class__.__name__, work)
    pwrap(" ".join(names) + ": " + " ".join(evnames).lower() + 
            " [%d_counters]" % (len(evnames - fixed_set)))

def setup_perf(events, evstr):
    plog = "plog.%d" % (os.getpid(),)
    rest = ["-x,", "-e", evstr]
    if interval_mode:
        rest += [interval_mode]
    rest += sys.argv[first:]
    prun = PerfRun()
    perf = os.getenv("PERF")
    if not perf:
        perf = "perf"
    inf = prun.execute(plog, [perf, "stat"]  + feat.perf_output(plog).split(" ") + rest)
    return inf, prun

class Stat:
    def __init__(self):
        self.total = 0
        self.errors = Counter()

def print_not(a, count , msg, j):
     print >>sys.stderr, ("warning: %s[%s] %s %.2f%% in %d measurements"
                % (emap.getperf(j), j, msg, 100.0 * (float(count) / float(a.total)), a.total))

# XXX need to get real ratios from perf
def print_account(ad):
    for j in ad:
        a = ad[j]
        for e in a.errors:
            print_not(a, a.errors[e], e, j)

def is_event(l, n):
    if len(l) <= n:
        return False
    return re.match(r"(r[0-9a-f]+|cycles|instructions|ref-cycles)", l[n])

def execute(events, runner, out):
    account = defaultdict(Stat)
    inf, prun = setup_perf(events, runner.evstr)
    res = defaultdict(list)
    rev = defaultdict(list)
    prev_interval = 0.0
    interval = None
    title = ""
    while True:
        try:
            l = inf.readline()
            if not l:
	        break
        except exceptions.IOError:
	     # handle pty EIO
	     break
        except KeyboardInterrupt:
            continue
        if interval_mode:
            m = re.match(r"\s*([0-9.]+),(.*)", l)
            if m:
                interval = float(m.group(1))
                l = m.group(2)
                if interval != prev_interval:
                    if res:
                        for j in sorted(res.keys()):
                            runner.print_res(res[j], rev[j], out, prev_interval, j)
                        res = defaultdict(list)
                        rev = defaultdict(list)
                    prev_interval = interval
        n = l.split(",")
        # timestamp is already removed
        # -a --per-socket socket,numcpus,count,event,...
        # -a --per-core core,numcpus,count,event,...
        # -a -A cpu,count,event,...
        # count,event,...
        if is_event(n, 1):
            title, count, event = "", n[0], n[1]
        elif is_event(n, 3):
            title, count, event = n[0], n[2], n[3]
        elif is_event(n, 2):
            title, count, event = n[0], n[1], n[2]
        else:
            print "unparseable perf output"
            sys.stdout.write(l)
            continue
        event = event.rstrip()
        if re.match(r"[0-9]+", count):
            val = float(count)
        elif count.startswith("<"):
            account[event].errors[count.replace("<","").replace(">","")] += 1
            val = 0
        else:
            print "unparseable perf count"
            sys.stdout.write(l)
            continue
        account[event].total += 1
        res[title].append(val)
        rev[title].append(event)
    inf.close()
    ret = prun.wait()
    for j in sorted(res.keys()):
        runner.print_res(res[j], rev[j], out, interval, j)
    print_account(account)
    return ret

def ev_append(ev, level, obj):
    if not (ev, level) in obj.evlevels:
        obj.evlevels.append((ev, level))
    return 1

def canon_event(e):
    m = re.match(r"(.*):(.*)", e)
    if m:
        e = m.group(1)
    if e.upper() in fixed_counters:
        return fixed_counters[e.upper()]
    if e.endswith("_0"):
        e = e[:-2]
    return e.lower()

def lookup_res(res, rev, ev, index):
    assert canon_event(emap.getperf(rev[index])) == canon_event(ev)
    return res[index]

def add_key(k, x, y):
    k[x] = y

# dedup a and keep b uptodate
def dedup2(a, b):
    k = dict()
    map(lambda x, y: add_key(k, x, y), a, b)
    return k.keys(), map(lambda x: k[x], k.keys())

def cmp_obj(a, b):
    if a.level == b.level:
        return a.nc - b.nc
    return a.level - b.level

def update_res_map(evnum, objl, base):
    for obj in objl:
        for lev in obj.evlevels:
            r = raw_event(lev[0])
            if r in evnum:
                obj.res_map[lev] = base + evnum.index(r)

def get_levels(evlev):
    return map(lambda x: x[1], evlev)

def get_names(evlev):
    return map(lambda x: x[0], evlev)

def num_non_fixed(l):
    n = cpu.counters
    while len(set(l[:n]) - fixed_set) < cpu.counters and n < len(l):
        n += 1
    return n

class Runner:
    "Schedule measurements of event groups. Try to run multiple in parallel."
    def __init__(self, max_level):
        self.evnum = [] # flat global list
        self.evstr = ""
        self.olist = []
        self.max_level = max_level

    def run(self, obj):
	obj.thresh = 0.0
        if obj.level > self.max_level:
            return
        obj.res = None
        obj.res_map = dict()
        self.olist.append(obj)

    def split_groups(self, objl, evlev):
        if len(set(get_levels(evlev))) == 1:
            # when there is only a single left just fill groups
            while evlev:
                n = num_non_fixed(get_names(evlev))
		l = evlev[:n]
                self.add(objl, raw_events(get_names(l)), l)
                evlev = evlev[n:]
        else:
            # resubmit groups for each level
            max_level = max(get_levels(evlev))
            for l in range(1, max_level + 1):
                evl = filter(lambda x: x[1] == l, evlev)
                if evl:
                    self.add(objl, raw_events(get_names(evl)), evl)

    def add(self, objl, evnum, evlev):
        assert evlev
        # does not fit into a group. 
        if len(set(evnum) - add_filter(ingroup_events)) > cpu.counters:
            self.split_groups(objl, evlev)
            return
        base = len(self.evnum)
        evnum, evlev = dedup2(evnum, evlev)
        update_res_map(evnum, objl, base)
        self.evnum += evnum
        if self.evstr:
            self.evstr += ","
        self.evstr += feat.event_group(evnum)
        if print_group:
            print_header(objl, get_names(evlev))

    # collect the events by pre-computing the equation
    def collect(self):
        self.objects = {}
        for obj in self.olist:
            self.objects[obj.name] = obj
        for obj in self.olist:
            obj.evlevels = []
            obj.compute(lambda ev, level: ev_append(ev, level, obj))
            obj.evlist = map(lambda x: x[0], obj.evlevels)
            obj.evnum = raw_events(obj.evlist)
            obj.nc = len(set(obj.evnum) - add_filter(ingroup_events))

    # fit events into available counters
    # simple first fit algorithm
    def schedule(self, out):
        curobj = []
        curev = []
        curlev = []
        # sort objects by level and inside each level by num-counters
        solist = sorted(self.olist, cmp=cmp_obj)
        # try to fit each objects events into groups
        # that fit into the available CPU counters
        for obj in solist:
            # try adding another object to the current group
            newev = curev + obj.evnum
            newlev = curlev + obj.evlevels
            needed = len(set(newev) - add_filter(ingroup_events))
            # when the current group doesn't have enough free slots
            # or is already too large
            # start a new group
            if cpu.counters < needed and curobj:
                self.add(curobj, curev, curlev)
                # restart new group
                curobj = []
                curev = []
                curlev = []
                newev = obj.evnum
                newlev = obj.evlevels
            # commit the object to the group
            curobj.append(obj)
            curev = newev
            curlev = newlev
        if curobj:
            self.add(curobj, curev, curlev)

    def print_res(self, res, rev, out, timestamp, title=""):
        if len(res) == 0:
            print "Nothing measured?"
            return
        for obj in self.olist:
            if obj.res_map:
                obj.compute(lambda e, level:
                            lookup_res(res, rev, e, obj.res_map[(e, level)]))
                if obj.thresh or print_all:
                    val = obj.val
                    if not obj.thresh and not dont_hide:
                        val = 0.0
                    out.p(obj.area if 'area' in obj.__class__.__dict__ else None,
                          obj.name, val, timestamp,
                          "below" if not obj.thresh else "above",
                          obj.desc[1:].replace("\n","\n\t"),
                          title)
                elif not print_all:
                    obj.thresh = 0 # hide children too
            else:
                out.warning("%s not measured" % (obj.__class__.__name__,))

def sysctl(name):
    try:
        f = open("/proc/sys/" + name.replace(".","/"), "r")
        val = int(f.readline())
        f.close()
    except IOError:
        return 0
    return val

# check nmi watchdog
if sysctl("kernel.nmi_watchdog") != 0:
    print >>sys.stderr,"Please disable nmi watchdog (echo 0 > /proc/sys/kernel/nmi_watchdog)"
    sys.exit(1)

if detailed_model:
    version = map(int, platform.release().split(".")[:2])
    if version[0] < 3 or (version[0] == 3 and version[1] < 10):
        print >>sys.stderr, "Older kernel than 3.10. Events may not be correctly scheduled."

if cpu.cpu == None:
    print >>sys.stderr, "Unsupported CPU model %d" % (cpu.model,)
    sys.exit(1)
    
runner = Runner(max_level)

if cpu.cpu == "ivb" and detailed_model:
    import ivb_client_ratios
    ev = ivb_client_ratios.Setup(runner)
elif cpu.cpu == "ivt" and detailed_model:
    import ivb_server_ratios
    ev = ivb_server_ratios.Setup(runner)
elif cpu.cpu == "snb" and detailed_model:
    import snb_client_ratios
    ev = snb_client_ratios.Setup(runner)
elif cpu.cpu == "hsw" and detailed_model:
    import hsw_client_ratios
    ev = hsw_client_ratios.Setup(runner)
else:
    import simple_ratios
    ev = simple_ratios.Setup(runner)

runner.collect()
out = Output(logfile, csv_mode)
runner.schedule(out)
sys.exit(execute(runner.evnum, runner, out))
